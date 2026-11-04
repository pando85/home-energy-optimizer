#!/usr/bin/env python3
"""Thermal charge-power soft-cap sweep on the realistic rolling-horizon policy.

The battery's internal temperature rises with charge power
(docs/planning/thermal-charge-soft-cap.md) and LFP ageing is worse hot, so the planner can price
the AC energy charged above a threshold power per slot (``charge_soft_cap`` of
``oracle.solve_oracle``: EUR per kWh above ``threshold_kw``).  This runner replays the unchanged
benchmark (the method of ``hack/high_soc_dwell_sweep.py``: ``policies=no_battery,rolling_horizon``,
one run per window and variant, written next to the sweep) for

* ``B``: the hard 20-95 % window with the recommended high-SOC dwell cost (0.01 EUR/kWh/h above
  90 %) and no soft cap, the reference;
* ``C<thr>@<cost>``: the same plus the soft cap at ``thr`` kW and ``cost`` EUR/kWh,

all with the deployed throughput cost, and tabulates relative figures only:

* cost against B, in percent of B's cost and in percent of **B's saving over no battery**;
* mean charge power while charging (kW);
* the share of the charged energy that is above 1.5 kW and above 2.0 kW (per slot), whatever each
  variant's own threshold, so every variant is read on the same scale;
* energy throughput against B.

``--full-charge`` adds the full-charge day study (the planner's checkpoint model on realised prices
and load, every variant against B) and reports whether the target is still reached.

Usage::

    uv run python hack/charge_soft_cap_sweep.py \\
        --dataset ~/data/<store>/analysis/v1/<dataset_id> --data-dir ~/data/<store> \\
        --run-id <fresh-name> [--thresholds 1.5,2.0] [--costs 0.005,0.01,0.02,0.05] \\
        [--workers 16] [--windows ...] [--replan-slots N] [--full-charge | --full-charge-only]

The benchmark runs are written under ``<data-dir>/analysis/v1/charge-soft-cap-sweep/<run-id>/`` with
``table.json`` / ``table.md``.  Only the relative tables are meant to be copied into the
repository; the run directories hold private euro figures.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow.parquet as pq

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "src"))

from battery_optimizer import benchmark  # noqa: E402
from battery_optimizer.config import load_config  # noqa: E402
from battery_optimizer.features import json_bytes  # noqa: E402
from battery_optimizer.paths import resolve_data_dir, write_part_atomic  # noqa: E402

DEFAULT_THRESHOLDS = "1.5,2.0"
DEFAULT_COSTS = "0.005,0.01,0.02,0.05"
DEFAULT_WINDOWS = (
    "W24:2024-02-01:2024-10-01,S2:2024-10-01:2025-05-01,SU25:2025-05-01:2025-10-01,"
    "S3:2025-10-01:2026-05-01,SU26:2026-05-01:2026-10-01"
)
POLICIES = "no_battery,rolling_horizon"
THROUGHPUT_COST = 0.01  # the deployed planning.throughput_cost_eur_per_kwh
DWELL = (0.90, 0.01)  # the recommended high-SOC dwell cost, on in every variant
SHARE_KW = (1.5, 2.0)  # the scales every variant's charged energy is read on
BAND_TOP = 0.95
TOL_KWH = 1e-6
CHARGING_KWH = 1e-6  # a slot with less than this charge is not charging


def _name(threshold: float, cost: float) -> str:
    return f"C{threshold:g}@{cost:g}"


@contextlib.contextmanager
def _dataset_directory(loaded: benchmark.LoadedDataset, directory: Path):
    """Write the run next to the sweep, not into the dataset directory."""
    original = benchmark.load_dataset
    patched = dataclasses.replace(loaded, directory=directory)
    benchmark.load_dataset = lambda *_a, **_k: patched
    try:
        yield
    finally:
        benchmark.load_dataset = original


def _bounds(spec: str, tz: ZoneInfo) -> tuple[str, datetime, datetime]:
    name, first, after = spec.split(":")
    start = datetime.combine(date.fromisoformat(first), time(0), tz).astimezone(UTC)
    end = datetime.combine(date.fromisoformat(after), time(0), tz).astimezone(UTC)
    return name, start, end


def charge_metrics(charge_kwh: np.ndarray, usable: np.ndarray, hours: float) -> dict:
    """Charge-power figures of one run (AC kWh per slot, ``hours`` per slot)."""
    ok = np.asarray(usable, dtype=bool)
    charge = np.where(ok, np.asarray(charge_kwh, dtype=float), 0.0)
    active = charge > CHARGING_KWH
    out = {
        "hours": float(ok.sum() * hours),
        "charge_kwh": float(charge.sum()),
        "charging_slots": int(active.sum()),
        "above_kwh": {
            f"{kw:g}": float(np.maximum(charge - kw * hours, 0.0).sum()) for kw in SHARE_KW
        },
        "peak_kw": float(charge.max() / hours) if charge.size else 0.0,
    }
    return out


def _run_one(task: dict) -> dict:
    root = Path(task["data_dir"])
    loaded = benchmark.load_dataset(task["dataset"], data_dir=root)
    out = Path(task["out_dir"])
    kwargs = {}
    if task["replan_slots"]:
        kwargs["rolling_horizon_replan_interval_slots"] = task["replan_slots"]
        kwargs["rolling_horizon_horizon_slots"] = task["horizon_slots"]
        kwargs["rolling_horizon_load_forecast_mode"] = "causal"
    cap = task["cap"]
    with _dataset_directory(loaded, out):
        run_dir = benchmark.benchmark_dataset(
            dataset=out,
            config_path=task["tariff"],
            train_end=datetime.fromisoformat(task["start"]),
            eval_end=datetime.fromisoformat(task["end"]),
            policies=POLICIES,
            time_limit_s=task["time_limit_s"],
            throughput_cost_eur_per_kwh=THROUGHPUT_COST,
            high_soc_cost=DWELL,
            charge_soft_cap=tuple(cap) if cap else None,
            data_dir=root,
            **kwargs,
        )
    rows = {
        r["policy"]: r for r in pq.read_table(run_dir / benchmark.RESULTS_PARQUET_NAME).to_pylist()
    }
    sched = pq.read_table(
        run_dir / benchmark.SCHEDULES_PARQUET_NAME, columns=["policy", "usable", "charge_kwh"]
    )
    is_rh = np.asarray(sched["policy"].to_pylist()) == "rolling_horizon"
    charge = sched["charge_kwh"].to_numpy(zero_copy_only=False)[is_rh]
    usable = sched["usable"].to_numpy(zero_copy_only=False)[is_rh]
    return {
        "window": task["window"],
        "variant": task["variant"],
        "run_dir": str(run_dir.relative_to(root)),
        "cost_no_battery": rows["no_battery"]["cost_eur"],
        "cost": rows["rolling_horizon"]["cost_eur"],
        "throughput_kwh": rows["rolling_horizon"]["throughput_kwh"],
        "charge": charge_metrics(charge, usable, task["hours"]),
    }


def _pool(rs: list[dict]) -> dict:
    """Sum the additive figures of one variant over windows (euros stay in memory only)."""
    acc = {
        "cost": 0.0,
        "cost_no_battery": 0.0,
        "throughput_kwh": 0.0,
        "hours": 0.0,
        "charge_kwh": 0.0,
        "charging_slots": 0,
        "above_kwh": {f"{kw:g}": 0.0 for kw in SHARE_KW},
        "peak_kw": 0.0,
    }
    for r in rs:
        for k in ("cost", "cost_no_battery", "throughput_kwh"):
            acc[k] += r[k]
        c = r["charge"]
        for k in ("hours", "charge_kwh", "charging_slots"):
            acc[k] += c[k]
        for kw in acc["above_kwh"]:
            acc["above_kwh"][kw] += c["above_kwh"][kw]
        acc["peak_kw"] = max(acc["peak_kw"], c["peak_kw"])
    return acc


def _figures(p: dict, ref: dict, slot_hours: float) -> dict:
    days = p["hours"] / 24.0
    saving_ref = ref["cost_no_battery"] - ref["cost"]
    charging_h = p["charging_slots"] * slot_hours
    return {
        "days": days,
        "cost_vs_B_pct": 100.0 * (p["cost"] - ref["cost"]) / ref["cost"],
        "cost_vs_B_pct_of_B_saving": 100.0 * (p["cost"] - ref["cost"]) / saving_ref,
        "mean_charge_kw_while_charging": p["charge_kwh"] / charging_h if charging_h else None,
        "charging_h_per_day": charging_h / days,
        "share_energy_above_kw_pct": {
            kw: (100.0 * v / p["charge_kwh"] if p["charge_kwh"] else None)
            for kw, v in p["above_kwh"].items()
        },
        "peak_charge_kw": p["peak_kw"],
        "throughput_vs_B_pct": 100.0 * (p["throughput_kwh"] / ref["throughput_kwh"] - 1.0),
    }


def tabulate(results: list[dict], variants: list[str], slot_hours: float) -> dict:
    windows = sorted({r["window"] for r in results})
    by = {(r["window"], r["variant"]): r for r in results}
    pooled = {v: _pool([by[(w, v)] for w in windows]) for v in variants}
    out: dict = {
        "pooled": {v: _figures(pooled[v], pooled["B"], slot_hours) for v in variants},
        "per_window": {},
    }
    for w in windows:
        per = {v: _pool([by[(w, v)]]) for v in variants}
        out["per_window"][w] = {v: _figures(per[v], per["B"], slot_hours) for v in variants}
    return out


def render(table: dict, variants: list[str], rule: float) -> str:
    def fmt(x, spec=".2f"):
        return "n/a" if x is None else format(x, spec)

    head = [
        "| variant | cost vs B | % of B's saving | mean kW while charging | energy above 1.5 kW "
        "| energy above 2.0 kW | peak kW | throughput vs B |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def rows(block):
        lines = []
        for v in variants:
            f = block[v]
            share = f["share_energy_above_kw_pct"]
            lines.append(
                f"| {v} | {f['cost_vs_B_pct']:+.2f}% | {f['cost_vs_B_pct_of_B_saving']:+.2f}% "
                f"| {fmt(f['mean_charge_kw_while_charging'])} | {fmt(share['1.5'], '.1f')}% "
                f"| {fmt(share['2'], '.1f')}% | {f['peak_charge_kw']:.2f} "
                f"| {f['throughput_vs_B_pct']:+.1f}% |"
            )
        return lines

    lines = [
        "# Thermal charge soft-cap sweep (relative figures)",
        "",
        f"Pooled over {table['pooled']['B']['days']:.0f} days. Selection rule: the largest cost "
        f"whose cost penalty against B is at most {rule:g}% of B's saving in every window "
        "(lowest passing threshold first).",
        "",
        *head,
        *rows(table["pooled"]),
        "",
    ]
    for w, block in table["per_window"].items():
        lines += [f"## {w} ({block['B']['days']:.0f} days)", "", *head, *rows(block), ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full-charge day
# ---------------------------------------------------------------------------


def _fc_one(task: dict) -> dict:
    """Solve one sampled day's checkpoint model for every variant, on realised data."""
    from battery_optimizer.battery import BatterySpec, Horizon
    from battery_optimizer.oracle import FullChargeModel, checkpoint_outcome, solve_oracle

    prices = np.asarray(task["prices"], dtype=float)
    load = np.asarray(task["load"], dtype=float)
    n = prices.size
    cap = task["capacity_kwh"]
    spec = BatterySpec(
        cap,
        task["power_kw"],
        task["eta"],
        task["eta"],
        task["soc_min"],
        1.0,
        task["soc_start"],
        None,
    )
    hz = Horizon(900, prices, load, np.ones(n, dtype=bool), None)
    out = {}
    w0 = task["window_start"]
    for variant, soft in task["variants"].items():
        fc = FullChargeModel(
            ceiling_kwh=tuple(cap * (BAND_TOP if t < w0 else 1.0) for t in range(n)),
            checkpoint_slots=tuple(range(w0, n)),
            target_kwh=cap,
            shortfall_penalty_eur_per_kwh=10.0,
            taper_start_soc_frac=BAND_TOP,
            taper_power_kw=0.5 * task["power_kw"],
            taper_first_slot=w0,
        )
        s = solve_oracle(
            spec,
            hz,
            time_limit_s=60.0,
            terminal_mode="free",
            terminal_value_eur_per_kwh=task["terminal_value"],
            floor_kwh=spec.soc_bound_kwh("min"),
            throughput_cost_eur_per_kwh=THROUGHPUT_COST,
            full_charge=fc,
            high_soc_cost=DWELL,
            charge_soft_cap=tuple(soft) if soft else None,
        )
        _slot, shortfall = checkpoint_outcome(s, fc)
        charge = np.asarray(s.charge_kwh)
        active = charge > CHARGING_KWH
        out[variant] = {
            "cost": float(s.cost_eur),
            "reached": shortfall <= 1e-6,
            "mean_kw_while_charging": float(charge[active].sum() / (active.sum() * 0.25))
            if active.any()
            else 0.0,
            "peak_kw": float(charge.max() / 0.25),
        }
    return {"day": task["day"], "cost_no_battery": float(np.dot(prices, load)), "variants": out}


def full_charge_study(args, loaded, cfg, variants: dict[str, tuple[float, float] | None]) -> dict:
    """Sampled full-charge days on realised prices and load (the dwell study's setup)."""
    table = loaded.table
    tz = ZoneInfo(cfg.tariff_periods.timezone)
    starts = table.column("interval_start").to_pylist()
    prices_all = np.asarray(table.column("price_eur_per_kwh").to_pylist(), dtype=float)
    load_all = np.asarray(table.column("gross_kwh").to_pylist(), dtype=float)
    index = {t: i for i, t in enumerate(starts)}
    battery = cfg.battery
    first, last = date.fromisoformat(args.fc_first), date.fromisoformat(args.fc_last)
    eta = float(battery.round_trip_efficiency) ** 0.5
    tasks = []
    day = first
    while day < last:
        t0 = datetime.combine(day - timedelta(days=1), time(20, 30), tz).astimezone(UTC)
        midnight = datetime.combine(day, time(0), tz).astimezone(UTC)
        day += timedelta(days=args.fc_stride)
        i0 = index.get(t0)
        if i0 is None or i0 + 192 > len(starts) or midnight not in index:
            continue
        pr, ld = prices_all[i0 : i0 + 192], load_all[i0 : i0 + 192]
        if not (np.isfinite(pr).all() and np.isfinite(ld).all()):
            continue
        tasks.append(
            {
                "day": midnight.date().isoformat(),
                "window_start": index[midnight] - i0,
                "prices": pr.tolist(),
                "load": ld.tolist(),
                "capacity_kwh": float(battery.capacity_kwh),
                "power_kw": float(battery.power_kw),
                "eta": eta,
                "soc_min": float(battery.soc_window_min),
                "soc_start": 0.5,
                "terminal_value": float(np.mean(pr)),
                "variants": variants,
            }
        )
    if not tasks:
        return {"days": 0, "variants": {}}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        res = list(pool.map(_fc_one, tasks))
    out: dict = {"days": len(res), "variants": {}}
    base = {r["day"]: r["variants"]["B"] for r in res}
    saving_b = sum(r["cost_no_battery"] - base[r["day"]]["cost"] for r in res)
    for v in variants:
        extra = sum(r["variants"][v]["cost"] - base[r["day"]]["cost"] for r in res)
        out["variants"][v] = {
            "reached_target_pct": 100.0
            * float(np.mean([r["variants"][v]["reached"] for r in res])),
            "mean_kw_while_charging": float(
                np.mean([r["variants"][v]["mean_kw_while_charging"] for r in res])
            ),
            "peak_kw_mean": float(np.mean([r["variants"][v]["peak_kw"] for r in res])),
            "cost_vs_B_pct_of_B_saving": 100.0 * extra / saving_b,
        }
    return out


def _run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or ".." in value:
        raise argparse.ArgumentTypeError(f"{value!r}: use only [A-Za-z0-9._-] and no '..'")
    return value


def recommend(
    table: dict, thresholds: list[float], costs: list[float], rule: float
) -> tuple[float, float] | None:
    """Lowest threshold that has a passing cost, and the largest passing cost at it."""
    for thr in sorted(thresholds):
        ok = [
            c
            for c in costs
            if all(
                block[_name(thr, c)]["cost_vs_B_pct_of_B_saving"] <= rule
                for block in table["per_window"].values()
            )
        ]
        if ok:
            return thr, max(ok)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--tariff", default="config/tariff.yaml")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--run-id", type=_run_id, required=True)
    ap.add_argument("--thresholds", default=DEFAULT_THRESHOLDS, help="kW, comma separated")
    ap.add_argument("--costs", default=DEFAULT_COSTS, help="EUR/kWh, comma separated")
    ap.add_argument("--windows", default=DEFAULT_WINDOWS)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--time-limit-s", type=float, default=600.0)
    ap.add_argument(
        "--replan-slots",
        type=int,
        default=0,
        help="replan every N slots on a causal forecast (0: the benchmark's legacy cadence)",
    )
    ap.add_argument("--horizon-slots", type=int, default=192)
    ap.add_argument("--rule-pct-of-saving", type=float, default=1.0)
    ap.add_argument("--full-charge", action="store_true")
    ap.add_argument("--full-charge-only", action="store_true", help="skip the benchmark sweep")
    ap.add_argument("--fc-first", default="2025-10-01")
    ap.add_argument("--fc-last", default="2026-05-01")
    ap.add_argument("--fc-stride", type=int, default=7)
    args = ap.parse_args(argv)

    root = resolve_data_dir(args.data_dir)
    out_dir = root / "analysis" / "v1" / "charge-soft-cap-sweep" / args.run_id
    if out_dir.exists():
        ap.error(f"{out_dir} exists: use a fresh --run-id")
    cfg = load_config(Path(args.tariff))
    tz = ZoneInfo(cfg.tariff_periods.timezone)
    thresholds = [float(c) for c in args.thresholds.split(",")]
    costs = [float(c) for c in args.costs.split(",")]
    windows = [_bounds(s, tz) for s in args.windows.split(",")]
    loaded = benchmark.load_dataset(args.dataset, data_dir=root)
    hours = loaded.interval_seconds / 3600.0
    variants: dict[str, tuple[float, float] | None] = {"B": None}
    for thr in thresholds:
        for cost in costs:
            variants[_name(thr, cost)] = (thr, cost)
    if args.full_charge_only:
        fc = full_charge_study(args, loaded, cfg, variants)
        out_dir.mkdir(parents=True, exist_ok=True)
        write_part_atomic(out_dir / "table.json", json_bytes({"full_charge": fc}))
        print(json.dumps(fc, indent=1))
        return 0
    tasks = [
        {
            "dataset": args.dataset,
            "data_dir": str(root),
            "tariff": args.tariff,
            "out_dir": str(out_dir / f"{n}-{v}"),
            "window": n,
            "variant": v,
            "cap": cap,
            "start": s.isoformat(),
            "end": e.isoformat(),
            "time_limit_s": args.time_limit_s,
            "replan_slots": args.replan_slots,
            "horizon_slots": args.horizon_slots,
            "hours": hours,
        }
        for n, s, e in windows
        for v, cap in variants.items()
    ]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(_run_one, tasks))
    names = list(variants)
    table = tabulate(results, names, hours)
    recommended = recommend(table, thresholds, costs, args.rule_pct_of_saving)
    report = {
        "dataset": args.dataset,
        "code_version": benchmark.code_version(),
        "dwell": {"threshold_frac": DWELL[0], "eur_per_kwh_h": DWELL[1]},
        "thresholds_kw": thresholds,
        "costs_eur_per_kwh": costs,
        "windows": args.windows,
        "cadence": f"causal replan every {args.replan_slots} slots"
        if args.replan_slots
        else "benchmark default (two events per day)",
        "rule": "lowest threshold with a cost whose penalty vs B is <= "
        f"{args.rule_pct_of_saving:g}% of B's saving in every window, then the largest such cost",
        "recommended_threshold_kw_and_eur_per_kwh": recommended,
        "runs": [
            {k: v for k, v in r.items() if k not in ("cost", "cost_no_battery")} for r in results
        ],
        "table": table,
    }
    if args.full_charge:
        report["full_charge"] = full_charge_study(args, loaded, cfg, variants)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_part_atomic(out_dir / "table.json", json_bytes(report))
    md = render(table, names, args.rule_pct_of_saving)
    md += f"\nRecommended (rule above): {recommended}\n"
    if report.get("full_charge"):
        md += "\n## Full-charge day\n\n" + json.dumps(report["full_charge"], indent=1) + "\n"
    write_part_atomic(out_dir / "table.md", md.encode("utf-8"))
    print(md)
    print(f"charge_soft_cap_sweep: written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
