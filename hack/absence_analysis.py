#!/usr/bin/env python3
"""Empty-house analysis of past whole-house absences for vacation mode (ADR-0010). Read-only.

For every absence window (``--absences``, a private JSON list of ``[label, leave, return]`` in local
time; heating was off and nobody was home) it reads the raw climate series from Prometheus (the live
shadow's own sources and slot rules, :func:`battery_optimizer.climate.shadow.build_live_data`) and
answers four questions:

1. **Cooling**: room-mean and per-room trajectory while heating stays off (the coasting window:
   Leave to the first heating slot), the outdoor mean, daily means, and the pooled settled level
   ``T_air = c + s T_out`` of coasting days 4 and later (the empty house's equilibrium); whether
   the live observer keeps a state through the absence.
2. **Coasting check of the model**: B2+D (the live shadow prior, ``--prior``) open loop with
   ``Q = 0`` from the observer state at Leave against the measured room mean at 24/48/72/120 h
   (bias, RMSE): the prior, the prior with ``ua``/``q0`` re-estimated as the service does on the
   28 days before Leave, and empty-house variants with the diurnal gain ``D = 0``: ``q0`` refitted
   on the coasting windows with the physics fixed (also leave-one-out), ``q0`` and ``ua``, and every
   parameter (exploratory, in sample).
3. **Warm-up**: measured, from the first heating slot after the absence to the room mean (and each
   room) at 21 degC, with the heat delivered; the model's prediction of the same warm-up from the
   measured heat; and the model's fastest recovery (top water level every slot, the prior's heat
   law and a 6 kW cap) from the coldest observed state at constant outdoor temperatures.

Prometheus is read only (``--prometheus-url``, e.g. a ``kubectl port-forward`` of the long-term
instance); the raw samples are cached under ``<data_dir>/analysis/v1/absence-analysis/raw/``.
Outputs (``report.json``, ``report.md``, hourly ``trajectories.csv``) go to
``<data_dir>/analysis/v1/absence-analysis/<run-id>/``: private household data, never committed.

Usage:
    uv run python hack/absence_analysis.py --absences <file.json> --prior <prior id> \\
        --prometheus-url http://localhost:19090 [--data-dir <store>] [--run-id <name>]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from scipy.optimize import least_squares

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "src"))

from battery_optimizer import extract_prom  # noqa: E402
from battery_optimizer.climate import b2d, shadow, thermal  # noqa: E402
from battery_optimizer.climate.config import load_climate_config  # noqa: E402
from battery_optimizer.climate.data import SITE_FILE, ClimateData, load_site  # noqa: E402
from battery_optimizer.climate.mpc_config import load_mpc_config  # noqa: E402
from battery_optimizer.climate.mpc_model import curve_water  # noqa: E402
from battery_optimizer.features import RawSeries  # noqa: E402
from battery_optimizer.paths import resolve_data_dir  # noqa: E402
from battery_optimizer.service.history import fetch_points  # noqa: E402

log = logging.getLogger("absence_analysis")
MODEL: list[Any] = []  # the prior's model, for the observer check

OUT_DIR = Path("analysis") / "v1" / "absence-analysis"
SLOTS_H = 4
LEADS_H = (24, 48, 72, 120)
BEFORE_DAYS = 30  # the service's 28-day re-estimation window, plus margin
AFTER_DAYS = 4
HEAT_ON_KW = 0.3  # a slot with more space heat than this counts as heating
FLOOR_C = 21.0
DAY_MIN_C = (23.5, 24.0)
WARMUP_T_OUT = (0.0, 3.0, 6.0, 9.0, 12.0)
WARMUP_MAX_H = 120
MIN_COAST_H = 24  # an absence whose heating stayed off for less than this is not a coasting case
GRACE_SLOTS = 4  # heating in the first hour after Leave is the tail of the last run
#: the series the job reads: the live shadow's, without the comfort helpers (which did not exist)
SOURCES = {k: v for k, v in shadow.live_sources(None).items() if not k.startswith("comfort_")}


@dataclass
class Absence:
    label: str
    leave: datetime  # UTC
    ret: datetime  # UTC
    d: ClimateData
    w: np.ndarray  # observer inputs
    outdoor: np.ndarray
    t_leave: int
    t_return: int
    t_coast_end: int = 0  # first heating slot after Leave (heating back on), else Return


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def _floor_day(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def fetch(base_url: str, start: datetime, end: datetime, cache: Path) -> dict[str, RawSeries]:
    """Raw samples of :data:`SOURCES` on ``[start, end)``, cached as one ``.npz``."""
    if not cache.exists():
        arrays: dict[str, np.ndarray] = {}
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            for i, (key, (entity, metric)) in enumerate(SOURCES.items()):
                pts = fetch_points(client, base_url, entity, metric, start, end)
                t = np.array([extract_prom.to_micros(p.ts) for p in pts], dtype=np.int64)
                v = np.array([p.value for p in pts], dtype=np.float64)
                order = np.argsort(t, kind="stable")
                t, v = t[order], v[order]
                keep = np.r_[True, np.diff(t) > 0] if t.size else np.zeros(0, bool)
                arrays[f"t{i}"], arrays[f"v{i}"] = t[keep], v[keep]
                log.info("  %s: %d samples", key, int(keep.sum()))
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache, **arrays)
    raw = np.load(cache)
    return {
        key: RawSeries(entity=e, metric=m, unit="", t_us=raw[f"t{i}"], values=raw[f"v{i}"])
        for i, (key, (e, m)) in enumerate(SOURCES.items())
    }


def load_absence(
    label: str, leave_local: str, ret_local: str, tz: ZoneInfo, base_url: str, root: Path, *cfgs
) -> Absence:
    ccfg, site = cfgs
    leave = datetime.fromisoformat(leave_local).replace(tzinfo=tz).astimezone(UTC)
    ret = datetime.fromisoformat(ret_local).replace(tzinfo=tz).astimezone(UTC)
    start = _floor_day(leave) - timedelta(days=BEFORE_DAYS)
    end = _floor_day(ret) + timedelta(days=AFTER_DAYS + 1)
    end = min(end, datetime.now(UTC).replace(minute=0, second=0, microsecond=0))
    cache = root / OUT_DIR / "raw" / f"{label}-{start:%Y%m%d}-{end:%Y%m%d}.npz"
    log.info("%s: %s to %s", label, start.date(), end.date())
    series = fetch(base_url, start, end, cache)
    d = shadow.build_live_data(series, start=start, end=end, config=ccfg, site=site)
    return Absence(label, leave, ret, d, np.empty(0), shadow.outdoor_series(d), 0, 0)


def slot_of(d: ClimateData, at: datetime) -> int:
    return int(np.searchsorted(d.starts_us, int(at.timestamp() * 1e6), side="right")) - 1


# ---------------------------------------------------------------------------
# Measured
# ---------------------------------------------------------------------------


def _first_hour_mean(y: np.ndarray, t: int) -> float:
    seg = y[t : t + SLOTS_H]
    return float(np.nanmean(seg)) if np.isfinite(seg).any() else float("nan")


def cooling_stats(a: Absence) -> dict[str, Any]:
    d, lo, hi = a.d, a.t_leave, a.t_coast_end
    out: dict[str, Any] = {
        "absence_hours": round((a.t_return - lo) / SLOTS_H, 1),
        "coast_hours": round((hi - lo) / SLOTS_H, 1),
        "absence_space_heat_kwh": _r(np.nansum(d.q_heat_kwh[lo : a.t_return])),
        "absence_outdoor_mean_c": _r(np.nanmean(a.outdoor[lo : a.t_return])),
        "outdoor_mean_c": _r(np.nanmean(a.outdoor[lo:hi])),
        "outdoor_min_c": _r(np.nanmin(a.outdoor[lo:hi])),
        "space_heat_kwh": _r(np.nansum(d.q_heat_kwh[lo:hi])),
        "space_heat_slots_on": int(np.sum(d.q_heat_kw[lo:hi] > HEAT_ON_KW)),
        "dhw_slots": int(np.sum(d.dhw[lo:hi])),
        "coverage_t_air": _r(np.isfinite(d.t_air[lo:hi]).mean(), 3),
    }
    rooms: dict[str, Any] = {}
    for name, y in [("room_mean", d.t_air), *d.room_temp.items()]:
        seg = y[lo:hi]
        if not np.isfinite(seg).any():
            continue
        i_min = int(np.nanargmin(seg))
        rooms[name] = {
            "start_c": _r(_first_hour_mean(y, lo)),
            "min_c": _r(seg[i_min]),
            "hours_to_min": _r(i_min / SLOTS_H, 1),
            "end_c": _r(_first_hour_mean(y, hi - SLOTS_H)),
        }
    out["rooms"] = rooms
    out["daily"] = daily_means(a)
    out["observer"] = observer_continuity(a)
    return out


def daily_means(a: Absence) -> list[dict[str, Any]]:
    rows = []
    lo = a.t_leave
    while lo + 24 * SLOTS_H <= a.t_coast_end:
        hi = lo + 24 * SLOTS_H
        rows.append(
            {
                "day": len(rows) + 1,
                "t_air_c": _r(np.nanmean(a.d.t_air[lo:hi])),
                "outdoor_c": _r(np.nanmean(a.outdoor[lo:hi])),
            }
        )
        lo = hi
    return rows


def observer_continuity(a: Absence) -> dict[str, Any]:
    """Does the live observer keep a state through the whole absence (ADR-0010 open item)?"""
    model = MODEL[0]
    lo = max(0, a.t_leave - 2 * shadow.WARMUP_SLOTS)
    hi = min(a.d.n, a.t_return + SLOTS_H)
    w = shadow.bridge_outages(a.w, a.d.cooling, hi)
    xs, age = model.observe(w[lo:hi], a.d.t_air[lo:hi])
    seg = slice(a.t_leave - lo, a.t_return - lo)
    restarts = int(np.sum(age[seg] == 0))
    return {
        "state_share": _r(np.isfinite(xs[seg, 0]).mean(), 3),
        "restarts": restarts,
        "unknown_input_slots": int(np.sum(~np.isfinite(w[a.t_leave : a.t_return]).all(axis=1))),
    }


def equilibrium(absences: Sequence[Absence], min_day: int = 4) -> dict[str, Any]:
    """Pooled ``T_air = c + s T_out`` on the daily means of coasting days >= *min_day*."""
    pts = []
    for a in absences:
        for row in daily_means(a)[min_day - 1 :]:
            if row["t_air_c"] is not None and row["outdoor_c"] is not None:
                pts.append((a.label, row["day"], row["outdoor_c"], row["t_air_c"]))
    if len(pts) < 3:
        return {"days": pts}
    x = np.array([p[2] for p in pts])
    y = np.array([p[3] for p in pts])
    s_, c_ = np.polyfit(x, y, 1)
    return {
        "days": pts,
        "slope": _r(s_, 3),
        "intercept_c": _r(c_),
        "rmse_k": _r(np.sqrt(np.mean((c_ + s_ * x - y) ** 2)), 3),
        "t_eq_at_c": {str(t): _r(c_ + s_ * t) for t in (0, 3, 6, 9, 12, 15)},
    }


def measured_warmup(a: Absence, t_end: int | None = None) -> dict[str, Any]:
    """From the first heating slot at or after ``Leave + 12 h`` (heat turned back on) to 21 degC."""
    d = a.d
    t_end = d.n if t_end is None else t_end
    q = d.q_heat_kw
    search = np.flatnonzero(q[a.t_leave + 12 * SLOTS_H : t_end] > HEAT_ON_KW)
    if search.size == 0:
        return {"heat_on": None}
    t_on = a.t_leave + 12 * SLOTS_H + int(search[0])
    out: dict[str, Any] = {
        "heat_on_local": _local(d, t_on),
        "heat_on_minus_return_h": _r((t_on - a.t_return) / SLOTS_H, 2),
        "t_air_at_on_c": _r(_first_hour_mean(d.t_air, max(t_on - SLOTS_H, 0))),
        "outdoor_mean_next_24h_c": _r(np.nanmean(a.outdoor[t_on : t_on + 96])),
        "water_target_mean_c": _r(
            np.nanmean(d.columns["flow_target_temp_mean_c"][t_on : t_on + 96])
        ),
    }
    hp_in = d.e_heat_kwh
    targets = {"room_mean": (d.t_air, FLOOR_C)}
    for r in shadow.GATE_ROOMS:
        targets[r] = (d.room_temp[r], FLOOR_C)
    targets["hall_kitchen_23.5"] = (d.room_temp["hall_kitchen"], 23.5)
    reach: dict[str, Any] = {}
    for name, (y, level) in targets.items():
        seg = y[t_on:t_end]
        hit = np.flatnonzero(seg >= level)
        if hit.size == 0:
            reach[name] = {"hours": None, "max_c": _r(np.nanmax(seg)) if seg.size else None}
            continue
        k = int(hit[0])
        reach[name] = {
            "hours": _r(k / SLOTS_H, 2),
            "heat_out_kwh": _r(np.nansum(d.q_heat_kwh[t_on : t_on + k + 1])),
            "heat_in_kwh": _r(np.nansum(hp_in[t_on : t_on + k + 1])),
            "mean_heat_kw": _r(np.nanmean(q[t_on : t_on + k + 1])),
            "outdoor_mean_c": _r(np.nanmean(a.outdoor[t_on : t_on + k + 1])),
        }
    out["reach"] = reach
    out["max_heat_kw_24h"] = _r(np.nanmax(q[t_on : t_on + 96]))
    out["mean_heat_kw_12h"] = _r(np.nanmean(q[t_on : t_on + 48]))
    out["t_on"] = t_on
    return out


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def observer_inputs(a: Absence, n_diurnal_harmonics: int) -> np.ndarray:
    hour_frac, weekend = b2d.slot_calendar(a.d)
    diurnal = b2d.diurnal_matrix(hour_frac, weekend, n_diurnal_harmonics)
    return shadow.observer_inputs(a.d, diurnal)


def observe_at(model, a: Absence, t0: int) -> np.ndarray | None:
    lo = max(0, t0 - 2 * shadow.WARMUP_SLOTS)
    w = shadow.bridge_outages(a.w, a.d.cooling, t0 + 1)
    xs, _age = model.observe(w[lo : t0 + 1], a.d.t_air[lo : t0 + 1])
    x = xs[-1]
    return x if np.isfinite(x).all() else None


def coast(params: Mapping[str, float], n_diurnal: int, a: Absence, x0, t0: int, n: int):
    """Open-loop air states of slots ``t0..t0+n-1`` (end of slot) with ``Q = 0``."""
    ad, bd = b2d.rc_matrices(dict(params), n_diurnal)
    w = a.w[t0 : t0 + n].copy()
    w[:, 0] = 0.0
    w[:, 1] = a.outdoor[t0 : t0 + n]
    w = np.nan_to_num(w, nan=0.0)  # solar/diurnal are never unknown; outdoor is held
    return thermal.simulate(ad, w @ bd.T, x0)[1:, 0]


def errors(pred: np.ndarray, obs: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lead in LEADS_H:
        k = lead * SLOTS_H
        if k > pred.size:
            continue
        e = pred[:k] - obs[:k]
        e = e[np.isfinite(e)]
        end = pred[k - 1] - obs[k - 1]
        out[f"{lead}h"] = {
            "bias_k": _r(e.mean()),
            "rmse_k": _r(np.sqrt((e**2).mean())),
            "error_at_lead_k": _r(end),
        }
    full = pred - obs
    full = full[np.isfinite(full)]
    out["full"] = {"bias_k": _r(full.mean()), "rmse_k": _r(np.sqrt((full**2).mean()))}
    return out


def pooled(report: Mapping[str, Any]) -> dict[str, Any]:
    """Per variant and lead over the coasting cases: mean bias, RMS of the RMSEs, mean error at
    the lead, and the number of cases that reach the lead."""
    out: dict[str, Any] = {}
    for rec in report["absences"].values():
        for variant, errs in rec.get("coast", {}).items():
            for lead, e in errs.items():
                out.setdefault(variant, {}).setdefault(lead, []).append(e)
    summary: dict[str, Any] = {}
    for variant, leads in out.items():
        summary[variant] = {}
        for lead, es in leads.items():
            at = [e["error_at_lead_k"] for e in es if e.get("error_at_lead_k") is not None]
            summary[variant][lead] = {
                "cases": len(es),
                "bias_k": _r(np.mean([e["bias_k"] for e in es])),
                "rmse_k": _r(np.sqrt(np.mean([e["rmse_k"] ** 2 for e in es]))),
                "error_at_lead_k": _r(np.mean(at)) if at else None,
            }
    return summary


def write_markdown(report: Mapping[str, Any], path: Path) -> None:
    lines = [
        f"# Absence analysis (prior {report['prior_id']}, season {report['prior_season']})",
        "",
    ]
    lines += ["## Cooling (coasting window: Leave to the first heating slot)", ""]
    lines += ["| absence | coast h | outdoor mean | start | min | h to min | end | rooms min |"]
    lines += ["|---|---|---|---|---|---|---|---|"]
    for k, a in report["absences"].items():
        c = a["cooling"]
        rm = c["rooms"].get("room_mean", {})
        mins = ", ".join(f"{r} {v['min_c']}" for r, v in c["rooms"].items() if r != "room_mean")
        lines.append(
            f"| {k} | {c['coast_hours']} | {c['outdoor_mean_c']} | {rm.get('start_c')} | "
            f"{rm.get('min_c')} | {rm.get('hours_to_min')} | {rm.get('end_c')} | {mins} |"
        )
    eq = report["equilibrium"]
    lines += [
        "",
        f"Settled days (>= day 4): T_air = {eq.get('intercept_c')} + {eq.get('slope')} T_out "
        f"(RMSE {eq.get('rmse_k')} K); T_eq {eq.get('t_eq_at_c')}",
        "",
    ]
    lines += ["## Coasting error, pooled (bias / RMSE / error at lead, K)", ""]
    for variant, leads in report["pooled"].items():
        cells = "; ".join(
            f"{lead}: {v['bias_k']} / {v['rmse_k']} / {v['error_at_lead_k']} (n={v['cases']})"
            for lead, v in leads.items()
        )
        lines.append(f"- **{variant}**: {cells}")
    lines += ["", "## Empty-house fit", "", "```", json.dumps(report["empty_fit"], indent=1), "```"]
    lines += ["", "## Warm-up (measured, model with measured heat)", ""]
    for k, a in report["absences"].items():
        w = a.get("warmup") or {}
        if not w.get("heat_on_local"):
            continue
        rm = w["reach"]["room_mean"]
        mp = (w.get("model_prior") or {}).get("hours")
        lines.append(
            f"- {k}: heat on {w['heat_on_minus_return_h']} h from Return "
            f"at {w['t_air_at_on_c']} degC; "
            f"room mean 21 after {rm.get('hours')} h, {rm.get('heat_out_kwh')} kWh heat "
            f"({rm.get('heat_in_kwh')} kWh el), mean {rm.get('mean_heat_kw')} kW; model {mp} h"
        )
    lines += ["", "## Fastest recovery (model, top water level)", ""]
    for label, runs in report["recovery"]["runs"].items():
        for start, rows in runs.items():
            cells = "; ".join(
                f"{r['t_out_c']}C: 21 all {r['all_rooms_21_h']} h, "
                f"hall 24 {r['hall_kitchen_24.0']} h"
                for r in rows
            )
            lines.append(f"- {label} / {start}: {cells}")
    path.write_text("\n".join(lines) + "\n")


def empty_params(prior: Mapping[str, float], n_diurnal: int, **kw: float) -> dict[str, float]:
    p = dict(prior)
    for i in range(n_diurnal):
        p[f"g{i}"] = 0.0
    p.update(kw)
    return p


def fit_empty(cases, prior, n_diurnal, names=("q0",), max_h=168):
    """Refit *names* with ``D = 0`` on the open-loop coasting residuals of *cases*."""

    def resid(th):
        p = empty_params(prior, n_diurnal, **dict(zip(names, th, strict=True)))
        out = []
        for a, x0 in cases:
            n = min(a.t_coast_end - a.t_leave, max_h * SLOTS_H)
            pred = coast(p, n_diurnal, a, x0, a.t_leave, n)
            r = pred - a.d.t_air[a.t_leave : a.t_leave + n]
            out.append(r[np.isfinite(r)])
        return np.concatenate(out)

    x0 = [prior[n] for n in names]
    lo = [b2d.PARAM_BOX[n][0] for n in names]
    hi = [b2d.PARAM_BOX[n][1] for n in names]
    r = least_squares(resid, x0, bounds=(lo, hi), x_scale="jac")
    return {n: float(v) for n, v in zip(names, r.x, strict=True)}, float(np.sqrt(np.mean(r.fun**2)))


def predicted_warmup(model, params, n_diurnal, a: Absence, t_on: int, t_end: int):
    """The model from the observer at heat-on, driven by the measured heat: hours to 21 degC."""
    x0 = observe_at(model, a, t_on)
    if x0 is None:
        return None
    ad, bd = b2d.rc_matrices(dict(params), n_diurnal)
    w = np.nan_to_num(a.w[t_on:t_end].copy(), nan=0.0)
    w[:, 1] = a.outdoor[t_on:t_end]
    w[:, 0] = np.nan_to_num(a.d.q_heat_kw[t_on:t_end])
    pred = thermal.simulate(ad, w @ bd.T, x0)[1:, 0]
    hit = np.flatnonzero(pred >= FLOOR_C)
    return {
        "x0": [_r(v) for v in x0],
        "hours": _r(hit[0] / SLOTS_H, 2) if hit.size else None,
        "error_6h_k": _r(pred[23] - a.d.t_air[t_on + 23]) if pred.size > 23 else None,
        "error_24h_k": _r(pred[95] - a.d.t_air[t_on + 95]) if pred.size > 95 else None,
    }


def fastest_recovery(
    params, n_diurnal, law, x0, t_out: float, water_max: float, offsets: dict[str, float]
) -> dict[str, Any]:
    """Top water level every slot, valves open, constant outdoor, empty house (``D = 0``)."""
    ad, bd = b2d.rc_matrices(dict(params), n_diurnal)
    n = WARMUP_MAX_H * SLOTS_H
    x = np.array(x0, float)
    air = np.empty(n)
    q_sum = 0.0
    for t in range(n):
        q = float(law.power(t_out, water_max, x[1]))
        w = np.zeros(4 + n_diurnal)
        w[:4] = [q, t_out, 0.0, 1.0]
        x = ad @ x + bd @ w
        air[t] = x[0]
        if t < 48 * SLOTS_H:
            q_sum += q / SLOTS_H
    out: dict[str, Any] = {
        "t_out_c": t_out,
        "heat_kw_first_slot": _r(law.power(t_out, water_max, x0[1])),
    }
    goals = {"room_mean_21": (FLOOR_C, 0.0)}
    for r, off in offsets.items():
        goals[f"{r}_21"] = (FLOOR_C, off)
    for level in DAY_MIN_C:
        goals[f"hall_kitchen_{level}"] = (level, offsets.get("hall_kitchen", 0.0))
    for name, (level, off) in goals.items():
        hit = np.flatnonzero(air + off >= level)
        out[name] = _r(hit[0] / SLOTS_H, 2) if hit.size else None
    out["all_rooms_21_h"] = max(
        (v for k, v in out.items() if k.endswith("_21") and k != "room_mean_21" and v is not None),
        default=None,
    )
    out["t_air_after_48h"] = _r(air[48 * SLOTS_H - 1])
    out["heat_kwh_48h"] = _r(q_sum)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _r(v: Any, nd: int = 2) -> float | None:
    try:
        f = float(v)
    except TypeError, ValueError:
        return None
    return round(f, nd) if np.isfinite(f) else None


def _local(d: ClimateData, t: int) -> str:
    at = datetime.fromtimestamp(int(d.starts_us[t]) / 1e6, UTC).astimezone(d.tz)
    return at.strftime("%Y-%m-%d %H:%M")


def trajectories(rows: list[dict[str, Any]], a: Absence, preds: Mapping[str, np.ndarray]) -> None:
    d = a.d
    lo = a.t_leave - 12 * SLOTS_H
    hi = min(d.n, a.t_return + 72 * SLOTS_H)
    for t in range(lo, hi, SLOTS_H):
        row = {
            "absence": a.label,
            "local": _local(d, t),
            "h_since_leave": (t - a.t_leave) / SLOTS_H,
            "t_air": _r(np.nanmean(d.t_air[t : t + SLOTS_H])),
            "outdoor": _r(np.nanmean(a.outdoor[t : t + SLOTS_H])),
            "heat_kw": _r(np.nanmean(d.q_heat_kw[t : t + SLOTS_H])),
        }
        for r, y in d.room_temp.items():
            row[r] = (
                _r(np.nanmean(y[t : t + SLOTS_H]))
                if np.isfinite(y[t : t + SLOTS_H]).any()
                else None
            )
        for name, p in preds.items():
            k = t - a.t_leave
            row[f"pred_{name}"] = _r(p[k]) if 0 <= k < p.size else None
        rows.append(row)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--absences", required=True, help="private JSON: [[label, leave, return], ...]")
    ap.add_argument("--prior", required=True, help="climate shadow prior id")
    ap.add_argument("--prometheus-url", default=None)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    ap.add_argument("--config", default=str(_PROJECT / "config" / "climate.yaml"))
    ap.add_argument("--mpc-config", default=str(_PROJECT / "config" / "climate-mpc.yaml"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    root = resolve_data_dir(args.data_dir)
    ccfg = load_climate_config(args.config)
    cfg = load_mpc_config(args.mpc_config)
    site = load_site(root / SITE_FILE)
    sp = shadow.load_prior(root, args.prior)
    prior = sp.prior.params
    nd = sp.prior.n_diurnal
    base_url = extract_prom.resolve_base_url(args.prometheus_url)
    tz = ZoneInfo(cfg.timezone)
    spec = json.loads(Path(args.absences).read_text())

    absences: list[Absence] = []
    for label, leave, ret in spec:
        a = load_absence(label, leave, ret, tz, base_url, root, ccfg, site)
        a.w = observer_inputs(a, cfg.model.diurnal_harmonics)
        a.t_leave, a.t_return = slot_of(a.d, a.leave), slot_of(a.d, a.ret)
        on = np.flatnonzero(a.d.q_heat_kw[a.t_leave + GRACE_SLOTS : a.t_return] > HEAT_ON_KW)
        a.t_coast_end = a.t_leave + GRACE_SLOTS + int(on[0]) if on.size else a.t_return
        absences.append(a)

    model_prior = sp.prior.model()
    MODEL[:] = [model_prior]
    report: dict[str, Any] = {
        "prior_id": sp.prior_id,
        "prior_season": sp.prior.season,
        "prior_params": {k: _r(v, 4) for k, v in prior.items() if not k.startswith("g")},
        "prior_equilibrium_excess_k": _r(prior["q0"] / prior["ua"]),
        "absences": {},
    }
    cases = []
    states: dict[str, np.ndarray | None] = {}
    for a in absences:
        rec: dict[str, Any] = {
            "leave_local": _local(a.d, a.t_leave),
            "return_local": _local(a.d, a.t_return),
            "season": str(a.d.day_season[a.d.day_of_slot[a.t_leave]]),
            "coast_end_local": _local(a.d, a.t_coast_end),
            "coasting_case": a.t_coast_end - a.t_leave >= MIN_COAST_H * SLOTS_H,
        }
        rec["cooling"] = cooling_stats(a)
        x0 = observe_at(model_prior, a, a.t_leave)
        states[a.label] = x0
        rec["observer_x0"] = None if x0 is None else [_r(v) for v in x0]
        if x0 is not None and a.t_coast_end - a.t_leave >= MIN_COAST_H * SLOTS_H:
            cases.append((a, x0))
        # the service's weekly re-estimation on the 28 days before Leave
        today = a.leave.astimezone(tz).date()
        re = shadow.refit(sp, a.d, cfg, today, w=a.w)
        rec["reestimated"] = {
            "refitted": re["refitted"],
            "days": re["window"]["days"],
            "ua": _r(re["params"]["ua"], 4),
            "q0": _r(re["params"]["q0"], 4),
        }
        a_re = re["params"]
        rec["_params_re"] = a_re
        report["absences"][a.label] = rec

    report["equilibrium"] = equilibrium(
        [a for a in absences if a.t_coast_end - a.t_leave >= 4 * 24 * SLOTS_H]
    )
    # empty-house q0 (D = 0, physics fixed), all absences and leave-one-out
    q0_all, rmse_all = fit_empty(cases, prior, nd, ("q0",))
    qua_all, rmse_qua = fit_empty(cases, prior, nd, ("q0", "ua"))
    free_all, rmse_free = fit_empty(cases, prior, nd, ("q0", "ua", "c_a", "c_s", "k"))
    report["empty_fit"] = {
        "cases": [a.label for a, _x in cases],
        "free_d0": {k: _r(v, 4) for k, v in free_all.items()},
        "rmse_free_k": _r(rmse_free, 3),
        "equilibrium_excess_k_free_d0": _r(free_all["q0"] / free_all["ua"]),
        "q0_d0": _r(q0_all["q0"], 4),
        "rmse_k": _r(rmse_all, 3),
        "q0_ua_d0": {k: _r(v, 4) for k, v in qua_all.items()},
        "rmse_q0_ua_k": _r(rmse_qua, 3),
        "equilibrium_excess_k_q0_d0": _r(q0_all["q0"] / prior["ua"]),
        "equilibrium_excess_k_q0_ua_d0": _r(qua_all["q0"] / qua_all["ua"]),
        "loo_q0": {},
    }
    rows: list[dict[str, Any]] = []
    for a in absences:
        rec = report["absences"][a.label]
        x0 = states[a.label]
        a_re = rec.pop("_params_re")
        if x0 is None or a.t_coast_end - a.t_leave < MIN_COAST_H * SLOTS_H:
            continue
        n = a.t_coast_end - a.t_leave
        obs = a.d.t_air[a.t_leave : a.t_coast_end]
        others = [c for c in cases if c[0] is not a]
        q0_loo, _ = fit_empty(others, prior, nd, ("q0",)) if others else (q0_all, 0.0)
        report["empty_fit"]["loo_q0"][a.label] = _r(q0_loo["q0"], 4)
        preds = {
            "prior": coast(prior, nd, a, x0, a.t_leave, n),
            "reest": coast(a_re, nd, a, x0, a.t_leave, n),
            "prior_d0": coast(empty_params(prior, nd), nd, a, x0, a.t_leave, n),
            "empty_loo": coast(empty_params(prior, nd, **q0_loo), nd, a, x0, a.t_leave, n),
            "empty_all": coast(empty_params(prior, nd, **q0_all), nd, a, x0, a.t_leave, n),
            "empty_q0_ua": coast(empty_params(prior, nd, **qua_all), nd, a, x0, a.t_leave, n),
            "empty_free": coast(empty_params(prior, nd, **free_all), nd, a, x0, a.t_leave, n),
        }
        rec["coast"] = {k: errors(p, obs) for k, p in preds.items()}
        trajectories(rows, a, preds)

        # warm-up: measured, and the model driven by the measured heat
        nxt = [b for b in absences if b.leave > a.leave]
        t_end = a.d.n
        if nxt:
            t_end = min(t_end, slot_of(a.d, nxt[0].leave))
        wu = measured_warmup(a, t_end)
        t_on = wu.pop("t_on", None)
        if t_on is not None:
            wu["model_prior"] = predicted_warmup(model_prior, prior, nd, a, t_on, t_end)
            wu["model_reest"] = predicted_warmup(model_prior, a_re, nd, a, t_on, t_end)
        rec["warmup"] = wu

    # fastest recovery from the coldest observed state
    coldest = None
    for a in absences:
        seg = a.d.t_air[a.t_leave : a.t_coast_end]
        if not np.isfinite(seg).any():
            continue
        i = int(np.nanargmin(seg))
        if coldest is None or seg[i] < coldest[1]:
            coldest = (a, float(seg[i]), a.t_leave + i)
    assert coldest is not None
    a_c, t_min, t_at = coldest
    x_obs = observe_at(model_prior, a_c, t_at)
    offsets = {
        r: float(np.nanmedian([v for v in sp.room_offsets[r] if v is not None]))
        for r in sp.prior.rooms
    }
    water_max = float(cfg.machine.water_max_c)
    rec_runs = {}
    for label, params in {
        "empty_q0_d0": empty_params(prior, nd, **q0_all),
        "prior_d0": empty_params(prior, nd),
        "empty_no_gain": empty_params(prior, nd, q0=0.0),
        "empty_free_d0": empty_params(prior, nd, **free_all),
    }.items():
        rec_runs[label] = {}
        for start_name, x0 in {
            "equilibrium": np.array([t_min, t_min]),
            "observer": x_obs,
        }.items():
            if x0 is None:
                continue
            for cap_name, law in {
                "law": sp.prior.law,
                "cap6kw": replace(sp.prior.law, qm0=6.0),
            }.items():
                rec_runs[label][f"{start_name}_{cap_name}"] = [
                    fastest_recovery(params, nd, law, x0, t, water_max, offsets)
                    for t in WARMUP_T_OUT
                ]
    report["recovery"] = {
        "coldest": {"absence": a_c.label, "t_air_c": _r(t_min), "local": _local(a_c.d, t_at)},
        "observer_x0": None if x_obs is None else [_r(v) for v in x_obs],
        "water_c": water_max,
        "heat_law": sp.prior.law.to_dict(),
        "room_offsets_median_k": {k: _r(v) for k, v in offsets.items()},
        "curve_water_c": {
            str(t): _r(curve_water(np.array([t]), cfg.water.curve)[0]) for t in WARMUP_T_OUT
        },
        "runs": rec_runs,
    }

    out = root / OUT_DIR / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    report["pooled"] = pooled(report)
    write_markdown(report, out / "report.md")
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    if rows:
        keys = list(dict.fromkeys(k for r in rows for k in r))
        with (out / "trajectories.csv").open("w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=keys)
            wr.writeheader()
            wr.writerows(rows)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
