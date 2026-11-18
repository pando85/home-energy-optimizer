"""The climate actuator inside the resident service (ADR-0009): config, runtime wiring, the guarded
client, the plan hand-off from the shadow, metrics, status and shutdown."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
import yaml
from climate_shadow_fakes import FakeSamples
from service_fakes import FakeHAServer, entity_state, ha_state, wait_until, write_service_config
from test_climate_actuator import ENTS, OWNER, make_plan
from test_climate_plan_service import states as plan_states
from test_climate_shadow import MIDNIGHT, make_job, read
from test_climate_shadow_service import (
    REGIME,
    make_service,
    metric,
    run_synced,
    shutdown,
)

from battery_optimizer.climate import shadow
from battery_optimizer.service.api import create_app
from battery_optimizer.service.config import (
    CLIMATE_ACTUATION_MODES,
    ClimateActuationSettings,
    load_service_config,
)
from battery_optimizer.service.ha_guard import actuation_allowlist, check_ws_message

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
LIVE = {"climate_actuation.mode": "live"}
DRY = {"climate_actuation.mode": "dry_run"}
MODE_ENTITY = ENTS.heat_mode
WATER_ENTITY = ENTS.water_target
WRITTEN = {ENTS.heat_mode, ENTS.water_target, ENTS.heartbeat}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_the_committed_config_is_climate_shadow_with_the_adrs_entities():
    config = load_service_config(REPO / "config" / "service.yaml")
    s = config.climate_actuation
    assert s.mode == "shadow" and CLIMATE_ACTUATION_MODES == ("shadow", "dry_run", "live")
    assert s == ClimateActuationSettings()  # the file holds exactly the code defaults
    assert (s.tick_interval_s, s.floor_margin_c, s.floor_c, s.safe_water_target_c) == (
        60.0,
        0.5,
        21.0,
        26,
    )
    e = s.entities
    assert (e.heat_mode, e.water_target, e.heartbeat) == (
        "input_select.heishamon_heatmode",
        "input_number.heishamon_heatshift_z1",
        "input_datetime.climate_optimizer_heartbeat",
    )
    assert e.observed_mode == "sensor.aquarea_mode"
    assert e.heating_mode == "sensor.aquarea_heating_mode"
    assert e.water_request == "sensor.aquarea_z1_heat_request_temperature"
    assert e.legacy_enable == "input_boolean.appdaemon_climate_enable"
    assert set(e.rooms) == {"hall_kitchen", "bedroom", "office_t"}
    raw = yaml.safe_load((REPO / "config" / "service.yaml").read_text())
    assert raw["climate_actuation"]["mode"] == "shadow"
    assert config.climate_shadow.mode_entity == OWNER  # the owner's select is the shadow's


def test_nothing_is_built_in_shadow_and_the_exposition_is_unchanged(tmp_path):
    server = FakeHAServer()
    service = make_service(tmp_path, server)  # climate_shadow on, climate_actuation default
    try:
        assert service.climate_actuator is None and service.ha._actuation is None
        assert "climate_actuator" not in service.status()
        assert "climate_actuator" not in service.metric_snapshot()
        assert "climate_actuator" not in service.metrics.render().decode()
        assert actuation_allowlist(service.config) is None
        assert ENTS.observed_mode not in service.store.tracked
        assert ENTS.heat_mode not in service.store.tracked
    finally:
        shutdown(service)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"climate_actuation.mode": "banana"}, "climate_actuation.mode"),
        ({"climate_actuation.mode": False}, "climate_actuation.mode"),  # a bare YAML off
        ({"climate_actuation.mode": None}, "climate_actuation.mode"),
        ({"climate_actuation.surprise": 1}, "unknown climate_actuation keys"),
        ({"climate_actuation.entities.extra": "a.b"}, "unknown climate_actuation.entities keys"),
        ({"climate_actuation.tick_interval_s": 121}, "tick_interval_s"),
        ({"climate_actuation.tick_interval_s": 0}, "tick_interval_s"),
        ({"climate_actuation.safe_water_target_c": 19}, "safe_water_target_c"),
        ({"climate_actuation.safe_water_target_c": 36}, "safe_water_target_c"),
        ({"climate_actuation.safe_water_target_c": 26.5}, "safe_water_target_c"),
        ({"climate_actuation.safe_water_target_c": True}, "safe_water_target_c"),
        ({"climate_actuation.floor_c": 17.9}, "floor_c"),
        ({"climate_actuation.floor_c": 23.1}, "floor_c"),
        ({"climate_actuation.floor_margin_c": -0.1}, "floor_margin_c"),
        ({"climate_actuation.floor_margin_c": 2.5}, "floor_margin_c"),
        ({"climate_actuation.max_attempts": 0}, "max_attempts"),
        ({"climate_actuation.max_attempts": 9}, "max_attempts"),
        ({"climate_actuation.entities.heat_mode": "switch.x"}, "entities.heat_mode"),
        ({"climate_actuation.entities.water_target": "input_select.x"}, "entities.water_target"),
        ({"climate_actuation.entities.heartbeat": "input_number.x"}, "entities.heartbeat"),
        (
            {"climate_actuation.entities.heartbeat": "input_datetime.battery_optimizer_heartbeat"},
            "battery's heartbeat",
        ),
        ({"climate_actuation.entities.observed_mode": "not an entity"}, "observed_mode"),
        ({"climate_actuation.entities.rooms": {}}, "rooms"),
        ({"climate_actuation.entities.rooms": {"Bad Room": "climate.x"}}, "rooms"),
        ({"climate_actuation.entities.rooms": {"a": "sensor.x"}}, "rooms"),
        ({"climate_actuation.entities.floor_helper_prefix": "input_number.floor"}, "prefix"),
        ({"climate_actuation.entities.heating_mode": "sensor.aquarea_mode"}, "distinct"),
        # dry_run and live act on the plan the shadow solves in this process
        ({"climate_actuation.mode": "dry_run"}, "needs climate_shadow.enabled"),
        ({"climate_actuation.mode": "live"}, "needs climate_shadow.enabled"),
        (
            {
                "climate_actuation.mode": "live",
                "climate_shadow.enabled": True,
                "climate_shadow.prior": "abcdef",
                "climate_shadow.entities.mode": None,
            },
            "climate_shadow.entities.mode",
        ),
    ],
)
def test_bad_climate_actuation_config_is_refused(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        load_service_config(write_service_config(tmp_path, **overrides))


def test_climate_actuation_may_be_set_by_the_site_overrides_file(tmp_path):
    base = write_service_config(tmp_path, **{"climate_shadow.enabled": True})
    cfg = yaml.safe_load(base.read_text())
    cfg["climate_shadow"]["prior"] = "abcdef"
    base.write_text(yaml.safe_dump(cfg))
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("climate_actuation:\n  mode: live\n  floor_c: 22.0\n")
    config = load_service_config(base, overrides)
    assert config.climate_actuation.mode == "live" and config.climate_actuation.floor_c == 22.0
    assert [o.path for o in config.overrides] == [
        "climate_actuation.mode",
        "climate_actuation.floor_c",
    ]
    overrides.write_text("climate_actuation:\n  modes: live\n")
    with pytest.raises(ValueError, match="climate_actuation"):
        load_service_config(base, overrides)


def test_the_climate_actuation_never_changes_the_battery_plan_hash(tmp_path):
    base = {"climate_shadow.enabled": True, "climate_shadow.prior": "abcdef"}
    shadow_cfg = load_service_config(write_service_config(tmp_path, **base))
    live = load_service_config(write_service_config(tmp_path, **base, **LIVE))
    assert live.climate_actuation.mode == "live"
    assert live.config_hash() == shadow_cfg.config_hash()
    assert live.public_dict()["climate_actuation"]["mode"] == "live"


# ---------------------------------------------------------------------------
# The shadow hands over the plan it solved here, and decides again after a restart
# ---------------------------------------------------------------------------


def test_a_restart_decides_the_boundary_again_when_an_actuator_waits_for_a_plan(tmp_path):
    samples = FakeSamples(MIDNIGHT - timedelta(days=12), MIDNIGHT + timedelta(days=3))
    legacy = shadow.LiveInputs(regime="legacy")
    first = make_job(tmp_path, samples)
    assert first.run_once(MIDNIGHT + timedelta(minutes=10), legacy)["status"] == "planned"
    assert first.plan_own is True and first.plan_status == "planned"

    plain = make_job(tmp_path, samples)  # no actuator: the boundary stays decided
    plain.load(MIDNIGHT + timedelta(minutes=30))
    assert plain.plan is not None and plain.plan_own is False
    assert plain.run_once(MIDNIGHT + timedelta(minutes=30), legacy)["status"] == "skipped"
    assert plain.plan_own is False

    again = make_job(tmp_path, samples)
    again.redecide_foreign = True  # build_job sets it with a climate actuator
    again.load(MIDNIGHT + timedelta(minutes=30))
    assert again.plan_own is False and list(again.queue) == [MIDNIGHT.isoformat()]
    assert again.run_once(MIDNIGHT + timedelta(minutes=30), legacy)["status"] == "planned"
    assert again.plan_own is True and again.plan_status == "planned"
    decisions = read(tmp_path, "climate_decisions")
    assert [d["t0"] for d in decisions] == [MIDNIGHT.isoformat()] * 2
    assert "redecision" not in decisions[0] and decisions[1]["redecision"] is True
    assert list(again.queue) == [MIDNIGHT.isoformat()]  # still one entry: scored once, not twice
    # only the first run after the restart: the boundary is decided now
    assert again.run_once(MIDNIGHT + timedelta(minutes=40), legacy)["status"] == "skipped"
    assert len(read(tmp_path, "climate_decisions")) == 2


def test_a_failed_decision_is_this_processes_plan_and_names_its_status(tmp_path):
    def broken(_now):
        raise OSError("prometheus down")

    job = make_job(tmp_path, broken)
    job.run_once(MIDNIGHT + timedelta(minutes=10), shadow.LiveInputs(regime="unknown"))
    assert job.plan_own is True and job.plan_status == "error" and job.plan["status"] == "error"


# ---------------------------------------------------------------------------
# End to end through the real guarded client
# ---------------------------------------------------------------------------

TIME = MIDNIGHT


def climate_states(*, mode="DHW", since=None, water=26, rooms=22.0, owner="live", legacy=None):
    at = TIME - timedelta(hours=3)
    out = [
        ha_state(REGIME, "on", TIME),
        ha_state(OWNER, owner, at),
        ha_state(ENTS.observed_mode, mode, since or at),
        ha_state(ENTS.heating_mode, 1, at),
        ha_state(ENTS.water_request, water, at),
        ha_state(ENTS.water_target, water, at),
        ha_state(ENTS.heat_mode, mode, since or at),
    ]
    if legacy is not None:
        out.append(ha_state(ENTS.legacy_enable, legacy, at))
    for room, entity in ENTS.rooms.items():
        out.append(ha_state(entity, "heat", TIME, attributes={"current_temperature": rooms}))
        del room
    return out


def build(tmp_path, states, **overrides):
    server = FakeHAServer(states)
    server.mirrors = {ENTS.heat_mode: ENTS.observed_mode, ENTS.water_target: ENTS.water_request}
    service = make_service(tmp_path, server, **{**LIVE, **overrides})

    async def no_rest(_entity):  # the REST refresh: nothing to add (and never a real network)
        return None

    service.rest.get_state = no_rest
    from climate_shadow_fakes import forecast_record

    fc_at = TIME - timedelta(minutes=55)
    service.sink.append_weather_forecast(fc_at, forecast_record(fc_at))
    return service, server


def actuate_tick(service):
    async def step():
        return await service.climate_actuator.tick(
            service.climate_actuation_context(service.clock.now())
        )

    return step


def calls(server):
    return [(m["domain"], m["service"], m["target"]["entity_id"]) for m in server.service_calls]


def test_live_acts_on_the_plan_the_shadow_solved_through_the_guard(tmp_path):
    service, server = build(tmp_path, climate_states(mode="Heat+DHW", water=30))
    try:
        summary, record = asyncio.run(
            run_synced(service, service.run_climate_shadow, actuate_tick(service))
        )
    finally:
        shutdown(service)
    assert summary["status"] == "planned"
    ctx = service.climate_actuation_context(service.clock.now())
    assert (
        ctx.plan is service.climate.plan and ctx.plan_own is True and ctx.plan_status == "planned"
    )
    assert record["fallback"] is None and record["guard_reasons"] == []
    assert record["desired"]["plan_id"] == service.climate.plan["plan_id"]
    # whatever the plan wanted, only the three entities were written, each frame passes the guard
    allowlist = actuation_allowlist(service.config)
    assert calls(server)  # at least the heartbeat
    for message in server.service_calls:
        check_ws_message(message, allowlist)
        assert message["target"]["entity_id"] in WRITTEN
    assert ("input_datetime", "set_datetime", ENTS.heartbeat) in calls(server)
    assert (
        record["heartbeat"] == "ok"
        and metric(service, "climate_actuator_heartbeats_total", result="ok") == 1.0
    )


def test_live_writes_the_planned_water_then_the_mode_and_reads_the_unit_back(tmp_path):
    service, server = build(tmp_path, climate_states(mode="DHW", water=24))

    async def go():
        await service.run_climate_shadow()
        now = service.clock.now()
        service.climate.plan = make_plan(heating=1, water=29, t0=MIDNIGHT)
        service.climate.plan_own, service.climate.plan_status = True, "planned"
        return await service.climate_actuator.tick(service.climate_actuation_context(now))

    try:
        (record,) = asyncio.run(run_synced(service, go))
    finally:
        shutdown(service)
    assert calls(server) == [
        ("input_number", "set_value", ENTS.water_target),
        ("input_select", "select_option", ENTS.heat_mode),
        ("input_datetime", "set_datetime", ENTS.heartbeat),
    ]
    assert server.service_calls[0]["service_data"] == {"value": 29}
    assert server.service_calls[1]["service_data"] == {"option": "Heat+DHW"}
    assert [w["result"] for w in record["writes"]] == ["ok", "ok"]
    assert server.states[ENTS.observed_mode]["state"] == "Heat+DHW"
    assert metric(service, "climate_actuator_writes_total", entity="heat_mode", result="ok") == 1.0
    rows = list((tmp_path / "data" / "service" / "v1" / "climate_actuation").rglob("*.jsonl"))
    lines = [json.loads(line) for row in rows for line in row.read_text().splitlines()]
    writes = [r for r in lines if r["kind"] == "climate_actuation_write"]
    assert [(w["entity_id"], w["new"], w["result"]) for w in writes] == [
        (ENTS.water_target, 29, "ok"),
        (ENTS.heat_mode, "Heat+DHW", "ok"),
    ]
    assert all(w["code_version"] == "test-sha" and w["old"] is not None for w in writes)


def test_dry_run_sends_nothing_and_records_what_it_would_write(tmp_path):
    service, server = build(tmp_path, climate_states(mode="DHW", water=24), **DRY)

    async def go():
        await service.run_climate_shadow()
        now = service.clock.now()
        service.climate.plan = make_plan(heating=1, water=29, t0=MIDNIGHT)
        service.climate.plan_own, service.climate.plan_status = True, "planned"
        return await service.climate_actuator.tick(service.climate_actuation_context(now))

    try:
        (record,) = asyncio.run(run_synced(service, go))
        status = service.status()["climate_actuator"]
        exposition = service.metrics.render().decode()
    finally:
        shutdown(service)
    assert server.service_calls == [] and service.ha._actuation is None
    assert [w["result"] for w in record["writes"]] == ["dry_run", "dry_run"]
    assert status["mode"] == "dry_run" and status["sends"] is False
    assert 'battery_optimizer_climate_actuator_mode{mode="dry_run"} 1.0' in exposition
    assert (
        metric(service, "climate_actuator_writes_total", entity="water_target", result="dry_run")
        == 1.0
    )


def test_the_legacy_controller_active_means_nothing_is_written_end_to_end(tmp_path):
    service, server = build(tmp_path, climate_states(mode="DHW", legacy="on"))
    try:
        summary, record = asyncio.run(
            run_synced(service, service.run_climate_shadow, actuate_tick(service))
        )
    finally:
        shutdown(service)
    assert summary["status"] == "planned" and server.service_calls == []
    assert record["guard_reasons"] == ["legacy_writer_active"]
    assert metric(service, "climate_actuator_guard_reason", reason="legacy_writer_active") == 1.0


def test_without_a_plan_of_its_own_the_safe_state_holds(tmp_path):
    """After a restart the plan is the stored one, not solved here: Heat+DHW at 26 until it is."""
    service, server = build(tmp_path, climate_states(mode="DHW", water=30))

    async def go():
        return await service.climate_actuator.tick(
            service.climate_actuation_context(service.clock.now())
        )

    try:
        (record,) = asyncio.run(run_synced(service, go))
    finally:
        shutdown(service)
    assert record["fallback"] == "no_plan"
    assert calls(server) == [
        ("input_number", "set_value", ENTS.water_target),
        ("input_select", "select_option", ENTS.heat_mode),
        ("input_datetime", "set_datetime", ENTS.heartbeat),
    ]
    assert server.service_calls[0]["service_data"] == {"value": 26}
    assert metric(service, "climate_actuator_fallbacks_total", reason="no_plan") == 1.0
    assert metric(service, "climate_actuator_fallback_active", reason="no_plan") == 1.0


def test_status_and_metrics_carry_the_climate_actuator(tmp_path):
    service, _server = build(tmp_path, climate_states(mode="Heat+DHW", water=26))

    async def go():
        await service.run_climate_shadow()
        await actuate_tick(service)()
        app = create_app(service, manage_lifecycle=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bo.test") as client:
            return (await client.get("/status")).json(), (await client.get("/metrics")).text

    try:
        ((status, text),) = asyncio.run(run_synced(service, go))
    finally:
        shutdown(service)
    block = status["climate_actuator"]
    assert block["mode"] == "live" and block["sends"] is True and block["in_control"] is True
    assert block["guard"]["ok"] is True and block["observed"]["owner_select"] == "live"
    assert status["climate_plan"]["live_allowed"] is True
    assert status["climate_plan"]["actuation"].startswith("heat mode and Z1 water target")
    for name in (
        "battery_optimizer_climate_actuator_mode",
        "battery_optimizer_climate_actuator_owner_select",
        "battery_optimizer_climate_actuator_in_control",
        "battery_optimizer_climate_actuator_guard_ok",
        "battery_optimizer_climate_actuator_guard_reason",
        "battery_optimizer_climate_actuator_fallback_active",
        "battery_optimizer_climate_actuator_not_direct_mode",
        "battery_optimizer_climate_actuator_desired_heating",
        "battery_optimizer_climate_actuator_observed_heating",
        "battery_optimizer_climate_actuator_floor_override_rooms",
        "battery_optimizer_climate_actuator_writes_total",
        "battery_optimizer_climate_actuator_write_failures_total",
        "battery_optimizer_climate_actuator_fallbacks_total",
        "battery_optimizer_climate_actuator_heartbeats_total",
    ):
        assert name in text, name
    assert 'battery_optimizer_climate_actuator_owner_select{state="live"} 1.0' in text
    assert 'battery_optimizer_climate_actuator_guard_reason{reason="select_not_live"} 0.0' in text


def test_live_plan_export_reports_the_effective_mode_live(tmp_path):
    """The export's lock is lifted for this path only (``climate_actuation.mode: live``)."""
    service, _server = build(tmp_path, plan_states("live"))

    async def go():
        await service.run_climate_shadow()
        app = create_app(service, manage_lifecycle=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bo.test") as client:
            return (await client.get("/climate/plan")).json()

    try:
        (body,) = asyncio.run(run_synced(service, go))
    finally:
        shutdown(service)
    assert body["requested_mode"] == "live" and body["effective_mode"] == "live"
    assert body["live_locked"] is False and body["live_lock_reason"] is None
    assert body["advisory"] is True and body["version"] == 1  # the contract is unchanged


def test_stop_attempts_the_safe_state_before_the_ha_session_closes(tmp_path):
    service, server = build(tmp_path, climate_states(mode="Heat+DHW", water=30))

    async def go():
        await service.run_climate_shadow()
        now = service.clock.now()
        service.climate.plan = make_plan(heating=1, water=30, t0=MIDNIGHT)
        service.climate.plan_own, service.climate.plan_status = True, "planned"
        await service.climate_actuator.tick(service.climate_actuation_context(now))
        assert server.service_calls and calls(server)[-1][2] == ENTS.heartbeat
        before = len(server.service_calls)
        await service.stop()
        return before

    try:
        (before,) = asyncio.run(run_synced(service, go))
    finally:
        shutdown(service)
    shutdown_calls = server.service_calls[before:]
    # Heat+DHW already shown: only the water target goes back to the safe 26
    assert [(m["target"]["entity_id"], m["service_data"]) for m in shutdown_calls] == [
        (ENTS.water_target, {"value": 26})
    ]


def test_the_actuator_loop_survives_a_failing_tick_and_wakes_on_the_owners_select(tmp_path):
    """Its own task: a bad tick is logged and the loop carries on; the owner's select wakes it."""
    service, server = build(tmp_path, climate_states(mode="DHW", water=26))
    ticks = []
    real_tick = service.climate_actuator.tick

    async def flaky(context):
        ticks.append(context.now)
        if len(ticks) == 1:
            raise RuntimeError("boom")
        return await real_tick(context)

    service.climate_actuator.tick = flaky

    async def go():
        loop_task = asyncio.create_task(service._climate_actuator_loop())
        await wait_until(lambda: len(ticks) >= 3)
        assert not loop_task.done()  # the first tick raised, the loop is alive
        service._climate_actuator_wake.clear()
        service._climate_wake_on(entity_state(OWNER, "shadow", service.clock.now()))
        assert service._climate_actuator_wake.is_set()
        service._climate_actuator_wake.clear()
        service._climate_wake_on(entity_state("sensor.unrelated", "1", service.clock.now()))
        assert not service._climate_actuator_wake.is_set()
        service._climate_wake_on(entity_state(ENTS.legacy_enable, "on", service.clock.now()))
        assert service._climate_actuator_wake.is_set()
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    try:
        asyncio.run(go())
    finally:
        shutdown(service)
    assert len(ticks) >= 3 and server.service_calls == []  # no HA session here: nothing sent
