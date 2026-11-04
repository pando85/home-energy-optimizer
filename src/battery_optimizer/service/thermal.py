"""Battery thermal model, Omnibattery's derate curve and the guarded refit (pure, no I/O, no clock).

The model is the one fitted in ``docs/planning/thermal-derate-aware-planning.md`` (section 2): a
first-order node ``dT/dt = (T_amb + k_c P_charge + k_d P_discharge - T) / tau`` (T degC, P kW, t
min) on the Marstek Venus E V3's only temperature, the internal one Omnibattery reads.  Its exact
discretisation over a span with constant power is ``T_end = a T_start + (1 - a)(T_amb + k_c P_c +
k_d P_d)`` with ``a = exp(-dt / tau)``.

This module holds three things, all of them pure:

* the **fit** (multi-step least squares, ``tau`` profiled on a grid) that the offline study
  ``hack/battery_thermal_derate.py`` uses and that the service refits online, extended with fixed
  coefficients and a ridge towards the prior;
* Omnibattery's documented **derate curve** (factor 1 up to the limit, linear to the floor over the
  band, never below the driver minimum) and the per-slot **prediction** of the temperature and of
  the power that is then achievable;
* the **refit** with its acceptance checks: a candidate replaces the current parameters only if it
  predicts held-out episodes better than they do and beats persistence by a margin, so a bad fit
  never replaces a good one.

Nothing here writes, reads a clock or touches Home Assistant.  Observability only: the planner
never calls it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "BOUNDS",
    "DEFAULT_PARAMS",
    "DEFAULT_STEP_S",
    "DerateSettings",
    "PlanThermal",
    "RefitResult",
    "ThermalParams",
    "derate_factor",
    "derated_cap_w",
    "episodes",
    "equilibrium_charge_kw",
    "exp_filter",
    "fit_tau_profile",
    "free_run",
    "predict_plan",
    "predict_rows",
    "refit",
    "rows",
    "score",
    "steady_state",
]

#: Grid step of the service's observation buffer (the offline study uses 30 s).
DEFAULT_STEP_S = 60.0
ACTIVE_KW = 0.1
GAP_MIN_DEFAULT = 60.0
HORIZONS_MIN = (10, 20, 30, 45, 60, 90)
#: Spacing of the forecast origins used as regression rows (2 min).
ORIGIN_STRIDE_S = 120.0
ORIGIN_STRIDE = 4  # the study's stride at its 30 s grid
STUDY_TAU_GRID_MIN = tuple(float(x) for x in np.geomspace(8.0, 480.0, 49))

#: Permitted range of each parameter (design section 3); a refit is clipped into it.
BOUNDS: dict[str, tuple[float, float]] = {
    "tau_min": (20.0, 120.0),
    "t_amb_c": (20.0, 40.0),
    "k_c": (2.0, 15.0),
    "k_d": (2.0, 20.0),
}
ONLINE_TAU_GRID_MIN = tuple(float(x) for x in np.geomspace(*BOUNDS["tau_min"], 21))
FOLD_TAU_GRID_MIN = tuple(float(x) for x in np.geomspace(*BOUNDS["tau_min"], 9))
#: Pseudo-rows of prior knowledge in the ridge: its weight fades as ``RIDGE_PRIOR_ROWS / rows``.
RIDGE_PRIOR_ROWS = 500.0
#: A gain (charge or discharge) is refitted only if its direction has at least ``GAIN_MIN_MINUTES``
#: of power above ``GAIN_MIN_KW`` from at least ``GAIN_MIN_EPISODES`` episodes; else it keeps its
#: value.
GAIN_MIN_KW = 1.0
GAIN_MIN_MINUTES = 10.0
GAIN_MIN_EPISODE_MINUTES = 5.0
GAIN_MIN_EPISODES = 3
#: Data needed for any refit attempt.
MIN_EPISODES = 2
MIN_HOURS = 12.0
MIN_SPAN_MIN = 90.0
#: Held-out 60 min persistence error below this means the data hold no thermal excitation.
MIN_EXCITATION_C = 1.0
#: Leave-one-episode-out MSE skill against persistence a refit needs (design section 3).
MIN_SKILL = 0.5
ACCEPTANCE_HORIZON_MIN = 60
ACCEPTANCE_WINDOW_DAYS = 7.0
MIN_WINDOW_ROWS = 30


@dataclass(frozen=True)
class ThermalParams:
    """``(tau, T_amb, k_c, k_d)`` of the first-order node."""

    tau_min: float
    t_amb_c: float
    k_c: float
    k_d: float

    def clipped(self) -> ThermalParams:
        return ThermalParams(
            **{k: min(max(v, BOUNDS[k][0]), BOUNDS[k][1]) for k, v in asdict(self).items()}
        )

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ThermalParams:
        """Validated parameters; ``ValueError`` on anything missing, non-finite or out of bounds."""
        values: dict[str, float] = {}
        for key, (lo, hi) in BOUNDS.items():
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"thermal parameter {key} is not a number")
            if not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f"thermal parameter {key}={value} is outside {lo}..{hi}")
            values[key] = float(value)
        return cls(**values)

    def steady_state(self, charge_kw: float, discharge_kw: float = 0.0) -> float:
        return self.t_amb_c + self.k_c * charge_kw + self.k_d * discharge_kw


#: The committed calibration (docs/components/battery-thermal.md): the service's prior.  PR #91
#: printed the two gains the wrong way round (its regressors took the discharge power as charge);
#: the charge gain is the 11.0 it called ``k_d``, the discharge gain the 6.9 it called ``k_c``.
DEFAULT_PARAMS = ThermalParams(tau_min=44.0, t_amb_c=32.6, k_c=11.0, k_d=6.9)


# --------------------------------------------------------------------------------------
# derate curve
# --------------------------------------------------------------------------------------


def derate_factor(temp_c: float, limit_c: float, band_c: float, floor_pct: float) -> float:
    """Omnibattery's documented curve: 1 up to ``limit_c``, linear to ``floor_pct / 100`` at
    ``limit_c + band_c``, flat after; no temperature (NaN) means no derate."""
    if math.isnan(temp_c):
        return 1.0
    floor = floor_pct / 100.0
    if temp_c <= limit_c:
        return 1.0
    if band_c <= 0.0 or temp_c >= limit_c + band_c:
        return floor
    return 1.0 - (1.0 - floor) * (temp_c - limit_c) / band_c


def derated_cap_w(
    temp_c: float,
    limit_w: float,
    limit_c: float,
    band_c: float,
    floor_pct: float,
    min_power_w: float = 0.0,
) -> float:
    """Power cap in W: ``limit * factor``, never below the driver's minimum power."""
    f = derate_factor(temp_c, limit_c, band_c, floor_pct)
    if f >= 1.0:
        return limit_w
    return max(limit_w * f, min(min_power_w, limit_w))


def steady_state(t_amb: float, k_c: float, kw: float) -> float:
    return t_amb + k_c * kw


def equilibrium_charge_kw(
    t_amb: float, k_c: float, limit_kw: float, limit_c: float, band_c: float, floor_pct: float
) -> tuple[float, float]:
    """Sustained charge power and temperature where the thermal steady state meets the derate
    curve (bisection on P; both are monotone), for a charge that is not otherwise limited."""
    lo, hi = 0.0, limit_kw
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        cap = limit_kw * derate_factor(steady_state(t_amb, k_c, mid), limit_c, band_c, floor_pct)
        if cap >= mid:
            lo = mid
        else:
            hi = mid
    p = 0.5 * (lo + hi)
    return p, steady_state(t_amb, k_c, p)


#: What the Omnibattery temperature-charge-limit entities say about the curve in force.
DERATE_STATES = ("off", "on", "unknown")
#: Plausible limit temperature (degC); outside it the number is treated as garbage (design 3).
LIMIT_C_RANGE = (25.0, 60.0)


@dataclass(frozen=True)
class DerateSettings:
    """The derate in force at one instant, from the owner's switch and numbers.

    ``state`` is ``off`` (switch off: no derate in the model), ``on`` (switch on and the numbers
    valid: the curve) or ``unknown`` (the switch or a number is missing, unavailable or out of
    range: the model stays blind, which is today's behaviour and the safe direction).
    """

    state: str = "unknown"
    limit_c: float | None = None
    band_c: float | None = None
    floor_pct: float | None = None
    discharge_enabled: bool = False
    limit_kw: float = 2.5
    min_kw: float = 0.8

    @classmethod
    def from_readings(
        cls,
        *,
        switch: bool | None,
        limit_c: float | None,
        band_c: float | None,
        floor_pct: float | None,
        discharge_switch: bool | None,
        limit_kw: float,
        min_kw: float,
    ) -> DerateSettings:
        valid = (
            limit_c is not None
            and band_c is not None
            and floor_pct is not None
            and LIMIT_C_RANGE[0] <= limit_c <= LIMIT_C_RANGE[1]
            and band_c > 0.0
            and 0.0 <= floor_pct <= 100.0
        )
        if switch is None:
            state = "unknown"
        elif not switch:
            state = "off"
        else:
            state = "on" if valid else "unknown"
        return cls(
            state=state,
            limit_c=limit_c,
            band_c=band_c,
            floor_pct=floor_pct,
            discharge_enabled=bool(discharge_switch) and state == "on",
            limit_kw=limit_kw,
            min_kw=min_kw,
        )

    def factor(self, temp_c: float | None) -> float:
        """Derate factor at *temp_c*: 1 unless the curve is in force and the temperature known."""
        if self.state != "on" or temp_c is None or math.isnan(temp_c):
            return 1.0
        assert self.limit_c is not None and self.band_c is not None and self.floor_pct is not None
        return derate_factor(temp_c, self.limit_c, self.band_c, self.floor_pct)

    def cap_kw(self, temp_c: float | None, direction: str = "charge") -> float:
        """Power cap in kW: the limit times the factor, never below the driver minimum.  The
        discharge side is capped only while its own switch is on."""
        if direction == "discharge" and not self.discharge_enabled:
            return self.limit_kw
        f = self.factor(temp_c)
        if f >= 1.0:
            return self.limit_kw
        return max(self.limit_kw * f, min(self.min_kw, self.limit_kw))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------
# the model on a regular grid: regressors, rows, fit (moved from hack/battery_thermal_derate.py)
# --------------------------------------------------------------------------------------


def exp_filter(u: np.ndarray, a: float) -> np.ndarray:
    """``F[k+1] = a F[k] + (1 - a) u[k]``, ``F[0] = 0`` (the start value cancels in differences)."""
    from scipy.signal import lfilter

    return lfilter([0.0, 1.0 - a], [1.0, -a], u)


def episodes(
    power_kw: np.ndarray, *, step_s: float = DEFAULT_STEP_S, gap_min: float = GAP_MIN_DEFAULT
) -> list[tuple[int, int]]:
    """Index ranges ``[lo, hi)`` that split the timeline at the midpoints of the idle gaps of at
    least ``gap_min`` minutes between active stretches (``|P| > ACTIVE_KW``)."""
    active = np.flatnonzero(np.abs(np.nan_to_num(power_kw)) > ACTIVE_KW)
    if active.size == 0:
        return [(0, power_kw.size)]
    cuts = []
    gap_steps = gap_min * 60.0 / step_s
    for a, b in zip(active[:-1], active[1:], strict=True):
        if b - a > gap_steps:
            cuts.append((a + b) // 2)
    edges = [0, *cuts, power_kw.size]
    return list(zip(edges[:-1], edges[1:], strict=True))


def inputs(
    p_kw: np.ndarray, t_grid: np.ndarray, kind: str, a: float
) -> tuple[np.ndarray, list[str]]:
    """Filtered regressors ``F[k, j]`` of the model input columns for decay ``a``.

    ``p_kw`` is **positive while charging** (the service multiplies its AC power by
    ``actuation.battery_ac_power_sign``; the study negates ``sensor.venus_ac_power``): the ``k_c``
    column is the charge power, the ``k_d`` column the discharge power.

    ``kind``: ``split`` (T_amb, k_c, k_d), ``pooled`` (T_amb, k_|P|), ``quad`` (split plus a
    ``k_c2 * P_charge^2`` term: resistive losses grow with the square of the power), ``harmonic``
    (split plus cos / sin of the hour of day on the ambient). Power NaN counts as idle."""
    p = np.nan_to_num(p_kw)
    cols = [np.ones_like(p)]
    names = ["t_amb"]
    if kind == "pooled":
        cols.append(np.abs(p))
        names.append("k_abs")
    else:
        cols += [np.maximum(p, 0.0), np.maximum(-p, 0.0)]
        names += ["k_c", "k_d"]
    if kind == "quad":
        cols.append(np.maximum(p, 0.0) ** 2)
        names.append("k_c2")
    if kind == "harmonic":
        ang = 2.0 * math.pi * t_grid / 86400.0
        cols += [np.cos(ang), np.sin(ang)]
        names += ["amb_cos", "amb_sin"]
    return np.column_stack([exp_filter(c, a) for c in cols]), names


def rows(
    temp: np.ndarray,
    f: np.ndarray,
    a: float,
    lo: int,
    hi: int,
    horizons_steps: tuple[int, ...],
    stride: int = ORIGIN_STRIDE,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Regression rows of the multi-step prediction inside ``[lo, hi)``.

    Returns ``y = T[k0+h] - a^h T[k0]``, ``x = F[k0+h] - a^h F[k0]``, the origin temperature and
    the origin index (so that ``T[k0+h] = a^h T[k0] + x . beta``)."""
    ys, xs, t0s, ks, hs = [], [], [], [], []
    for h in horizons_steps:
        k0 = np.arange(lo, hi - h, stride)
        good = ~np.isnan(temp[k0]) & ~np.isnan(temp[k0 + h])
        k0 = k0[good]
        ah = a**h
        ys.append(temp[k0 + h] - ah * temp[k0])
        xs.append(f[k0 + h] - ah * f[k0])
        t0s.append(temp[k0])
        ks.append(k0)
        hs.append(np.full(k0.size, h))
    if not ys:
        return np.empty(0), np.empty((0, f.shape[1])), np.empty(0), np.empty(0)
    return (
        np.concatenate(ys),
        np.vstack(xs),
        np.concatenate(t0s),
        np.column_stack([np.concatenate(ks), np.concatenate(hs)]),
    )


def _stride(step_s: float) -> int:
    return max(1, round(ORIGIN_STRIDE_S / step_s))


def fit_tau_profile(
    temp: np.ndarray,
    p_kw: np.ndarray,
    t_grid: np.ndarray,
    spans: list[tuple[int, int]],
    kind: str,
    horizons_steps: tuple[int, ...],
    taus_min: tuple[float, ...] = STUDY_TAU_GRID_MIN,
    *,
    step_s: float = DEFAULT_STEP_S,
    fixed: Mapping[str, float] | None = None,
    prior: Mapping[str, float] | None = None,
    ridge_rows: float = 0.0,
) -> dict:
    """Least squares of the multi-step error for each ``tau`` on the grid; the best one wins.

    ``fixed`` holds the named coefficients at the given values (their column is moved to the
    target); ``prior`` with ``ridge_rows > 0`` adds a ridge of ``ridge_rows`` pseudo-rows of the
    column's own scale towards the prior value of every free coefficient, so its weight fades as
    the number of rows grows.  ``rmse`` is the data residual, without the penalty.

    Returns ``{"tau_min", "beta", "names", "rmse", "n", "profile": [(tau, rmse), ...]}``."""
    best, profile = None, []
    fixed = dict(fixed or {})
    stride = _stride(step_s)
    for tau in taus_min:
        a = math.exp(-step_s / 60.0 / tau)
        f, names = inputs(p_kw, t_grid, kind, a)
        parts = [rows(temp, f, a, lo, hi, horizons_steps, stride) for lo, hi in spans]
        y = np.concatenate([p[0] for p in parts])
        x = np.vstack([p[1] for p in parts])
        free = [i for i, n in enumerate(names) if n not in fixed]
        if y.size < len(free) + 3:
            continue
        beta = np.empty(len(names))
        target = y.copy()
        for i, n in enumerate(names):
            if n in fixed:
                beta[i] = fixed[n]
                target -= x[:, i] * fixed[n]
        design = x[:, free]
        if ridge_rows > 0.0 and prior:
            pen = np.sqrt(ridge_rows * np.mean(design**2, axis=0))
            anchor = np.array([prior.get(names[i], 0.0) for i in free])
            sol, *_ = np.linalg.lstsq(
                np.vstack([design, np.diag(pen)]),
                np.concatenate([target, pen * anchor]),
                rcond=None,
            )
        else:
            sol, *_ = np.linalg.lstsq(design, target, rcond=None)
        beta[free] = sol
        rmse = float(np.sqrt(np.mean((target - design @ sol) ** 2)))
        profile.append((tau, rmse))
        if best is None or rmse < best["rmse"]:
            best = {"tau_min": tau, "beta": beta, "names": names, "rmse": rmse, "n": int(y.size)}
    if best is None:
        raise ValueError("no usable rows")
    best["profile"] = profile
    return best


def predict_rows(
    fit: dict,
    temp: np.ndarray,
    p_kw: np.ndarray,
    t_grid: np.ndarray,
    kind: str,
    span: tuple[int, int],
    horizons_steps: tuple[int, ...],
    beta: np.ndarray | None = None,
    *,
    step_s: float = DEFAULT_STEP_S,
) -> dict:
    """Multi-step predictions inside ``span`` with a fitted model: arrays ``h, pred, obs, t0,
    p_mean`` (mean absolute power over the horizon, kW) and ``k0`` (the origin index)."""
    a = math.exp(-step_s / 60.0 / fit["tau_min"])
    f, _ = inputs(p_kw, t_grid, kind, a)
    y, x, t0, idx = rows(temp, f, a, span[0], span[1], horizons_steps, _stride(step_s))
    if y.size == 0:
        return {k: np.empty(0) for k in ("h", "pred", "obs", "t0", "p_mean", "k0")}
    b = fit["beta"] if beta is None else beta
    ah = a ** idx[:, 1]
    pabs = np.cumsum(np.concatenate([[0.0], np.abs(np.nan_to_num(p_kw))]))
    p_mean = (pabs[idx[:, 0] + idx[:, 1]] - pabs[idx[:, 0]]) / idx[:, 1]
    return {
        "h": idx[:, 1] * step_s / 60.0,
        "pred": ah * t0 + x @ b,
        "obs": y + ah * t0,
        "t0": t0,
        "p_mean": p_mean,
        "k0": idx[:, 0],
    }


def free_run(
    temp0: float,
    p_kw: np.ndarray,
    tau_min: float,
    t_amb: float,
    k_c: float,
    k_d: float,
    k_c2: float = 0.0,
    *,
    step_s: float = DEFAULT_STEP_S,
) -> np.ndarray:
    """Exact discretisation of the first-order node on the grid from ``temp0`` (+ = charging)."""
    a = math.exp(-step_s / 60.0 / tau_min)
    p = np.nan_to_num(p_kw)
    out = np.empty(p.size + 1)
    out[0] = temp0
    for k in range(p.size):
        pc = max(p[k], 0.0)
        target = t_amb + k_c * pc + k_c2 * pc * pc + k_d * max(-p[k], 0.0)
        out[k + 1] = a * out[k] + (1.0 - a) * target
    return out


def score(pred: dict) -> dict:
    """RMSE / bias of the model and of persistence by horizon, plus the pooled figures."""
    out: dict = {"by_h": {}}
    for h in sorted(set(pred["h"].tolist())):
        m = pred["h"] == h
        e = pred["pred"][m] - pred["obs"][m]
        pe = pred["t0"][m] - pred["obs"][m]
        out["by_h"][f"{h:g}"] = {
            "n": int(m.sum()),
            "rmse": float(np.sqrt(np.mean(e**2))),
            "bias": float(e.mean()),
            "rmse_persistence": float(np.sqrt(np.mean(pe**2))),
        }
    if pred["h"].size:
        e = pred["pred"] - pred["obs"]
        pe = pred["t0"] - pred["obs"]
        out["pooled"] = {
            "n": int(e.size),
            "rmse": float(np.sqrt(np.mean(e**2))),
            "rmse_persistence": float(np.sqrt(np.mean(pe**2))),
        }
        hot = pred["p_mean"] > 1.0
        if hot.sum() >= 5:
            out["pooled_above_1kw"] = {
                "n": int(hot.sum()),
                "rmse": float(np.sqrt(np.mean(e[hot] ** 2))),
                "rmse_persistence": float(np.sqrt(np.mean(pe[hot] ** 2))),
                "bias": float(e[hot].mean()),
            }
    return out


# --------------------------------------------------------------------------------------
# per-slot prediction of a plan
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanThermal:
    """Per-slot thermal prediction of one plan (all arrays aligned with the plan's slots).

    ``charge_kw`` / ``discharge_kw`` are the **achievable** mean powers after the derate; the
    ``*_planned_kw`` arrays are what the plan asked for.  ``derate_factor`` and ``charge_cap_kw``
    are the worst (lowest) value inside the slot.
    """

    temp_start_c: np.ndarray
    temp_end_c: np.ndarray
    temp_max_c: np.ndarray
    derate_factor: np.ndarray
    charge_cap_kw: np.ndarray
    charge_planned_kw: np.ndarray
    charge_kw: np.ndarray
    discharge_planned_kw: np.ndarray
    discharge_kw: np.ndarray
    cold_start: bool
    interval_s: float

    @property
    def n(self) -> int:
        return int(self.temp_start_c.size)

    def columns(self, digits: int = 3) -> dict[str, list[float]]:
        """The arrays as rounded JSON lists (a plan record carries up to 192 slots)."""
        names = (
            "temp_start_c",
            "temp_end_c",
            "temp_max_c",
            "derate_factor",
            "charge_cap_kw",
            "charge_planned_kw",
            "charge_kw",
            "discharge_planned_kw",
            "discharge_kw",
        )
        return {n: [round(float(v), digits) for v in getattr(self, n)] for n in names}

    def summary(self) -> dict[str, Any]:
        h = self.interval_s / 3600.0
        return {
            "max_temp_c": round(float(self.temp_max_c.max()), 3) if self.n else None,
            "derated_slots": int(np.sum(self.derate_factor < 1.0)),
            "planned_charge_kwh": round(float(self.charge_planned_kw.sum() * h), 4),
            "achievable_charge_kwh": round(float(self.charge_kw.sum() * h), 4),
            "planned_discharge_kwh": round(float(self.discharge_planned_kw.sum() * h), 4),
            "achievable_discharge_kwh": round(float(self.discharge_kw.sum() * h), 4),
        }


def predict_plan(
    params: ThermalParams,
    derate: DerateSettings,
    temp_c: float | None,
    charge_kw: Sequence[float],
    discharge_kw: Sequence[float],
    interval_s: float,
    *,
    substep_s: float = 60.0,
) -> PlanThermal:
    """Simulate the node through a plan, derating each substep.

    The battery draws ``min(requested, cap(T))`` at every substep, where ``cap`` is Omnibattery's
    curve at the temperature of that instant (so the derate is a feedback inside a slot, not a
    cap frozen at the slot start), and the temperature follows the power that is *achieved*.  With
    the derate ``off`` or ``unknown`` the cap is the limit and the plan is simulated blind.
    ``temp_c`` ``None`` (unavailable) starts from ``T_amb`` and flags ``cold_start``.
    """
    n = len(charge_kw)
    if len(discharge_kw) != n:
        raise ValueError("charge and discharge powers must be aligned")
    cold = temp_c is None or not math.isfinite(temp_c)
    t = params.t_amb_c if cold else float(temp_c)  # type: ignore[arg-type]
    steps = max(1, math.ceil(interval_s / substep_s))
    dt_min = interval_s / steps / 60.0
    a = math.exp(-dt_min / params.tau_min)
    out = {
        k: np.empty(n)
        for k in (
            "temp_start_c",
            "temp_end_c",
            "temp_max_c",
            "derate_factor",
            "charge_cap_kw",
            "charge_kw",
            "discharge_kw",
        )
    }
    for i in range(n):
        req_c, req_d = max(float(charge_kw[i]), 0.0), max(float(discharge_kw[i]), 0.0)
        out["temp_start_c"][i] = t
        t_max, factor, cap_min = t, 1.0, derate.limit_kw
        got_c = got_d = 0.0
        for _ in range(steps):
            cap_c = derate.cap_kw(t, "charge")
            cap_d = derate.cap_kw(t, "discharge")
            pc, pd = min(req_c, cap_c), min(req_d, cap_d)
            got_c += pc
            got_d += pd
            factor = min(factor, derate.factor(t))
            cap_min = min(cap_min, cap_c)
            t = a * t + (1.0 - a) * (params.t_amb_c + params.k_c * pc + params.k_d * pd)
            t_max = max(t_max, t)
        factor = min(factor, derate.factor(t))
        out["temp_end_c"][i] = t
        out["temp_max_c"][i] = t_max
        out["derate_factor"][i] = factor
        out["charge_cap_kw"][i] = min(cap_min, derate.cap_kw(t, "charge"))
        out["charge_kw"][i] = got_c / steps
        out["discharge_kw"][i] = got_d / steps
    return PlanThermal(
        charge_planned_kw=np.maximum(np.asarray(charge_kw, dtype=float), 0.0),
        discharge_planned_kw=np.maximum(np.asarray(discharge_kw, dtype=float), 0.0),
        cold_start=cold,
        interval_s=float(interval_s),
        **out,
    )


# --------------------------------------------------------------------------------------
# the guarded refit
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RefitResult:
    """Outcome of one refit attempt.

    ``result``: ``accepted`` (``params`` replace the current ones), ``rejected`` (a candidate was
    fitted and failed an acceptance check; the current parameters stay) or ``insufficient_data``
    (nothing was judged: too little data or no thermal excitation).
    """

    result: str
    reason: str
    params: ThermalParams | None = None
    quality: dict[str, Any] = field(default_factory=dict)


def usable_spans(
    temp: np.ndarray,
    p_kw: np.ndarray,
    *,
    step_s: float = DEFAULT_STEP_S,
    gap_min: float = GAP_MIN_DEFAULT,
    min_span_min: float = MIN_SPAN_MIN,
) -> list[tuple[int, int]]:
    """Episodes of the buffer where the power is known, long enough and with a temperature.

    A missing power (the process or Home Assistant was away) splits the buffer: the model must
    never read a gap as idle.  Each stretch is then split into episodes at idle gaps."""
    known = np.isfinite(p_kw)
    spans: list[tuple[int, int]] = []
    n = p_kw.size
    k = 0
    while k < n:
        if not known[k]:
            k += 1
            continue
        end = k
        while end < n and known[end]:
            end += 1
        for lo, hi in episodes(p_kw[k:end], step_s=step_s, gap_min=gap_min):
            lo, hi = lo + k, hi + k
            if (hi - lo) * step_s / 60.0 >= min_span_min and np.isfinite(temp[lo:hi]).sum() >= 30:
                spans.append((lo, hi))
        k = end
    return spans


def _supported(
    p_kw: np.ndarray, spans: list[tuple[int, int]], sign: float, step_s: float
) -> tuple[float, int]:
    """Minutes of power above ``GAIN_MIN_KW`` in one direction, and the episodes that have at
    least ``GAIN_MIN_EPISODE_MINUTES`` of it."""
    minutes_per_point = step_s / 60.0
    total, eps = 0.0, 0
    for lo, hi in spans:
        m = float(np.sum(sign * p_kw[lo:hi] > GAIN_MIN_KW)) * minutes_per_point
        total += m
        eps += int(m >= GAIN_MIN_EPISODE_MINUTES)
    return total, eps


def refit(
    temp: np.ndarray,
    p_kw: np.ndarray,
    *,
    current: ThermalParams,
    prior: ThermalParams,
    step_s: float = DEFAULT_STEP_S,
) -> RefitResult:
    """Fit the buffer (``p_kw`` positive while charging, NaN where unknown) and judge the result.

    The candidate is the multi-step least squares of the study with ``tau`` profiled inside its
    bounds, a ridge towards the prior that fades with the rows, and a gain held at its current
    value unless its direction has data (``GAIN_MIN_*``).  It is **accepted** only if

    1. its leave-one-episode-out 60 min mean squared error beats persistence by at least
       ``MIN_SKILL`` (``1 - mse / mse_persistence``), and
    2. on the held-out rows of the last ``ACCEPTANCE_WINDOW_DAYS`` days its 60 min rmse is lower
       than that of the current parameters on the same rows.

    Otherwise the current parameters stay (``rejected``).  Too little data, or data without
    thermal excitation (held-out persistence rmse under ``MIN_EXCITATION_C``), is
    ``insufficient_data``: nothing is judged.  Returns the candidate clipped to ``BOUNDS``.
    """
    grid = np.arange(temp.size, dtype=float) * step_s
    spans = usable_spans(temp, p_kw, step_s=step_s)
    hours = sum(int(np.isfinite(temp[lo:hi]).sum()) for lo, hi in spans) * step_s / 3600.0
    quality: dict[str, Any] = {"episodes": len(spans), "hours": round(hours, 2)}
    if len(spans) < MIN_EPISODES or hours < MIN_HOURS:
        return RefitResult("insufficient_data", "too little usable data", None, quality)

    fixed: dict[str, float] = {}
    for key, sign in (("k_c", 1.0), ("k_d", -1.0)):
        minutes, eps = _supported(p_kw, spans, sign, step_s)
        quality[f"{key}_minutes_above_1kw"] = round(minutes, 1)
        quality[f"{key}_episodes_above_1kw"] = eps
        if minutes < GAIN_MIN_MINUTES or eps < GAIN_MIN_EPISODES:
            fixed[key] = getattr(current, key)
    quality["gains_held"] = sorted(fixed)
    prior_map = {"t_amb": prior.t_amb_c, "k_c": prior.k_c, "k_d": prior.k_d}
    hs = tuple(round(h * 60 / step_s) for h in HORIZONS_MIN)
    hold = round(ACCEPTANCE_HORIZON_MIN * 60 / step_s)

    def fit_on(train: list[tuple[int, int]], taus: tuple[float, ...]) -> dict:
        return fit_tau_profile(
            temp,
            p_kw,
            grid,
            train,
            "split",
            hs,
            taus,
            step_s=step_s,
            fixed=fixed,
            prior=prior_map,
            ridge_rows=RIDGE_PRIOR_ROWS,
        )

    def to_params(fit: dict) -> ThermalParams:
        c = dict(zip(fit["names"], fit["beta"], strict=True))
        return ThermalParams(fit["tau_min"], c["t_amb"], c["k_c"], c["k_d"]).clipped()

    try:
        candidate = to_params(fit_on(spans, ONLINE_TAU_GRID_MIN))
    except ValueError:
        return RefitResult("insufficient_data", "no usable rows", None, quality)

    def as_fit(p: ThermalParams) -> dict:
        return {"tau_min": p.tau_min, "beta": np.array([p.t_amb_c, p.k_c, p.k_d])}

    held: dict[str, list[np.ndarray]] = {k: [] for k in ("cand", "cur", "obs", "t0", "k0")}
    for i, span in enumerate(spans):
        train = [s for j, s in enumerate(spans) if j != i]
        try:
            fold = to_params(fit_on(train, FOLD_TAU_GRID_MIN))
        except ValueError:
            continue
        pc = predict_rows(as_fit(fold), temp, p_kw, grid, "split", span, (hold,), step_s=step_s)
        if pc["h"].size == 0:
            continue
        pu = predict_rows(as_fit(current), temp, p_kw, grid, "split", span, (hold,), step_s=step_s)
        held["cand"].append(pc["pred"])
        held["cur"].append(pu["pred"])
        held["obs"].append(pc["obs"])
        held["t0"].append(pc["t0"])
        held["k0"].append(pc["k0"])
    if not held["obs"]:
        return RefitResult("insufficient_data", "no held-out rows", None, quality)
    cat = {k: np.concatenate(v) for k, v in held.items()}
    err_cand = cat["cand"] - cat["obs"]
    err_cur = cat["cur"] - cat["obs"]
    err_per = cat["t0"] - cat["obs"]
    mse_per = float(np.mean(err_per**2))
    quality.update(
        {
            "held_out_rows": int(cat["obs"].size),
            "rmse_60_candidate": round(float(np.sqrt(np.mean(err_cand**2))), 3),
            "rmse_60_current": round(float(np.sqrt(np.mean(err_cur**2))), 3),
            "rmse_60_persistence": round(math.sqrt(mse_per), 3),
        }
    )
    if math.sqrt(mse_per) < MIN_EXCITATION_C:
        return RefitResult("insufficient_data", "no thermal excitation in the data", None, quality)
    skill = 1.0 - float(np.mean(err_cand**2)) / mse_per
    quality["skill_vs_persistence"] = round(skill, 3)
    window_from = temp.size - ACCEPTANCE_WINDOW_DAYS * 86400.0 / step_s
    recent = cat["k0"] >= window_from
    if int(recent.sum()) < MIN_WINDOW_ROWS:
        recent = np.ones_like(recent, dtype=bool)
    rmse_cand_w = float(np.sqrt(np.mean(err_cand[recent] ** 2)))
    rmse_cur_w = float(np.sqrt(np.mean(err_cur[recent] ** 2)))
    quality["window_rows"] = int(recent.sum())
    quality["window_rmse_60_candidate"] = round(rmse_cand_w, 3)
    quality["window_rmse_60_current"] = round(rmse_cur_w, 3)
    if skill < MIN_SKILL:
        return RefitResult(
            "rejected",
            f"skill {skill:.2f} against persistence below {MIN_SKILL}",
            candidate,
            quality,
        )
    if not rmse_cand_w < rmse_cur_w:
        return RefitResult(
            "rejected",
            "does not beat the current parameters on the recent held-out rows",
            candidate,
            quality,
        )
    return RefitResult(
        "accepted", "beats the current parameters and persistence", candidate, quality
    )
