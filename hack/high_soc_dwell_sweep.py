#!/usr/bin/env python3
"""High-SOC dwell-cost sweep on the realistic rolling-horizon policy.

LFP calendar aging is worse at high SOC, so the planner can price the *time* spent above a
threshold SOC (``high_soc_cost`` of ``oracle.solve_oracle``: EUR per kWh above the threshold per
hour held).  This runner replays the unchanged benchmark (``hack/throughput_sweep.py`` method:
``policies=no_battery,rolling_horizon``, one run per window and variant, written next to the
sweep) for

* ``A``: a hard 20-90 % window, no dwell cost;
* ``B``: the hard 20-95 % window the service has today, no dwell cost (the reference);
* ``C<v>``: the 20-95 % window plus the dwell cost above ``--threshold`` at ``v`` EUR/kWh/h,

all with the deployed throughput cost, and tabulates relative figures only:

* cost against B, in percent of B's cost and in percent of B's saving over no battery;
* mean hours per day strictly above 90 %, at or above 90 % (a plan parked exactly at a hard
  90 % ceiling counts there but not above it) and at or above 95 % SOC (the ceiling of B);
* energy throughput against B;
* the share of the energy in the 90-95 % band that is discharged within N hours of reaching it.
  The band is cut into thin SOC levels; for each level and each uninterrupted run of slots
  above it, the run length is the time that slice of energy sat there.  The share is the
  fraction of those (level, run) slices whose run is at most N hours.

``--full-charge`` adds the full-charge day study: for sampled days it solves the planner's
checkpoint model on the day's realised prices and load (a perfect forecast, so only the dwell
price differs between variants) and reports the hours at or above 95 % and the hours from first
reaching 100 % until the plan is back at or below the threshold.

Usage::

    uv run python hack/high_soc_dwell_sweep.py \\
        --dataset ~/data/<store>/analysis/v1/<dataset_id> --data-dir ~/data/<store> \\
        --run-id <fresh-name> [--costs 0.002,0.005,0.01,0.02,0.05] [--workers 16] \\
        [--replan-slots N] [--full-charge | --full-charge-only]

The benchmark runs are written under ``<data-dir>/analysis/v1/high-soc-sweep/<run-id>/`` with
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

DEFAULT_COSTS = "0.002,0.005,0.01,0.02,0.05"
DEFAULT_WINDOWS = (
    "W24:2024-02-01:2024-10-01,S2:2024-10-01:2025-05-01,SU25:2025-05-01:2025-10-01,"
    "S3:2025-10-01:2026-05-01,SU26:2026-05-01:2026-10-01"
)
POLICIES = "no_battery,rolling_horizon"
THROUGHPUT_COST = 0.01  # the deployed planning.throughput_cost_eur_per_kwh
THRESHOLD = 0.90
BAND_TOP = 0.95
LEVELS = 50  # SOC levels cutting the 90-95 % band
WITHIN_HOURS = (2.0, 6.0)
TOL_KWH = 1e-6


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


@contextlib.contextmanager
def _soc_max(frac: float | None):
    """Override the SOC window ceiling the benchmark resolves from ``tariff.yaml``.

    The tariff is pinned by the dataset's config hash, so the ceiling is changed after it has
    been read.  Each variant runs in its own directory, so the run ids cannot collide.
    """
    original = benchmark.resolve_battery_assumptions
    if frac is None:
        yield
        return

    def patched(*args, **kwargs):
        return dataclasses.replace(original(*args, **kwargs), soc_max_frac=frac)

    benchmark.resolve_battery_assumptions = patched
    try:
        yield
    finally:
        benchmark.resolve_battery_assumptions = original


def _bounds(spec: str, tz: ZoneInfo) -> tuple[str, datetime, datetime]:
    name, first, after = spec.split(":")
    start = datetime.combine(date.fromisoformat(first), time(0), tz).astimezone(UTC)
    end = datetime.combine(date.fromisoformat(after), time(0), tz).astimezone(UTC)
    return name, start, end


def soc_metrics(soc_kwh: np.ndarray, usable: np.ndarray, capacity_kwh: float, hours: float) -> dict:
    """SOC-trace figures of one run: hours above the thresholds and the band run lengths."""
    soc = np.asarray(soc_kwh, dtype=float)
    ok = np.asarray(usable, dtype=bool)
    out = {
        "hours": float(ok.sum() * hours),
        "h_above_90": float(((soc > THRESHOLD * capacity_kwh + TOL_KWH) & ok).sum() * hours),
        "h_at_or_above_90": float(((soc >= THRESHOLD * capacity_kwh - TOL_KWH) & ok).sum() * hours),
        "h_above_95": float(((soc >= BAND_TOP * capacity_kwh - TOL_KWH) & ok).sum() * hours),
        "h_at_100": float(((soc >= capacity_kwh - TOL_KWH) & ok).sum() * hours),
    }
    runs: list[float] = []
    levels = (
        THRESHOLD + (np.arange(LEVELS) + 0.5) / LEVELS * (BAND_TOP - THRESHOLD)
    ) * capacity_kwh
    for level in levels:
        above = np.concatenate(([False], soc > level, [False]))
        edges = np.flatnonzero(np.diff(above.astype(np.int8)))
        runs.extend(((edges[1::2] - edges[0::2]) * hours).tolist())
    arr = np.asarray(runs, dtype=float)
    out["band_runs"] = int(arr.size)
    out["band_runs_within"] = {str(n): int((arr <= n + 1e-9).sum()) for n in WITHIN_HOURS}
    out["band_run_hours_sum"] = float(arr.sum())
    return out


def _run_one(task: dict) -> dict:
    root = Path(task["data_dir"])
    loaded = benchmark.load_dataset(task["dataset"], data_dir=root)
    out = Path(task["out_dir"])
    cost = task["dwell_cost"]
    kwargs = {}
    if task["replan_slots"]:
        kwargs["rolling_horizon_replan_interval_slots"] = task["replan_slots"]
        kwargs["rolling_horizon_horizon_slots"] = task["horizon_slots"]
        kwargs["rolling_horizon_load_forecast_mode"] = "causal"
    with _dataset_directory(loaded, out), _soc_max(task["soc_max"]):
        run_dir = benchmark.benchmark_dataset(
            dataset=out,
            config_path=task["tariff"],
            train_end=datetime.fromisoformat(task["start"]),
            eval_end=datetime.fromisoformat(task["end"]),
            policies=POLICIES,
            time_limit_s=task["time_limit_s"],
            throughput_cost_eur_per_kwh=THROUGHPUT_COST,
            high_soc_cost=(THRESHOLD, cost) if cost else None,
            data_dir=root,
            **kwargs,
        )
    rows = {
        r["policy"]: r for r in pq.read_table(run_dir / benchmark.RESULTS_PARQUET_NAME).to_pylist()
    }
    sched = pq.read_table(run_dir / "schedules.parquet", columns=["policy", "usable", "soc_kwh"])
    is_rh = np.asarray(sched["policy"].to_pylist()) == "rolling_horizon"
    soc = sched["soc_kwh"].to_numpy(zero_copy_only=False)[is_rh]
    usable = sched["usable"].to_numpy(zero_copy_only=False)[is_rh]
    return {
        "window": task["window"],
        "variant": task["variant"],
        "run_dir": str(run_dir.relative_to(root)),
        "cost_no_battery": rows["no_battery"]["cost_eur"],
        "cost": rows["rolling_horizon"]["cost_eur"],
        "throughput_kwh": rows["rolling_horizon"]["throughput_kwh"],
        "efc": rows["rolling_horizon"]["efc"],
        "soc": soc_metrics(soc, usable, task["capacity_kwh"], task["hours"]),
    }


def _pool(rs: list[dict]) -> dict:
    """Sum the additive figures of one variant over windows (euros stay in memory only)."""
    acc = {
        "cost": 0.0,
        "cost_no_battery": 0.0,
        "throughput_kwh": 0.0,
        "hours": 0.0,
        "h_above_90": 0.0,
        "h_at_or_above_90": 0.0,
        "h_above_95": 0.0,
        "band_runs": 0,
        "band_within": {str(n): 0 for n in WITHIN_HOURS},
        "band_hours": 0.0,
    }
    for r in rs:
        acc["cost"] += r["cost"]
        acc["cost_no_battery"] += r["cost_no_battery"]
        acc["throughput_kwh"] += r["throughput_kwh"]
        for k in ("hours", "h_above_90", "h_at_or_above_90", "h_above_95"):
            acc[k] += r["soc"][k]
        acc["band_runs"] += r["soc"]["band_runs"]
        acc["band_hours"] += r["soc"]["band_run_hours_sum"]
        for n in acc["band_within"]:
            acc["band_within"][n] += r["soc"]["band_runs_within"][n]
    return acc


def _figures(p: dict, ref: dict) -> dict:
    days = p["hours"] / 24.0
    saving_ref = ref["cost_no_battery"] - ref["cost"]
    runs = p["band_runs"]
    return {
        "days": days,
        "cost_vs_B_pct": 100.0 * (p["cost"] - ref["cost"]) / ref["cost"],
        "cost_vs_B_pct_of_B_saving": 100.0 * (p["cost"] - ref["cost"]) / saving_ref,
        "saving_rel_to_no_battery_pct": 100.0
        * (p["cost_no_battery"] - p["cost"])
        / p["cost_no_battery"],
        "h_per_day_above_90": p["h_above_90"] / days,
        "h_per_day_at_or_above_90": p["h_at_or_above_90"] / days,
        "h_per_day_above_95": p["h_above_95"] / days,
        "throughput_vs_B_pct": 100.0 * (p["throughput_kwh"] / ref["throughput_kwh"] - 1.0),
        "band_share_within": {
            n: (None if not runs else 100.0 * c / runs) for n, c in p["band_within"].items()
        },
        "band_mean_run_h": None if not runs else p["band_hours"] / runs,
    }


def tabulate(results: list[dict], variants: list[str]) -> dict:
    windows = sorted({r["window"] for r in results})
    out: dict = {"pooled": {}, "per_window": {}}
    by = {(r["window"], r["variant"]): r for r in results}
    pooled = {v: _pool([by[(w, v)] for w in windows]) for v in variants}
    out["pooled"] = {v: _figures(pooled[v], pooled["B"]) for v in variants}
    for w in windows:
        per = {v: _pool([by[(w, v)]]) for v in variants}
        out["per_window"][w] = {v: _figures(per[v], per["B"]) for v in variants}
    return out


def render(table: dict, variants: list[str], rule: float) -> str:
    def fmt(x, spec=".2f"):
        return "n/a" if x is None else format(x, spec)

    head = [
        "| variant | cost vs B | % of B's saving | h/day >90% | h/day at/above 90% "
        "| h/day at/above 95% | throughput vs B | share <=2 h | share <=6 h |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    def rows(block):
        lines = []
        for v in variants:
            f = block[v]
            lines.append(
                f"| {v} | {f['cost_vs_B_pct']:+.2f}% | {f['cost_vs_B_pct_of_B_saving']:+.2f}% "
                f"| {f['h_per_day_above_90']:.2f} | {f['h_per_day_at_or_above_90']:.2f} "
                f"| {f['h_per_day_above_95']:.2f} | {f['throughput_vs_B_pct']:+.1f}% "
                f"| {fmt(f['band_share_within']['2.0'], '.0f')}% "
                f"| {fmt(f['band_share_within']['6.0'], '.0f')}% |"
            )
        return lines

    lines = [
        "# High-SOC dwell-cost sweep (relative figures)",
        "",
        f"Pooled over {table['pooled']['B']['days']:.0f} days. Selection rule: the largest cost "
        f"whose cost penalty against B is at most {rule:g}% of B's saving in every window.",
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
    """Solve one sampled day's checkpoint model for every dwell cost, on realised data."""
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
    for variant, cost in task["variants"].items():
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
            high_soc_cost=(THRESHOLD, cost) if cost else None,
        )
        slot, shortfall = checkpoint_outcome(s, fc)
        soc = np.asarray(s.soc_kwh)
        reached = None if shortfall > 1e-6 else int(slot)
        # Hours from the slot that first reaches 100 % until the plan is back at the threshold.
        hold = None
        if reached is not None:
            below = np.flatnonzero(soc[reached:] <= THRESHOLD * cap + 1e-6)
            hold = float((below[0] if below.size else n - reached) * 0.25)
        out[variant] = {
            "cost": float(s.cost_eur),
            "reached": reached is not None,
            "h_at_100": float((soc >= cap - 1e-6).sum() * 0.25),
            "h_above_90": float((soc > THRESHOLD * cap + 1e-6).sum() * 0.25),
            "hold_from_full_to_90_h": hold,
        }
    return {
        "day": task["day"],
        "cost_no_battery": float(np.dot(prices, load)),
        "variants": out,
    }


def full_charge_study(args, loaded, cfg, costs: list[float]) -> dict | None:
    """Sampled full-charge days on realised prices and load.

    Each day is solved from 20:30 the evening before (192 slots), the ceiling is 0.95 until local
    midnight and 1.0 from then on, the checkpoint may fall in any slot from midnight (the
    planner's commit model: shortfall penalty 10, taper from 95 %), the plan starts at 50 %.
    """
    table = loaded.table
    tz = ZoneInfo(cfg.tariff_periods.timezone)
    starts = table.column("interval_start").to_pylist()
    prices_all = np.asarray(table.column("price_eur_per_kwh").to_pylist(), dtype=float)
    load_all = np.asarray(table.column("gross_kwh").to_pylist(), dtype=float)
    index = {t: i for i, t in enumerate(starts)}
    battery = cfg.battery
    first, last = date.fromisoformat(args.fc_first), date.fromisoformat(args.fc_last)
    eta = float(battery.round_trip_efficiency) ** 0.5
    variants = {"B": 0.0, **{f"C{c:g}": c for c in costs}}
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
        return None
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        res = list(pool.map(_fc_one, tasks))
    out: dict = {"days": len(res), "variants": {}}
    base = {r["day"]: r["variants"]["B"] for r in res}
    saving_b = sum(r["cost_no_battery"] - base[r["day"]]["cost"] for r in res)
    for v in variants:
        holds = [
            r["variants"][v]["hold_from_full_to_90_h"]
            for r in res
            if r["variants"][v]["hold_from_full_to_90_h"] is not None
        ]
        extra = sum(r["variants"][v]["cost"] - base[r["day"]]["cost"] for r in res)
        out["variants"][v] = {
            "reached_target_pct": 100.0
            * float(np.mean([r["variants"][v]["reached"] for r in res])),
            "h_at_100_mean": float(np.mean([r["variants"][v]["h_at_100"] for r in res])),
            "h_above_90_mean": float(np.mean([r["variants"][v]["h_above_90"] for r in res])),
            "hold_from_full_to_90_h_mean": float(np.mean(holds)) if holds else None,
            "cost_vs_B_pct_of_B_saving": 100.0 * extra / saving_b,
        }
    return out


def _run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or ".." in value:
        raise argparse.ArgumentTypeError(f"{value!r}: use only [A-Za-z0-9._-] and no '..'")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--tariff", default="config/tariff.yaml")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--run-id", type=_run_id, required=True)
    ap.add_argument("--costs", default=DEFAULT_COSTS)
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
    out_dir = root / "analysis" / "v1" / "high-soc-sweep" / args.run_id
    if out_dir.exists():
        ap.error(f"{out_dir} exists: use a fresh --run-id")
    cfg = load_config(Path(args.tariff))
    tz = ZoneInfo(cfg.tariff_periods.timezone)
    costs = [float(c) for c in args.costs.split(",")]
    windows = [_bounds(s, tz) for s in args.windows.split(",")]
    cap = float(cfg.battery.capacity_kwh)
    loaded = benchmark.load_dataset(args.dataset, data_dir=root)
    hours = loaded.interval_seconds / 3600.0
    soc_max_today = float(cfg.battery.soc_window_max)
    if args.full_charge_only:
        fc = full_charge_study(args, loaded, cfg, costs)
        out_dir.mkdir(parents=True, exist_ok=True)
        write_part_atomic(out_dir / "table.json", json_bytes({"full_charge": fc}))
        print(json.dumps(fc, indent=1))
        return 0
    variants: dict[str, tuple[float | None, float]] = {
        "A": (0.90, 0.0),
        "B": (None, 0.0),
        **{f"C{c:g}": (None, c) for c in costs},
    }
    tasks = [
        {
            "dataset": args.dataset,
            "data_dir": str(root),
            "tariff": args.tariff,
            "out_dir": str(out_dir / f"{n}-{v}"),
            "window": n,
            "variant": v,
            "soc_max": soc_max,
            "dwell_cost": dwell,
            "start": s.isoformat(),
            "end": e.isoformat(),
            "time_limit_s": args.time_limit_s,
            "replan_slots": args.replan_slots,
            "horizon_slots": args.horizon_slots,
            "capacity_kwh": cap,
            "hours": hours,
        }
        for n, s, e in windows
        for v, (soc_max, dwell) in variants.items()
    ]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(_run_one, tasks))
    names = list(variants)
    table = tabulate(results, names)
    # The largest swept cost whose penalty stays within the rule in every window (so pooled too).
    ok = [
        c
        for c in costs
        if all(
            block[f"C{c:g}"]["cost_vs_B_pct_of_B_saving"] <= args.rule_pct_of_saving
            for block in table["per_window"].values()
        )
    ]
    recommended = max(ok) if ok else None
    report = {
        "dataset": args.dataset,
        "code_version": benchmark.code_version(),
        "soc_window_today_max": soc_max_today,
        "threshold_frac": THRESHOLD,
        "costs_eur_per_kwh_h": costs,
        "windows": args.windows,
        "cadence": f"causal replan every {args.replan_slots} slots"
        if args.replan_slots
        else "benchmark default (two events per day)",
        "rule": f"largest cost whose cost penalty vs B is <= {args.rule_pct_of_saving:g}% of "
        "B's saving in every window",
        "recommended_eur_per_kwh_h": recommended,
        "runs": [
            {k: v for k, v in r.items() if k not in ("cost", "cost_no_battery")} for r in results
        ],
        "table": table,
    }
    if args.full_charge:
        report["full_charge"] = full_charge_study(args, loaded, cfg, costs)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_part_atomic(out_dir / "table.json", json_bytes(report))
    md = render(table, names, args.rule_pct_of_saving)
    md += f"\nRecommended (rule above): {recommended}\n"
    if report.get("full_charge"):
        md += "\n## Full-charge day\n\n" + json.dumps(report["full_charge"], indent=1) + "\n"
    write_part_atomic(out_dir / "table.md", md.encode("utf-8"))
    print(md)
    print(f"high_soc_dwell_sweep: written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
