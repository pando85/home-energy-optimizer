"""The periodic full charge in the service: evaluation, commit plan, directive, state, status.

The pure rule and state machine are in ``test_full_charge_scheduler.py``; the actuator side of the
settle hold is in ``test_service_actuator.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest
from service_fakes import TARIFF, TZ, FakeHAServer, entity_state, feed, service_config, utc
from test_service_actuator import (
    CAP_ATTRS,
    ENTS,
    FC_ENTS,
    LIVE,
    NUMBER_ATTRS,
    FakeHA,
)

from battery_optimizer.service.actuator import WEEKLY_FULL_CHARGE_STATUS
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.ha_client import HARestClient
from battery_optimizer.service.ha_guard import WEEKDAY_OPTIONS
from battery_optimizer.service.runtime import ShadowService
from battery_optimizer.service.sink import ShadowSink
from battery_optimizer.service.timegrid import floor_slot, next_slot_boundary

# 2026-10-20 19:00Z = 21:00 CEST on a Tuesday: tomorrow's prices are published.
EVENING = utc(2026, 10, 20, 19, 0)
TOMORROW = date(2026, 10, 21)  # a Wednesday
BOOTSTRAP = "2026-10-11"  # L: tomorrow is d = 10, the first day of the unknown row's band
ON = {"full_charge.enabled": True, "full_charge.bootstrap_last_full": BOOTSTRAP}


def make_service(tmp_path, clock, *, mode="live", tz_ok=True, feature_mode=None, **overrides):
    """``feature_mode`` (``full_charge.mode``) defaults to live for a live service, else shadow."""
    feature_mode = feature_mode or ("live" if mode == "live" else "shadow")
    config = service_config(
        tmp_path, **{**LIVE, "mode": mode, **ON, "full_charge.mode": feature_mode, **overrides}
    )
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="test-sha",
        ws_transport=FakeHAServer(),
        rest_transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )
    service.ha.stats.connected = True
    service.ha.stats.synced = True
    service.rest = None  # no REST refresh in unit tests
    service._fc_tz_ok = tz_ok
    if tz_ok:
        service.full_charge.suspend(None)  # as after the first tick that finds the zone verified
    # The tests live before the real commissioning date (config/service.yaml): lift that guard.
    service.full_charge._earliest = None
    return service


@pytest.fixture
def cleanup():
    services = []
    yield services.append
    for service in services:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)


def world(
    service,
    clock,
    *,
    soc=50.0,
    status="balanced",
    weekly="off",
    day="monday",
    delay="off",
    read_at=None,
    balance="unknown",
    trend="stable",
    options=WEEKDAY_OPTIONS,
):
    """Every entity the actuator and the scheduler read, as of ``clock.now()``."""
    at = clock.now()
    store = service.store
    for entity, value, attrs in (
        (ENTS.battery_ac_power, 0, {}),
        (ENTS.battery_soc, soc, {}),
        (ENTS.battery_liveness, "None", {}),
        (ENTS.integration_status, status, {}),
        (ENTS.kill_switch, "on", {}),
        (ENTS.allow_charge, "off", {}),
        (ENTS.allow_discharge, "off", {}),
        (ENTS.power_limits, "off", {}),
        (ENTS.pd_target, 0, NUMBER_ATTRS),
        (ENTS.max_discharge_power, 0, CAP_ATTRS),
        (FC_ENTS.enable_switch, weekly, {}),
        (FC_ENTS.day_select, day, {"options": list(options)} if options else {}),
        (FC_ENTS.delay_switch, delay, {}),
        (FC_ENTS.charge_delay_switch, "off", {}),
        (FC_ENTS.last_balance_read, read_at.isoformat() if read_at else "unknown", {}),
        (FC_ENTS.balance_status, balance, {}),
        (FC_ENTS.delta_trend, trend, {}),
        (FC_ENTS.delta_avg_4w, 187.5, {}),
        (FC_ENTS.cell_delta, 183, {}),
        (FC_ENTS.balancing_mode, "off", {}),
    ):
        store.apply(entity_state(entity, value, at, attributes=attrs))
    feed(service, at, soc=soc / 100.0)


def run(coro):
    return asyncio.run(coro)


def records(tmp_path, kind="full_charge"):
    root = tmp_path / "data" / "service" / "v1" / kind
    rows = [
        json.loads(line) for path in root.rglob("*.jsonl") for line in path.read_text().splitlines()
    ]
    return sorted(rows, key=lambda row: row["at"])


def state_file(tmp_path):
    return json.loads(
        (tmp_path / "data" / "service" / "v1" / "state" / "full_charge.json").read_text()
    )


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_the_feature_is_off_by_default_and_changes_no_surface(tmp_path, cleanup):
    clock = FakeClock(EVENING)
    config = service_config(tmp_path, **LIVE)
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="t",
        ws_transport=FakeHAServer(),
    )
    cleanup(service)
    assert service.full_charge is None and not config.full_charge.enabled
    assert "full_charge" not in service.status()
    assert service.actuation_context(clock.now()).full_charge is None
    text = service.metrics.render().decode()
    assert "battery_optimizer_full_charge" not in text
    assert not hasattr(service.metrics, "full_charge_runs")
    # the entities are not subscribed to either
    assert FC_ENTS.enable_switch not in service.store.tracked
    assert not (tmp_path / "data" / "service" / "v1" / "state" / "full_charge.json").exists()
    run(service.full_charge_tick())  # a no-op


def test_a_live_service_with_the_feature_in_shadow_changes_nothing_in_the_control_path(
    tmp_path, cleanup
):
    clock = FakeClock(utc(2026, 10, 20, 23, 50))  # 01:50 CEST on the run's day
    service = make_service(tmp_path, clock, mode="live", feature_mode="shadow")
    cleanup(service)
    fake = FakeHA(service.store, clock)
    service.actuator._send = fake.send
    fc = service.full_charge
    assert service.config.full_charge_isolated and fc.writer_mode == "simulated"
    assert service.actuator._fc is None  # the actuator never hears of the feature
    world(service, clock)
    feed(service, clock.now())
    fc.record_evaluation(TOMORROW, {"forcing_cost_eur": 0.0, "shortfall_kwh": 0}, clock.now())
    service._full_charge_arm_due = lambda now: True
    weekly = (FC_ENTS.enable_switch, FC_ENTS.day_select)

    async def go():
        await service.full_charge_tick()
        assert fc.state.state == "armed"  # the simulated run advances as in dry_run
        assert service._fc_directive is None  # ... but nothing is handed to the actuator
        assert service.actuation_context(clock.now()).full_charge is None
        assert await service.request_solve("t") == "ok"
        # the regular plan stays the plan of record: no target plan is ever committed
        assert service.plans[-1].snapshot.full_charge is None
        assert service._commit_window(service.plans[-1].snapshot.slot_starts) is None
        first = next_slot_boundary(clock.now())
        clock.set(first + timedelta(seconds=1))
        world(service, clock)
        feed(service, clock.now())
        await service.open_slot(first)
        record = await service.actuator.tick(service.actuation_context(clock.now()))
        assert record["guard_reasons"] == [] and service.actuator.full_charge_refusal is None
        assert "weekly_enable" not in service.actuator.readings
        assert "full_charge" not in service.actuator.view()
        # Omnibattery's weekly status is never healthy here: the guard holds safe idle
        world_clock_step(service, clock)
        world(service, clock, status=WEEKLY_FULL_CHARGE_STATUS, weekly="on", day="wednesday")
        feed(service, clock.now())
        record = await service.actuator.tick(service.actuation_context(clock.now()))
        assert "integration_unhealthy" in record["guard_reasons"]
        assert record["desired"]["mode"] == "safe_idle"
        # the day ends: a simulated outcome, nothing sent
        clock.set(utc(2026, 10, 21, 22, 1))
        world(service, clock)
        await service.full_charge_tick()
        assert service._fc_directive is None

    run(go())
    assert [c for c in fake.calls if c[2] in weekly] == []
    assert fc.state.last_outcome["outcome"] == "done_simulated"
    assert fc.state.last_full_source == "simulated"
    assert state_file(tmp_path)["writer_mode"] == "simulated"
    status = service.status()["full_charge"]
    assert status["mode"] == "shadow" and "beside live control" in status["writer"]
    assert status["directive"] is None and status["actuator"] is None
    text = service.metrics.render().decode()
    assert 'battery_optimizer_full_charge_mode{mode="shadow"} 1.0' in text
    assert 'battery_optimizer_full_charge_mode{mode="live"} 0.0' in text
    assert 'battery_optimizer_full_charge_runs_total{outcome="done_simulated"} 1.0' in text
    assert 'battery_optimizer_actuator_writes_total{result="ok",role="full_charge_day"} 0.0' in text
    from battery_optimizer.service.ha_guard import actuation_allowlist

    allow = actuation_allowlist(service.config)
    assert FC_ENTS.enable_switch not in allow.switches and not allow.selects


def world_clock_step(service, clock):
    clock.advance(1)


def test_feature_live_in_a_live_service_commits_and_arms(tmp_path, cleanup):
    clock = FakeClock(EVENING)
    service = make_service(tmp_path, clock, mode="live", feature_mode="live")
    cleanup(service)
    assert not service.config.full_charge_isolated
    assert service.config.full_charge_writer_mode == "live"
    assert service.actuator._fc is service.config.full_charge
    assert (
        'battery_optimizer_full_charge_mode{mode="live"} 1.0' in service.metrics.render().decode()
    )
    assert service.status()["full_charge"]["mode"] == "live"


def test_enabled_in_every_mode_it_tracks_the_entities_once(tmp_path, cleanup):
    for mode in ("shadow", "dry_run", "live"):
        clock = FakeClock(EVENING)
        (tmp_path / mode).mkdir()
        service = make_service(tmp_path / mode, clock, mode=mode)
        cleanup(service)
        tracked = service.store.tracked
        assert set(FC_ENTS.all()) <= set(tracked)
        assert ENTS.integration_status in tracked
        # the balance sensors are shared with battery_health: one store entry each
        assert len(tracked) == len(set(tracked))
        assert service.full_charge.writer_mode == ("live" if mode == "live" else "simulated")
        assert (service.actuator is None) == (mode == "shadow")
        assert "battery_optimizer_full_charge_state" in service.metrics.render().decode()


# ---------------------------------------------------------------------------
# Gating: time zone, entity contract, sync
# ---------------------------------------------------------------------------


def rest_service(tmp_path, clock, time_zone):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if isinstance(time_zone, Exception):
            raise time_zone
        return httpx.Response(200, json={"time_zone": time_zone, "version": "2026.10"})

    service = make_service(tmp_path, FakeClock(EVENING), tz_ok=None)
    service.rest = HARestClient(
        settings=service.config.home_assistant, transport=httpx.MockTransport(handler)
    )
    return service, calls


def test_a_time_zone_mismatch_disables_the_feature_and_alerts(tmp_path, cleanup):
    service, calls = rest_service(tmp_path, FakeClock(EVENING), "America/New_York")
    cleanup(service)
    world(service, service.clock)
    assert service.config.timezone == "Europe/Madrid"
    assert run(service.verify_ha_timezone()) is False and calls == ["/api/config"]
    run(service.full_charge_tick())
    assert service.full_charge.suspended == "timezone_mismatch"
    assert service._fc_directive is None  # hands off: neither armed nor disarmed
    status = service.status()["full_charge"]
    assert status["time_zone"] == {
        "service": "Europe/Madrid",
        "home_assistant": "America/New_York",
        "ok": False,
    }
    assert "timezone_mismatch" in status["alerts"] and status["suspended"] == "timezone_mismatch"
    text = service.metrics.render().decode()
    assert 'battery_optimizer_full_charge_alert{reason="timezone_mismatch"} 1.0' in text
    # no decision is ever made while suspended
    assert service.full_charge.evaluation_wanted(service.clock.now(), True) is None
    assert run(service.request_solve("t")) == "ok" and service.full_charge.state.state == "idle"
    service.close_rest = None
    events = [
        json.loads(line)
        for p in (tmp_path / "data").rglob("events.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert any(e["kind"] == "full_charge_timezone_mismatch" for e in events)


def test_a_matching_time_zone_lets_the_feature_run(tmp_path, cleanup):
    service, _ = rest_service(tmp_path, FakeClock(EVENING), "Europe/Madrid")
    cleanup(service)
    world(service, service.clock)
    assert run(service.verify_ha_timezone()) is True
    run(service.full_charge_tick())
    assert service.full_charge.suspended is None
    assert service._fc_directive is not None and service._fc_directive.armed is False
    assert "timezone_mismatch" not in service.status()["full_charge"]["alerts"]


def test_an_unanswered_time_zone_check_keeps_the_feature_suspended_and_is_retried(
    tmp_path, cleanup
):
    service, calls = rest_service(tmp_path, FakeClock(EVENING), httpx.ConnectError("down"))
    cleanup(service)
    world(service, service.clock)
    assert run(service.verify_ha_timezone()) is None and service._fc_tz_ok is None
    run(service.full_charge_tick())
    assert service.full_charge.suspended == "timezone_unverified"
    assert service._fc_directive is None
    assert service.status()["full_charge"]["time_zone"]["ok"] is None


def test_nothing_is_decided_or_committed_before_the_time_zone_is_verified(tmp_path, cleanup):
    clock = FakeClock(EVENING)
    service = make_service(tmp_path, clock, tz_ok=None)
    cleanup(service)
    assert service.full_charge.suspended == "timezone_unverified"
    world(service, clock)
    clock.advance(1)
    feed(service, clock.now(), tomorrow=True)
    assert run(service.request_solve("t")) == "ok"
    state = service.full_charge.state
    assert state.state == "idle" and state.evaluated_day is None
    assert service.plans[-1].snapshot.full_charge is None


def test_readings_before_the_commissioning_date_are_ignored(tmp_path, cleanup):
    clock = FakeClock(EVENING)
    config = service_config(tmp_path, **{**LIVE, **ON})
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="t",
        ws_transport=FakeHAServer(),
    )
    cleanup(service)
    assert service.full_charge._earliest == utc(2026, 11, 3, 19, 14, 32, 572000)


def test_without_home_assistant_the_feature_cannot_verify_the_time_zone(tmp_path, cleanup):
    clock = FakeClock(EVENING)
    service = make_service(tmp_path, clock, tz_ok=None)
    cleanup(service)
    assert run(service.verify_ha_timezone()) is None  # no REST client


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s, c: s.store.apply(
            entity_state(FC_ENTS.day_select, "monday", c.now(), attributes={"options": ["monday"]})
        ),
        lambda s, c: s.store.apply(entity_state(FC_ENTS.enable_switch, "unavailable", c.now())),
        lambda s, c: s.store.apply(entity_state(FC_ENTS.delay_switch, "unknown", c.now())),
    ],
)
def test_a_broken_weekly_contract_suspends_the_feature_until_it_is_fixed(tmp_path, cleanup, mutate):
    clock = FakeClock(EVENING)
    service = make_service(tmp_path, clock)
    cleanup(service)
    world(service, clock)
    clock.advance(1)
    mutate(service, clock)
    run(service.full_charge_tick())
    assert service.full_charge.suspended == "entities_missing" and service._fc_directive is None
    assert "entities_missing" in service.status()["full_charge"]["alerts"]
    clock.advance(1)
    world(service, clock)
    run(service.full_charge_tick())
    assert service.full_charge.suspended is None and service._fc_directive is not None
    assert "entities_missing" not in service.status()["full_charge"]["alerts"]


def test_the_scheduler_is_hands_off_until_home_assistant_has_synced(tmp_path, cleanup):
    clock = FakeClock(EVENING)
    service = make_service(tmp_path, clock)
    cleanup(service)
    world(service, clock)
    service.ha.stats.synced = False
    run(service.full_charge_tick())
    assert (
        service._fc_directive is None and service.actuation_context(clock.now()).full_charge is None
    )
    service.ha.stats.synced = True
    run(service.full_charge_tick())
    assert service.actuation_context(clock.now()).full_charge is not None


# ---------------------------------------------------------------------------
# Evaluation after the regular plan; the commit plan
# ---------------------------------------------------------------------------


def evening_service(tmp_path, cleanup, **overrides):
    clock = FakeClock(EVENING)
    service = make_service(tmp_path, clock, **overrides)
    cleanup(service)
    world(service, clock)
    clock.advance(1)  # a changed price state needs a newer last_updated to be accepted
    feed(service, clock.now(), tomorrow=True)
    return service, clock


def test_the_evaluation_runs_after_the_regular_plan_is_published(tmp_path, cleanup, monkeypatch):
    from battery_optimizer.service import runtime

    service, clock = evening_service(tmp_path, cleanup, **{"full_charge.threshold_min_eur": 100.0})
    seen = {}
    real = runtime.evaluate_full_charge

    def spy(snapshot, regular):
        seen["plans_published"] = len(service.plans)
        seen["worker_busy"] = service._worker_busy
        seen["mode"] = snapshot.full_charge.mode
        seen["window"] = (snapshot.full_charge.window_start, snapshot.full_charge.checkpoint_end)
        return real(snapshot, regular)

    monkeypatch.setattr(runtime, "evaluate_full_charge", spy)
    assert run(service.request_solve("scheduled")) == "ok"
    assert seen["plans_published"] == 1 and seen["mode"] == "evaluate"
    start, end = seen["window"]
    assert 0 < start < end
    plan = service.plans[-1]
    assert (
        plan.snapshot.full_charge is None and plan.full_charge is None
    )  # the plan of record is regular
    fc = service.full_charge
    assert fc.state.state == "scheduled" and fc.state.run.day == TOMORROW
    assert fc.state.run.forcing_cost_eur is not None and not fc.state.run.forced
    decision = [r for r in records(tmp_path) if r["event"] == "decision"][0]
    assert decision["accepted"] is True and decision["d"] == 10 and decision["day"] == "2026-10-21"
    assert decision["solver"]["target"]["status"] and decision["checkpoint_slot"]
    assert {"normal_cost_eur", "lifted_cost_eur", "target_cost_eur", "vs_normal_eur"} <= set(
        decision
    )
    assert state_file(tmp_path)["state"] == "scheduled"
    assert service._worker_busy is False


def test_a_costly_day_is_declined_and_not_priced_again(tmp_path, cleanup, monkeypatch):
    from battery_optimizer.service import runtime

    service, clock = evening_service(
        tmp_path,
        cleanup,
        **{
            "full_charge.threshold_min_eur": 0.0,
            "full_charge.threshold_scale_eur": 0.0,
            "full_charge.fixed_cost_eur": 5.0,
        },
    )
    calls = []
    real = runtime.evaluate_full_charge
    monkeypatch.setattr(runtime, "evaluate_full_charge", lambda s, r: calls.append(1) or real(s, r))
    assert run(service.request_solve("scheduled")) == "ok"
    assert service.full_charge.state.state == "idle" and calls == [1]
    assert service.full_charge.state.last_decision["accepted"] is False
    clock.advance(1000)
    world(service, clock)
    clock.advance(1)
    feed(service, clock.now(), tomorrow=True)
    assert run(service.request_solve("scheduled")) == "ok"
    assert calls == [1]  # a decision is sticky for the day: no second evaluation


def test_no_solve_is_made_below_the_minimum(tmp_path, cleanup, monkeypatch):
    from battery_optimizer.service import runtime

    service, clock = evening_service(
        tmp_path, cleanup, **{"full_charge.bootstrap_last_full": "2026-10-18"}
    )
    monkeypatch.setattr(
        runtime, "evaluate_full_charge", lambda s, r: pytest.fail("priced below min")
    )
    assert run(service.request_solve("scheduled")) == "ok"
    assert service.full_charge.state.state == "idle" and records(tmp_path, "full_charge") == [
        r for r in records(tmp_path) if r["event"] == "bootstrap"
    ]


def test_a_failed_evaluation_never_costs_the_plan_and_is_retried(tmp_path, cleanup, monkeypatch):
    from battery_optimizer.service import runtime

    service, clock = evening_service(tmp_path, cleanup, **{"full_charge.threshold_min_eur": 100.0})
    real = runtime.evaluate_full_charge

    def boom(snapshot, regular):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(runtime, "evaluate_full_charge", boom)
    assert run(service.request_solve("scheduled")) == "ok"
    assert len(service.plans) == 1 and service.full_charge.state.state == "idle"
    assert service.full_charge.state.evaluated_day is None  # retried at the next solve
    events = [
        json.loads(line)
        for p in (tmp_path / "data").rglob("events.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert any(e["kind"] == "full_charge_evaluation_failed" for e in events)
    monkeypatch.setattr(runtime, "evaluate_full_charge", real)
    clock.advance(1000)
    world(service, clock)
    clock.advance(1)
    feed(service, clock.now(), tomorrow=True)
    assert run(service.request_solve("scheduled")) == "ok"
    assert service.full_charge.state.state == "scheduled"


def test_a_forced_day_needs_no_solve_and_a_commit_plan_follows(tmp_path, cleanup):
    service, clock = evening_service(
        tmp_path, cleanup, **{"full_charge.bootstrap_last_full": "2026-10-07"}
    )
    run(service.full_charge_tick())  # d = 14: forced for tomorrow, no evaluation
    fc = service.full_charge
    assert fc.state.state == "scheduled" and fc.state.run.forced and fc.state.run.day == TOMORROW
    assert run(service.request_solve("event:full_charge")) == "ok"
    plan = service.plans[-1]
    spec = plan.snapshot.full_charge
    assert spec is not None and spec.mode == "commit" and spec.window_start > 0
    assert spec.ceiling_before_frac == pytest.approx(0.95) and spec.target_soc_frac == 1.0
    assert plan.full_charge is not None and plan.full_charge.checkpoint_slot is not None
    assert plan.full_charge.shortfall_kwh == pytest.approx(0.0, abs=1e-6)
    assert fc.state.run.plan_id == plan.plan_id  # noted for the records
    assert plan.to_record()["inputs"]["full_charge"]["mode"] == "commit"
    # the checkpoint is a slot of tomorrow, at least two slots before local midnight
    slot = plan.full_charge.checkpoint_slot.astimezone(TZ)
    assert (
        slot.date() == TOMORROW and slot.hour <= 23 and not (slot.hour == 23 and slot.minute >= 30)
    )


def test_a_pending_run_inside_the_day_lifts_the_soc_clamp(tmp_path, cleanup):
    """On the run's day a measured 98% is planned as 98%, the evening before as 95%."""
    service, clock = evening_service(
        tmp_path, cleanup, **{"full_charge.bootstrap_last_full": "2026-10-07"}
    )
    clock.advance(1)
    world(service, clock, soc=98.0)
    clock.advance(1)
    feed(service, clock.now(), soc=0.98)
    run(service.full_charge_tick())
    snap = service.build_snapshot(clock.now(), "t")
    assert (
        snap.full_charge.window_start > 0
        and snap.soc_frac == pytest.approx(0.95)
        and snap.soc_clamped
    )
    # the next morning, inside the run's day
    clock.set(utc(2026, 10, 20, 22, 30))  # 00:30 CEST on the 21st
    world(service, clock, soc=98.0)
    feed(service, clock.now(), tomorrow=False)
    snap = service.build_snapshot(clock.now(), "t")
    assert snap.full_charge.window_start == 0 and snap.soc_frac == pytest.approx(0.98)
    assert not snap.soc_clamped


def test_after_the_run_the_plans_are_regular_again(tmp_path, cleanup):
    service, clock = evening_service(
        tmp_path, cleanup, **{"full_charge.bootstrap_last_full": "2026-10-07"}
    )
    run(service.full_charge_tick())
    assert service.build_snapshot(clock.now(), "t").full_charge is not None
    fc = service.full_charge
    fc.state.state, fc.state.run = "idle", None
    assert service.build_snapshot(clock.now(), "t").full_charge is None


# ---------------------------------------------------------------------------
# A whole run through the service (live writer)
# ---------------------------------------------------------------------------


def settle_clock(clock, service, seconds, **kw):
    clock.advance(seconds)
    world(service, clock, **kw)


async def tick_both(service):
    await service.full_charge_tick()
    await service.actuator.tick(service.actuation_context(service.clock.now()))
    await service.full_charge_tick()


def test_a_live_run_from_the_decision_to_a_measured_balance(tmp_path, cleanup):
    clock = FakeClock(utc(2026, 10, 20, 22, 44))  # 00:44 CEST on the run's day (the 21st)
    service = make_service(tmp_path, clock, **{"full_charge.bootstrap_last_full": "2026-10-07"})
    cleanup(service)
    fake = FakeHA(service.store, clock)
    service.actuator._send = fake.send
    fc = service.full_charge
    day = TOMORROW
    world(service, clock)
    feed(service, clock.now(), tomorrow=False)
    # the decision was taken yesterday evening (forced), the plan of this process charges at 01:45
    fc.record_evaluation(
        day, {"forcing_cost_eur": 0.0, "shortfall_kwh": 0.0}, clock.now() - timedelta(hours=4)
    )
    run(service._fc_flush(clock.now()))
    assert fc.state.state == "scheduled"
    service._full_charge_arm_due = lambda now: now >= utc(2026, 10, 20, 23, 45)

    async def go():
        first = next_slot_boundary(clock.now())
        clock.set(first + timedelta(seconds=1))
        world(service, clock)
        feed(service, clock.now())
        assert await service.request_solve("t") == "ok"
        await service.open_slot(floor_slot(clock.now(), 900))
        await tick_both(service)  # before the trigger: scheduled, directive disarmed, nothing armed
        assert fc.state.state == "scheduled" and service._fc_directive.armed is False
        assert [c for c in fake.calls if c[2] in (FC_ENTS.enable_switch, FC_ENTS.day_select)] == []
        # the trigger: armed, the actuator writes the day then the switch
        settle_clock(clock, service, 3600)
        feed(service, clock.now())
        await service.open_slot(floor_slot(clock.now(), 900))
        service.plans.clear()
        assert await service.request_solve("t") == "ok"
        service.adopt_late(clock.now())
        await tick_both(service)
        assert fc.state.state == "armed" and service._fc_directive.armed
        weekly = [
            (c[1], c[2], c[3])
            for c in fake.calls
            if c[2] in (FC_ENTS.enable_switch, FC_ENTS.day_select)
        ]
        assert weekly == [
            ("select_option", FC_ENTS.day_select, {"option": "wednesday"}),
            ("turn_on", FC_ENTS.enable_switch, None),
        ]
        # Omnibattery acknowledges
        settle_clock(
            clock,
            service,
            20,
            status=WEEKLY_FULL_CHARGE_STATUS,
            weekly="on",
            day="wednesday",
            soc=70.0,
        )
        feed(service, clock.now())
        await service.full_charge_tick()
        assert fc.state.state == "charging"
        status = service.status()["full_charge"]
        assert status["state"] == "charging" and status["run"]["status_seen"] is True
        # SOC reaches 99, then the run completes
        settle_clock(
            clock,
            service,
            1800,
            status=WEEKLY_FULL_CHARGE_STATUS,
            weekly="on",
            day="wednesday",
            soc=99.0,
        )
        feed(service, clock.now())
        await service.full_charge_tick()
        assert fc.state.state == "reached"
        settle_clock(clock, service, 60, status="balanced", weekly="on", day="wednesday", soc=100.0)
        feed(service, clock.now(), soc=1.0)
        await service.full_charge_tick()
        assert fc.state.state == "settling"
        directive = service.actuation_context(clock.now()).full_charge
        assert directive.armed and directive.settle_until == clock.now() + timedelta(seconds=300)
        # the reading arrives inside the window: a measured full charge
        reading = clock.now() + timedelta(seconds=75)
        settle_clock(
            clock,
            service,
            80,
            status="balanced",
            weekly="on",
            day="wednesday",
            soc=100.0,
            read_at=reading,
            balance="green",
        )
        feed(service, clock.now(), soc=1.0)
        await service.full_charge_tick()
        assert fc.state.state == "idle"
        assert service.actuation_context(clock.now()).full_charge.armed is False  # disarm

    run(go())
    assert fc.state.last_full_local_date == day and fc.state.last_full_source == "balance_reading"
    assert fc.state.last_outcome["outcome"] == "done"
    events = [r["event"] for r in records(tmp_path)]
    assert events.count("decision") == 1 and events.count("outcome") == 1
    states = [
        (r["from_state"], r["to_state"]) for r in records(tmp_path) if r["event"] == "transition"
    ]
    assert states == [
        ("idle", "scheduled"),
        ("scheduled", "armed"),
        ("armed", "charging"),
        ("charging", "reached"),
        ("reached", "settling"),
        ("settling", "idle"),
    ]
    outcome = [r for r in records(tmp_path) if r["event"] == "outcome"][0]
    assert outcome["evidence"]["cell_delta_mv"] == 183 and outcome["soc_peak_pct"] == 100.0
    text = service.metrics.render().decode()
    assert 'battery_optimizer_full_charge_runs_total{outcome="done"} 1.0' in text
    assert 'battery_optimizer_full_charge_state{state="idle"} 1.0' in text
    assert 'battery_optimizer_actuator_writes_total{result="ok",role="full_charge_day"} 1.0' in text
    assert state_file(tmp_path)["last_outcome"]["outcome"] == "done"


# ---------------------------------------------------------------------------
# State: restart, corruption, writer separation
# ---------------------------------------------------------------------------


def scheduled_service(tmp_path, cleanup, **kw):
    clock = FakeClock(EVENING)
    service = make_service(tmp_path, clock, **kw)
    cleanup(service)
    world(service, clock)
    service.full_charge.record_evaluation(
        TOMORROW, {"forcing_cost_eur": 0.0, "shortfall_kwh": 0}, clock.now()
    )
    run(service._fc_flush(clock.now()))
    return service, clock


def test_a_restart_learns_the_calendar_decision_and_writes_nothing_until_it_has_synced(
    tmp_path, cleanup
):
    first, clock = scheduled_service(tmp_path, cleanup)
    assert state_file(tmp_path)["run"]["day"] == "2026-10-21"
    second = make_service(tmp_path, clock)
    cleanup(second)
    assert (
        second.full_charge.state.state == "scheduled"
        and second.full_charge.state.run.day == TOMORROW
    )
    assert second.full_charge.reset_reason is None and not second.full_charge.dirty
    # hands off until the first tick after a sync: the persisted state alone causes no write
    assert (
        second._fc_directive is None and second.actuation_context(clock.now()).full_charge is None
    )
    world(second, clock)
    run(second.full_charge_tick())
    assert second._fc_directive.armed is False  # derived from the state: scheduled, not armed yet


def test_a_restart_mid_run_rederives_an_armed_directive_without_a_disarm(tmp_path, cleanup):
    first, clock = scheduled_service(tmp_path, cleanup)
    clock.set(utc(2026, 10, 20, 23, 50))
    world(first, clock, weekly="on", day="wednesday", status=WEEKLY_FULL_CHARGE_STATUS, soc=90.0)
    st = first.full_charge.state
    st.state = "charging"
    st.run.status_seen, st.run.armed_at, st.run.soc_peak_pct = True, clock.now(), 90.0
    first.full_charge.dirty = True
    run(first._fc_flush(clock.now()))
    second = make_service(tmp_path, clock)
    cleanup(second)
    world(second, clock, weekly="on", day="wednesday", status=WEEKLY_FULL_CHARGE_STATUS, soc=91.0)
    assert (
        second.actuation_context(clock.now()).full_charge is None
    )  # nothing is written before sync
    run(second.full_charge_tick())
    assert second.full_charge.state.state == "charging"
    assert second._fc_directive.armed and second._fc_directive.weekday == "wednesday"
    assert second.status()["full_charge"]["directive"] == {
        "armed": True,
        "weekday": "wednesday",
        "settle_until": None,
    }


def test_a_corrupt_state_file_is_set_aside_and_bootstrapped(tmp_path, cleanup):
    clock = FakeClock(EVENING)
    sink = ShadowSink(tmp_path / "data")
    sink.root.joinpath("state").mkdir(parents=True)
    path = sink.root / "state" / "full_charge.json"
    path.write_text("{not json")
    service = make_service(tmp_path, clock)
    cleanup(service)
    assert service.full_charge.state.state == "idle"
    assert service.full_charge.state.last_full_local_date == date(
        2026, 10, 11
    )  # bootstrap override
    assert "corrupt" in service._fc_state_problem
    assert not path.exists() and list(path.parent.glob("full_charge.json.corrupt-*"))
    assert service.status()["full_charge"]["state_problem"]
    run(service._fc_flush(clock.now()))
    assert state_file(tmp_path)["writer_mode"] == "live"  # a fresh file replaces it


def test_a_structurally_invalid_state_file_is_set_aside_too(tmp_path, cleanup):
    clock = FakeClock(EVENING)
    sink = ShadowSink(tmp_path / "data")
    sink.save_full_charge({"schema_version": 1, "writer_mode": "live", "state": "scheduled"})
    service = make_service(tmp_path, clock)
    cleanup(service)
    assert "invalid" in service._fc_state_problem and service.full_charge.state.state == "idle"
    assert list((sink.root / "state").glob("full_charge.json.corrupt-*"))


def test_the_simulated_state_of_the_feature_in_shadow_is_discarded_when_it_goes_live(
    tmp_path, cleanup
):
    shadow, clock = scheduled_service(tmp_path, cleanup, mode="live", feature_mode="shadow")
    assert state_file(tmp_path)["writer_mode"] == "simulated"
    assert shadow.full_charge.state.state == "scheduled"
    live = make_service(tmp_path, clock, mode="live", feature_mode="live")
    cleanup(live)
    run(live._fc_flush(clock.now()))
    assert live.full_charge.reset_reason == "writer_mode simulated -> live"
    assert live.full_charge.state.state == "idle" and live.full_charge.state.run is None
    assert live.full_charge.state.writer_mode == "live"
    assert live.full_charge.state.last_full_source == "bootstrap"  # eligible at once again
    assert live.full_charge.evaluation_wanted(clock.now(), True) == TOMORROW
    assert state_file(tmp_path)["writer_mode"] == "live"
    back = make_service(tmp_path, clock, mode="live", feature_mode="shadow")
    cleanup(back)
    assert back.full_charge.reset_reason == "writer_mode live -> simulated"
    # dry_run and a live service with the feature in shadow share the simulated state
    run(back._fc_flush(clock.now()))
    dry = make_service(tmp_path, clock, mode="dry_run")
    cleanup(dry)
    assert dry.full_charge.reset_reason is None


def test_a_simulated_state_is_discarded_at_the_first_live_start(tmp_path, cleanup):
    sim, clock = scheduled_service(tmp_path, cleanup, mode="dry_run")
    assert state_file(tmp_path)["writer_mode"] == "simulated"
    live = make_service(tmp_path, clock, mode="live")
    cleanup(live)
    run(live._fc_flush(clock.now()))
    assert live.full_charge.reset_reason == "writer_mode simulated -> live"
    assert live.full_charge.state.state == "idle" and live.full_charge.state.writer_mode == "live"
    assert live.full_charge.state.last_full_source == "bootstrap"
    again = make_service(tmp_path, clock, mode="dry_run")
    cleanup(again)
    assert again.full_charge.reset_reason == "writer_mode live -> simulated"


def test_an_unwritable_state_raises_the_alert_and_keeps_the_state_in_memory(
    tmp_path, cleanup, monkeypatch
):
    service, clock = scheduled_service(tmp_path, cleanup)

    def broken(state):
        raise OSError("read-only file system")

    monkeypatch.setattr(service.sink, "save_full_charge", broken)
    service.full_charge.dirty = True
    run(service._fc_flush(clock.now()))
    assert "state_unwritable" in service.status()["full_charge"]["alerts"]
    assert service.full_charge.state.state == "scheduled"  # kept in memory
    monkeypatch.undo()
    service.full_charge.dirty = True
    run(service._fc_flush(clock.now()))
    assert "state_unwritable" not in service.status()["full_charge"]["alerts"]


# ---------------------------------------------------------------------------
# Status, metrics, alerts
# ---------------------------------------------------------------------------


def test_status_and_metrics_expose_the_block_with_fixed_label_sets(tmp_path, cleanup):
    service, clock = scheduled_service(tmp_path, cleanup)
    clock.advance(1)
    world(service, clock, read_at=utc(2026, 10, 18, 3), balance="yellow")
    run(service.full_charge_tick())
    status = service.status()["full_charge"]
    json.dumps(service.status())
    assert status["enabled"] and status["state"] == "scheduled" and status["writer"] == "live"
    assert status["mode"] == "live"
    assert status["run"]["day"] == "2026-10-21" and status["last_decision"]["accepted"] is True
    assert status["cadence"]["class"] == "yellow" and status["cadence"]["min_days"] == 7
    assert status["last_full_local_date"] == "2026-10-18"
    assert (
        status["next_eligible_day"] == "2026-10-25" and status["forced_after_day"] == "2026-11-01"
    )
    assert (
        status["evidence"]["cell_delta_mv"] == 183 and status["evidence"]["delay_switch"] == "off"
    )
    assert status["actuator"] is None or "delay_switch" in status["actuator"]
    assert status["consecutive_failures"] == 0 and status["paused_until"] is None
    text = service.metrics.render().decode()
    for state in ("idle", "scheduled", "armed", "charging", "reached", "settling"):
        assert f'battery_optimizer_full_charge_state{{state="{state}"}}' in text
    assert 'battery_optimizer_full_charge_state{state="scheduled"} 1.0' in text
    assert 'battery_optimizer_full_charge_cadence_class{class="yellow"} 1.0' in text
    assert "battery_optimizer_full_charge_days_since_last 2.0" in text
    assert "battery_optimizer_full_charge_scheduled_day_timestamp_seconds" in text
    assert "battery_optimizer_full_charge_last_timestamp_seconds" in text
    assert "battery_optimizer_full_charge_cadence_min_days 7.0" in text
    for reason in (
        "delay_switch_on",
        "timezone_mismatch",
        "entities_missing",
        "never_reached",
        "unacknowledged",
        "reached_unmeasured",
        "balance_alert",
        "paused",
        "state_unwritable",
    ):
        assert f'battery_optimizer_full_charge_alert{{reason="{reason}"}} 0.0' in text
    for outcome in (
        "done",
        "done_simulated",
        "failed_never_armed",
        "failed_unacknowledged",
        "failed_never_reached",
        "failed_reached_unmeasured",
        "cancelled",
    ):
        assert f'battery_optimizer_full_charge_runs_total{{outcome="{outcome}"}}' in text


def test_an_actuator_refusal_becomes_an_alert_and_cancels_the_run(tmp_path, cleanup):
    clock = FakeClock(utc(2026, 10, 20, 23, 50))  # 01:50 CEST on the run's day
    service = make_service(tmp_path, clock)
    cleanup(service)
    service.actuator._send = FakeHA(service.store, clock).send
    world(service, clock, delay="on")  # the owner left the weekly delay switch on
    feed(service, clock.now())
    service.full_charge.record_evaluation(
        TOMORROW, {"forcing_cost_eur": 0.0, "shortfall_kwh": 0}, clock.now() - timedelta(hours=3)
    )
    service._full_charge_arm_due = lambda now: True

    async def go():
        await service.full_charge_tick()  # armed
        assert service.full_charge.state.state == "armed"
        await service.actuator.tick(service.actuation_context(clock.now()))
        assert service.actuator.full_charge_refusal == "delay_switch_on"
        assert "delay_switch_on" in service.status()["full_charge"]["alerts"]
        await service.full_charge_tick()

    run(go())
    out = service.full_charge.state.last_outcome
    assert out["outcome"] == "cancelled" and out["reason"] == "refused:delay_switch_on"
    assert service.full_charge.state.consecutive_failures == 0


def test_a_dry_run_service_simulates_the_run_and_sends_nothing(tmp_path, cleanup):
    clock = FakeClock(utc(2026, 10, 20, 23, 50))
    service = make_service(tmp_path, clock, mode="dry_run")
    cleanup(service)
    sent = []

    async def send(*args):  # pragma: no cover - must never be called
        sent.append(args)

    service.actuator._send = send
    world(service, clock)
    feed(service, clock.now())
    service.full_charge.record_evaluation(
        TOMORROW, {"forcing_cost_eur": 0.0, "shortfall_kwh": 0}, clock.now() - timedelta(hours=3)
    )
    service._full_charge_arm_due = lambda now: True

    async def go():
        await service.full_charge_tick()
        assert assert_armed(service)
        assert await service.request_solve("t") == "ok"
        first = next_slot_boundary(clock.now())
        clock.set(first + timedelta(seconds=1))
        world(service, clock)
        feed(service, clock.now())
        await service.open_slot(first)
        await service.actuator.tick(service.actuation_context(clock.now()))
        await service.full_charge_tick()
        clock.set(utc(2026, 10, 21, 22, 1))  # the end of the run's day
        world(service, clock)
        await service.full_charge_tick()

    run(go())
    assert sent == []
    assert service.full_charge.state.last_outcome["outcome"] == "done_simulated"
    assert service.full_charge.state.last_full_source == "simulated"
    rows = records(tmp_path, "actuation")
    simulated = [
        w["role"] for r in rows for w in r["writes"] if w["role"].startswith("full_charge")
    ]
    assert simulated == ["full_charge_day", "full_charge_enable"]


def assert_armed(service):
    return service.full_charge.state.state == "armed" and service._fc_directive.armed
