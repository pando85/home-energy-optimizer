"""Prometheus history adapter and the causal load forecast.

The forecaster needs measured gross household load for the trailing window.  It
is read from the long-term Prometheus archive with the **same** primitives the
offline pipeline uses, so live and offline agree on what a kWh is:

* raw samples via :func:`battery_optimizer.extract_prom.query_range_vector`
  (instant queries over range vectors — never ``query_range``, which fabricates
  samples on its step grid), chunked by :func:`~battery_optimizer.extract_prom.iter_chunks`;
* stitched by :func:`battery_optimizer.build.stitch_series`, turned into counter
  deltas by :func:`~battery_optimizer.build.counter_deltas` and allocated onto the
  UTC quarter-hour grid by :func:`~battery_optimizer.build.allocate_energy`
  (energy from counter deltas only, gaps and resets left unknown, never invented).

The forecast itself runs through the load-forecaster seam
(:func:`battery_optimizer.load_forecast.run_forecaster`) — the same seam the day-ahead
policy and the rolling-horizon benchmark's causal mode use — with the information cut
at the **local midnight of the decision day**, which is exactly
:class:`~battery_optimizer.rolling_horizon.CausalForecastProvider`'s cutoff rule.
Nothing measured today enters today's or tomorrow's forecast.  The forecaster is
``planning.load_forecaster`` (default ``trailing_mean``, LF-0); oracle forecasters are
refused here as well as at config validation.

Gross load: with no battery telemetry mapped, gross load is the grid import
counter (the offline build's ``battery_absent`` assumption, recorded as the
``basis``).  With battery energy counters mapped it is
``grid_import - battery_charge + battery_discharge`` per slot, and a slot where
any of the three is unknown is unusable.  A slot that ends at or before
``battery.commissioned_at`` is the grid counter alone, as in the offline build: the
battery could not exchange energy yet, and its counters may not exist that far back.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from battery_optimizer import build as build_mod
from battery_optimizer import extract_prom
from battery_optimizer.load_forecast import (
    DEFAULT_LOAD_FORECASTER,
    resolve_forecaster_params,
    run_forecaster,
)
from battery_optimizer.policies import contiguous_day_bounds

from .timegrid import local_date, local_day_bounds_utc, slot_range

__all__ = [
    "BASIS_GRID_ONLY",
    "BASIS_GRID_PLUS_BATTERY",
    "ForecastUnavailableError",
    "HistoryFetcher",
    "LoadForecast",
    "LoadHistory",
    "PrometheusHistory",
    "fetch_points",
    "forecast_load",
]

BASIS_GRID_ONLY = (
    "grid_import_counter (battery absent from history: gross load == grid import, the "
    "offline build's battery_absent assumption)"
)
BASIS_GRID_PLUS_BATTERY = "grid_import_counter - battery_charge_counter + battery_discharge_counter"

ENERGY_METRIC = "hass_sensor_energy_kwh"
PRICE_METRIC = "hass_sensor_unit_u0x20ac_per_kwh"


class ForecastUnavailableError(RuntimeError):
    """No honest load forecast can be produced for the requested slots."""


@dataclass(frozen=True)
class LoadHistory:
    """Trailing gross-load history on the slot grid, cut at a local midnight."""

    starts: tuple[datetime, ...]
    load_kwh: np.ndarray
    usable: np.ndarray
    cutoff: datetime
    """Local midnight of the day this history was fetched for (exclusive end)."""
    cutoff_day: date
    window_days: int
    basis: str
    fetched_at: datetime
    mean_price_eur_per_kwh: float | None = None
    price_samples: int = 0
    notes: tuple[str, ...] = ()

    @property
    def usable_slots(self) -> int:
        return int(self.usable.sum())

    def summary(self) -> dict[str, Any]:
        return {
            "cutoff": self.cutoff.isoformat(),
            "cutoff_day": self.cutoff_day.isoformat(),
            "window_days": self.window_days,
            "slots": len(self.starts),
            "usable_slots": self.usable_slots,
            "basis": self.basis,
            "fetched_at": self.fetched_at.isoformat(),
            "mean_price_eur_per_kwh": self.mean_price_eur_per_kwh,
            "price_samples": self.price_samples,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class LoadForecast:
    starts: tuple[datetime, ...]
    load_kwh: tuple[float, ...]
    meta: dict[str, Any] = field(default_factory=dict)


def forecast_load(
    history: LoadHistory,
    target_starts: Sequence[datetime],
    *,
    tz: ZoneInfo,
    window_days: int,
    interval_seconds: int = 900,
    forecaster: str = DEFAULT_LOAD_FORECASTER,
) -> LoadForecast:
    """Causal load forecast for *target_starts* from the registered *forecaster*.

    Raises:
        ForecastUnavailableError: a target lies before the history cutoff, or a target
            slot would get the zero-history fallback (no usable data at all).
        ValueError: *forecaster* is unknown or an oracle.
    """
    if not target_starts:
        raise ForecastUnavailableError("no target slots")
    if min(target_starts) < history.cutoff:
        raise ForecastUnavailableError(
            f"history cut at {history.cutoff.isoformat()} is newer than the first target slot"
        )
    end = max(target_starts) + timedelta(seconds=interval_seconds)
    _, last_day_end = local_day_bounds_utc(local_date(end - timedelta(seconds=1), tz), tz)
    future = slot_range(history.cutoff, last_day_end, interval_seconds)
    starts = list(history.starts) + future
    load = np.concatenate([np.nan_to_num(history.load_kwh, nan=0.0), np.zeros(len(future))])
    usable = np.concatenate([history.usable, np.zeros(len(future), dtype=bool)])
    labels = [local_date(s, tz).isoformat() for s in starts]
    days = contiguous_day_bounds(labels)
    target_labels = {local_date(s, tz).isoformat() for s in target_starts}
    result = run_forecaster(
        forecaster,
        load_kwh=load,
        usable=usable,
        days=days,
        cutoff_idx=len(history.starts),
        target_days=[j for j, (label, _lo, _hi) in enumerate(days) if label in target_labels],
        params=resolve_forecaster_params(forecaster, window_days=window_days),
    )
    forecast, per_day = result.values, result.per_day
    index = {s: i for i, s in enumerate(starts)}
    values: list[float] = []
    for s in target_starts:
        i = index.get(s)
        if i is None:
            raise ForecastUnavailableError(f"target slot {s.isoformat()} is off the slot grid")
        values.append(float(forecast[i]))
    diag = [d for d in per_day if d["local_day"] in target_labels]
    if any(d["slots_zero_no_history"] for d in diag):
        raise ForecastUnavailableError(
            "no usable load history for some target slots: refusing to plan on a zero forecast"
        )
    return LoadForecast(
        starts=tuple(target_starts),
        load_kwh=tuple(values),
        meta={
            "method": (
                "trailing_slot_mean_causal (policies.forecast_load_trailing_slot_mean)"
                if forecaster == DEFAULT_LOAD_FORECASTER
                else f"{forecaster}_causal (load_forecast.run_forecaster)"
            ),
            "load_forecaster": {k: result.provenance[k] for k in ("name", "params", "oracle")},
            "cutoff": history.cutoff.isoformat(),
            "window_days": window_days,
            "basis": history.basis,
            "days": [
                {
                    k: d[k]
                    for k in (
                        "local_day",
                        "window_days_with_usable_data",
                        "slots_pooled_fallback",
                        "slots_zero_no_history",
                        "fallback",
                    )
                }
                for d in diag
            ],
        },
    )


HistoryFetcher = Callable[[datetime, date], LoadHistory]


def fetch_points(
    client: httpx.Client,
    base_url: str,
    entity: str,
    metric: str,
    start: datetime,
    end: datetime,
) -> list[build_mod.RawPoint]:
    """Raw samples of one entity in ``[start, end)``: instant queries over day-long range
    vectors (never ``query_range``), non-finite values dropped.  *base_url* is resolved."""
    spec = extract_prom.EntitySpec(entity, metric, "")
    points: list[build_mod.RawPoint] = []
    for c_start, c_end in extract_prom.iter_chunks(start, end, 86400):
        window = extract_prom.range_window_seconds(c_start, c_end)
        matrix = extract_prom.query_range_vector(
            client,
            base_url=base_url,
            query=extract_prom.build_range_query(spec, window),
            time_us=extract_prom.to_micros(c_end),
        )
        lo, hi = extract_prom.to_micros(c_start), extract_prom.to_micros(c_end)
        for series in matrix:
            labels = {str(k): str(v) for k, v in (series.get("metric") or {}).items()}
            key = extract_prom.compute_series_key(labels)
            for raw_ts, raw_value in series.get("values") or []:
                ts_us = extract_prom.parse_epoch_time(raw_ts)
                if not lo <= ts_us < hi:
                    continue
                value = float(raw_value)
                if not math.isfinite(value):
                    continue
                points.append(
                    build_mod.RawPoint(
                        ts=extract_prom.from_micros(ts_us), value=value, series_key=key
                    )
                )
    return points


class PrometheusHistory:
    """Synchronous Prometheus reader (run it in a worker thread, never on the loop)."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        tz: ZoneInfo,
        window_days: int,
        grid_energy_entity: str,
        price_entity: str | None,
        battery_charge_energy_entity: str | None = None,
        battery_discharge_energy_entity: str | None = None,
        battery_commissioned_at: datetime | None = None,
        interval_seconds: int = 900,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._base_url = extract_prom.resolve_base_url(base_url)
        self._timeout = timeout_s
        self._tz = tz
        self._window_days = window_days
        self._grid = grid_energy_entity
        self._price = price_entity
        self._charge = battery_charge_energy_entity
        self._discharge = battery_discharge_energy_entity
        self._commissioned_at = battery_commissioned_at
        self._interval = interval_seconds
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0))
        )
        if bool(self._charge) != bool(self._discharge):
            raise ValueError("battery charge and discharge energy entities go together")

    # -- raw samples ---------------------------------------------------------

    def _points(
        self, client: httpx.Client, entity: str, metric: str, start: datetime, end: datetime
    ) -> list[build_mod.RawPoint]:
        return fetch_points(client, self._base_url, entity, metric, start, end)

    def _slot_energy(
        self, client: httpx.Client, entity: str, start: datetime, end: datetime
    ) -> tuple[list[datetime], list[float | None]]:
        # One extra day before the window: the first slot needs the reading before it.
        points = self._points(client, entity, ENERGY_METRIC, start - timedelta(days=1), end)
        grid = build_mod.make_utc_grid(start, end, interval_seconds=self._interval)
        if len(points) < 2:
            return grid, [None] * len(grid)
        series = build_mod.stitch_series(
            entity=entity, metric=ENERGY_METRIC, unit="kWh", points=points
        )
        spans = build_mod.counter_deltas(series)
        allocated, _ledger = build_mod.allocate_energy(
            series, spans, grid, interval_seconds=self._interval
        )
        values: list[float | None] = [
            e.raw_grid_kwh if e.energy_quality == build_mod.ENERGY_MEASURED else None
            for e in allocated
        ]
        return grid, values

    def _mean_price(
        self, client: httpx.Client, start: datetime, end: datetime
    ) -> tuple[float | None, int]:
        if not self._price:
            return None, 0
        points = self._points(client, self._price, PRICE_METRIC, start, end)
        values = [p.value for p in points]
        if not values:
            return None, 0
        return float(np.mean(values)), len(values)

    # -- public --------------------------------------------------------------

    def fetch(self, now: datetime, day: date) -> LoadHistory:
        """History for the *window_days* local days strictly before *day*."""
        cutoff, _ = local_day_bounds_utc(day, self._tz)
        window_start, _ = local_day_bounds_utc(day - timedelta(days=self._window_days), self._tz)
        notes: list[str] = []
        with self._client_factory() as client:
            grid_starts, grid_kwh = self._slot_energy(client, self._grid, window_start, cutoff)
            basis = BASIS_GRID_ONLY
            gross: list[float | None] = list(grid_kwh)
            if self._charge and self._discharge:
                basis = BASIS_GRID_PLUS_BATTERY
                _, charge = self._slot_energy(client, self._charge, window_start, cutoff)
                _, discharge = self._slot_energy(client, self._discharge, window_start, cutoff)
                step = timedelta(seconds=self._interval)
                commissioned = self._commissioned_at

                def slot_gross(
                    start: datetime, g: float | None, c: float | None, d: float | None
                ) -> float | None:
                    if commissioned is not None and start + step <= commissioned:
                        return g  # the battery could not exchange energy yet
                    return None if g is None or c is None or d is None else g - c + d

                gross = [
                    slot_gross(s, g, c, d)
                    for s, g, c, d in zip(grid_starts, grid_kwh, charge, discharge, strict=True)
                ]
                negative = sum(1 for v in gross if v is not None and v < 0)
                if negative:
                    notes.append(f"{negative} slot(s) with negative reconstructed load excluded")
                    gross = [None if v is not None and v < 0 else v for v in gross]
            mean_price, price_samples = self._mean_price(client, window_start, cutoff)
        load = np.array([np.nan if v is None else v for v in gross], dtype=float)
        usable = ~np.isnan(load)
        return LoadHistory(
            starts=tuple(grid_starts),
            load_kwh=load,
            usable=usable,
            cutoff=cutoff,
            cutoff_day=day,
            window_days=self._window_days,
            basis=basis,
            fetched_at=now.astimezone(UTC),
            mean_price_eur_per_kwh=mean_price,
            price_samples=price_samples,
            notes=tuple(notes),
        )
