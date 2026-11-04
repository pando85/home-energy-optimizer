"""Per-slot plan values: the **day-ahead plan** of every slot, next to the **final plan**.

Definitions (docs/service.md, "Day-ahead and final plan"):

* the *day-ahead plan* of a slot is the **first** plan, by ``(generated_at, plan_sequence)``,
  whose intents covered the slot and that was generated at or before the slot's start (usually
  made the previous afternoon, when the next day's PVPC prices are published; the lead time
  varies).  A plan made after the slot began (a catch-up plan) is not a forecast of it;
* the *final plan* is the plan of record when the slot opened: the intent frozen at the slot
  open (``current_intent`` in ``/plan``);
* the *actual* is what the slot close measured.

Only the plan's own numbers are tracked here: the signed battery power, the SOC target at the
slot end, the house-load forecast of the slot and the expected buy price.  The load forecast is
the plan's ``load_kwh`` input for the slot (``inputs.load_kwh`` of the plan file).

The tracker holds one :class:`SlotPlan` per slot from a short look-back to the end of the
longest horizon, so its memory is bounded.  Ordering by ``(generated_at, plan_sequence)``
rather than by arrival makes it independent of the order plans are fed in, which is what lets a
restart rebuild it from the immutable plan files of the sink (:meth:`DayAheadTracker.rebuild`).
Observability only: nothing here reaches the planner or the actuator.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .sink import iter_records
from .virtual_battery import intent_energy_kwh

__all__ = [
    "DayAheadTracker",
    "SlotPlan",
    "revision_kwh",
    "slot_plans_from_record",
    "slot_plans_from_result",
]

log = logging.getLogger(__name__)

#: How long a slot stays tracked after its end (a slot closes ``slot_close_grace_s`` after it).
RETENTION = timedelta(hours=2)


@dataclass(frozen=True)
class SlotPlan:
    """What one plan said about one slot."""

    plan_id: str
    plan_sequence: int
    generated_at: datetime
    slot_start: datetime
    interval_seconds: int
    mode: str
    signed_power_w: float
    """``+`` charge, ``-`` discharge (requested AC power), the sign of ``current_intent``."""
    soc_target_frac: float
    """SOC target at the end of the slot."""
    load_kwh: float | None
    """The plan's house-load forecast for the slot (``None``: the plan record has none)."""
    expected_buy_price_eur_per_kwh: float | None

    @property
    def order(self) -> tuple[datetime, int]:
        return (self.generated_at, self.plan_sequence)

    @property
    def lead_seconds(self) -> float:
        """Slot start minus the plan's generation time (negative for a plan made inside it)."""
        return (self.slot_start - self.generated_at).total_seconds()

    @property
    def load_forecast_w(self) -> float | None:
        """Mean forecast house power over the slot."""
        if self.load_kwh is None:
            return None
        return self.load_kwh * 3_600_000.0 / self.interval_seconds

    def energy_kwh(self) -> tuple[float, float]:
        """``(charge_kwh, discharge_kwh)`` the plan asks for over the whole slot (AC side)."""
        return intent_energy_kwh(self.mode, abs(self.signed_power_w), self.interval_seconds)

    def net_kwh(self) -> float:
        """Requested net battery energy over the slot: ``+`` charge, ``-`` discharge."""
        charge, discharge = self.energy_kwh()
        return charge - discharge

    def to_dict(self) -> dict[str, Any]:
        charge, discharge = self.energy_kwh()
        return {
            "plan_id": self.plan_id,
            "plan_sequence": self.plan_sequence,
            "generated_at": self.generated_at.isoformat(),
            "lead_seconds": self.lead_seconds,
            "mode": self.mode,
            "signed_power_w": self.signed_power_w,
            "soc_target_frac": self.soc_target_frac,
            "load_forecast_kwh": self.load_kwh,
            "load_forecast_w": self.load_forecast_w,
            "expected_buy_price_eur_per_kwh": self.expected_buy_price_eur_per_kwh,
            "charge_kwh": charge,
            "discharge_kwh": discharge,
        }


def revision_kwh(day_ahead: SlotPlan | None, final: SlotPlan | None) -> float | None:
    """``|day-ahead - final|`` requested net battery energy of a slot, ``None`` if one is absent."""
    if day_ahead is None or final is None:
        return None
    return abs(day_ahead.net_kwh() - final.net_kwh())


def _utc(value: str | datetime) -> datetime:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if at.tzinfo is None:
        raise ValueError(f"naive timestamp {value!r}")
    return at.astimezone(UTC)


def slot_plans_from_result(result: Any) -> list[SlotPlan]:
    """The per-slot values of a :class:`~.planner.PlanResult`, one per intent."""
    snapshot = result.snapshot
    load = {
        start: float(kwh)
        for start, kwh in zip(snapshot.slot_starts, snapshot.load_kwh, strict=True)
    }
    return [
        SlotPlan(
            plan_id=intent.plan_id,
            plan_sequence=intent.plan_sequence,
            generated_at=intent.generated_at,
            slot_start=intent.valid_from,
            interval_seconds=snapshot.interval_seconds,
            mode=intent.mode,
            signed_power_w=intent.signed_power_w,
            soc_target_frac=intent.soc_target_frac,
            load_kwh=load.get(intent.valid_from),
            expected_buy_price_eur_per_kwh=intent.expected_buy_price_eur_per_kwh,
        )
        for intent in result.intents
    ]


def slot_plans_from_record(record: Mapping[str, Any]) -> list[SlotPlan]:
    """The per-slot values of a persisted plan record (``service/v1/plans/.../<plan_id>.json``).

    Raises ``KeyError``, ``ValueError`` or ``TypeError`` on a record that is not a plan.
    """
    inputs = record.get("inputs") or {}
    interval = int(inputs["interval_seconds"]) if "interval_seconds" in inputs else None
    load: dict[datetime, float] = {}
    starts, values = inputs.get("slot_starts") or (), inputs.get("load_kwh") or ()
    if len(starts) == len(values):
        load = {_utc(s): float(v) for s, v in zip(starts, values, strict=True)}
    out: list[SlotPlan] = []
    for intent in record["intents"]:
        start = _utc(intent["valid_from"])
        step = (
            interval
            if interval is not None
            else int((_utc(intent["interval_end"]) - start).total_seconds())
        )
        price = intent.get("expected_buy_price_eur_per_kwh")
        out.append(
            SlotPlan(
                plan_id=str(intent["plan_id"]),
                plan_sequence=int(intent["plan_sequence"]),
                generated_at=_utc(intent["generated_at"]),
                slot_start=start,
                interval_seconds=step,
                mode=str(intent["mode"]),
                signed_power_w=float(intent["signed_power_w"]),
                soc_target_frac=float(intent["soc_target_frac"]),
                load_kwh=load.get(start),
                expected_buy_price_eur_per_kwh=None if price is None else float(price),
            )
        )
    return out


class DayAheadTracker:
    """The day-ahead :class:`SlotPlan` of each slot: the earliest plan that covered it.

    ``retention`` bounds the look-back: a slot ended more than that ago is dropped, and is never
    admitted.  ``None`` keeps everything (the offline tool).
    """

    def __init__(self, retention: timedelta | None = RETENTION) -> None:
        self._retention = retention
        self._slots: dict[datetime, SlotPlan] = {}

    def __len__(self) -> int:
        return len(self._slots)

    def get(self, slot_start: datetime) -> SlotPlan | None:
        return self._slots.get(_utc(slot_start))

    def upcoming(self, now: datetime) -> list[SlotPlan]:
        """Tracked slots that have not ended at *now* (the one in progress included), in order."""
        return [
            p
            for _, p in sorted(self._slots.items())
            if p.slot_start + timedelta(seconds=p.interval_seconds) > now
        ]

    def observe(self, plans: Iterable[SlotPlan], now: datetime) -> int:
        """Admit the earliest plan per slot; returns how many slots changed.

        A plan counts for a slot only if it was generated at or before the slot's start.
        """
        cutoff = None if self._retention is None else now - self._retention
        changed = 0
        for plan in plans:
            if plan.generated_at > plan.slot_start:
                continue
            end = plan.slot_start + timedelta(seconds=plan.interval_seconds)
            if cutoff is not None and end <= cutoff:
                continue
            known = self._slots.get(plan.slot_start)
            if known is None or plan.order < known.order:
                self._slots[plan.slot_start] = plan
                changed += 1
        return changed

    def prune(self, now: datetime) -> int:
        """Drop the slots that ended before the retention window; returns how many."""
        if self._retention is None:
            return 0
        cutoff = now - self._retention
        old = [
            s for s, p in self._slots.items() if s + timedelta(seconds=p.interval_seconds) <= cutoff
        ]
        for slot in old:
            del self._slots[slot]
        return len(old)

    def rebuild(self, data_dir: Path, now: datetime) -> int:
        """Re-observe the plan files of the last two UTC days; returns the plan files used.

        The files are immutable and the choice is by ``(generated_at, plan_sequence)``, so a
        rebuild gives what an uninterrupted process would hold.  A corrupt file is skipped.
        """
        used = 0
        for record in iter_records(data_dir, "plans", now, now):
            try:
                slots = slot_plans_from_record(record)
            except KeyError, ValueError, TypeError:
                log.warning("day-ahead rebuild: skipped an unreadable plan record")
                continue
            self.observe(slots, now)
            used += 1
        return used
