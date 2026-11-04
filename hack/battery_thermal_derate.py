#!/usr/bin/env python3
"""Thermal model and Omnibattery derate check from the Home Assistant recorder (read-only).

Follow-up of ``battery_thermal_study.py`` (which uses 5-minute windows of the long-term
Prometheus). This script uses the recorder's event resolution and answers two questions:

1. **Thermal model.** How well does a first-order node

       dT/dt = (T_amb + k_c * P_charge + k_d * P_discharge - T) / tau        (T degC, P kW, t min)

   predict the battery's internal temperature ``h`` minutes ahead from the measured AC power?
   The fit minimises the multi-step prediction error (``h`` in 10..90 min, origins every 2 min),
   which is linear in ``(T_amb, k_c, k_d)`` for a given ``tau``, so ``tau`` is profiled on a grid.
   Validation is honest: leave one *episode* out (an episode is a stretch of the timeline between
   the midpoints of idle gaps longer than ``--episode-gap-min``), walk-forward (fit on the past
   only, the deployment case), against persistence (hold the origin temperature) and against the
   earlier study's fit. Variants: separate charge / discharge gains versus one gain, a daily
   harmonic on the ambient, a per-episode ambient (an oracle, to size the ambient drift).
2. **Derate curve.** Omnibattery's Temperature Charge Limit (``control/temperature_limit.py``,
   documented, not read here) multiplies the per-battery charge limit by a factor that is 1 at
   ``T <= limit``, falls linearly to ``floor`` at ``T >= limit + band`` and never drops the power
   below the driver's minimum. The script predicts that cap from the recorded temperature and
   the recorded switch / number states and compares it with the charge power Omnibattery wrote
   (``number.venus_set_charge_power``) and the AC power that was measured, from the moment the
   switch turned on.

Data access is read only: one ``kubectl exec ... psql`` with ``default_transaction_read_only=on``
and an explicit ``--kube-context`` (the global context is never touched), cached as CSV
**outside the repository** (``--cache-dir``); ``--offline`` forbids the call. Only temperatures,
powers and switch states are printed: no energy totals, costs, addresses or tokens.

Usage::

    uv run python hack/battery_thermal_derate.py --kube-context <context> \\
        --cache-dir <dir outside the repo> [--out report.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.signal import lfilter

STEP_S = 30
MAX_AGE_S = {"temp": 300.0}  # a temperature older than this is missing; the others hold
HORIZONS_MIN = (10, 20, 30, 45, 60, 90)
ORIGIN_STRIDE = 4  # steps between forecast origins (2 min)
TAU_GRID_MIN = tuple(float(x) for x in np.geomspace(8.0, 480.0, 49))
SPIKE_C = 1.5  # a temperature event this far from the local median is a sensor glitch
ACTIVE_KW = 0.1
ONLINE_LOOKBACK_H = 3.0
BOOTSTRAP_DRAWS = 400
BOOTSTRAP_BLOCK_H = 2.0
ONLINE_EST_STEPS = 60  # 30 min rows estimate the ambient online
GAP_MIN_DEFAULT = 60.0
COMMISSIONED_UTC = (
    "2026-11-03T19:14:32Z"  # first AC energy (regime log); earlier data is cold start
)
PRIOR_FIT = {"tau_min": 38.0, "k_c": 0.208 / 0.0266, "t_amb": 33.4}  # docs/planning study

ENTITIES = {
    "temp": "sensor.venus_internal_temperature",
    "power": "sensor.venus_ac_power",  # + discharging, - charging
    "soc": "sensor.venus_battery_soc",
    "set_charge": "number.venus_set_charge_power",
    "pd_target": "number.omnibattery_pd_target_grid_power",
    "sys_max_charge": "number.omnibattery_system_max_charge_power",
    "venus_max_charge": "number.venus_max_charge_power",
    "pd_min_charge": "number.omnibattery_pd_min_charge_power",
    "sw": "switch.omnibattery_temp_charge_limit",
    "sw_discharge": "switch.omnibattery_temp_charge_limit_discharge",
    "limit_c": "number.omnibattery_temp_charge_limit_c",
    "band_c": "number.omnibattery_temp_charge_limit_band_c",
    "floor_pct": "number.omnibattery_temp_charge_limit_floor_pct",
}
BOOL = {"on": 1.0, "off": 0.0}


# --------------------------------------------------------------------------------------
# data access
# --------------------------------------------------------------------------------------


def fetch_states(args: argparse.Namespace, start: float) -> Path:
    """Run the one read-only query (or find the cache) and return the CSV path."""
    cache = Path(args.cache_dir) / "ha_states.csv"
    if args.offline:
        if not cache.exists():
            raise SystemExit(f"--offline: no cached {cache}")
        return cache
    names = ",".join(f"'{e}'" for e in ENTITIES.values())
    sql = (
        "set default_transaction_read_only=on; copy (select sm.entity_id, s.last_updated_ts, "
        "s.state from states s join states_meta sm using(metadata_id) "
        f"where sm.entity_id in ({names}) and s.last_updated_ts >= {start:.0f} "
        "order by 2) to stdout with csv"
    )
    cmd = [
        "kubectl",
        f"--context={args.kube_context}",
        "-n",
        args.namespace,
        "exec",
        args.pod,
        "-c",
        "postgres",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "home_assistant",
        "-Atc",
        sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"psql failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(proc.stdout)
    return cache


def load_events(path: Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """``{key: (t_epoch_s, value)}``; non-numeric states other than on/off are NaN (unavailable)."""
    by_entity = {e: k for k, e in ENTITIES.items()}
    raw: dict[str, list[tuple[float, float]]] = {k: [] for k in ENTITIES}
    for row in csv.reader(path.read_text().splitlines()):
        if len(row) != 3 or row[0] not in by_entity:
            continue
        try:
            t = float(row[1])
        except ValueError:
            continue
        s = row[2]
        if s in BOOL:
            v = BOOL[s]
        else:
            try:
                v = float(s)
            except ValueError:
                v = math.nan
        raw[by_entity[row[0]]].append((t, v))
    out = {}
    for k, rows in raw.items():
        rows.sort()
        arr = np.asarray(rows, dtype=float).reshape(-1, 2)
        out[k] = (arr[:, 0], arr[:, 1])
    return out


# --------------------------------------------------------------------------------------
# pure helpers (unit-tested in tests/test_battery_thermal_derate.py)
# --------------------------------------------------------------------------------------


def despike(values: np.ndarray, *, half_window: int = 3, threshold: float = SPIKE_C) -> np.ndarray:
    """NaN the events further than ``threshold`` from the median of their neighbours."""
    out = values.copy()
    n = values.size
    for i in range(n):
        lo, hi = max(0, i - half_window), min(n, i + half_window + 1)
        neigh = np.delete(values[lo:hi], i - lo)
        neigh = neigh[~np.isnan(neigh)]
        if neigh.size >= 2 and abs(values[i] - np.median(neigh)) > threshold:
            out[i] = math.nan
    return out


def zoh(t: np.ndarray, v: np.ndarray, grid: np.ndarray, max_age_s: float | None) -> np.ndarray:
    """Zero-order hold of the events on ``grid``; NaN before the first event, after an
    unavailable event, and (with ``max_age_s``) when the last event is older than that."""
    out = np.full(grid.size, math.nan)
    if t.size == 0:
        return out
    idx = np.searchsorted(t, grid, side="right") - 1
    ok = idx >= 0
    out[ok] = v[idx[ok]]
    if max_age_s is not None:
        age = grid - t[np.clip(idx, 0, None)]
        out[~ok | (age > max_age_s)] = math.nan
    return out


def exp_filter(u: np.ndarray, a: float) -> np.ndarray:
    """``F[k+1] = a F[k] + (1 - a) u[k]``, ``F[0] = 0`` (the start value cancels in differences)."""
    f = lfilter([0.0, 1.0 - a], [1.0, -a], u)
    return f


def episodes(
    power_kw: np.ndarray, *, step_s: float = STEP_S, gap_min: float = GAP_MIN_DEFAULT
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
        cols += [np.maximum(-p, 0.0), np.maximum(p, 0.0)]
        names += ["k_c", "k_d"]
    if kind == "quad":
        cols.append(np.maximum(-p, 0.0) ** 2)
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


def fit_tau_profile(
    temp: np.ndarray,
    p_kw: np.ndarray,
    t_grid: np.ndarray,
    spans: list[tuple[int, int]],
    kind: str,
    horizons_steps: tuple[int, ...],
    taus_min: tuple[float, ...] = TAU_GRID_MIN,
) -> dict:
    """Least squares of the multi-step error for each ``tau`` on the grid; the best one wins.

    Returns ``{"tau_min", "beta", "names", "rmse", "n", "profile": [(tau, rmse), ...]}``."""
    best, profile = None, []
    for tau in taus_min:
        a = math.exp(-STEP_S / 60.0 / tau)
        f, names = inputs(p_kw, t_grid, kind, a)
        parts = [rows(temp, f, a, lo, hi, horizons_steps) for lo, hi in spans]
        y = np.concatenate([p[0] for p in parts])
        x = np.vstack([p[1] for p in parts])
        if y.size < x.shape[1] + 3:
            continue
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        rmse = float(np.sqrt(np.mean((y - x @ beta) ** 2)))
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
) -> dict:
    """Multi-step predictions inside ``span`` with a fitted model: arrays ``h, pred, obs, t0,
    p_mean`` (mean absolute power over the horizon, kW)."""
    a = math.exp(-STEP_S / 60.0 / fit["tau_min"])
    f, _ = inputs(p_kw, t_grid, kind, a)
    y, x, t0, idx = rows(temp, f, a, span[0], span[1], horizons_steps)
    if y.size == 0:
        return {k: np.empty(0) for k in ("h", "pred", "obs", "t0", "p_mean")}
    b = fit["beta"] if beta is None else beta
    ah = a ** idx[:, 1]
    pabs = np.cumsum(np.concatenate([[0.0], np.abs(np.nan_to_num(p_kw))]))
    p_mean = (pabs[idx[:, 0] + idx[:, 1]] - pabs[idx[:, 0]]) / idx[:, 1]
    return {
        "h": idx[:, 1] * STEP_S / 60.0,
        "pred": ah * t0 + x @ b,
        "obs": y + ah * t0,
        "t0": t0,
        "p_mean": p_mean,
    }


def free_run(
    temp0: float,
    p_kw: np.ndarray,
    tau_min: float,
    t_amb: float,
    k_c: float,
    k_d: float,
    k_c2: float = 0.0,
) -> np.ndarray:
    """Exact discretisation of the first-order node on the 30 s grid from ``temp0``."""
    a = math.exp(-STEP_S / 60.0 / tau_min)
    p = np.nan_to_num(p_kw)
    out = np.empty(p.size + 1)
    out[0] = temp0
    for k in range(p.size):
        pc = max(-p[k], 0.0)
        target = t_amb + k_c * pc + k_c2 * pc * pc + k_d * max(p[k], 0.0)
        out[k + 1] = a * out[k] + (1.0 - a) * target
    return out


def steady_state(t_amb: float, k_c: float, kw: float) -> float:
    return t_amb + k_c * kw


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
    """Charge power cap in W: ``limit * factor``, never below the driver's minimum power."""
    f = derate_factor(temp_c, limit_c, band_c, floor_pct)
    if f >= 1.0:
        return limit_w
    return max(limit_w * f, min(min_power_w, limit_w))


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


# --------------------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------------------


def score(pred: dict) -> dict:
    """RMSE / bias of the model and of persistence by horizon, plus the pooled figures."""
    out = {"by_h": {}}
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


def concat_pred(parts: list[dict]) -> dict:
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


def ambient_oracle_beta(fit: dict, y: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Keep the gains, refit only ``T_amb`` on the held-out episode (an oracle, not a forecast)."""
    beta = fit["beta"].copy()
    resid = y - x[:, 1:] @ beta[1:]
    beta[0] = float(np.linalg.lstsq(x[:, :1], resid, rcond=None)[0][0])
    return beta


def online_ambient_pred(
    fit: dict,
    temp: np.ndarray,
    p_kw: np.ndarray,
    t_grid: np.ndarray,
    kind: str,
    span: tuple[int, int],
    horizons_steps: tuple[int, ...],
    *,
    lookback_h: float = ONLINE_LOOKBACK_H,
    est_steps: int = ONLINE_EST_STEPS,
) -> dict:
    """Predictions with ``T_amb`` re-estimated at each origin from the preceding hours only
    (the gains and ``tau`` stay as fitted): the deployable form of the ambient correction."""
    a = math.exp(-STEP_S / 60.0 / fit["tau_min"])
    f, _ = inputs(p_kw, t_grid, kind, a)
    beta = fit["beta"]
    hy, hx, _, hidx = rows(temp, f, a, 0, temp.size, (est_steps,))
    resid = hy - hx[:, 1:] @ beta[1:]
    y, x, t0, idx = rows(temp, f, a, span[0], span[1], horizons_steps)
    amb = {}
    for k0 in np.unique(idx[:, 0]):
        sel = (hidx[:, 0] <= k0 - est_steps) & (hidx[:, 0] >= k0 - lookback_h * 3600 / STEP_S)
        denom = float(hx[sel, 0] @ hx[sel, 0])
        amb[k0] = (
            float(hx[sel, 0] @ resid[sel]) / denom if sel.sum() >= 20 and denom > 0 else beta[0]
        )
    t_amb = np.array([amb[k] for k in idx[:, 0]])
    ah = a ** idx[:, 1]
    pabs = np.cumsum(np.concatenate([[0.0], np.abs(np.nan_to_num(p_kw))]))
    return {
        "h": idx[:, 1] * STEP_S / 60.0,
        "pred": ah * t0 + x[:, 1:] @ beta[1:] + x[:, 0] * t_amb,
        "obs": y + ah * t0,
        "t0": t0,
        "p_mean": (pabs[idx[:, 0] + idx[:, 1]] - pabs[idx[:, 0]]) / idx[:, 1],
    }


def validate(
    temp: np.ndarray, p_kw: np.ndarray, t_grid: np.ndarray, spans: list[tuple[int, int]], kind: str
) -> dict:
    """Leave-one-episode-out and walk-forward predictions for one model ``kind``."""
    hs = tuple(round(h * 60 / STEP_S) for h in HORIZONS_MIN)
    full = fit_tau_profile(temp, p_kw, t_grid, spans, kind, hs)
    out = {
        "fit_all": _fit_summary(full),
        "loeo": {},
        "walk_forward": {},
        "jackknife": [],
    }
    loeo_parts, loeo_oracle, loeo_online, wf_parts = [], [], [], []
    for i, span in enumerate(spans):
        train = [s for j, s in enumerate(spans) if j != i]
        try:
            fit = fit_tau_profile(temp, p_kw, t_grid, train, kind, hs)
        except ValueError:
            continue
        out["jackknife"].append({"left_out": i, **_fit_summary(fit)})
        pr = predict_rows(fit, temp, p_kw, t_grid, kind, span, hs)
        if pr["h"].size == 0:
            continue
        out["loeo"][str(i)] = score(pr)
        loeo_parts.append(pr)
        a = math.exp(-STEP_S / 60.0 / fit["tau_min"])
        f, _ = inputs(p_kw, t_grid, kind, a)
        y, x, t0, idx = rows(temp, f, a, span[0], span[1], hs)
        bo = ambient_oracle_beta(fit, y, x)
        loeo_oracle.append(predict_rows(fit, temp, p_kw, t_grid, kind, span, hs, beta=bo))
        loeo_online.append(online_ambient_pred(fit, temp, p_kw, t_grid, kind, span, hs))
        out["loeo_online_ambient"] = out.get("loeo_online_ambient", {})
        out["loeo_online_ambient"][str(i)] = score(loeo_online[-1])
        if i >= 1:
            try:
                wf = fit_tau_profile(temp, p_kw, t_grid, spans[:i], kind, hs)
                wpr = predict_rows(wf, temp, p_kw, t_grid, kind, span, hs)
            except ValueError:
                continue
            if wpr["h"].size:
                out["walk_forward"][str(i)] = score(wpr)
                wf_parts.append(wpr)
    out["loeo_pooled"] = score(concat_pred(loeo_parts))
    out["loeo_ambient_oracle_pooled"] = score(concat_pred(loeo_oracle))
    out["loeo_online_ambient_pooled"] = score(concat_pred(loeo_online))
    if wf_parts:
        out["walk_forward_pooled"] = score(concat_pred(wf_parts))
    return out


def _fit_summary(fit: dict) -> dict:
    prof = np.asarray(fit["profile"])
    near = prof[prof[:, 1] <= prof[:, 1].min() * 1.05, 0]
    return {
        "tau_min": fit["tau_min"],
        "coef": {n: float(b) for n, b in zip(fit["names"], fit["beta"], strict=True)},
        "rmse_fit": fit["rmse"],
        "n_rows": fit["n"],
        "tau_within_5pct_of_best": [float(near.min()), float(near.max())],
    }


def prior_fit_score(
    temp: np.ndarray, p_kw: np.ndarray, t_grid: np.ndarray, spans: list[tuple[int, int]]
) -> dict:
    """The earlier study's one-session fit (``k_d`` taken equal to ``k_c``) as a predictor."""
    hs = tuple(round(h * 60 / STEP_S) for h in HORIZONS_MIN)
    fit = {
        "tau_min": PRIOR_FIT["tau_min"],
        "beta": np.array([PRIOR_FIT["t_amb"], PRIOR_FIT["k_c"], PRIOR_FIT["k_c"]]),
    }
    parts = [predict_rows(fit, temp, p_kw, t_grid, "split", s, hs) for s in spans]
    parts = [p for p in parts if p["h"].size]
    return score(concat_pred(parts))


def session_free_run(
    temp: np.ndarray, p_kw: np.ndarray, t_grid: np.ndarray, span: tuple[int, int], fit: dict
) -> dict:
    """Free simulation of a whole episode from its first observed temperature."""
    lo, hi = span
    k = lo + int(np.flatnonzero(~np.isnan(temp[lo:hi]))[0])
    c = fit["coef"]
    sim = free_run(
        temp[k], p_kw[k:hi], fit["tau_min"], c["t_amb"], c["k_c"], c["k_d"], c.get("k_c2", 0.0)
    )
    obs = temp[k : hi + 1]
    seen = ~np.isnan(obs[: sim.size]) & (np.arange(obs.size) < sim.size)
    err = sim[: obs.size][seen] - obs[seen]
    return {
        "start_utc": _iso(t_grid[k]),
        "hours": float((hi - k) * STEP_S / 3600.0),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "rmse_persistence": float(np.sqrt(np.mean((temp[k] - obs[seen]) ** 2))),
        "peak_obs": float(np.nanmax(obs)),
        "peak_sim": float(sim.max()),
        "end_obs": float(obs[~np.isnan(obs)][-1]),
        "end_sim": float(sim[: obs.size][~np.isnan(obs)][-1]),
    }


def derate_check(
    ev: dict[str, tuple[np.ndarray, np.ndarray]], grid: np.ndarray, temp: np.ndarray
) -> dict:
    """Predicted cap against the written charge power from the first ``on`` of the switch."""
    sw_t, sw_v = ev["sw"]
    on = np.flatnonzero(sw_v == 1.0)
    if on.size == 0:
        return {"status": "the switch was never on in the window"}
    t_on = float(sw_t[on[-1]])  # the last turn-on; an on state before it would be a re-write
    params = {}
    for key in ("limit_c", "band_c", "floor_pct", "sys_max_charge", "venus_max_charge"):
        t, v = ev[key]
        i = np.searchsorted(t, t_on, side="right") - 1
        params[key] = float(v[i]) if i >= 0 else math.nan
    limit_w = min(params["sys_max_charge"], params["venus_max_charge"])
    set_t, set_v = ev["set_charge"]
    pw = zoh(*ev["power"], grid, None)
    sw_g = zoh(*ev["sw"], grid, None)
    cap = np.array(
        [
            derated_cap_w(t, limit_w, params["limit_c"], params["band_c"], params["floor_pct"])
            for t in temp
        ]
    )
    after = (grid >= t_on + 90.0) & (sw_g == 1.0)  # 90 s: the integration's reaction time
    set_g = zoh(set_t, set_v, grid, None)
    rows_out = {
        "switch_on_utc": _iso(t_on),
        "params": params,
        "window_min_after_on": float((grid[-1] - t_on) / 60.0),
    }
    ok = after & ~np.isnan(temp) & ~np.isnan(set_g)
    rows_out["n_grid_points"] = int(ok.sum())
    if ok.sum() == 0:
        rows_out["status"] = "no usable point after the switch turned on"
        return rows_out
    factor = np.array(
        [
            derate_factor(t, params["limit_c"], params["band_c"], params["floor_pct"])
            for t in temp[ok]
        ]
    )
    rows_out.update(
        {
            "temp_range": [float(temp[ok].min()), float(temp[ok].max())],
            "predicted_factor_range": [float(factor.min()), float(factor.max())],
            "predicted_cap_w_range": [float(cap[ok].min()), float(cap[ok].max())],
            "set_charge_w_range": [float(set_g[ok].min()), float(set_g[ok].max())],
            "ac_charge_w_range": [float((-pw[ok]).min()), float((-pw[ok]).max())],
            "max_set_minus_cap_w": float((set_g[ok] - cap[ok]).max()),
            "mean_set_minus_cap_w": float((set_g[ok] - cap[ok]).mean()),
            "share_set_within_30w_of_cap": float(np.mean(np.abs(set_g[ok] - cap[ok]) <= 30.0)),
            "share_set_above_cap_plus_30w": float(np.mean(set_g[ok] > cap[ok] + 30.0)),
            "ac_over_set_median": float(np.median((-pw[ok]) / np.maximum(set_g[ok], 1.0))),
        }
    )
    before = (grid >= t_on - 600.0) & (grid < t_on - 5.0) & ~np.isnan(temp) & ~np.isnan(set_g)
    if before.any():
        rows_out["ten_minutes_before_on"] = {
            "temp_range": [float(temp[before].min()), float(temp[before].max())],
            "set_charge_w_range": [float(set_g[before].min()), float(set_g[before].max())],
            "counterfactual_cap_w_range": [float(cap[before].min()), float(cap[before].max())],
        }
    # the step: first set value after the switch turned on
    j = np.flatnonzero(set_t > t_on)
    if j.size:
        rows_out["first_set_after_on"] = {
            "delay_s": float(set_t[j[0]] - t_on),
            "value_w": float(set_v[j[0]]),
        }
    return rows_out


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def block_bootstrap(
    temp: np.ndarray,
    p_kw: np.ndarray,
    t_grid: np.ndarray,
    spans: list[tuple[int, int]],
    kind: str,
    tau_min: float,
    *,
    draws: int = BOOTSTRAP_DRAWS,
    block_h: float = BOOTSTRAP_BLOCK_H,
    seed: int = 0,
) -> dict:
    """Moving-block bootstrap of the coefficients at a fixed ``tau`` (rows of one block share an
    hour-scale error, so whole ``block_h`` blocks are resampled). Returns the draws."""
    hs = tuple(round(h * 60 / STEP_S) for h in HORIZONS_MIN)
    a = math.exp(-STEP_S / 60.0 / tau_min)
    f, names = inputs(p_kw, t_grid, kind, a)
    parts = [rows(temp, f, a, lo, hi, hs) for lo, hi in spans]
    y = np.concatenate([q[0] for q in parts])
    x = np.vstack([q[1] for q in parts])
    block = np.concatenate([q[3][:, 0] for q in parts]) // int(block_h * 3600 / STEP_S)
    ids = np.unique(block)
    members = [np.flatnonzero(block == b) for b in ids]
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(draws):
        pick = rng.integers(0, len(ids), size=len(ids))
        sel = np.concatenate([members[k] for k in pick])
        beta, *_ = np.linalg.lstsq(x[sel], y[sel], rcond=None)
        out.append(beta)
    return {"names": names, "betas": np.asarray(out)}


def interval(values: np.ndarray, lo: float = 5.0, hi: float = 95.0) -> list[float]:
    return [float(np.percentile(values, lo)), float(np.percentile(values, hi))]


def run(ev: dict, start: float, end: float, gap_min: float) -> dict:
    grid = np.arange(start - start % STEP_S, end, STEP_S, dtype=float)
    t_ev, v_ev = ev["temp"]
    v_clean = despike(v_ev)
    keep = ~(np.isnan(v_clean) & ~np.isnan(v_ev))  # a glitch is dropped, "unavailable" stays
    temp = zoh(t_ev[keep], v_clean[keep], grid, MAX_AGE_S["temp"])
    p_kw = -zoh(*ev["power"], grid, None) / 1000.0  # + = charging
    spans = [s for s in episodes(p_kw, gap_min=gap_min) if np.isfinite(temp[s[0] : s[1]]).any()]
    report: dict = {
        "window": [_iso(grid[0]), _iso(grid[-1])],
        "hours": float((grid[-1] - grid[0]) / 3600.0),
        "despiked_events": int(np.isnan(v_clean).sum() - np.isnan(v_ev).sum()),
        "episodes": [],
    }
    for i, (lo, hi) in enumerate(spans):
        seg = p_kw[lo:hi]
        report["episodes"].append(
            {
                "id": i,
                "start": _iso(grid[lo]),
                "hours": float((hi - lo) * STEP_S / 3600.0),
                "max_charge_kw": float(np.nanmax(np.maximum(seg, 0.0))),
                "mean_charge_kw_when_charging": _mean_where(seg, seg > ACTIVE_KW),
                "max_discharge_kw": float(np.nanmax(np.maximum(-seg, 0.0))),
                "temp_min": float(np.nanmin(temp[lo:hi])),
                "temp_max": float(np.nanmax(temp[lo:hi])),
            }
        )
    report["models"] = {
        kind: validate(temp, p_kw, grid, spans, kind)
        for kind in ("split", "pooled", "quad", "harmonic")
    }
    report["prior_study_fit_as_predictor"] = prior_fit_score(temp, p_kw, grid, spans)
    split = report["models"]["split"]["fit_all"]
    report["free_run_loeo"] = []
    hs = tuple(round(h * 60 / STEP_S) for h in HORIZONS_MIN)
    for kind in ("split", "quad"):
        for i, span in enumerate(spans):
            train = [s for j, s in enumerate(spans) if j != i]
            fit = _fit_summary(fit_tau_profile(temp, p_kw, grid, train, kind, hs))
            if fit["coef"]["k_c"] <= 0 or span[1] - span[0] < 120:
                continue
            fr = session_free_run(temp, p_kw, grid, span, fit)
            report["free_run_loeo"].append({"model": kind, "episode": i, **fr})
    c = split["coef"]
    report["steady_state"] = {
        "t_amb": c["t_amb"],
        "k_c_degC_per_kw": c["k_c"],
        "k_d_degC_per_kw": c["k_d"],
        "tau_min": split["tau_min"],
        "charge_kw_for_40degC": (40.0 - c["t_amb"]) / c["k_c"] if c["k_c"] > 0 else None,
    }
    bs = block_bootstrap(temp, p_kw, grid, spans, "split", split["tau_min"])
    report["bootstrap_split_at_best_tau"] = {
        n: interval(bs["betas"][:, i]) for i, n in enumerate(bs["names"])
    }
    report["derate"] = derate_check(ev, grid, temp)
    d = report["derate"]
    if "params" in d and c["k_c"] > 0:
        pr = d["params"]
        args = (
            min(pr["sys_max_charge"], pr["venus_max_charge"]) / 1000.0,
            pr["limit_c"],
            pr["band_c"],
            pr["floor_pct"],
        )
        eq = equilibrium_charge_kw(c["t_amb"], c["k_c"], *args)
        report["derate_equilibrium"] = {"charge_kw": eq[0], "temp_c": eq[1]}
        boot_eq = np.array(
            [equilibrium_charge_kw(b[0], b[1], *args) for b in bs["betas"] if b[1] > 0]
        )
        report["derate_equilibrium"]["bootstrap_p5_p95_kw"] = interval(boot_eq[:, 0])
        jk = [
            equilibrium_charge_kw(j["coef"]["t_amb"], j["coef"]["k_c"], *args)
            for j in report["models"]["split"]["jackknife"]
            if j["coef"]["k_c"] > 0
        ]
        report["derate_equilibrium"]["jackknife_range_kw"] = [
            min(q[0] for q in jk),
            max(q[0] for q in jk),
        ]
    return report


def _mean_where(x: np.ndarray, mask: np.ndarray) -> float | None:
    return float(np.nanmean(x[mask])) if mask.any() else None


def _fmt_score(s: dict) -> str:
    p = s.get("pooled")
    if not p:
        return "no data"
    line = f"n {p['n']}, rmse {p['rmse']:.2f} (persistence {p['rmse_persistence']:.2f})"
    hot = s.get("pooled_above_1kw")
    if hot:
        line += (
            f"; >1 kW: n {hot['n']}, rmse {hot['rmse']:.2f} "
            f"(persistence {hot['rmse_persistence']:.2f}), bias {hot['bias']:+.2f}"
        )
    return line


def print_report(r: dict) -> None:
    p = print
    p(
        f"window {r['window'][0]} to {r['window'][1]} ({r['hours']:.1f} h); "
        f"despiked temperature events {r['despiked_events']}"
    )
    p("\nepisodes (split at the midpoint of idle gaps)")
    p("| id | start (UTC) | h | max charge kW | mean charge kW | max discharge kW | T range |")
    p("|---|---|---|---|---|---|---|")
    for e in r["episodes"]:
        mc = e["mean_charge_kw_when_charging"]
        p(
            f"| {e['id']} | {e['start']} | {e['hours']:.1f} | {e['max_charge_kw']:.2f} | "
            f"{'-' if mc is None else f'{mc:.2f}'} | {e['max_discharge_kw']:.2f} | "
            f"{e['temp_min']:.1f} to {e['temp_max']:.1f} |"
        )
    for kind, m in r["models"].items():
        fa = m["fit_all"]
        coef = ", ".join(f"{k} {v:.3f}" for k, v in fa["coef"].items())
        p(
            f"\nmodel {kind}: tau {fa['tau_min']:.0f} min (within 5% of best rmse: "
            f"{fa['tau_within_5pct_of_best'][0]:.0f} to {fa['tau_within_5pct_of_best'][1]:.0f}), "
            f"{coef}; in-sample rmse {fa['rmse_fit']:.2f} degC over {fa['n_rows']} rows"
        )
        jk = m["jackknife"]
        if jk:
            for name in fa["coef"]:
                vals = [j["coef"][name] for j in jk]
                p(f"  jackknife {name}: {min(vals):.3f} to {max(vals):.3f}")
            taus = [j["tau_min"] for j in jk]
            p(f"  jackknife tau: {min(taus):.0f} to {max(taus):.0f} min")
        p(f"  leave-one-episode-out: {_fmt_score(m['loeo_pooled'])}")
        p(f"  online ambient (last 3 h): {_fmt_score(m['loeo_online_ambient_pooled'])}")
        p(f"  ambient oracle (upper bound): {_fmt_score(m['loeo_ambient_oracle_pooled'])}")
        for i, sc in m["loeo"].items():
            p(f"    episode {i} held out: {_fmt_score(sc)}")
        if "walk_forward_pooled" in m:
            p(f"  walk-forward: {_fmt_score(m['walk_forward_pooled'])}")
        by_h = m["loeo_pooled"]["by_h"]
        p(
            "  LOEO by horizon (min: rmse / persistence / bias): "
            + "; ".join(
                f"{h}: {v['rmse']:.2f}/{v['rmse_persistence']:.2f}/{v['bias']:+.2f}"
                for h, v in by_h.items()
            )
        )
    p(f"\nearlier study's fit as a predictor: {_fmt_score(r['prior_study_fit_as_predictor'])}")
    p("\nfree run of each episode from its first temperature (parameters from the other episodes)")
    for fr in r["free_run_loeo"]:
        p(
            f"  {fr['model']} episode {fr['episode']} from {fr['start_utc']} "
            f"({fr['hours']:.1f} h): rmse "
            f"{fr['rmse']:.2f} (persistence {fr['rmse_persistence']:.2f}); peak obs "
            f"{fr['peak_obs']:.1f} sim {fr['peak_sim']:.1f}; end obs {fr['end_obs']:.1f} "
            f"sim {fr['end_sim']:.1f}"
        )
    s = r["steady_state"]
    p(
        f"\nsteady state: T_amb {s['t_amb']:.1f} degC, k_c {s['k_c_degC_per_kw']:.2f} degC/kW, "
        f"k_d {s['k_d_degC_per_kw']:.2f} degC/kW, tau {s['tau_min']:.0f} min; charge power "
        f"that holds 40 degC: {s['charge_kw_for_40degC']}"
    )
    bsr = r["bootstrap_split_at_best_tau"]
    p(
        "block bootstrap at the best tau, p5 to p95: "
        + ", ".join(f"{k} {v[0]:.2f} to {v[1]:.2f}" for k, v in bsr.items())
    )
    d = r["derate"]
    p("\nderate check")
    for k, v in d.items():
        p(f"  {k}: {v}")
    if "derate_equilibrium" in r:
        e = r["derate_equilibrium"]
        p(
            f"  equilibrium (thermal steady state meets the curve): {e['charge_kw']:.2f} kW at "
            f"{e['temp_c']:.1f} degC; bootstrap p5 to p95 {e['bootstrap_p5_p95_kw'][0]:.2f} to "
            f"{e['bootstrap_p5_p95_kw'][1]:.2f} kW; jackknife {e['jackknife_range_kw'][0]:.2f} to "
            f"{e['jackknife_range_kw'][1]:.2f} kW"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--kube-context", help="kubectl context; required unless --offline")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-postgres-0")
    ap.add_argument("--cache-dir", required=True, help="outside the repository")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--start", default=COMMISSIONED_UTC, help="first instant used (default: AC on)")
    ap.add_argument("--end", default="now")
    ap.add_argument("--episode-gap-min", type=float, default=GAP_MIN_DEFAULT)
    ap.add_argument("--out", default=None, help="write the JSON report here")
    args = ap.parse_args(argv)
    if not args.offline and not args.kube_context:
        ap.error("--kube-context is required (the global context is never used)")
    if (repo := Path(__file__).resolve().parents[1]) in Path(args.cache_dir).resolve().parents:
        ap.error(f"--cache-dir must be outside the repository ({repo})")
    start = _ts(args.start)
    end = time.time() if args.end == "now" else _ts(args.end)
    path = fetch_states(args, start - 48 * 3600)
    ev = load_events(path)
    report = run(ev, start, end, args.episode_gap_min)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=float) + "\n")
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
