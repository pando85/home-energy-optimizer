"""Shadow-vs-native comparison over the recorded store (``shadow-report``).

Inputs are only what the service recorded: closed-slot rows (measurements, the
decision frozen at slot start, the PVPC price), plan files and events.  Nothing
is fetched again, so a report is reproducible from the store alone.

Method (stated in every report as ``assumptions``):

1. **Coverage** — expected slots are the local days' real quarter-hours (92 /
   96 / 100 on DST days); a slot is *covered* when a valid, unexpired shadow
   intent was in force at its start and the inputs were fresh.  A slot that
   adopted an intent later (``late_intent``) is not covered.
2. **Planned vs actual** — requested battery energy per slot (intent power x
   the time it was in force: the slot, or its rest after a late adoption)
   against the energy the native controller actually moved
   (battery counters, else integrated battery power, labelled), plus the mean
   absolute and mean signed difference of slot-average power.
3. **Cost** — the native cost is ``price x measured grid import`` per slot.  The
   counterfactual replays the decisions open-loop on a virtual battery that
   starts from the first slot's measured SOC (or the recorded virtual SOC before
   commissioning), using the measured gross load
   ``grid - battery_charge + battery_discharge`` and the benchmark's own
   clipping (:mod:`.virtual_battery`): ``price x (load + c - d)``.  Both costs
   use the same slots, prices and loads.  The battery state at the end of the
   window differs between the two, so an inventory-adjusted saving values the
   difference at the plans' terminal value.
4. **SOC-limit violations** — decisions that, applied to the *measured* SOC at
   slot start, would have left the configured SOC window; plus the energy the
   replay had to clip.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from battery_optimizer.config import TariffConfig

from .planner import battery_spec_from_tariff, clamp_soc
from .sink import iter_records
from .timegrid import local_date, local_day_bounds_utc, local_day_slots
from .virtual_battery import intent_energy_kwh, step_virtual_battery

__all__ = ["ReportError", "build_shadow_report", "parse_window", "render_markdown"]


class ReportError(ValueError):
    """The window or the store cannot produce a report."""


ASSUMPTIONS = (
    "Costs are pretax PVPC energy only (tariff taxes are unconfirmed in tariff.yaml); fixed "
    "charges are excluded, so savings are variable-energy savings.",
    "The counterfactual is an open-loop replay: each slot's frozen decision is applied to a "
    "virtual battery against the measured gross load, clipped by power, SOC window, import "
    "ceiling and the no-export rule (the rolling-horizon benchmark's clipping). The plans "
    "themselves were computed from the measured (entity) or virtual (simulated) SOC, so the "
    "replayed SOC can drift from the one the planner saw.",
    "Gross load = grid import - battery charge + battery discharge. Without mapped battery "
    "telemetry the battery is assumed absent and gross load equals grid import.",
    "Energy comes from cumulative counters at slot edges; integrated power is used only when a "
    "counter edge is unknown and is labelled power_integral.",
    "Battery efficiency, SOC window and power are the tariff.yaml values (provisional, not "
    "measured on the installed unit); idle self-discharge is not modelled.",
    "Slots without a decision replay as idle; a slot that adopted an intent late replays it for "
    "the rest of the slot only (its average power); slots without measured load or price are "
    "excluded from the cost comparison and counted.",
)
#: Slot decisions that carry an intent the battery was asked to follow.
_INTENT_DECISIONS = ("intent", "late_intent")


@dataclass(frozen=True)
class _Slot:
    start: datetime
    row: dict[str, Any]


def _intent_share(row: dict[str, Any], interval_seconds: int) -> float:
    """The share of the slot its intent was in force (``adopted_after_s`` of a late intent)."""
    after = _f(row.get("adopted_after_s"))
    if after is None:
        return 1.0
    return min(1.0, max(0.0, 1.0 - after / interval_seconds))


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def _load_slots(data_dir: Path, since: datetime, until: datetime) -> list[_Slot]:
    best: dict[datetime, dict[str, Any]] = {}
    for row in iter_records(data_dir, "slots", since, until):
        try:
            start = datetime.fromisoformat(row["slot_start"])
        except KeyError, TypeError, ValueError:
            continue
        if not since <= start < until:
            continue
        previous = best.get(start)
        # A restart can close one slot twice; keep the row observed for longest.
        if previous is None or row.get("observed_from", "") < previous.get("observed_from", ""):
            best[start] = row
    return [_Slot(s, best[s]) for s in sorted(best)]


def _gross_load(row: dict[str, Any]) -> float | None:
    value = _f(row.get("gross_load_kwh"))
    if value is not None:
        return value
    measured = row.get("measured") or {}
    grid = _f(measured.get("grid_kwh"))
    if grid is None:
        return None
    if not row.get("battery_mapped"):
        return grid
    charge = _f(measured.get("battery_charge_kwh"))
    discharge = _f(measured.get("battery_discharge_kwh"))
    if charge is None or discharge is None:
        return None
    return grid - charge + discharge


def _new_day() -> dict[str, Any]:
    return {
        "slots_expected": 0,
        "slots_recorded": 0,
        "decisions": Counter(),
        "planned_charge_kwh": 0.0,
        "planned_discharge_kwh": 0.0,
        "actual_charge_kwh": 0.0,
        "actual_discharge_kwh": 0.0,
        "actual_battery_slots": 0,
        "power_abs_dev_sum_w": 0.0,
        "power_dev_sum_w": 0.0,
        "power_dev_slots": 0,
        "comparable_slots": 0,
        "native_cost_eur": 0.0,
        "counterfactual_cost_eur": 0.0,
        "native_grid_kwh": 0.0,
        "counterfactual_grid_kwh": 0.0,
        "soc_violations": 0,
        "clipped_kwh": 0.0,
        "excluded_no_load": 0,
        "excluded_no_price": 0,
        "solves_skipped": Counter(),
        "solve_failures": 0,
        "plans": 0,
        "ha_disconnects": 0,
    }


def build_shadow_report(
    data_dir: Path,
    *,
    since: datetime,
    until: datetime,
    tariff: TariffConfig,
    tz: ZoneInfo,
    interval_seconds: int = 900,
) -> dict[str, Any]:
    if since.tzinfo is None or until.tzinfo is None:
        raise ReportError("--since/--until must be timezone-aware")
    if until <= since:
        raise ReportError("--until must be after --since")
    slots = _load_slots(data_dir, since, until)
    base = battery_spec_from_tariff(tariff, float(tariff.battery.soc_window_min or 0.0))
    spec, import_limit, provenance = base

    days: dict[str, dict[str, Any]] = defaultdict(_new_day)
    day = local_date(since, tz)
    while day <= local_date(until - timedelta(microseconds=1), tz):
        for start in local_day_slots(day, tz, interval_seconds):
            if since <= start < until:
                days[day.isoformat()]["slots_expected"] += 1
        day += timedelta(days=1)

    # Replay seed: measured SOC (entity) or the recorded virtual SOC (simulated).
    soc: float | None = None
    soc_origin = "none"
    battery_mapped = any(s.row.get("battery_mapped") for s in slots)
    soc_sources = Counter(str(s.row.get("soc_source")) for s in slots)
    for s in slots:
        measured_soc = _f((s.row.get("measured") or {}).get("soc_start_frac"))
        virtual = s.row.get("virtual") or {}
        if measured_soc is not None:
            soc, soc_origin = measured_soc, f"measured SOC at {s.start.isoformat()}"
            break
        if _f(virtual.get("soc_start_frac")) is not None:
            soc = _f(virtual.get("soc_start_frac"))
            soc_origin = f"recorded virtual SOC at {s.start.isoformat()}"
            break
    if soc is None:
        initial = tariff.battery.initial_soc_frac or tariff.battery.soc_window_min or 0.0
        soc, soc_origin = float(initial), "tariff.yaml initial_soc_frac"
    soc = clamp_soc(soc, tariff)[0]
    cf_start_soc = soc
    native_start_soc: float | None = None
    native_end_soc: float | None = None

    expected_next: datetime | None = None
    gaps = 0
    for s in slots:
        row = s.row
        key = str(row.get("local_day") or local_date(s.start, tz).isoformat())
        d = days[key]
        d["slots_recorded"] += 1
        if expected_next is not None and s.start > expected_next:
            gaps += int((s.start - expected_next).total_seconds() // interval_seconds)
        expected_next = s.start + timedelta(seconds=interval_seconds)

        decision = str(row.get("decision") or "not_observed")
        d["decisions"][decision] += 1
        intent = row.get("intent") if decision in _INTENT_DECISIONS else None
        share = _intent_share(row, interval_seconds)
        mode = intent.get("mode") if intent else None
        target = (_f(intent.get("target_power_w")) or 0.0) * share if intent else 0.0
        planned_c, planned_d = intent_energy_kwh(mode, target or 0.0, interval_seconds)
        d["planned_charge_kwh"] += planned_c
        d["planned_discharge_kwh"] += planned_d

        measured = row.get("measured") or {}
        a_c = _f(measured.get("battery_charge_kwh"))
        a_d = _f(measured.get("battery_discharge_kwh"))
        if a_c is not None and a_d is not None:
            d["actual_charge_kwh"] += a_c
            d["actual_discharge_kwh"] += a_d
            d["actual_battery_slots"] += 1
        actual_w = _f(measured.get("battery_power_avg_w"))
        if intent is not None and actual_w is not None:
            planned_w = (_f(intent.get("signed_power_w")) or 0.0) * share
            d["power_abs_dev_sum_w"] += abs(planned_w - actual_w)
            d["power_dev_sum_w"] += planned_w - actual_w
            d["power_dev_slots"] += 1
        if row.get("soc_violation") is True:
            d["soc_violations"] += 1
        m_soc0 = _f(measured.get("soc_start_frac"))
        m_soc1 = _f(measured.get("soc_end_frac"))
        if native_start_soc is None and m_soc0 is not None:
            native_start_soc = m_soc0
        if m_soc1 is not None:
            native_end_soc = m_soc1

        load = _gross_load(row)
        step = step_virtual_battery(
            spec,
            soc_start_frac=soc,
            mode=mode,
            target_power_w=target or 0.0,
            load_kwh=load,
            interval_seconds=interval_seconds,
            import_limit_kw=import_limit,
        )
        soc = step.soc_end_frac
        d["clipped_kwh"] += step.clipped_charge_kwh + step.clipped_discharge_kwh
        price = _f(row.get("price_eur_per_kwh"))
        grid = _f(measured.get("grid_kwh"))
        if load is None or grid is None:
            d["excluded_no_load"] += 1
            continue
        if price is None:
            d["excluded_no_price"] += 1
            continue
        assert step.grid_kwh is not None
        d["comparable_slots"] += 1
        d["native_cost_eur"] += price * grid
        d["counterfactual_cost_eur"] += price * step.grid_kwh
        d["native_grid_kwh"] += grid
        d["counterfactual_grid_kwh"] += step.grid_kwh

    terminal_values: list[float] = []
    # Plans made shortly before the window still decide its first slots, so they
    # contribute their terminal value; only plans inside the window are counted.
    lookback = since - timedelta(hours=1)
    for plan in iter_records(data_dir, "plans", lookback, until):
        try:
            generated = datetime.fromisoformat(plan["generated_at"])
        except KeyError, TypeError, ValueError:
            continue
        if not lookback <= generated < until:
            continue
        if generated >= since:
            days[local_date(generated, tz).isoformat()]["plans"] += 1
        tv = _f((plan.get("snapshot") or {}).get("terminal_value_eur_per_kwh"))
        if tv is not None:
            terminal_values.append(tv)
    for event in iter_records(data_dir, "events", since, until):
        try:
            at = datetime.fromisoformat(event["at"])
        except KeyError, TypeError, ValueError:
            continue
        if not since <= at < until:
            continue
        d = days[local_date(at, tz).isoformat()]
        kind = event.get("kind")
        if kind == "solve_skipped":
            d["solves_skipped"][str(event.get("reason"))] += 1
        elif kind in ("solve_error", "solve_timeout"):
            d["solve_failures"] += 1
        elif kind == "ha_disconnected":
            d["ha_disconnects"] += 1

    day_rows = [_finish_day(k, v) for k, v in sorted(days.items())]
    overall = _finish_day("overall", _sum_days(days.values()))

    capacity = spec.capacity_kwh
    tv = sum(terminal_values) / len(terminal_values) if terminal_values else None
    cf_delta_kwh = (soc - cf_start_soc) * capacity
    native_delta_kwh = (
        (native_end_soc - native_start_soc) * capacity
        if battery_mapped and native_start_soc is not None and native_end_soc is not None
        else 0.0
    )
    savings = overall["native_cost_eur"] - overall["counterfactual_cost_eur"]
    adjusted = None
    if tv is not None:
        adjusted = savings + tv * (cf_delta_kwh - native_delta_kwh)
    covered_days = sum(1 for r in day_rows if r["coverage"] is not None and r["coverage"] >= 0.9)
    basis = (
        "native_battery"
        if battery_mapped and soc_sources.get("entity", 0) > 0
        else "no_battery_pre_commissioning"
    )
    return {
        "kind": "shadow_report",
        "window": {"since": since.isoformat(), "until": until.isoformat(), "timezone": str(tz)},
        "comparison_basis": basis,
        "soc_sources": dict(soc_sources),
        "battery_mapped": battery_mapped,
        "assumptions": list(ASSUMPTIONS),
        "battery_model": {
            "capacity_kwh": spec.capacity_kwh,
            "power_kw": spec.power_kw,
            "charge_efficiency": spec.charge_efficiency,
            "discharge_efficiency": spec.discharge_efficiency,
            "soc_window": [spec.soc_min_frac, spec.soc_max_frac],
            "import_limit_kw": import_limit,
            **provenance,
        },
        "replay": {
            "start_soc_frac": cf_start_soc,
            "start_soc_origin": soc_origin,
            "end_soc_frac": soc,
            "missing_slot_rows": gaps,
        },
        "inventory": {
            "terminal_value_eur_per_kwh": tv,
            "counterfactual_stored_delta_kwh": cf_delta_kwh,
            "native_stored_delta_kwh": native_delta_kwh,
        },
        "savings_eur": savings,
        "savings_inventory_adjusted_eur": adjusted,
        "exit_gate": {
            "days_with_coverage_ge_90pct": covered_days,
            "days_in_window": len(day_rows),
            "required_days": 14,
            "basis_ok": basis == "native_battery",
            "met": covered_days >= 14 and basis == "native_battery",
        },
        "overall": overall,
        "days": day_rows,
    }


def _sum_days(values: Any) -> dict[str, Any]:
    total = _new_day()
    for d in values:
        for key, value in d.items():
            if isinstance(value, Counter):
                total[key].update(value)
            else:
                total[key] += value
    return total


def _finish_day(label: str, d: dict[str, Any]) -> dict[str, Any]:
    covered = d["decisions"].get("intent", 0)
    expected = d["slots_expected"]
    pdev = d["power_dev_slots"]
    return {
        "day": label,
        "slots_expected": expected,
        "slots_recorded": d["slots_recorded"],
        "slots_with_intent": covered,
        "coverage": (covered / expected) if expected else None,
        "decisions": dict(d["decisions"]),
        "solves_skipped": dict(d["solves_skipped"]),
        "solve_failures": d["solve_failures"],
        "plans": d["plans"],
        "ha_disconnects": d["ha_disconnects"],
        "planned_charge_kwh": d["planned_charge_kwh"],
        "planned_discharge_kwh": d["planned_discharge_kwh"],
        "actual_charge_kwh": d["actual_charge_kwh"] if d["actual_battery_slots"] else None,
        "actual_discharge_kwh": d["actual_discharge_kwh"] if d["actual_battery_slots"] else None,
        "power_mae_w": d["power_abs_dev_sum_w"] / pdev if pdev else None,
        "power_bias_w": d["power_dev_sum_w"] / pdev if pdev else None,
        "comparable_slots": d["comparable_slots"],
        "native_cost_eur": d["native_cost_eur"],
        "counterfactual_cost_eur": d["counterfactual_cost_eur"],
        "savings_eur": d["native_cost_eur"] - d["counterfactual_cost_eur"],
        "native_grid_kwh": d["native_grid_kwh"],
        "counterfactual_grid_kwh": d["counterfactual_grid_kwh"],
        "soc_violations": d["soc_violations"],
        "clipped_kwh": d["clipped_kwh"],
        "excluded_no_load": d["excluded_no_load"],
        "excluded_no_price": d["excluded_no_price"],
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    w = report["window"]
    o = report["overall"]
    lines = [
        "# Shadow vs native report",
        "",
        f"Window: {w['since']} to {w['until']} ({w['timezone']}).  ",
        f"Comparison basis: **{report['comparison_basis']}** (SOC sources: "
        f"{report['soc_sources']}).",
        "",
        "## Overall",
        "",
        f"- Coverage: {_fmt(o['coverage'])} ({o['slots_with_intent']} of "
        f"{o['slots_expected']} expected slots had a valid shadow intent; "
        f"{o['slots_recorded']} recorded)",
        f"- Decisions: {o['decisions']}; skipped solves: {o['solves_skipped']}; "
        f"solve failures: {o['solve_failures']}; plans: {o['plans']}",
        f"- Planned energy: charge {_fmt(o['planned_charge_kwh'])} kWh, discharge "
        f"{_fmt(o['planned_discharge_kwh'])} kWh; actual: charge "
        f"{_fmt(o['actual_charge_kwh'])} kWh, discharge {_fmt(o['actual_discharge_kwh'])} kWh",
        f"- Power deviation (planned - actual): MAE {_fmt(o['power_mae_w'], 1)} W, bias "
        f"{_fmt(o['power_bias_w'], 1)} W",
        f"- Cost over {o['comparable_slots']} comparable slots: native "
        f"{_fmt(o['native_cost_eur'], 4)} EUR, counterfactual "
        f"{_fmt(o['counterfactual_cost_eur'], 4)} EUR, saving "
        f"{_fmt(report['savings_eur'], 4)} EUR (inventory-adjusted "
        f"{_fmt(report['savings_inventory_adjusted_eur'], 4)} EUR at "
        f"{_fmt(report['inventory']['terminal_value_eur_per_kwh'], 4)} EUR/kWh)",
        f"- SOC-limit violations: {o['soc_violations']}; replay clipped "
        f"{_fmt(o['clipped_kwh'])} kWh; excluded slots: {o['excluded_no_load']} without "
        f"load, {o['excluded_no_price']} without price",
        f"- Replay start SOC {_fmt(report['replay']['start_soc_frac'])} "
        f"({report['replay']['start_soc_origin']}), end "
        f"{_fmt(report['replay']['end_soc_frac'])}; missing slot rows "
        f"{report['replay']['missing_slot_rows']}",
        "",
        f"Exit gate C: {report['exit_gate']['days_with_coverage_ge_90pct']} day(s) with "
        f">= 90 % coverage of {report['exit_gate']['required_days']} required; basis ok: "
        f"{report['exit_gate']['basis_ok']}; met: **{report['exit_gate']['met']}**",
        "",
        "## Per day",
        "",
        "| Day | Coverage | Intent/expected | Planned c/d kWh | Actual c/d kWh | "
        "Native EUR | Counterfactual EUR | Saving EUR | SOC viol. | Skipped |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["days"]:
        lines.append(
            f"| {r['day']} | {_fmt(r['coverage'])} | {r['slots_with_intent']}/"
            f"{r['slots_expected']} | {_fmt(r['planned_charge_kwh'])}/"
            f"{_fmt(r['planned_discharge_kwh'])} | {_fmt(r['actual_charge_kwh'])}/"
            f"{_fmt(r['actual_discharge_kwh'])} | {_fmt(r['native_cost_eur'], 4)} | "
            f"{_fmt(r['counterfactual_cost_eur'], 4)} | {_fmt(r['savings_eur'], 4)} | "
            f"{r['soc_violations']} | {sum(r['solves_skipped'].values())} |"
        )
    lines += ["", "## Assumptions", ""]
    lines += [f"- {a}" for a in report["assumptions"]]
    lines += ["", "## Battery model", "", "```json", json.dumps(report["battery_model"], indent=2)]
    lines += ["```", ""]
    return "\n".join(lines)


def parse_window(
    since: str | None, until: str | None, tz: ZoneInfo, now: datetime
) -> tuple[datetime, datetime]:
    """ISO instants (with offset) or local dates (``YYYY-MM-DD`` = that day's local
    midnight).  Default: the last 14 complete local days before today."""

    def parse(value: str) -> datetime:
        text = value.strip()
        if len(text) == 10:
            return local_day_bounds_utc(date.fromisoformat(text), tz)[0]
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ReportError(f"{value!r} is naive: give a UTC offset or a local date")
        return moment

    until_dt = parse(until) if until else local_day_bounds_utc(local_date(now, tz), tz)[0]
    if since:
        since_dt = parse(since)
    else:
        since_dt = local_day_bounds_utc(local_date(until_dt, tz) - timedelta(days=14), tz)[0]
    return since_dt, until_dt
