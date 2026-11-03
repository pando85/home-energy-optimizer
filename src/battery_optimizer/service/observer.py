"""Per-slot observation of what actually happened (the native behaviour).

The store's listener feeds every accepted or refreshed state here, in source-time
order.  When a slot closes, its row is computed from those samples:

* **Energy from counters first.**  Grid import (and battery charge/discharge when
  mapped) is the difference of the cumulative counter at the two slot edges, the
  edge value being the last reading at or before the edge and no older than
  ``counter_max_age_s``.  Only when a counter edge is unknown does the row fall
  back to integrating power (sample-and-hold, each sample held at most its
  freshness limit), and it says so in ``*_energy_source``.
* **SOC at the edges** is the last fresh reading at or before each edge.
* Every value carries its coverage; nothing missing is filled with zero.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import ServiceConfig
from .entity_store import EntityState, UpdateResult

__all__ = ["SlotObserver", "SlotMeasurements", "normalise_value"]

_POWER_SCALE = {"w": 1.0, "kw": 1000.0, "mw": 1_000_000.0}
_ENERGY_SCALE = {"kwh": 1.0, "wh": 0.001, "mwh": 1000.0}

#: How much sample history is kept per entity (enough for two slots plus holds).
_RETENTION = timedelta(hours=3)


def normalise_value(role: str, state: EntityState) -> float | None:
    """Numeric value in canonical units (W, kWh, SOC fraction); ``None`` if unusable."""
    value = state.numeric()
    if value is None:
        return None
    unit = str(state.attributes.get("unit_of_measurement") or "").strip().lower()
    if role == "battery_soc":
        if unit in ("%", ""):
            return value / 100.0 if unit == "%" or value > 1.0 else value
        return None
    if role.endswith("_power"):
        scale = _POWER_SCALE.get(unit or "w")
        return None if scale is None else value * scale
    if role.endswith("_energy"):
        scale = _ENERGY_SCALE.get(unit or "kwh")
        return None if scale is None else value * scale
    return value


@dataclass(frozen=True)
class SlotMeasurements:
    slot_start: datetime
    slot_end: datetime
    grid_kwh: float | None
    grid_energy_source: str | None
    grid_power_avg_w: float | None
    grid_power_coverage: float
    battery_charge_kwh: float | None
    battery_discharge_kwh: float | None
    battery_energy_source: str | None
    battery_power_avg_w: float | None
    battery_power_coverage: float
    soc_start_frac: float | None
    soc_end_frac: float | None
    context: dict[str, str] = field(default_factory=dict)

    @property
    def battery_net_kwh(self) -> float | None:
        if self.battery_charge_kwh is None or self.battery_discharge_kwh is None:
            return None
        return self.battery_charge_kwh - self.battery_discharge_kwh

    def gross_load_kwh(self, battery_mapped: bool) -> float | None:
        """Household load: grid import minus battery charge plus battery discharge."""
        if self.grid_kwh is None:
            return None
        if not battery_mapped:
            return self.grid_kwh
        if self.battery_charge_kwh is None or self.battery_discharge_kwh is None:
            return None
        return self.grid_kwh - self.battery_charge_kwh + self.battery_discharge_kwh

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid_kwh": self.grid_kwh,
            "grid_energy_source": self.grid_energy_source,
            "grid_power_avg_w": self.grid_power_avg_w,
            "grid_power_coverage": self.grid_power_coverage,
            "battery_charge_kwh": self.battery_charge_kwh,
            "battery_discharge_kwh": self.battery_discharge_kwh,
            "battery_energy_source": self.battery_energy_source,
            "battery_power_avg_w": self.battery_power_avg_w,
            "battery_power_coverage": self.battery_power_coverage,
            "soc_start_frac": self.soc_start_frac,
            "soc_end_frac": self.soc_end_frac,
            "context": dict(self.context),
        }


class SlotObserver:
    """Collects role-tagged samples and summarises closed slots."""

    def __init__(self, config: ServiceConfig) -> None:
        self._config = config
        self._roles = config.entities.configured()
        self._role_by_entity = {e: r for r, e in self._roles.items()}
        self._observed = set(config.entities.observed)
        self._samples: dict[str, deque[tuple[datetime, float | None]]] = {
            role: deque() for role in self._roles
        }
        self._context: dict[str, EntityState] = {}
        self.late_samples = 0
        self._closed_until: datetime | None = None

    # -- intake ----------------------------------------------------------------

    def on_state(self, state: EntityState, result: UpdateResult) -> None:
        if state.entity_id in self._observed:
            self._context[state.entity_id] = state
        role = self._role_by_entity.get(state.entity_id)
        if role is None or role == "price":
            return
        at = state.last_updated if result is UpdateResult.ACCEPTED else state.observed_at
        if self._closed_until is not None and at < self._closed_until:
            self.late_samples += 1
        series = self._samples[role]
        if series and at < series[-1][0]:
            return  # the store already orders per entity; defensive only
        series.append((at, normalise_value(role, state)))

    def prune(self, now: datetime) -> None:
        horizon = now - _RETENTION
        for series in self._samples.values():
            while len(series) > 1 and series[1][0] <= horizon:
                series.popleft()

    # -- reductions --------------------------------------------------------------

    def _edge_value(self, role: str, edge: datetime, max_age_s: float) -> float | None:
        series = self._samples.get(role)
        if not series:
            return None
        best: tuple[datetime, float | None] | None = None
        for at, value in series:
            if at <= edge:
                best = (at, value)
            else:
                break
        if best is None or best[1] is None:
            return None
        if (edge - best[0]).total_seconds() > max_age_s:
            return None
        return best[1]

    def _integral(
        self, role: str, start: datetime, end: datetime, hold_s: float
    ) -> tuple[float | None, float]:
        """``(average_value, coverage_fraction)`` with bounded sample-and-hold."""
        series = list(self._samples.get(role) or ())
        span = (end - start).total_seconds()
        if not series or span <= 0:
            return None, 0.0
        area = 0.0
        covered = 0.0
        for i, (at, value) in enumerate(series):
            if value is None:
                continue
            nxt = series[i + 1][0] if i + 1 < len(series) else end
            seg_start = max(at, start)
            seg_end = min(nxt, at + timedelta(seconds=hold_s), end)
            dt = (seg_end - seg_start).total_seconds()
            if dt > 0:
                area += value * dt
                covered += dt
        if covered <= 0:
            return None, 0.0
        return area / covered, covered / span

    def _counter_delta(self, role: str, start: datetime, end: datetime) -> float | None:
        max_age = self._config.observation.counter_max_age_s
        v0 = self._edge_value(role, start, max_age)
        v1 = self._edge_value(role, end, max_age)
        if v0 is None or v1 is None or v1 < v0:
            return None  # unknown edge, or a counter reset: unknowable, not zero
        return v1 - v0

    def measure(self, slot_start: datetime, slot_end: datetime) -> SlotMeasurements:
        cfg = self._config
        min_cov = 0.9
        span_h = (slot_end - slot_start).total_seconds() / 3600.0

        grid_avg, grid_cov = (None, 0.0)
        if "grid_power" in self._roles:
            grid_avg, grid_cov = self._integral(
                "grid_power", slot_start, slot_end, cfg.freshness_for("grid_power")
            )
        grid_kwh: float | None = None
        grid_source: str | None = None
        if "grid_energy" in self._roles:
            grid_kwh = self._counter_delta("grid_energy", slot_start, slot_end)
            grid_source = "counter" if grid_kwh is not None else None
        if grid_kwh is None and grid_avg is not None and grid_cov >= min_cov:
            # Fallback only, and labelled: power is a crosscheck signal, not an energy source.
            grid_kwh = max(0.0, grid_avg) * span_h / 1000.0
            grid_source = "power_integral"

        charge_kwh: float | None = None
        discharge_kwh: float | None = None
        battery_source: str | None = None
        if "battery_charge_energy" in self._roles:
            charge_kwh = self._counter_delta("battery_charge_energy", slot_start, slot_end)
            discharge_kwh = self._counter_delta("battery_discharge_energy", slot_start, slot_end)
            if charge_kwh is not None and discharge_kwh is not None:
                battery_source = "counter"
            else:
                charge_kwh = discharge_kwh = None

        batt_avg: float | None = None
        batt_cov = 0.0
        hold = cfg.freshness_for("battery_power")
        if "battery_power" in self._roles:
            batt_avg, batt_cov = self._integral("battery_power", slot_start, slot_end, hold)
        elif "battery_charge_power" in self._roles:
            c_avg, c_cov = self._integral("battery_charge_power", slot_start, slot_end, hold)
            d_avg, d_cov = self._integral("battery_discharge_power", slot_start, slot_end, hold)
            if c_avg is not None and d_avg is not None:
                batt_avg = abs(c_avg) - abs(d_avg)
                batt_cov = min(c_cov, d_cov)
                if battery_source is None and batt_cov >= min_cov:
                    charge_kwh = abs(c_avg) * span_h / 1000.0
                    discharge_kwh = abs(d_avg) * span_h / 1000.0
                    battery_source = "power_integral"
        if battery_source is None and batt_avg is not None and batt_cov >= min_cov:
            charge_kwh = max(batt_avg, 0.0) * span_h / 1000.0
            discharge_kwh = max(-batt_avg, 0.0) * span_h / 1000.0
            battery_source = "power_integral"

        soc_limit = cfg.freshness_for("battery_soc")
        soc_age = math.inf if soc_limit is None else soc_limit  # None: availability only
        soc_start = soc_end = None
        if "battery_soc" in self._roles:
            soc_start = self._edge_value("battery_soc", slot_start, soc_age)
            soc_end = self._edge_value("battery_soc", slot_end, soc_age)

        context = {
            entity: state.state
            for entity, state in self._context.items()
            if state.last_updated <= slot_end
        }
        self._closed_until = slot_end
        return SlotMeasurements(
            slot_start=slot_start,
            slot_end=slot_end,
            grid_kwh=grid_kwh,
            grid_energy_source=grid_source,
            grid_power_avg_w=grid_avg,
            grid_power_coverage=round(grid_cov, 4),
            battery_charge_kwh=charge_kwh,
            battery_discharge_kwh=discharge_kwh,
            battery_energy_source=battery_source,
            battery_power_avg_w=batt_avg,
            battery_power_coverage=round(batt_cov, 4),
            soc_start_frac=soc_start,
            soc_end_frac=soc_end,
            context=context,
        )
