"""Shadow runtime: readiness gating, bounded executor, DST horizons, intents, slot cycle."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import date, timedelta

import pytest
from service_fakes import (
    ENTITY_MODE,
    NO_BATTERY,
    NOW,
    TARIFF,
    TZ,
    FakeHAServer,
    entity_state,
    feed,
    service_config,
    utc,
    wait_until,
)

from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.planner import run_planner
from battery_optimizer.service.runtime import ShadowService
from battery_optimizer.service.timegrid import local_date, next_slot_boundary


def make_service(tmp_path, clock, *, entity_mode=False, planner=run_planner, **overrides):
    settings = {**(ENTITY_MODE if entity_mode else {}), **overrides}
    config = service_config(tmp_path, **settings)
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="test-sha",
        ws_transport=FakeHAServer(),
        planner=planner,
    )
    service.ha.stats.connected = True
    service.ha.stats.synced = True
    return service


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cleanup():
    services = []
    yield services.append
    for service in services:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Readiness and refusal to plan on stale inputs
# ---------------------------------------------------------------------------


def test_fresh_inputs_produce_a_persisted_shadow_plan(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, entity_mode=True)
    cleanup(service)
    feed(service, NOW)
    assert service.evaluate_inputs(NOW) == []
    assert run(service.request_solve("test")) == "ok"
    plan = service.plans[-1]
    first = next_slot_boundary(NOW)
    assert plan.plan.horizon_start == first
    assert plan.plan.expires_at == first + timedelta(minutes=30)
    assert all(i.shadow for i in plan.intents)
    assert plan.snapshot.soc_source == "entity" and plan.snapshot.soc_frac == pytest.approx(0.5)
    assert service.readiness(NOW).ready
    files = list((tmp_path / "data" / "service" / "v1" / "plans").rglob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["shadow"] is True and record["plan_id"] == plan.plan_id
    assert record["inputs_hash"] == plan.snapshot.inputs_hash()
    assert record["intents"][0]["valid_from"] == first.isoformat()


def test_the_committed_wear_cost_reaches_the_plan_snapshot(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, entity_mode=True)
    cleanup(service)
    feed(service, NOW)
    assert run(service.request_solve("test")) == "ok"
    snapshot = service.plans[-1].snapshot
    assert snapshot.throughput_cost_eur_per_kwh == 0.01
    assert snapshot.input_payload()["throughput_cost_eur_per_kwh"] == 0.01


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda s, c: c.advance(400), "grid_power_stale"),
        (lambda s, c: setattr(s.ha.stats, "connected", False), "ha_disconnected"),
        (lambda s, c: setattr(s.ha.stats, "synced", False), "ha_not_synced"),
        (lambda s, c: setattr(s, "price_curve", None), "prices_missing"),
        (lambda s, c: setattr(s, "history", None), "history_unavailable"),
        (lambda s, c: setattr(s, "tariff", None), "tariff_invalid"),
    ],
)
def test_stale_or_missing_inputs_refuse_to_plan(tmp_path, cleanup, mutate, reason):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, entity_mode=True)
    cleanup(service)
    feed(service, NOW)
    mutate(service, clock)
    assert run(service.request_solve("test")) == f"skipped:{reason}"
    assert not service.plans
    readiness = service.readiness()
    assert not readiness.ready and reason in readiness.reasons
    skipped = service.metrics.skipped.labels(reason=reason)._value.get()
    assert skipped == 1


def test_idle_soc_without_reports_still_plans(tmp_path, cleanup):
    # Omnibattery reports the SOC only on change: an idle battery's SOC is hours old but valid.
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, entity_mode=True)
    cleanup(service)
    old = NOW - timedelta(hours=3)
    feed(service, old)
    service.store.apply(
        entity_state(
            "sensor.shellyem_main_channel_1_power",
            380,
            NOW,
            attributes={"unit_of_measurement": "W"},
        )
    )
    assert service.config.freshness_for("battery_soc") is None
    assert run(service.request_solve("test")) == "ok"
    assert service.plans[-1].snapshot.soc_source == "entity"


@pytest.mark.parametrize(
    ("entity", "state", "planned"),
    [
        ("sensor.omnibattery_non_responsive_batteries", "None", True),
        ("sensor.omnibattery_non_responsive_batteries", "1", False),
        ("sensor.omnibattery_non_responsive_batteries", "unavailable", False),
        ("sensor.test_soc", "unavailable", False),
        ("sensor.test_soc", "140", False),
    ],
)
def test_entity_soc_needs_availability_and_a_responsive_battery(
    tmp_path, cleanup, entity, state, planned
):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, entity_mode=True)
    cleanup(service)
    feed(service, NOW)
    clock.advance(1)
    service.store.apply(
        entity_state(entity, state, clock.now(), attributes={"unit_of_measurement": "%"})
    )
    expected = "ok" if planned else "skipped:soc_stale"
    assert run(service.request_solve("test")) == expected


def test_liveness_is_not_required_when_unconfigured(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(
        tmp_path, clock, entity_mode=True, **{"actuation.entities.battery_liveness": None}
    )
    cleanup(service)
    assert "sensor.omnibattery_non_responsive_batteries" not in service.store.tracked
    feed(service, NOW)
    assert run(service.request_solve("test")) == "ok"


def test_stale_soc_refuses_to_plan_when_an_age_limit_is_set(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, entity_mode=True, **{"freshness_s.battery_soc": 900})
    cleanup(service)
    old = NOW - timedelta(seconds=1000)
    feed(service, old)  # SOC (and everything else) last reported 1000 s ago
    service.store.apply(
        entity_state(
            "sensor.shellyem_main_channel_1_power",
            380,
            NOW,
            attributes={"unit_of_measurement": "W"},
        )
    )
    assert run(service.request_solve("test")) == "skipped:soc_stale"
    assert not service.plans
    # A REST refresh re-confirming the same value (newer last_reported) makes it fresh.
    service.store.apply(
        entity_state(
            "sensor.test_soc", 50, old, reported=NOW, attributes={"unit_of_measurement": "%"}
        )
    )
    assert run(service.request_solve("test")) == "ok"


def test_prices_running_out_refuse_to_plan(tmp_path, cleanup):
    late = utc(2026, 10, 20, 21, 20)  # 23:20 CEST, no next-day prices yet
    clock = FakeClock(late)
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, late)
    assert run(service.request_solve("test")) == "skipped:prices_insufficient"
    feed(service, late, tomorrow=True)
    assert run(service.request_solve("test")) == "ok"
    assert service.plans[-1].plan.n == 2 + 96  # 21:30-22:00Z today, then all of tomorrow


# ---------------------------------------------------------------------------
# Bounded executor
# ---------------------------------------------------------------------------


def test_solves_never_overlap_and_timeouts_keep_the_worker(tmp_path, cleanup):
    gate = threading.Event()

    def slow(snapshot):
        gate.wait(10)
        return run_planner(snapshot)

    clock = FakeClock(NOW)
    service = make_service(
        tmp_path,
        clock,
        planner=slow,
        **{"planning.solve_timeout_s": 0.3, "planning.solver_time_limit_s": 0.2},
    )
    cleanup(service)
    feed(service, NOW)

    async def go():
        first = asyncio.create_task(service.request_solve("first"))
        await wait_until(lambda: service._worker_busy)
        assert await service.request_solve("second") == "skipped:busy"
        assert await first == "timeout"
        # The thread is still running: a new solve must not start a second worker.
        assert await service.request_solve("third") == "skipped:busy"
        gate.set()
        await wait_until(lambda: not service._worker_busy)
        assert await service.request_solve("fourth") == "ok"

    run(go())
    m = service.metrics
    assert m.skipped.labels(reason="busy")._value.get() == 2
    assert m.solves.labels(result="timeout")._value.get() == 1
    assert m.solves.labels(result="ok")._value.get() == 1
    assert len(service.plans) == 1


def test_late_plan_is_discarded_as_obsolete(tmp_path, cleanup):
    clock = FakeClock(NOW)

    def slow_clock(snapshot):
        clock.advance(15 * 60)  # the first slot has started by the time it lands
        return run_planner(snapshot)

    service = make_service(tmp_path, clock, planner=slow_clock)
    cleanup(service)
    feed(service, NOW)
    assert run(service.request_solve("test")) == "discarded:obsolete"
    assert not service.plans


def test_planner_error_is_reported(tmp_path, cleanup):
    def broken(snapshot):
        raise RuntimeError("solver exploded")

    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, planner=broken)
    cleanup(service)
    feed(service, NOW)
    assert run(service.request_solve("test")) == "error"
    assert "solver exploded" in service.last_solve["error"]
    assert not service.plans


# ---------------------------------------------------------------------------
# Intents: time-bounded, expired ones never current
# ---------------------------------------------------------------------------


def test_old_and_expired_intents_are_never_current(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, NOW)
    assert run(service.request_solve("test")) == "ok"
    plan = service.plans[-1]
    first = plan.plan.horizon_start
    step = timedelta(minutes=15)
    assert service.decision_for_slot(first)[1] == "intent"
    assert service.decision_for_slot(first + step)[1] == "intent"
    # TTL is two slots: the third slot's intent exists in the plan but is not actionable.
    assert service.decision_for_slot(first + 2 * step) == (None, "plan_expired")
    beyond = [i for i in plan.intents if i.valid_from >= plan.plan.expires_at]
    assert beyond and not any(i.actionable for i in beyond)
    assert not any(i.is_current(i.valid_from) for i in beyond)
    # A plan made before the slot it is asked about is required.
    assert service.decision_for_slot(NOW - step) == (None, "no_plan")


def test_newest_plan_wins_and_inputs_hash_tracks_inputs(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, entity_mode=True)
    cleanup(service)
    feed(service, NOW, soc=0.5)
    run(service.request_solve("a"))
    clock.advance(30)
    feed(service, clock.now(), soc=0.9)
    run(service.request_solve("b"))
    a, b = service.plans
    assert b.plan.plan_sequence > a.plan.plan_sequence
    assert a.inputs_hash != b.inputs_hash
    intent, decision = service.decision_for_slot(b.plan.horizon_start)
    assert decision == "intent" and intent.plan_id == b.plan_id


# ---------------------------------------------------------------------------
# DST: horizons are UTC slot runs over 23/25-hour local days
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "day", "slots"),
    [
        # 2026-10-25 fall-back: 02:50 CEST (first pass of the repeated hour)
        (utc(2026, 10, 25, 0, 50), date(2026, 10, 25), 88),
        # 2026-10-25 fall-back: 02:50 CET (second pass), one hour of UTC later
        (utc(2026, 10, 25, 1, 50), date(2026, 10, 25), 84),
        # 2026-03-29 spring-forward: 01:50 CET, 02:00-03:00 does not exist
        (utc(2026, 3, 29, 0, 50), date(2026, 3, 29), 84),
    ],
)
def test_dst_days_plan_contiguous_utc_slots(tmp_path, cleanup, now, day, slots):
    clock = FakeClock(now)
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, now)
    assert service.price_curve.reference_day == day
    assert run(service.request_solve("dst")) == "ok"
    plan = service.plans[-1]
    starts = [i.valid_from for i in plan.intents]
    assert len(starts) == slots == len(set(starts))
    assert all(b - a == timedelta(minutes=15) for a, b in zip(starts, starts[1:], strict=False))
    _, day_end = (plan.plan.horizon_start, plan.plan.horizon_end)
    assert local_date(day_end - timedelta(seconds=1), TZ) == day
    assert plan.snapshot.forecast_meta["days"][0]["slots_zero_no_history"] == 0


# ---------------------------------------------------------------------------
# Slot cycle: open (decision frozen), close (measurements + replay), sink rows
# ---------------------------------------------------------------------------


def _slot_rows(tmp_path):
    rows = []
    for path in (tmp_path / "data" / "service" / "v1" / "slots").rglob("*.jsonl"):
        rows += [json.loads(line) for line in path.read_text().splitlines()]
    return rows


def test_slot_cycle_records_decision_measurements_and_counterfactual(tmp_path, cleanup):
    clock = FakeClock(NOW)
    # Gross load = grid here; with the battery mapped it also needs the battery counters.
    service = make_service(tmp_path, clock, **NO_BATTERY)
    cleanup(service)
    feed(service, NOW, energy=1000.0)
    assert run(service.request_solve("test")) == "ok"
    first = service.plans[-1].plan.horizon_start
    clock.set(first)
    feed(service, first, grid=800.0, energy=1000.05)
    opened = run(service.open_slot(first))
    assert opened.decision == "intent" and opened.inputs_ready
    for minute in (5, 10, 15):
        at = first + timedelta(minutes=minute)
        feed(service, at, grid=800.0, energy=1000.05 + 0.2 * minute / 15)
    clock.set(first + timedelta(minutes=15, seconds=20))
    record = run(service.close_slot(first))
    assert record["decision"] == "intent"
    assert record["measured"]["grid_energy_source"] == "counter"
    assert record["measured"]["grid_kwh"] == pytest.approx(0.2)
    assert record["price_eur_per_kwh"] == service.price_curve.price_at(first)
    assert record["virtual"]["applied"] is True
    assert record["native_cost_eur"] == pytest.approx(record["price_eur_per_kwh"] * 0.2)
    assert record["counterfactual_cost_eur"] is not None
    rows = _slot_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["slot_start"] == first.isoformat()
    assert rows[0]["shadow"] is True


def test_not_ready_at_slot_start_means_no_decision(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, NOW)
    run(service.request_solve("test"))
    first = service.plans[-1].plan.horizon_start
    clock.set(first)
    service.ha.stats.connected = False  # e.g. HA dropped just before the boundary
    opened = run(service.open_slot(first))
    assert opened.intent is None and opened.decision == "not_ready"
    assert "ha_disconnected" in opened.reasons
    assert service.current_intent(first) is None


def test_plans_slots_and_repeated_skips_are_logged_once(tmp_path, cleanup, caplog):
    caplog.set_level("INFO", logger="battery_optimizer.service.runtime")
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **NO_BATTERY)
    cleanup(service)
    assert run(service.request_solve("scheduled")).startswith("skipped:")
    assert run(service.request_solve("scheduled")).startswith("skipped:")
    skips = [r.getMessage() for r in caplog.records if r.getMessage().startswith("solve skipped")]
    assert len(skips) == 1  # the same reason again is not repeated at INFO
    feed(service, NOW)
    assert run(service.request_solve("test")) == "ok"
    plan = service.plans[-1]
    first = plan.plan.horizon_start
    intent = plan.intent_for_slot(first)
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith(f"plan generated plan_id={plan.plan_id} trigger=test status=")
        and f"first_slot={first.isoformat()} first_mode={intent.mode}" in m
        and "soc_source=simulated" in m
        for m in lines
    )
    clock.set(first)
    feed(service, first)
    caplog.clear()
    run(service.open_slot(first))
    assert [r.getMessage() for r in caplog.records] == [
        f"slot opened start={first.isoformat()} decision=intent mode={intent.mode} "
        f"power_w={intent.target_power_w:.0f} plan_id={plan.plan_id} not_ready=-"
    ]
    caplog.clear()
    service.ha.stats.connected = False
    run(service.open_slot(first + timedelta(minutes=15)))
    (line,) = [r.getMessage() for r in caplog.records]
    assert "decision=not_ready mode=- power_w=- plan_id=-" in line
    assert "not_ready=ha_disconnected" in line


def test_scheduler_solves_before_the_boundary_and_opens_on_it(tmp_path, cleanup):
    clock = FakeClock(NOW)  # 10:50, boundary 11:00, solve due at 10:58
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, NOW)
    # Started mid-slot: the 10:45 slot opens without a plan and one catch-up solve fills it.
    delay = run(service.scheduler_tick())
    assert delay == pytest.approx(8 * 60)
    assert service.last_solve["trigger"] == "catch_up" and len(service.plans) == 1
    assert service._slot_open[utc(2026, 10, 20, 10, 45)].decision == "late_intent"
    clock.advance(delay)
    feed(service, clock.now())
    delay = run(service.scheduler_tick())
    assert service.last_solve["trigger"] == "scheduled" and len(service.plans) == 2
    assert delay == pytest.approx(120)
    clock.advance(delay)
    feed(service, clock.now())
    run(service.scheduler_tick())
    opened = service._slot_open[utc(2026, 10, 20, 11, 0)]
    assert opened.decision == "intent" and opened.intent.plan_id == service.plans[-1].plan_id
    assert service.current_intent() is not None


# ---------------------------------------------------------------------------
# Late adoption: a slot that opened without an intent takes one later in the slot
# ---------------------------------------------------------------------------

SLOT = utc(2026, 10, 20, 11, 0)  # the first slot of a plan solved at NOW
CURRENT = utc(2026, 10, 20, 10, 45)  # the slot in progress at NOW


def _not_ready_open(tmp_path, cleanup, **overrides):
    """A plan for SLOT, then SLOT opens while the day's history is missing."""
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **overrides)
    cleanup(service)
    feed(service, NOW, energy=1000.0)
    assert run(service.request_solve("scheduled")) == "ok"
    plan = service.plans[-1]
    clock.set(SLOT)
    feed(service, SLOT, energy=1000.0)
    history, service.history = service.history, None  # e.g. the local-midnight refresh
    opened = run(service.open_slot(SLOT))
    assert opened.decision == "not_ready" and opened.intent is None
    assert "history_unavailable" in opened.reasons
    return clock, service, plan, history


def _open_without_intent(service, slot_start):
    service.ha.stats.synced = False
    opened = run(service.open_slot(slot_start))
    service.ha.stats.synced = True
    assert opened.intent is None
    return opened


def test_not_ready_slot_adopts_the_plan_once_inputs_are_ready(tmp_path, cleanup, caplog):
    caplog.set_level("INFO", logger="battery_optimizer.service.runtime")
    clock, service, plan, history = _not_ready_open(tmp_path, cleanup, **NO_BATTERY)
    assert service.actuation_context(SLOT).intent is None
    # Still not ready: nothing adopted, and the scheduler comes back soon to look again.
    clock.set(SLOT + timedelta(seconds=30))
    feed(service, clock.now(), energy=1000.0, history=False)
    delay = run(service.scheduler_tick())
    assert service._slot_open[SLOT].intent is None and delay <= 15
    # Ready again a minute in: the plan made before the slot is adopted for the rest of it.
    clock.set(SLOT + timedelta(seconds=60))
    feed(service, clock.now(), energy=1000.0, history=False)
    service.history = history
    caplog.clear()
    run(service.scheduler_tick())
    opened = service._slot_open[SLOT]
    intent = plan.intent_for_slot(SLOT)
    assert opened.decision == "late_intent" and opened.decision_at_open == "not_ready"
    assert opened.intent == intent and opened.adopted_at == SLOT + timedelta(seconds=60)
    assert len(service.plans) == 1  # a covering plan existed: no extra solve
    assert f"slot adopted start={SLOT.isoformat()} mode={intent.mode} " in caplog.text
    assert f"plan_id={plan.plan_id} after_s=60 decision_at_open=not_ready" in caplog.text
    ctx = service.actuation_context(clock.now())
    assert ctx.intent == intent and ctx.plan_from_this_process
    assert service.current_intent() == intent
    # The slot record is honest about the late start: energy only for the 14 minutes left.
    opened.intent = replace(intent, mode="charge", target_power_w=1200.0, signed_power_w=1200.0)
    for minute in (5, 10, 15):
        feed(service, SLOT + timedelta(minutes=minute), energy=1000.0 + 0.1 * minute / 15)
    clock.set(SLOT + timedelta(minutes=15, seconds=20))
    record = run(service.close_slot(SLOT))
    assert record["decision"] == "late_intent" and record["decision_at_open"] == "not_ready"
    assert record["adopted_at"] == (SLOT + timedelta(seconds=60)).isoformat()
    assert record["adopted_after_s"] == 60.0
    assert record["inputs_ready_at_open"] is False
    assert record["not_ready_reasons"] == ["history_unavailable"]
    assert record["intent"]["plan_id"] == plan.plan_id
    assert record["intent"]["target_power_w"] == 1200.0  # the intent itself, not pro-rated
    assert record["planned_charge_kwh"] == pytest.approx(0.3 * 14 / 15)  # 1.2 kW, 14 min
    assert record["planned_discharge_kwh"] == 0.0
    assert record["virtual"]["charge_kwh"] == pytest.approx(0.3 * 14 / 15)
    assert service.metrics.shadow_slots.labels(decision="late_intent")._value.get() == 1


def test_a_slot_with_its_intent_at_open_records_no_adoption(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **NO_BATTERY)
    cleanup(service)
    feed(service, NOW, energy=1000.0)
    assert run(service.request_solve("scheduled")) == "ok"
    clock.set(SLOT)
    feed(service, SLOT, energy=1000.0)
    run(service.open_slot(SLOT))
    clock.set(SLOT + timedelta(minutes=15, seconds=20))
    record = run(service.close_slot(SLOT))
    assert record["decision"] == record["decision_at_open"] == "intent"
    assert record["adopted_at"] is None and record["adopted_after_s"] is None


def test_not_ready_slot_is_not_adopted_while_inputs_stay_stale(tmp_path, cleanup):
    clock, service, _, _ = _not_ready_open(tmp_path, cleanup)
    clock.set(SLOT + timedelta(minutes=5))
    feed(service, clock.now(), history=False)
    assert not service.adopt_late()
    assert run(service.scheduler_tick()) <= 15
    assert service._slot_open[SLOT].intent is None
    assert service.actuation_context(clock.now()).decision == "not_ready"


def test_expired_plan_is_never_adopted(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, NOW)
    assert run(service.request_solve("scheduled")) == "ok"
    late = SLOT + 2 * timedelta(minutes=15)  # the plan expired at this slot's start
    clock.set(late + timedelta(seconds=30))
    feed(service, clock.now())
    _open_without_intent(service, late)
    assert not service.adopt_late()
    assert service._slot_open[late].intent is None


def test_a_slot_that_opened_with_an_intent_is_never_replanned(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, entity_mode=True)
    cleanup(service)
    feed(service, NOW, soc=0.5)
    assert run(service.request_solve("scheduled")) == "ok"
    first = service.plans[-1]
    clock.set(SLOT)
    feed(service, SLOT, soc=0.5)
    assert run(service.open_slot(SLOT)).decision == "intent"
    # A mid-slot event solve plans from the next boundary and leaves the slot alone.
    clock.set(SLOT + timedelta(minutes=3))
    feed(service, clock.now(), soc=0.95)
    assert run(service.request_solve("event:soc_change")) == "ok"
    assert service.plans[-1].plan.horizon_start == SLOT + timedelta(minutes=15)
    assert not service.adopt_late()
    run(service.scheduler_tick())
    opened = service._slot_open[SLOT]
    assert opened.decision == "intent" and opened.adopted_at is None
    assert opened.intent.plan_id == first.plan_id


def test_restart_mid_slot_catches_up_with_a_plan_for_the_slot_in_progress(tmp_path, cleanup):
    clock = FakeClock(NOW)  # a fresh process at 10:50, inside the 10:45 slot
    service = make_service(tmp_path, clock, entity_mode=True)
    cleanup(service)
    service.ha.stats.synced = False  # the first tick runs before the HA sync
    run(service.scheduler_tick())
    assert service._slot_open[CURRENT].decision == "not_ready" and not service.plans
    service.ha.stats.synced = True
    feed(service, NOW)
    # The resync's event solve is a catch-up plan that starts in the slot in progress.
    assert run(service.request_solve("event:resync")) == "ok"
    plan = service.plans[-1]
    assert plan.plan.horizon_start == CURRENT
    assert plan.plan.expires_at == CURRENT + timedelta(minutes=30)
    opened = service._slot_open[CURRENT]
    assert opened.decision == "late_intent" and opened.intent.plan_id == plan.plan_id
    assert opened.adopted_at == NOW
    ctx = service.actuation_context(NOW)
    assert ctx.intent is not None and ctx.plan_from_this_process
    # The next slot is served by the scheduled solve as before.
    clock.set(utc(2026, 10, 20, 10, 58))
    feed(service, clock.now())
    run(service.scheduler_tick())
    assert service.last_solve["trigger"] == "scheduled"
    assert service.plans[-1].plan.horizon_start == SLOT


def test_no_catch_up_plan_in_the_last_solve_lead_of_a_slot(tmp_path, cleanup):
    clock = FakeClock(utc(2026, 10, 20, 10, 59))
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, clock.now())
    _open_without_intent(service, CURRENT)
    assert run(service.request_solve("event:resync")) == "ok"
    assert service.plans[-1].plan.horizon_start == SLOT
    assert service._slot_open[CURRENT].intent is None


def test_catch_up_plan_is_kept_inside_its_slot_and_discarded_after_it(tmp_path, cleanup):
    clock = FakeClock(NOW)
    solve_s = {"s": 60}

    def slow(snapshot):
        clock.advance(solve_s["s"])
        return run_planner(snapshot)

    service = make_service(tmp_path, clock, planner=slow)
    cleanup(service)
    feed(service, NOW)
    _open_without_intent(service, CURRENT)
    assert run(service.request_solve("event:resync")) == "ok"  # lands at 10:51
    assert service._slot_open[CURRENT].adopted_at == NOW + timedelta(seconds=60)
    # Another slot without an intent; this catch-up solve lands after its slot has ended.
    later = utc(2026, 10, 20, 11, 30)
    clock.set(later + timedelta(minutes=5))
    feed(service, clock.now())
    _open_without_intent(service, later)
    solve_s["s"] = 15 * 60
    assert run(service.request_solve("event:resync")) == "discarded:obsolete"
    assert service._slot_open[later].intent is None
