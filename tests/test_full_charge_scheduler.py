"""The periodic full charge's pure scheduler: cadence, rule, run state machine, replay.

Plan: docs/planning/periodic-full-charge.md, including the 2026-11-04 amendment (A1 to A7).
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from battery_optimizer.service.actuator import FullChargeDirective
from battery_optimizer.service.config import CadenceRow, FullChargeCadence, FullChargeSettings
from battery_optimizer.service.full_charge import (
    ARMED_STATES,
    OUTCOMES,
    STATES,
    FullChargeScheduler,
    FullChargeState,
    Observation,
    PlanSlot,
    Reading,
    arm_due,
    cadence_class,
    day_window,
    threshold_eur,
)

TZ = ZoneInfo("Europe/Madrid")
S = FullChargeSettings(enabled=True)
WEEKLY = "weekly_full_charge"
D0 = date(2026, 11, 4)  # a Wednesday


def local(day: date, hh: int = 12, mm: int = 0, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=TZ).astimezone(UTC)


def make(
    now: datetime | None = None,
    *,
    mode: str = "live",
    settings: FullChargeSettings = S,
    state: FullChargeState | None = None,
    **kw,
) -> FullChargeScheduler:
    return FullChargeScheduler(
        settings, TZ, writer_mode=mode, now=now or local(D0), state=state, **kw
    )


def obs(now: datetime, **kw) -> Observation:
    return Observation(now=now, **kw)


def read(day: date, hh: int = 3, mm: int = 0, status: str | None = "green") -> Reading:
    return Reading(local(day, hh, mm), status)


def events(sched: FullChargeScheduler, name: str | None = None) -> list[dict]:
    out = sched.drain_events()
    return [e for e in out if name is None or e["event"] == name]


# ---------------------------------------------------------------------------
# Threshold function
# ---------------------------------------------------------------------------


def test_the_threshold_equals_the_minimum_at_min_days_and_diverges_at_max_days():
    base, scale = 0.01, 0.01
    values = [threshold_eur(d, 10, 14, base, scale) for d in range(10, 14)]
    assert values[0] == pytest.approx(base)
    assert all(a < b for a, b in zip(values, values[1:], strict=False))  # monotone
    assert values[-1] == pytest.approx(0.01 + 0.01 * 3 / 1)
    assert threshold_eur(14, 10, 14, base, scale) == math.inf  # the forced case
    assert threshold_eur(30, 10, 14, base, scale) == math.inf
    assert threshold_eur(9, 10, 14, base, scale) == -math.inf  # never before min


@pytest.mark.parametrize(("lo", "hi"), [(5, 7), (7, 14), (10, 14), (14, 28)])
def test_the_threshold_rises_towards_every_rows_max(lo, hi):
    values = [threshold_eur(d, lo, hi, 0.01, 0.01) for d in range(lo, hi)]
    assert values[0] == pytest.approx(0.01)
    assert values == sorted(values) and len(set(values)) == len(values)
    assert values[-1] > values[0] or hi - lo == 1


# ---------------------------------------------------------------------------
# Cadence class: the amendment's table (A3)
# ---------------------------------------------------------------------------

TODAY = D0


@pytest.mark.parametrize(
    ("statuses", "trend", "expected"),
    [
        ([], "stable", "unknown"),
        ([None], "stable", "unknown"),  # a reading without a valid status is not comparable
        (["unknown"], "stable", "unknown"),
        (["green"] * 3, "stable", "unknown"),  # fewer than min_comparable_readings
        (["green"] * 4, "stable", "green"),
        (["green"] * 6, "falling", "green"),
        (["green"] * 4, "rising", "unknown"),  # a rising trend blocks the long cadence
        (["green"] * 4, None, "unknown"),  # an unknown trend never selects green
        (["green"] * 4, "bogus", "unknown"),
        (["yellow", "green", "green", "green", "green"], "stable", "green"),  # last four count
        (["green", "green", "yellow", "green", "green"], "stable", "unknown"),
        (["yellow"], "stable", "yellow"),
        (["green", "green", "green", "green", "yellow"], "stable", "yellow"),
        (["green", "orange"], "stable", "yellow"),  # a single orange after a better reading
        (["green", "red"], "stable", "yellow"),
        (["green", "orange", "red"], "stable", "alert"),  # two in a row
        (["orange", "orange"], "stable", "alert"),
        (["red", "red"], "rising", "alert"),
        (["orange", "green", "orange"], "stable", "yellow"),  # not in a row
        (["orange", "red", "green"], "stable", "unknown"),  # recovered: latest green, last 4 mixed
    ],
)
def test_the_cadence_class_follows_the_latest_valid_readings(statuses, trend, expected):
    readings = [
        read(D0 - timedelta(days=len(statuses) - i), status=s) for i, s in enumerate(statuses)
    ]
    assert cadence_class(readings, trend, TODAY, S) == expected


def test_unknown_readings_between_valid_ones_are_skipped():
    readings = [
        read(D0 - timedelta(days=9 - i), status=s)
        for i, s in enumerate(["orange", None, "red", "unknown"])
    ]
    assert cadence_class(readings, "stable", TODAY, S) == "alert"


def test_a_stale_green_reading_never_selects_the_long_cadence():
    fresh = [read(D0 - timedelta(days=28 + 7 * (3 - i))) for i in range(4)]  # the latest: 28 days
    assert cadence_class(fresh, "stable", D0, S) == "green"
    assert cadence_class(fresh, "stable", D0 + timedelta(days=1), S) == "unknown"  # 29 days old


def test_the_thresholds_of_the_class_come_from_the_configured_table():
    custom = replace(
        S,
        cadence=FullChargeCadence(unknown=CadenceRow(9, 12)),
        min_comparable_readings=2,
        alert_consecutive=3,
    )
    assert cadence_class([read(D0, status="green")] * 2, "stable", D0, custom) == "green"
    two = [read(D0, status="red"), read(D0, 4, status="red")]
    assert cadence_class(two, "stable", D0, custom) == "yellow"  # needs three in a row here
    assert cadence_class([*two, read(D0, 5, status="red")], "stable", D0, custom) == "alert"
    assert make(settings=custom).cadence(local(D0))[1] == CadenceRow(9, 12)


# ---------------------------------------------------------------------------
# The rule: d against the active row; bootstrap; overdue fallback
# ---------------------------------------------------------------------------


def with_last_full(last: date, *, status: str | None = "green", now: datetime | None = None):
    sched = make(now or local(D0), settings=replace(S, bootstrap_last_full=last))
    if status is not None:
        sched.state.readings = [Reading(local(last, 3), status)]
    sched.drain_events()
    return sched


@pytest.mark.parametrize("days_ago", range(0, 21))
def test_the_rule_for_d_zero_to_twenty_in_the_unknown_row(days_ago):
    """Evening of D0 - 1; the candidate day is D0, so d = days_ago (row 10 / 14)."""
    sched = with_last_full(
        D0 - timedelta(days=days_ago), status=None, now=local(D0 - timedelta(days=1), 21)
    )
    evening = local(D0 - timedelta(days=1), 21)
    wanted = sched.evaluation_wanted(evening, True)
    sched.step(obs(evening, tomorrow_published=True))
    if days_ago < 10:
        assert wanted is None and sched.state.state == "idle"  # below min: nothing, no solve
    elif days_ago < 14:
        assert wanted == D0 and sched.state.state == "idle"  # the threshold test needs a solve
    else:
        assert wanted is None and sched.state.state == "scheduled"  # forced, no solve
        assert sched.state.run.forced and sched.state.run.day in (D0 - timedelta(days=1), D0)


@pytest.mark.parametrize(
    ("status", "lo", "hi"),
    [("yellow", 7, 14), ("orange", 7, 14), ("red", 7, 14), (None, 10, 14)],
)
def test_the_bounds_of_each_row_gate_the_evaluation(status, lo, hi):
    for d in (lo - 1, lo, hi - 1, hi):
        evening = local(D0 - timedelta(days=1), 21)
        sched = with_last_full(D0 - timedelta(days=d), status=status, now=evening)
        wanted = sched.evaluation_wanted(evening, True)
        if d < lo:
            assert wanted is None
        elif d < hi:
            assert wanted == D0
        else:
            assert wanted is None  # forced instead of priced


def test_green_and_alert_rows():
    evening = local(D0 - timedelta(days=1), 21)
    green = with_last_full(D0 - timedelta(days=13), now=evening)
    green.state.readings = [read(D0 - timedelta(days=13 + 7 * i)) for i in range(4)][::-1]
    green.state.trend = "stable"
    assert green.cadence(evening)[0] == "green" and green.evaluation_wanted(evening, True) is None
    green.state.last_full_local_date = D0 - timedelta(days=14)
    assert green.evaluation_wanted(evening, True) == D0
    green.state.last_full_local_date = D0 - timedelta(days=28)
    green.step(obs(evening, tomorrow_published=True))
    assert green.state.state == "scheduled" and green.state.run.forced

    alert = with_last_full(D0 - timedelta(days=5), now=evening)
    alert.state.readings = [
        read(D0 - timedelta(days=12), status="orange"),
        read(D0 - timedelta(days=5), status="red"),
    ]
    assert alert.cadence(evening) == ("alert", CadenceRow(5, 7))
    assert alert.evaluation_wanted(evening, True) == D0
    assert "balance_alert" in alert.alerts(evening)
    alert.state.last_full_local_date = D0 - timedelta(days=7)
    alert.step(obs(evening, tomorrow_published=True))
    assert alert.state.run.forced


def test_a_single_orange_is_repeated_at_the_yellow_cadence_and_is_never_an_alert():
    evening = local(D0 - timedelta(days=1), 21)
    sched = with_last_full(D0 - timedelta(days=7), now=evening)
    sched.state.readings = [
        read(D0 - timedelta(days=14)),
        read(D0 - timedelta(days=7), status="orange"),
    ]
    assert sched.cadence(evening) == ("yellow", CadenceRow(7, 14))
    assert sched.alerts(evening) == set()
    assert sched.evaluation_wanted(evening, True) == D0  # repeat the measurement


def test_the_bootstrap_is_eligible_at_once_and_forced_max_days_after_enablement():
    start = local(D0, 12)
    sched = make(start)
    assert sched.state.last_full_local_date is None
    # Enabled today: tomorrow is eligible (d is the unknown row's min) and priced by the threshold.
    assert sched.days_since(D0 + timedelta(days=1), start) == 10
    assert sched.evaluation_wanted(local(D0, 21), True) == D0 + timedelta(days=1)
    assert sched.next_eligible_day(start) == D0
    assert sched.forced_after_day(start) == D0 + timedelta(days=14)
    # A declined day stays declined, the next evening prices the next day at the same d.
    sched.record_evaluation(
        D0 + timedelta(days=1), {"forcing_cost_eur": 5.0, "shortfall_kwh": 0}, local(D0, 21)
    )
    assert sched.evaluation_wanted(local(D0, 22), True) is None
    assert sched.evaluation_wanted(local(D0 + timedelta(days=1), 21), True) == D0 + timedelta(
        days=2
    )
    # Forced at max days from enablement (14 days for the unknown row).
    evening = local(D0 + timedelta(days=13), 21)
    sched.step(obs(evening, tomorrow_published=True))
    assert sched.state.state == "scheduled" and sched.state.run.forced
    assert sched.state.run.day == D0 + timedelta(days=14)


def test_bootstrap_last_full_is_an_explicit_override_and_an_older_reading_does_not_beat_it():
    sched = make(local(D0), settings=replace(S, bootstrap_last_full=date(2026, 11, 1)))
    assert sched.state.last_full_local_date == date(2026, 11, 1)
    assert sched.state.last_full_source == "bootstrap"
    sched.step(obs(local(D0), read_at=local(date(2026, 10, 30)), balance_status="green"))
    assert sched.state.last_full_local_date == date(2026, 11, 1)  # older than the override


def test_a_reading_from_before_commissioning_is_not_honoured():
    sched = make(local(D0), earliest_reading=local(date(2026, 11, 3), 20))
    sched.step(obs(local(D0), read_at=local(date(2026, 11, 2)), balance_status="green"))
    assert sched.state.readings == [] and sched.state.last_full_local_date is None
    sched.step(obs(local(D0), read_at=local(date(2026, 11, 3), 23), balance_status="green"))
    assert sched.state.last_full_local_date == date(2026, 11, 3)  # honoured as L, source reading
    assert sched.state.last_full_source == "balance_reading"


def test_the_overdue_fallback_decides_for_today_when_the_bound_is_reached():
    # Yesterday evening nothing was decided (service down); today d is already at max.
    morning = local(D0, 7)
    sched = with_last_full(D0 - timedelta(days=14), status=None, now=morning)
    sched.step(obs(morning))  # tomorrow is not published yet: only the overdue path can fire
    assert sched.state.state == "scheduled"
    assert sched.state.run.overdue and sched.state.run.forced and sched.state.run.day == D0
    decision = events(sched, "decision")[0]
    assert decision["overdue"] is True and decision["accepted"] is True


def test_one_attempt_per_day_and_a_failed_day_is_not_retried_at_once():
    morning = local(D0, 7)
    sched = with_last_full(D0 - timedelta(days=14), status=None, now=morning)
    sched.step(obs(morning))
    sched.step(obs(local(D0, 8)))  # scheduled; the day passes without arming
    sched.step(obs(local(D0 + timedelta(days=1), 0, 5)))  # D0 failed; the next day is overdue too
    assert sched.state.last_attempt_day == D0
    assert sched.state.state == "scheduled" and sched.state.run.day == D0 + timedelta(days=1)
    sched.step(obs(local(D0 + timedelta(days=2), 0, 5)))
    assert sched.state.last_attempt_day == D0 + timedelta(days=1)
    assert sched.state.consecutive_failures == 2


def test_a_decision_is_sticky_and_no_second_evaluation_is_wanted():
    evening = local(D0, 21)
    sched = with_last_full(D0 - timedelta(days=11), status=None, now=evening)
    assert sched.evaluation_wanted(evening, True) == D0 + timedelta(days=1)
    assert sched.record_evaluation(
        D0 + timedelta(days=1), {"forcing_cost_eur": 0.0, "shortfall_kwh": 0}, evening
    )
    assert sched.state.state == "scheduled"
    assert sched.evaluation_wanted(evening, True) is None
    assert (
        sched.record_evaluation(D0 + timedelta(days=1), {"forcing_cost_eur": 0.0}, evening) is False
    )


@pytest.mark.parametrize(
    ("forcing", "fixed", "shortfall", "accepted"),
    [
        (0.0, 0.0, 0.0, True),
        (0.0133, 0.0, 0.0, True),  # right at the threshold of d = 11 (0.01 + 0.01 / 3)
        (0.0134, 0.0, 0.0, False),
        (0.0, 0.02, 0.0, False),  # the fixed cost is added
        (-0.5, 0.0, 0.0, True),  # a negative forcing cost is fine
        (0.0, 0.0, 0.5, False),  # the target was missed: declined for the day
        (None, 0.0, 0.0, False),
    ],
)
def test_the_threshold_test_on_the_forcing_cost(forcing, fixed, shortfall, accepted):
    evening = local(D0, 21)
    sched = with_last_full(D0 - timedelta(days=10), status=None, now=evening)  # d(tomorrow) = 11
    sched.settings = replace(S, fixed_cost_eur=fixed)
    ev = {"forcing_cost_eur": forcing, "shortfall_kwh": shortfall, "vs_normal_eur": -0.1}
    assert sched.record_evaluation(D0 + timedelta(days=1), ev, evening) is accepted
    decision = events(sched, "decision")[0]
    assert decision["accepted"] is accepted and decision["d"] == 11
    assert decision["threshold_eur"] == pytest.approx(0.01 + 0.01 / 3)
    assert sched.state.evaluated_day == D0 + timedelta(days=1)


# ---------------------------------------------------------------------------
# arm_due and day_window
# ---------------------------------------------------------------------------

DAY = D0 + timedelta(days=1)
DAY_START = local(DAY, 0)


def slots(*specs):
    return [
        PlanSlot(DAY_START + timedelta(minutes=15 * i), c, soc) for i, (c, soc) in enumerate(specs)
    ]


def due(now, plan, **kw):
    args = dict(interval_seconds=900, arm_lead_slots=1, arm_before_soc_frac=0.93)
    return arm_due(now, DAY_START, plan, **{**args, **kw})


def test_arming_is_due_one_slot_before_the_first_charge_slot():
    plan = slots(*[(False, 0.5)] * 8, (True, 0.6), (True, 0.7))
    first = DAY_START + timedelta(minutes=15 * 8)
    assert not due(first - timedelta(minutes=16), plan)
    assert due(first - timedelta(minutes=15), plan)
    assert due(first + timedelta(hours=3), plan)


def test_arming_is_due_when_the_plan_would_exceed_the_soc_threshold_before_any_charge():
    plan = slots((False, 0.5), (False, 0.94), (True, 1.0))
    assert due(DAY_START, plan)  # the slot at index 1 trips it; lead puts it at the day's start


def test_arming_never_precedes_the_day_and_needs_a_trigger():
    plan = slots((True, 0.6))
    assert not due(DAY_START - timedelta(minutes=1), plan)  # waits for local midnight
    assert due(DAY_START, plan)
    assert not due(DAY_START + timedelta(hours=5), slots((False, 0.5), (False, 0.5)))
    assert not due(DAY_START + timedelta(hours=5), [])


def test_slots_before_the_day_are_ignored_and_the_earliest_trigger_wins():
    prior = [PlanSlot(DAY_START - timedelta(minutes=15), True, 0.7)]
    plan = [*prior, *slots((False, 0.5), (False, 0.5), (True, 0.8), (False, 0.97))]
    assert due(DAY_START + timedelta(minutes=15), plan) is True
    assert due(DAY_START + timedelta(minutes=10), plan) is False
    assert due(DAY_START + timedelta(minutes=15), plan, arm_lead_slots=0) is False
    assert due(DAY_START + timedelta(minutes=30), plan, arm_lead_slots=0) is True


def test_day_window_in_a_snapshot_and_across_dst():
    starts = [local(D0, 22) + timedelta(minutes=15 * i) for i in range(4 * 2 + 96)]
    window = day_window(starts, DAY, TZ, 2)
    assert window == (8, 8 + 96 - 2)  # the day's 96 slots minus the margin
    assert day_window(starts, DAY + timedelta(days=3), TZ, 2) is None
    inside = [local(DAY, 20) + timedelta(minutes=15 * i) for i in range(10)]
    assert day_window(inside, DAY, TZ, 2) == (0, 8)  # the snapshot starts inside the day
    assert day_window(inside[:2], DAY, TZ, 2) is None  # too little left for a checkpoint
    # 2026-10-25 has 25 hours (100 slots); 2026-03-29 has 23 (92 slots).
    fall = [local(date(2026, 10, 24), 23) + timedelta(minutes=15 * i) for i in range(4 + 100)]
    assert day_window(fall, date(2026, 10, 25), TZ, 2) == (4, 4 + 100 - 2)
    spring = [local(date(2026, 3, 28), 23) + timedelta(minutes=15 * i) for i in range(4 + 92)]
    assert day_window(spring, date(2026, 3, 29), TZ, 2) == (4, 4 + 92 - 2)


# ---------------------------------------------------------------------------
# The run state machine (live writer)
# ---------------------------------------------------------------------------


class Run:
    """Drives one scheduler through a scheduled day with a controllable plant."""

    def __init__(self, *, mode="live", settings=S, last=None, read_at=None, status="green"):
        self.day = D0 + timedelta(days=1)
        last = last or D0 - timedelta(days=10)
        self.sched = make(
            local(D0, 21), mode=mode, settings=replace(settings, bootstrap_last_full=last)
        )
        if read_at is not None:
            self.sched.state.readings = [Reading(read_at, status)]
        self.sched.drain_events()
        self.now = local(D0, 21)
        assert self.sched.record_evaluation(
            self.day, {"forcing_cost_eur": 0.0, "shortfall_kwh": 0.0}, self.now
        )
        self.sched.drain_events()
        self.read_at = read_at
        self.status = None
        self.soc = 50.0
        self.integration = "balanced"
        self.balance = "unknown" if read_at is None else status

    def step(self, seconds=30, **kw):
        self.now += timedelta(seconds=seconds)
        values = dict(
            soc_pct=self.soc,
            integration_status=self.integration,
            read_at=self.read_at,
            balance_status=self.balance,
            delta_trend="stable",
            driven_armed=kw.pop("driven", self.sched.state.state in ARMED_STATES),
        )
        values.update(kw)
        return self.sched.step(Observation(now=self.now, **values))

    def at(self, hh, mm=0):
        self.now = local(self.day, hh, mm) - timedelta(seconds=30)
        return self

    def arm(self):
        self.at(1, 45)
        self.step(arm_due=True)
        assert self.sched.state.state == "armed"
        return self

    def charge(self):
        self.arm()
        self.integration = WEEKLY
        self.step()
        assert self.sched.state.state == "charging"
        return self

    def reach(self):
        self.charge()
        self.soc = 99.0
        self.step()
        assert self.sched.state.state == "reached"
        return self

    def complete(self, reason="status_left"):
        self.reach()
        self.integration = "balanced"
        self.step()
        assert self.sched.state.state == "settling"
        return self

    @property
    def state(self):
        return self.sched.state

    def outcome(self):
        return events(self.sched, "outcome")[-1]


def test_a_full_run_counts_only_when_a_new_valid_reading_arrives():
    run = Run()
    run.complete()
    # The directive holds the battery idle for the settle window, switch still on.
    directive = run.sched.last_directive
    assert directive.armed and directive.weekday == "thursday"
    assert directive.settle_until == run.state.run.completed_at + timedelta(seconds=300)
    run.step(60)
    assert run.state.state == "settling"  # no reading yet
    run.read_at, run.balance = run.now, "green"
    directive = run.step(10)
    assert run.state.state == "idle" and run.state.run is None
    assert directive == FullChargeDirective(False)  # disarm: the hold ends, the switch goes off
    out = run.outcome()
    assert out["outcome"] == "done" and out["reason"] == "balance_reading"
    assert (
        run.state.last_full_local_date == run.day
        and run.state.last_full_source == "balance_reading"
    )
    assert run.state.consecutive_failures == 0 and run.state.last_attempt_day == run.day


def test_soc_99_or_100_without_a_new_reading_is_reached_unmeasured_and_a_failed_attempt():
    run = Run()
    before = run.state.last_full_local_date
    run.complete()
    run.soc = 100.0
    run.step(200)
    assert run.state.state == "settling"
    run.step(120)  # past the 300 s settle window with no reading
    assert run.state.state == "idle"
    out = run.outcome()
    assert out["outcome"] == "failed_reached_unmeasured" and out["reason"] == "settle_timeout"
    assert run.state.last_full_local_date == before  # L is not moved
    assert run.state.consecutive_failures == 1 and "reached_unmeasured" in run.sched.alerts(run.now)
    assert run.sched.last_directive == FullChargeDirective(False)


def test_a_reading_that_did_not_advance_past_the_arming_value_does_not_count():
    old = local(D0 - timedelta(days=9), 3)
    run = Run(read_at=old, status="green")
    run.complete()
    run.step(100)
    assert run.state.state == "settling"  # the sensor still shows the value seen when armed
    run.step(200)
    assert run.outcome()["outcome"] == "failed_reached_unmeasured"


def test_a_new_reading_with_an_unknown_status_does_not_count_until_the_status_follows():
    run = Run()
    run.complete()
    run.read_at, run.balance = run.now, "unknown"
    run.step(20)
    assert run.state.state == "settling"
    run.balance = "yellow"  # the status follows the timestamp inside the window
    run.step(20)
    assert run.outcome()["outcome"] == "done"
    assert run.state.readings[-1].status == "yellow"
    # ... and a status that never follows is an unmeasured run
    run = Run()
    run.complete()
    run.read_at, run.balance = run.now, "unknown"
    run.step(400)
    assert run.outcome()["outcome"] == "failed_reached_unmeasured"


def test_the_reading_may_arrive_before_the_settle_hold_is_needed():
    run = Run()
    run.reach()
    run.integration = "balanced"
    run.read_at, run.balance = run.now + timedelta(seconds=20), "green"
    run.step(30)  # completion and the reading are seen in the same step
    run.step(1)
    assert run.outcome()["outcome"] == "done"


@pytest.mark.parametrize("how", ["soc_100", "status_left", "top_hold_timeout"])
def test_every_completion_signal_starts_the_settle_hold(how):
    run = Run()
    run.reach()
    if how == "soc_100":
        run.soc = 100.0
    elif how == "status_left":
        run.integration = "balanced"
    else:
        run.step(120 * 60 - 31)
        assert run.state.state == "reached"
    run.step(31 if how == "top_hold_timeout" else 30)
    assert run.state.state == "settling" and run.state.run.completion_reason == how


def test_a_status_that_left_below_97_percent_is_an_aborted_run():
    run = Run()
    run.charge()
    run.soc = 90.0
    run.step()
    run.integration = "balanced"
    run.step()
    out = run.outcome()
    assert out["outcome"] == "failed_never_reached" and out["reason"] == "aborted"
    assert run.state.consecutive_failures == 1


def test_a_status_that_left_at_98_percent_is_a_completion():
    run = Run()
    run.charge()
    run.soc = 98.0
    run.step()
    run.integration = "balanced"
    run.step()
    assert run.state.state == "settling" and run.state.run.completion_reason == "status_left"


def test_soc_never_reached_by_the_end_of_the_day_fails_the_run():
    run = Run()
    run.charge()
    run.soc = 85.0
    run.step()
    run.now = local(run.day + timedelta(days=1), 0, 1)
    run.step()
    assert run.outcome()["outcome"] == "failed_never_reached"
    assert "never_reached" in run.sched.alerts(run.now)


def test_the_day_ending_while_reached_counts_a_reading_that_arrived_else_is_unmeasured():
    run = Run()
    run.reach()
    run.now = local(run.day + timedelta(days=1), 0, 1)
    run.step()
    assert run.outcome()["outcome"] == "failed_reached_unmeasured"
    run = Run()
    run.reach()
    run.read_at, run.balance = run.now + timedelta(seconds=5), "green"
    run.now = local(run.day + timedelta(days=1), 0, 1)
    run.step()
    # _observe_readings already stamped L from the reading; the run still ends with the day
    assert run.outcome()["outcome"] == "done"


def test_a_day_that_ends_while_settling_is_unmeasured():
    run = Run()
    run.complete()
    run.now = local(run.day + timedelta(days=1), 0, 0)
    run.step()
    assert run.outcome()["outcome"] == "failed_reached_unmeasured"


def test_a_scheduled_day_that_is_never_armed_fails_at_the_end_of_the_day():
    run = Run()
    run.at(23, 59)
    run.step()
    assert run.state.state == "scheduled"
    run.step(120)
    out = run.outcome()
    assert out["outcome"] == "failed_never_armed" and out["reason"] == "day_over"


def test_arming_waits_for_the_day_and_for_the_trigger():
    run = Run()
    run.now = local(D0, 23, 50)
    run.step(arm_due=True)  # a stale trigger before the run's day does not arm
    assert run.state.state == "scheduled"
    run.at(1, 45)
    run.step(arm_due=False)
    assert run.state.state == "scheduled"
    run.step(arm_due=True)
    assert run.state.state == "armed" and run.state.run.armed_at == run.now


def test_the_acknowledgement_clock_starts_when_the_actuator_drives_the_arm():
    run = Run()
    run.arm()
    # A guard-failure safe idle for ten minutes: nothing was driven, nothing times out.
    for _ in range(20):
        run.step(30, driven=False)
    assert run.state.state == "armed"
    run.step(30, driven=True)
    run.step(80, driven=True)  # still inside ack_timeout_s (90)
    assert run.state.state == "armed"
    run.step(20, driven=True)
    out = run.outcome()
    assert out["outcome"] == "failed_unacknowledged" and out["reason"] == "ack_timeout"
    assert "unacknowledged" in run.sched.alerts(run.now)


def test_an_armed_day_that_is_never_driven_fails_never_armed():
    run = Run()
    run.arm()
    run.step(30, driven=False)
    run.now = local(run.day + timedelta(days=1), 0, 1)
    run.step(driven=False)
    assert run.outcome()["outcome"] == "failed_never_armed"


@pytest.mark.parametrize(
    ("refusal", "outcome", "counts"),
    [
        ("delay_switch_on", "cancelled", False),
        ("delay_switch_unreadable", "cancelled", False),
        ("day_options_invalid", "cancelled", False),
        ("entities_missing", "cancelled", False),
        ("kill_switch_off", "cancelled", False),
        ("write_failed", "failed_unacknowledged", True),
    ],
)
def test_a_refused_arming_ends_the_run(refusal, outcome, counts):
    run = Run()
    run.arm()
    run.step(refusal=refusal)
    out = run.outcome()
    assert out["outcome"] == outcome and out["reason"].endswith(refusal)
    assert run.state.consecutive_failures == (1 if counts else 0)
    assert run.state.last_attempt_day == run.day  # one attempt per day, retried next evening
    assert run.sched.last_directive == FullChargeDirective(False)


def test_a_refusal_during_a_run_cancels_it():
    run = Run()
    run.charge()
    run.step(refusal="delay_switch_on")
    assert run.outcome()["outcome"] == "cancelled"


def test_an_aborted_day_is_retried_the_next_evening_not_at_once():
    run = Run()
    run.charge()
    run.integration = "balanced"
    run.soc = 40.0
    run.step()
    assert run.outcome()["outcome"] == "failed_never_reached"
    evening = local(run.day, 21)
    assert run.sched.evaluation_wanted(evening, True) == run.day + timedelta(days=1)


def test_three_failures_pause_the_automatic_selection_and_a_reading_clears_it():
    sched = with_last_full(D0 - timedelta(days=20), status=None, now=local(D0, 7))
    day = D0
    for i in range(3):
        sched.step(obs(local(day, 7 + i)))  # overdue: today is decided
        assert sched.state.state == "scheduled"
        sched.step(obs(local(day + timedelta(days=1), 0, 1)))  # the day passes
        day += timedelta(days=1)
    assert sched.state.paused_until == local(day, 0, 1) + timedelta(days=5)
    assert sched.state.consecutive_failures == 0 and "paused" in sched.alerts(local(day, 1))
    assert sched.state.state == "idle"  # paused: the overdue bound does not decide
    # while paused nothing new is decided
    sched = with_last_full(D0 - timedelta(days=20), status=None, now=local(D0, 7))
    sched.state.paused_until = local(D0 + timedelta(days=5))
    sched.step(obs(local(D0, 8), tomorrow_published=True))
    assert sched.state.state == "idle" and sched.evaluation_wanted(local(D0, 21), True) is None
    sched.step(obs(local(D0 + timedelta(days=5), 13), tomorrow_published=False))
    assert sched.state.state == "scheduled"  # the pause is over, the bound is overdue
    # a new valid reading (whoever caused it) clears the pause and the counter
    fresh = with_last_full(D0 - timedelta(days=20), status=None, now=local(D0, 7))
    fresh.state.paused_until = local(D0 + timedelta(days=9))
    fresh.state.consecutive_failures = 2
    fresh.step(obs(local(D0, 8), read_at=local(D0, 3), balance_status="green"))
    assert fresh.state.paused_until is None and fresh.state.consecutive_failures == 0


def test_soc_at_99_outside_a_run_does_not_move_l_but_a_new_reading_does():
    sched = make(local(D0), settings=replace(S, bootstrap_last_full=date(2026, 10, 30)))
    sched.step(obs(local(D0, 15), soc_pct=99.5, integration_status="balanced"))
    assert sched.state.last_full_local_date == date(2026, 10, 30)  # SOC alone never stamps L
    sched.step(
        obs(local(D0, 16), read_at=local(D0, 15, 40), balance_status="yellow", delta_trend="stable")
    )
    assert (
        sched.state.last_full_local_date == D0 and sched.state.last_full_source == "balance_reading"
    )
    assert sched.cadence(local(D0, 17))[0] == "yellow"
    # an unknown or stale reading never moves L
    sched.step(obs(local(D0, 17), read_at=local(D0, 15, 40), balance_status="unknown"))
    sched.step(obs(local(D0, 18), read_at=None, balance_status=None))
    assert sched.state.last_full_local_date == D0


def test_a_reading_whose_status_arrives_later_still_stamps_l():
    sched = make(local(D0))
    sched.step(obs(local(D0, 15), read_at=local(D0, 14, 59), balance_status="unknown"))
    assert sched.state.last_full_local_date is None and len(sched.state.readings) == 1
    sched.step(obs(local(D0, 15, 1), read_at=local(D0, 14, 59), balance_status="green"))
    assert sched.state.last_full_local_date == D0
    assert [r.status for r in sched.state.readings] == ["green"]


def test_two_orange_readings_in_a_row_raise_the_alert_and_a_better_one_clears_it():
    sched = make(local(D0))
    for i, status in enumerate(("orange", "orange")):
        sched.step(
            obs(
                local(D0 + timedelta(days=i), 15),
                read_at=local(D0 + timedelta(days=i), 14),
                balance_status=status,
            )
        )
        assert ("balance_alert" in sched.alerts(local(D0 + timedelta(days=i), 16))) == (i == 1)
    now = local(D0 + timedelta(days=9))
    assert sched.cadence(now) == ("alert", CadenceRow(5, 7))
    sched.step(obs(now, read_at=local(D0 + timedelta(days=9), 3), balance_status="green"))
    assert "balance_alert" not in sched.alerts(now)
    assert sched.cadence(now)[0] == "unknown"


# ---------------------------------------------------------------------------
# Simulated writer (shadow and dry_run)
# ---------------------------------------------------------------------------


def test_a_simulated_run_ends_as_done_simulated_at_the_end_of_its_day_and_moves_a_simulated_l():
    run = Run(mode="simulated")
    assert run.sched.writer_mode == "simulated"
    run.arm()
    assert run.sched.last_directive.armed  # dry_run: the actuator simulates the pair
    run.step(driven=False)
    assert run.state.state == "armed"  # no acknowledgement is waited for
    run.now = local(run.day, 23, 59)
    run.step()
    assert run.state.state == "armed"
    run.now = local(run.day + timedelta(days=1), 0, 1)
    run.step()
    out = run.outcome()
    assert out["outcome"] == "done_simulated"
    assert run.state.last_full_local_date == run.day and run.state.last_full_source == "simulated"
    assert run.state.consecutive_failures == 0


def test_a_simulated_run_is_cancelled_by_a_real_refusal_but_ignores_the_kill_switch():
    run = Run(mode="simulated")
    run.arm()
    run.step(refusal="kill_switch_off")
    assert run.state.state == "armed"  # a dry_run needs no kill switch
    run.step(refusal="delay_switch_on")
    assert run.outcome()["outcome"] == "cancelled"


def test_a_live_run_is_cancelled_by_the_kill_switch():
    run = Run()
    run.arm()
    run.step(refusal="kill_switch_off")
    assert run.outcome()["outcome"] == "cancelled"


def test_a_state_of_the_other_writer_is_discarded_and_the_real_l_is_bootstrapped_from_a_reading():
    sim = make(
        local(D0), mode="simulated", settings=replace(S, bootstrap_last_full=date(2026, 11, 1))
    )
    sim.state.last_full_local_date = date(2026, 11, 3)
    sim.state.last_full_source = "simulated"
    live = make(local(D0, 12), mode="live", state=sim.state)
    assert live.reset_reason == "writer_mode simulated -> live"
    assert live.state.last_full_local_date is None and live.state.writer_mode == "live"
    assert live.state.enabled_at == local(D0, 12)
    assert any(e["event"] == "bootstrap" and e["reset_reason"] for e in live.drain_events())
    live.step(obs(local(D0, 13), read_at=local(D0, 12, 30), balance_status="green"))
    assert live.state.last_full_local_date == D0  # the real reading is honoured again


# ---------------------------------------------------------------------------
# Suspension, directive, persistence and restart
# ---------------------------------------------------------------------------


def test_a_suspended_scheduler_is_hands_off_and_decides_nothing():
    evening = local(D0, 21)
    sched = with_last_full(D0 - timedelta(days=20), status=None, now=evening)
    sched.suspend("timezone_mismatch")
    assert sched.step(obs(evening, tomorrow_published=True)) == FullChargeDirective(False)
    assert sched.state.state == "idle" and sched.evaluation_wanted(evening, True) is None
    assert (
        sched.record_evaluation(D0 + timedelta(days=1), {"forcing_cost_eur": 0.0}, evening) is False
    )
    sched.suspend(None)
    sched.step(obs(evening, tomorrow_published=True))
    assert sched.state.state == "scheduled"


@pytest.mark.parametrize("target", ["scheduled", "armed", "charging", "reached", "settling"])
def test_restart_in_every_state_keeps_the_calendar_decision_and_rederives_the_directive(target):
    run = Run()
    {
        "scheduled": lambda: None,
        "armed": run.arm,
        "charging": run.charge,
        "reached": run.reach,
        "settling": run.complete,
    }[target]()
    assert run.state.state == target
    before = run.sched.last_directive
    saved = json.loads(json.dumps(run.state.to_json()))  # through the state file
    restarted = make(run.now, state=FullChargeState.from_json(saved))
    assert restarted.reset_reason is None and restarted.state.state == target
    assert restarted.state.run.day == run.day
    assert restarted.state.run.driven_at is None  # the acknowledgement clock restarts
    # Nothing is written from the persisted state alone: the directive is only derived on a step.
    assert restarted.last_directive == FullChargeDirective(False)
    directive = restarted.step(
        Observation(
            now=run.now + timedelta(seconds=30),
            soc_pct=run.soc,
            integration_status=run.integration,
            read_at=run.read_at,
            balance_status=run.balance,
            driven_armed=target in ARMED_STATES,
        )
    )
    assert restarted.state.state == target
    assert directive.armed == before.armed and directive.weekday == before.weekday
    assert directive.settle_until == before.settle_until  # the hold survives the restart


def test_restart_after_the_hold_deadline_ends_the_run_as_unmeasured():
    run = Run()
    run.complete()
    restarted = make(run.now, state=FullChargeState.from_json(run.state.to_json()))
    restarted.step(
        Observation(
            now=run.now + timedelta(seconds=301), soc_pct=100, integration_status="balanced"
        )
    )
    assert restarted.state.state == "idle"
    assert restarted.state.last_outcome["outcome"] == "failed_reached_unmeasured"


def test_restart_while_charging_and_completion_during_the_outage():
    run = Run()
    run.reach()
    restarted = make(run.now, state=FullChargeState.from_json(run.state.to_json()))
    assert restarted.state.run.soc_peak_pct == 99.0
    restarted.step(
        Observation(now=run.now + timedelta(hours=1), soc_pct=95.0, integration_status="balanced")
    )
    assert restarted.state.state == "settling"  # the persisted peak proves the charge completed


def test_the_state_round_trips_through_json_and_rejects_corrupt_files():
    run = Run(read_at=local(D0 - timedelta(days=8), 3))
    run.complete()
    data = run.state.to_json()
    json.dumps(data, allow_nan=False)  # the state file never holds NaN or infinity
    again = FullChargeState.from_json(json.loads(json.dumps(data)))
    # the acknowledgement clock is the one thing a restart does not keep
    assert again.to_json() == {**data, "run": {**data["run"], "driven_at": None}}
    for broken in (
        {**data, "schema_version": 99},
        {**data, "state": "bogus"},
        {**data, "writer_mode": "other"},
        {**data, "run": None},  # state without its run
        {**data, "enabled_at": "2026-11-04T10:00:00"},  # naive timestamp
        {**data, "enabled_at": None},
        {k: v for k, v in data.items() if k != "state"},
    ):
        with pytest.raises((ValueError, KeyError, TypeError, AssertionError)):
            FullChargeState.from_json(broken)


def test_a_fresh_start_records_the_bootstrap_and_marks_the_state_for_saving():
    sched = make(local(D0))
    assert sched.dirty and sched.state.state == "idle"
    assert sched.state.enabled_at == local(D0)
    assert events(sched, "bootstrap")[0]["writer_mode"] == "live"


def test_the_view_has_the_status_fields():
    run = Run()
    run.charge()
    view = run.sched.view(run.now)
    assert view["state"] == "charging" and view["run"]["weekday"] == "thursday"
    assert view["cadence"] == {
        "class": "unknown",
        "min_days": 10,
        "max_days": 14,
        "trend": "stable",
        "valid_readings": 0,
    }
    assert view["days_since"] is not None and view["forced_after_day"]
    json.dumps(view)
    assert set(STATES) >= {view["state"]}
    assert "done" in OUTCOMES


# ---------------------------------------------------------------------------
# 40 synthetic days: the cadence stays inside the active row and runs never overlap
# ---------------------------------------------------------------------------


class Plant:
    """A battery with Omnibattery's weekly run: charges when armed, measures 70 s after."""

    def __init__(self, statuses, *, unmeasured=()):
        self.statuses = list(statuses)
        self.unmeasured = set(unmeasured)
        self.runs = 0
        self.integration = "balanced"
        self.soc = 60.0
        self.read_at: datetime | None = None
        self.balance = "unknown"
        self.trend = "stable"
        self._phase: str | None = None
        self._pending: tuple[datetime, str] | None = None

    def tick(self, now: datetime, directive: FullChargeDirective, day: date | None) -> None:
        local_now = now.astimezone(TZ)
        if self._pending and now >= self._pending[0]:
            self.read_at, self.balance = self._pending[0], self._pending[1]
            self._pending = None
        if not directive.armed or day is None or local_now.date() != day:
            if not directive.armed:
                self._phase = None
            if self._phase is None:  # the normal daily cycling between runs
                self.integration, self.soc = "balanced", 60.0
            return
        if self._phase is None:  # Omnibattery starts the run as soon as the weekly switch is armed
            self._phase, self.integration = "charging", WEEKLY
        if self._phase == "charging":
            self.soc = min(100.0, self.soc + 1.0)
            if self.soc >= 100.0:
                self._phase, self.integration = "done", "balanced"
                self.runs += 1
                if self.runs not in self.unmeasured:
                    status = self.statuses[min(self.runs, len(self.statuses)) - 1]
                    self._pending = (now + timedelta(seconds=70), status)


def replay(mode: str, days: int = 40, statuses=("green",) * 40, unmeasured=()):
    start = local(D0, 12)
    sched = make(start, mode=mode)
    plant = Plant(statuses, unmeasured=unmeasured)
    now = start
    end = start + timedelta(days=days)
    log: list[dict] = []
    published_at = time(20, 15)
    last_eval: date | None = None
    while now < end:
        local_now = now.astimezone(TZ)
        published = local_now.time() >= published_at
        run = sched.state.run
        directive = sched.last_directive
        # dry_run: the battery is not run by the weekly switch, nothing is measured
        plant.tick(
            now,
            directive if mode == "live" else FullChargeDirective(False),
            run.day if run else None,
        )
        if published and last_eval != local_now.date():
            last_eval = local_now.date()
            wanted = sched.evaluation_wanted(now, True)
            if wanted is not None:
                # Prices of the day vary: every third evening is expensive (declined).
                cost = 0.2 if wanted.toordinal() % 3 == 0 else 0.0
                sched.record_evaluation(
                    wanted, {"forcing_cost_eur": cost, "shortfall_kwh": 0.0}, now
                )
        plan_charges_at = local(run.day, 1, 45) if run else None
        sched.step(
            Observation(
                now=now,
                soc_pct=plant.soc,
                integration_status=plant.integration,
                read_at=plant.read_at,
                balance_status=plant.balance,
                delta_trend=plant.trend,
                driven_armed=directive.armed,
                tomorrow_published=published,
                arm_due=plan_charges_at is not None and now >= plan_charges_at,
            )
        )
        log.extend(sched.drain_events())
        busy = sched.state.state in ARMED_STATES
        now += timedelta(seconds=30 if busy else 900)
    return sched, plant, log


def assert_runs_never_overlap(log):
    active = None
    last_day = None
    for event in log:
        if event["event"] != "transition":
            continue
        if event["to_state"] == "scheduled":
            assert event["from_state"] == "idle" and active is None  # one run at a time
            active = event["day"]
            assert last_day is None or active > last_day  # strictly later days
            last_day = active
        elif event["to_state"] == "idle":
            assert active is not None and event["day"] == active
            active = None


def test_forty_synthetic_days_in_the_unknown_row_stay_inside_ten_to_fourteen_days():
    sched, plant, log = replay("live", statuses=("green", "green", "yellow", "green"))
    assert_runs_never_overlap(log)
    decisions = [e for e in log if e["event"] == "decision" and e["accepted"]]
    outcomes = [e for e in log if e["event"] == "outcome"]
    assert len(decisions) == len(outcomes) >= 3
    assert all(o["outcome"] == "done" for o in outcomes)
    for d in decisions:
        assert d["min_days"] <= d["d"] <= d["max_days"], d
    days = [date.fromisoformat(o["day"]) for o in outcomes]
    gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False)]
    assert all(g >= 7 for g in gaps) and all(g <= 14 for g in gaps), gaps
    assert sched.state.consecutive_failures == 0 and sched.state.state == "idle"


def test_forty_synthetic_days_with_good_readings_reach_the_long_cadence_and_never_exceed_it():
    sched, plant, log = replay("live", days=120, statuses=("green",) * 40)
    assert_runs_never_overlap(log)
    decisions = [e for e in log if e["event"] == "decision" and e["accepted"]]
    for d in decisions:
        assert d["min_days"] <= d["d"] <= d["max_days"], d
    assert {d["cadence_class"] for d in decisions} >= {"unknown", "green"}
    green = [d for d in decisions if d["cadence_class"] == "green"]
    assert green and all(14 <= d["d"] <= 28 for d in green)


def test_a_degrading_pack_tightens_the_cadence_and_alerts_on_two_orange_readings():
    sched, plant, log = replay(
        "live", days=60, statuses=("green", "orange", "orange", "green", "green")
    )
    assert_runs_never_overlap(log)
    decisions = [e for e in log if e["event"] == "decision" and e["accepted"]]
    classes = [d["cadence_class"] for d in decisions]
    assert "yellow" in classes and "alert" in classes
    for d in decisions:
        assert d["min_days"] <= d["d"] <= d["max_days"], d
    alert = [d for d in decisions if d["cadence_class"] == "alert"]
    assert all(5 <= d["d"] <= 7 for d in alert)


def test_runs_without_a_reading_fail_unmeasured_pause_and_never_move_l():
    sched, plant, log = replay("live", days=40, unmeasured=set(range(1, 100)))
    assert_runs_never_overlap(log)
    outcomes = [e for e in log if e["event"] == "outcome"]
    assert outcomes and all(o["outcome"] == "failed_reached_unmeasured" for o in outcomes)
    assert sched.state.last_full_local_date is None  # unmeasured is never a full charge
    assert any(e["event"] == "paused" for e in log)  # three failures in a row pause the rule
    days = [date.fromisoformat(o["day"]) for o in outcomes]
    assert days == sorted(set(days))  # one attempt per day, never overlapping


def test_forty_synthetic_days_of_a_dry_run_keep_a_simulated_cadence_of_ten_to_fourteen_days():
    sched, plant, log = replay("simulated", days=60)
    assert_runs_never_overlap(log)
    outcomes = [e for e in log if e["event"] == "outcome"]
    assert len(outcomes) >= 4 and all(o["outcome"] == "done_simulated" for o in outcomes)
    days = [date.fromisoformat(o["day"]) for o in outcomes]
    gaps = [(b - a).days for a, b in zip(days, days[1:], strict=False)]
    assert all(10 <= g <= 14 for g in gaps), gaps
    assert sched.state.last_full_source == "simulated"
