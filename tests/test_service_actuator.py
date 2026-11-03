"""The live actuator (ADR-0007): mapping, ordering, readback, guard, kill switch, heartbeat."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from service_fakes import (
    NOW,
    REPO,
    TARIFF,
    TZ,
    FakeHAServer,
    entity_state,
    feed,
    flat_history,
    ha_state,
    pvpc_attributes,
    service_config,
    wait_until,
    write_service_config,
)

from battery_optimizer.service.actuator import (
    GUARD_REASONS,
    ActuationContext,
    Actuator,
    safe_idle,
)
from battery_optimizer.service.api import create_app
from battery_optimizer.service.clock import FakeClock, SystemClock
from battery_optimizer.service.config import (
    ActuationEntities,
    ActuationSettings,
    load_service_config,
)
from battery_optimizer.service.entity_store import EntityStore
from battery_optimizer.service.ha_client import HACommandError
from battery_optimizer.service.ha_guard import ForbiddenHACallError
from battery_optimizer.service.metrics import ServiceMetrics
from battery_optimizer.service.planner import ShadowIntent
from battery_optimizer.service.runtime import ShadowService
from battery_optimizer.service.timegrid import floor_slot, local_date, next_slot_boundary

GRID = "sensor.shellyem_main_channel_1_power"
ENTS = ActuationEntities(
    kill_switch="input_boolean.battery_optimizer_live",
    heartbeat="input_datetime.battery_optimizer_heartbeat",
    allow_charge="switch.venus_battery_allow_charge",
    allow_discharge="switch.venus_battery_allow_discharge",
    pd_target="number.omnibattery_pd_target_grid_power",
    power_limits="switch.omnibattery_system_power_limits",
    max_discharge_power="number.omnibattery_system_max_discharge_power",
    integration_status="sensor.omnibattery_integration_status",
    battery_ac_power="sensor.venus_ac_power",
    battery_soc="sensor.venus_battery_soc",
    battery_liveness="sensor.omnibattery_non_responsive_batteries",
)
SETTINGS = ActuationSettings(entities=ENTS, battery_ac_power_sign=1)
SLOT = floor_slot(NOW, 900)  # 10:45Z; NOW = 10:50Z
# As on the installation: Omnibattery bounds the PD target by the batteries' charge limit.
NUMBER_ATTRS = {"min": -2500, "max": 2500, "step": 1}
CAP_ATTRS = {"min": 0, "max": 2500, "step": 1}


def intent(mode="charge", power=1000.0, *, valid_from=SLOT, plan_id="p1") -> ShadowIntent:
    end = valid_from + timedelta(minutes=15)
    sign = {"charge": 1.0, "discharge": -1.0}.get(mode, 0.0)
    return ShadowIntent(
        plan_id=plan_id,
        plan_sequence=1,
        sequence_id=0,
        valid_from=valid_from,
        valid_until=end,
        interval_end=end,
        mode=mode,
        target_power_w=0.0 if mode == "idle" else power,
        signed_power_w=sign * power,
        soc_target_frac=0.5,
        expected_buy_price_eur_per_kwh=0.1,
        generated_at=valid_from - timedelta(minutes=2),
        expires_at=valid_from + timedelta(minutes=30),
        inputs_hash="h",
        code_version="t",
    )


def ctx(clock, the_intent=None, **kw) -> ActuationContext:
    values = {
        "now": clock.now(),
        "ha_synced": True,
        "intent": the_intent,
        "decision": "intent" if the_intent is not None else "no_plan",
        "plan_from_this_process": True,
        "price_fresh": True,
    }
    values.update(kw)
    return ActuationContext(**values)


class FakeHA:
    """Applies each accepted write to the store, as HA's state_changed event would."""

    def __init__(self, store, clock, *, silent=(), refuse=(), forbid=()):
        self.store, self.clock = store, clock
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.silent, self.refuse, self.forbid = set(silent), set(refuse), set(forbid)
        self.refuse_once: set[str] = set()
        #: entity -> callable run after a write to it lands (e.g. the owner flips a switch)
        self.after: dict[str, object] = {}

    async def send(self, domain, service, entity, data):
        self.calls.append((domain, service, entity, data))
        try:
            await self._apply(domain, service, entity, data)
        finally:
            hook = self.after.pop(entity, None)
            if hook is not None:
                hook()

    async def _apply(self, domain, service, entity, data):
        if entity in self.forbid:
            raise ForbiddenHACallError("nope")
        if entity in self.refuse:
            raise HACommandError("actuation", "refused")
        if entity in self.refuse_once:
            self.refuse_once.discard(entity)
            raise HACommandError("actuation", "refused once")
        if entity in self.silent:
            return
        self.clock.advance(0.05)
        old = self.store.get(entity)
        attrs = dict(old.attributes) if old is not None else {}
        if domain == "switch":
            value = "on" if service == "turn_on" else "off"
        elif domain == "number":
            value = data["value"]
        else:
            value = self.clock.now().isoformat()
        self.store.apply(entity_state(entity, value, self.clock.now(), attributes=attrs))

    def writes(self):
        """Calls without the heartbeat, as ``(service, entity-suffix, value)``."""
        out = []
        for domain, service, entity, data in self.calls:
            if domain == "input_datetime":
                continue
            value = data.get("value") if data else None
            out.append((f"{domain}.{service}", entity.split(".", 1)[1], value))
        return out

    def heartbeats(self):
        return [c for c in self.calls if c[0] == "input_datetime"]


def world(
    clock,
    *,
    grid=400.0,
    ac=0.0,
    soc=50.0,
    status="balanced",
    kill="on",
    charge="off",
    discharge="off",
    limits="off",
    pd=0.0,
    cap=0.0,
    liveness="None",
    at=None,
):
    store = EntityStore.for_entities((GRID, *ENTS.all()))
    at = at or clock.now()
    for entity, value, attrs in (
        (GRID, grid, {"unit_of_measurement": "W"}),
        (ENTS.battery_ac_power, ac, {"unit_of_measurement": "W"}),
        (ENTS.battery_soc, soc, {"unit_of_measurement": "%"}),
        (ENTS.integration_status, status, {}),
        (ENTS.kill_switch, kill, {}),
        (ENTS.allow_charge, charge, {}),
        (ENTS.allow_discharge, discharge, {}),
        (ENTS.power_limits, limits, {}),
        (ENTS.pd_target, pd, NUMBER_ATTRS),
        (ENTS.max_discharge_power, cap, CAP_ATTRS),
        (ENTS.battery_liveness, liveness, {}),
    ):
        if value is not None:
            store.apply(entity_state(entity, value, at, attributes=attrs))
    return store


def make(clock, store, *, mode="live", settings=SETTINGS, records=None, **fake_kw):
    ha = FakeHA(store, clock, **fake_kw)

    async def record(at, rec):
        if records is not None:
            records.append(rec)

    metrics = ServiceMetrics(actuation=True)
    actuator = Actuator(
        settings=settings,
        mode=mode,
        store=store,
        clock=clock,
        grid_entity=GRID,
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
# Mapping and write ordering
# ---------------------------------------------------------------------------


def test_charge_sets_the_target_before_enabling_charge():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    actuator, ha, metrics = make(clock, store)
    record = tick(actuator, ctx(clock, intent("charge", 1000)))
    assert ha.writes() == [
        ("number.set_value", "omnibattery_pd_target_grid_power", 1400.0),
        ("switch.turn_on", "venus_battery_allow_charge", None),
    ]
    assert record["desired"]["mode"] == "charge" and record["desired"]["house_load_w"] == 400
    assert len(ha.heartbeats()) == 1 and record["heartbeat"] == "ok"
    assert actuator.alert is None and actuator.reasons == ()
    assert counter(metrics, "actuator_writes", role="pd_target", result="ok") == 1
    assert metrics.actuator_last_write._value.get() > 0


def test_switching_from_discharge_to_charge_removes_discharge_first():
    clock = FakeClock(NOW)
    store = world(clock, grid=50, ac=-800, discharge="on", limits="on", pd=50, cap=800)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    # house load = 50 - (-800) = 850; target = 1850
    assert ha.writes() == [
        ("switch.turn_off", "venus_battery_allow_discharge", None),
        ("number.set_value", "omnibattery_pd_target_grid_power", 1850.0),
        ("switch.turn_on", "venus_battery_allow_charge", None),
    ]


def test_discharge_bounds_power_and_keeps_a_positive_import_margin():
    clock = FakeClock(NOW)
    store = world(clock, grid=2400, ac=1000, charge="on", pd=2400, cap=0)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("discharge", 800)))
    assert ha.writes() == [
        ("switch.turn_off", "venus_battery_allow_charge", None),
        ("number.set_value", "omnibattery_system_max_discharge_power", 800.0),
        ("switch.turn_on", "omnibattery_system_power_limits", None),
        ("number.set_value", "omnibattery_pd_target_grid_power", 50.0),
        ("switch.turn_on", "venus_battery_allow_discharge", None),
    ]


def test_a_small_discharge_never_writes_the_unlimited_zero_cap():
    clock = FakeClock(NOW)
    store = world(clock)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("discharge", 30)))
    caps = [v for s, e, v in ha.writes() if e == "omnibattery_system_max_discharge_power"]
    assert caps == [100.0]


def test_planned_idle_turns_both_directions_off_then_resets_the_target():
    clock = FakeClock(NOW)
    store = world(clock, charge="on", pd=1800)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("idle")))
    assert ha.writes() == [
        ("switch.turn_off", "venus_battery_allow_charge", None),
        ("number.set_value", "omnibattery_pd_target_grid_power", 0.0),
    ]


@pytest.mark.parametrize(
    ("grid", "ac", "power", "target", "clamped"),
    [
        (4000, 0, 2000, 2500.0, True),  # never above min(import cap, entity max)
        (-300, 0, 100, 0.0, False),  # never a negative (export) target
        (1234.7, 0, 1000, 2234.0, False),  # rounded down
    ],
)
def test_charge_target_is_clamped_to_the_import_cap(grid, ac, power, target, clamped):
    clock = FakeClock(NOW)
    store = world(clock, grid=grid, ac=ac, pd=777)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("charge", power)))
    assert ha.writes()[0] == ("number.set_value", "omnibattery_pd_target_grid_power", target)
    assert record["desired"]["clamped"] is clamped


def test_the_entity_maximum_is_the_effective_charge_ceiling():
    # Omnibattery's PD target is bounded to +-2500 W (the batteries' summed charge limit).
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0)
    clock.advance(1)
    store.apply(
        entity_state(
            ENTS.pd_target, 0, clock.now(), attributes={"min": -2500, "max": 2500, "step": 10}
        )
    )
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("charge", 2500)))
    assert ha.writes()[0] == ("number.set_value", "omnibattery_pd_target_grid_power", 2500.0)
    assert record["desired"]["pd_target_w"] == 2500.0  # the value actually written
    assert record["desired"]["clamped"] is True


@pytest.mark.parametrize(
    ("mode", "power", "load", "clamped_w"),
    [
        ("charge", 1000, 400, 400.0),  # 400 + 1000 > 1000: only 600 W of charge requested
        ("charge", 500, 2000, 500.0),  # load alone above the ceiling: no charge at all
        ("charge", 500, 400, 0.0),
        ("discharge", 1200, 400, 400.0),  # cap entity max 800
        ("discharge", 600, 400, 0.0),
    ],
)
def test_clamped_battery_power_is_exported(mode, power, load, clamped_w):
    from battery_optimizer.service.metrics import _actuator_families

    # System power limits on, as on 2026-11-03: PD target -800..+1000, discharge cap max 800.
    clock = FakeClock(NOW)
    store = world(clock, grid=load, ac=0, limits="on")
    clock.advance(1)
    store.apply(
        entity_state(
            ENTS.pd_target, 0, clock.now(), attributes={"min": -800, "max": 1000, "step": 1}
        )
    )
    store.apply(
        entity_state(
            ENTS.max_discharge_power, 0, clock.now(), attributes={"min": 0, "max": 800, "step": 1}
        )
    )
    actuator, _, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent(mode, power)))
    assert record["desired"]["clamped_w"] == clamped_w
    assert record["desired"]["clamped"] is (clamped_w > 0)
    family = next(
        f for f in _actuator_families(actuator.snapshot()) if f.name.endswith("_clamped_watts")
    )
    assert family.name == "battery_optimizer_actuator_clamped_watts"
    assert [s.value for s in family.samples] == [clamped_w]


def test_battery_power_sign_is_configuration():
    clock = FakeClock(NOW)
    # The sensor reads -1000 while charging 1000 W: sign -1 makes it +1000 (charge positive).
    store = world(clock, grid=1400, ac=-1000, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, settings=replace(SETTINGS, battery_ac_power_sign=-1))
    record = tick(actuator, ctx(clock, intent("charge", 1000)))
    assert record["observed"]["house_load_w"] == 400
    assert ha.writes() == []  # already at 400 + 1000


def test_number_target_respects_the_entity_step_and_max():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    clock.advance(1)
    store.apply(
        entity_state(
            ENTS.pd_target, 0, clock.now(), attributes={"min": -3000, "max": 1000, "step": 10}
        )
    )
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert ha.writes()[0][2] == 1000.0


# ---------------------------------------------------------------------------
# Command policy: hysteresis and rate limit within a mode
# ---------------------------------------------------------------------------


def test_small_or_too_frequent_setpoint_changes_are_not_sent():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert len(ha.writes()) == 2

    def readings(grid, ac):
        store.apply(entity_state(GRID, grid, clock.now()))
        store.apply(entity_state(ENTS.battery_ac_power, ac, clock.now()))

    clock.advance(10)
    readings(1450, 1000)  # load 450: target 1450, 50 W from 1400
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert len(ha.writes()) == 2
    clock.advance(5)
    readings(1700, 1000)  # load 700: 300 W change, but the last write was 15 s ago
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert len(ha.writes()) == 2
    clock.advance(20)
    readings(1700, 1000)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert ha.writes()[-1] == ("number.set_value", "omnibattery_pd_target_grid_power", 1700.0)


def test_switching_a_direction_on_always_restores_its_numbers():
    # Discharge was switched off from outside (watchdog / owner) and the target reset to 0:
    # the 50 W margin is below min_setpoint_change_w, but it must be written before turn_on.
    clock = FakeClock(NOW)
    store = world(clock, grid=600, ac=0, discharge="on", limits="on", pd=50, cap=500)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("discharge", 500)))
    assert ha.writes() == []
    clock.advance(10)
    store.apply(entity_state(ENTS.allow_discharge, "off", clock.now()))
    store.apply(entity_state(ENTS.pd_target, 0, clock.now(), attributes=NUMBER_ATTRS))
    store.apply(entity_state(GRID, 600, clock.now()))
    tick(actuator, ctx(clock, intent("discharge", 500)))
    assert ha.writes() == [
        ("number.set_value", "omnibattery_pd_target_grid_power", 50.0),
        ("switch.turn_on", "venus_battery_allow_discharge", None),
    ]


def test_the_discharge_margin_and_cap_are_never_rate_limited():
    clock = FakeClock(NOW)
    store = world(clock, grid=600, ac=0, discharge="on", limits="on", pd=50, cap=500)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("discharge", 500)))
    clock.advance(5)  # within min_command_interval_s, changes below min_setpoint_change_w
    store.apply(entity_state(ENTS.pd_target, 0, clock.now(), attributes=NUMBER_ATTRS))
    store.apply(entity_state(ENTS.max_discharge_power, 450, clock.now(), attributes=CAP_ATTRS))
    store.apply(entity_state(GRID, 600, clock.now()))
    tick(actuator, ctx(clock, intent("discharge", 500)))
    assert ha.writes() == [
        ("number.set_value", "omnibattery_system_max_discharge_power", 500.0),
        ("number.set_value", "omnibattery_pd_target_grid_power", 50.0),
    ]


# ---------------------------------------------------------------------------
# Kill switch re-checked before every enabling write
# ---------------------------------------------------------------------------


def test_kill_switch_off_mid_reconcile_aborts_before_enabling():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    # the owner turns the kill switch off while the PD target write is being read back
    ha.after[ENTS.pd_target] = lambda: store.apply(
        entity_state(ENTS.kill_switch, "off", clock.now())
    )
    record = tick(actuator, ctx(clock, intent("charge", 1000)))
    assert ha.writes() == [("number.set_value", "omnibattery_pd_target_grid_power", 1400.0)]
    assert ha.heartbeats() == []
    assert actuator.reasons == ("kill_switch_off",)
    assert actuator.desired == safe_idle("kill_switch_off")
    assert not actuator.in_control and actuator.alert is None
    assert record["writes"][-1]["result"] == "aborted"
    # off stays off: nothing more is written
    clock.advance(30)
    store.apply(entity_state(GRID, 400, clock.now()))
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert len(ha.writes()) == 1


def test_kill_switch_off_mid_reconcile_still_turns_the_direction_off():
    clock = FakeClock(NOW)
    store = world(clock, grid=600, ac=-500, charge="on", pd=1400, cap=500)
    actuator, ha, _ = make(clock, store)
    ha.after[ENTS.allow_charge] = lambda: store.apply(
        entity_state(ENTS.kill_switch, "off", clock.now())
    )
    tick(actuator, ctx(clock, intent("discharge", 500)))
    # turn_off is never blocked; the cap write after it is
    assert ha.writes() == [("switch.turn_off", "venus_battery_allow_charge", None)]
    assert store.get(ENTS.allow_discharge).state == "off"


# ---------------------------------------------------------------------------
# Readback, retries, failure -> safe idle
# ---------------------------------------------------------------------------


def test_readback_timeout_retries_then_falls_back_to_safe_idle():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    actuator, ha, metrics = make(clock, store, silent={ENTS.pd_target})
    record = tick(actuator, ctx(clock, intent("charge", 1000)))
    pd_calls = [c for c in ha.writes() if c[1] == "omnibattery_pd_target_grid_power"]
    assert len(pd_calls) == SETTINGS.max_attempts
    assert ("switch.turn_on", "venus_battery_allow_charge", None) not in ha.writes()
    assert actuator.desired == safe_idle("write_failed")
    assert actuator.alert == "write_failed"
    assert counter(metrics, "actuator_write_failures", reason="readback_timeout") == 3
    assert counter(metrics, "actuator_writes", role="pd_target", result="failed") == 1
    assert counter(metrics, "actuator_safe_idles", reason="write_failed") == 1
    # safe idle itself succeeded (both switches already off): still alive, still heartbeating
    assert record["heartbeat"] == "ok"


def test_a_transient_ha_error_is_retried():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    actuator, ha, metrics = make(clock, store)
    ha.refuse_once.add(ENTS.pd_target)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert actuator.alert is None
    assert counter(metrics, "actuator_write_failures", reason="ha_error") == 1
    assert store.get(ENTS.allow_charge).state == "on"


def test_a_forbidden_write_is_never_retried():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    actuator, ha, metrics = make(clock, store, forbid={ENTS.pd_target})
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert len([c for c in ha.calls if c[2] == ENTS.pd_target]) == 1
    assert counter(metrics, "actuator_write_failures", reason="forbidden") == 1
    assert actuator.alert == "write_failed"


def test_when_even_safe_idle_fails_the_heartbeat_stops():
    clock = FakeClock(NOW)
    store = world(clock, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, refuse={ENTS.allow_charge, ENTS.allow_discharge})
    record = tick(actuator, ctx(clock, None))  # no plan -> safe idle -> refused
    assert actuator.alert == "safe_idle_failed"
    assert ha.heartbeats() == [] and record["heartbeat"] is None


# ---------------------------------------------------------------------------
# The guard: every reason forces safe idle
# ---------------------------------------------------------------------------


def _stale(store, clock, entity):
    store.apply(entity_state(entity, 1, clock.now() - timedelta(hours=1)))


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda s, c, k: k.update(intent=None, decision="no_plan"), "no_plan"),
        (lambda s, c, k: k.update(intent=None, decision="plan_expired"), "plan_expired"),
        (lambda s, c, k: k.update(intent=None, decision="not_ready"), "inputs_not_ready"),
        (
            lambda s, c, k: k.update(intent=intent("charge", valid_from=SLOT - timedelta(hours=1))),
            "intent_expired",
        ),
        (lambda s, c, k: k.update(plan_from_this_process=False), "plan_not_from_this_process"),
        (lambda s, c, k: k.update(price_fresh=False), "price_stale"),
        (lambda s, c, k: c.advance(31), "grid_power_stale"),
        (lambda s, c, k: s.apply(entity_state(GRID, "unavailable", c.now())), "grid_power_stale"),
        (lambda s, c, k: s.apply(entity_state(ENTS.battery_soc, 101, c.now())),
         "battery_soc_stale"),
        (
            lambda s, c, k: s.apply(entity_state(ENTS.battery_ac_power, "unknown", c.now())),
            "battery_power_stale",
        ),
        (
            lambda s, c, k: s.apply(entity_state(ENTS.integration_status, "manual", c.now())),
            "integration_unhealthy",
        ),
        (
            lambda s, c, k: s.apply(
                entity_state(ENTS.integration_status, "unavailable", c.now())
            ),
            "integration_unhealthy",
        ),
        (
            lambda s, c, k: s.apply(entity_state(ENTS.integration_status, "backup_mode", c.now())),
            "integration_unhealthy",
        ),
    ],
)  # fmt: skip
def test_every_guard_reason_forces_safe_idle(setup, reason):
    clock = FakeClock(NOW)
    store = world(clock, charge="on", pd=1400)
    actuator, ha, metrics = make(clock, store)
    kw = {"intent": intent("charge", 1000)}
    clock.advance(1)  # a later report than the world's
    setup(store, clock, kw)
    tick(actuator, ctx(clock, **kw))
    assert reason in actuator.reasons and reason in GUARD_REASONS
    assert actuator.desired.mode == "safe_idle"
    # only the direction switches are touched in safe idle
    assert ha.writes() == [("switch.turn_off", "venus_battery_allow_charge", None)]
    assert counter(metrics, "actuator_safe_idles", reason=actuator.reasons[0]) == 1


def test_missing_sensors_are_stale():
    clock = FakeClock(NOW)
    store = world(clock, ac=None, soc=None, status=None)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert {"battery_soc_stale", "battery_power_stale", "integration_unhealthy"} <= set(
        actuator.reasons
    )
    assert ha.writes() == []


def test_an_idle_battery_with_hours_old_readings_can_start_a_charge():
    # Omnibattery reports SOC and AC power only on change: hours old but available is healthy.
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0, at=NOW - timedelta(hours=5))
    store.apply(entity_state(GRID, 400, clock.now()))  # the Shelly meter reports every 2 s
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert actuator.reasons == ()
    assert ha.writes() == [
        ("number.set_value", "omnibattery_pd_target_grid_power", 1400.0),
        ("switch.turn_on", "venus_battery_allow_charge", None),
    ]


@pytest.mark.parametrize(
    ("entity", "state", "reason"),
    [
        (ENTS.battery_soc, "unavailable", "battery_soc_stale"),
        (ENTS.battery_ac_power, "unavailable", "battery_power_stale"),
        (ENTS.battery_liveness, "1", "battery_unresponsive"),
        (ENTS.battery_liveness, "venus", "battery_unresponsive"),
        (ENTS.battery_liveness, "unavailable", "battery_unresponsive"),
        (ENTS.battery_liveness, "none", "battery_unresponsive"),  # the healthy text is exact
    ],
)
def test_unavailable_battery_or_unresponsive_liveness_holds_safe_idle(entity, state, reason):
    clock = FakeClock(NOW)
    store = world(clock, charge="on", pd=1400)
    clock.advance(1)
    store.apply(entity_state(entity, state, clock.now()))
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert actuator.reasons == (reason,)
    assert ha.writes() == [("switch.turn_off", "venus_battery_allow_charge", None)]


def test_a_missing_liveness_sensor_is_unresponsive_and_an_unset_one_is_not_checked():
    clock = FakeClock(NOW)
    store = world(clock, liveness=None)
    actuator, _, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("idle")))
    assert actuator.reasons == ("battery_unresponsive",)
    unchecked = replace(SETTINGS, entities=replace(ENTS, battery_liveness=None))
    actuator, _, _ = make(clock, store, settings=unchecked)
    tick(actuator, ctx(clock, intent("idle")))
    assert actuator.reasons == ()


def test_a_configured_battery_age_is_still_enforced():
    clock = FakeClock(NOW)
    store = world(clock, at=NOW - timedelta(seconds=400))
    store.apply(entity_state(GRID, 400, clock.now()))
    aged = replace(SETTINGS, battery_soc_max_age_s=300.0, battery_ac_power_max_age_s=60.0)
    actuator, _, _ = make(clock, store, settings=aged)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert actuator.reasons == ("battery_soc_stale", "battery_power_stale")
    actuator, _, _ = make(clock, store, settings=replace(aged, battery_soc_max_age_s=500.0))
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert actuator.reasons == ("battery_power_stale",)


def test_import_over_cap_while_charging_trips_charge_off_and_holds_it():
    clock = FakeClock(NOW)
    store = world(clock, grid=4800, ac=1500, charge="on", pd=4500)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 2000)))
    assert actuator.reasons[0] == "import_over_cap"
    assert ha.writes() == [("switch.turn_off", "venus_battery_allow_charge", None)]
    # import back below the cap, still within the hold: charge stays refused
    clock.advance(60)
    store.apply(entity_state(GRID, 3000, clock.now()))
    store.apply(entity_state(ENTS.battery_ac_power, 0, clock.now()))
    tick(actuator, ctx(clock, intent("charge", 2000)))
    assert actuator.reasons == ("import_over_cap",)
    assert len(ha.writes()) == 1
    # after the hold, charging resumes
    clock.advance(SETTINGS.import_trip_hold_s)
    store.apply(entity_state(GRID, 3000, clock.now()))
    store.apply(entity_state(ENTS.battery_ac_power, 0, clock.now()))
    store.apply(entity_state(ENTS.battery_soc, 50, clock.now()))
    tick(actuator, ctx(clock, intent("charge", 1000, valid_from=floor_slot(clock.now(), 900))))
    assert actuator.reasons == ()
    assert ha.writes()[-1] == ("switch.turn_on", "venus_battery_allow_charge", None)


def test_high_import_while_not_charging_is_not_a_trip():
    clock = FakeClock(NOW)
    store = world(clock, grid=4800, ac=-500, discharge="on", limits="on", pd=50, cap=500)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("discharge", 500)))
    assert "import_over_cap" not in actuator.reasons
    assert ha.writes() == []  # already discharging as asked


# ---------------------------------------------------------------------------
# Kill switch, heartbeat, HA sync, shutdown, dry run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kill", ["off", "unavailable", None])
def test_nothing_is_written_while_the_kill_switch_is_not_on(kill):
    clock = FakeClock(NOW)
    store = world(clock, kill=kill, charge="on")
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert ha.calls == []  # no write, no heartbeat
    assert actuator.reasons == ("kill_switch_off",)
    assert asyncio.run(actuator.shutdown()) is True
    assert ha.calls == []  # never in control: nothing to hand back


def test_kill_switch_off_edge_sets_safe_idle_once_then_stops_writing():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert store.get(ENTS.allow_charge).state == "on"
    calls = len(ha.calls)
    clock.advance(1)
    store.apply(entity_state(ENTS.kill_switch, "off", clock.now()))
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert ha.calls[calls:] == [("switch", "turn_off", ENTS.allow_charge, None)]
    assert store.get(ENTS.allow_charge).state == "off" and not actuator.in_control
    clock.advance(30)
    store.apply(entity_state(ENTS.allow_charge, "on", clock.now()))  # the owner takes over
    tick(actuator, ctx(clock, intent("charge", 1000)))
    assert len(ha.calls) == calls + 1


def test_heartbeat_is_published_every_reconciled_tick():
    clock = FakeClock(NOW)
    store = world(clock)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, intent("idle")))
    clock.advance(30)
    store.apply(entity_state(GRID, 400, clock.now()))
    store.apply(entity_state(ENTS.battery_ac_power, 0, clock.now()))
    at = clock.now()
    tick(actuator, ctx(clock, intent("idle")))
    beats = ha.heartbeats()
    assert len(beats) == 2
    assert beats[0][1:3] == ("set_datetime", ENTS.heartbeat)
    assert beats[1][3] == {"timestamp": at.timestamp()}
    assert counter(metrics, "actuator_heartbeats", result="ok") == 2


def test_no_writes_without_a_synced_home_assistant():
    clock = FakeClock(NOW)
    store = world(clock, charge="on")
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000), ha_synced=False))
    assert ha.calls == [] and actuator.reasons == ("ha_not_synced",)


def test_shutdown_in_control_sets_safe_idle():
    clock = FakeClock(NOW)
    store = world(clock, grid=900, ac=-500, discharge="on", limits="on", pd=50, cap=500)
    records: list = []
    actuator, ha, _ = make(clock, store, records=records)
    tick(actuator, ctx(clock, intent("discharge", 500)))
    assert actuator.in_control and ha.writes() == []
    assert asyncio.run(actuator.shutdown()) is True
    assert ha.writes() == [("switch.turn_off", "venus_battery_allow_discharge", None)]
    assert records[-1]["kind"] == "actuation_shutdown"
    assert not actuator.in_control


def test_dry_run_sends_nothing_but_records_every_write():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    records: list = []
    actuator, ha, metrics = make(clock, store, mode="dry_run", records=records)
    record = tick(actuator, ctx(clock, intent("charge", 1000)))
    assert ha.calls == []
    assert [w["result"] for w in record["writes"]] == ["dry_run", "dry_run"]
    assert [w["role"] for w in record["writes"]] == ["pd_target", "allow_charge"]
    assert record["heartbeat"] == "dry_run" and record["sent"] is False
    assert records == [record]
    assert counter(metrics, "actuator_writes", role="pd_target", result="dry_run") == 1
    asyncio.run(actuator.shutdown())  # records the would-be safe idle (nothing to switch off)
    assert ha.calls == []


def test_dry_run_plans_against_its_simulated_state_so_it_does_not_repeat():
    clock = FakeClock(NOW)
    store = world(clock, grid=400, ac=0)
    records: list = []
    actuator, ha, _ = make(clock, store, mode="dry_run", records=records)
    first = tick(actuator, ctx(clock, intent("charge", 1000)))
    assert [w["role"] for w in first["writes"]] == ["pd_target", "allow_charge"]
    clock.advance(30)
    store.apply(entity_state(GRID, 400, clock.now()))
    second = tick(actuator, ctx(clock, intent("charge", 1000)))
    assert second["writes"] == [] and ha.calls == []
    assert store.get(ENTS.allow_charge).state == "off"  # the real entity is untouched
    # the charge target follows the load, subject to the same hysteresis as live
    clock.advance(30)
    store.apply(entity_state(GRID, 800, clock.now()))
    third = tick(actuator, ctx(clock, intent("charge", 1000)))
    assert [(w["role"], w["value"]) for w in third["writes"]] == [("pd_target", 1800.0)]
    assert len(records) == 2


def test_unchanged_ticks_are_not_recorded_again():
    clock = FakeClock(NOW)
    store = world(clock)
    records: list = []
    actuator, _, _ = make(clock, store, records=records)
    tick(actuator, ctx(clock, intent("idle")))
    tick(actuator, ctx(clock, intent("idle")))
    assert len(records) == 1


def test_actuator_refuses_shadow_mode_and_a_missing_sign():
    clock = FakeClock(NOW)
    store = world(clock)
    with pytest.raises(ValueError):
        make(clock, store, mode="shadow")
    with pytest.raises(ValueError):
        make(clock, store, settings=replace(SETTINGS, battery_ac_power_sign=None))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LIVE = {
    "mode": "live",
    "actuation.battery_ac_power_sign": 1,
    "planning.soc_source": "entity",
    "entities.battery_soc": "sensor.test_soc",
}


def test_committed_actuation_section_names_the_installation_entities():
    config = load_service_config(REPO / "config" / "service.yaml")
    assert config.actuation.entities == ENTS
    # measured 2026-11-03: sensor.venus_ac_power is negative while charging
    assert config.actuation.battery_ac_power_sign == -1
    # availability only: Omnibattery reports SOC and AC power on change only
    assert config.actuation.battery_soc_max_age_s is None
    assert config.actuation.battery_ac_power_max_age_s is None
    assert config.actuation.grid_power_max_age_s == 30
    assert config.actuation.import_cap_w == 4500
    policy = config.actuation.command_policy()
    assert (policy.max_attempts, policy.min_setpoint_change_w) == (3, 100)


def test_the_actuation_section_does_not_change_the_plan_identity(tmp_path):
    base = load_service_config(write_service_config(tmp_path))
    other = load_service_config(write_service_config(tmp_path, **{"actuation.import_cap_w": 3000}))
    assert base.config_hash() == other.config_hash()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"mode": "dry_run", "actuation.battery_ac_power_sign": None}, "battery_ac_power_sign"),
        ({**LIVE, "planning.soc_source": "simulated"}, "soc_source=entity"),
        ({**LIVE, "actuation.entities.pd_target": None}, "needs actuation.entities.pd_target"),
        ({**LIVE, "actuation.entities.allow_charge": "number.x"}, "switch"),
        ({**LIVE, "actuation.entities.kill_switch": ENTS.allow_charge}, "input_boolean"),
        ({**LIVE, "actuation.entities.power_limits": ENTS.allow_charge}, "distinct"),
        ({**LIVE, "actuation.battery_ac_power_sign": 2}, "1, -1"),
        ({**LIVE, "actuation.battery_ac_power_sign": True}, "1, -1"),
        ({**LIVE, "actuation.discharge_margin_w": 5000}, "must not exceed import_cap_w"),
        ({**LIVE, "actuation.min_discharge_cap_w": 0}, "min_discharge_cap_w"),
        ({**LIVE, "actuation.min_discharge_cap_w": 3000}, "max_discharge_cap_w"),
        ({**LIVE, "actuation.command_policy.max_attempts": 0}, "max_attempts"),
        ({**LIVE, "actuation.healthy_states": []}, "healthy_states"),
        ({**LIVE, "actuation.bogus": 1}, "unknown actuation keys"),
        ({**LIVE, "actuation.freshness_s.price": 1}, "unknown actuation.freshness_s keys"),
        ({**LIVE, "actuation.freshness_s.grid_power": None}, "freshness_s.grid_power"),
        ({**LIVE, "actuation.discharge_margin_w": 0}, "discharge_margin_w must be > 0"),
        ({**LIVE, "actuation.import_cap_w": 4800}, "below the 5000 W contract"),
        ({**LIVE, "actuation.import_guard_margin_w": 500}, "below the 5000 W contract"),
        ({**LIVE, "actuation.tick_interval_s": 121}, "tick_interval_s must be <= 120"),
        ({**LIVE, "actuation.max_discharge_cap_w": 3000}, "battery's 2500 W"),
        ({**LIVE, "actuation.freshness_s.battery_soc": 0}, "freshness_s.battery_soc"),
        ({**LIVE, "actuation.entities.battery_liveness": "binary_sensor.x"}, "battery_liveness"),
        ({**LIVE, "actuation.entities.battery_liveness": None}, "needs actuation.entities.batt"),
    ],
)  # fmt: skip
def test_invalid_actuation_config_is_refused(tmp_path, override, message):
    with pytest.raises(ValueError, match=message):
        load_service_config(write_service_config(tmp_path, **override))


def test_dry_run_and_live_configs_load(tmp_path):
    dry = load_service_config(
        write_service_config(tmp_path, mode="dry_run", **{"actuation.battery_ac_power_sign": -1})
    )
    assert dry.mode == "dry_run" and dry.actuation.battery_ac_power_sign == -1
    live = load_service_config(write_service_config(tmp_path, **LIVE))
    assert live.mode == "live"
    aged = load_service_config(
        write_service_config(
            tmp_path,
            **{
                **LIVE,
                "actuation.freshness_s.battery_soc": 600,
            },
        )
    )
    assert aged.actuation.battery_soc_max_age_s == 600


# ---------------------------------------------------------------------------
# Runtime wiring
# ---------------------------------------------------------------------------


def make_service(tmp_path, clock, mode="live"):
    config = service_config(tmp_path, **{**LIVE, "mode": mode})
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="test-sha",
        ws_transport=FakeHAServer(),
    )
    service.ha.stats.connected = True
    service.ha.stats.synced = True
    service.rest = None  # no REST refresh against a real network in unit tests
    return service


@pytest.fixture
def cleanup():
    services = []
    yield services.append
    for service in services:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)


def _live_world(service, clock):
    store = service.store
    for entity, value, attrs in (
        (ENTS.battery_ac_power, 0, {}),
        (ENTS.battery_soc, 50, {}),
        (ENTS.battery_liveness, "None", {}),
        (ENTS.integration_status, "balanced", {}),
        (ENTS.kill_switch, "on", {}),
        (ENTS.allow_charge, "off", {}),
        (ENTS.allow_discharge, "off", {}),
        (ENTS.power_limits, "off", {}),
        (ENTS.pd_target, 0, NUMBER_ATTRS),
        (ENTS.max_discharge_power, 0, CAP_ATTRS),
    ):
        store.apply(entity_state(entity, value, clock.now(), attributes=attrs))


def test_shadow_service_has_no_actuator_and_an_unchanged_surface(tmp_path, cleanup):
    clock = FakeClock(NOW)
    config = service_config(tmp_path)
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="t",
        ws_transport=FakeHAServer(),
    )
    cleanup(service)
    assert service.actuator is None and service.ha._actuation is None
    assert ENTS.allow_charge not in service.store.tracked
    status = service.status()
    assert status["mode"] == "shadow" and "actuator" not in status
    assert status["actuation"].startswith("none (read-only adapters")
    assert b"actuator" not in service.metrics.render()


def test_actuator_wakes_on_kill_switch_changes_and_debounced_over_cap_import(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    cleanup(service)
    wake = service._actuator_wake

    def grid(value):
        clock.advance(2)
        service.store.apply(entity_state(GRID, value, clock.now()))

    grid(4800)  # over the cap, but not charging: no wake
    assert not wake.is_set()
    service.actuator.readings = {"allow_charge": True}
    grid(4800)
    assert wake.is_set()
    wake.clear()
    grid(4900)  # 2 s later: debounced
    assert not wake.is_set()
    clock.advance(30)
    grid(4900)  # a tick interval later: wakes again
    assert wake.is_set()
    wake.clear()
    grid(1000)  # under the cap
    assert not wake.is_set()
    clock.advance(1)
    service.store.apply(entity_state(ENTS.kill_switch, "off", clock.now()))
    assert wake.is_set()


def test_restart_needs_a_plan_solved_by_this_process(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    cleanup(service)
    feed(service, NOW)
    _live_world(service, clock)
    assert asyncio.run(service.request_solve("test")) == "ok"
    plan = service.plans[-1]
    first = next_slot_boundary(NOW)
    clock.set(first + timedelta(seconds=1))
    feed(service, clock.now(), history=True)
    _live_world(service, clock)
    asyncio.run(service.open_slot(first))
    context = service.actuation_context(clock.now())
    assert context.intent is not None and context.plan_from_this_process
    # A plan this process did not solve (persisted, or from before the start) is never acted on.
    service._started_at = plan.plan.generated_at + timedelta(seconds=1)
    assert not service.actuation_context(clock.now()).plan_from_this_process
    service.plans.clear()
    assert not service.actuation_context(clock.now()).plan_from_this_process


def test_dry_run_service_records_actuation_and_sends_nothing(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, mode="dry_run")
    cleanup(service)
    sent = []

    async def send(*args):  # pragma: no cover - must never be called
        sent.append(args)

    service.actuator._send = send
    feed(service, NOW)
    _live_world(service, clock)
    assert asyncio.run(service.request_solve("test")) == "ok"
    first = next_slot_boundary(NOW)
    clock.set(first + timedelta(seconds=1))
    feed(service, clock.now())
    _live_world(service, clock)

    async def go():
        await service.open_slot(first)
        return await service.actuator.tick(service.actuation_context(clock.now()))

    record = asyncio.run(go())
    assert sent == []
    assert record["mode"] == "dry_run" and record["guard_reasons"] == []
    files = list((tmp_path / "data" / "service" / "v1" / "actuation").rglob("actuation.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows[0]["kind"] == "actuation_tick" and rows[0]["code_version"] == "test-sha"
    status = service.status()
    assert status["mode"] == "dry_run" and status["actuator"]["sends"] is False
    text = service.metrics.render().decode()
    assert 'battery_optimizer_actuator_mode{mode="dry_run"} 1.0' in text
    assert "battery_optimizer_actuator_guard_ok 1.0" in text


def test_live_service_stop_sets_safe_idle_before_closing_ha(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    cleanup(service)
    _live_world(service, clock)
    feed(service, NOW)
    fake = FakeHA(service.store, clock)
    service.actuator._send = fake.send
    clock.advance(1)
    service.store.apply(entity_state(ENTS.allow_charge, "on", clock.now()))

    async def go():
        await service.actuator.tick(service.actuation_context(clock.now()))  # no plan: idle
        clock.advance(1)
        service.store.apply(entity_state(ENTS.allow_discharge, "on", clock.now()))  # remote edit
        await service.stop()

    asyncio.run(go())
    assert fake.writes() == [
        ("switch.turn_off", "venus_battery_allow_charge", None),
        ("switch.turn_off", "venus_battery_allow_discharge", None),
    ]
    status = service.status()
    assert status["actuator"]["in_control"] is False


def test_live_service_end_to_end_through_the_guarded_websocket(tmp_path):
    """Lifespan start -> HA sync -> safe idle + heartbeat over the WebSocket -> stop."""
    now = datetime.now(UTC)
    day = local_date(now, TZ)
    server = FakeHAServer(
        [
            ha_state(GRID, 420, now, attributes={"unit_of_measurement": "W"}),
            ha_state("sensor.esios_pvpc", 0.15, now, attributes=pvpc_attributes(day)),
            ha_state(ENTS.kill_switch, "on", now),
            ha_state(ENTS.integration_status, "balanced", now),
            ha_state(ENTS.battery_ac_power, 0, now),
            ha_state(ENTS.battery_soc, 50, now),
            ha_state(ENTS.battery_liveness, "None", now),
            ha_state(ENTS.allow_charge, "on", now),  # left on, and no plan yet: switched off
            ha_state(ENTS.allow_discharge, "off", now),
            ha_state(ENTS.power_limits, "off", now),
            ha_state(ENTS.pd_target, 0, now, attributes=NUMBER_ATTRS),
            ha_state(ENTS.max_discharge_power, 0, now, attributes=CAP_ATTRS),
        ]
    )
    config = service_config(tmp_path, **LIVE)
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=SystemClock(),
        code_version="e2e",
        ws_transport=server,
        history_fetcher=lambda at, d: flat_history(d),
    )
    service.rest = None  # no network: the WebSocket is the only path to HA here
    app = create_app(service)

    def calls():
        return [(m["domain"], m["service"], m["target"]["entity_id"]) for m in server.service_calls]

    heartbeat = ("input_datetime", "set_datetime", ENTS.heartbeat)

    async def go():
        async with app.router.lifespan_context(app):
            await wait_until(lambda: heartbeat in calls(), timeout=10)
            status = service.status()
            assert status["mode"] == "live" and status["actuator"]["in_control"] is True

    asyncio.run(go())
    assert calls()[0] == ("switch", "turn_off", ENTS.allow_charge)
    written = {entity for _, _, entity in calls()}
    assert written <= {ENTS.allow_charge, ENTS.allow_discharge, ENTS.heartbeat}
