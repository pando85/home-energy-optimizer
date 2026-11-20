"""The live climate actuator (ADR-0009): mapping, ordering, machine limits, floors, fallback, owner
select, legacy controller, heartbeat, dry_run and shutdown."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from service_fakes import entity_state

from battery_optimizer.service.climate_actuator import (
    FALLBACK_REASONS,
    HEAT_OFF,
    HEAT_ON,
    MIN_OFF_S,
    MIN_ON_S,
    ClimateActuator,
    ClimateContext,
    Write,
)
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import ClimateActuationEntities, ClimateActuationSettings
from battery_optimizer.service.entity_store import EntityStore
from battery_optimizer.service.ha_client import HACommandError
from battery_optimizer.service.ha_guard import (
    ActuationAllowlist,
    ForbiddenHACallError,
    check_ws_message,
)
from battery_optimizer.service.metrics import ServiceMetrics

OWNER = "input_select.climate_optimizer_mode"
ENTS = ClimateActuationEntities()
SETTINGS = ClimateActuationSettings(mode="live")
ROOMS = tuple(ENTS.rooms)
T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=20)
LONG_AGO = timedelta(hours=3)


def make_plan(
    heating=1, water=28, *, t0=T0, status="planned", valid_minutes=90, n=96, plan_id="plan-1"
):
    """A ClimatePlan v1 (the fields the actuator reads); *heating* is a value or a list."""
    heat = list(heating) if isinstance(heating, (list, tuple)) else [heating] * n
    waters = list(water) if isinstance(water, (list, tuple)) else [water] * n
    return {
        "version": 1,
        "advisory": True,
        "plan_id": plan_id,
        "status": status,
        "reason": None,
        "regime": "comfort",
        "code_version": "test",
        "generated_at": t0.isoformat(),
        "model": {},
        "inputs": {"comfort": {}, "away": None, "weather": None},
        "t0": t0.isoformat(),
        "valid_until": (t0 + timedelta(minutes=valid_minutes)).isoformat(),
        "slots": {
            "start": t0.isoformat(),
            "step_minutes": 15,
            "n": n,
            "heating": heat,
            "water_c": [w if h else None for w, h in zip(waters, heat)],
            "dhw": [0] * n,
            "predicted_room_c": {},
        },
    }


def ctx(clock, plan=None, **kw) -> ClimateContext:
    values = {
        "now": clock.now(),
        "ha_synced": True,
        "plan": plan,
        "plan_own": True,
        "plan_status": "planned" if plan is not None else None,
    }
    values.update(kw)
    return ClimateContext(**values)


class FakeHA:
    """Applies each accepted write to the store like HA and the unit would (the echo of the unit's
    own state is what the actuator reads back)."""

    def __init__(self, store, clock, *, silent=(), refuse=(), forbid=()):
        self.store, self.clock = store, clock
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.sent_at: list[tuple[datetime, str]] = []
        self.silent, self.refuse, self.forbid = set(silent), set(refuse), set(forbid)

    async def send(self, domain, service, entity, data):
        self.calls.append((domain, service, entity, data))
        self.sent_at.append((self.clock.now(), entity))
        if entity in self.forbid:
            raise ForbiddenHACallError("nope")
        if entity in self.refuse:
            raise HACommandError("actuation", "refused")
        if entity in self.silent:
            return
        self.clock.advance(0.05)
        now = self.clock.now()
        if domain == "input_select":
            self.store.apply(entity_state(ENTS.observed_mode, data["option"], now))
        elif domain == "input_number":
            self.store.apply(entity_state(ENTS.water_request, data["value"], now))
        # the heartbeat helper is not echoed (the actuator does not read it back)

    def writes(self):
        """The writes without the heartbeat, as ``(entity suffix, option or value)``."""
        out = []
        for domain, _service, entity, data in self.calls:
            if domain == "input_datetime":
                continue
            out.append((entity.split(".", 1)[1], data.get("option", data.get("value"))))
        return out

    def heartbeats(self):
        return [c for c in self.calls if c[0] == "input_datetime"]


def world(
    clock,
    *,
    owner="live",
    legacy="off",
    mode="DHW",
    since=LONG_AGO,
    direct=1,
    water=26,
    rooms=22.0,
    floors=None,
    room_age=timedelta(minutes=1),
):
    """The store a tick reads: owner select, legacy toggle, the unit's mode (changed *since* ago),
    Heating_Mode, the water request, thermostats (a number or a per-room dict) and floor helpers."""
    store = EntityStore.for_entities((OWNER, *ENTS.all()))
    now = clock.now()
    for entity, value, at in (
        (OWNER, owner, now - LONG_AGO),
        (ENTS.legacy_enable, legacy, now - LONG_AGO),
        (ENTS.observed_mode, mode, now - since),
        (ENTS.heating_mode, direct, now - LONG_AGO),
        (ENTS.water_request, water, now - LONG_AGO),
    ):
        if value is not None:
            store.apply(entity_state(entity, value, at))
    temps = rooms if isinstance(rooms, dict) else dict.fromkeys(ROOMS, rooms)
    for room, temperature in temps.items():
        if temperature is not None:
            store.apply(
                entity_state(
                    ENTS.rooms[room],
                    "heat",
                    now - room_age,
                    attributes={"current_temperature": temperature},
                )
            )
    for room, floor in (floors or {}).items():
        store.apply(entity_state(ENTS.floor_helper(room), floor, now - timedelta(minutes=5)))
    return store


def make(clock, store, *, mode="live", settings=SETTINGS, records=None, **fake_kw):
    ha = FakeHA(store, clock, **fake_kw)

    async def record(at, rec):
        if records is not None:
            records.append(rec)

    metrics = ServiceMetrics(climate_actuation=True)
    actuator = ClimateActuator(
        settings=replace(settings, mode=mode),
        owner_entity=OWNER,
        store=store,
        clock=clock,
        send=ha.send,
        record=record,
        metrics=metrics,
    )
    return actuator, ha, metrics


def tick(actuator, context):
    return asyncio.run(actuator.tick(context))


def counter(metrics, name, **labels):
    return getattr(metrics, name).labels(**labels)._value.get()


# ---------------------------------------------------------------------------
# Mapping and write ordering (decision 4)
# ---------------------------------------------------------------------------


def test_heating_on_writes_the_water_target_before_the_mode():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", water=26)
    actuator, ha, metrics = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=29)))
    assert ha.writes() == [("heishamon_heatshift_z1", 29), ("heishamon_heatmode", HEAT_ON)]
    assert ha.calls[0][:3] == ("input_number", "set_value", ENTS.water_target)
    assert ha.calls[1][:3] == ("input_select", "select_option", ENTS.heat_mode)
    assert record["desired"]["source"] == "plan" and record["desired"]["water_c"] == 29
    assert len(ha.heartbeats()) == 1 and record["heartbeat"] == "ok"
    assert actuator.alert is None and actuator.reasons == () and actuator.in_control
    assert counter(metrics, "climate_actuator_writes", entity="water_target", result="ok") == 1
    assert counter(metrics, "climate_actuator_writes", entity="heat_mode", result="ok") == 1
    assert counter(metrics, "climate_actuator_heartbeats", result="ok") == 1
    assert set(actuator.last_confirmed_write_at) == {"heat_mode", "water_target"}


def test_heating_off_writes_the_mode_only_and_leaves_the_water_target():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=30)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=0)))
    assert ha.writes() == [("heishamon_heatmode", HEAT_OFF)]
    assert len(ha.heartbeats()) == 1


def test_a_reconciled_unit_gets_no_writes_but_still_a_heartbeat():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=28)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=28)))
    assert ha.writes() == [] and record["writes"] == []
    assert len(ha.heartbeats()) == 1


def test_the_slot_in_progress_is_used():
    clock = FakeClock(T0 + timedelta(minutes=50))  # slot 3 of the plan
    store = world(clock, mode="DHW")
    actuator, ha, _ = make(clock, store)
    heating = [0, 0, 0, 1] + [0] * 92
    tick(actuator, ctx(clock, make_plan(heating=heating, water=31)))
    assert ha.writes() == [("heishamon_heatshift_z1", 31), ("heishamon_heatmode", HEAT_ON)]


def test_only_a_heat_mode_unit_is_not_rewritten_when_it_is_in_another_mode():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat", since=LONG_AGO, water=28)  # neither of ours
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=28)))
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON)]


# ---------------------------------------------------------------------------
# Machine limits (decision 5)
# ---------------------------------------------------------------------------


def test_minimum_on_holds_a_switch_to_dhw():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(seconds=MIN_ON_S - 60))
    actuator, ha, metrics = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert ha.writes() == [] and record["holds"] == ["min_on"]
    assert len(ha.heartbeats()) == 1  # a hold is not a failure
    clock.advance(61)
    tick(actuator, ctx(clock, make_plan(heating=0)))
    assert ha.writes() == [("heishamon_heatmode", HEAT_OFF)]


def test_minimum_off_holds_a_switch_to_heat_and_the_water_target_with_it():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=timedelta(seconds=MIN_OFF_S - 60), water=26)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    assert ha.writes() == [] and record["holds"] == ["min_off"]
    clock.advance(61)
    tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    assert ha.writes() == [("heishamon_heatshift_z1", 30), ("heishamon_heatmode", HEAT_ON)]


def test_the_dwell_clock_is_the_observed_mode_change_so_it_survives_a_restart():
    """A fresh actuator (a restart) reads the unit's last change from Home Assistant."""
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(minutes=5))
    first, ha1, _ = make(clock, store)
    second, ha2, _ = make(clock, store)
    for actuator, ha in ((first, ha1), (second, ha2)):
        tick(actuator, ctx(clock, make_plan(heating=0)))
        assert ha.writes() == []  # five minutes into a minimum on of thirty


@pytest.mark.parametrize("direct", [0, 2, None])
def test_the_water_target_is_written_only_in_direct_mode(direct):
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", direct=direct, water=26)
    actuator, ha, metrics = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON)]  # heating still follows the plan
    assert record["not_direct_mode"] is True and actuator.snapshot()["not_direct_mode"] == 1.0
    assert len(ha.heartbeats()) == 1


def test_the_water_target_moves_by_whole_kelvin_and_at_most_every_fifteen_minutes():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=1), water=26.4)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    assert ha.writes() == []  # 0.4 K: below one kelvin
    tick(actuator, ctx(clock, make_plan(heating=1, water=29)))
    assert ha.writes() == [("heishamon_heatshift_z1", 29)]
    clock.advance(300)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=33)))
    assert ha.writes() == [("heishamon_heatshift_z1", 29)] and record["holds"] == ["water_interval"]
    clock.advance(11 * 60)  # 15 minutes after the write
    tick(actuator, ctx(clock, make_plan(heating=1, water=33)))
    assert ha.writes() == [("heishamon_heatshift_z1", 29), ("heishamon_heatshift_z1", 33)]


def test_a_one_kelvin_flip_flop_of_the_plan_writes_nothing_while_heating_runs():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=1), water=28)
    actuator, ha, _ = make(clock, store)
    for water in (27, 28, 27, 28, 29, 28):  # replans moving by 1 K around the unit's 28
        record = tick(actuator, ctx(clock, make_plan(heating=1, water=water, t0=clock.now())))
        assert record["holds"] == [] and record["heartbeat"] == "ok"
        clock.advance(1800)
    assert ha.writes() == []
    tick(actuator, ctx(clock, make_plan(heating=1, water=30, t0=clock.now())))  # 2 K: written
    assert ha.writes() == [("heishamon_heatshift_z1", 30)]


def test_the_hysteresis_band_is_configurable():
    for band, wanted, expected in ((0.0, 27, [27]), (1.0, 27, []), (2.0, 30, []), (2.0, 31, [31])):
        clock = FakeClock(NOW)
        store = world(clock, mode="Heat+DHW", since=timedelta(hours=1), water=28)
        actuator, ha, _ = make(clock, store, settings=replace(SETTINGS, water_hysteresis_k=band))
        tick(actuator, ctx(clock, make_plan(heating=1, water=wanted)))
        assert [v for _e, v in ha.writes()] == expected, (band, wanted)


def test_the_start_of_a_heating_block_writes_the_desired_water_exactly():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=LONG_AGO, water=28)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=27)))  # 1 K off, but heating starts
    assert ha.writes() == [("heishamon_heatshift_z1", 27), ("heishamon_heatmode", HEAT_ON)]


def test_the_safe_state_water_target_is_written_exactly_while_heating_runs():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=LONG_AGO, water=27)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, plan=None))  # the safe target is 26
    assert ha.writes() == [("heishamon_heatshift_z1", 26)]


def test_the_fifteen_minute_interval_still_holds_a_hysteresis_write():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=1), water=26)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=29)))
    clock.advance(300)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=32)))
    assert record["holds"] == ["water_interval"] and len(ha.writes()) == 1


def test_at_least_ten_seconds_separate_two_writes_to_one_entity():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=26)
    actuator, ha, _ = make(clock, store, silent={ENTS.heat_mode})
    tick(actuator, ctx(clock, make_plan(heating=0)))  # never confirmed: retried
    times = [at for at, entity in ha.sent_at if entity == ENTS.heat_mode]
    assert len(times) == SETTINGS.max_attempts
    assert all((b - a).total_seconds() >= 10 for a, b in zip(times, times[1:]))


def test_an_integer_payload_in_range_is_all_that_may_leave_the_process():
    for bad in (None, 19, 36, 26.0, 26.5, True, float("nan"), "26"):
        with pytest.raises(ForbiddenHACallError):
            Write(
                "water_target", ENTS.water_target, "input_number", "set_value", value=bad
            ).validate()
    for bad in (None, "", "Heat", "Cool+DHW", "Auto", "heat+dhw", "dhw", "Heat+DHW "):
        with pytest.raises(ForbiddenHACallError):
            Write(
                "heat_mode", ENTS.heat_mode, "input_select", "select_option", option=bad
            ).validate()
    Write("water_target", ENTS.water_target, "input_number", "set_value", value=20).validate()
    Write("water_target", ENTS.water_target, "input_number", "set_value", value=35).validate()
    Write("heat_mode", ENTS.heat_mode, "input_select", "select_option", option=HEAT_OFF).validate()
    with pytest.raises(ForbiddenHACallError):
        Write("x", "switch.aquarea_force_dhw_mode", "switch", "turn_on").validate()


def test_a_plan_water_target_outside_the_range_is_an_invalid_plan_not_a_clamp_on_the_wire():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", water=26)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=40)))
    assert record["fallback"] == "plan_invalid"
    # never the out-of-range value: the safe state (Heat+DHW; the water is already at 26) instead
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON)]


# ---------------------------------------------------------------------------
# Comfort floors (decision 6)
# ---------------------------------------------------------------------------


def test_a_room_below_its_floor_forces_heat_exempt_from_minimum_off():
    clock = FakeClock(NOW)
    rooms = {"hall_kitchen": 20.4, "bedroom": 22.0, "office_t": 22.0}
    store = world(clock, mode="DHW", since=timedelta(minutes=2), water=26, rooms=rooms)
    actuator, ha, metrics = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))  # the plan says off
    # no planned water target in an off slot: the safe target; the minimum off does not apply
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON)]  # water already at 26
    assert record["desired"]["source"] == "floor" and record["desired"]["rooms"] == ["hall_kitchen"]
    assert metrics.climate_actuator_floor_overrides._value.get() == 1


def test_a_floor_override_uses_the_planned_water_target_when_the_slot_heats():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=timedelta(minutes=2), rooms=20.0, water=26)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=31)))
    assert ha.writes() == [("heishamon_heatshift_z1", 31), ("heishamon_heatmode", HEAT_ON)]


def test_the_floor_releases_at_the_floor_plus_the_margin_and_minimum_on_still_applies():
    clock = FakeClock(NOW)
    rooms = {"hall_kitchen": 20.9, "bedroom": 22.0, "office_t": 22.0}
    store = world(clock, mode="DHW", since=timedelta(minutes=2), water=26, rooms=rooms)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=0)))
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON)]
    # 21.2 is above the floor (21.0) but below floor + margin (21.5): still forced
    store.apply(
        entity_state(
            ENTS.rooms["hall_kitchen"],
            "heat",
            clock.now() + timedelta(seconds=1),
            attributes={"current_temperature": 21.2},
        )
    )
    clock.advance(60)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["desired"]["source"] == "floor"
    store.apply(
        entity_state(
            ENTS.rooms["hall_kitchen"],
            "heat",
            clock.now() + timedelta(seconds=1),
            attributes={"current_temperature": 21.5},
        )
    )
    clock.advance(60)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["desired"]["source"] == "plan" and record["holds"] == ["min_on"]
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON)]  # minimum on holds the switch back


def test_an_available_in_range_helper_sets_the_floor_otherwise_the_config():
    clock = FakeClock(NOW)
    # helper 22.5: 22.0 is below it; the other rooms' helpers are out of range
    store = world(
        clock,
        mode="DHW",
        since=LONG_AGO,
        water=26,
        rooms=22.0,
        floors={"hall_kitchen": 22.5, "bedroom": 17.0, "office_t": 24.0},
    )
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["observed"]["floors"] == {"hall_kitchen": 22.5, "bedroom": 21.0, "office_t": 21.0}
    assert record["desired"]["rooms"] == ["hall_kitchen"]


def test_an_owner_floor_helper_is_used_however_long_ago_it_was_set():
    # an input_number set by the owner never re-reports: 3 h or 30 days old, it still counts
    for age in (timedelta(hours=3), timedelta(days=30)):
        clock = FakeClock(NOW)
        store = world(clock, mode="DHW", since=LONG_AGO, water=26, rooms=21.5)
        store.apply(entity_state(ENTS.floor_helper("bedroom"), 22.0, clock.now() - age))
        actuator, _ha, _ = make(clock, store)
        record = tick(actuator, ctx(clock, make_plan(heating=0)))
        assert record["observed"]["floors"]["bedroom"] == 22.0
        assert record["desired"]["source"] == "floor" and record["desired"]["rooms"] == ["bedroom"]


def test_an_unavailable_or_non_numeric_floor_helper_falls_back_to_the_config():
    for state in ("unavailable", "unknown", "garbage", "nan"):
        clock = FakeClock(NOW)
        store = world(clock, mode="DHW", since=LONG_AGO, water=26, rooms=22.0)
        store.apply(entity_state(ENTS.floor_helper("bedroom"), state, clock.now()))
        actuator, _ha, _ = make(clock, store)
        record = tick(actuator, ctx(clock, make_plan(heating=0)))
        assert record["observed"]["floors"]["bedroom"] == 21.0, state
        assert record["desired"]["source"] == "plan"


def test_a_room_without_a_reading_is_skipped_and_none_at_all_is_a_fallback():
    clock = FakeClock(NOW)
    rooms = {"hall_kitchen": None, "bedroom": 22.0, "office_t": 22.0}  # one thermostat missing
    store = world(clock, mode="DHW", since=LONG_AGO, water=26, rooms=rooms)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["fallback"] is None and ha.writes() == []
    # a reading past the backstop is skipped
    clock2 = FakeClock(NOW)
    store2 = world(
        clock2, mode="DHW", since=LONG_AGO, water=26, rooms=15.0, room_age=timedelta(hours=7)
    )
    actuator2, ha2, _ = make(clock2, store2)
    record = tick(actuator2, ctx(clock2, make_plan(heating=0)))
    assert record["fallback"] == "no_room_reading"
    assert ha2.writes() == [("heishamon_heatmode", HEAT_ON)]


def test_a_steady_room_reading_older_than_an_hour_is_used_while_ha_is_synced():
    # HA publishes a thermostat only when it changes: 75 min without news is a steady room
    clock = FakeClock(NOW)
    store = world(
        clock, mode="DHW", since=LONG_AGO, water=26, rooms=22.0, room_age=timedelta(minutes=75)
    )
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["observed"]["rooms"] == dict.fromkeys(ROOMS, 22.0)
    assert record["fallback"] is None and ha.writes() == []
    # and a steady cold one still forces the floor override
    clock2 = FakeClock(NOW)
    store2 = world(
        clock2, mode="DHW", since=LONG_AGO, water=26, rooms=19.0, room_age=timedelta(hours=3)
    )
    actuator2, _ha2, _ = make(clock2, store2)
    record = tick(actuator2, ctx(clock2, make_plan(heating=0)))
    assert record["desired"]["source"] == "floor"


def test_unavailable_missing_and_implausible_room_readings_are_still_skipped():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=LONG_AGO, water=26, rooms=22.0)
    now = clock.now()
    store.apply(entity_state(ENTS.rooms["hall_kitchen"], "unavailable", now))
    store.apply(
        entity_state(ENTS.rooms["bedroom"], "heat", now, attributes={"current_temperature": 99.0})
    )
    store.apply(entity_state(ENTS.rooms["office_t"], "heat", now, attributes={}))
    actuator, _ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["observed"]["rooms"] == dict.fromkeys(ROOMS)
    assert record["fallback"] == "no_room_reading"


# ---------------------------------------------------------------------------
# Fallback = the safe heating state (decision 7)
# ---------------------------------------------------------------------------

BAD_PLANS = {
    "no_plan": lambda: {"plan": None},
    "plan_not_from_this_process": lambda: {"plan": make_plan(), "plan_own": False},
    "plan_expired": lambda: {"plan": make_plan(t0=T0 - timedelta(hours=2))},
    "plan_failed": lambda: {
        "plan": {**make_plan(status="error"), "slots": None},
        "plan_status": "plan_failed",
    },
    "plan_invalid": lambda: {
        "plan": {**make_plan(status="error"), "slots": None},
        "plan_status": "error",
    },
    "observer_no_state": lambda: {
        "plan": {**make_plan(status="skipped"), "slots": None},
        "plan_status": "no_state",
    },
}


@pytest.mark.parametrize("reason", sorted(BAD_PLANS))
def test_every_plan_problem_writes_the_safe_state_once_then_stops(reason):
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=timedelta(minutes=2), water=30)  # minimum off running
    actuator, ha, metrics = make(clock, store)
    record = tick(actuator, ctx(clock, **BAD_PLANS[reason]()))
    assert record["fallback"] == reason and record["desired"]["source"] == "safe_state"
    # exempt from the minimum off and the water interval; direct mode: water 26 (the safe target)
    assert ha.writes() == [("heishamon_heatshift_z1", 26), ("heishamon_heatmode", HEAT_ON)]
    assert counter(metrics, "climate_actuator_fallbacks", reason=reason) == 1
    assert len(ha.heartbeats()) == 1  # the safe state is confirmed: the heartbeat continues
    for _ in range(3):  # then no further writes until a valid plan, whatever the unit does
        clock.advance(60)
        tick(actuator, ctx(clock, **BAD_PLANS[reason]()))
    assert len(ha.writes()) == 2 and len(ha.heartbeats()) == 4
    assert counter(metrics, "climate_actuator_fallbacks", reason=reason) == 1
    assert actuator.snapshot()["fallback"] == reason and actuator.snapshot()["guard_ok"] is False


def test_the_fallback_reasons_are_a_closed_set():
    assert set(BAD_PLANS) <= set(FALLBACK_REASONS)
    assert len(FALLBACK_REASONS) == len(set(FALLBACK_REASONS))


def test_a_valid_plan_ends_the_fallback_and_the_plan_drives_again():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=LONG_AGO, water=26)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, plan=None))
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON)]
    clock.advance(31 * 60)  # past the minimum on
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["fallback"] is None and actuator.fallback is None
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON), ("heishamon_heatmode", HEAT_OFF)]


def test_the_safe_state_in_curve_mode_writes_the_mode_only():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", direct=0, water=22)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, plan=None))
    assert ha.writes() == [("heishamon_heatmode", HEAT_ON)] and record["not_direct_mode"] is True


def test_an_unreadable_mode_sensor_is_held_for_the_grace_then_the_safe_state():
    clock = FakeClock(NOW)
    store = world(clock, mode="unavailable", water=26)
    actuator, ha, metrics = make(clock, store, silent={ENTS.heat_mode})
    for _ in range(3):  # HA restarting: nothing written, no heartbeat, no alert, no fallback
        record = tick(actuator, ctx(clock, make_plan(heating=0)))
        assert record["fallback"] is None and record["guard_reasons"] == ["restart_grace"]
        # the optimizer is alive and holding on purpose: the watchdog's heartbeat continues
        assert ha.writes() == [] and record["heartbeat"] == "ok" and actuator.alert is None
        assert actuator.in_control and actuator.snapshot()["guard_ok"] is False
        assert actuator.snapshot()["reasons"] == ("restart_grace",)
        clock.advance(60)
    assert counter(metrics, "climate_actuator_fallbacks", reason="mode_sensor_unavailable") == 0
    clock.advance(SETTINGS.ha_restart_grace_s)  # it persists past the grace: a real loss
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["fallback"] == "mode_sensor_unavailable"
    assert record["guard_reasons"] == []
    assert record["heartbeat"] is None  # it cannot confirm even the safe state: no heartbeat
    assert actuator.alert == "safe_state_failed"
    assert counter(metrics, "climate_actuator_fallbacks", reason="mode_sensor_unavailable") == 1


def test_a_mode_sensor_that_returns_within_the_grace_resumes_the_plan_with_no_safe_state():
    clock = FakeClock(NOW)
    store = world(clock, mode="unavailable", water=26)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=0)))
    assert ha.writes() == []
    clock.advance(240)
    store.apply(entity_state(ENTS.observed_mode, "DHW", clock.now() - timedelta(minutes=30)))
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["guard_reasons"] == [] and record["fallback"] is None
    assert ha.writes() == [] and record["heartbeat"] == "ok"
    clock.advance(3600)  # a later loss starts a new grace
    store.apply(entity_state(ENTS.observed_mode, "unavailable", clock.now()))
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["guard_reasons"] == ["restart_grace"] and ha.writes() == []
    assert all(
        counter(metrics, "climate_actuator_fallbacks", reason=reason) == 0
        for reason in FALLBACK_REASONS
    )


def test_a_zero_grace_writes_the_safe_state_at_once():
    clock = FakeClock(NOW)
    store = world(clock, mode="unavailable", water=26)
    actuator, _ha, _ = make(clock, store, settings=replace(SETTINGS, ha_restart_grace_s=0.0))
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["fallback"] == "mode_sensor_unavailable" and record["guard_reasons"] == []


def test_without_a_restart_episode_a_missing_room_reading_is_an_immediate_fallback():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=LONG_AGO, water=26, rooms=None)
    actuator, _ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["fallback"] == "no_room_reading" and record["guard_reasons"] == []


def test_a_resync_gives_the_unit_its_grace_before_missing_rooms_are_a_fallback():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=LONG_AGO, water=26, rooms=None)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=0), ha_synced=False))
    assert record["guard_reasons"] == ["ha_not_synced"] and ha.calls == []  # no heartbeat either
    clock.advance(3000)  # the outage itself does not use up the grace
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["guard_reasons"] == ["restart_grace"] and record["fallback"] is None
    assert ha.writes() == [] and record["heartbeat"] == "ok"
    clock.advance(SETTINGS.ha_restart_grace_s + 1)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["fallback"] == "no_room_reading" and record["guard_reasons"] == []


def test_a_clean_tick_after_a_resync_leaves_no_grace_behind():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=LONG_AGO, water=26)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=0), ha_synced=False))
    clock.advance(60)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    assert record["guard_reasons"] == [] and actuator._grace_since is None
    store.apply(entity_state(ENTS.rooms["bedroom"], "unavailable", clock.now()))
    for room in ("hall_kitchen", "office_t"):
        store.apply(entity_state(ENTS.rooms[room], "unavailable", clock.now()))
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    assert record["fallback"] == "no_room_reading"  # no episode: not held


def test_every_other_fallback_reason_ignores_the_grace():
    clock = FakeClock(NOW)
    store = world(clock, mode="unavailable", water=26)  # a restart episode is under way
    actuator, ha, _ = make(clock, store, silent={ENTS.heat_mode})
    tick(actuator, ctx(clock, make_plan(heating=0)))
    assert ha.writes() == []
    record = tick(actuator, ctx(clock, plan=None))  # a missing plan is still an immediate fallback
    assert record["fallback"] == "no_plan"


class VanishingHA(FakeHA):
    """The mode sensor goes unavailable the moment a heat-mode write is sent (HA restarts)."""

    async def send(self, domain, service, entity, data):
        if entity == ENTS.heat_mode:
            self.calls.append((domain, service, entity, data))
            self.sent_at.append((self.clock.now(), entity))
            self.store.apply(entity_state(ENTS.observed_mode, "unavailable", self.clock.now()))
            raise HACommandError("actuation", "no synced Home Assistant session")
        await super().send(domain, service, entity, data)


def _actuator(clock, store, ha, **kw):
    return ClimateActuator(
        settings=replace(SETTINGS, mode="live"),
        owner_entity=OWNER,
        store=store,
        clock=clock,
        send=ha.send,
        metrics=ServiceMetrics(climate_actuation=True),
        **kw,
    )


def test_a_write_that_fails_while_ha_restarts_is_held_with_no_safe_state_and_no_alert():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=26)
    ha = VanishingHA(store, clock)
    actuator = _actuator(clock, store, ha)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert actuator.alert is None and record["heartbeat"] == "ok"
    assert record["guard_reasons"] == ["restart_grace"] and record["fallback"] is None
    # only the plan write was tried (retried), never the safe state
    writes = [c for c in ha.calls if c[0] != "input_datetime"]
    assert {entity for _d, _s, entity, _x in writes} == {ENTS.heat_mode}
    assert all(data["option"] == HEAT_OFF for _d, _s, _e, data in writes)
    clock.advance(SETTINGS.ha_restart_grace_s + 60)  # still gone after the grace: the safe state
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["fallback"] == "mode_sensor_unavailable"


def test_a_write_that_fails_with_the_session_gone_is_held_too():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=26)
    ha = FakeHA(store, clock, refuse={ENTS.heat_mode})
    actuator = _actuator(clock, store, ha, synced=lambda: False)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert actuator.alert is None and record["guard_reasons"] == ["restart_grace"]
    assert all(data["option"] == HEAT_OFF for _d, _s, _e, data in ha.calls)
    assert record["heartbeat"] is None and ha.heartbeats() == []  # no session: nothing to send


@pytest.mark.parametrize("owner", [None, "unavailable", "unknown", ""])
def test_an_unreadable_select_is_held_for_the_grace_not_select_left_live(owner):
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=26)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=26)))  # in control
    n_beats = len(ha.heartbeats())
    if owner is None:
        store._states.pop(OWNER)  # the select is missing from the store
    else:
        store.apply(entity_state(OWNER, owner, clock.now() + timedelta(seconds=1)))
    for _ in range(3):
        clock.advance(60)
        record = tick(actuator, ctx(clock, make_plan(heating=0)))
        assert record["guard_reasons"] == ["select_not_live", "restart_grace"]
        assert actuator.in_control and record["fallback"] is None and actuator.alert is None
        assert ha.writes() == [] and record["heartbeat"] == "ok"
    assert len(ha.heartbeats()) == n_beats + 3
    assert counter(metrics, "climate_actuator_fallbacks", reason="select_left_live") == 0
    # it returns: the plan drives again, no safe state was ever written
    store.apply(entity_state(OWNER, "live", clock.now() + timedelta(seconds=1)))
    clock.advance(60)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    assert record["guard_reasons"] == [] and ha.writes() == []


def test_an_unreadable_select_that_outlasts_the_grace_is_select_left_live():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=30)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    n_beats = len(ha.heartbeats())
    store.apply(entity_state(OWNER, "unavailable", clock.now() + timedelta(seconds=1)))
    clock.advance(60)
    assert tick(actuator, ctx(clock, make_plan(heating=1, water=30)))["guard_reasons"] == [
        "select_not_live",
        "restart_grace",
    ]
    clock.advance(SETTINGS.ha_restart_grace_s)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    assert record["guard_reasons"] == ["select_not_live"]
    assert counter(metrics, "climate_actuator_fallbacks", reason="select_left_live") == 1
    assert ("heishamon_heatshift_z1", 26) in ha.writes()  # the safe state
    assert actuator.in_control is False
    assert len(ha.heartbeats()) == n_beats + 1  # only the held tick's, none after leaving live


@pytest.mark.parametrize("owner", ["shadow", "off", "banana"])
def test_an_explicit_select_other_than_live_is_immediate_even_in_a_restart_episode(owner):
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=30)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    tick(actuator, ctx(clock, make_plan(heating=1, water=30), ha_synced=False))
    clock.advance(60)  # resync: a grace is running
    store.apply(entity_state(OWNER, owner, clock.now()))
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    assert record["guard_reasons"] == ["select_not_live"]
    assert counter(metrics, "climate_actuator_fallbacks", reason="select_left_live") == 1
    assert actuator.in_control is False and actuator._grace_since is None


def test_the_safe_state_already_in_force_writes_nothing_but_beats():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=LONG_AGO, water=26)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, plan=None))
    assert ha.writes() == [] and len(ha.heartbeats()) == 1


def test_a_failed_plan_write_falls_back_to_the_safe_state_for_that_tick():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=30)
    # the unit never shows DHW: the mode write is unconfirmed, then the safe state (already on)
    actuator, ha, metrics = make(clock, store, silent={ENTS.heat_mode})
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert counter(metrics, "climate_actuator_fallbacks", reason="write_failed") == 1
    assert counter(metrics, "climate_actuator_writes", entity="heat_mode", result="failed") == 1
    assert counter(metrics, "climate_actuator_write_failures", reason="readback_timeout") == 3
    assert actuator.alert == "write_failed"  # the safe state itself needed nothing: confirmed
    assert record["heartbeat"] == "ok"
    # the safe state is water 26: the water target is written back too
    assert ("heishamon_heatshift_z1", 26) in ha.writes()


def test_a_guard_refusal_is_never_retried():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", water=26)
    actuator, ha, metrics = make(clock, store, forbid={ENTS.heat_mode})
    tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    assert len([c for c in ha.calls if c[2] == ENTS.heat_mode]) == 2  # plan, then the safe state
    assert counter(metrics, "climate_actuator_write_failures", reason="forbidden") == 2
    assert counter(metrics, "climate_actuator_write_failures", reason="readback_timeout") == 0
    assert actuator.alert == "safe_state_failed" and ha.heartbeats() == []


def test_ha_errors_are_retried_up_to_the_bound_then_counted():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", water=26)
    actuator, ha, metrics = make(clock, store, refuse={ENTS.heat_mode})
    tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    assert len([c for c in ha.calls if c[2] == ENTS.heat_mode]) == 2 * SETTINGS.max_attempts
    assert counter(metrics, "climate_actuator_write_failures", reason="ha_error") == 6


# ---------------------------------------------------------------------------
# The owner's select, the legacy controller, HA not synced (decisions 3 and 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("owner", [None, "unknown", "unavailable", "off", "shadow", "banana", ""])
def test_anything_but_live_means_no_writes_and_no_heartbeat(owner):
    clock = FakeClock(NOW)
    store = world(clock, owner=owner, mode="DHW", water=26)
    actuator, ha, metrics = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=1)))
    assert ha.calls == [] and record["guard_reasons"] == ["select_not_live"]
    assert actuator.in_control is False and actuator.snapshot()["in_control"] == 0.0
    assert record["heartbeat"] is None


def test_the_select_leaving_live_writes_the_safe_state_once_then_nothing():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=30)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=0)))  # in control; the plan wants DHW
    assert ha.writes() == [("heishamon_heatmode", HEAT_OFF)]
    n_beats = len(ha.heartbeats())
    store.apply(entity_state(OWNER, "shadow", clock.now() + timedelta(seconds=1)))
    clock.advance(30)
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["guard_reasons"] == ["select_not_live"]
    assert ha.writes() == [
        ("heishamon_heatmode", HEAT_OFF),
        ("heishamon_heatshift_z1", 26),
        ("heishamon_heatmode", HEAT_ON),  # the safe state, exempt from the minimum off
    ]
    assert counter(metrics, "climate_actuator_fallbacks", reason="select_left_live") == 1
    assert len(ha.heartbeats()) == n_beats  # no heartbeat after leaving live
    clock.advance(60)
    tick(actuator, ctx(clock, make_plan(heating=0)))
    assert len(ha.writes()) == 3 and len(ha.heartbeats()) == n_beats
    assert actuator.in_control is False


def test_the_select_is_read_never_written():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", water=26)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    store.apply(entity_state(OWNER, "off", clock.now() + timedelta(seconds=1)))
    tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    assert all(entity != OWNER for _d, _s, entity, _data in ha.calls)


@pytest.mark.parametrize("scenario", ["plan", "no_plan", "select_left_live"])
def test_the_legacy_controller_active_means_no_write_at_all(scenario):
    clock = FakeClock(NOW)
    store = world(clock, legacy="on", mode="DHW", water=30)
    actuator, ha, metrics = make(clock, store)
    if scenario == "plan":
        context = ctx(clock, make_plan(heating=1, water=28))
    elif scenario == "no_plan":  # a fallback trigger elsewhere; here nothing at all
        context = ctx(clock, plan=None)
    else:
        store.apply(entity_state(OWNER, "off", clock.now() + timedelta(seconds=1)))
        context = ctx(clock, make_plan(heating=1))
    record = tick(actuator, context)
    assert ha.calls == [] and record["guard_reasons"] == ["legacy_writer_active"]
    assert record["heartbeat"] is None and actuator.in_control is False
    assert counter(metrics, "climate_actuator_fallbacks", reason="no_plan") == 0


def test_the_legacy_toggle_missing_unavailable_or_off_counts_as_off():
    for legacy in (None, "unavailable", "unknown", "off", "yes"):
        clock = FakeClock(NOW)
        store = world(clock, legacy=legacy, mode="DHW", water=26)
        actuator, ha, _ = make(clock, store)
        tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
        assert ha.writes() == [("heishamon_heatmode", HEAT_ON)], legacy


def test_the_legacy_controller_coming_on_while_in_control_stops_every_write():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", water=26)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=26)))
    n = len(ha.calls)
    store.apply(entity_state(ENTS.legacy_enable, "on", clock.now() + timedelta(seconds=1)))
    clock.advance(30)
    tick(actuator, ctx(clock, plan=None))
    assert len(ha.calls) == n and actuator.in_control is False
    assert asyncio.run(actuator.shutdown()) is True and len(ha.calls) == n  # nor at shutdown


def test_nothing_is_written_while_home_assistant_is_not_synced():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", water=26)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, make_plan(heating=1), ha_synced=False))
    assert ha.calls == [] and record["guard_reasons"] == ["ha_not_synced"]


# ---------------------------------------------------------------------------
# dry_run, records and shutdown
# ---------------------------------------------------------------------------


def test_dry_run_records_every_write_it_would_make_and_sends_nothing():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=LONG_AGO, water=26)
    records: list[dict] = []
    actuator, ha, metrics = make(clock, store, mode="dry_run", records=records)
    record = tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    assert ha.calls == [] and record["sent"] is False
    rows = [r for r in records if r["kind"] == "climate_actuation_write"]
    assert [(r["role"], r["old"], r["new"], r["result"]) for r in rows] == [
        ("water_target", 26.0, 30, "dry_run"),
        ("heat_mode", "DHW", HEAT_ON, "dry_run"),
    ]
    assert all(r["sent"] is False and r["reason"] == "plan" for r in rows)
    assert counter(metrics, "climate_actuator_writes", entity="heat_mode", result="dry_run") == 1
    assert counter(metrics, "climate_actuator_heartbeats", result="dry_run") == 1
    # the simulated state is what the next tick plans against: no repeat
    clock.advance(60)
    tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    assert [r for r in records if r["kind"] == "climate_actuation_write"] == rows
    # and the dwell clock follows the simulated change: heating off is held by the minimum on
    record = tick(actuator, ctx(clock, make_plan(heating=0)))
    assert record["holds"] == ["min_on"]


def test_dry_run_still_honours_the_owner_select_and_the_legacy_toggle():
    for owner, legacy in (("shadow", "off"), ("live", "on")):
        clock = FakeClock(NOW)
        store = world(clock, owner=owner, legacy=legacy)
        records: list[dict] = []
        actuator, ha, _ = make(clock, store, mode="dry_run", records=records)
        tick(actuator, ctx(clock, make_plan(heating=1)))
        assert not [r for r in records if r["kind"] == "climate_actuation_write"]


def test_every_live_write_is_recorded_with_time_entity_old_new_and_reason():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=LONG_AGO, water=26)
    records: list[dict] = []
    actuator, _ha, _ = make(clock, store, records=records)
    tick(actuator, ctx(clock, plan=None))
    (row,) = [r for r in records if r["kind"] == "climate_actuation_write"]
    assert row["entity_id"] == ENTS.heat_mode and row["old"] == "DHW" and row["new"] == HEAT_ON
    assert row["reason"] == "no_plan" and row["result"] == "ok" and row["sent"] is True
    assert row["at"].startswith("2026-11-18T10:2")
    ticks = [r for r in records if r["kind"] == "climate_actuation_tick"]
    assert ticks and ticks[0]["fallback"] == "no_plan"


def test_shutdown_attempts_the_safe_state_when_in_control():
    clock = FakeClock(NOW)
    store = world(clock, mode="Heat+DHW", since=timedelta(hours=2), water=30)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    assert ha.writes() == []
    assert asyncio.run(actuator.shutdown()) is True
    assert ha.writes() == [("heishamon_heatshift_z1", 26)]  # the mode already is Heat+DHW
    assert counter(metrics, "climate_actuator_fallbacks", reason="shutdown") == 1
    assert actuator.in_control is False


def test_shutdown_writes_nothing_when_not_in_control():
    clock = FakeClock(NOW)
    store = world(clock, owner="shadow", mode="DHW")
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1)))
    assert asyncio.run(actuator.shutdown()) is True and ha.calls == []
    fresh, ha2, _ = make(clock, store)  # never ticked
    assert asyncio.run(fresh.shutdown()) is True and ha2.calls == []


def test_view_and_snapshot_describe_the_state():
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=LONG_AGO, water=26)
    actuator, _ha, _ = make(clock, store)
    assert (
        actuator.snapshot()["ticked"] is False and actuator.snapshot()["owner_select"] == "missing"
    )
    tick(actuator, ctx(clock, make_plan(heating=1, water=30)))
    view, snap = actuator.view(), actuator.snapshot()
    assert view["mode"] == "live" and view["sends"] is True and view["in_control"] is True
    assert view["guard"] == {"ok": True, "reasons": []}
    assert snap["owner_select"] == "live" and snap["desired_heating"] == 1.0
    assert snap["desired_water_celsius"] == 30 and snap["observed_heating"] == 1.0
    assert set(snap["last_confirmed_write"]) == {"heat_mode", "water_target"}


def test_the_actuators_own_writes_are_inside_the_guards_allowlist():
    """What the actuator sends is exactly what the climate allowlist admits (and nothing else)."""
    allow = ActuationAllowlist(
        datetimes=frozenset({ENTS.heartbeat}),
        input_selects={ENTS.heat_mode: frozenset({HEAT_ON, HEAT_OFF})},
        input_numbers={ENTS.water_target: (20, 35)},
    )
    clock = FakeClock(NOW)
    store = world(clock, mode="DHW", since=LONG_AGO, water=26, rooms=20.0)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, make_plan(heating=1, water=33)))
    assert ha.calls
    for i, (domain, service, entity, data) in enumerate(ha.calls):
        message = {
            "id": i + 1,
            "type": "call_service",
            "domain": domain,
            "service": service,
            "target": {"entity_id": entity},
            "service_data": data,
        }
        check_ws_message(message, allow)


def test_the_module_names_no_entity_it_must_never_write():
    """ADR-0009: never DHW, the tank target, the heater, boost, quiet or holiday mode, setpoints."""
    from pathlib import Path

    import battery_optimizer.service.climate_actuator as module

    text = Path(module.__file__).read_text().lower()
    for banned in (
        "force_dhw",
        "tank_temp",
        "powermode",
        "quietmode",
        "holiday",
        "set_temperature",
    ):
        assert banned not in text, banned
    assert set(ClimateActuationEntities().written()) == {
        ENTS.heat_mode,
        ENTS.water_target,
        ENTS.heartbeat,
    }
