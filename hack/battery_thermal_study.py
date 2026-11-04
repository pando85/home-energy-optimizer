#!/usr/bin/env python3
"""Thermal evidence study: how fast does the battery's internal temperature rise with charge power?

Reads the battery's internal temperature, AC power and SOC history **read-only** from the
long-term Prometheus through ``kubectl get --raw`` (GET only, an explicit ``--kube-context``, the
global kube context is never touched), fits a first-order thermal model

    dT/dt = a_c * P_charge + a_d * P_discharge - b * T + c        (T in degC, P in kW, t in min)

(equivalently ``dT/dt = a|P| - b (T - T_amb)`` with ``T_amb = c / b``, an ambient proxy that also
absorbs idle self-heating) on non-overlapping windows, reports the fit quality honestly, and
projects the steady and peak temperature for a few charge powers.  Only temperatures, SOC points and
powers are printed: no energy totals, costs, addresses or entity values.

The data are days old, so the script says so: it prints a verdict ("fit usable" or "too thin")
from the number of charge windows, the explained variance, a leave-one-block-out check and the
power range actually observed, and projections outside the observed power range are flagged.

Usage::

    uv run python hack/battery_thermal_study.py --kube-context <context> \\
        [--start 2026-11-03T00:00:00Z] [--end now] [--cache-dir DIR] [--out report.json]

``--cache-dir`` stores the raw responses (outside the repository) and re-reads them next time;
``--offline`` forbids the cluster call and needs the cache.  Metric names and the service path are
options because they are installation details.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

DEFAULT_SERVICE = "monitoring/prometheus-long-term:9090"
METRICS = {
    "temp": 'hass_sensor_temperature_celsius{entity="sensor.venus_internal_temperature"}',
    "power": 'hass_sensor_power_w{entity="sensor.venus_ac_power"}',  # charge is negative
    "soc": 'hass_sensor_battery_percent{entity="sensor.venus_battery_soc"}',
}
STEP_S = 30
WINDOW_STEPS = 10  # 5 min windows, non-overlapping
MAX_FFILL_STEPS = 6  # carry a value over at most 3 min
JUMP_C = 1.0  # a one-step temperature jump above this is a sensor discontinuity, not physics
CHARGE_KW = 0.2  # a session is a run above this charge power
SESSION_GAP_S = 300
MIN_SESSION_S = 600
CAPACITY_KWH = 5.12  # nominal, documented in docs/components/battery-model.md
ETA_CHARGE = 0.9  # sqrt of the 0.80 round trip used by the planner
PROJECT_KW = (1.0, 1.5, 2.0, 2.5)
SWING_POINTS = (20, 40, 70)  # SOC points of a charge, for the projected duration
MIN_CHARGE_WINDOWS = 8
MIN_R2 = 0.5
BLOCK_S = 6 * 3600  # blocks for the cross-check and the bootstrap
BOOTSTRAP = 2000


def _kubectl_get_raw(context: str, path: str) -> dict:
    """One GET through the API server proxy: the only call this script makes to the cluster."""
    proc = subprocess.run(
        ["kubectl", f"--context={context}", "get", "--raw", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"kubectl get --raw failed ({proc.returncode}): {proc.stderr.strip()[:300]}"
        )
    return json.loads(proc.stdout)


def fetch(args: argparse.Namespace, name: str, start: int, end: int) -> list[tuple[int, float]]:
    cache = None if args.cache_dir is None else Path(args.cache_dir) / f"{name}.json"
    if cache is not None and cache.exists():
        payload = json.loads(cache.read_text())
    elif args.offline:
        raise SystemExit(f"--offline: no cached {name} in {args.cache_dir}")
    else:
        ns, svc = args.service.split("/", 1)
        query = urllib.parse.urlencode(
            {
                "query": f"max by (entity) ({METRICS[name]})",
                "start": start,
                "end": end,
                "step": STEP_S,
            }
        )
        path = f"/api/v1/namespaces/{ns}/services/{svc}/proxy/api/v1/query_range?{query}"
        payload = _kubectl_get_raw(args.kube_context, path)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload))
    if payload.get("status") != "success" or not payload["data"]["result"]:
        raise SystemExit(f"no data for {name}: check the metric name with a series query")
    return [(int(t), float(v)) for t, v in payload["data"]["result"][0]["values"]]


def aligned(series: dict[str, list[tuple[int, float]]]) -> dict[str, np.ndarray]:
    """The three series on one 30 s grid, forward-filled over short gaps, NaN beyond."""
    t0 = min(s[0][0] for s in series.values())
    t1 = max(s[-1][0] for s in series.values())
    t0 -= t0 % STEP_S
    grid = np.arange(t0, t1 + 1, STEP_S)
    out = {"t": grid.astype(float)}
    for name, values in series.items():
        arr = np.full(grid.size, np.nan)
        for t, v in values:
            k = (t - t0) // STEP_S
            if 0 <= k < arr.size:
                arr[k] = v
        last, age = np.nan, MAX_FFILL_STEPS + 1
        for k in range(arr.size):
            if np.isnan(arr[k]):
                age += 1
                if age <= MAX_FFILL_STEPS:
                    arr[k] = last
            else:
                last, age = arr[k], 0
        out[name] = arr
    return out


def sessions(d: dict[str, np.ndarray]) -> list[dict]:
    """Charge and discharge sessions: runs of |P| above CHARGE_KW with short gaps merged."""
    out = []
    p_kw = -d["power"] / 1000.0  # positive = charging
    for kind, active in (("charge", p_kw > CHARGE_KW), ("discharge", p_kw < -CHARGE_KW)):
        idx = np.flatnonzero(active)
        if idx.size == 0:
            continue
        runs = np.split(idx, np.flatnonzero(np.diff(idx) * STEP_S > SESSION_GAP_S) + 1)
        for run in runs:
            a, b = int(run[0]), int(run[-1])
            if (b - a) * STEP_S < MIN_SESSION_S:
                continue
            temp, soc = d["temp"][a : b + 1], d["soc"][a : b + 1]
            after = d["temp"][b : b + 2 * 3600 // STEP_S]
            if np.isnan(temp).all():
                continue
            out.append(
                {
                    "kind": kind,
                    "start": datetime.fromtimestamp(d["t"][a], UTC).strftime("%m-%d %H:%MZ"),
                    "minutes": round((b - a) * STEP_S / 60.0),
                    "mean_kw": float(np.nanmean(np.abs(p_kw[a : b + 1]))),
                    "soc_from": float(soc[~np.isnan(soc)][0]),
                    "soc_to": float(soc[~np.isnan(soc)][-1]),
                    "temp_from": float(temp[~np.isnan(temp)][0]),
                    "temp_peak": float(np.nanmax(np.concatenate([temp, after]))),
                    "a": a,
                    "b": b,
                }
            )
    return sorted(out, key=lambda s: s["a"])


def windows(d: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Non-overlapping windows: dT/dt (degC/min), mean charge/discharge kW, mean T, block id."""
    t, temp, pw = d["t"], d["temp"], d["power"]
    rows = []
    for k in range(0, temp.size - WINDOW_STEPS, WINDOW_STEPS):
        seg_t = temp[k : k + WINDOW_STEPS + 1]
        seg_p = pw[k : k + WINDOW_STEPS + 1]
        if np.isnan(seg_t).any() or np.isnan(seg_p).any():
            continue
        if np.abs(np.diff(seg_t)).max() > JUMP_C:
            continue
        p_kw = -seg_p / 1000.0
        rows.append(
            (
                (seg_t[-1] - seg_t[0]) / (WINDOW_STEPS * STEP_S / 60.0),
                float(np.maximum(p_kw, 0.0).mean()),
                float(np.maximum(-p_kw, 0.0).mean()),
                float(seg_t.mean()),
                int((t[k] - t[0]) // BLOCK_S),
                float(t[k]),
            )
        )
    arr = np.asarray(rows, dtype=float)
    return {
        "y": arr[:, 0],
        "pc": arr[:, 1],
        "pd": arr[:, 2],
        "T": arr[:, 3],
        "block": arr[:, 4],
        "t0": arr[:, 5],
    }


def ols(x: np.ndarray, y: np.ndarray) -> dict:
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    dof = max(len(y) - x.shape[1], 1)
    sigma2 = float(resid @ resid) / dof
    cov = sigma2 * np.linalg.pinv(x.T @ x)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return {
        "beta": beta,
        "se": np.sqrt(np.diag(cov)),
        "r2": 1.0 - float(resid @ resid) / ss_tot if ss_tot else float("nan"),
        "rmse": math.sqrt(float(resid @ resid) / len(y)),
        "n": int(len(y)),
    }


def design(w: dict[str, np.ndarray], model: str) -> np.ndarray:
    one = np.ones_like(w["y"])
    if model == "pooled":  # a|P|
        return np.column_stack([w["pc"] + w["pd"], -w["T"], one])
    if model == "split":  # a_c P_charge + a_d P_discharge
        return np.column_stack([w["pc"], w["pd"], -w["T"], one])
    raise ValueError(model)


def cross_check(w: dict[str, np.ndarray], model: str) -> dict:
    """Leave-one-block-out: predict each block's dT/dt from a fit on the others."""
    x, y, blocks = design(w, model), w["y"], w["block"]
    pred = np.full_like(y, np.nan)
    for b in np.unique(blocks):
        te = blocks == b
        if (~te).sum() < x.shape[1] + 2:
            continue
        beta = ols(x[~te], y[~te])["beta"]
        pred[te] = x[te] @ beta
    ok = ~np.isnan(pred)
    if ok.sum() < 3:
        return {"r2": None, "rmse": None, "n": int(ok.sum())}
    base = float(((y[ok] - y[ok].mean()) ** 2).sum())
    err = float(((y[ok] - pred[ok]) ** 2).sum())
    return {"r2": 1.0 - err / base, "rmse": math.sqrt(err / ok.sum()), "n": int(ok.sum())}


def event_mask(w: dict[str, np.ndarray], d: dict[str, np.ndarray], sess: dict) -> np.ndarray:
    """Windows from 15 min before a session to 2 h after it: its heating phase and cool-down."""
    lo, hi = d["t"][sess["a"]] - 900.0, d["t"][sess["b"]] + 7200.0
    return (w["t0"] >= lo) & (w["t0"] <= hi)


def simulate(
    d, sess, a_c, a_d, b, c, *, after_steps: int = 3600 // STEP_S
) -> tuple[float, float] | None:
    """Free-simulation RMSE (degC) over one session plus ``after_steps`` after it.

    Returns ``(model rmse, persistence rmse)``: persistence holds the starting temperature, the
    baseline a model has to beat to be worth anything.
    """
    a, e = sess["a"], min(sess["b"] + after_steps, d["temp"].size - 1)
    temp = d["temp"][a : e + 1]
    pw = -d["power"][a : e + 1] / 1000.0
    if np.isnan(temp[0]):
        return None
    pw = np.nan_to_num(pw, nan=0.0)  # a missing power sample counts as idle
    sim = np.empty_like(temp)
    sim[0] = temp[0]
    dt = STEP_S / 60.0
    for k in range(1, temp.size):
        sim[k] = sim[k - 1] + dt * (
            a_c * max(pw[k - 1], 0.0) + a_d * max(-pw[k - 1], 0.0) - b * sim[k - 1] + c
        )
    seen = ~np.isnan(temp)
    return (
        float(np.sqrt(np.mean((sim[seen] - temp[seen]) ** 2))),
        float(np.sqrt(np.mean((temp[0] - temp[seen]) ** 2))),
    )


def project(beta: np.ndarray) -> dict:
    """Steady and session-peak temperature at a constant charge power, from an idle start."""
    a_c = beta[0]  # the charge coefficient is first in both designs
    b, c = beta[-2], beta[-1]  # design() carries -T and a constant last
    t_amb = c / b
    rows = {}
    for p in PROJECT_KW:
        steady = t_amb + a_c * p / b
        peaks = {}
        for swing in SWING_POINTS:
            minutes = swing / 100.0 * CAPACITY_KWH / (p * ETA_CHARGE) * 60.0
            peaks[str(swing)] = {
                "minutes": minutes,
                "peak": t_amb + (steady - t_amb) * (1.0 - math.exp(-b * minutes)),
            }
        rows[f"{p:g}"] = {"steady": steady, "by_swing": peaks}
    return {"t_amb": t_amb, "tau_min": 1.0 / b if b > 0 else None, "a_c": a_c, "by_kw": rows}


def bootstrap_projection(w: dict[str, np.ndarray], model: str, seed: int = 0) -> dict:
    """Block bootstrap of the projected steady rise above T_amb and the 70-point peak."""
    rng = np.random.default_rng(seed)
    blocks = np.unique(w["block"])
    x_all, y_all = design(w, model), w["y"]
    draws = {f"{p:g}": [] for p in PROJECT_KW}
    for _ in range(BOOTSTRAP):
        pick = rng.choice(blocks, size=blocks.size, replace=True)
        idx = np.concatenate([np.flatnonzero(w["block"] == b) for b in pick])
        if (w["pc"][idx] > CHARGE_KW).sum() < 3:
            continue
        beta = ols(x_all[idx], y_all[idx])["beta"]
        if beta[-2] <= 1e-4:  # no cooling identified in this draw
            continue
        proj = project(beta)
        for p in PROJECT_KW:
            row = proj["by_kw"][f"{p:g}"]
            draws[f"{p:g}"].append((row["steady"] - proj["t_amb"], row["by_swing"]["70"]["peak"]))
    out = {}
    for key, vals in draws.items():
        if len(vals) < 50:
            out[key] = None
            continue
        arr = np.asarray(vals)
        out[key] = {
            "draws": len(vals),
            "steady_rise_p10_p90": [float(np.percentile(arr[:, 0], q)) for q in (10, 90)],
            "peak70_p10_p90": [float(np.percentile(arr[:, 1], q)) for q in (10, 90)],
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--kube-context", help="kubectl context; required unless --offline")
    ap.add_argument("--service", default=DEFAULT_SERVICE, help="namespace/service:port")
    ap.add_argument("--start", default="2026-11-03T00:00:00Z")
    ap.add_argument("--end", default="now")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default=None, help="write the JSON report here")
    args = ap.parse_args(argv)
    if not args.offline and not args.kube_context:
        ap.error("--kube-context is required (the global context is never used)")

    start = int(datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp())
    end = (
        int(time.time())
        if args.end == "now"
        else int(datetime.fromisoformat(args.end.replace("Z", "+00:00")).timestamp())
    )
    series = {name: fetch(args, name, start, end) for name in METRICS}
    d = aligned(series)
    sess = sessions(d)
    w = windows(d)
    n_charge = int((w["pc"] > CHARGE_KW).sum())
    n_dis = int((w["pd"] > CHARGE_KW).sum())

    report: dict = {
        "span_hours": float((d["t"][-1] - d["t"][0]) / 3600.0),
        "windows": {"total": int(w["y"].size), "charging": n_charge, "discharging": n_dis},
        "charge_power_observed_kw": [
            float(w["pc"][w["pc"] > CHARGE_KW].min()) if n_charge else None,
            float(w["pc"].max()),
        ],
        "sessions": [{k: v for k, v in s.items() if k not in ("a", "b")} for s in sess],
        "models": {},
    }
    for model in ("pooled", "split"):
        fit = ols(design(w, model), w["y"])
        names = {
            "pooled": ["a", "b", "c"],
            "split": ["a_charge", "a_discharge", "b", "c"],
        }[model]
        cv = cross_check(w, model)
        sims = [
            simulate(d, s, *_sim_params(fit["beta"], model)) for s in sess if s["kind"] == "charge"
        ]
        report["models"][model] = {
            "coef": {n: [float(b), float(se)] for n, b, se in zip(names, fit["beta"], fit["se"])},
            "r2": fit["r2"],
            "rmse_degC_per_min": fit["rmse"],
            "n": fit["n"],
            "cv_leave_block_out": cv,
            "free_simulation_rmse_model_vs_persistence_charge_sessions": sims,
            "projection": project(fit["beta"]),
            "bootstrap": bootstrap_projection(w, model),
        }
    charge_sessions = [s for s in sess if s["kind"] == "charge"]
    if charge_sessions:
        main_s = max(charge_sessions, key=lambda s: s["minutes"] * s["mean_kw"])
        mask = event_mask(w, d, main_s)
        sub = {k: v[mask] for k, v in w.items()}
        fit = ols(design(sub, "pooled"), sub["y"])
        params = _sim_params(fit["beta"], "pooled")
        others = [s for s in sess if s is not main_s]
        report["event_fit"] = {
            "session": {k: v for k, v in main_s.items() if k not in ("a", "b")},
            "coef": {
                n: [float(b), float(se)]
                for n, b, se in zip(["a", "b", "c"], fit["beta"], fit["se"])
            },
            "r2": fit["r2"],
            "n": fit["n"],
            "projection": project(fit["beta"]),
            "bootstrap_note": "not computed: one session",
            "out_of_session_simulation_rmse_model_vs_persistence": {
                f"{s['kind']} {s['start']} {s['mean_kw']:.2f} kW": simulate(d, s, *params)
                for s in others
            },
        }
    split = report["models"]["split"]
    cv_r2 = split["cv_leave_block_out"]["r2"]
    reasons = []
    if n_charge < MIN_CHARGE_WINDOWS:
        reasons.append(f"only {n_charge} charging windows (< {MIN_CHARGE_WINDOWS})")
    if split["r2"] < MIN_R2:
        reasons.append(f"R2 {split['r2']:.2f} < {MIN_R2}")
    if cv_r2 is None or cv_r2 < 0.0:
        reasons.append("the leave-block-out check does not beat a constant")
    hi = report["charge_power_observed_kw"][1]
    report["verdict"] = {
        "usable": not reasons,
        "reasons": reasons,
        "extrapolates_above_kw": hi,
        "note": "projections at powers above the observed maximum are extrapolations",
    }

    text = json.dumps(report, indent=2, default=float)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print_report(report)
    return 0


def _sim_params(beta: np.ndarray, model: str) -> tuple[float, float, float, float]:
    if model == "pooled":
        return beta[0], beta[0], beta[1], beta[2]
    return beta[0], beta[1], beta[2], beta[3]


def _coef_line(coef: dict) -> str:
    return ", ".join(f"{k} {v[0]:.4f} ({v[1]:.4f})" for k, v in coef.items())


def _projection_table(pr: dict, bootstrap: dict | None) -> list[str]:
    cols = " | ".join(f"peak after {x}-pt charge (min)" for x in SWING_POINTS)
    out = [
        f"  | kW | steady degC | {cols} |"
        + (" bootstrap 70-pt peak p10-p90 |" if bootstrap else "")
    ]
    for kw, row in pr["by_kw"].items():
        cells = " | ".join(
            f"{row['by_swing'][str(x)]['peak']:.1f} ({row['by_swing'][str(x)]['minutes']:.0f})"
            for x in SWING_POINTS
        )
        line = f"  | {kw} | {row['steady']:.1f} | {cells} |"
        if bootstrap:
            bs = bootstrap.get(kw)
            span = "n/a" if bs is None else "{:.1f} to {:.1f}".format(*bs["peak70_p10_p90"])
            line += f" {span} |"
        out.append(line)
    return out


def print_report(r: dict) -> None:
    p = print
    p(f"span {r['span_hours']:.1f} h; windows {r['windows']}")
    lo, hi = r["charge_power_observed_kw"]
    p(f"charge power observed in windows: {lo} to {hi:.2f} kW (mean over 5 min)")
    p("\nsessions")
    p("| kind | start (UTC) | min | mean kW | SOC % | T start | T peak (+2 h) |")
    p("|---|---|---|---|---|---|---|")
    for s in r["sessions"]:
        p(
            f"| {s['kind']} | {s['start']} | {s['minutes']} | {s['mean_kw']:.2f} "
            f"| {s['soc_from']:.0f} to {s['soc_to']:.0f} | {s['temp_from']:.1f} "
            f"| {s['temp_peak']:.1f} |"
        )
    for name, m in r["models"].items():
        rmse = m["rmse_degC_per_min"]
        p(f"\nmodel {name}: R2 {m['r2']:.2f}, rmse {rmse:.3f} degC/min, n {m['n']}")
        p("  coef (value, se): " + _coef_line(m["coef"]))
        cv = m["cv_leave_block_out"]
        p(f"  leave-block-out R2 {cv['r2']}, rmse {cv['rmse']}, n {cv['n']}")
        p(
            "  free simulation (model, persistence) rmse per charge session, degC: "
            f"{m['free_simulation_rmse_model_vs_persistence_charge_sessions']}"
        )
        pr = m["projection"]
        p(f"  T_amb proxy {pr['t_amb']:.1f} degC, tau {pr['tau_min']:.0f} min")
        for line in _projection_table(pr, m["bootstrap"]):
            p(line)
    ev = r.get("event_fit")
    if ev:
        s = ev["session"]
        p(
            f"\nevent fit on the {s['start']} charge session and its cool-down: "
            f"R2 {ev['r2']:.2f}, n {ev['n']}; coef {_coef_line(ev['coef'])}"
        )
        pr = ev["projection"]
        p(f"  T_amb proxy {pr['t_amb']:.1f} degC, tau {pr['tau_min']:.0f} min")
        for line in _projection_table(pr, None):
            p(line)
        p("  other sessions, free simulation (model, persistence) rmse, degC:")
        for key, val in ev["out_of_session_simulation_rmse_model_vs_persistence"].items():
            p(f"    {key}: {val}")
    v = r["verdict"]
    p(f"\nverdict: {'fit usable' if v['usable'] else 'too thin'}; {v['reasons']}")
    p(f"{v['note']} (observed up to {v['extrapolates_above_kw']:.2f} kW)")


if __name__ == "__main__":
    sys.exit(main())
