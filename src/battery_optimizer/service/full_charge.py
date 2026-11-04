"""Periodic full charge for cell balancing: the decision rule and the run state machine.

Pure: no I/O, no clock, no Home Assistant.  The runtime feeds :meth:`FullChargeScheduler.step` an
:class:`Observation` every few seconds and the result of the planner's evaluation solve when one
is wanted; it reads back the :class:`~.actuator.FullChargeDirective` for the actuator, the state
for the state file and the events for the append-only records.  Plan and amendment:
``docs/planning/periodic-full-charge.md`` (the amendment wins where they differ).

**What counts as a full charge** (amendment A1).  Only a *new valid balance measurement*:
``last_balance_read`` advances past the value seen when the run was armed and the balance status
is not ``unknown``.  A run that reaches SOC 99 or 100 without a new reading is ``reached,
unmeasured``: a failed attempt, never balanced.  A stale, missing or ``unknown`` reading is
unknown, never zero imbalance.  Outside a run a new valid reading stamps ``L`` too, whoever caused
the charge; a SOC of 99 alone never does.

**Cadence** (A3).  ``min``/``max`` days come from the class of the latest valid readings, worst
first: ``alert`` (orange or red on ``alert_consecutive`` valid readings in a row), ``yellow``
(latest valid reading yellow, or a single orange or red), ``green`` (``min_comparable_readings``
valid readings, all green, the latest not older than the green row's ``max_days``, trend known and
not rising), else ``unknown``.  With ``d`` days since ``L``: ``d < min`` nothing, ``min <= d <
max`` the threshold test on the extra cost of the checkpoint, ``d >= max`` forced.  The bootstrap
has no ``L``: ``d`` is the ``unknown`` row's ``min`` until that many days passed since the
feature was enabled, then the days since enablement, so the first run is eligible at once and
forced ``max`` days after enablement.

**Run states**: ``idle``, ``scheduled`` (a day was chosen), ``armed`` (the directive says arm),
``charging`` (``integration_status`` is ``weekly_full_charge``), ``reached`` (SOC at or above
``reached_soc_pct``), ``settling`` (completion seen: the actuator holds idle until the reading
arrives or ``settle_timeout_s``).  Every run ends in one outcome (:data:`OUTCOMES`) and the
machine returns to ``idle`` in the same step; one attempt per local day.  In ``shadow`` and
``dry_run`` (``writer_mode: simulated``) nothing runs on the battery: a simulated run ends as
``done_simulated`` at the end of its day and moves a simulated ``L``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from .actuator import WEEKLY_FULL_CHARGE_STATUS, FullChargeDirective
from .config import CadenceRow, FullChargeSettings
from .ha_guard import WEEKDAY_OPTIONS

__all__ = [
    "ALERT_REASONS",
    "BALANCE_STATUSES_KNOWN",
    "CADENCE_CLASSES",
    "OUTCOMES",
    "STATES",
    "FullChargeScheduler",
    "FullChargeState",
    "Observation",
    "PlanSlot",
    "Reading",
    "Run",
    "arm_due",
    "cadence_class",
    "day_window",
    "threshold_eur",
]

SCHEMA_VERSION = 1
STATES: tuple[str, ...] = ("idle", "scheduled", "armed", "charging", "reached", "settling")
#: States in which the committed (target) plan is the plan of record.
COMMIT_STATES: tuple[str, ...] = ("scheduled", "armed", "charging", "reached")
#: States whose directive arms Omnibattery's weekly full charge.
ARMED_STATES: tuple[str, ...] = ("armed", "charging", "reached", "settling")
CADENCE_CLASSES: tuple[str, ...] = ("unknown", "green", "yellow", "alert")
BALANCE_STATUSES_KNOWN: tuple[str, ...] = ("green", "yellow", "orange", "red")
OUTCOMES: tuple[str, ...] = (
    "done",
    "done_simulated",
    "failed_never_armed",
    "failed_unacknowledged",
    "failed_never_reached",
    "failed_reached_unmeasured",
    "cancelled",
)
ALERT_REASONS: tuple[str, ...] = (
    "delay_switch_on",
    "timezone_mismatch",
    "entities_missing",
    "never_reached",
    "unacknowledged",
    "reached_unmeasured",
    "balance_alert",
    "paused",
    "state_unwritable",
)
#: Actuator refusal (``Actuator.full_charge_refusal``) -> alert while the refusal lasts.
REFUSAL_ALERTS: Mapping[str, str | None] = {
    "delay_switch_on": "delay_switch_on",
    "delay_switch_unreadable": "entities_missing",
    "day_options_invalid": "entities_missing",
    "entities_missing": "entities_missing",
    "kill_switch_off": None,
}
#: The only trend text that blocks the green cadence (Omnibattery: ``rising``, ``stable``,
#: ``falling``).
_RISING = "rising"
_TRENDS = ("rising", "stable", "falling")
#: Peak SOC (percent) at which a run whose status left counts as completed rather than aborted.
_COMPLETED_PEAK_PCT = 97.0
#: A forced-free evaluation whose target missed by more than this (kWh) is declined for the day.
SHORTFALL_TOLERANCE_KWH = 0.05
_READINGS_MIN_MEMORY = 8


def _iso(at: datetime | None) -> str | None:
    return at.astimezone(UTC).isoformat() if at is not None else None


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp in the full-charge state: {value!r}")
    return parsed.astimezone(UTC)


def _day(value: Any) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def threshold_eur(d: int, min_days: int, max_days: int, base_eur: float, scale_eur: float) -> float:
    """The extra cost one run may have on day ``d`` since ``L`` (amendment A3).

    ``base`` at ``d = min``, rising monotonically and diverging as ``d`` approaches ``max``
    (``inf`` from ``max`` on: that is the forced case).  Below ``min`` no run is allowed: ``-inf``.
    """
    if d < min_days:
        return -math.inf
    if d >= max_days:
        return math.inf
    return base_eur + scale_eur * (d - min_days) / (max_days - d)


@dataclass
class Reading:
    """One balance measurement as the sensors reported it.

    ``status`` follows the sensor until the next reading arrives (Omnibattery may update the
    timestamp and the status in two steps); ``None`` or ``unknown`` is not a valid reading.
    """

    read_at: datetime
    status: str | None = None

    @property
    def valid(self) -> bool:
        return self.status in BALANCE_STATUSES_KNOWN

    def to_json(self) -> dict[str, Any]:
        return {"read_at": _iso(self.read_at), "status": self.status}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Reading:
        read_at = _dt(raw["read_at"])
        assert read_at is not None
        status = raw.get("status")
        return cls(read_at, None if status is None else str(status))


def cadence_class(
    readings: Sequence[Reading],
    trend: str | None,
    today: date,
    settings: FullChargeSettings,
) -> str:
    """The cadence class of the latest valid readings, worst first (see the module docstring)."""
    valid = [r for r in readings if r.valid]
    if not valid:
        return "unknown"
    recent = [r.status for r in valid[-settings.alert_consecutive :]]
    if len(recent) >= settings.alert_consecutive and all(s in ("orange", "red") for s in recent):
        return "alert"
    if valid[-1].status != "green":  # yellow, or one orange or red after a better reading
        return "yellow"
    last = valid[-settings.min_comparable_readings :]
    age_days = (today - valid[-1].read_at.date()).days  # UTC date: a day's slack is enough here
    if (
        len(last) >= settings.min_comparable_readings
        and all(r.status == "green" for r in last)
        and trend in _TRENDS
        and trend != _RISING
        and age_days <= settings.cadence.green.max_days
    ):
        return "green"
    return "unknown"


@dataclass(frozen=True)
class PlanSlot:
    """What the arming rule needs of one planned slot."""

    start: datetime
    charging: bool
    soc_end_frac: float


def arm_due(
    now: datetime,
    day_start: datetime,
    slots: Sequence[PlanSlot],
    *,
    interval_seconds: int,
    arm_lead_slots: int,
    arm_before_soc_frac: float,
) -> bool:
    """Whether the committed plan says to arm Omnibattery now (plan, section 6).

    The trigger slot is the first slot of the run's day in which the plan charges or ends above
    ``arm_before_soc_frac`` (the earlier of the two); arming is due ``arm_lead_slots`` before it.
    Never before the day starts: the actuator writes the weekday of the local day it is on, so the
    arm waits for local midnight (the plan allowed one slot earlier; the merged actuator refuses
    ``not_today`` before it).  No trigger in the plan: not due.
    """
    for slot in sorted(slots, key=lambda s: s.start):
        if slot.start < day_start:
            continue
        if slot.charging or slot.soc_end_frac > arm_before_soc_frac:
            due = slot.start - timedelta(seconds=arm_lead_slots * interval_seconds)
            return now >= max(due, day_start)
    return False


def day_window(
    slot_starts: Sequence[datetime], day: date, tz: tzinfo, margin_slots: int
) -> tuple[int, int] | None:
    """``(window_start, checkpoint_end)`` of *day* in a snapshot's slots, or ``None``.

    ``window_start`` is the first slot of *day* in the snapshot (0 when the snapshot starts inside
    it); the checkpoint may be any slot ending at least ``margin_slots`` before local midnight, so
    ``checkpoint_end`` (exclusive) is the day's last slot index plus one minus the margin.
    """
    indices = [i for i, s in enumerate(slot_starts) if s.astimezone(tz).date() == day]
    if not indices:
        return None
    start, end = indices[0], indices[-1] + 1 - margin_slots
    return (start, end) if end > start else None


@dataclass
class Run:
    """The run in progress: a calendar decision plus what was seen of it (never a plan)."""

    day: date
    decided_at: datetime
    days_since: int | None
    forced: bool
    overdue: bool
    forcing_cost_eur: float | None
    threshold_eur: float | None
    cadence_class: str
    plan_id: str | None = None
    read_at_armed: datetime | None = None
    armed_at: datetime | None = None
    driven_at: datetime | None = None
    status_seen: bool = False
    soc_peak_pct: float | None = None
    reached_at: datetime | None = None
    completed_at: datetime | None = None
    completion_reason: str | None = None
    settle_until: datetime | None = None

    @property
    def weekday(self) -> str:
        return WEEKDAY_OPTIONS[self.day.weekday()]

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "weekday": self.weekday,
            "decided_at": _iso(self.decided_at),
            "days_since": self.days_since,
            "forced": self.forced,
            "overdue": self.overdue,
            "forcing_cost_eur": self.forcing_cost_eur,
            "threshold_eur": None
            if self.threshold_eur is None or math.isinf(self.threshold_eur)
            else self.threshold_eur,
            "cadence_class": self.cadence_class,
            "plan_id": self.plan_id,
            "read_at_armed": _iso(self.read_at_armed),
            "armed_at": _iso(self.armed_at),
            "driven_at": _iso(self.driven_at),
            "status_seen": self.status_seen,
            "soc_peak_pct": self.soc_peak_pct,
            "reached_at": _iso(self.reached_at),
            "completed_at": _iso(self.completed_at),
            "completion_reason": self.completion_reason,
            "settle_until": _iso(self.settle_until),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Run:
        day, decided = _day(raw["day"]), _dt(raw["decided_at"])
        assert day is not None and decided is not None
        return cls(
            day=day,
            decided_at=decided,
            days_since=raw.get("days_since"),
            forced=bool(raw.get("forced")),
            overdue=bool(raw.get("overdue")),
            forcing_cost_eur=raw.get("forcing_cost_eur"),
            threshold_eur=raw.get("threshold_eur"),
            cadence_class=str(raw.get("cadence_class", "unknown")),
            plan_id=raw.get("plan_id"),
            read_at_armed=_dt(raw.get("read_at_armed")),
            armed_at=_dt(raw.get("armed_at")),
            # The acknowledgement clock restarts after a restart: it counts from the next drive.
            driven_at=None,
            status_seen=bool(raw.get("status_seen")),
            soc_peak_pct=raw.get("soc_peak_pct"),
            reached_at=_dt(raw.get("reached_at")),
            completed_at=_dt(raw.get("completed_at")),
            completion_reason=raw.get("completion_reason"),
            settle_until=_dt(raw.get("settle_until")),
        )


@dataclass
class FullChargeState:
    """The persisted knowledge: ``L``, readings, failures and the run (no plan, no command)."""

    writer_mode: str  # "live" or "simulated"
    enabled_at: datetime
    state: str = "idle"
    last_full_at: datetime | None = None
    last_full_local_date: date | None = None
    #: ``balance_reading``, ``simulated`` or ``bootstrap``.
    last_full_source: str | None = None
    readings: list[Reading] = field(default_factory=list)
    trend: str | None = None
    run: Run | None = None
    #: The local day of the last attempt: one attempt per day, a failed day is not retried at once.
    last_attempt_day: date | None = None
    #: The day an evaluation was obtained for (accepted or declined): a decision is sticky.
    evaluated_day: date | None = None
    consecutive_failures: int = 0
    paused_until: datetime | None = None
    last_decision: dict[str, Any] | None = None
    last_outcome: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "writer_mode": self.writer_mode,
            "enabled_at": _iso(self.enabled_at),
            "state": self.state,
            "last_full_at": _iso(self.last_full_at),
            "last_full_local_date": None
            if self.last_full_local_date is None
            else self.last_full_local_date.isoformat(),
            "last_full_source": self.last_full_source,
            "readings": [r.to_json() for r in self.readings],
            "trend": self.trend,
            "run": self.run.to_json() if self.run is not None else None,
            "last_attempt_day": None
            if self.last_attempt_day is None
            else self.last_attempt_day.isoformat(),
            "evaluated_day": None if self.evaluated_day is None else self.evaluated_day.isoformat(),
            "consecutive_failures": self.consecutive_failures,
            "paused_until": _iso(self.paused_until),
            "last_decision": self.last_decision,
            "last_outcome": self.last_outcome,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> FullChargeState:
        """Parse a state file; raises ``ValueError``/``KeyError``/``TypeError`` if corrupt."""
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported full-charge state schema {raw.get('schema_version')!r}")
        enabled_at = _dt(raw["enabled_at"])
        assert enabled_at is not None
        writer_mode = str(raw["writer_mode"])
        if writer_mode not in ("live", "simulated"):
            raise ValueError(f"bad writer_mode {writer_mode!r}")
        state = str(raw["state"])
        if state not in STATES:
            raise ValueError(f"bad state {state!r}")
        run = Run.from_json(raw["run"]) if raw.get("run") else None
        if (state == "idle") == (run is not None):
            raise ValueError("state and run disagree")
        return cls(
            writer_mode=writer_mode,
            enabled_at=enabled_at,
            state=state,
            last_full_at=_dt(raw.get("last_full_at")),
            last_full_local_date=_day(raw.get("last_full_local_date")),
            last_full_source=raw.get("last_full_source"),
            readings=[Reading.from_json(r) for r in raw.get("readings") or []],
            trend=raw.get("trend"),
            run=run,
            last_attempt_day=_day(raw.get("last_attempt_day")),
            evaluated_day=_day(raw.get("evaluated_day")),
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            paused_until=_dt(raw.get("paused_until")),
            last_decision=raw.get("last_decision"),
            last_outcome=raw.get("last_outcome"),
        )


@dataclass(frozen=True)
class Observation:
    """Everything one step reads (the runtime builds it from the entity store and the actuator)."""

    now: datetime
    #: Battery SOC in percent (``None``: missing, stale or the battery is unresponsive).
    soc_pct: float | None = None
    #: Omnibattery's ``integration_status`` text (``None``: missing or unavailable).
    integration_status: str | None = None
    #: ``last_balance_read`` (UTC) and the status and trend text; ``None``: missing or unknown.
    read_at: datetime | None = None
    balance_status: str | None = None
    delta_trend: str | None = None
    #: The actuator has reconciled the armed directive (live: confirmed by readback).
    driven_armed: bool | None = None
    #: ``Actuator.full_charge_refusal`` while an armed directive is refused.
    refusal: str | None = None
    tomorrow_published: bool = False
    #: :func:`arm_due` for the run's day, computed by the caller from the committed plan.
    arm_due: bool = False
    #: Read-only evidence recorded next to a transition or outcome.
    evidence: Mapping[str, Any] = field(default_factory=dict)


class FullChargeScheduler:
    """The decision rule, the cadence and the run state machine (see the module docstring)."""

    def __init__(
        self,
        settings: FullChargeSettings,
        tz: tzinfo,
        *,
        writer_mode: str,
        now: datetime,
        state: FullChargeState | None = None,
        earliest_reading: datetime | None = None,
    ) -> None:
        if writer_mode not in ("live", "simulated"):
            raise ValueError("writer_mode must be 'live' or 'simulated'")
        self.settings = settings
        self._tz = tz
        self.writer_mode = writer_mode
        self._earliest = earliest_reading
        self._events: list[dict[str, Any]] = []
        self.dirty = False
        #: Why the feature is suspended (no decision, never armed); ``None``: running.
        self.suspended: str | None = None
        self.last_directive = FullChargeDirective(False)
        self.reset_reason: str | None = None
        if state is not None and state.writer_mode != writer_mode:
            # Simulated and real knowledge never mix: the first live start bootstraps the real L
            # (a reading after commissioning is honoured again below), and vice versa.
            self.reset_reason = f"writer_mode {state.writer_mode} -> {writer_mode}"
            state = None
        if state is None:
            state = FullChargeState(writer_mode=writer_mode, enabled_at=now)
            if settings.bootstrap_last_full is not None:
                state.last_full_local_date = settings.bootstrap_last_full
                state.last_full_source = "bootstrap"
            self.dirty = True
            self._emit(
                now,
                "bootstrap",
                writer_mode=writer_mode,
                bootstrap_last_full=None
                if settings.bootstrap_last_full is None
                else settings.bootstrap_last_full.isoformat(),
                reset_reason=self.reset_reason,
            )
        self.state = state

    # ------------------------------------------------------------------ small helpers

    def local_date(self, at: datetime) -> date:
        return at.astimezone(self._tz).date()

    def _emit(self, at: datetime, event: str, **fields: Any) -> None:
        self._events.append({"kind": "full_charge", "event": event, "at": _iso(at), **fields})

    def drain_events(self) -> list[dict[str, Any]]:
        out, self._events = self._events, []
        return out

    def _transition(self, to: str, now: datetime, reason: str, obs: Observation | None) -> None:
        st = self.state
        self._emit(
            now,
            "transition",
            from_state=st.state,
            to_state=to,
            reason=reason,
            day=None if st.run is None else st.run.day.isoformat(),
            evidence=dict(obs.evidence) if obs is not None else {},
        )
        st.state = to
        self.dirty = True

    def suspend(self, reason: str | None) -> None:
        self.suspended = reason

    # ------------------------------------------------------------------ cadence

    def cadence(self, now: datetime) -> tuple[str, CadenceRow]:
        today = self.local_date(now)
        cls = cadence_class(self.state.readings, self.state.trend, today, self.settings)
        return cls, self.settings.cadence.row(cls)

    def days_since(self, day: date, now: datetime) -> int:
        """``d``: days from ``L`` to *day*; without ``L`` see the module docstring (bootstrap)."""
        st = self.state
        if st.last_full_local_date is not None:
            return (day - st.last_full_local_date).days
        enabled = (day - self.local_date(st.enabled_at)).days
        return max(enabled, self.settings.cadence.unknown.min_days)

    def next_eligible_day(self, now: datetime) -> date:
        """The first local day the rule may pick, and the day it is forced (see ``/status``)."""
        _, row = self.cadence(now)
        st = self.state
        if st.last_full_local_date is None:
            return self.local_date(st.enabled_at)
        return st.last_full_local_date + timedelta(days=row.min_days)

    def forced_after_day(self, now: datetime) -> date:
        _, row = self.cadence(now)
        st = self.state
        base = st.last_full_local_date or self.local_date(st.enabled_at)
        return base + timedelta(days=row.max_days)

    def paused(self, now: datetime) -> bool:
        return self.state.paused_until is not None and now < self.state.paused_until

    # ------------------------------------------------------------------ readings

    def _observe_readings(self, obs: Observation) -> None:
        st, now = self.state, obs.now
        if obs.delta_trend is not None and obs.delta_trend != st.trend:
            st.trend = obs.delta_trend
            self.dirty = True
        read_at = obs.read_at
        if read_at is None or (self._earliest is not None and read_at < self._earliest):
            return
        status = obs.balance_status if obs.balance_status in BALANCE_STATUSES_KNOWN else None
        if not st.readings or read_at > st.readings[-1].read_at:
            st.readings.append(Reading(read_at, status))
            del st.readings[: -max(_READINGS_MIN_MEMORY, self.settings.min_comparable_readings + 4)]
            self.dirty = True
            self._emit(now, "reading", read_at=_iso(read_at), status=obs.balance_status)
        elif read_at == st.readings[-1].read_at and st.readings[-1].status != status:
            if status is not None or st.readings[-1].status is None:
                st.readings[-1].status = status
                self.dirty = True
        latest = st.readings[-1]
        newer = (st.last_full_at is None or latest.read_at > st.last_full_at) and (
            st.last_full_local_date is None
            or self.local_date(latest.read_at) >= st.last_full_local_date
        )
        if latest.valid and newer:
            # A new valid reading is a measured full charge, whoever caused it.
            st.last_full_at = latest.read_at
            st.last_full_local_date = self.local_date(latest.read_at)
            st.last_full_source = "balance_reading"
            self.dirty = True
            if st.consecutive_failures or st.paused_until is not None:
                st.consecutive_failures, st.paused_until = 0, None
            self._emit(
                now,
                "last_full",
                last_full_local_date=st.last_full_local_date.isoformat(),
                status=latest.status,
                source="balance_reading",
            )

    def _new_valid_reading(self, run: Run) -> Reading | None:
        for reading in reversed(self.state.readings):
            if run.read_at_armed is not None and reading.read_at <= run.read_at_armed:
                return None
            if reading.valid:
                return reading
        return None

    # ------------------------------------------------------------------ the step

    def step(self, obs: Observation) -> FullChargeDirective:
        """Advance the machine one observation and return the directive for the actuator."""
        if self.suspended is not None:
            self.last_directive = FullChargeDirective(False)
            return self.last_directive
        self._observe_readings(obs)
        st = self.state
        today = self.local_date(obs.now)
        run = st.run
        if run is not None:
            if st.state in ("armed", "charging", "reached") and obs.soc_pct is not None:
                run.soc_peak_pct = max(run.soc_peak_pct or 0.0, obs.soc_pct)
            handler = {
                "scheduled": self._step_scheduled,
                "armed": self._step_armed,
                "charging": self._step_charging,
                "reached": self._step_reached,
                "settling": self._step_settling,
            }[st.state]
            handler(run, obs, today)
        if st.state == "idle":  # also right after a run ended: a new day is decided at once
            self._step_idle(obs, today)
        self.last_directive = self._directive()
        return self.last_directive

    def _directive(self) -> FullChargeDirective:
        st = self.state
        if st.run is None or st.state not in ARMED_STATES:
            return FullChargeDirective(False)
        settle = st.run.settle_until if st.state == "settling" else None
        return FullChargeDirective(True, st.run.weekday, settle)

    # -- idle: the rule ------------------------------------------------------------------------

    def _may_try(self, day: date, now: datetime) -> bool:
        st = self.state
        return not self.paused(now) and (st.last_attempt_day is None or day > st.last_attempt_day)

    def evaluation_wanted(self, now: datetime, tomorrow_published: bool) -> date | None:
        """The day the planner should price, or ``None`` (nothing to price right now)."""
        st = self.state
        if self.suspended is not None or st.state != "idle" or not tomorrow_published:
            return None
        day = self.local_date(now) + timedelta(days=1)
        if st.evaluated_day == day or not self._may_try(day, now):
            return None
        _, row = self.cadence(now)
        d = self.days_since(day, now)
        return day if row.min_days <= d < row.max_days else None

    def _step_idle(self, obs: Observation, today: date) -> None:
        st, now = self.state, obs.now
        _, row = self.cadence(now)
        if self._may_try(today, now) and self.days_since(today, now) >= row.max_days:
            # Overdue fallback: no decision was reached yesterday evening, the bound is reached.
            self._decide(today, obs, forced=True, overdue=True)
            return
        tomorrow = today + timedelta(days=1)
        if (
            obs.tomorrow_published
            and st.evaluated_day != tomorrow
            and self._may_try(tomorrow, now)
            and self.days_since(tomorrow, now) >= row.max_days
        ):
            self._decide(tomorrow, obs, forced=True, overdue=False)

    def record_evaluation(self, day: date, evaluation: Mapping[str, Any], now: datetime) -> bool:
        """Apply the planner's evaluation of *day*: choose it or decline it; sticky either way.

        ``evaluation`` holds ``forcing_cost_eur``, ``vs_normal_eur``, ``shortfall_kwh`` and the
        record fields of :class:`~.planner.FullChargeEvaluation`.  Returns whether *day* was
        chosen.  A day whose target missed by more than :data:`SHORTFALL_TOLERANCE_KWH` is declined.
        """
        st = self.state
        if self.suspended is not None or st.state != "idle":
            return False
        cls, row = self.cadence(now)
        d = self.days_since(day, now)
        limit = threshold_eur(
            d,
            row.min_days,
            row.max_days,
            self.settings.threshold_min_eur,
            self.settings.threshold_scale_eur,
        )
        forcing = evaluation.get("forcing_cost_eur")
        shortfall = float(evaluation.get("shortfall_kwh") or 0.0)
        total = None if forcing is None else float(forcing) + self.settings.fixed_cost_eur
        accepted = total is not None and total <= limit and shortfall <= SHORTFALL_TOLERANCE_KWH
        st.evaluated_day = day
        self.dirty = True
        decision = {
            "day": day.isoformat(),
            "d": d,
            "cadence_class": cls,
            "min_days": row.min_days,
            "max_days": row.max_days,
            "forced": False,
            "overdue": False,
            "accepted": accepted,
            "forcing_cost_eur": forcing,
            "vs_normal_eur": evaluation.get("vs_normal_eur"),
            "fixed_cost_eur": self.settings.fixed_cost_eur,
            "threshold_eur": limit if math.isfinite(limit) else None,
            "shortfall_kwh": shortfall,
            "checkpoint_slot": evaluation.get("checkpoint_slot"),
            "solver": evaluation.get("solver"),
            "normal_cost_eur": evaluation.get("normal_cost_eur"),
            "lifted_cost_eur": evaluation.get("lifted_cost_eur"),
            "target_cost_eur": evaluation.get("target_cost_eur"),
        }
        st.last_decision = {**decision, "at": _iso(now)}
        self._emit(now, "decision", **decision)
        if accepted:
            self._schedule(
                day, now, cls, d, forced=False, overdue=False, forcing=float(forcing), limit=limit
            )
        return accepted

    def _decide(self, day: date, obs: Observation, *, forced: bool, overdue: bool) -> None:
        st, now = self.state, obs.now
        cls, row = self.cadence(now)
        d = self.days_since(day, now)
        st.evaluated_day = day
        decision = {
            "day": day.isoformat(),
            "d": d,
            "cadence_class": cls,
            "min_days": row.min_days,
            "max_days": row.max_days,
            "forced": forced,
            "overdue": overdue,
            "accepted": True,
        }
        st.last_decision = {**decision, "at": _iso(now)}
        self._emit(now, "decision", **decision)
        self._schedule(day, now, cls, d, forced=forced, overdue=overdue, forcing=None, limit=None)

    def _schedule(
        self,
        day: date,
        now: datetime,
        cls: str,
        d: int,
        *,
        forced: bool,
        overdue: bool,
        forcing: float | None,
        limit: float | None,
    ) -> None:
        st = self.state
        read_at = st.readings[-1].read_at if st.readings else None
        st.run = Run(
            day=day,
            decided_at=now,
            days_since=d,
            forced=forced,
            overdue=overdue,
            forcing_cost_eur=forcing,
            threshold_eur=limit,
            cadence_class=cls,
            read_at_armed=read_at,
        )
        self._transition("scheduled", now, "forced" if forced else "threshold", None)

    def note_plan(self, plan_id: str) -> None:
        run = self.state.run
        if run is not None and run.plan_id != plan_id:
            run.plan_id = plan_id
            self.dirty = True

    # -- the run -------------------------------------------------------------------------------

    def _finish(
        self,
        outcome: str,
        now: datetime,
        reason: str,
        obs: Observation | None,
        *,
        today: date | None = None,
    ) -> None:
        """End the run: record the outcome, count a failure, return to ``idle``."""
        st = self.state
        run = st.run
        assert run is not None and outcome in OUTCOMES
        failed = outcome.startswith("failed_")
        summary = {
            "outcome": outcome,
            "reason": reason,
            "day": run.day.isoformat(),
            "at": _iso(now),
            "soc_peak_pct": run.soc_peak_pct,
            "completion_reason": run.completion_reason,
            "forced": run.forced,
        }
        self._emit(
            now,
            "outcome",
            **{k: v for k, v in summary.items() if k != "at"},
            run=run.to_json(),
            consecutive_failures=st.consecutive_failures + (1 if failed else 0),
            evidence=dict(obs.evidence) if obs is not None else {},
        )
        st.last_outcome = summary
        st.last_attempt_day = run.day
        if failed:
            st.consecutive_failures += 1
            if st.consecutive_failures >= self.settings.max_consecutive_failures:
                st.paused_until = now + timedelta(days=self.settings.failure_pause_days)
                st.consecutive_failures = 0
                self._emit(now, "paused", paused_until=_iso(st.paused_until))
        self._transition("idle", now, outcome, obs)
        st.run = None

    def _step_scheduled(self, run: Run, obs: Observation, today: date) -> None:
        now = obs.now
        if today > run.day:
            self._finish("failed_never_armed", now, "day_over", obs)
        elif today == run.day and obs.arm_due:
            read_at = self.state.readings[-1].read_at if self.state.readings else None
            run.read_at_armed = read_at
            run.armed_at = now
            self._transition("armed", now, "arm_due", obs)

    def _refused(self, run: Run, obs: Observation) -> bool:
        """A refused arming ends the run: ``cancelled``, or a failed attempt for a failed write."""
        refusal = obs.refusal
        if refusal is None or (refusal == "kill_switch_off" and self.writer_mode == "simulated"):
            return False  # only a live run needs the owner's kill switch
        if refusal == "write_failed":
            self._finish("failed_unacknowledged", obs.now, "write_failed", obs)
        else:
            self._finish("cancelled", obs.now, f"refused:{refusal}", obs)
        return True

    def _step_armed(self, run: Run, obs: Observation, today: date) -> None:
        now = obs.now
        if self._refused(run, obs):
            return
        if self.writer_mode == "simulated":
            if today > run.day:
                self._finish_simulated(run, now, obs)
            return
        if obs.driven_armed and run.driven_at is None:
            run.driven_at = now
            self.dirty = True
        if obs.integration_status == WEEKLY_FULL_CHARGE_STATUS:
            run.status_seen = True
            self._transition("charging", now, "status_seen", obs)
            return
        if today > run.day:
            outcome = "failed_unacknowledged" if run.driven_at else "failed_never_armed"
            self._finish(outcome, now, "day_over", obs)
        elif (
            run.driven_at is not None
            and (now - run.driven_at).total_seconds() > self.settings.ack_timeout_s
        ):
            self._finish("failed_unacknowledged", now, "ack_timeout", obs)

    def _finish_simulated(self, run: Run, now: datetime, obs: Observation) -> None:
        st = self.state
        st.last_full_at = now
        st.last_full_local_date = run.day
        st.last_full_source = "simulated"
        self._finish("done_simulated", now, "day_over", obs)

    def _completed(self, run: Run, now: datetime, reason: str, obs: Observation) -> None:
        run.completed_at = now
        run.completion_reason = reason
        run.settle_until = now + timedelta(seconds=self.settings.settle_timeout_s)
        self._transition("settling", now, reason, obs)

    def _left_status(self, obs: Observation) -> bool:
        status = obs.integration_status
        return status is not None and status != WEEKLY_FULL_CHARGE_STATUS

    def _step_charging(self, run: Run, obs: Observation, today: date) -> None:
        now, peak = obs.now, run.soc_peak_pct or 0.0
        if self._refused(run, obs):
            return
        if obs.soc_pct is not None and obs.soc_pct >= 100.0:
            self._completed(run, now, "soc_100", obs)
        elif self._left_status(obs):
            if peak >= _COMPLETED_PEAK_PCT:
                self._completed(run, now, "status_left", obs)
            else:
                self._finish("failed_never_reached", now, "aborted", obs)
        elif obs.soc_pct is not None and obs.soc_pct >= self.settings.reached_soc_pct:
            run.reached_at = now
            self._transition("reached", now, "soc_reached", obs)
        elif today > run.day:
            self._finish("failed_never_reached", now, "day_over", obs)

    def _step_reached(self, run: Run, obs: Observation, today: date) -> None:
        now = obs.now
        if self._refused(run, obs):
            return
        assert run.reached_at is not None
        if obs.soc_pct is not None and obs.soc_pct >= 100.0:
            self._completed(run, now, "soc_100", obs)
        elif self._left_status(obs):
            self._completed(run, now, "status_left", obs)
        elif today > run.day:
            self._end_without_hold(run, now, obs)
        elif (now - run.reached_at).total_seconds() >= self.settings.top_hold_timeout_min * 60.0:
            self._completed(run, now, "top_hold_timeout", obs)

    def _end_without_hold(self, run: Run, now: datetime, obs: Observation) -> None:
        """The day ended before a settle hold could run: a reading that arrived still counts."""
        if self._new_valid_reading(run) is not None:
            self._finish("done", now, "day_over_with_reading", obs)
        else:
            self._finish("failed_reached_unmeasured", now, "day_over", obs)

    def _step_settling(self, run: Run, obs: Observation, today: date) -> None:
        now = obs.now
        assert run.settle_until is not None
        reading = self._new_valid_reading(run)
        if reading is not None:
            self._finish("done", now, "balance_reading", obs)
        elif now >= run.settle_until:
            self._finish("failed_reached_unmeasured", now, "settle_timeout", obs)
        elif today > run.day:
            self._finish("failed_reached_unmeasured", now, "day_over", obs)

    # ------------------------------------------------------------------ views

    def alerts(self, now: datetime) -> set[str]:
        """Scheduler-owned alerts (the runtime adds the ones it owns)."""
        st = self.state
        out: set[str] = set()
        if self.paused(now):
            out.add("paused")
        if self.cadence(now)[0] == "alert":
            out.add("balance_alert")
        if st.consecutive_failures and st.last_outcome is not None:
            out.update(
                {
                    "failed_never_reached": {"never_reached"},
                    "failed_never_armed": {"never_reached"},
                    "failed_unacknowledged": {"unacknowledged"},
                    "failed_reached_unmeasured": {"reached_unmeasured"},
                }.get(st.last_outcome["outcome"], set())
            )
        return out

    def view(self, now: datetime) -> dict[str, Any]:
        """The scheduler part of the ``/status`` ``full_charge`` block."""
        st = self.state
        cls, row = self.cadence(now)
        today = self.local_date(now)
        run = st.run
        return {
            "state": st.state,
            "suspended": self.suspended,
            "writer_mode": st.writer_mode,
            "enabled_at": _iso(st.enabled_at),
            "last_full_at": _iso(st.last_full_at),
            "last_full_local_date": None
            if st.last_full_local_date is None
            else st.last_full_local_date.isoformat(),
            "last_full_source": st.last_full_source,
            "days_since": None
            if st.last_full_local_date is None
            else (today - st.last_full_local_date).days,
            "cadence": {
                "class": cls,
                "min_days": row.min_days,
                "max_days": row.max_days,
                "trend": st.trend,
                "valid_readings": sum(1 for r in st.readings if r.valid),
            },
            "next_eligible_day": self.next_eligible_day(now).isoformat(),
            "forced_after_day": self.forced_after_day(now).isoformat(),
            "readings": [r.to_json() for r in st.readings],
            "run": None if run is None else run.to_json(),
            "last_decision": st.last_decision,
            "last_outcome": st.last_outcome,
            "consecutive_failures": st.consecutive_failures,
            "paused_until": _iso(st.paused_until),
            "alerts": sorted(self.alerts(now)),
        }
