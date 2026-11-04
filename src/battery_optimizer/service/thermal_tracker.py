"""The thermal state of the battery: readings, a one-minute buffer, the model, predictions, scores.

A state machine fed with observed entity states and the instant they were observed; it does no
I/O and reads no clock (the runtime passes ``now``).  The runtime feeds it from the entity store
(the existing GET / ``state_changed`` path), asks it to predict each published plan, scores the
predictions when a slot closes, runs the guarded refit of :mod:`.thermal` on a copy of its buffer
and persists its state under ``service/v1/state/``.  **Observability only**: nothing here reaches
the planner or the actuator, and no entity is ever written.

* **Readings.**  The internal temperature, the signed battery AC power (positive while charging,
  after ``actuation.battery_ac_power_sign``), Omnibattery's Temperature Charge Limit switch, its
  limit, band and floor, the optional discharge switch and the two cap numbers.  An unavailable,
  unknown or non-numeric state is ``None`` (unknown is never zero); a missing role never blocks
  anything, it only leaves the derate ``unknown``.
* **Buffer.**  ``buffer_days`` of temperature and power on a one-minute grid, by sample-and-hold:
  a value is held until the next observation; a temperature older than ``temperature_max_age_s``
  and every minute the Home Assistant session was not synced is missing (NaN), never idle.
* **Model.**  ``(tau, T_amb, k_c, k_d)`` of :mod:`.thermal`, from the configured prior until a
  refit is accepted; status ``prior``, ``fitted`` or ``degraded`` (the last refit was rejected).
* **Predictions.**  Per published plan, a per-slot temperature and achievable power.
* **Scores.**  At slot close, the predicted against the measured temperature at the slot's end
  (error counters by lead) and, in slots where a derate was predicted or measured, the predicted
  against the observed charge power.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from .battery_health import parse_number, parse_text
from .config import ThermalSettings
from .thermal import (
    DerateSettings,
    PlanThermal,
    RefitResult,
    ThermalParams,
    predict_plan,
)

__all__ = [
    "LEADS",
    "MODEL_STATUSES",
    "THERMAL_ROLES",
    "ThermalTracker",
]

#: Roles :meth:`ThermalTracker.observe` understands (``charge_power`` is the battery AC power).
THERMAL_ROLES: tuple[str, ...] = (
    "temperature",
    "limit_switch",
    "discharge_switch",
    "limit_c",
    "band_c",
    "floor_pct",
    "system_max_charge_power",
    "venus_max_charge_power",
    "charge_power",
)
MODEL_STATUSES = ("prior", "fitted", "degraded")
#: Lead labels of the scored predictions: the plan of record when the slot opened, and the newest
#: plan made at least one and three hours before the slot's end.
LEADS: tuple[str, ...] = ("final", "1h", "3h")
_LEAD_SECONDS = {"1h": 3600.0, "3h": 3 * 3600.0}
_STEP_S = 60
_PREDICTION_MEMORY = 64
#: A slot's observed mean power needs at least this share of its minutes known.
_MIN_COVERAGE = 0.8
STATE_VERSION = 1


@dataclass(frozen=True)
class _Prediction:
    plan_id: str
    generated_at: datetime
    slot_starts: tuple[datetime, ...]
    thermal: PlanThermal
    derate: DerateSettings
    params: ThermalParams

    def index(self, slot_start: datetime) -> int | None:
        try:
            return self.slot_starts.index(slot_start)
        except ValueError:
            return None


def _switch(state: str | None) -> bool | None:
    text = parse_text(state)
    return None if text is None else {"on": True, "off": False}.get(text.lower())


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


def _num(value: float | None, digits: int = 3) -> float | None:
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


class ThermalTracker:
    """See the module docstring.  Not thread-safe (event loop only; the refit gets array copies)."""

    def __init__(
        self,
        settings: ThermalSettings,
        *,
        ac_power_sign: int,
        buffer_days: float = 21.0,
        temperature_max_age_s: float = 600.0,
    ) -> None:
        if ac_power_sign not in (1, -1):
            raise ValueError("ac_power_sign must be +1 or -1")
        self.settings = settings
        self._sign = ac_power_sign
        self._max_age_s = temperature_max_age_s
        self.prior = ThermalParams(**vars(settings.prior)).clipped()
        self.params = self.prior
        self.status = "prior"
        self.fitted_at: datetime | None = None
        self.quality: dict[str, Any] = {}
        self.last_refit: dict[str, Any] | None = None
        self.last_attempt_at: datetime | None = None
        self.restore_problem: str | None = None

        self._temp: float | None = None
        self._temp_at: datetime | None = None
        self._power_kw: float | None = None
        self._readings: dict[str, Any] = dict.fromkeys(
            (
                "limit_switch",
                "discharge_switch",
                "limit_c",
                "band_c",
                "floor_pct",
                "system_max_charge_power",
                "venus_max_charge_power",
            )
        )
        size = int(buffer_days * 86400 / _STEP_S)
        self._temps: deque[float] = deque(maxlen=size)
        self._powers: deque[float] = deque(maxlen=size)
        self._end_minute: int | None = None  # epoch minute of the newest sample
        self.predictions: deque[_Prediction] = deque(maxlen=_PREDICTION_MEMORY)
        #: Cumulative scores since start: per lead (n, abs, sq, signed) and the derate-slot power.
        self.scores: dict[str, dict[str, float]] = {
            lead: {"n": 0.0, "abs": 0.0, "sq": 0.0, "signed": 0.0} for lead in LEADS
        }
        self.power_scores: dict[str, float] = {"n": 0.0, "abs": 0.0, "signed": 0.0}

    # ------------------------------------------------------------------ feeding

    def observe(self, role: str, state: str | None, at: datetime) -> None:
        """Feed one observation: *state* is the HA state string (``None`` = missing)."""
        if role not in THERMAL_ROLES:
            raise KeyError(f"unknown thermal role {role!r}")
        self.advance(at)
        if role == "temperature":
            self._temp = parse_number(state)
            self._temp_at = at
        elif role == "charge_power":
            watts = parse_number(state)
            self._power_kw = None if watts is None else self._sign * watts / 1000.0
        elif role in ("limit_switch", "discharge_switch"):
            self._readings[role] = _switch(state)
        else:
            self._readings[role] = parse_number(state)

    def advance(self, now: datetime, observing: bool = True) -> None:
        """Append the one-minute samples up to *now* (held values; NaN while not *observing*)."""
        minute = int(now.timestamp() // _STEP_S)
        end = self._end_minute
        if end is None or minute - end > (self._temps.maxlen or 0):
            self._temps.clear()
            self._powers.clear()
            end = minute - 1
        for m in range(end + 1, minute + 1):
            at = datetime.fromtimestamp(m * _STEP_S, UTC)
            temp = self.temperature_at(at) if observing else None
            power = self._power_kw if observing else None
            self._temps.append(math.nan if temp is None else temp)
            self._powers.append(math.nan if power is None else power)
        self._end_minute = max(minute, end)

    # ------------------------------------------------------------------ reading

    def temperature_at(self, at: datetime) -> float | None:
        """The held temperature at *at*, ``None`` if unknown or older than the age limit."""
        if self._temp is None or self._temp_at is None:
            return None
        if (at - self._temp_at).total_seconds() > self._max_age_s:
            return None
        return self._temp

    def temperature_now(self, now: datetime) -> float | None:
        return self.temperature_at(now)

    @property
    def limit_kw(self) -> float:
        """``min(system cap, Venus cap)`` from the entities, else the configured limit."""
        caps = [
            self._readings[k]
            for k in ("system_max_charge_power", "venus_max_charge_power")
            if self._readings[k] is not None and self._readings[k] > 0
        ]
        return min(caps) / 1000.0 if caps else self.settings.charge_limit_kw

    def derate_settings(self) -> DerateSettings:
        r = self._readings
        return DerateSettings.from_readings(
            switch=r["limit_switch"],
            limit_c=r["limit_c"],
            band_c=r["band_c"],
            floor_pct=r["floor_pct"],
            discharge_switch=r["discharge_switch"],
            limit_kw=self.limit_kw,
            min_kw=self.settings.driver_min_kw,
        )

    def _sample(self, minute: int) -> tuple[float, float]:
        """``(temperature, power_kw)`` of a grid minute, NaN when outside the buffer."""
        end = self._end_minute
        if end is None:
            return math.nan, math.nan
        k = len(self._temps) - 1 - (end - minute)
        if not 0 <= k < len(self._temps):
            return math.nan, math.nan
        return self._temps[k], self._powers[k]

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        """Copies of the buffer: temperature (degC) and power (kW, + charging), NaN = unknown."""
        return np.asarray(self._temps, dtype=float), np.asarray(self._powers, dtype=float)

    # ------------------------------------------------------------------ prediction

    def predict(
        self,
        *,
        plan_id: str,
        generated_at: datetime,
        slot_starts: Sequence[datetime],
        interval_s: float,
        charge_kw: Sequence[float],
        discharge_kw: Sequence[float],
        now: datetime,
    ) -> dict[str, Any]:
        """Predict the plan's per-slot temperature and achievable power; returns the JSON block
        recorded with the plan record.  The simulation starts at the measured temperature of *now*
        (the minutes up to the first slot are not simulated: the lead is at most one slot)."""
        derate = self.derate_settings()
        start = self.temperature_now(now)
        thermal = predict_plan(self.params, derate, start, charge_kw, discharge_kw, interval_s)
        self.predictions.append(
            _Prediction(plan_id, generated_at, tuple(slot_starts), thermal, derate, self.params)
        )
        return {
            "mode": self.settings.mode,
            "derate": {**derate.to_dict(), "temperature_known": start is not None},
            "temperature_start_c": _num(start),
            "cold_start": thermal.cold_start,
            "model": self.model_view(),
            "interval_seconds": int(interval_s),
            "slots": thermal.columns(),
            "summary": thermal.summary(),
        }

    def prediction_for(self, plan_id: str) -> _Prediction | None:
        for p in reversed(self.predictions):
            if p.plan_id == plan_id:
                return p
        return None

    def current_slot(self, slot_start: datetime) -> dict[str, Any] | None:
        """The newest prediction's numbers for *slot_start* (``None`` if none covers it)."""
        for p in reversed(self.predictions):
            i = p.index(slot_start)
            if i is not None:
                t = p.thermal
                return {
                    "plan_id": p.plan_id,
                    "temp_start_c": _num(t.temp_start_c[i]),
                    "temp_end_c": _num(t.temp_end_c[i]),
                    "derate_factor": _num(t.derate_factor[i]),
                    "charge_cap_kw": _num(t.charge_cap_kw[i]),
                    "charge_planned_kw": _num(t.charge_planned_kw[i]),
                    "charge_kw": _num(t.charge_kw[i]),
                    "discharge_planned_kw": _num(t.discharge_planned_kw[i]),
                    "discharge_kw": _num(t.discharge_kw[i]),
                }
        return None

    # ------------------------------------------------------------------ scoring

    def _pick(self, lead: str, slot_end: datetime, final_plan_id: str | None) -> _Prediction | None:
        if lead == "final":
            return None if final_plan_id is None else self.prediction_for(final_plan_id)
        cutoff = slot_end - timedelta(seconds=_LEAD_SECONDS[lead])
        eligible = [p for p in self.predictions if p.generated_at <= cutoff]
        return max(eligible, key=lambda p: p.generated_at, default=None)

    def score_slot(
        self, slot_start: datetime, interval_s: float, final_plan_id: str | None
    ) -> dict[str, Any]:
        """Score the predictions of a closed slot (call after :meth:`advance` to its close).

        Returns the block of the slot record: per lead the predicted and measured temperature at
        the slot's end and their error, and, for the plan of record in a slot where a derate was
        predicted or measured, the predicted against the observed mean charge power.
        """
        slot_end = slot_start + timedelta(seconds=interval_s)
        first, last = int(slot_start.timestamp() // _STEP_S), int(slot_end.timestamp() // _STEP_S)
        measured = self._sample(last)[0]
        measured_end = None if math.isnan(measured) else float(measured)
        samples = [self._sample(m) for m in range(first, last)]
        known = [p for _, p in samples if not math.isnan(p)]
        observed_kw = (
            float(np.mean([max(p, 0.0) for p in known]))
            if known and len(known) >= _MIN_COVERAGE * len(samples)
            else None
        )
        out: dict[str, Any] = {
            "measured_temp_end_c": _num(measured_end),
            "observed_charge_kw": _num(observed_kw),
            "leads": {},
            "charge": None,
        }
        for lead in LEADS:
            pick = self._pick(lead, slot_end, final_plan_id)
            i = None if pick is None else pick.index(slot_start)
            if pick is None or i is None:
                continue
            predicted = float(pick.thermal.temp_end_c[i])
            entry: dict[str, Any] = {
                "plan_id": pick.plan_id,
                "predicted_temp_end_c": _num(predicted),
                "error_c": None,
            }
            if measured_end is not None:
                error = predicted - measured_end
                entry["error_c"] = _num(error)
                s = self.scores[lead]
                s["n"] += 1
                s["abs"] += abs(error)
                s["sq"] += error * error
                s["signed"] += error
            out["leads"][lead] = entry
        pick = self._pick("final", slot_end, final_plan_id)
        i = None if pick is None else pick.index(slot_start)
        if pick is not None and i is not None and observed_kw is not None:
            t = pick.thermal
            measured_factor = pick.derate.factor(measured_end) if measured_end is not None else 1.0
            active = bool(t.derate_factor[i] < 1.0 or measured_factor < 1.0)
            predicted_kw = float(t.charge_kw[i])
            planned_kw = float(t.charge_planned_kw[i])
            out["charge"] = {
                "plan_id": pick.plan_id,
                "planned_kw": _num(planned_kw),
                "predicted_achievable_kw": _num(predicted_kw),
                "observed_kw": _num(observed_kw),
                "derate_active": active,
            }
            if active and planned_kw > 0.0:
                error = predicted_kw - observed_kw
                self.power_scores["n"] += 1
                self.power_scores["abs"] += abs(error)
                self.power_scores["signed"] += error
                out["charge"]["error_kw"] = _num(error)
        return out

    # ------------------------------------------------------------------ refit

    def refit_due(self, now: datetime, tz: Any) -> bool:
        """The daily refit is due once the local ``refit_hour_local`` has passed with no attempt
        since (also at start-up after a missed one, and when none was ever made)."""
        local = now.astimezone(tz)
        slot = local.replace(hour=self.settings.refit_hour_local, minute=0, second=0, microsecond=0)
        if local < slot:
            slot -= timedelta(days=1)
        return self.last_attempt_at is None or self.last_attempt_at < slot.astimezone(UTC)

    def apply_refit(self, result: RefitResult | None, error: str | None, now: datetime) -> str:
        """Record one attempt; returns the outcome label (``accepted``, ``rejected``,
        ``insufficient_data`` or ``error``).  Only an accepted fit replaces the parameters."""
        self.last_attempt_at = now
        if result is None:
            outcome, reason, quality = "error", error or "refit failed", {}
        else:
            outcome, reason, quality = result.result, result.reason, result.quality
            if outcome == "accepted" and result.params is not None:
                self.params = result.params
                self.status = "fitted"
                self.fitted_at = now
                self.quality = quality
            elif outcome == "rejected":
                self.status = "degraded"
        self.last_refit = {
            "at": now.isoformat(),
            "result": outcome,
            "reason": reason,
            "quality": quality,
            "candidate": result.params.to_dict() if result and result.params else None,
        }
        return outcome

    # ------------------------------------------------------------------ views

    def model_view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            **{k: round(v, 4) for k, v in self.params.to_dict().items()},
            "fitted_at": _iso(self.fitted_at),
        }

    def snapshot(self, now: datetime) -> dict[str, Any]:
        """The ``/status`` block and the source of the metrics."""
        derate = self.derate_settings()
        temp = self.temperature_now(now)
        factor = derate.factor(temp) if derate.state == "on" and temp is not None else None
        derating = (
            False if derate.state == "off" else (None if factor is None else bool(factor < 1.0))
        )
        age = None if self._temp_at is None else max(0.0, (now - self._temp_at).total_seconds())
        known_temp = int(np.sum(~np.isnan(np.asarray(self._temps, dtype=float))))
        return {
            "mode": self.settings.mode,
            "actuation": "none (read-only; no entity is ever written)",
            "temperature_c": _num(temp),
            "temperature_age_s": None if age is None else round(age, 1),
            "power_kw": _num(self._power_kw),
            "derate": {
                **derate.to_dict(),
                "state": derate.state,
                "factor_now": _num(factor),
                "derating_now": derating,
            },
            "model": {
                **self.model_view(),
                "prior": self.prior.to_dict(),
                "quality": self.quality,
                "last_refit": self.last_refit,
                "last_attempt_at": _iso(self.last_attempt_at),
                "restore_problem": self.restore_problem,
            },
            "buffer": {
                "samples": len(self._temps),
                "hours_with_temperature": round(known_temp * _STEP_S / 3600.0, 2),
                "newest_minute": self._end_minute,
            },
            "scoring": {
                "temperature": {
                    lead: {
                        "n": int(s["n"]),
                        "mean_abs_error_c": _num(s["abs"] / s["n"]) if s["n"] else None,
                        "rmse_c": _num(math.sqrt(s["sq"] / s["n"])) if s["n"] else None,
                        "bias_c": _num(s["signed"] / s["n"]) if s["n"] else None,
                    }
                    for lead, s in self.scores.items()
                },
                "derate_power": {
                    "n": int(self.power_scores["n"]),
                    "mean_abs_error_kw": _num(self.power_scores["abs"] / self.power_scores["n"])
                    if self.power_scores["n"]
                    else None,
                    "bias_kw": _num(self.power_scores["signed"] / self.power_scores["n"])
                    if self.power_scores["n"]
                    else None,
                },
            },
            "predictions_in_memory": len(self.predictions),
        }

    # ------------------------------------------------------------------ persistence

    def to_state(self) -> dict[str, Any]:
        """The model state (``service/v1/state/thermal.json``): small, written on every change."""
        return {
            "version": STATE_VERSION,
            "status": self.status,
            "params": self.params.to_dict(),
            "fitted_at": _iso(self.fitted_at),
            "quality": self.quality,
            "last_refit": self.last_refit,
            "last_attempt_at": _iso(self.last_attempt_at),
        }

    def samples_state(self) -> dict[str, Any] | None:
        """The buffer (``service/v1/state/thermal_samples.json``); ``None`` while it is empty."""
        if self._end_minute is None or not self._temps:
            return None
        return {
            "version": STATE_VERSION,
            "step_s": _STEP_S,
            "end_minute": self._end_minute,
            "temp": [None if math.isnan(v) else round(v, 2) for v in self._temps],
            "power_kw": [None if math.isnan(v) else round(v, 3) for v in self._powers],
        }

    def restore_state(self, state: Mapping[str, Any] | None) -> None:
        """Load a saved model state; anything invalid is ignored and reported in
        ``restore_problem`` (the prior stays).  A model that was never fitted keeps the configured
        prior, so editing the prior takes effect."""
        if state is None:
            return
        try:
            if state.get("version") != STATE_VERSION:
                raise ValueError("unsupported version")
            status = state.get("status")
            if status not in MODEL_STATUSES:
                raise ValueError(f"unknown status {status!r}")
            params = ThermalParams.from_dict(state.get("params") or {})
            fitted_at = _parse_time(state.get("fitted_at"))
            attempt = _parse_time(state.get("last_attempt_at"))
            quality = state.get("quality") or {}
            last = state.get("last_refit")
            if not isinstance(quality, dict) or not (last is None or isinstance(last, dict)):
                raise ValueError("malformed quality or last_refit")
        except (ValueError, TypeError) as exc:
            self.restore_problem = f"thermal state ignored: {exc}"
            return
        if status != "prior":
            self.params = params
        self.status = status
        self.fitted_at = fitted_at
        self.last_attempt_at = attempt
        self.quality = quality
        self.last_refit = last

    def restore_samples(self, state: Mapping[str, Any] | None, now: datetime) -> None:
        """Load a saved buffer (invalid or older than the buffer: ignored).  The minutes between
        its end and the next :meth:`advance` are filled as unknown, never as idle."""
        if state is None:
            return
        try:
            if state.get("version") != STATE_VERSION or state.get("step_s") != _STEP_S:
                raise ValueError("unsupported version or step")
            end = state["end_minute"]
            temp, power = state["temp"], state["power_kw"]
            if (
                isinstance(end, bool)
                or not isinstance(end, int)
                or not isinstance(temp, list)
                or len(temp) != len(power)
            ):
                raise ValueError("malformed buffer")
            maxlen = self._temps.maxlen or 0
            temp, power = temp[-maxlen:], power[-maxlen:]
            for name, series in (("temp", temp), ("power_kw", power)):
                for v in series:
                    if v is not None and (
                        isinstance(v, bool)
                        or not isinstance(v, (int, float))
                        or not math.isfinite(v)
                    ):
                        raise ValueError(f"malformed {name} sample")
        except (KeyError, ValueError, TypeError) as exc:
            self.restore_problem = f"thermal samples ignored: {exc}"
            return
        if int(now.timestamp() // _STEP_S) - end > maxlen:
            return  # older than the whole buffer: nothing usable
        self._temps.extend(math.nan if v is None else float(v) for v in temp)
        self._powers.extend(math.nan if v is None else float(v) for v in power)
        self._end_minute = end


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("naive timestamp")
    return at.astimezone(UTC)
