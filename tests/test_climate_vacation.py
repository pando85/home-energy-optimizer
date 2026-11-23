"""Vacation mode v1 (ADR-0010): the window, the decision, the job, the actuator, the contract and
the metrics."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from climate_shadow_fakes import (
    CCFG,
    MPC,
    SETTINGS,
    SITE,
    FakeSamples,
    forecast_record,
    house,
    prior,
    utc,
)
from service_fakes import entity_state
from test_climate_actuator import ENTS, OWNER, ROOMS, FakeHA, ctx, make_plan
from test_climate_actuator import world as actuator_world

from battery_optimizer.climate import plan_export, shadow, vacation
from battery_optimizer.climate.vacation import VacationRead, VacationSettings, VacationWindow
from battery_optimizer.service.climate_actuator import HEAT_OFF, HEAT_ON, ClimateActuator
from battery_optimizer.service.climate_shadow import ClimateShadowJob
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import (
    ClimateActuationSettings,
    ClimateShadowSettings,
    _parse_vacation,
)
from battery_optimizer.service.entity_store import EntityStore
from battery_optimizer.service.metrics import ServiceMetrics
from battery_optimizer.service.sink import ShadowSink, iter_records

TZ = ZoneInfo("Europe/Madrid")
VS = VacationSettings()
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/schemas/climate-plan-v1.json").read_text()
)


def local(at: datetime) -> str:
    """An ``input_datetime`` state (date and time, local) for the UTC instant *at*."""
    return at.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")


def window(start: datetime, end: datetime, now: datetime, settings: VacationSettings = VS):
    return vacation.read_window(local(start), local(end), tz=TZ, now=now, settings=settings)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


NOW = utc(2026, 11, 24, 10)


def test_a_valid_window_is_read_in_local_time_with_ready_at_two_hours_early():
    read = vacation.read_window(
        "2026-11-25 08:00:00", "2026-12-02 20:00:00", tz=TZ, now=NOW, settings=VS
    )
    assert read.invalid_reason is None
    w = read.window
    assert w is not None
    assert w.start == utc(2026, 11, 25, 7) and w.end == utc(2026, 12, 2, 19)
    assert w.ready_at == utc(2026, 12, 2, 17)


def test_a_date_only_helper_is_local_midnight():
    assert vacation.parse_helper("2026-11-25", TZ) == utc(2026, 11, 24, 23)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (NOW, NOW),  # the cancel script, and fresh helpers, set both to the same instant
        (NOW - timedelta(days=3), NOW - timedelta(hours=1)),  # a vacation already over
    ],
)
def test_an_empty_or_finished_window_is_no_vacation_and_not_invalid(start, end):
    read = window(start, end, NOW)
    assert read == VacationRead(None, None)
    view = vacation.view(read, NOW, guard_c=12.0)
    assert view["phase"] == "none" and view["invalid_reason"] is None
    assert view["start"] is None and view["end"] is None


@pytest.mark.parametrize(
    ("start", "end", "reason"),
    [
        (NOW + timedelta(days=2), NOW + timedelta(days=1), "end_before_start"),
        (NOW, NOW + timedelta(days=61), "too_long"),
    ],
)
def test_a_rejected_window_echoes_the_helpers_and_has_no_ready_at(start, end, reason):
    read = window(start, end, NOW)
    assert read.window is None and read.invalid_reason == reason
    view = vacation.view(read, NOW, guard_c=12.0)
    assert view["phase"] == "none" and view["invalid_reason"] == reason
    assert view["start"] == start.isoformat() and view["end"] == end.isoformat()
    assert view["ready_at"] is None and view["recovery_start"] is None
    assert vacation.window_from_view(view) is None  # never mistaken for a window


@pytest.mark.parametrize(
    ("start_state", "end_state", "reason"),
    [
        (None, None, "helpers_unavailable"),
        ("2026-11-25 08:00:00", "garbage", "unparseable"),
        ("unknown", "2026-11-25 08:00:00", "unparseable"),
    ],
)
def test_unusable_helpers_are_no_vacation(start_state, end_state, reason):
    read = vacation.read_window(start_state, end_state, tz=TZ, now=NOW, settings=VS)
    assert read.window is None and read.invalid_reason == reason


def test_a_disabled_vacation_reads_nothing():
    off = VacationSettings(enabled=False)
    assert window(NOW, NOW + timedelta(days=3), NOW, off) == VacationRead(None, None)
    assert off.entities() == ()


def test_the_phases():
    w = VacationWindow(NOW, NOW + timedelta(days=7), NOW + timedelta(days=7, hours=-2))
    assert vacation.phase(w, NOW - timedelta(hours=1), None) == "scheduled"
    assert vacation.phase(w, NOW + timedelta(days=1), None) == "away"
    rec = NOW + timedelta(days=5)
    assert vacation.phase(w, NOW + timedelta(days=4), rec) == "away"
    assert vacation.phase(w, NOW + timedelta(days=6), rec) == "recovery"
    assert vacation.phase(w, w.ready_at, rec) == "ready"
    assert vacation.phase(w, w.end, rec) == "none"
    assert vacation.phase(None, NOW, None) == "none"


def test_the_horizon_reaches_the_return_only_within_the_look_ahead():
    t0 = NOW
    far = VacationWindow(t0 - timedelta(days=1), t0 + timedelta(days=5), t0 + timedelta(days=5))
    assert vacation.horizon_hours(far, t0, 24, VS) == 24
    near = VacationWindow(
        t0 - timedelta(days=1), t0 + timedelta(hours=41, minutes=30), t0 + timedelta(hours=39.5)
    )
    assert vacation.horizon_hours(near, t0, 24, VS) == 48  # ceil(41.5 + 6)
    capped = VacationWindow(t0, t0 + timedelta(hours=70), t0 + timedelta(hours=40))
    assert vacation.horizon_hours(capped, t0, 24, VS) == 72
    soon = VacationWindow(t0 - timedelta(days=3), t0 + timedelta(hours=1), t0 - timedelta(hours=1))
    assert vacation.horizon_hours(soon, t0, 24, VS) == 24  # never shorter than the config's
    assert vacation.horizon_hours(None, t0, 24, VS) == 24


def test_the_terminal_term_is_off_only_for_a_horizon_ending_in_the_absence():
    w = VacationWindow(NOW, NOW + timedelta(days=4), NOW + timedelta(days=4, hours=-2))
    assert vacation.terminal_off(w, NOW, 24)
    assert not vacation.terminal_off(w, NOW + timedelta(days=3), 30)  # covers the return
    assert not vacation.terminal_off(w, NOW - timedelta(days=2), 24)  # ends before the start
    assert not vacation.terminal_off(None, NOW, 24)


def test_the_minimum_is_the_guard_while_away_and_the_return_comfort_from_ready_at():
    start, end = NOW + timedelta(hours=1), NOW + timedelta(hours=5)
    w = VacationWindow(start, end, end - timedelta(hours=2))
    ends = [NOW + timedelta(minutes=15 * (i + 1)) for i in range(28)]
    normal = np.full((3, 28), 21.0)
    out = vacation.apply_minimum(normal, ends, w, np.array([21.0, 23.5, 21.0]), 12.0)
    for i, te in enumerate(ends):
        if te < start or te >= end:
            assert (out[:, i] == 21.0).all()
        elif te < w.ready_at:
            assert (out[:, i] == 12.0).all()
        else:
            assert out[:, i].tolist() == [21.0, 23.5, 21.0]
    assert (normal == 21.0).all()  # the input is not modified


def test_the_warm_up_start_is_the_first_heating_slot_of_the_absence():
    w = VacationWindow(NOW, NOW + timedelta(days=2), NOW + timedelta(hours=46))
    heating = [0] * 96
    heating[50] = 1
    assert vacation.recovery_start(w, NOW - timedelta(hours=2), heating) == NOW + timedelta(
        minutes=15 * 50 - 120
    )
    assert vacation.recovery_start(w, NOW, [0] * 96) is None
    early = [1] + [0] * 95  # a heating slot before the start is not the warm-up
    assert vacation.recovery_start(w, NOW - timedelta(minutes=15), early) is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_the_vacation_config_defaults_and_entities():
    assert _parse_vacation({}) == VacationSettings()
    s = ClimateShadowSettings()
    assert VS.start_entity in s.entities() and VS.end_entity in s.entities()


@pytest.mark.parametrize(
    "raw",
    [
        {"guard_c": 20},
        {"guard_c": 2},
        {"start_entity": "input_number.x"},
        {"start_entity": VS.end_entity},
        {"lookahead_hours": 70},
        {"lookahead_hours": 12},
        {"ready_margin_minutes": 1000},
        {"max_days": 400},
        {"unknown": 1},
    ],
)
def test_bad_vacation_config_is_refused(raw):
    with pytest.raises(ValueError, match="vacation"):
        _parse_vacation(raw)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


MIDNIGHT = utc(2026, 11, 15, 23)
T0 = MIDNIGHT + timedelta(hours=10)


def _decide(read: VacationRead | None, hours: int = 24, t0: datetime = T0):
    sp = prior()
    start = t0 - timedelta(days=4)
    d = shadow.build_live_data(
        house(start, t0 + timedelta(minutes=5)),
        start=start,
        end=t0 + timedelta(hours=hours),
        config=CCFG,
        site=SITE,
    )
    mrec = {"params": dict(sp.prior.params), "model_hash": "m"}
    inputs = shadow.LiveInputs(
        regime="comfort",
        forecast=forecast_record(t0 - timedelta(minutes=50), hours=80),
        vacation=read,
    )
    return shadow.decide(sp, mrec, d, MPC, t0, inputs, settings=SETTINGS, vacation_settings=VS)


def test_without_a_vacation_the_decision_is_unchanged():
    rec = _decide(VacationRead(None, None))
    assert rec["horizon_hours"] == 24 and rec["terminal"] is True
    assert rec["inputs"]["vacation"]["phase"] == "none"
    assert len(rec["plan"]["plan_on"]) == 96


def test_away_with_the_return_far_plans_24_h_at_the_guard_without_terminal_term():
    read = window(T0 - timedelta(days=1), T0 + timedelta(days=5), T0)
    normal = _decide(VacationRead(None, None))
    rec = _decide(read)
    assert rec["status"] == "planned"
    assert rec["horizon_hours"] == 24 and rec["terminal"] is False
    assert rec["inputs"]["vacation"]["phase"] == "away"
    # no comfort to hold: the house coasts (the synthetic house stays far above the guard)
    assert rec["plan"]["plan_heat_slots"] < normal["plan"]["plan_heat_slots"]
    assert rec["plan"]["plan_heat_slots"] == 0
    # the valve model keeps the normal schedule's setpoints (thermostats are not touched)
    assert rec["inputs"]["setpoints"] == normal["inputs"]["setpoints"]


def test_near_the_return_the_horizon_and_the_record_cover_it():
    end = T0 + timedelta(hours=30)
    read = window(T0 - timedelta(days=3), end, T0)
    hours = shadow.decision_hours(MPC, shadow.LiveInputs(regime="x", vacation=read), VS, T0)
    assert hours == 36
    rec = _decide(read, hours=hours)
    assert rec["status"] == "planned" and rec["horizon_hours"] == 36 and rec["terminal"] is True
    plan = rec["plan"]
    assert len(plan["plan_on"]) == 144 and all(len(r) == 144 for r in plan["plan_temps"])
    assert len(rec["inputs"]["t_out"]) == 144
    # the warm-up: it heats before ready_at, and reaches the return comfort there
    v = rec["inputs"]["vacation"]
    assert v["ready_at"] == (end - timedelta(hours=2)).isoformat()
    assert v["recovery_start"] is not None
    assert v["phase"] in ("away", "recovery")
    # the exported plan has n = 144 slots and validates against the contract
    exported = plan_export.build_plan(rec, code_version="test")
    assert exported["slots"]["n"] == 144
    assert exported["inputs"]["vacation"] == v
    jsonschema = pytest.importorskip("jsonschema")
    view = {**exported, **plan_export.resolve_mode("live", "off", live_allowed=True).fields()}
    view["summary"] = plan_export.summarise(exported, T0 + timedelta(minutes=10))
    jsonschema.validate(view, SCHEMA)


def test_the_dhw_history_repeats_over_a_horizon_longer_than_a_day():
    end = T0 + timedelta(hours=30)
    rec = _decide(window(T0 - timedelta(days=3), end, T0), hours=36)
    slots = rec["inputs"]["dhw_slots"]
    # 03:00 UTC daily: slot 72 (tomorrow) and 72 + 96 is beyond 144, so one; and nothing invented
    assert 72 in slots and all(s % 96 == 72 for s in slots)


# ---------------------------------------------------------------------------
# The job: records, cancel, refit pause, forced re-decision
# ---------------------------------------------------------------------------


def _job(tmp_path, samples=None):
    return ClimateShadowJob(
        settings=ClimateShadowSettings(enabled=True, prior="abcdef"),
        climate_config=CCFG,
        mpc_config=MPC,
        prior=prior(),
        site=SITE,
        sink=ShadowSink(tmp_path),
        data_dir=tmp_path,
        samples=samples or FakeSamples(MIDNIGHT - timedelta(days=35), MIDNIGHT + timedelta(days=3)),
        code_version="test",
        plan_settings=SETTINGS,
    )


def _records(tmp_path, kind):
    return list(iter_records(tmp_path, kind, utc(2026, 1, 1), utc(2027, 1, 1)))


def test_windows_are_recorded_and_a_cancel_closes_them_at_the_cancel(tmp_path):
    job = _job(tmp_path)
    job._loaded = True
    now = T0
    read = window(now - timedelta(days=1), now + timedelta(days=3), now)
    job._track_vacation(read, now)
    job._track_vacation(read, now + timedelta(hours=1))  # unchanged: nothing new
    cancel = now + timedelta(hours=5)
    job._track_vacation(VacationRead(None, None), cancel)
    recs = _records(tmp_path, "climate_vacations")
    assert [r["closed"] for r in recs] == [False, True]
    assert recs[1]["end"] == cancel.isoformat()
    assert job.vacations[read.window.start] == cancel
    # a restart reads them back
    again = _job(tmp_path)
    again.load(cancel + timedelta(hours=1))
    assert again.vacations == job.vacations and again._vacation_last is None


def test_a_cancel_before_the_start_leaves_an_empty_window(tmp_path):
    job = _job(tmp_path)
    job._loaded = True
    read = window(T0 + timedelta(days=1), T0 + timedelta(days=3), T0)
    job._track_vacation(read, T0)
    job._track_vacation(VacationRead(None, None), T0 + timedelta(hours=1))
    start = read.window.start
    assert job.vacations[start] == start
    assert not job._vacation_in_window(start.date() + timedelta(days=2), start + timedelta(days=2))


def test_a_restart_inside_a_vacation_remembers_the_open_window(tmp_path):
    job = _job(tmp_path)
    job._loaded = True
    read = window(T0 - timedelta(days=1), T0 + timedelta(days=3), T0)
    job._track_vacation(read, T0)
    again = _job(tmp_path)
    again.load(T0 + timedelta(hours=2))
    assert again._vacation_last == read.window


def test_the_refit_pauses_while_a_vacation_is_in_its_window(tmp_path):
    job = _job(tmp_path)
    job._loaded = True
    job.model = {"valid_from": "2026-11-01", "model_hash": "old", "params": {}}
    today = T0.astimezone(TZ).date()
    job.vacations = {T0 - timedelta(days=10): T0 - timedelta(days=3)}
    job._maybe_refit(None, today, T0)
    assert job.model["model_hash"] == "old" and job.refit_paused == "vacation"
    # once the vacation has left the 28-day window the refit runs again
    job.vacations = {T0 - timedelta(days=40): T0 - timedelta(days=33)}
    assert not job._vacation_in_window(today, T0)
    # an active vacation pauses it too
    job.vacations = {T0 - timedelta(hours=1): T0 + timedelta(days=5)}
    assert job._vacation_in_window(today, T0)


def test_a_forced_run_decides_the_same_boundary_again_without_scoring_it_twice(tmp_path):
    job = _job(tmp_path)
    now = T0 + timedelta(minutes=10)
    inputs = shadow.LiveInputs(regime="comfort", vacation=VacationRead(None, None))
    first = job.run_once(now, inputs)
    assert first["status"] in ("planned", "no_state", "no_inputs")
    assert job.run_once(now, inputs)["status"] == "skipped"
    read = window(now, now + timedelta(days=2), now)
    again = job.run_once(now + timedelta(minutes=2), replace(inputs, vacation=read), force=True)
    assert again["status"] != "skipped"
    decisions = _records(tmp_path, "climate_decisions")
    assert len(decisions) == 2 and decisions[1].get("redecision") is True
    assert decisions[1]["inputs"]["vacation"]["start"] == read.window.start.isoformat()


# ---------------------------------------------------------------------------
# The actuator: guard, vacation hold, back to normal before the return
# ---------------------------------------------------------------------------


ANOW = utc(2026, 11, 26, 10, 20)


def _vac_world(clock, start, end, **kw):
    store = actuator_world(clock, **kw)
    full = EntityStore.for_entities((OWNER, *ENTS.all(), VS.start_entity, VS.end_entity))
    for entity in (OWNER, *ENTS.all()):
        state = store.get(entity)
        if state is not None:
            full.apply(state)
    at = clock.now() - timedelta(days=1)
    full.apply(entity_state(VS.start_entity, local(start), at))
    full.apply(entity_state(VS.end_entity, local(end), at))
    return full


def _make(clock, store):
    ha = FakeHA(store, clock)
    metrics = ServiceMetrics(climate_actuation=True)

    async def record(at, rec):
        return None

    actuator = ClimateActuator(
        settings=ClimateActuationSettings(mode="live"),
        owner_entity=OWNER,
        store=store,
        clock=clock,
        send=ha.send,
        record=record,
        metrics=metrics,
        vacation=VS,
        tz=TZ,
    )
    return actuator, ha, metrics


def _tick(actuator, context):
    return asyncio.run(actuator.tick(context))


def _count(metrics, name, **labels):
    return getattr(metrics, name).labels(**labels)._value.get()


def test_while_away_the_floor_is_the_guard_not_the_room_floor():
    clock = FakeClock(ANOW)
    store = _vac_world(clock, ANOW - timedelta(days=1), ANOW + timedelta(days=5), rooms=16.0)
    actuator, ha, _ = _make(clock, store)
    rec = _tick(actuator, ctx(clock, make_plan(0, t0=ANOW - timedelta(minutes=20))))
    assert rec["desired"]["source"] == "plan" and rec["desired"]["heating"] is False
    assert rec["observed"]["floors"] == dict.fromkeys(ROOMS, 12.0)
    assert rec["observed"]["vacation_absent"] is True
    assert ha.writes() == []  # already DHW
    snap = actuator.snapshot()
    assert snap["floor_override_rooms"] == 0 and snap["guard_override_rooms"] == 0


def test_a_room_below_the_guard_forces_heat_and_counts_as_a_guard_override():
    clock = FakeClock(ANOW)
    rooms = {"bedroom": 11.0, "hall_kitchen": 16.0, "office_t": 16.0}
    store = _vac_world(clock, ANOW - timedelta(days=1), ANOW + timedelta(days=5), rooms=rooms)
    actuator, ha, metrics = _make(clock, store)
    rec = _tick(actuator, ctx(clock, make_plan(0, t0=ANOW - timedelta(minutes=20))))
    assert rec["desired"]["source"] == "vacation_guard" and rec["desired"]["heating"] is True
    assert ("heishamon_heatmode", HEAT_ON) in ha.writes()
    snap = actuator.snapshot()
    assert snap["guard_override_rooms"] == 1 and snap["floor_override_rooms"] == 0
    assert metrics.climate_actuator_floor_overrides._value.get() == 0


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"plan": None}, "no_plan"),
        ({"plan": make_plan(t0=ANOW - timedelta(hours=3))}, "plan_expired"),
        (
            {
                "plan": {
                    **make_plan(status="skipped", t0=ANOW - timedelta(minutes=20)),
                    "slots": None,
                },
                "plan_status": "no_state",
            },
            "observer_no_state",
        ),
    ],
)
def test_no_plan_far_from_the_return_holds_heating_off_with_no_safe_state(kw, reason):
    clock = FakeClock(ANOW)
    store = _vac_world(
        clock, ANOW - timedelta(days=1), ANOW + timedelta(days=5), mode=HEAT_ON, rooms=16.0
    )
    actuator, ha, metrics = _make(clock, store)
    rec = _tick(actuator, ctx(clock, **kw))
    assert rec["fallback"] is None and rec["vacation_hold"] == reason
    assert rec["desired"]["source"] == "vacation_hold" and rec["desired"]["heating"] is False
    assert ha.writes() == [("heishamon_heatmode", HEAT_OFF)]
    assert len(ha.heartbeats()) == 1  # alive and holding on purpose: the watchdog stays quiet
    assert _count(metrics, "climate_actuator_vacation_fallbacks", reason=reason) == 1
    assert _count(metrics, "climate_actuator_fallbacks", reason=reason) == 0
    for _ in range(3):
        clock.advance(60)
        _tick(actuator, ctx(clock, **kw))
    assert _count(metrics, "climate_actuator_vacation_fallbacks", reason=reason) == 1
    assert actuator.snapshot()["vacation_fallback_active"] == 1.0


def test_the_hold_keeps_the_guard():
    clock = FakeClock(ANOW)
    rooms = {"bedroom": 11.0, "hall_kitchen": 16.0, "office_t": 16.0}
    store = _vac_world(clock, ANOW - timedelta(days=1), ANOW + timedelta(days=5), rooms=rooms)
    actuator, ha, _ = _make(clock, store)
    rec = _tick(actuator, ctx(clock, plan=None))
    assert rec["desired"]["source"] == "vacation_guard" and rec["desired"]["heating"] is True
    assert ("heishamon_heatmode", HEAT_ON) in ha.writes()


def test_within_the_look_ahead_of_the_return_no_plan_is_the_normal_safe_state():
    clock = FakeClock(ANOW)
    end = ANOW + timedelta(hours=40)  # ready_at - 48 h is already past
    store = _vac_world(clock, ANOW - timedelta(days=3), end, rooms=16.0)
    actuator, ha, metrics = _make(clock, store)
    rec = _tick(actuator, ctx(clock, plan=None))
    assert rec["fallback"] == "no_plan" and rec["desired"]["source"] == "safe_state"
    assert ("heishamon_heatmode", HEAT_ON) in ha.writes()
    assert _count(metrics, "climate_actuator_fallbacks", reason="no_plan") == 1
    assert _count(metrics, "climate_actuator_vacation_fallbacks", reason="no_plan") == 0


def test_the_hold_ends_at_ready_at_minus_the_look_ahead():
    clock = FakeClock(ANOW)
    end = ANOW + timedelta(hours=50, minutes=30)  # the switch is 30 min from now
    store = _vac_world(clock, ANOW - timedelta(days=3), end, mode=HEAT_OFF, rooms=16.0)
    actuator, ha, _ = _make(clock, store)
    assert _tick(actuator, ctx(clock, plan=None))["vacation_hold"] == "no_plan"
    clock.advance(31 * 60)
    rec = _tick(actuator, ctx(clock, plan=None))
    assert rec["vacation_hold"] is None and rec["fallback"] == "no_plan"
    assert ("heishamon_heatmode", HEAT_ON) in ha.writes()


def test_from_ready_at_the_normal_floors_apply_again():
    clock = FakeClock(ANOW)
    end = ANOW + timedelta(hours=1)  # ready_at an hour ago
    store = _vac_world(clock, ANOW - timedelta(days=3), end, rooms=16.0)
    actuator, ha, _ = _make(clock, store)
    rec = _tick(actuator, ctx(clock, make_plan(0, t0=ANOW - timedelta(minutes=20))))
    assert rec["observed"]["floors"] == dict.fromkeys(ROOMS, 21.0)
    assert rec["desired"]["source"] == "floor" and rec["desired"]["heating"] is True


def test_a_missing_room_reading_keeps_the_normal_fallback_while_away():
    clock = FakeClock(ANOW)
    store = _vac_world(clock, ANOW - timedelta(days=1), ANOW + timedelta(days=5), rooms=None)
    actuator, ha, _ = _make(clock, store)
    rec = _tick(actuator, ctx(clock, plan=None))
    assert rec["vacation_hold"] is None and rec["fallback"] == "no_plan"


def test_an_invalid_window_is_ignored_by_the_actuator():
    clock = FakeClock(ANOW)
    store = _vac_world(clock, ANOW + timedelta(days=5), ANOW - timedelta(days=1), rooms=16.0)
    actuator, ha, _ = _make(clock, store)
    rec = _tick(actuator, ctx(clock, plan=None))
    assert rec["fallback"] == "no_plan" and rec["observed"]["floors"] == dict.fromkeys(ROOMS, 21.0)


# ---------------------------------------------------------------------------
# Summary and metrics
# ---------------------------------------------------------------------------


def test_the_summary_always_carries_the_vacation_even_without_a_plan():
    mode = plan_export.resolve_mode("live", "off", live_allowed=True)
    v = vacation.view(window(NOW, NOW + timedelta(days=2), NOW), NOW, guard_c=12.0)
    body = plan_export.summary_response(None, enabled=True, mode=mode, now=NOW, vacation=v)
    assert body["state"] == "no_plan" and body["vacation"] == v and v["phase"] == "away"
    empty = plan_export.summary_response(None, enabled=True, mode=mode, now=NOW)
    assert empty["vacation"]["phase"] == "none"


def _families(view):
    from battery_optimizer.service.metrics import _climate_plan_families

    mode = plan_export.resolve_mode("live", "off", live_allowed=True)
    return {
        f.name: {tuple(s.labels.values()): s.value for s in f.samples}
        for f in _climate_plan_families(
            {"mode": mode, "state": "no_plan", "summary": None, "vacation": view}
        )
    }


def test_the_phase_and_invalid_gauges_are_one_hot_and_prelabelled():
    v = vacation.view(window(NOW, NOW + timedelta(days=2), NOW), NOW, guard_c=12.0)
    fam = _families(v)
    phase = fam["battery_optimizer_climate_vacation_phase"]
    assert phase == {(p,): (1.0 if p == "away" else 0.0) for p in vacation.PHASES}
    invalid = fam["battery_optimizer_climate_vacation_invalid"]
    assert invalid == {(r,): 0.0 for r in vacation.INVALID_REASONS}
    bad = vacation.view(window(NOW, NOW + timedelta(days=90), NOW), NOW, guard_c=12.0)
    assert _families(bad)["battery_optimizer_climate_vacation_invalid"][("too_long",)] == 1.0
    cancelled = vacation.view(window(NOW, NOW, NOW), NOW, guard_c=12.0)
    fam = _families(cancelled)
    assert set(fam["battery_optimizer_climate_vacation_invalid"].values()) == {0.0}
    assert fam["battery_optimizer_climate_vacation_phase"][("none",)] == 1.0


def test_the_actuator_vacation_metrics_are_exposed_and_prelabelled():
    from prometheus_client import generate_latest

    from battery_optimizer.service.climate_actuator import FALLBACK_REASONS

    clock = FakeClock(ANOW)
    store = _vac_world(clock, ANOW - timedelta(days=1), ANOW + timedelta(days=5), rooms=16.0)
    actuator, _ha, metrics = _make(clock, store)
    text = generate_latest(metrics.registry).decode()
    for reason in FALLBACK_REASONS:
        assert (
            f'battery_optimizer_climate_actuator_vacation_fallbacks_total{{reason="{reason}"}} 0.0'
            in text
        )
    _tick(actuator, ctx(clock, plan=None))
    from battery_optimizer.service.metrics import _climate_actuator_families

    fam = {f.name: f.samples for f in _climate_actuator_families(actuator.snapshot())}
    assert fam["battery_optimizer_climate_actuator_vacation_fallback_active"][0].value == 1.0
    assert fam["battery_optimizer_climate_actuator_guard_override_rooms"][0].value == 0.0


# ---------------------------------------------------------------------------
# The service: helpers in the store, the summary, the metrics, the re-decision on a change
# ---------------------------------------------------------------------------


def test_the_service_reads_the_helpers_and_serves_the_vacation(tmp_path):
    from service_fakes import FakeHAServer, ha_state
    from test_climate_plan_service import fetch, states
    from test_climate_shadow_service import RUN_AT, make_service, metric, run_synced, shutdown

    start, end = RUN_AT - timedelta(days=1), RUN_AT + timedelta(days=5)
    server = FakeHAServer(
        [
            *states("live"),
            ha_state(VS.start_entity, local(start), RUN_AT - timedelta(days=2)),
            ha_state(VS.end_entity, local(end), RUN_AT - timedelta(days=2)),
        ]
    )
    service = make_service(tmp_path, server, **{"climate_shadow.optimizer_mode": "shadow"})
    try:
        (summary,) = asyncio.run(run_synced(service, service.run_climate_shadow))
    finally:
        shutdown(service)
    assert summary["status"] in ("planned", "no_state", "no_inputs")
    body = fetch(service, "/climate/plan/summary").json()
    v = body["vacation"]
    assert v["phase"] == "away" and v["start"] == start.isoformat()
    assert v["end"] == end.isoformat() and v["invalid_reason"] is None and v["guard_c"] == 12.0
    assert metric(service, "climate_vacation_phase", phase="away") == 1.0
    assert metric(service, "climate_vacation_phase", phase="none") == 0.0
    assert metric(service, "climate_vacation_invalid", reason="too_long") == 0.0
    (decision,) = _records(tmp_path / "data", "climate_decisions")
    assert decision["inputs"]["vacation"]["phase"] == "away"
    assert [r["closed"] for r in _records(tmp_path / "data", "climate_vacations")] == [False]


def test_a_changed_helper_asks_for_a_new_decision_but_the_first_sync_does_not(tmp_path):
    from service_fakes import FakeHAServer
    from test_climate_shadow_service import RUN_AT, make_service

    service = make_service(tmp_path, FakeHAServer([]))
    first = entity_state(VS.end_entity, local(RUN_AT), RUN_AT)
    service._vacation_changed(first)
    assert not service._climate_rerun.is_set()
    service._vacation_changed(entity_state(VS.end_entity, local(RUN_AT), RUN_AT))
    assert not service._climate_rerun.is_set()  # the same value again
    later = entity_state(VS.end_entity, local(RUN_AT + timedelta(days=3)), RUN_AT)
    service._vacation_changed(later)
    assert service._climate_rerun.is_set()


def test_unavailable_helpers_keep_the_last_window_until_it_is_over():
    w = VacationWindow(NOW, NOW + timedelta(days=3), NOW + timedelta(days=3, hours=-2))
    gone = VacationRead(None, "helpers_unavailable")
    assert vacation.hold_last(gone, w, NOW + timedelta(hours=1)) == VacationRead(w, None)
    assert vacation.hold_last(gone, w, w.end) == gone
    assert vacation.hold_last(gone, None, NOW) == gone
    cancelled = VacationRead(None, None)  # a real cancel is never overridden
    assert vacation.hold_last(cancelled, w, NOW) == cancelled
    rejected = VacationRead(None, "end_before_start")
    assert vacation.hold_last(rejected, w, NOW) == rejected


def test_a_restarting_ha_does_not_end_the_vacation_of_a_decision(tmp_path):
    job = _job(tmp_path)
    now = T0 + timedelta(minutes=10)
    read = window(now - timedelta(days=1), now + timedelta(days=3), now)
    inputs = shadow.LiveInputs(regime="comfort", vacation=read)
    job.run_once(now, inputs)
    later = now + timedelta(hours=1)
    job.run_once(later, replace(inputs, vacation=VacationRead(None, "helpers_unavailable")))
    decisions = _records(tmp_path, "climate_decisions")
    assert [d["inputs"]["vacation"]["phase"] for d in decisions] == ["away", "away"]
    assert [r["closed"] for r in _records(tmp_path, "climate_vacations")] == [False]


def test_the_actuator_keeps_the_guard_while_the_helpers_are_unavailable():
    clock = FakeClock(ANOW)
    store = _vac_world(clock, ANOW - timedelta(days=1), ANOW + timedelta(days=5), rooms=16.0)
    actuator, _ha, _ = _make(clock, store)
    plan = make_plan(0, t0=ANOW - timedelta(minutes=20))
    assert _tick(actuator, ctx(clock, plan))["observed"]["vacation_absent"] is True
    clock.advance(60)
    for entity in (VS.start_entity, VS.end_entity):
        store.apply(entity_state(entity, "unavailable", clock.now()))
    rec = _tick(actuator, ctx(clock, plan))
    assert rec["observed"]["vacation_absent"] is True
    assert rec["observed"]["floors"] == dict.fromkeys(ROOMS, 12.0)
