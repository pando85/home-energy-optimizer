"""Offline KPIs of the day-ahead plan against the final plan and the actual.

Inputs are only what the service recorded under ``service/v1/``: the immutable plan files and the
closed-slot rows.  Nothing is fetched again and no plan is re-solved.  Definitions are those of
:mod:`.plan_tracker`:

* **day-ahead** of a slot: the first plan, by ``(generated_at, plan_sequence)``, that covered it
  and was generated at or before the slot's start;
* **final**: the intent frozen at the slot's open (the row's ``intent``), with the plan file's
  load forecast for the slot;
* **actual**: the slot close's measurements (``gross_load_kwh``, battery counters, SOC).

Battery KPIs are computed over the *comparable* slots only (a day-ahead plan, a final intent and a
measured battery energy all present), so the three series cover the same slots.  Energies are AC
side.  The saving is ``sum(price x (discharge - charge))`` at the slot's recorded pretax PVPC
price: the cost avoided against a house without the battery, before the energy left in the battery
at the end of the window is valued and before any tax.  Load KPIs are per plan, over the slots
where the plan has a load forecast and the load was measured.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .plan_tracker import DayAheadTracker, SlotPlan, revision_kwh, slot_plans_from_record
from .sink import iter_records
from .timegrid import local_day_slots

__all__ = ["SlotComparison", "build_kpi_report", "compare_slots", "summarise"]

#: How far before the window a plan may have been generated and still be a slot's day-ahead plan.
_LOOKBACK = timedelta(days=3)


@dataclass
class SlotComparison:
    """One closed slot: day-ahead vs final vs actual."""

    slot_start: datetime
    local_day: str
    price_eur_per_kwh: float | None
    load_actual_kwh: float | None
    battery_charge_kwh: float | None
    battery_discharge_kwh: float | None
    soc_end_frac: float | None
    day_ahead: SlotPlan | None
    final: SlotPlan | None
    decision: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def revision_kwh(self) -> float | None:
        return revision_kwh(self.day_ahead, self.final)

    def to_row(self) -> dict[str, Any]:
        def plan(prefix: str, p: SlotPlan | None) -> dict[str, Any]:
            return {
                f"{prefix}_plan_id": p.plan_id if p else None,
                f"{prefix}_lead_s": p.lead_seconds if p else None,
                f"{prefix}_power_w": p.signed_power_w if p else None,
                f"{prefix}_soc_target": p.soc_target_frac if p else None,
                f"{prefix}_load_kwh": p.load_kwh if p else None,
                f"{prefix}_charge_kwh": p.energy_kwh()[0] if p else None,
                f"{prefix}_discharge_kwh": p.energy_kwh()[1] if p else None,
            }

        return {
            "slot_start": self.slot_start.isoformat(),
            "local_day": self.local_day,
            "decision": self.decision,
            "price_eur_per_kwh": self.price_eur_per_kwh,
            **plan("day_ahead", self.day_ahead),
            **plan("final", self.final),
            "actual_load_kwh": self.load_actual_kwh,
            "actual_charge_kwh": self.battery_charge_kwh,
            "actual_discharge_kwh": self.battery_discharge_kwh,
            "actual_soc_end": self.soc_end_frac,
            "plan_revision_kwh": self.revision_kwh,
        }


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _intent_plan(row: dict[str, Any], interval: int) -> SlotPlan | None:
    intent = row.get("intent")
    if not intent:
        return None
    return SlotPlan(
        plan_id=str(intent["plan_id"]),
        plan_sequence=int(intent["plan_sequence"]),
        generated_at=_utc(intent["generated_at"]),
        slot_start=_utc(intent["valid_from"]),
        interval_seconds=interval,
        mode=str(intent["mode"]),
        signed_power_w=float(intent["signed_power_w"]),
        soc_target_frac=float(intent["soc_target_frac"]),
        load_kwh=None,
        expected_buy_price_eur_per_kwh=intent.get("expected_buy_price_eur_per_kwh"),
    )


def compare_slots(
    data_dir: Path, since: datetime, until: datetime
) -> tuple[list[SlotComparison], dict[str, Any]]:
    """Every closed slot of ``[since, until)`` with its day-ahead and final plan.

    Also returns what the plan files lacked (``missing``), for the report's coverage note.
    """
    rows: dict[datetime, dict[str, Any]] = {}
    for row in iter_records(data_dir, "slots", since, until):
        try:
            start = _utc(row["slot_start"])
        except KeyError, ValueError:
            continue
        if since <= start < until:
            rows[start] = row  # a restart can append a slot twice: the last row stands
    needed = {
        (str(r["intent"]["plan_id"]), _utc(r["intent"]["valid_from"]))
        for r in rows.values()
        if r.get("intent")
    }
    tracker = DayAheadTracker(retention=None)
    finals: dict[tuple[str, datetime], SlotPlan] = {}
    plans_read = 0
    for record in iter_records(data_dir, "plans", since - _LOOKBACK, until):
        try:
            slots = slot_plans_from_record(record)
        except KeyError, ValueError, TypeError:
            continue
        plans_read += 1
        tracker.observe((p for p in slots if p.slot_start >= since and p.slot_start < until), until)
        for p in slots:
            if (p.plan_id, p.slot_start) in needed:
                finals[(p.plan_id, p.slot_start)] = p
    out: list[SlotComparison] = []
    final_without_plan = 0
    for start in sorted(rows):
        row = rows[start]
        interval = int(row.get("interval_seconds") or 900)
        measured = row.get("measured") or {}
        final = _intent_plan(row, interval)
        if final is not None:
            from_file = finals.get((final.plan_id, start))
            if from_file is None:
                final_without_plan += 1
            else:
                final = from_file
        out.append(
            SlotComparison(
                slot_start=start,
                local_day=str(row.get("local_day") or ""),
                price_eur_per_kwh=row.get("price_eur_per_kwh"),
                load_actual_kwh=row.get("gross_load_kwh"),
                battery_charge_kwh=measured.get("battery_charge_kwh"),
                battery_discharge_kwh=measured.get("battery_discharge_kwh"),
                soc_end_frac=measured.get("soc_end_frac"),
                day_ahead=tracker.get(start),
                final=final,
                decision=str(row.get("decision")),
            )
        )
    return out, {"plan_files_read": plans_read, "final_without_plan_file": final_without_plan}


def _mean(values: list[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _load_kpis(
    slots: list[SlotComparison], plan: str, tz: ZoneInfo, interval: int
) -> dict[str, Any]:
    scored = [
        (s, p.load_kwh, s.load_actual_kwh)
        for s in slots
        if (p := getattr(s, plan)) is not None
        and p.load_kwh is not None
        and s.load_actual_kwh is not None
    ]
    errors = [actual - forecast for _, forecast, actual in scored]
    days: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s, forecast, actual in scored:
        days[s.local_day].append((forecast, actual))
    daily = []
    for day, items in sorted(days.items()):
        forecast_kwh = math.fsum(f for f, _ in items)
        actual_kwh = math.fsum(a for _, a in items)
        expected = len(local_day_slots(date.fromisoformat(day), tz, interval))
        daily.append(
            {
                "local_day": day,
                "scored_slots": len(items),
                "complete": len(items) == expected,
                "forecast_kwh": forecast_kwh,
                "actual_kwh": actual_kwh,
                "error_pct": None
                if actual_kwh <= 0
                else 100.0 * (forecast_kwh - actual_kwh) / actual_kwh,
            }
        )
    complete = [d["error_pct"] for d in daily if d["complete"] and d["error_pct"] is not None]
    return {
        "scored_slots": len(scored),
        "mae_kwh_per_slot": _mean([abs(e) for e in errors]),
        "bias_kwh_per_slot": _mean(errors),  # actual - forecast: positive = under-forecast
        "forecast_kwh": math.fsum(f for _, f, _ in scored),
        "actual_kwh": math.fsum(a for _, _, a in scored),
        "daily": daily,
        "complete_days": len(complete),
        "mean_abs_daily_error_pct": _mean([abs(e) for e in complete]),
        "mean_daily_error_pct": _mean(complete),  # forecast vs actual: positive = over-forecast
    }


def _battery_kpis(slots: list[SlotComparison]) -> dict[str, Any]:
    comparable = [
        s
        for s in slots
        if s.day_ahead is not None
        and s.final is not None
        and s.battery_charge_kwh is not None
        and s.battery_discharge_kwh is not None
    ]
    series: dict[str, list[tuple[SlotComparison, float, float]]] = {
        "day_ahead": [(s, *s.day_ahead.energy_kwh()) for s in comparable if s.day_ahead],
        "final": [(s, *s.final.energy_kwh()) for s in comparable if s.final],
        "actual": [
            (s, s.battery_charge_kwh or 0.0, s.battery_discharge_kwh or 0.0) for s in comparable
        ],
    }

    def weighted(items: list[tuple[SlotComparison, float, float]], leg: int) -> float | None:
        """Energy-weighted mean price of the charge (leg 1) or discharge (leg 2) energy."""
        num = den = 0.0
        for item in items:
            price = item[0].price_eur_per_kwh
            if price is None:
                continue
            num += price * item[leg]
            den += item[leg]
        return num / den if den > 0 else None

    out: dict[str, Any] = {"comparable_slots": len(comparable)}
    for name, items in series.items():
        saving_by_day: dict[str, float] = defaultdict(float)
        for s, charge, discharge in items:
            if s.price_eur_per_kwh is not None:
                saving_by_day[s.local_day] += s.price_eur_per_kwh * (discharge - charge)
        out[name] = {
            "charge_kwh": math.fsum(i[1] for i in items),
            "discharge_kwh": math.fsum(i[2] for i in items),
            "avg_price_charging_eur_per_kwh": weighted(items, 1),
            "avg_price_discharging_eur_per_kwh": weighted(items, 2),
            "saving_eur": math.fsum(saving_by_day.values()),
            "saving_eur_per_day": dict(sorted(saving_by_day.items())),
        }
    revisions = [(s.local_day, s.revision_kwh) for s in comparable if s.revision_kwh is not None]
    per_day: dict[str, float] = defaultdict(float)
    for day, kwh in revisions:
        per_day[day] += kwh
    out["plan_revision"] = {
        "total_kwh": math.fsum(k for _, k in revisions),
        "slots_revised": sum(1 for _, k in revisions if k > 1e-9),
        "per_day_kwh": dict(sorted(per_day.items())),
    }
    # How far each plan's net battery energy is from what the battery did, slot by slot.
    for name in ("day_ahead", "final"):
        out[name]["mae_net_kwh_per_slot"] = _mean(
            [
                abs(
                    (i[1] - i[2])
                    - ((i[0].battery_charge_kwh or 0.0) - (i[0].battery_discharge_kwh or 0.0))
                )
                for i in series[name]
            ]
        )
    return out


def _soc_kpis(slots: list[SlotComparison]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for plan in ("day_ahead", "final"):
        diffs = [
            s.soc_end_frac - p.soc_target_frac
            for s in slots
            if (p := getattr(s, plan)) is not None and s.soc_end_frac is not None
        ]
        out[plan] = {
            "slots": len(diffs),
            "mae_frac": _mean([abs(d) for d in diffs]),
            "bias_frac": _mean(diffs),  # actual - target
        }
    return out


def summarise(
    slots: list[SlotComparison], *, tz: ZoneInfo, interval_seconds: int = 900
) -> dict[str, Any]:
    """The KPI summary of compared slots."""
    leads = [s.day_ahead.lead_seconds for s in slots if s.day_ahead is not None]
    return {
        "slots": len(slots),
        "slots_with_day_ahead": sum(1 for s in slots if s.day_ahead is not None),
        "slots_with_final": sum(1 for s in slots if s.final is not None),
        "day_ahead_lead_hours": {
            "min": min(leads) / 3600 if leads else None,
            "mean": (_mean(leads) or 0.0) / 3600 if leads else None,
            "max": max(leads) / 3600 if leads else None,
        },
        "load": {
            plan: _load_kpis(slots, plan, tz, interval_seconds) for plan in ("day_ahead", "final")
        },
        "battery": _battery_kpis(slots),
        "soc": _soc_kpis(slots),
    }


def build_kpi_report(
    data_dir: Path, since: datetime, until: datetime, *, tz: ZoneInfo
) -> tuple[dict[str, Any], list[SlotComparison]]:
    """The KPI report of ``[since, until)`` from the store under *data_dir*."""
    slots, coverage = compare_slots(data_dir, since, until)
    report = {
        "window": {"since": since.isoformat(), "until": until.isoformat(), "tz": str(tz)},
        "coverage": coverage,
        **summarise(slots, tz=tz),
    }
    return report, slots
