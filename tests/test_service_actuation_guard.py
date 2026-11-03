"""The live write path of ha_guard: exactly the allowlisted Omnibattery writes, live mode only."""

from __future__ import annotations

import asyncio
import json
import random

import pytest
from service_fakes import REPO, FakeHAServer, service_config, utc, wait_until, write_service_config

from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import HomeAssistantSettings, Secret, load_service_config
from battery_optimizer.service.entity_store import EntityStore
from battery_optimizer.service.ha_client import GuardedConnection, HAWebSocketClient
from battery_optimizer.service.ha_guard import (
    ACTUATION_SERVICES,
    ActuationAllowlist,
    ForbiddenHACallError,
    actuation_allowlist,
    actuation_request,
    check_ws_frame,
    check_ws_message,
)

LIVE = {
    "mode": "live",
    "actuation.battery_ac_power_sign": 1,
    "planning.soc_source": "entity",
    "entities.battery_soc": "sensor.test_soc",
}
ALLOW = ActuationAllowlist(
    switches=frozenset(
        {
            "switch.venus_battery_allow_charge",
            "switch.venus_battery_allow_discharge",
            "switch.omnibattery_system_power_limits",
        }
    ),
    numbers={
        "number.omnibattery_pd_target_grid_power": (0.0, 4500.0),
        "number.omnibattery_system_max_discharge_power": (100.0, 2500.0),
    },
    datetimes=frozenset({"input_datetime.battery_optimizer_heartbeat"}),
)
_MISSING = object()


def call(domain="switch", service="turn_on", entity="switch.venus_battery_allow_charge", **kw):
    message = {
        "id": 9,
        "type": "call_service",
        "domain": domain,
        "service": service,
        "target": {"entity_id": entity},
    }
    for key, value in kw.items():
        if value is _MISSING:
            message.pop(key, None)
        else:
            message[key] = value
    return message


PD = "number.omnibattery_pd_target_grid_power"
CAP = "number.omnibattery_system_max_discharge_power"
HB = "input_datetime.battery_optimizer_heartbeat"

ALLOWED = [
    call(),
    call(service="turn_off"),
    call(entity="switch.venus_battery_allow_discharge"),
    call(entity="switch.omnibattery_system_power_limits"),
    call(id=_MISSING),
    call(service_data={}),
    call(target=_MISSING, service_data={"entity_id": "switch.venus_battery_allow_charge"}),
    call("number", "set_value", PD, service_data={"value": 0}),
    call("number", "set_value", PD, service_data={"value": 4500}),
    call("number", "set_value", PD, service_data={"value": 1234.5}),
    call("number", "set_value", CAP, service_data={"value": 100}),
    call("number", "set_value", CAP, service_data={"value": 2500}),
    call("input_datetime", "set_datetime", HB, service_data={"timestamp": 1.7e9}),
]


@pytest.mark.parametrize("message", ALLOWED)
def test_live_allowlist_admits_exactly_the_omnibattery_writes(message):
    check_ws_message(message, ALLOW)
    check_ws_frame(json.dumps(message, default=repr), ALLOW)


@pytest.mark.parametrize("message", ALLOWED)
def test_without_an_allowlist_every_write_is_refused(message):
    # shadow and dry_run hand the guard no allowlist: the PR-0 behaviour, unchanged
    with pytest.raises(ForbiddenHACallError):
        check_ws_message(message)
    with pytest.raises(ForbiddenHACallError):
        check_ws_frame(json.dumps(message))


@pytest.mark.parametrize(
    "message",
    [
        # wrong domain or service
        call(domain="light"),
        call(domain="switch", service="toggle"),
        call("input_boolean", "turn_on", "input_boolean.battery_optimizer_live"),
        call("number", "increment", PD),
        call("homeassistant", "turn_on"),
        call("script", "turn_on", "script.x"),
        call("input_datetime", "set_datetime", HB, service_data={"datetime": "2026-01-01"}),
        call(domain=_MISSING),
        call(service=_MISSING),
        # other entity, or an entity under the wrong service
        call(entity="switch.heater"),
        call(entity="switch.venus_battery_manual_mode"),
        call("number", "set_value", "number.omnibattery_system_max_charge_power",
             service_data={"value": 1000}),
        call("number", "set_value", "switch.venus_battery_allow_charge", service_data={"value": 1}),
        call(entity=PD),
        call("input_datetime", "set_datetime", "input_datetime.x", service_data={"timestamp": 1}),
        # entity lists, several entities, wildcards, other target selectors
        call(target={"entity_id": ["switch.venus_battery_allow_charge"]}),
        call(target={"entity_id": ["switch.venus_battery_allow_charge",
                                   "switch.venus_battery_allow_discharge"]}),
        call(target={"entity_id": "all"}),
        call(target={"entity_id": "switch.venus_battery_allow_charge", "device_id": "d"}),
        call(target={"area_id": "garage"}),
        call(target={}),
        call(target=_MISSING),
        call(service_data={"entity_id": "switch.venus_battery_allow_discharge"}),
        call(target=["switch.venus_battery_allow_charge"]),
        # extra keys, response, payload where none belongs
        call(return_response=True),
        call(context={"id": "x"}),
        call(extra=1),
        call(service_data={"brightness": 1}),
        call(service_data="on"),
        # bad values: unlimited 0 on the cap, export or above-cap target, non-numbers
        call("number", "set_value", CAP, service_data={"value": 0}),
        call("number", "set_value", CAP, service_data={"value": 99}),
        call("number", "set_value", CAP, service_data={"value": 2501}),
        call("number", "set_value", PD, service_data={"value": -50}),
        call("number", "set_value", PD, service_data={"value": 4500.5}),
        call("number", "set_value", PD, service_data={"value": "100"}),
        call("number", "set_value", PD, service_data={"value": True}),
        call("number", "set_value", PD, service_data={"value": float("nan")}),
        call("number", "set_value", PD, service_data={"value": float("inf")}),
        call("number", "set_value", PD, service_data={}),
        call("number", "set_value", PD, service_data={"value": 1, "extra": 2}),
        call("input_datetime", "set_datetime", HB, service_data={"timestamp": 0}),
        call("input_datetime", "set_datetime", HB, service_data={"timestamp": "now"}),
        call("input_datetime", "set_datetime", HB),
        # protocol hygiene
        call(id="9"),
        call(id=True),
    ],
)  # fmt: skip
def test_live_allowlist_refuses_everything_else(message):
    with pytest.raises(ForbiddenHACallError):
        check_ws_message(message, ALLOW)
    with pytest.raises(ForbiddenHACallError):
        check_ws_frame(json.dumps(message, default=repr), ALLOW)


def test_the_weather_query_and_reads_still_pass_with_an_allowlist():
    weather = {
        "id": 1,
        "type": "call_service",
        "domain": "weather",
        "service": "get_forecasts",
        "service_data": {"type": "hourly"},
        "target": {"entity_id": "weather.forecast_home"},
        "return_response": True,
    }
    check_ws_message(weather, ALLOW)
    check_ws_message({"id": 2, "type": "get_states"}, ALLOW)
    with pytest.raises(ForbiddenHACallError):
        check_ws_message({"id": 3, "type": "fire_event", "event_type": "x"}, ALLOW)
    with pytest.raises(ForbiddenHACallError):  # a weather-domain write is judged as weather
        check_ws_message({**weather, "service": "turn_on"}, ALLOW)


def test_a_bad_write_hidden_in_a_coalesced_frame_is_refused():
    frame = json.dumps([call(), call(entity="switch.heater")])
    with pytest.raises(ForbiddenHACallError):
        check_ws_frame(frame, ALLOW)


def test_the_service_set_is_closed():
    assert ACTUATION_SERVICES == {
        ("switch", "turn_on"),
        ("switch", "turn_off"),
        ("number", "set_value"),
        ("input_datetime", "set_datetime"),
    }


def test_allowlist_validates_its_own_entities():
    with pytest.raises(ValueError):
        ActuationAllowlist(switches=frozenset({"number.x"}))
    with pytest.raises(ValueError):
        ActuationAllowlist(numbers={"number.x": (10.0, 0.0)})
    with pytest.raises(ValueError):
        ActuationAllowlist(datetimes=frozenset({"input_boolean.x"}))


@pytest.mark.parametrize(("mode", "built"), [("shadow", False), ("dry_run", False), ("live", True)])
def test_only_live_mode_builds_an_allowlist(tmp_path, mode, built):
    overrides = {**LIVE, "mode": mode}
    config = load_service_config(write_service_config(tmp_path, **overrides))
    allowlist = actuation_allowlist(config)
    assert (allowlist is not None) is built
    if built:
        ents = config.actuation.entities
        assert allowlist.switches == {ents.allow_charge, ents.allow_discharge, ents.power_limits}
        assert dict(allowlist.numbers) == {
            ents.pd_target: (0.0, 4500.0),
            ents.max_discharge_power: (100.0, 2500.0),
        }
        assert allowlist.datetimes == {ents.heartbeat}
        assert ents.kill_switch not in allowlist.switches  # only the owner flips it


def test_committed_config_is_shadow_and_builds_no_allowlist():
    config = load_service_config(REPO / "config" / "service.yaml")
    assert config.mode == "shadow"
    assert actuation_allowlist(config) is None


def test_actuation_request_builds_and_validates():
    frame = actuation_request(4, "number", "set_value", PD, {"value": 50}, ALLOW)
    assert frame == {
        "id": 4,
        "type": "call_service",
        "domain": "number",
        "service": "set_value",
        "target": {"entity_id": PD},
        "service_data": {"value": 50},
    }
    assert "service_data" not in actuation_request(
        5, "switch", "turn_off", ALLOW_CHARGE, None, ALLOW
    )
    with pytest.raises(ForbiddenHACallError):
        actuation_request(6, "switch", "turn_off", ALLOW_CHARGE, None, None)


ALLOW_CHARGE = "switch.venus_battery_allow_charge"


def test_guarded_connection_with_and_without_allowlist():
    sent = []

    class Inner:
        async def send(self, text):
            sent.append(text)

        async def recv(self):  # pragma: no cover - unused
            return ""

        async def close(self):  # pragma: no cover - unused
            return None

    async def go():
        with pytest.raises(ForbiddenHACallError):
            await GuardedConnection(Inner()).send(json.dumps(call()))
        await GuardedConnection(Inner(), ALLOW).send(json.dumps(call()))
        with pytest.raises(ForbiddenHACallError):
            await GuardedConnection(Inner(), ALLOW).send(json.dumps(call(entity="switch.heater")))

    asyncio.run(go())
    assert len(sent) == 1


SETTINGS = HomeAssistantSettings(
    url="https://ha.test",
    token=Secret("t"),
    request_timeout_s=0.3,
    reconnect_initial_delay_s=0.01,
    reconnect_max_delay_s=0.05,
    reconnect_jitter_frac=0.0,
    heartbeat_interval_s=0.05,
    heartbeat_timeout_s=0.1,
)


@pytest.mark.parametrize("allowlist", [None, ALLOW])
def test_websocket_client_actuate_sends_only_with_an_allowlist(allowlist):
    server = FakeHAServer()
    client = HAWebSocketClient(
        settings=SETTINGS,
        store=EntityStore.for_entities([]),
        clock=FakeClock(utc(2026, 10, 20, 10)),
        transport=server,
        rng=random.Random(0),
        actuation=allowlist,
    )

    async def go():
        task = asyncio.create_task(client.run())
        try:
            await wait_until(lambda: client.stats.synced)
            if allowlist is None:
                with pytest.raises(ForbiddenHACallError):
                    await client.actuate("switch", "turn_off", ALLOW_CHARGE)
            else:
                await client.actuate("switch", "turn_off", ALLOW_CHARGE)
                await client.actuate("number", "set_value", PD, {"value": 50})
            with pytest.raises(ForbiddenHACallError):
                await client.actuate("switch", "turn_on", "switch.heater")
        finally:
            await client.stop()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())
    calls = [(m["domain"], m["service"], m["target"]["entity_id"]) for m in server.service_calls]
    if allowlist is None:
        assert calls == []
    else:
        assert calls == [("switch", "turn_off", ALLOW_CHARGE), ("number", "set_value", PD)]


def test_live_service_builds_a_guarded_client_and_dry_run_does_not(tmp_path):
    from service_fakes import TARIFF

    from battery_optimizer.service.runtime import ShadowService

    for mode, expect in (("dry_run", False), ("live", True)):
        config = service_config(tmp_path, **{**LIVE, "mode": mode})
        service = ShadowService(
            config=config,
            tariff=TARIFF,
            data_dir=tmp_path / "data",
            clock=FakeClock(utc(2026, 10, 20, 10)),
            code_version="t",
            ws_transport=FakeHAServer(),
        )
        assert (service.ha._actuation is not None) is expect
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)
