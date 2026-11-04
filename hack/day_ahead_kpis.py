#!/usr/bin/env python3
"""Day-ahead vs final vs actual KPIs of the battery plan, from the service store (read-only).

For a window of UTC days it reads the plan files and the closed-slot rows the service recorded
under ``<data_dir>/service/v1/`` and prints, per plan (day-ahead = the first plan that covered a
slot, final = the plan of record when the slot opened), the house-load forecast error (MAE, bias,
daily energy error), the battery energy each plan asked for against what the battery did, the plan
revision between the two, the average price paid to charge against the average price earned
discharging, the saving against a house without the battery, and the SOC target error.  See
``battery_optimizer.service.plan_kpis`` for the exact definitions.

Usage:
    uv run python hack/day_ahead_kpis.py --from 2026-11-04 --to 2026-11-11 \\
        [--data-dir ~/data/<store>] [--tz Europe/Madrid] [--run-id <name>] [--json]

``--from`` is inclusive and ``--to`` exclusive (UTC days).  ``BATTERY_OPTIMIZER_DATA_DIR`` replaces
``--data-dir``.  The summary goes to stdout (``--json`` for the JSON report).  With ``--run-id`` the
report and the per-slot table are also written to
``<data_dir>/analysis/v1/day-ahead-kpis/<run-id>/`` (``report.json``, ``slots.csv``); the repository
is never touched and nothing is written without ``--run-id``.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "src"))

from battery_optimizer.paths import resolve_data_dir, write_part_atomic  # noqa: E402
from battery_optimizer.service.plan_kpis import SlotComparison, build_kpi_report  # noqa: E402


def _run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or ".." in value:
        raise argparse.ArgumentTypeError(f"{value!r}: use only [A-Za-z0-9._-] and no '..'")
    return value


def _day(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)


def _f(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_text(report: dict[str, Any]) -> str:
    out = [
        f"window {report['window']['since']} .. {report['window']['until']}",
        f"slots {report['slots']}  with day-ahead {report['slots_with_day_ahead']}  "
        f"with final {report['slots_with_final']}  "
        f"(plan files read {report['coverage']['plan_files_read']}, final intents without a plan "
        f"file {report['coverage']['final_without_plan_file']})",
        "day-ahead lead (h): min {min}  mean {mean}  max {max}".format(
            **{k: _f(v, 1) for k, v in report["day_ahead_lead_hours"].items()}
        ),
        "",
        "House load forecast (bias = actual - forecast, kWh per slot)",
    ]
    for plan, k in report["load"].items():
        out.append(
            f"  {plan:<10} scored {k['scored_slots']:>5}  MAE {_f(k['mae_kwh_per_slot'], 4)}  "
            f"bias {_f(k['bias_kwh_per_slot'], 4)}  daily |error| "
            f"{_f(k['mean_abs_daily_error_pct'], 1)}% over {k['complete_days']} complete days"
        )
    b = report["battery"]
    out += [
        "",
        f"Battery plan vs actual over {b['comparable_slots']} comparable slots (AC kWh, EUR)",
    ]
    for name in ("day_ahead", "final", "actual"):
        k = b[name]
        out.append(
            f"  {name:<10} charge {_f(k['charge_kwh'], 2)}  discharge {_f(k['discharge_kwh'], 2)}  "
            f"avg price charging {_f(k['avg_price_charging_eur_per_kwh'], 4)}  "
            f"discharging {_f(k['avg_price_discharging_eur_per_kwh'], 4)}  "
            f"saving {_f(k['saving_eur'], 2)}"
        )
    rev = b["plan_revision"]
    out.append(
        f"  plan revision |day-ahead - final|: {_f(rev['total_kwh'], 2)} kWh over "
        f"{rev['slots_revised']} revised slots"
    )
    out += ["", "SOC target error at the slot end (actual - target, fraction)"]
    for plan, k in report["soc"].items():
        out.append(
            f"  {plan:<10} slots {k['slots']:>5}  MAE {_f(k['mae_frac'], 4)}  "
            f"bias {_f(k['bias_frac'], 4)}"
        )
    return "\n".join(out) + "\n"


def slots_csv(slots: list[SlotComparison]) -> str:
    rows = [s.to_row() for s in slots]
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="since", type=_day, required=True)
    parser.add_argument("--to", dest="until", type=_day, required=True)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--tz", default="Europe/Madrid", help="time zone of the local days")
    parser.add_argument("--run-id", type=_run_id, default=None)
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    args = parser.parse_args(argv)
    if args.until <= args.since:
        parser.error("--to must be after --from")
    root = resolve_data_dir(args.data_dir)
    report, slots = build_kpi_report(root, args.since, args.until, tz=ZoneInfo(args.tz))
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(text if args.json else render_text(report))
    if args.run_id:
        out_dir = root / "analysis" / "v1" / "day-ahead-kpis" / args.run_id
        write_part_atomic(out_dir / "report.json", text.encode("utf-8"))
        write_part_atomic(out_dir / "slots.csv", slots_csv(slots).encode("utf-8"))
        print(f"written to {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
