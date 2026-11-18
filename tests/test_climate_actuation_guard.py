"""The climate write path of ha_guard (ADR-0009): exactly three entities, live mode only, and the
battery's allowlist unchanged."""

from __future__ import annotations

import json

import pytest
from service_fakes import REPO, service_config, write_service_config

from battery_optimizer.service.config import load_service_config
from battery_optimizer.service.ha_guard import (
    ACTUATION_SERVICES,
    CLIMATE_HEAT_MODE_OPTIONS,
    CLIMATE_WATER_RANGE_C,
    ActuationAllowlist,
    ForbiddenHACallError,
    actuation_allowlist,
    actuation_request,
    battery_allowlist,
    check_ws_frame,
    check_ws_message,
    climate_allowlist,
)

BATTERY_LIVE = {
    "mode": "live",
    "actuation.battery_ac_power_sign": 1,
    "planning.soc_source": "entity",
    "entities.battery_soc": "sensor.test_soc",
}
CLIMATE = {
    "climate_actuation.mode": "live",
    "climate_shadow.enabled": True,
    "climate_shadow.prior": "abcdef",
}
MODE = "input_select.heishamon_heatmode"
WATER = "input_number.heishamon_heatshift_z1"
HEARTBEAT = "input_datetime.climate_optimizer_heartbeat"
THERMOSTAT = "climate.thermostat_bedroom"
ALLOW = ActuationAllowlist(
    datetimes=frozenset({HEARTBEAT}),
    input_selects={MODE: frozenset({"Heat+DHW", "DHW"})},
    input_numbers={WATER: (20, 35)},
)
_MISSING = object()


def call(domain="input_select", service="select_option", entity=MODE, **kw):
    message = {
        "id": 9,
        "type": "call_service",
        "domain": domain,
        "service": service,
        "target": {"entity_id": entity},
        "service_data": {"option": "Heat+DHW"},
    }
    for key, value in kw.items():
        if value is _MISSING:
            message.pop(key, None)
        else:
            message[key] = value
    return message


def option(value, **kw):
    return call(service_data={"option": value}, **kw)


def water(value, **kw):
    return call("input_number", "set_value", WATER, service_data={"value": value}, **kw)


def beat(value=1.7e9):
    return call("input_datetime", "set_datetime", HEARTBEAT, service_data={"timestamp": value})


ALLOWED = [
    option("Heat+DHW"),
    option("DHW"),
    option("DHW", id=_MISSING),
    call(target=_MISSING, service_data={"entity_id": MODE, "option": "DHW"}),
    *[water(value) for value in range(20, 36)],
    water(20, id=_MISSING),
    beat(),
    beat(1.0),
]


@pytest.mark.parametrize("message", ALLOWED)
def test_the_climate_allowlist_admits_exactly_the_three_writes(message):
    check_ws_message(message, ALLOW)
    check_ws_frame(json.dumps(message), ALLOW)


@pytest.mark.parametrize("message", ALLOWED)
def test_without_an_allowlist_every_climate_write_is_refused(message):
    # shadow and dry_run hand the guard no allowlist
    with pytest.raises(ForbiddenHACallError):
        check_ws_message(message)
    with pytest.raises(ForbiddenHACallError):
        check_ws_frame(json.dumps(message))


@pytest.mark.parametrize(
    "message",
    [
        # the heat-mode options: only Heat+DHW and DHW, verbatim
        *[
            option(o)
            for o in (
                "Heat",
                "Cool",
                "Auto",
                "Heat+DHW ",
                " DHW",
                "heat+dhw",
                "dhw",
                "DHW+Heat",
                "Cool+DHW",
                "Auto(Heat)+DHW",
                "Auto(Cool)",
                "Auto(Cool)+DHW",
                "",
                None,
                0,
                1,
                True,
                ["DHW"],
                {"option": "DHW"},
            )
        ],
        call(service_data={}),
        call(service_data=_MISSING),
        call(service_data={"option": "DHW", "extra": 1}),
        call(service_data={"value": "DHW"}),
        call(service_data={"options": ["DHW"]}),
        # the water target: integers 20 to 35 only
        *[
            water(v)
            for v in (
                19,
                36,
                0,
                -5,
                40,
                100,
                26.5,
                26.0,
                20.0,
                35.0,
                19.999,
                35.001,
                True,
                False,
                "26",
                "",
                None,
                [26],
                float("nan"),
                float("inf"),
                float("-inf"),
            )
        ],
        call("input_number", "set_value", WATER, service_data={}),
        call("input_number", "set_value", WATER, service_data=_MISSING),
        call("input_number", "set_value", WATER, service_data={"value": 26, "extra": 1}),
        call("input_number", "set_value", WATER, service_data={"option": 26}),
        # the heartbeat: a finite positive timestamp
        beat(0),
        beat(-1),
        beat("now"),
        beat(True),
        beat(float("nan")),
        beat(float("inf")),
        call("input_datetime", "set_datetime", HEARTBEAT, service_data={"datetime": "x"}),
        call("input_datetime", "set_datetime", HEARTBEAT, service_data={}),
        # protocol hygiene, response and unrelated fields
        option("DHW", return_response=True),
        option("DHW", context={"id": "x"}),
        option("DHW", extra=1),
        option("DHW", id="9"),
        option("DHW", id=True),
        # lists, several entities, wildcards, other selectors
        option("DHW", target={"entity_id": [MODE]}),
        option("DHW", target={"entity_id": [MODE, "input_select.heishamon_powermode"]}),
        option("DHW", target={"entity_id": "all"}),
        option("DHW", target={"entity_id": MODE, "device_id": "d"}),
        option("DHW", target={"area_id": "living"}),
        option("DHW", target={}),
        option("DHW", target=_MISSING),
        water(26, target={"entity_id": [WATER]}),
        water(26, target={"entity_id": [WATER, "input_number.heishamon_tank_temp"]}),
        call(
            target={"entity_id": MODE},
            service_data={"entity_id": "input_select.heishamon_powermode", "option": "DHW"},
        ),
        # every other entity of those domains, and every other thing of the climate
        option("DHW", target={"entity_id": "input_select.heishamon_powermode"}),
        option("DHW", target={"entity_id": "input_select.heishamon_quietmode"}),
        option("DHW", target={"entity_id": "input_select.appdaemon_climate_heat_mode"}),
        option("DHW", target={"entity_id": "input_select.climate_optimizer_mode"}),  # the owner's
        option("live", target={"entity_id": "input_select.climate_optimizer_mode"}),
        option("DHW", target={"entity_id": "input_select.heishamon_heatmode_2"}),
        water(30, target={"entity_id": "input_number.heishamon_tank_temp"}),
        water(30, target={"entity_id": "input_number.heishamon_heatshift_z2"}),
        water(30, target={"entity_id": "input_number.comfort_floor_bedroom"}),
        water(30, target={"entity_id": "input_number.appdaemon_climate_min_hours_per_day"}),
        call("switch", "turn_on", "switch.aquarea_force_dhw_mode", service_data=_MISSING),
        call("switch", "turn_off", "switch.aquarea_force_dhw_mode", service_data=_MISSING),
        call("input_boolean", "turn_on", "input_boolean.appdaemon_climate_enable"),
        call("input_boolean", "turn_off", "input_boolean.appdaemon_climate_enable"),
        call("climate", "set_temperature", THERMOSTAT, service_data={"temperature": 25}),
        call("climate", "set_hvac_mode", THERMOSTAT, service_data={"hvac_mode": "heat"}),
        call("number", "set_value", WATER, service_data={"value": 26}),
        call("select", "select_option", MODE, service_data={"option": "DHW"}),
        call("input_text", "set_value", "input_text.comfort_dhw_plan", service_data={"value": "x"}),
        call("homeassistant", "turn_on", MODE),
        call("mqtt", "publish", "x", service_data={"topic": "panasonic_heat_pump/commands/x"}),
        call("script", "turn_on", "script.x"),
        call("automation", "trigger", "automation.set_heatmode"),
        # the other services of those domains
        call("input_select", "select_next", MODE, service_data={"cycle": True}),
        call("input_select", "select_previous", MODE, service_data={"cycle": True}),
        call("input_select", "select_first", MODE, service_data=_MISSING),
        call("input_select", "select_last", MODE, service_data=_MISSING),
        call("input_select", "set_options", MODE, service_data={"options": ["Heat"]}),
        call("input_number", "increment", WATER, service_data=_MISSING),
        call("input_number", "decrement", WATER, service_data=_MISSING),
        call("input_number", "reload", WATER, service_data=_MISSING),
        # an entity under the wrong service
        call("input_select", "select_option", WATER, service_data={"option": "DHW"}),
        call("input_number", "set_value", MODE, service_data={"value": 26}),
        call("input_select", "select_option", HEARTBEAT, service_data={"option": "DHW"}),
        call("input_datetime", "set_datetime", MODE, service_data={"timestamp": 1.7e9}),
        call("input_datetime", "set_datetime", "input_datetime.battery_optimizer_heartbeat",
             service_data={"timestamp": 1.7e9}),
        call(service_data={"option": "DHW"}, domain=_MISSING),
        call(service_data={"option": "DHW"}, service=_MISSING),
    ],
)  # fmt: skip
def test_the_climate_allowlist_refuses_everything_else(message):
    with pytest.raises(ForbiddenHACallError):
        check_ws_message(message, ALLOW)
    with pytest.raises(ForbiddenHACallError):
        check_ws_frame(json.dumps(message, default=repr), ALLOW)


def test_a_bad_write_hidden_in_a_coalesced_frame_is_refused():
    with pytest.raises(ForbiddenHACallError):
        check_ws_frame(json.dumps([option("DHW"), option("Heat")]), ALLOW)
    check_ws_frame(json.dumps([option("DHW"), water(26), beat()]), ALLOW)


def test_the_options_and_the_range_are_the_adrs_not_configuration():
    assert CLIMATE_HEAT_MODE_OPTIONS == {"Heat+DHW", "DHW"}
    assert CLIMATE_WATER_RANGE_C == (20, 35)
    assert ("input_select", "select_option") in ACTUATION_SERVICES
    assert ("input_number", "set_value") in ACTUATION_SERVICES


def test_actuation_request_builds_and_validates_the_climate_frames():
    frame = actuation_request(4, "input_select", "select_option", MODE, {"option": "DHW"}, ALLOW)
    assert frame == {
        "id": 4,
        "type": "call_service",
        "domain": "input_select",
        "service": "select_option",
        "target": {"entity_id": MODE},
        "service_data": {"option": "DHW"},
    }
    frame = actuation_request(5, "input_number", "set_value", WATER, {"value": 28}, ALLOW)
    assert frame["service_data"] == {"value": 28}
    for bad in (
        ("input_select", "select_option", MODE, {"option": "Heat"}, ALLOW),
        ("input_number", "set_value", WATER, {"value": 36}, ALLOW),
        ("input_number", "set_value", WATER, {"value": 26.5}, ALLOW),
        ("input_select", "select_option", MODE, {"option": "DHW"}, None),
        ("input_number", "set_value", WATER, {"value": 26}, None),
    ):
        with pytest.raises(ForbiddenHACallError):
            actuation_request(6, *bad)


def test_the_allowlist_validates_its_new_fields():
    with pytest.raises(ValueError):
        ActuationAllowlist(input_selects={"select.x": frozenset({"a"})})
    with pytest.raises(ValueError):
        ActuationAllowlist(input_selects={"input_select.x": frozenset()})
    with pytest.raises(ValueError):
        ActuationAllowlist(input_selects={"input_select.x": frozenset({""})})
    with pytest.raises(ValueError):
        ActuationAllowlist(input_numbers={"number.x": (20, 35)})
    with pytest.raises(ValueError):
        ActuationAllowlist(input_numbers={"input_number.x": (35, 20)})
    with pytest.raises(ValueError):
        ActuationAllowlist(input_numbers={"input_number.x": (20.5, 35)})
    with pytest.raises(ValueError):
        ActuationAllowlist(input_numbers={"input_number.x": (True, 35)})
    assert ActuationAllowlist().input_selects == {} and ActuationAllowlist().input_numbers == {}


# ---------------------------------------------------------------------------
# What the config builds: mode gating, independence from the battery
# ---------------------------------------------------------------------------


def cfg(tmp_path, **overrides):
    return load_service_config(write_service_config(tmp_path, **overrides))


def test_the_committed_config_is_climate_shadow_and_builds_no_allowlist():
    config = load_service_config(REPO / "config" / "service.yaml")
    assert config.climate_actuation.mode == "shadow" and config.mode == "shadow"
    assert climate_allowlist(config) is None and actuation_allowlist(config) is None


@pytest.mark.parametrize(("mode", "built"), [("shadow", False), ("dry_run", False), ("live", True)])
def test_only_live_mode_builds_the_climate_allowlist(tmp_path, mode, built):
    config = cfg(tmp_path, **{**CLIMATE, "climate_actuation.mode": mode})
    allowlist = actuation_allowlist(config)
    assert (allowlist is not None) is built
    if built:
        ents = config.climate_actuation.entities
        assert allowlist == climate_allowlist(config)
        assert allowlist.input_selects == {ents.heat_mode: frozenset({"Heat+DHW", "DHW"})}
        assert dict(allowlist.input_numbers) == {ents.water_target: (20, 35)}
        assert allowlist.datetimes == {ents.heartbeat}
        # nothing of the battery, and nothing else of the climate
        assert not allowlist.switches and not allowlist.numbers and not allowlist.selects
        for written in (ents.heat_mode, ents.water_target, ents.heartbeat):
            assert written in (
                *allowlist.input_selects,
                *allowlist.input_numbers,
                *allowlist.datetimes,
            )
        for read_only in (
            ents.observed_mode,
            ents.heating_mode,
            ents.water_request,
            ents.legacy_enable,
            "input_select.climate_optimizer_mode",  # the owner's select is never allowlisted
            *ents.rooms.values(),
        ):
            assert read_only not in (
                *allowlist.input_selects,
                *allowlist.input_numbers,
                *allowlist.datetimes,
            )


def test_the_battery_allowlist_is_unchanged_and_the_two_are_independent(tmp_path):
    battery_only = cfg(tmp_path, **BATTERY_LIVE)
    both = cfg(tmp_path, **BATTERY_LIVE, **CLIMATE)
    climate_only = cfg(tmp_path, **CLIMATE)
    ba = actuation_allowlist(battery_only)
    ents = battery_only.actuation.entities
    # a battery in live with the climate in shadow gets none of the climate entries
    assert ba == battery_allowlist(battery_only)
    assert ba.input_selects == {} and ba.input_numbers == {}
    assert ba.switches == {ents.allow_charge, ents.allow_discharge, ents.power_limits}
    assert dict(ba.numbers) == {
        ents.pd_target: (0.0, 4500.0),
        ents.max_discharge_power: (100.0, 2500.0),
    }
    assert ba.datetimes == {ents.heartbeat} and not ba.selects
    # the reverse: the climate alone gets none of the battery's
    ca = actuation_allowlist(climate_only)
    assert not ca.switches and not ca.numbers and not ca.selects
    assert ents.heartbeat not in ca.datetimes
    # both: the union, nothing more
    union = actuation_allowlist(both)
    assert union == ba.merged(ca)
    assert union.switches == ba.switches and union.numbers == ba.numbers
    assert union.datetimes == ba.datetimes | ca.datetimes
    assert union.input_selects == ca.input_selects and union.input_numbers == ca.input_numbers
    # a battery write is refused under the climate-only list, and the reverse
    pd = {
        "id": 1,
        "type": "call_service",
        "domain": "number",
        "service": "set_value",
        "target": {"entity_id": ents.pd_target},
        "service_data": {"value": 100},
    }
    check_ws_message(pd, ba)
    check_ws_message(pd, union)
    with pytest.raises(ForbiddenHACallError):
        check_ws_message(pd, ca)
    with pytest.raises(ForbiddenHACallError):
        check_ws_message(option("DHW"), ba)
    check_ws_message(option("DHW"), union)


def test_the_service_builds_the_guarded_client_by_mode(tmp_path):
    from service_fakes import TARIFF, FakeHAServer, utc

    from battery_optimizer.service.clock import FakeClock
    from battery_optimizer.service.runtime import ShadowService

    cases = (
        ({}, False),
        ({"climate_actuation.mode": "shadow"}, False),
        ({"climate_actuation.mode": "dry_run"}, False),
        ({"climate_actuation.mode": "live"}, True),
    )
    for extra, guarded in cases:
        config = service_config(
            tmp_path, **{"climate_shadow.enabled": True, "climate_shadow.prior": "abcdef", **extra}
        )
        service = ShadowService(
            config=config,
            tariff=TARIFF,
            data_dir=tmp_path / "data",
            clock=FakeClock(utc(2026, 11, 18, 10)),
            code_version="t",
            ws_transport=FakeHAServer(),
        )
        assert (service.ha._actuation is not None) is guarded, extra
        assert (service.climate_actuator is not None) is (
            extra.get("climate_actuation.mode", "shadow") != "shadow"
        )
        service._executor.shutdown(wait=True)
        service._climate_pool.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)
