"""Shadow runtime: readiness gating, bounded executor, DST horizons, intents, slot cycle."""

from __future__ import annotations

import asyncio
import json
import threading
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


def test_scheduler_solves_before_the_boundary_and_opens_on_it(tmp_path, cleanup):
    clock = FakeClock(NOW)  # 10:50, boundary 11:00, solve due at 10:58
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, NOW)
    delay = run(service.scheduler_tick())
    assert delay == pytest.approx(8 * 60)
    assert not service.plans
    clock.advance(delay)
    feed(service, clock.now())
    delay = run(service.scheduler_tick())
    assert service.last_solve["trigger"] == "scheduled" and len(service.plans) == 1
    assert delay == pytest.approx(120)
    clock.advance(delay)
    feed(service, clock.now())
    run(service.scheduler_tick())
    assert service._slot_open[utc(2026, 10, 20, 11, 0)].decision == "intent"
    assert service.current_intent() is not None
