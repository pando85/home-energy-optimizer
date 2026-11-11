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
    WRITE_ROLES,
    ActuationContext,
    Actuator,
    FullChargeDirective,
    safe_idle,
)
from battery_optimizer.service.api import create_app
from battery_optimizer.service.clock import FakeClock, SystemClock
from battery_optimizer.service.config import (
    ActuationEntities,
    ActuationSettings,
    FullChargeCadence,
    FullChargeEntities,
    FullChargeSettings,
    LoadFollowingSettings,
    load_service_config,
)
from battery_optimizer.service.entity_store import EntityStore
from battery_optimizer.service.ha_client import HACommandError
from battery_optimizer.service.ha_guard import WEEKDAY_OPTIONS, ForbiddenHACallError
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
        elif domain == "select":
            value = data["option"]
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
    extra=(),
):
    store = EntityStore.for_entities((GRID, *ENTS.all(), *extra))
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


def make(
    clock, store, *, mode="live", settings=SETTINGS, records=None, full_charge=None, **fake_kw
):
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
        full_charge=full_charge,
        tz=TZ,
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


def _actuator_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name.endswith(".actuator")]


def test_transitions_are_logged_once_and_not_on_an_unchanged_tick(caplog):
    caplog.set_level("INFO", logger="battery_optimizer.service.actuator")
    clock = FakeClock(NOW)
    store = world(clock)
    actuator, _, _ = make(clock, store)
    tick(actuator, ctx(clock))  # no plan: safe idle
    lines = _actuator_lines(caplog)
    assert "actuator kill_switch=on previous=unknown" in lines
    assert "actuator in_control=True" in lines
    assert "actuator guard reasons=no_plan" in lines
    assert any(m.startswith("actuator desired mode=safe_idle reason=no_plan") for m in lines)
    caplog.clear()
    clock.advance(30)
    store.apply(entity_state(GRID, 400, clock.now()))
    store.apply(entity_state(ENTS.battery_ac_power, 0, clock.now()))
    tick(actuator, ctx(clock))
    assert _actuator_lines(caplog) == []  # unchanged: nothing logged
    clock.advance(1)
    store.apply(entity_state(ENTS.kill_switch, "off", clock.now()))
    tick(actuator, ctx(clock))
    lines = _actuator_lines(caplog)
    assert "actuator kill_switch=off previous=on" in lines
    assert "actuator in_control=False" in lines
    assert "actuator guard reasons=kill_switch_off" in lines


def test_each_write_is_logged_with_its_role_and_value(caplog):
    caplog.set_level("INFO", logger="battery_optimizer.service.actuator")
    clock = FakeClock(NOW)
    actuator, _, _ = make(clock, world(clock, grid=400, ac=0))
    tick(actuator, ctx(clock, intent("charge", 1000)))
    lines = _actuator_lines(caplog)
    assert "actuator write role=pd_target service=number.set_value value=1400 result=ok" in lines
    assert "actuator write role=allow_charge service=switch.turn_on value=- result=ok" in lines
    assert "actuator guard reasons=ok" not in lines  # the reasons were already empty
    assert not any("heartbeat" in m for m in lines)  # the heartbeat is never logged per tick

    caplog.clear()
    clock = FakeClock(NOW)
    dry, _, _ = make(clock, world(clock, grid=400, ac=0), mode="dry_run")
    tick(dry, ctx(clock, intent("charge", 1000)))
    assert (
        "actuator dry_run write role=pd_target service=number.set_value value=1400 "
        "result=dry_run" in _actuator_lines(caplog)
    )


def test_an_import_trip_start_and_end_are_warnings(caplog):
    caplog.set_level("INFO", logger="battery_optimizer.service.actuator")
    clock = FakeClock(NOW)
    store = world(clock, grid=4800, ac=1500, charge="on", pd=4500)
    actuator, _, _ = make(clock, store)
    for advance, grid in ((0, 4800), (60, 3000), (SETTINGS.import_trip_hold_s, 3000)):
        clock.advance(advance)
        for entity, value in ((GRID, grid), (ENTS.battery_ac_power, 0), (ENTS.battery_soc, 50)):
            store.apply(entity_state(entity, value, clock.now()))
        tick(actuator, ctx(clock, intent("charge", 1000, valid_from=floor_slot(clock.now(), 900))))
    trips = [
        (r.levelname, r.getMessage()) for r in caplog.records if "import trip" in r.getMessage()
    ]
    assert [level for level, _ in trips] == ["WARNING", "WARNING"]
    assert trips[0][1].startswith("actuator import trip grid_power_w=4800")
    assert trips[1][1] == "actuator import trip ended"


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


def test_grid_power_updates_feed_the_discharge_metrics_only_while_acting(tmp_path, cleanup):
    from battery_optimizer.service.actuator import Desired

    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, mode="dry_run")
    cleanup(service)
    _live_world(service, clock)
    clock.advance(1)
    service.store.apply(entity_state(ENTS.battery_ac_power, -700, clock.now()))
    registry = service.metrics.registry
    hist = "battery_optimizer_actuator_discharge_grid_watts"
    export = "battery_optimizer_actuator_export_seconds_total"
    band = {"power_band": "500_1000"}

    def grid(value):
        clock.advance(2)
        service.store.apply(entity_state(GRID, value, clock.now()))

    grid(-40)  # no tick yet: not in control
    assert registry.get_sample_value(f"{hist}_count", band) == 0.0
    # What a discharge tick leaves behind.
    service.actuator.in_control = True
    service.actuator.last_tick_at = clock.now()
    service.actuator.desired = Desired(mode="discharge", reason="intent", allow_discharge=True)
    grid(-40)
    grid(-30)
    assert registry.get_sample_value(f"{hist}_count", band) == 2.0
    assert registry.get_sample_value(export) == pytest.approx(2.0)
    # Another entity's update is not a grid-power sample.
    service.store.apply(entity_state(ENTS.battery_soc, 49, clock.now()))
    assert registry.get_sample_value(f"{hist}_count", band) == 2.0


def test_shadow_service_exposes_no_discharge_metrics_and_ignores_grid_updates(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, mode="shadow")
    cleanup(service)
    assert service.actuator is None
    service.store.apply(entity_state(GRID, -40, clock.now()))
    text = service.metrics.render().decode()
    assert "actuator_discharge_grid" not in text and "actuator_export_seconds" not in text
    assert "actuator_discharge_margin" not in text


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


def test_restart_mid_slot_takes_control_in_that_slot_through_the_full_guard(tmp_path, cleanup):
    clock = FakeClock(NOW)  # a fresh process, 5 minutes into the 10:45 slot
    service = make_service(tmp_path, clock, mode="dry_run")
    cleanup(service)
    _live_world(service, clock)
    service.ha.stats.synced = False  # the slot opens before the first HA sync
    asyncio.run(service.open_slot(SLOT))
    assert service.actuation_context(NOW).decision == "not_ready"
    service.ha.stats.synced = True
    feed(service, NOW)

    async def go():
        assert await service.request_solve("event:resync") == "ok"  # a catch-up plan
        return await service.actuator.tick(service.actuation_context(clock.now()))

    record = asyncio.run(go())
    plan = service.plans[-1]
    assert service._slot_open[SLOT].decision == "late_intent"
    assert record["guard_reasons"] == [] and record["desired"]["reason"] == "intent"
    assert record["intent"]["plan_id"] == plan.plan_id
    # The kill switch still gates a late intent like any other.
    clock.advance(1)
    service.store.apply(entity_state(ENTS.kill_switch, "off", clock.now()))
    record = asyncio.run(service.actuator.tick(service.actuation_context(clock.now())))
    assert "kill_switch_off" in record["guard_reasons"]


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


# ---------------------------------------------------------------------------
# Periodic full charge: Omnibattery's weekly full charge as the enabler (ADR-0007 amendment)
# ---------------------------------------------------------------------------

FC_ENTS = FullChargeEntities(
    enable_switch="switch.omnibattery_weekly_full_charge_enabled",
    day_select="select.omnibattery_weekly_full_charge_day",
    delay_switch="switch.omnibattery_weekly_full_charge_delay",
    charge_delay_switch="switch.omnibattery_charge_delay",
    last_balance_read="sensor.venus_last_balance_read",
    balancing_mode="binary_sensor.venus_balancing_mode",
    cell_delta="sensor.venus_cell_delta",
    balance_status="sensor.venus_balance_status",
    delta_trend="sensor.venus_delta_trend",
    delta_avg_4w="sensor.venus_delta_avg_4w",
)
FC = FullChargeSettings(enabled=True, entities=FC_ENTS)
TODAY = WEEKDAY_OPTIONS[NOW.astimezone(TZ).weekday()]  # local Tuesday
ARMED = FullChargeDirective(True, TODAY)
DISARMED = FullChargeDirective(False)
WEEKLY = "weekly_full_charge"


def fc_world(clock, *, weekly="off", day="monday", delay="off", options=WEEKDAY_OPTIONS, **kw):
    store = world(clock, extra=FC_ENTS.all(), **kw)
    at = clock.now()
    store.apply(entity_state(FC_ENTS.enable_switch, weekly, at))
    attrs = {} if options is None else {"options": list(options)}
    store.apply(entity_state(FC_ENTS.day_select, day, at, attributes=attrs))
    if delay is not None:
        store.apply(entity_state(FC_ENTS.delay_switch, delay, at))
    return store


def fc_calls(ha):
    """The calls on the two full-charge entities, as ``(service, kind, data)``."""
    return [
        (service, entity.split(".", 1)[1], data)
        for domain, service, entity, data in ha.calls
        if entity in (FC_ENTS.enable_switch, FC_ENTS.day_select)
    ]


def fc_arming_writes(ha):
    return [c for c in fc_calls(ha) if c[0] in ("turn_on", "select_option")]


def test_the_two_full_charge_roles_are_write_roles():
    assert {"full_charge_day", "full_charge_enable"} <= set(WRITE_ROLES)


def test_an_armed_directive_needs_a_weekday():
    with pytest.raises(ValueError, match="weekday"):
        FullChargeDirective(True)
    with pytest.raises(ValueError, match="weekday"):
        FullChargeDirective(True, "Tuesday")
    assert FullChargeDirective(False).weekday is None
    assert [FullChargeDirective(True, d).armed for d in WEEKDAY_OPTIONS] == [True] * 7


def test_the_feature_needs_the_service_time_zone():
    clock = FakeClock(NOW)
    with pytest.raises(ValueError, match="time zone"):
        Actuator(
            settings=SETTINGS,
            mode="live",
            store=world(clock),
            clock=clock,
            grid_entity=GRID,
            send=FakeHA(world(clock), clock).send,
            full_charge=FC,
        )


def test_arming_writes_the_day_then_the_switch_before_charge_is_enabled():
    clock = FakeClock(NOW)
    store = fc_world(clock)
    actuator, ha, metrics = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    order = [c[2] for c in ha.calls if c[0] != "input_datetime"]
    assert order == [ENTS.pd_target, FC_ENTS.day_select, FC_ENTS.enable_switch, ENTS.allow_charge]
    assert ha.calls[1][1:] == ("select_option", FC_ENTS.day_select, {"option": TODAY})
    assert ha.calls[2][1:] == ("turn_on", FC_ENTS.enable_switch, None)
    assert actuator.desired.full_charge_armed is True
    assert actuator.desired.to_dict()["full_charge_weekday"] == TODAY
    assert counter(metrics, "actuator_writes", role="full_charge_day", result="ok") == 1
    assert counter(metrics, "actuator_writes", role="full_charge_enable", result="ok") == 1


def test_arming_in_an_idle_slot_writes_only_the_pair_and_is_idempotent():
    clock = FakeClock(NOW)
    store = fc_world(clock)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("idle"), full_charge=ARMED))
    assert [c[:2] for c in fc_calls(ha)] == [
        ("select_option", "omnibattery_weekly_full_charge_day"),
        ("turn_on", "omnibattery_weekly_full_charge_enabled"),
    ]
    # Omnibattery acknowledges: status weekly_full_charge; nothing is rewritten
    clock.advance(30)
    store.apply(entity_state(ENTS.integration_status, WEEKLY, clock.now()))
    for entity, value in ((GRID, 400), (ENTS.battery_ac_power, 0)):
        store.apply(entity_state(entity, value, clock.now()))
    calls = len(ha.calls)
    tick(actuator, ctx(clock, intent("idle"), full_charge=ARMED))
    assert actuator.reasons == () and len(fc_calls(ha)) == 2
    assert [c for c in ha.calls[calls:] if c[0] != "input_datetime"] == []


def test_an_already_matching_day_and_switch_are_not_rewritten():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert fc_calls(ha) == []
    assert store.get(ENTS.allow_charge).state == "on"


def test_only_the_day_is_written_when_the_switch_is_already_on():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day="monday")
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("idle"), full_charge=ARMED))
    assert [c[0] for c in fc_calls(ha)] == ["select_option"]


def test_disarming_is_the_first_write_of_the_restrictive_block():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("idle"), full_charge=DISARMED))
    order = [(c[1], c[2]) for c in ha.calls if c[0] != "input_datetime"]
    assert order[0] == ("turn_off", FC_ENTS.enable_switch)
    assert order[1] == ("turn_off", ENTS.allow_charge)
    assert actuator.desired.full_charge_armed is False
    # idempotent: switch already off, nothing more to write
    clock.advance(1)
    calls = len(ha.calls)
    tick(actuator, ctx(clock, intent("idle"), full_charge=DISARMED))
    assert [c for c in ha.calls[calls:] if c[2] == FC_ENTS.enable_switch] == []


def test_the_switch_is_driven_off_while_the_feature_is_enabled_and_no_run_is_armed():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY)  # e.g. the owner flipped it by hand
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("idle"), full_charge=DISARMED))
    assert fc_calls(ha) == [("turn_off", "omnibattery_weekly_full_charge_enabled", None)]


# ---- the feature off: the two roles are never touched ----------------------------------------


@pytest.mark.parametrize(
    ("settings", "directive"),
    [
        (None, ARMED),
        (None, DISARMED),
        (FullChargeSettings(enabled=False, entities=FC_ENTS), ARMED),
        (FullChargeSettings(enabled=False, entities=FC_ENTS), DISARMED),
    ],
)
@pytest.mark.parametrize("mode", ["live", "dry_run"])
def test_with_the_feature_off_the_weekly_entities_are_never_touched(settings, directive, mode):
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day="friday", charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, mode=mode, full_charge=settings)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=directive))
    clock.advance(1)
    tick(actuator, ctx(clock, intent("idle"), full_charge=directive))
    assert fc_calls(ha) == []
    assert actuator.full_charge_refusal is None
    assert "full_charge" not in actuator.view() and "full_charge" not in actuator.snapshot()
    assert "weekly_enable" not in actuator.readings
    assert "full_charge_armed" not in actuator.desired.to_dict()
    assert asyncio.run(actuator.shutdown()) in (True, False)
    assert fc_calls(ha) == []


@pytest.mark.parametrize("mode", ["live", "dry_run"])
def test_enabled_but_without_a_directive_the_weekly_entities_are_never_touched(mode):
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day="friday", charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, mode=mode, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000)))
    clock.advance(1)
    tick(actuator, ctx(clock, intent("idle")))
    assert fc_calls(ha) == []
    assert asyncio.run(actuator.shutdown()) is True
    assert fc_calls(ha) == []  # not even the shutdown disarm: nothing ever drove the feature


def test_without_the_feature_the_weekly_status_still_holds_safe_idle():
    clock = FakeClock(NOW)
    store = fc_world(clock, status=WEEKLY)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert actuator.reasons == ("integration_unhealthy",)
    assert actuator.desired.mode == "safe_idle"


# ---- guard status -----------------------------------------------------------------------------


def test_the_weekly_status_is_healthy_only_while_a_run_is_armed_for_today():
    clock = FakeClock(NOW)
    store = fc_world(clock, status=WEEKLY, weekly="on", day=TODAY, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert actuator.reasons == () and actuator.desired.mode == "charge"
    assert ha.writes() == []


@pytest.mark.parametrize(
    "directive",
    [None, DISARMED, FullChargeDirective(True, "wednesday")],
    ids=["no_directive", "disarmed", "armed_for_another_day"],
)
def test_the_weekly_status_holds_safe_idle_otherwise(directive):
    clock = FakeClock(NOW)
    store = fc_world(clock, status=WEEKLY, weekly="on", day=TODAY, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=directive))
    assert actuator.reasons == ("integration_unhealthy",)
    assert actuator.desired.mode == "safe_idle"
    assert store.get(ENTS.allow_charge).state == "off"


@pytest.mark.parametrize("status", ["manual", "backup_mode", "grid_charging", "unavailable"])
def test_every_other_status_still_holds_safe_idle_while_armed(status):
    clock = FakeClock(NOW)
    store = fc_world(clock, status=status, weekly="on", day=TODAY)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert actuator.reasons == ("integration_unhealthy",)
    assert actuator.desired.mode == "safe_idle"


def test_a_refused_directive_does_not_make_the_weekly_status_healthy():
    clock = FakeClock(NOW)
    store = fc_world(clock, status=WEEKLY, weekly="on", day=TODAY, delay="on")
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert actuator.reasons == ("integration_unhealthy",)
    assert actuator.full_charge_refusal == "delay_switch_on"
    assert fc_calls(ha) == [("turn_off", "omnibattery_weekly_full_charge_enabled", None)]


def test_a_guard_failure_safe_idle_neither_arms_nor_disarms():
    clock = FakeClock(NOW)
    store = fc_world(clock)  # switch off
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED, price_fresh=False))
    assert actuator.reasons == ("price_stale",) and actuator.desired.mode == "safe_idle"
    assert fc_calls(ha) == []

    clock = FakeClock(NOW)
    store = fc_world(clock, status=WEEKLY, weekly="on", day=TODAY, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED, price_fresh=False))
    assert actuator.reasons == ("price_stale",)
    assert store.get(ENTS.allow_charge).state == "off"  # the safe idle
    assert fc_calls(ha) == []  # mid-run: the switch stays on, nothing is toggled for nothing


def test_a_safe_idle_still_disarms_when_the_directive_says_so():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=DISARMED, price_fresh=False))
    assert actuator.desired.mode == "safe_idle"
    assert [(c[1], c[2]) for c in ha.calls if c[0] != "input_datetime"][:2] == [
        ("turn_off", FC_ENTS.enable_switch),
        ("turn_off", ENTS.allow_charge),
    ]


# ---- refusals --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("world_kw", "directive", "reason"),
    [
        ({"delay": "on"}, ARMED, "delay_switch_on"),
        ({"delay": "unavailable"}, ARMED, "delay_switch_unreadable"),
        ({"delay": "unknown"}, ARMED, "delay_switch_unreadable"),
        ({"delay": None}, ARMED, "delay_switch_unreadable"),
        ({"options": WEEKDAY_OPTIONS[:6]}, ARMED, "day_options_invalid"),
        ({"options": ["Monday", "Tuesday"]}, ARMED, "day_options_invalid"),
        ({"options": None}, ARMED, "day_options_invalid"),
        ({"day": "unavailable"}, ARMED, "entities_missing"),
        ({"weekly": "unavailable"}, ARMED, "entities_missing"),
        ({}, FullChargeDirective(True, "wednesday"), "not_today"),
        ({"kill": "off"}, ARMED, "kill_switch_off"),
    ],
)
def test_arming_is_refused_and_never_written(world_kw, directive, reason):
    clock = FakeClock(NOW)
    store = fc_world(clock, **world_kw)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=directive))
    assert actuator.full_charge_refusal == reason
    assert fc_arming_writes(ha) == []
    assert actuator.view()["full_charge"]["refusal"] == reason
    if reason != "kill_switch_off":  # the battery itself keeps following the plan
        assert actuator.reasons == () and store.get(ENTS.allow_charge).state == "on"
    clock.advance(1)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=DISARMED))
    assert actuator.full_charge_refusal is None


def test_a_refusal_is_logged_once(caplog):
    clock = FakeClock(NOW)
    store = fc_world(clock, delay="on")
    actuator, _, _ = make(clock, store, full_charge=FC)
    with caplog.at_level("INFO", logger="battery_optimizer.service.actuator"):
        for _ in range(3):
            clock.advance(1)
            tick(actuator, ctx(clock, intent("idle"), full_charge=ARMED))
    lines = [r for r in caplog.records if "arming refused" in r.getMessage()]
    assert len(lines) == 1 and "delay_switch_on" in lines[0].getMessage()
    assert lines[0].levelname == "WARNING"


def test_kill_switch_off_writes_nothing_even_when_armed():
    clock = FakeClock(NOW)
    store = fc_world(clock, kill="off")
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert ha.calls == []


def test_no_writes_without_a_synced_home_assistant_when_armed():
    clock = FakeClock(NOW)
    store = fc_world(clock)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED, ha_synced=False))
    assert ha.calls == []


# ---- readback, retry, forbidden --------------------------------------------------------------


def test_a_transient_error_on_the_day_select_is_retried():
    clock = FakeClock(NOW)
    store = fc_world(clock)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    ha.refuse_once.add(FC_ENTS.day_select)
    tick(actuator, ctx(clock, intent("idle"), full_charge=ARMED))
    assert [c[0] for c in fc_calls(ha)] == ["select_option", "select_option", "turn_on"]
    assert store.get(FC_ENTS.day_select).state == TODAY
    assert store.get(FC_ENTS.enable_switch).state == "on"


def test_the_readback_of_the_switch_is_awaited_then_retried_then_safe_idle():
    clock = FakeClock(NOW)
    store = fc_world(clock, charge="on", pd=1400)
    actuator, ha, metrics = make(clock, store, full_charge=FC, silent={FC_ENTS.enable_switch})
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert [c[0] for c in fc_calls(ha)] == ["select_option"] + ["turn_on"] * 3
    assert counter(metrics, "actuator_writes", role="full_charge_enable", result="failed") == 1
    assert counter(metrics, "actuator_write_failures", reason="readback_timeout") == 3
    assert actuator.alert == "write_failed" and actuator.desired == safe_idle("write_failed")
    assert store.get(ENTS.allow_charge).state == "off"  # charge was never enabled


def test_a_forbidden_arming_write_is_never_retried_and_the_pair_is_not_retried_each_tick():
    clock = FakeClock(NOW)
    store = fc_world(clock)
    actuator, ha, metrics = make(clock, store, full_charge=FC, forbid={FC_ENTS.day_select})
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert [c[0] for c in fc_calls(ha)] == ["select_option"]  # once, no retry, no switch
    assert counter(metrics, "actuator_write_failures", reason="forbidden") == 1
    assert actuator.desired == safe_idle("write_failed") and actuator.alert == "write_failed"
    # the next tick does not retry the same directive (no safe-idle loop all day) ...
    clock.advance(30)
    for entity, value in ((GRID, 400), (ENTS.battery_ac_power, 0)):
        store.apply(entity_state(entity, value, clock.now()))
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert [c[0] for c in fc_calls(ha)] == ["select_option"]
    assert actuator.full_charge_refusal == "write_failed"
    assert actuator.desired.mode == "charge"  # ... and the battery follows the plan again
    # ... but a new directive (the scheduler cancelled, then re-armed) gets a fresh try
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=DISARMED))
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert [c[0] for c in fc_calls(ha)] == ["select_option", "select_option"]


# ---- dry_run ----------------------------------------------------------------------------------


def test_dry_run_simulates_the_two_entities_and_sends_nothing():
    clock = FakeClock(NOW)
    store = fc_world(clock)
    records: list = []
    actuator, ha, metrics = make(clock, store, mode="dry_run", records=records, full_charge=FC)
    record = tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert ha.calls == []
    assert [(w["role"], w["result"]) for w in record["writes"]] == [
        ("pd_target", "dry_run"),
        ("full_charge_day", "dry_run"),
        ("full_charge_enable", "dry_run"),
        ("allow_charge", "dry_run"),
    ]
    assert record["writes"][1]["option"] == TODAY
    assert record["writes"][1]["service"] == "select.select_option"
    assert record["desired"]["full_charge_armed"] is True
    assert counter(metrics, "actuator_writes", role="full_charge_day", result="dry_run") == 1
    # planned against the simulated state: not repeated, and the actuator's view follows it
    clock.advance(1)
    record = tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert record["writes"] == []
    view = actuator.view()["full_charge"]
    assert (view["enable_switch"], view["day"], view["delay_switch"]) == (True, TODAY, False)
    # disarm is simulated too
    clock.advance(1)
    record = tick(actuator, ctx(clock, intent("charge", 1000), full_charge=DISARMED))
    assert [(w["role"], w["service"]) for w in record["writes"]] == [
        ("full_charge_enable", "switch.turn_off")
    ]
    clock.advance(1)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=DISARMED))
    assert actuator.view()["full_charge"]["enable_switch"] is False
    assert ha.calls == []


# ---- kill switch edge and shutdown -----------------------------------------------------------


def test_kill_switch_off_edge_disarms_once_after_the_safe_idle():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, status=WEEKLY, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert fc_calls(ha) == []
    calls = len(ha.calls)
    clock.advance(1)
    store.apply(entity_state(ENTS.kill_switch, "off", clock.now()))
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert [(c[1], c[2]) for c in ha.calls[calls:]] == [
        ("turn_off", ENTS.allow_charge),
        ("turn_off", FC_ENTS.enable_switch),
    ]
    assert not actuator.in_control
    assert actuator.full_charge_refusal == "kill_switch_off"
    clock.advance(30)  # hands off from here on
    store.apply(entity_state(FC_ENTS.enable_switch, "on", clock.now()))
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert len(ha.calls) == calls + 2


def test_kill_switch_off_mid_arming_aborts_before_the_switch_and_disarms_nothing_extra():
    clock = FakeClock(NOW)
    store = fc_world(clock)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    ha.after[FC_ENTS.day_select] = lambda: store.apply(
        entity_state(ENTS.kill_switch, "off", clock.now())
    )
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert [c[0] for c in fc_calls(ha)] == ["select_option"]  # the switch was never turned on
    assert store.get(ENTS.allow_charge).state == "off" and not actuator.in_control


def test_kill_switch_off_after_the_switch_turned_on_turns_it_back_off():
    clock = FakeClock(NOW)
    store = fc_world(clock)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    ha.after[FC_ENTS.enable_switch] = lambda: store.apply(
        entity_state(ENTS.kill_switch, "off", clock.now())
    )
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert [c[0] for c in fc_calls(ha)] == ["select_option", "turn_on", "turn_off"]
    assert store.get(FC_ENTS.enable_switch).state == "off"
    assert store.get(ENTS.allow_charge).state == "off" and not actuator.in_control


def test_shutdown_disarms_after_the_safe_idle_when_in_control():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, status=WEEKLY, charge="on", pd=1400)
    records: list = []
    actuator, ha, _ = make(clock, store, records=records, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=ARMED))
    assert actuator.in_control
    assert asyncio.run(actuator.shutdown()) is True
    assert [(c[1], c[2]) for c in ha.calls if c[0] != "input_datetime"] == [
        ("turn_off", ENTS.allow_charge),
        ("turn_off", FC_ENTS.enable_switch),
    ]
    assert [w["role"] for w in records[-1]["writes"]] == ["allow_charge", "full_charge_enable"]


def test_shutdown_is_best_effort_about_the_switch():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, status=WEEKLY)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("idle"), full_charge=ARMED))
    ha.refuse.add(FC_ENTS.enable_switch)
    assert asyncio.run(actuator.shutdown()) is True  # the safe idle itself succeeded
    assert [c[0] for c in fc_calls(ha)] == ["turn_off"] * 3


def test_shutdown_without_control_or_without_the_feature_writes_nothing():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, kill="off")
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("idle"), full_charge=ARMED))
    assert asyncio.run(actuator.shutdown()) is True
    assert ha.calls == []
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY)
    actuator, ha, _ = make(clock, store)  # feature off
    tick(actuator, ctx(clock, intent("idle")))
    assert asyncio.run(actuator.shutdown()) is True
    assert fc_calls(ha) == []


# ---- views for the scheduler ------------------------------------------------------------------


def test_view_and_snapshot_expose_the_weekly_readings():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, delay="off", status=WEEKLY)
    actuator, _, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("idle"), full_charge=ARMED))
    expected = {
        "enable_switch": True,
        "day": TODAY,
        "delay_switch": False,
        "integration_status": WEEKLY,
        "driven_armed": True,
        "settle_hold": False,
        "refusal": None,
    }
    assert actuator.view()["full_charge"] == expected
    snapshot = actuator.snapshot()["full_charge"]
    assert snapshot == {
        "enable_switch": 1.0,
        "day": TODAY,
        "delay_switch": 0.0,
        "integration_status": WEEKLY,
        "refusal": None,
    }
    assert actuator.readings["weekly_enable"] is True and actuator.readings["weekly_day"] == TODAY
    json.dumps(actuator.view())  # /status must stay serialisable


def test_the_directive_follows_the_local_weekday_across_midnight():
    # 23:30 local Tuesday == 21:30Z; 00:10 local Wednesday == 22:10Z (CEST, UTC+2)
    late = datetime(2026, 10, 20, 21, 30, tzinfo=UTC)
    clock = FakeClock(late)
    store = fc_world(clock, status=WEEKLY, weekly="on", day="tuesday")
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("idle", valid_from=floor_slot(late, 900)), full_charge=ARMED))
    assert actuator.full_charge_refusal is None and actuator.reasons == ()
    clock.advance(40 * 60)
    for entity, value in ((GRID, 400), (ENTS.battery_ac_power, 0)):
        store.apply(entity_state(entity, value, clock.now()))
    store.apply(entity_state(ENTS.integration_status, WEEKLY, clock.now()))
    next_intent = intent("idle", valid_from=floor_slot(clock.now(), 900))
    tick(actuator, ctx(clock, next_intent, full_charge=ARMED))  # a stale Tuesday directive
    assert actuator.full_charge_refusal == "not_today"
    assert actuator.reasons == ("integration_unhealthy",)
    assert fc_calls(ha) == [("turn_off", "omnibattery_weekly_full_charge_enabled", None)]


# ---- the settle hold (amendment A2) -----------------------------------------------------------


def hold(clock, seconds=300):
    return FullChargeDirective(True, TODAY, clock.now() + timedelta(seconds=seconds))


def test_a_settle_hold_needs_an_armed_directive_and_an_aware_deadline():
    with pytest.raises(ValueError, match="armed"):
        FullChargeDirective(False, None, NOW + timedelta(seconds=300))
    with pytest.raises(ValueError, match="timezone-aware"):
        FullChargeDirective(True, TODAY, datetime(2026, 11, 4, 12, 0))
    assert FullChargeDirective(True, TODAY).settle_until is None


@pytest.mark.parametrize("planned", ["charge", "discharge"])
def test_the_settle_hold_idles_whatever_the_plan_says_and_keeps_the_switch_on(planned):
    clock = FakeClock(NOW)
    store = fc_world(
        clock, weekly="on", day=TODAY, status="balanced", charge="on", discharge="off", pd=1400
    )
    if planned == "discharge":
        store.apply(entity_state(ENTS.allow_charge, "off", clock.now()))
        store.apply(entity_state(ENTS.allow_discharge, "on", clock.now()))
    actuator, ha, _ = make(clock, store, full_charge=FC)
    record = tick(actuator, ctx(clock, intent(planned, 1500), full_charge=hold(clock)))
    assert record["desired"]["mode"] == "idle" and record["desired"]["reason"] == "settle_hold"
    assert store.get(ENTS.allow_charge).state == "off"
    assert store.get(ENTS.allow_discharge).state == "off"
    assert float(store.get(ENTS.pd_target).state) == 0.0  # the neutral target
    assert store.get(FC_ENTS.enable_switch).state == "on"  # the weekly switch is not turned off yet
    assert [c for c in fc_calls(ha) if c[0] == "turn_off"] == []
    assert actuator.view()["full_charge"]["settle_hold"] is True
    assert record["guard_reasons"] == [] and len(ha.heartbeats()) == 1


def test_the_hold_ends_by_the_directive_disarming_after_it_in_the_same_reconcile():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, status="balanced", pd=0)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=hold(clock)))
    assert actuator.desired.reason == "settle_hold"
    clock.advance(20)  # the reading arrived: the scheduler drops the hold and disarms
    calls = len(ha.calls)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=DISARMED))
    writes = [(c[1], c[2]) for c in ha.calls[calls:] if c[0] != "input_datetime"]
    assert writes[0] == ("turn_off", FC_ENTS.enable_switch)  # disarm moves behind the hold
    assert ("turn_on", ENTS.allow_charge) in writes  # and the normal intent resumes
    assert (
        actuator.desired.mode == "charge" and actuator.view()["full_charge"]["settle_hold"] is False
    )


def test_the_hold_lapses_at_its_deadline_even_without_a_new_directive():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, status="balanced", pd=0)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    directive = hold(clock, 60)
    tick(actuator, ctx(clock, intent("charge", 1000), full_charge=directive))
    assert actuator.desired.reason == "settle_hold"
    clock.advance(61)
    for entity, value in ((GRID, 400), (ENTS.battery_ac_power, 0)):
        store.apply(entity_state(entity, value, clock.now()))
    tick(actuator, ctx(clock, intent("charge", 1000, valid_from=SLOT), full_charge=directive))
    assert (
        actuator.desired.mode == "charge"
    )  # the scheduler is expected to disarm; idle is not forever


def test_a_guard_failure_during_the_hold_still_wins():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, status="balanced", charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, full_charge=FC)
    clock.advance(400)  # grid and SOC readings are stale now
    record = tick(actuator, ctx(clock, intent("charge", 1000), full_charge=hold(clock)))
    assert record["desired"]["mode"] == "safe_idle"
    assert "grid_power_stale" in record["guard_reasons"]
    assert store.get(ENTS.allow_charge).state == "off"
    assert store.get(FC_ENTS.enable_switch).state == "on"  # a safe idle neither arms nor disarms


def test_the_hold_is_the_same_in_dry_run_and_sends_nothing():
    clock = FakeClock(NOW)
    store = fc_world(clock, weekly="on", day=TODAY, status="balanced", charge="on", pd=1400)
    actuator, ha, _ = make(clock, store, mode="dry_run", full_charge=FC)
    record = tick(actuator, ctx(clock, intent("charge", 1500), full_charge=hold(clock)))
    assert record["desired"]["reason"] == "settle_hold" and ha.calls == []
    assert [w["role"] for w in record["writes"]] == ["allow_charge", "pd_target"]
    assert all(w["result"] == "dry_run" for w in record["writes"])


def test_a_settle_deadline_is_ignored_while_the_feature_is_off():
    clock = FakeClock(NOW)
    store = fc_world(clock, status="balanced")
    actuator, ha, _ = make(clock, store)  # no full_charge settings
    record = tick(actuator, ctx(clock, intent("charge", 1000), full_charge=hold(clock)))
    assert record["desired"]["mode"] == "charge"
    assert fc_calls(ha) == []


# ---- configuration ----------------------------------------------------------------------------

FC_ON = {**LIVE, "full_charge.enabled": True}  # full_charge.mode stays shadow


def test_the_committed_full_charge_section_is_off_and_names_the_installation_entities():
    fc = load_service_config(REPO / "config" / "service.yaml").full_charge
    assert fc == FullChargeSettings(enabled=False, entities=FC_ENTS)
    assert fc.mode == "shadow"
    assert fc.cadence == FullChargeCadence()  # the amendment's table: 10/14, 14/28, 7/14, 5/7
    assert (fc.min_comparable_readings, fc.alert_consecutive, fc.settle_timeout_s) == (4, 2, 300.0)
    assert (fc.threshold_min_eur, fc.threshold_scale_eur, fc.fixed_cost_eur) == (0.01, 0.01, 0.0)
    assert (fc.taper.start_soc_frac, fc.taper.power_kw) == (0.90, 0.2)
    assert fc.bootstrap_last_full is None and fc.reached_soc_pct == 99


def test_a_missing_full_charge_section_means_the_feature_is_off(tmp_path):
    import yaml

    raw = yaml.safe_load((REPO / "config" / "service.yaml").read_text())
    del raw["full_charge"]
    path = tmp_path / "service.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert load_service_config(path).full_charge == FullChargeSettings()


def test_the_full_charge_section_does_not_change_the_plan_identity(tmp_path):
    base = load_service_config(write_service_config(tmp_path))
    other = load_service_config(write_service_config(tmp_path, **{"full_charge.enabled": True}))
    assert other.full_charge.enabled is True
    assert base.config_hash() == other.config_hash()
    assert base.public_dict()["full_charge"]["enabled"] is False
    json.dumps(other.public_dict())  # /status serialises it


@pytest.mark.parametrize("mode", ["shadow", "dry_run", "live"])
def test_the_feature_can_be_enabled_in_every_mode(tmp_path, mode):
    config = load_service_config(write_service_config(tmp_path, **{**FC_ON, "mode": mode}))
    assert config.mode == mode and config.full_charge.enabled
    assert config.full_charge.mode == "shadow"
    assert config.full_charge_writer_mode == "simulated"
    assert config.full_charge_isolated is (mode == "live")  # live control, feature simulated


def test_the_feature_can_go_live_only_in_a_live_service(tmp_path):
    config = load_service_config(
        write_service_config(tmp_path, **{**FC_ON, "full_charge.mode": "live"})
    )
    assert config.full_charge.mode == "live" and config.full_charge_writer_mode == "live"
    assert config.full_charge_isolated is False
    assert config.public_dict()["full_charge"]["mode"] == "live"
    # the mode is part of /status but not of the plan identity
    base = load_service_config(write_service_config(tmp_path, **FC_ON))
    assert base.config_hash() == config.config_hash()


def test_a_bootstrap_date_and_a_disabled_taper_are_accepted(tmp_path):
    config = load_service_config(
        write_service_config(
            tmp_path,
            **{
                **FC_ON,
                "full_charge.bootstrap_last_full": "2026-11-01",
                "full_charge.taper.start_soc_frac": None,
            },
        )
    )
    assert config.full_charge.bootstrap_last_full.isoformat() == "2026-11-01"
    assert config.full_charge.taper.start_soc_frac is None
    assert config.public_dict()["full_charge"]["bootstrap_last_full"] == "2026-11-01"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"full_charge.bogus": 1}, "unknown full_charge keys"),
        ({"full_charge.entities.bogus": "switch.x"}, "unknown full_charge.entities keys"),
        ({"full_charge.taper.bogus": 1}, "unknown full_charge.taper keys"),
        ({"full_charge.enabled": "yes"}, "full_charge.enabled must be true or false"),
        ({"full_charge.min_interval_days": 5}, "unknown full_charge keys"),
        ({"full_charge.mode": "dry_run"}, "full_charge.mode must be one of"),
        ({"full_charge.mode": True}, "full_charge.mode must be one of"),
        ({"full_charge.mode": "live"}, "full_charge.mode=live needs the service mode=live"),
        (
            {"mode": "dry_run", "full_charge.mode": "live"},
            "full_charge.mode=live needs the service mode=live",
        ),
        ({"full_charge.cadence.bogus": {}}, "unknown full_charge.cadence keys"),
        ({"full_charge.cadence.green": 14}, "cadence.green must be a mapping"),
        ({"full_charge.cadence.green.bogus": 1}, "unknown full_charge.cadence.green keys"),
        ({"full_charge.cadence.green.min_days": 0}, "cadence.green.min_days"),
        ({"full_charge.cadence.green.min_days": 28}, "cadence.green needs 0 < min_days < max_days"),
        ({"full_charge.cadence.green.max_days": 31}, "cadence.green needs .* <= 30"),
        ({"full_charge.cadence.yellow.min_days": 2.5}, "cadence.yellow.min_days"),
        ({"full_charge.cadence.green.min_days": 9}, "green.min_days >= unknown.min_days"),
        ({"full_charge.cadence.yellow.min_days": 4}, "yellow.min_days >= alert.min_days"),
        ({"full_charge.min_comparable_readings": 0}, "min_comparable_readings"),
        ({"full_charge.alert_consecutive": 1}, "alert_consecutive"),
        ({"full_charge.settle_timeout_s": 0}, "settle_timeout_s"),
        ({"full_charge.settle_timeout_s": 901}, "settle_timeout_s must be <= 900"),
        (
            {"battery_health.entities.balance_status": "sensor.some_other_status"},
            "balance_status and battery_health.entities.balance_status must name the same",
        ),
        ({"full_charge.threshold_min_eur": -0.01}, "threshold_min_eur"),
        ({"full_charge.threshold_min_eur": float("nan")}, "threshold_min_eur"),
        ({"full_charge.threshold_scale_eur": float("inf")}, "threshold_scale_eur"),
        ({"full_charge.fixed_cost_eur": -1}, "fixed_cost_eur"),
        ({"full_charge.fixed_cost_eur": True}, "fixed_cost_eur"),
        ({"full_charge.shortfall_penalty_eur_per_kwh": 0}, "shortfall_penalty_eur_per_kwh"),
        ({"full_charge.shortfall_penalty_eur_per_kwh": -1}, "shortfall_penalty_eur_per_kwh"),
        ({"full_charge.target_soc_frac": 1.1}, "arm_before_soc_frac < target_soc_frac <= 1"),
        ({"full_charge.target_soc_frac": 0.9}, "arm_before_soc_frac < target_soc_frac <= 1"),
        ({"full_charge.arm_before_soc_frac": 0}, "arm_before_soc_frac"),
        ({"full_charge.arm_before_soc_frac": 1.0}, "arm_before_soc_frac < target_soc_frac"),
        ({"full_charge.reached_soc_pct": 101}, "reached_soc_pct"),
        ({"full_charge.reached_soc_pct": 0}, "reached_soc_pct"),
        ({"full_charge.completion_margin_slots": 0}, "completion_margin_slots"),
        ({"full_charge.arm_lead_slots": -1}, "arm_lead_slots"),
        ({"full_charge.ack_timeout_s": 0}, "ack_timeout_s"),
        ({"full_charge.top_hold_timeout_min": 0}, "top_hold_timeout_min"),
        ({"full_charge.max_consecutive_failures": 0}, "max_consecutive_failures"),
        ({"full_charge.failure_pause_days": 0}, "failure_pause_days"),
        ({"full_charge.taper.start_soc_frac": 0.5}, "start_soc_frac must be in"),
        ({"full_charge.taper.start_soc_frac": 1.0}, "start_soc_frac must be in"),
        ({"full_charge.taper.power_kw": 0}, "taper.power_kw"),
        ({"full_charge.taper.power_kw": 2.6}, "taper.power_kw must not exceed"),
        ({"full_charge.bootstrap_last_full": "yesterday"}, "bootstrap_last_full"),
        ({"full_charge.bootstrap_last_full": 5}, "bootstrap_last_full"),
        ({"full_charge.entities.enable_switch": "number.x"}, "enable_switch must be a switch"),
        ({"full_charge.entities.day_select": "switch.x"}, "day_select must be a select"),
        ({"full_charge.entities.delay_switch": "sensor.x"}, "delay_switch must be a switch"),
        ({"full_charge.entities.balancing_mode": "sensor.x"}, "balancing_mode must be a binary_"),
        ({"full_charge.entities.cell_delta": "x"}, "cell_delta"),
        (
            {"full_charge.entities.delay_switch": FC_ENTS.enable_switch},
            "must name distinct entities",
        ),
        (
            {"full_charge.entities.enable_switch": ENTS.allow_charge},
            "must name distinct entities",
        ),
        (
            {"full_charge.entities.delay_switch": ENTS.power_limits},
            "must name distinct entities",
        ),
        ({"actuation.healthy_states": ["balanced", "weekly_full_charge"]}, "weekly_full_charge"),
    ],
)  # fmt: skip
def test_invalid_full_charge_config_is_refused(tmp_path, override, message):
    with pytest.raises(ValueError, match=message):
        load_service_config(write_service_config(tmp_path, **override))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"full_charge.entities.enable_switch": None}, "needs full_charge.entities.enable_switch"),
        ({"full_charge.entities.day_select": None}, "needs full_charge.entities.day_select"),
        ({"full_charge.entities.delay_switch": None}, "needs full_charge.entities.delay_switch"),
        ({"full_charge.entities.cell_delta": None}, "needs full_charge.entities.cell_delta"),
        ({"full_charge.entities.balance_status": None}, "needs full_charge.entities.balance_st"),
        ({"full_charge.entities.delta_trend": None}, "needs full_charge.entities.delta_trend"),
        ({"full_charge.entities.delta_avg_4w": None}, "needs full_charge.entities.delta_avg_4w"),
        ({"planning.soc_source": "simulated"}, "needs planning.soc_source=entity"),
        ({"actuation.entities.integration_status": None}, "integration_status"),
        ({"actuation.entities.battery_soc": None}, "battery_soc"),
    ],
)  # fmt: skip
def test_enabling_the_feature_requires_its_inputs(tmp_path, override, message):
    on = {"full_charge.enabled": True, **override}
    with pytest.raises(ValueError, match=message):
        load_service_config(write_service_config(tmp_path, **on))
    # the same gaps are fine while the feature is off
    off = {k: v for k, v in override.items() if k.startswith("full_charge.")}
    if off:
        load_service_config(write_service_config(tmp_path, **off))


# ---------------------------------------------------------------------------
# Load-following discharge (ADR-0007 amendment 2026-11-07)
# ---------------------------------------------------------------------------

LF = replace(SETTINGS, load_following=LoadFollowingSettings(enabled=True, max_w=2500.0))
CAP = "omnibattery_system_max_discharge_power"


def lf_intent(mode="discharge", power=1000.0, soc_target=0.5, **kw) -> ShadowIntent:
    return replace(intent(mode, power, **kw), soc_target_frac=soc_target)


def caps(ha):
    return [v for _, e, v in ha.writes() if e == CAP]


def set_soc(store, clock, soc, seconds=30):
    clock.advance(seconds)
    store.apply(entity_state(GRID, 400, clock.now()))
    store.apply(entity_state(ENTS.battery_soc, soc, clock.now()))


def lf_snapshot(actuator):
    from battery_optimizer.service.metrics import _actuator_families

    return {
        f.name: [s.value for s in f.samples]
        for f in _actuator_families(actuator.snapshot())
        if "load_following" in f.name
    }


def test_load_following_raises_the_cap_to_the_ceiling_above_the_plan_soc():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=60)
    actuator, ha, _ = make(clock, store, settings=LF)
    record = tick(actuator, ctx(clock, lf_intent(soc_target=0.5)))
    # same ordering as the planned cap: before the limits, the margin and the direction
    assert ha.writes() == [
        ("number.set_value", CAP, 2500.0),
        ("switch.turn_on", "omnibattery_system_power_limits", None),
        ("number.set_value", "omnibattery_pd_target_grid_power", 50.0),
        ("switch.turn_on", "venus_battery_allow_discharge", None),
    ]
    desired = record["desired"]
    assert desired["load_following"] is True and desired["max_discharge_w"] == 2500.0
    assert desired["planned_max_discharge_w"] == 1000.0
    assert desired["load_following_floor_pct"] == 50.0
    assert desired["pd_target_w"] == 50.0  # the margin is unchanged: never export
    assert lf_snapshot(actuator) == {
        "battery_optimizer_actuator_load_following_engaged": [1.0],
        "battery_optimizer_actuator_load_following_raise_watts": [1500.0],
    }


def test_load_following_keeps_the_planned_cap_below_the_plan_soc():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=49)
    actuator, ha, _ = make(clock, store, settings=LF)
    record = tick(actuator, ctx(clock, lf_intent(soc_target=0.5)))
    assert caps(ha) == [1000.0]
    assert record["desired"]["load_following"] is False
    assert record["desired"]["planned_max_discharge_w"] == 1000.0
    assert lf_snapshot(actuator) == {
        "battery_optimizer_actuator_load_following_engaged": [0.0],
        "battery_optimizer_actuator_load_following_raise_watts": [0.0],
    }


@pytest.mark.parametrize("mode", ["charge", "idle"])
def test_load_following_leaves_charge_and_idle_slots_unchanged(mode):
    runs = []
    for settings in (SETTINGS, LF):
        clock = FakeClock(NOW)
        store = world(clock, grid=400, ac=0, soc=90, discharge="on", limits="on", cap=800)
        actuator, ha, _ = make(clock, store, settings=settings)
        record = tick(actuator, ctx(clock, lf_intent(mode, soc_target=0.2)))
        runs.append((ha.writes(), record["desired"]))
    assert runs[0] == runs[1]
    assert "load_following" not in runs[1][1]
    assert CAP not in [e for _, e, _ in runs[1][0]]


@pytest.mark.parametrize(
    "off",
    [SETTINGS, replace(SETTINGS, load_following=LoadFollowingSettings(max_w=2500.0))],
)
def test_load_following_disabled_is_todays_behaviour(off):
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=90)
    actuator, ha, _ = make(clock, store, settings=off)
    record = tick(actuator, ctx(clock, lf_intent(soc_target=0.2)))
    assert caps(ha) == [1000.0]
    assert "load_following" not in record["desired"]
    assert "planned_max_discharge_w" not in record["desired"]
    assert "load_following" not in actuator.snapshot()
    assert lf_snapshot(actuator) == {}


@pytest.mark.parametrize(
    ("max_w", "max_cap", "entity_max", "power", "cap"),
    [
        (2500.0, 2500.0, 2000, 1000.0, 2000.0),  # the entity's own max
        (None, 1800.0, 2500, 1000.0, 1800.0),  # max_w unset: actuation.max_discharge_cap_w
        (3000.0, 2200.0, 2500, 1000.0, 2200.0),  # never above max_discharge_cap_w
        (1500.0, 2500.0, 2500, 2400.0, 2400.0),  # never below the planned cap
        (1500.0, 2500.0, 2500, 30.0, 1500.0),  # a tiny plan still has the 100 W floor beneath
    ],
)
def test_load_following_ceiling_is_clamped_to_every_bound(max_w, max_cap, entity_max, power, cap):
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=80)
    clock.advance(1)
    store.apply(
        entity_state(
            ENTS.max_discharge_power,
            0,
            clock.now(),
            attributes={"min": 0, "max": entity_max, "step": 1},
        )
    )
    settings = replace(
        SETTINGS,
        max_discharge_cap_w=max_cap,
        load_following=LoadFollowingSettings(enabled=True, max_w=max_w),
    )
    actuator, ha, _ = make(clock, store, settings=settings)
    record = tick(actuator, ctx(clock, lf_intent(power=power, soc_target=0.5)))
    assert caps(ha) == [cap]
    assert record["desired"]["load_following"] is True


@pytest.mark.parametrize(("soc", "engaged"), [(10.0, False), (10.9, False), (11.0, True)])
def test_load_following_never_goes_below_the_plans_minimum_soc(soc, engaged):
    # The plan's end-of-slot SOC is the floor; at the planner's minimum SOC (10%) the measured
    # SOC must exceed it by the hysteresis before the cap is raised.
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=soc)
    actuator, ha, _ = make(clock, store, settings=LF)
    tick(actuator, ctx(clock, lf_intent(power=400.0, soc_target=0.10)))
    assert caps(ha) == [2500.0 if engaged else 400.0]


def test_load_following_hysteresis_engages_above_and_releases_below_the_plan_soc(caplog):
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=50.5)
    actuator, ha, _ = make(clock, store, settings=LF)
    the_intent = lf_intent(soc_target=0.5)
    states = []
    with caplog.at_level("INFO", logger="battery_optimizer.service.actuator"):
        for soc in (50.5, 51.0, 50.0, 50.0, 49.9, 50.5, 50.9, 51.0):
            set_soc(store, clock, soc, seconds=10)
            record = tick(actuator, ctx(clock, the_intent))
            states.append(record["desired"]["load_following"])
    # needs 51 to engage, holds at 50 (the plan SOC), releases below it, needs 51 again
    assert states == [False, True, True, True, False, False, False, True]
    assert caps(ha) == [1000.0, 2500.0, 1000.0, 2500.0]  # one write per edge, no flapping
    lines = [r.getMessage() for r in caplog.records if "load_following=" in r.getMessage()]
    assert [line.split()[1] for line in lines] == [
        "load_following=on",
        "load_following=off",
        "load_following=on",
    ]


def test_load_following_latch_carries_over_discharge_slots_and_resets_otherwise():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=52)
    actuator, ha, _ = make(clock, store, settings=LF)
    tick(actuator, ctx(clock, lf_intent(soc_target=0.5)))
    assert actuator.desired.load_following is True
    # next discharge slot: its own plan SOC (48%), judged with the release threshold
    nxt = SLOT + timedelta(minutes=15)
    clock.advance((nxt - clock.now()).total_seconds())
    set_soc(store, clock, 48.5, seconds=0)
    tick(actuator, ctx(clock, lf_intent(soc_target=0.48, valid_from=nxt)))
    assert actuator.desired.load_following is True
    # an idle slot resets the latch; the next discharge slot needs the hysteresis again
    idle = nxt + timedelta(minutes=15)
    clock.advance((idle - clock.now()).total_seconds())
    set_soc(store, clock, 48.5, seconds=0)
    tick(actuator, ctx(clock, lf_intent("idle", valid_from=idle)))
    assert actuator.desired.load_following is None
    after = idle + timedelta(minutes=15)
    clock.advance((after - clock.now()).total_seconds())
    set_soc(store, clock, 48.5, seconds=0)
    tick(actuator, ctx(clock, lf_intent(soc_target=0.48, valid_from=after)))
    assert actuator.desired.load_following is False
    assert caps(ha)[-1] == 1000.0


def test_load_following_latch_resets_on_a_guard_failure():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=51)
    actuator, _, _ = make(clock, store, settings=LF)
    tick(actuator, ctx(clock, lf_intent(soc_target=0.5)))
    assert actuator.desired.load_following is True
    set_soc(store, clock, 50)
    tick(actuator, ctx(clock, lf_intent(soc_target=0.5), price_fresh=False))
    assert actuator.desired.mode == "safe_idle"
    set_soc(store, clock, 50)
    tick(actuator, ctx(clock, lf_intent(soc_target=0.5)))
    assert actuator.desired.load_following is False  # 50 < 50 + 1: not re-engaged


def test_load_following_in_dry_run_records_the_raised_cap_and_sends_nothing():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=60)
    records: list = []
    actuator, ha, metrics = make(clock, store, mode="dry_run", settings=LF, records=records)
    record = tick(actuator, ctx(clock, lf_intent(soc_target=0.5)))
    assert ha.calls == []
    cap = [w for w in record["writes"] if w["role"] == "max_discharge_power"]
    assert cap == [
        {
            "role": "max_discharge_power",
            "entity_id": ENTS.max_discharge_power,
            "service": "number.set_value",
            "value": 2500.0,
            "result": "dry_run",
        }
    ]
    assert record["desired"]["load_following"] is True and record["sent"] is False
    assert records == [record]
    assert counter(metrics, "actuator_writes", role="max_discharge_power", result="dry_run") == 1
    # released: the would-be write back to the planned cap is recorded too, still nothing sent
    set_soc(store, clock, 49)
    released = tick(actuator, ctx(clock, lf_intent(soc_target=0.5)))
    assert [(w["role"], w["value"], w["result"]) for w in released["writes"]] == [
        ("max_discharge_power", 1000.0, "dry_run")
    ]
    assert ha.calls == [] and store.get(ENTS.max_discharge_power).numeric() == 0


def test_load_following_config_defaults_off_and_does_not_change_the_plan_identity(tmp_path):
    committed = load_service_config(REPO / "config" / "service.yaml")
    assert committed.actuation.load_following == LoadFollowingSettings()
    assert committed.actuation.load_following.enabled is False
    on = load_service_config(
        write_service_config(
            tmp_path,
            **{
                "actuation.load_following": {
                    "enabled": True,
                    "max_w": 2500,
                    "soc_hysteresis_pct": 2,
                }
            },
        )
    )
    assert on.actuation.load_following == LoadFollowingSettings(True, 2500.0, 2.0)
    assert on.config_hash() == committed.config_hash()


def test_load_following_ceiling_stays_inside_the_guard_allowlist(tmp_path):
    from battery_optimizer.service.ha_guard import actuation_allowlist, actuation_request

    config = load_service_config(
        write_service_config(tmp_path, **{**LIVE, "actuation.load_following.enabled": True})
    )
    allowlist = actuation_allowlist(config)
    assert allowlist is not None
    ceiling = config.actuation.load_following_ceiling_w
    assert ceiling == 2500.0
    assert allowlist.numbers[ENTS.max_discharge_power] == (100.0, 2500.0)  # unchanged
    frame = actuation_request(
        1, "number", "set_value", ENTS.max_discharge_power, {"value": ceiling}, allowlist
    )
    assert frame["service_data"] == {"value": 2500.0}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"actuation.load_following.max_w": 3000}, "within \\[min_discharge_cap_w"),
        ({"actuation.load_following.max_w": 50}, "within \\[min_discharge_cap_w"),
        ({"actuation.load_following.max_w": 0}, "max_w must be > 0"),
        ({"actuation.load_following.soc_hysteresis_pct": 0}, "soc_hysteresis_pct must be > 0"),
        ({"actuation.load_following.soc_hysteresis_pct": 11}, "soc_hysteresis_pct must be <="),
        ({"actuation.load_following.enabled": "yes"}, "enabled must be true or false"),
        ({"actuation.load_following.bogus": 1}, "unknown actuation.load_following keys"),
    ],
)
def test_invalid_load_following_config_is_refused(tmp_path, override, message):
    with pytest.raises(ValueError, match=message):
        load_service_config(write_service_config(tmp_path, **override))


# ---------------------------------------------------------------------------
# Load-following with the storage-value floor (ADR-0007 amendment 2026-11-11): the raised cap is
# held by what one kWh kept is worth to the plan, not by the plan's end-of-slot SOC.
# ---------------------------------------------------------------------------

SV = replace(
    SETTINGS,
    load_following=LoadFollowingSettings(enabled=True, max_w=2500.0, floor="storage_value"),
)
RESERVE = 20.0  # the tariff's SOC floor (%) in these tests
WEAR = 0.01


def sv_intent(value=0.10, price=0.20, soc_target=0.5, power=1000.0, **kw) -> ShadowIntent:
    return replace(
        lf_intent(power=power, soc_target=soc_target, **kw),
        storage_value_eur_per_kwh=value,
        expected_buy_price_eur_per_kwh=price,
    )


def sv_ctx(clock, the_intent, **kw) -> ActuationContext:
    values = {"soc_floor_pct": RESERVE, "throughput_cost_eur_per_kwh": WEAR}
    values.update(kw)
    return ctx(clock, the_intent, **values)


def sv_tick(settings, soc, the_intent, **kw):
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=soc)
    actuator, ha, metrics = make(clock, store, settings=settings)
    record = tick(actuator, sv_ctx(clock, the_intent, **kw))
    return record, actuator, ha, metrics


def test_the_storage_value_floor_raises_the_cap_below_the_plan_soc_while_the_spread_pays():
    # 45% is under the plan's 50% end-of-slot SOC: the plan-SOC rule would hold the planned cap
    record, actuator, ha, _ = sv_tick(SV, 45, sv_intent(value=0.10, price=0.20))
    assert caps(ha) == [2500.0]
    desired = record["desired"]
    assert desired["load_following"] is True and desired["max_discharge_w"] == 2500.0
    assert desired["planned_max_discharge_w"] == 1000.0
    assert desired["load_following_rule"] == "storage_value"
    assert desired["load_following_storage_value_eur_per_kwh"] == 0.10
    assert desired["load_following_spread_eur_per_kwh"] == pytest.approx(0.09)  # 0.20 - 0.01 - 0.10
    assert desired["load_following_floor_pct"] == 21.0  # the tariff floor + its release band
    assert desired["pd_target_w"] == 50.0  # the margin is untouched: never export
    # the same plan under the default rule holds the cap
    _, _, ha_plan, _ = sv_tick(LF, 45, sv_intent(value=0.10, price=0.20))
    assert caps(ha_plan) == [1000.0]


def test_the_storage_value_floor_releases_to_the_planned_cap_when_keeping_the_energy_pays_more():
    # the energy is worth more kept: 0.12 - 0.01 - 0.11 = 0 < the 0.005 margin, and 45% is under
    # the plan's 50% end-of-slot SOC, so the plan-SOC rule holds the cap too
    record, _, ha, _ = sv_tick(SV, 45, sv_intent(value=0.11, price=0.12))
    assert caps(ha) == [1000.0]  # exactly the planned cap, as today
    desired = record["desired"]
    assert desired["load_following"] is False and desired["max_discharge_w"] == 1000.0
    assert desired["load_following_rule"] == "plan_soc"
    assert desired["load_following_storage_value_eur_per_kwh"] == 0.11
    assert desired["load_following_spread_eur_per_kwh"] == pytest.approx(0.0, abs=1e-9)
    assert desired["load_following_floor_pct"] == 50.0


@pytest.mark.parametrize(
    ("soc", "engaged"), [(49.0, False), (50.9, False), (51.0, True), (90, True)]
)
def test_when_keeping_pays_as_much_the_plan_soc_rule_still_follows_the_load(soc, engaged):
    # spread 0 < margin: never less load-following than the default floor, which engages at plan
    # SOC + 1 point (the 0.18.0 behaviour held the planned cap here)
    the_intent = sv_intent(value=0.11, price=0.12)
    record, _, ha, _ = sv_tick(SV, soc, the_intent)
    desired = record["desired"]
    assert desired["load_following"] is engaged and caps(ha) == [2500.0 if engaged else 1000.0]
    assert desired["load_following_rule"] == "plan_soc"
    assert desired["load_following_floor_pct"] == 50.0
    _, _, ha_default, _ = sv_tick(LF, soc, the_intent)
    assert caps(ha_default) == caps(ha)


@pytest.mark.parametrize(
    ("price", "engaged"),
    [(0.1251, True), (0.1250, True), (0.1249, False), (0.1, False)],
)
def test_the_margin_is_inclusive(price, engaged):
    # spread = price - 0.01 - 0.11; the margin is 0.005
    # 45% is under the plan's 50% SOC, so only the value rule can engage
    record, _, _, _ = sv_tick(SV, 45, sv_intent(value=0.11, price=price))
    assert record["desired"]["load_following"] is engaged


def test_a_negative_value_engages_it_too():
    # idle energy that costs dwell is worth less than nothing: discharging it is a gain
    record, _, ha, _ = sv_tick(SV, 80, sv_intent(value=-0.05, price=0.01))
    assert caps(ha) == [2500.0] and record["desired"]["load_following"] is True


@pytest.mark.parametrize(
    "case",
    [
        {"the_intent": sv_intent(value=None)},
        {"the_intent": replace(sv_intent(), expected_buy_price_eur_per_kwh=None)},
        {"the_intent": sv_intent(), "soc_floor_pct": None},
        {"the_intent": sv_intent(value=float("nan"))},
    ],
    ids=["no_value", "no_price", "no_tariff_floor", "nan_value"],
)
@pytest.mark.parametrize(("soc", "engaged"), [(49.0, False), (50.9, False), (51.0, True)])
def test_without_a_usable_value_the_plan_soc_rule_decides(case, soc, engaged):
    case = dict(case)
    record, _, ha, _ = sv_tick(SV, soc, case.pop("the_intent"), **case)
    desired = record["desired"]
    assert desired["load_following"] is engaged and caps(ha) == [2500.0 if engaged else 1000.0]
    assert desired["load_following_rule"] == "plan_soc"
    assert desired["load_following_storage_value_eur_per_kwh"] is None
    assert desired["load_following_spread_eur_per_kwh"] is None
    assert desired["load_following_floor_pct"] == 50.0  # the plan's SOC, as today


def test_the_fallback_keeps_the_plan_soc_hysteresis(caplog):
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=50.5)
    actuator, ha, _ = make(clock, store, settings=SV)
    the_intent = sv_intent(value=None)
    states = []
    for soc in (50.5, 51.0, 50.0, 49.9, 51.0):
        set_soc(store, clock, soc, seconds=10)
        states.append(tick(actuator, sv_ctx(clock, the_intent))["desired"]["load_following"])
    assert states == [False, True, True, False, True]


def test_the_soc_stays_clear_of_the_tariff_floor_with_hysteresis(caplog):
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=21.5)
    actuator, ha, _ = make(clock, store, settings=SV)
    the_intent = sv_intent(value=0.0, price=0.30)  # a huge spread: only the SOC can hold it back
    states = []
    with caplog.at_level("INFO", logger="battery_optimizer.service.actuator"):
        for soc in (21.5, 21.9, 22.0, 21.5, 21.0, 20.9, 21.5, 21.9, 22.0):
            set_soc(store, clock, soc, seconds=10)
            record = tick(actuator, sv_ctx(clock, the_intent))
            states.append(record["desired"]["load_following"])
    # engages at floor + 1 (reserve release band) + 1 (hysteresis), holds down to floor + 1
    assert states == [False, False, True, True, True, False, False, False, True]
    assert caps(ha) == [1000.0, 2500.0, 1000.0, 2500.0]  # one write per edge, no flapping
    lines = [r.getMessage() for r in caplog.records if "load_following=" in r.getMessage()]
    assert [line.split()[1] for line in lines] == [
        "load_following=on",
        "load_following=off",
        "load_following=on",
    ]
    assert "rule=storage_value storage_value=0 spread=0.29" in lines[0]


def test_reserve_protection_still_wins_at_the_floor():
    # at the floor the recovery charge replaces the discharge, whatever the spread
    record, actuator, ha, _ = sv_tick(SV, RESERVE, sv_intent(value=0.0, price=0.30))
    desired = record["desired"]
    assert desired["mode"] == "charge" and desired["reason"] == "below_soc_floor"
    assert "load_following" not in desired and caps(ha) == []
    assert actuator._floor_engaged is True


@pytest.mark.parametrize("mode", ["charge", "idle"])
def test_the_storage_value_floor_never_discharges_in_charge_or_idle_slots(mode):
    runs = []
    for settings in (SETTINGS, SV):
        clock = FakeClock(NOW)
        store = world(clock, grid=400, ac=0, soc=90, discharge="on", limits="on", cap=800)
        actuator, ha, _ = make(clock, store, settings=settings)
        record = tick(actuator, sv_ctx(clock, replace(sv_intent(), mode=mode, signed_power_w=0.0)))
        runs.append((ha.writes(), record["desired"]))
    assert runs[0] == runs[1]
    assert "load_following" not in runs[1][1] and "load_following_rule" not in runs[1][1]
    assert CAP not in [e for _, e, _ in runs[1][0]]


@pytest.mark.parametrize(
    ("max_w", "entity_max", "power", "cap"),
    [
        (2500.0, 2000, 1000.0, 2000.0),  # the entity's own max
        (1500.0, 2500, 2400.0, 2400.0),  # never below the planned cap
        (1800.0, 2500, 30.0, 1800.0),  # a tiny plan has the 100 W floor beneath
    ],
)
def test_the_storage_value_floor_keeps_every_cap_bound(max_w, entity_max, power, cap):
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=80)
    clock.advance(1)
    store.apply(
        entity_state(
            ENTS.max_discharge_power,
            0,
            clock.now(),
            attributes={"min": 0, "max": entity_max, "step": 1},
        )
    )
    settings = replace(
        SETTINGS,
        load_following=LoadFollowingSettings(enabled=True, max_w=max_w, floor="storage_value"),
    )
    actuator, ha, _ = make(clock, store, settings=settings)
    tick(actuator, sv_ctx(clock, sv_intent(power=power)))
    assert caps(ha) == [cap]
    assert 100.0 <= cap <= 2500.0


def test_the_default_floor_is_bit_identical_whatever_the_plan_says_about_value():
    """``plan_soc`` ignores the value: same writes, records and metrics as before the feature."""
    runs = []
    for the_intent in (lf_intent(), sv_intent(value=0.0, price=0.40), sv_intent(value=None)):
        clock = FakeClock(NOW)
        store = world(clock, grid=1500, ac=0, soc=60)
        records: list = []
        actuator, ha, _ = make(clock, store, settings=LF, records=records)
        record = tick(actuator, sv_ctx(clock, the_intent))
        set_soc(store, clock, 49)
        released = tick(actuator, sv_ctx(clock, the_intent))
        runs.append((ha.writes(), record["desired"], released["desired"], lf_snapshot(actuator)))
    assert runs[0] == runs[1] == runs[2]
    desired = runs[0][1]
    assert not [k for k in desired if "value" in k or "rule" in k or "spread" in k]
    assert set(runs[0][3]) == {
        "battery_optimizer_actuator_load_following_engaged",
        "battery_optimizer_actuator_load_following_raise_watts",
    }
    assert runs[0][1]["load_following"] is True and runs[0][2]["load_following"] is False


def test_the_default_floor_does_not_book_extra_energy():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=60)
    actuator, _, _ = make(clock, store, settings=LF)
    tick(actuator, sv_ctx(clock, sv_intent()))
    set_soc(store, clock, 60)
    store.apply(entity_state(ENTS.battery_ac_power, -1800, clock.now()))
    tick(actuator, sv_ctx(clock, sv_intent()))
    assert "extra_kwh" not in actuator.snapshot()["load_following"]


def sv_snapshot(actuator):
    from battery_optimizer.service.metrics import _actuator_families

    out = {}
    for family in _actuator_families(actuator.snapshot()):
        if "load_following" in family.name:
            out[family.name] = {
                tuple(sorted(s.labels.items())): s.value
                for s in family.samples
                if not s.name.endswith("_created")
            }
    return out


def feed_power(store, clock, ac, seconds=30, soc=60):
    clock.advance(seconds)
    store.apply(entity_state(GRID, 1500, clock.now()))
    store.apply(entity_state(ENTS.battery_soc, soc, clock.now()))
    store.apply(entity_state(ENTS.battery_ac_power, ac, clock.now()))


def test_the_energy_above_the_planned_cap_is_counted_while_engaged_and_valued():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=60)
    actuator, _, _ = make(clock, store, settings=SV)
    the_intent = sv_intent(value=0.10, price=0.20, power=1000.0)  # planned cap 1000 W, spread 0.09
    tick(actuator, sv_ctx(clock, the_intent))
    fams = sv_snapshot(actuator)
    assert fams["battery_optimizer_actuator_load_following_extra_kwh"] == {
        (("rule", "plan_soc"),): 0.0,
        (("rule", "storage_value"),): 0.0,
    }
    # 30 s later the battery delivers 1800 W; before it was at 0 W: trapezoid (0 + 800) / 2 W
    feed_power(store, clock, -1800)
    tick(actuator, sv_ctx(clock, the_intent))
    # another 30 s at 1800 W: (800 + 800) / 2 W
    feed_power(store, clock, -1800)
    tick(actuator, sv_ctx(clock, the_intent))
    # (the simulated HA latency adds a fraction of a second to each interval: loose tolerance)
    expected = (400.0 * 30 + 800.0 * 30) / 3_600_000.0
    fams = sv_snapshot(actuator)
    kwh = fams["battery_optimizer_actuator_load_following_extra_kwh"]
    assert kwh[(("rule", "storage_value"),)] == pytest.approx(expected, rel=0.02)
    assert kwh[(("rule", "plan_soc"),)] == 0.0
    value = fams["battery_optimizer_actuator_load_following_extra_value_eur"]
    assert value[()] == pytest.approx(
        expected * 0.09, rel=0.02
    )  # (price - wear - storage value) x kWh
    assert fams["battery_optimizer_actuator_load_following_storage_value_eur_per_kwh"][()] == 0.1
    assert fams["battery_optimizer_actuator_load_following_value_rule"][()] == 1.0


def test_discharge_at_or_below_the_planned_cap_and_released_ticks_add_nothing():
    clock = FakeClock(NOW)
    # 45% is under the plan's 50% SOC: once the value rule lets go, the plan-SOC rule holds too
    store = world(clock, grid=1500, ac=0, soc=45)
    actuator, _, _ = make(clock, store, settings=SV)
    engaged = sv_intent(value=0.10, price=0.20, power=1000.0)
    tick(actuator, sv_ctx(clock, engaged))
    feed_power(store, clock, -900, soc=45)  # under the planned cap: nothing above it
    tick(actuator, sv_ctx(clock, engaged))
    held = sv_intent(value=0.30, price=0.20, power=1000.0)  # keeping it pays: released
    feed_power(
        store, clock, -900, soc=45
    )  # the last engaged interval ended at 900 W: still nothing
    tick(actuator, sv_ctx(clock, held))
    feed_power(
        store, clock, -1800, soc=45
    )  # released ticks book nothing, whatever the battery does
    tick(actuator, sv_ctx(clock, held))
    feed_power(store, clock, -1800, soc=45)
    tick(actuator, sv_ctx(clock, held))
    fams = sv_snapshot(actuator)
    assert sum(fams["battery_optimizer_actuator_load_following_extra_kwh"].values()) == 0.0
    assert fams["battery_optimizer_actuator_load_following_extra_value_eur"][()] == 0.0


def test_the_fallback_rule_counts_its_energy_apart_and_without_a_value():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=60)
    actuator, _, _ = make(clock, store, settings=SV)
    no_value = sv_intent(value=None)
    tick(actuator, sv_ctx(clock, no_value))
    feed_power(store, clock, -2000)
    tick(actuator, sv_ctx(clock, no_value))
    fams = sv_snapshot(actuator)
    kwh = fams["battery_optimizer_actuator_load_following_extra_kwh"]
    assert kwh[(("rule", "plan_soc"),)] == pytest.approx((500.0 * 30) / 3_600_000.0, rel=0.02)
    assert kwh[(("rule", "storage_value"),)] == 0.0
    assert fams["battery_optimizer_actuator_load_following_extra_value_eur"][()] == 0.0
    assert fams["battery_optimizer_actuator_load_following_value_rule"][()] == 0.0
    assert "battery_optimizer_actuator_load_following_storage_value_eur_per_kwh" in fams
    assert not fams["battery_optimizer_actuator_load_following_storage_value_eur_per_kwh"]


def test_a_missed_tick_does_not_book_more_than_two_tick_intervals():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=60)
    actuator, _, _ = make(clock, store, settings=SV)
    the_intent = sv_intent(value=0.10, price=0.20, power=1000.0)
    tick(actuator, sv_ctx(clock, the_intent))
    feed_power(store, clock, -1800, seconds=600)  # ten minutes without a tick
    tick(actuator, sv_ctx(clock, the_intent))
    fams = sv_snapshot(actuator)
    kwh = fams["battery_optimizer_actuator_load_following_extra_kwh"][(("rule", "storage_value"),)]
    assert kwh == pytest.approx(400.0 * 60 / 3_600_000.0)  # capped at 2 x tick_interval_s


def test_a_guard_failure_drops_the_engaged_interval():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=60)
    actuator, _, _ = make(clock, store, settings=SV)
    the_intent = sv_intent(value=0.10, price=0.20, power=1000.0)
    tick(actuator, sv_ctx(clock, the_intent))
    feed_power(store, clock, -1800)
    tick(actuator, sv_ctx(clock, the_intent, price_fresh=False))  # safe idle ends the interval
    feed_power(store, clock, -1800)
    tick(actuator, sv_ctx(clock, the_intent))
    kwh = sv_snapshot(actuator)["battery_optimizer_actuator_load_following_extra_kwh"]
    assert sum(kwh.values()) == pytest.approx(400.0 * 30 / 3_600_000.0, rel=0.02)  # first only


def test_the_storage_value_floor_in_dry_run_records_the_rule_and_sends_nothing():
    clock = FakeClock(NOW)
    store = world(clock, grid=1500, ac=0, soc=60)
    records: list = []
    actuator, ha, _ = make(clock, store, mode="dry_run", settings=SV, records=records)
    record = tick(actuator, sv_ctx(clock, sv_intent()))
    assert ha.calls == [] and record["sent"] is False
    assert record["desired"]["load_following_rule"] == "storage_value"
    assert [w["value"] for w in record["writes"] if w["role"] == "max_discharge_power"] == [2500.0]
    assert records == [record]


def test_the_storage_value_floor_config_defaults_and_parsing(tmp_path):
    committed = load_service_config(REPO / "config" / "service.yaml")
    defaults = committed.actuation.load_following
    assert defaults.floor == "plan_soc" and defaults.storage_value_margin_eur_per_kwh == 0.005
    assert LoadFollowingSettings().floor == "plan_soc" and not LoadFollowingSettings().enabled
    on = load_service_config(
        write_service_config(
            tmp_path,
            **{
                "actuation.load_following": {
                    "enabled": True,
                    "floor": "storage_value",
                    "storage_value_margin_eur_per_kwh": 0.02,
                }
            },
        )
    )
    assert on.actuation.load_following.floor == "storage_value"
    assert on.actuation.load_following.storage_value_margin_eur_per_kwh == 0.02
    assert on.config_hash() == committed.config_hash()  # a control setting, not a plan input
    zero = load_service_config(
        write_service_config(
            tmp_path, **{"actuation.load_following.storage_value_margin_eur_per_kwh": 0}
        )
    )
    assert zero.actuation.load_following.storage_value_margin_eur_per_kwh == 0.0


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"actuation.load_following.floor": "soc"}, "floor must be one of plan_soc, storage_value"),
        ({"actuation.load_following.floor": None}, "floor must be one of"),
        ({"actuation.load_following.storage_value_margin_eur_per_kwh": -0.001}, "must be >= 0"),
        ({"actuation.load_following.storage_value_margin_eur_per_kwh": 0.2}, "must be <= 0.1"),
        ({"actuation.load_following.storage_value_margin_eur_per_kwh": True}, "must be a number"),
        (
            {"actuation.load_following.storage_value_margin_eur_per_kwh": float("nan")},
            "must be >= 0",
        ),
    ],
)
def test_invalid_storage_value_floor_config_is_refused(tmp_path, override, message):
    with pytest.raises(ValueError, match=message):
        load_service_config(write_service_config(tmp_path, **override))


def test_the_storage_value_floor_adds_nothing_to_the_guard_allowlist(tmp_path):
    from battery_optimizer.service.ha_guard import actuation_allowlist

    plain = load_service_config(
        write_service_config(tmp_path, **{**LIVE, "actuation.load_following.enabled": True})
    )
    sv = load_service_config(
        write_service_config(
            tmp_path,
            **{
                **LIVE,
                "actuation.load_following.enabled": True,
                "actuation.load_following.floor": "storage_value",
            },
        )
    )
    a, b = actuation_allowlist(plain), actuation_allowlist(sv)
    assert a is not None and b is not None and a == b
    assert b.numbers[ENTS.max_discharge_power] == (100.0, 2500.0)


def test_the_service_plans_the_value_and_feeds_the_actuator(tmp_path, cleanup):
    clock = FakeClock(NOW)
    config = service_config(
        tmp_path,
        **{
            **LIVE,
            "mode": "dry_run",
            "actuation.load_following": {"enabled": True, "floor": "storage_value"},
        },
    )
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="test-sha",
        ws_transport=FakeHAServer(),
    )
    cleanup(service)
    service.ha.stats.connected = True
    service.ha.stats.synced = True
    service.rest = None
    feed(service, NOW)
    _live_world(service, clock)
    assert asyncio.run(service.request_solve("test")) == "ok"
    plan = service.plans[-1]
    value = plan.storage_value
    assert value is not None and value.status == "ok" and value.eur_per_kwh is not None
    first = plan.intents[0]
    assert first.storage_value_eur_per_kwh == value.eur_per_kwh
    assert all(i.storage_value_eur_per_kwh is None for i in plan.intents[1:])
    text = service.metrics.render().decode()
    line = [x for x in text.splitlines() if x.startswith("battery_optimizer_planner_storage_value")]
    assert line and float(line[-1].split()[-1]) == pytest.approx(value.eur_per_kwh)
    context = service.actuation_context(clock.now())
    assert context.throughput_cost_eur_per_kwh == config.planning.throughput_cost_eur_per_kwh
    assert "storage_value" in service.plans[-1].to_record()


# ---------------------------------------------------------------------------
# Reserve protection (ADR-0007 amendment 2026-11-08): a bounded recovery charge
# while the measured SOC sits at or below the tariff's floor.  Forcing idle would
# not stop the measured residual drain (2026-11-08: idle slots, 18% to 15% in 6 h),
# so the tick must oppose it with the existing charge row.
# ---------------------------------------------------------------------------


def test_idle_at_the_floor_becomes_a_bounded_recovery_charge():
    clock = FakeClock(NOW)
    store = world(clock, soc=20.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    desired = record["desired"]
    assert desired["mode"] == "charge" and desired["reason"] == "below_soc_floor"
    assert desired["allow_charge"] is True and desired["allow_discharge"] is False
    # the existing charge row: the PD target is house load + power, so it sits above the load
    assert desired["house_load_w"] == 400 and desired["pd_target_w"] == 600.0
    assert desired["pd_target_w"] > desired["house_load_w"]
    assert desired["clamped"] is False
    assert ha.writes() == [
        ("number.set_value", "omnibattery_pd_target_grid_power", 600.0),
        ("switch.turn_on", "venus_battery_allow_charge", None),
    ]
    assert actuator._floor_engaged is True


def test_recovery_engages_below_the_floor_after_a_restart():
    # A restart at 15% (floor 20%): the latch starts down, the fresh measured SOC raises it.
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0, discharge="on", cap=800, limits="on", pd=50)
    actuator, ha, _ = make(clock, store)
    assert actuator._floor_engaged is False  # nothing evaluated before the first tick
    record = tick(actuator, ctx(clock, intent("discharge", 800), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "charge"
    assert record["desired"]["reason"] == "below_soc_floor"
    # restrictive first: discharge goes off before the recovery charge is enabled
    assert ha.writes() == [
        ("switch.turn_off", "venus_battery_allow_discharge", None),
        ("number.set_value", "omnibattery_pd_target_grid_power", 600.0),
        ("switch.turn_on", "venus_battery_allow_charge", None),
    ]


def test_recovery_uses_the_load_reconstructed_with_the_battery_sign():
    # grid 100 W import while the battery gives back 800 W: the house load is 900 W, so the
    # recovery opposes the house's draw, not what the meter happens to show.
    clock = FakeClock(NOW)
    store = world(clock, soc=18.0, grid=100, ac=-800)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["house_load_w"] == 900
    assert record["desired"]["pd_target_w"] == 1100.0


@pytest.mark.parametrize("soc", [20.0, 19.9, 15.0, 0.0])
def test_recovery_engages_at_or_below_the_floor(soc):
    clock = FakeClock(NOW)
    store = world(clock, soc=soc, grid=400, ac=0)
    actuator, _, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "charge"
    assert record["desired"]["reason"] == "below_soc_floor"


def test_a_stronger_planned_charge_is_preserved():
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("charge", 1500), soc_floor_pct=20.0))
    desired = record["desired"]
    assert desired["mode"] == "charge" and desired["reason"] == "intent"
    assert desired["pd_target_w"] == 1900.0  # the plan's own 1500 W, not the 200 W recovery
    assert actuator._floor_engaged is True  # the latch is evaluated either way


def test_a_weaker_planned_charge_is_raised_to_the_recovery_power():
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("charge", 50), soc_floor_pct=20.0))
    desired = record["desired"]
    assert desired["mode"] == "charge" and desired["reason"] == "below_soc_floor"
    assert desired["pd_target_w"] == 600.0  # load 400 + recovery 200, not the plan's 450


def test_a_weaker_planned_charge_without_headroom_keeps_its_own_row():
    # ceiling 2500 (the entity max), load 2400: only 100 W of headroom, less than the 200 W
    # recovery.  Raising would push the target to the ceiling the plan never asked for, so the
    # plan's own charge row runs, with its normal clamp accounting.
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=2400, ac=0)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("charge", 50), soc_floor_pct=20.0))
    desired = record["desired"]
    assert desired["mode"] == "charge" and desired["reason"] == "intent"
    assert desired["pd_target_w"] == 2450.0  # the plan's own 50 W, not 2500 (the ceiling)
    assert actuator._floor_engaged is True  # the latch is still reported


def test_a_stronger_planned_charge_is_never_reduced_even_without_headroom():
    # The same tight ceiling with a plan that wants more than fits: preserved, clamped as usual,
    # never lowered to the recovery power.
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=2400, ac=0)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("charge", 1500), soc_floor_pct=20.0))
    desired = record["desired"]
    assert desired["mode"] == "charge" and desired["reason"] == "intent"
    assert desired["pd_target_w"] == 2500.0  # min(load + P, import_cap_w, entity max)
    assert desired["clamped"] is True


def test_the_recovery_power_is_a_setting():
    clock = FakeClock(NOW)
    settings = replace(SETTINGS, soc_floor_recovery_w=500.0)
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store, settings=settings)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["pd_target_w"] == 900.0  # load 400 + 500


def test_the_entity_charge_maximum_bounds_the_recovery():
    # System power limits on: the PD target's max is the system max charge (here 900 W).
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0, limits="on")
    clock.advance(1)
    store.apply(
        entity_state(
            ENTS.pd_target, 0, clock.now(), attributes={"min": -800, "max": 900, "step": 1}
        )
    )
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "charge"
    assert record["desired"]["pd_target_w"] == 600.0  # 400 + 200 fits under the 900 W ceiling
    assert record["desired"]["clamped"] is False


@pytest.mark.parametrize("grid", [2400, 2500, 2600, 4600])
def test_no_import_headroom_holds_idle_instead_of_a_clamped_charge(grid):
    # ceiling min(import_cap 4500, entity max 2500) = 2500; headroom under 200 W buys no recovery,
    # so the tick idles instead of pushing the setpoint to the cap for nothing.
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=grid, ac=0, pd=1400)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("discharge", 800), soc_floor_pct=20.0))
    desired = record["desired"]
    assert desired["mode"] == "idle" and desired["reason"] == "below_soc_floor"
    assert desired["allow_charge"] is False and desired["pd_target_w"] == 0.0
    assert ha.writes() == [("number.set_value", "omnibattery_pd_target_grid_power", 0.0)]
    # the discharge the plan asked for is never enabled near the floor
    assert ("switch.turn_on", "venus_battery_allow_discharge", None) not in ha.writes()


def test_a_missing_load_never_produces_a_recovery_charge():
    # The guard is the first line; this checks the layer itself refuses to invent headroom.
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, ac=None)
    actuator, ha, _ = make(clock, store)
    readings = {"battery_soc_pct": 15.0, "house_load_w": None}
    context = ctx(clock, intent("idle"), soc_floor_pct=20.0)
    assert actuator._below_floor(context, readings) is True
    assert actuator._charge_headroom_w(readings) == 0.0
    assert actuator.reserve_desired(context, readings).mode == "idle"


def test_hysteresis_holds_until_the_floor_plus_one_point():
    clock = FakeClock(NOW)
    store = world(clock, soc=20.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert actuator._floor_engaged is True
    # inside the band (floor + 0.5): the latch holds, the recovery keeps charging
    clock.advance(60)
    store.apply(entity_state(ENTS.battery_soc, 20.5, clock.now()))
    store.apply(entity_state(GRID, 400, clock.now()))
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["reason"] == "below_soc_floor"
    assert actuator._floor_engaged is True
    # floor + 1.0: releases, the planned mapping runs again
    clock.advance(60)
    store.apply(entity_state(ENTS.battery_soc, 21.0, clock.now()))
    store.apply(entity_state(GRID, 400, clock.now()))
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["reason"] == "intent"
    assert record["desired"]["mode"] == "idle"
    assert actuator._floor_engaged is False


def test_the_hysteresis_width_is_a_setting():
    clock = FakeClock(NOW)
    settings = replace(SETTINGS, soc_floor_hysteresis_pct=3.0)
    store = world(clock, soc=20.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store, settings=settings)
    tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    clock.advance(60)
    store.apply(entity_state(ENTS.battery_soc, 22.0, clock.now()))
    store.apply(entity_state(GRID, 400, clock.now()))
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["reason"] == "below_soc_floor"  # still inside the 3-point band
    clock.advance(60)
    store.apply(entity_state(ENTS.battery_soc, 23.0, clock.now()))
    store.apply(entity_state(GRID, 400, clock.now()))
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["reason"] == "intent"


def test_no_floor_means_no_protection():
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0, discharge="on", cap=800, limits="on")
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("discharge", 800), soc_floor_pct=None))
    assert record["desired"]["mode"] == "discharge" and record["desired"]["reason"] == "intent"
    assert actuator._floor_engaged is False


@pytest.mark.parametrize(
    ("soc", "floor", "expected"),
    [
        (None, 20.0, "safe_idle"),  # stale or missing SOC: the guard holds safe idle first
        (float("nan"), 20.0, "safe_idle"),  # not a number: never a reason to engage
        (150.0, 20.0, "safe_idle"),  # outside the SOC range the readings accept
        (15.0, None, "idle"),  # no tariff floor: nothing to protect, the plan runs
    ],
)
def test_bad_or_missing_inputs_never_command_a_recovery(soc, floor, expected):
    clock = FakeClock(NOW)
    store = world(clock, soc=soc, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=floor))
    assert record["desired"]["mode"] == expected
    if floor is not None:
        assert "battery_soc_stale" in actuator.reasons
    assert actuator._floor_engaged is False
    assert not any(e == ENTS.allow_charge and s == "turn_on" for _d, s, e, _x in ha.calls)


def test_the_latch_survives_a_guard_failure():
    # A stale reading must not silently drop protection: the guard is safe idle anyway, and only
    # a fresh SOC above the release point clears the latch.
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert actuator._floor_engaged is True
    clock.advance(60)
    store.apply(entity_state(ENTS.battery_soc, None, clock.now()))  # SOC unreadable
    store.apply(entity_state(GRID, 400, clock.now()))
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "safe_idle"
    assert actuator._floor_engaged is True


def test_the_import_trip_and_its_hold_win_over_the_recovery():
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=4800, ac=0, charge="on", pd=1400)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "safe_idle"
    assert "import_over_cap" in actuator.reasons and actuator.trip_until is not None
    # the hold refuses charge: an idle tick inside it does not synthesize a recovery charge, and it
    # stays idle because the floor is latched (reason below_soc_floor, not the plan's idle intent)
    clock.advance(60)
    store.apply(entity_state(GRID, 1000, clock.now()))  # the headroom is back, the hold is not
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "idle"
    assert record["desired"]["reason"] == "below_soc_floor"
    assert record["desired"]["allow_charge"] is False
    assert not any(e == ENTS.allow_charge and s == "turn_on" for _d, s, e, _x in ha.calls)
    # once the hold expires the recovery resumes (the SOC is still below the floor)
    clock.advance(SETTINGS.import_trip_hold_s)
    store.apply(entity_state(GRID, 1000, clock.now()))
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "charge"
    assert record["desired"]["reason"] == "below_soc_floor"


@pytest.mark.parametrize("discharge", ["off", "on"])
def test_the_import_trip_hold_blocks_a_planned_discharge_below_the_floor(discharge):
    # The guard scopes the trip's refusal to charge intents, so this layer must refuse the
    # discharge as well: idling with no charge is the floor standing, extra discharge is not.
    clock = FakeClock(NOW)
    store = world(
        clock,
        soc=15.0,
        grid=4800,
        ac=0,
        charge="on",
        discharge=discharge,
        cap=800,
        limits="on",
        pd=1400,
    )
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("charge", 1000), soc_floor_pct=20.0))  # the trip fires here
    assert "import_over_cap" in actuator.reasons and actuator.trip_until is not None
    clock.advance(60)
    store.apply(entity_state(GRID, 1000, clock.now()))  # the import is back below the cap
    record = tick(actuator, ctx(clock, intent("discharge", 800), soc_floor_pct=20.0))
    desired = record["desired"]
    assert desired["mode"] == "idle" and desired["reason"] == "below_soc_floor"
    assert desired["allow_charge"] is False and desired["allow_discharge"] is False
    assert not any(e == ENTS.allow_charge and s == "turn_on" for _d, s, e, _x in ha.calls)
    assert not any(e == ENTS.allow_discharge and s == "turn_on" for _d, s, e, _x in ha.calls)
    # after the hold the recovery takes over (and the planned discharge does not)
    clock.advance(SETTINGS.import_trip_hold_s)
    store.apply(entity_state(GRID, 1000, clock.now()))
    record = tick(actuator, ctx(clock, intent("discharge", 800), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "charge"
    assert record["desired"]["reason"] == "below_soc_floor"


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"ha_synced": False}, "ha_not_synced"),
        ({"decision": "no_plan"}, "no_plan"),
        ({"decision": "plan_expired"}, "plan_expired"),
        ({"decision": "not_ready"}, "inputs_not_ready"),
        ({"plan_from_this_process": False}, "plan_not_from_this_process"),
        ({"price_fresh": False}, "price_stale"),
    ],
)
def test_plan_readiness_and_sync_guards_keep_priority(kw, reason):
    clock = FakeClock(NOW)
    the_intent = None if "decision" in kw else intent("idle")
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, the_intent, soc_floor_pct=20.0, **kw))
    assert reason in actuator.reasons
    if "ha_synced" in kw:  # nothing can be sent: no desired at all, let alone a charge
        assert record["desired"] is None
    else:
        assert record["desired"]["mode"] == "safe_idle"
    assert not any(e == ENTS.allow_charge and s == "turn_on" for _d, s, e, _x in ha.calls)


def test_an_unhealthy_integration_status_wins_over_the_recovery():
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0, status="backup_mode")
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "safe_idle"
    assert "integration_unhealthy" in actuator.reasons
    assert actuator._floor_engaged is False  # the guard ran before the latch could be consulted


def test_the_kill_switch_wins_and_leaves_the_latch_alone():
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0, kill="off")
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert actuator.in_control is False and actuator._floor_engaged is False
    assert ha.writes() == []


def test_an_unresponsive_battery_holds_safe_idle_not_a_recovery():
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0, liveness="1")
    actuator, ha, _ = make(clock, store)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "safe_idle"
    assert "battery_unresponsive" in actuator.reasons
    assert actuator._floor_engaged is False


def test_the_settle_hold_keeps_priority_over_the_recovery():
    clock = FakeClock(NOW)
    store = fc_world(
        clock, weekly="on", day=TODAY, status="balanced", soc=15.0, grid=400, ac=0, pd=1400
    )
    actuator, ha, _ = make(clock, store, full_charge=FC)
    record = tick(actuator, ctx(clock, intent("charge", 1000), full_charge=hold(clock)))
    assert record["desired"]["mode"] == "idle" and record["desired"]["reason"] == "settle_hold"
    assert record["desired"]["allow_charge"] is False
    assert not any(e == ENTS.allow_charge and s == "turn_on" for _d, s, e, _x in ha.calls)


def test_a_recovery_charge_still_arms_the_weekly_full_charge():
    clock = FakeClock(NOW)
    store = fc_world(
        clock, weekly="off", day=TODAY, status="balanced", soc=15.0, grid=400, ac=0, pd=0
    )
    actuator, ha, _ = make(clock, store, full_charge=FC)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0, full_charge=ARMED))
    desired = record["desired"]
    assert desired["mode"] == "charge" and desired["reason"] == "below_soc_floor"
    assert desired["full_charge_armed"] is True and desired["full_charge_weekday"] == TODAY
    # the day select already reads TODAY, so only the switch is written (idempotent arming)
    assert ("switch.turn_on", "omnibattery_weekly_full_charge_enabled", None) in ha.writes()
    entities = [e for _s, e, _v in ha.writes()]
    assert entities.index("omnibattery_weekly_full_charge_enabled") < entities.index(
        "venus_battery_allow_charge"
    )  # armed before the direction is switched on


@pytest.mark.parametrize("mode", ["dry_run", "live"])
def test_reserve_protection_applies_in_both_actuation_modes(mode):
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store, mode=mode)
    record = tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert record["desired"]["mode"] == "charge"
    assert record["desired"]["reason"] == "below_soc_floor"
    assert record["sent"] is (mode == "live")
    if mode == "dry_run":
        assert ha.writes() == []  # computed and recorded, nothing sent
        assert record["writes"][0]["result"] == "dry_run"
    else:
        assert ("number.set_value", "omnibattery_pd_target_grid_power", 600.0) in ha.writes()


def test_the_recovery_reason_is_not_counted_as_a_safe_idle():
    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, metrics = make(clock, store)
    tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert actuator.desired.mode == "charge"
    # the safe-idle counter covers real guard fallbacks; a deliberate recovery is not one
    assert counter(metrics, "actuator_safe_idles", reason="below_soc_floor") == 0


def test_status_view_reports_the_reserve_state():
    import json

    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    assert actuator.view()["reserve"] == {
        "floor_pct": 20.0,
        "engaged": True,
        "recovery_w": 200.0,
        "hysteresis_pct": 1.0,
    }
    json.dumps(actuator.view())  # /status must stay serialisable


def test_the_reserve_engaged_gauge_is_exported():
    from battery_optimizer.service.metrics import _actuator_families

    clock = FakeClock(NOW)
    store = world(clock, soc=15.0, grid=400, ac=0)
    actuator, ha, _ = make(clock, store)
    tick(actuator, ctx(clock, intent("idle"), soc_floor_pct=20.0))
    family = next(
        f for f in _actuator_families(actuator.snapshot()) if f.name.endswith("_reserve_engaged")
    )
    assert family.name == "battery_optimizer_actuator_reserve_engaged"
    assert [s.value for s in family.samples] == [1.0]


def test_actuation_context_passes_the_tariff_floor(tmp_path, cleanup):
    # One authoritative floor: battery.soc_window_min of config/tariff.yaml, never a second setting.
    from battery_optimizer.service.runtime import _soc_floor_pct

    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    cleanup(service)
    assert service.tariff.battery.soc_window_min == 0.20
    assert service.actuation_context(clock.now()).soc_floor_pct == 20.0
    assert _soc_floor_pct(None) is None

    def window(value):
        return replace(
            service.tariff, battery=replace(service.tariff.battery, soc_window_min=value)
        )

    assert _soc_floor_pct(window(None)) is None
    assert _soc_floor_pct(window(float("nan"))) is None  # a bogus floor disables, never arms
    assert _soc_floor_pct(window(1.5)) is None
    assert _soc_floor_pct(window(0.15)) == 15.0


def test_the_tariff_floor_recovers_a_live_slot_below_it(tmp_path, cleanup):
    # End to end through the runtime's own context, as after a restart at 15%.
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, mode="dry_run")
    cleanup(service)
    _live_world(service, clock)
    service.ha.stats.synced = False  # the slot opens before the first HA sync
    asyncio.run(service.open_slot(SLOT))
    assert service.actuation_context(NOW).decision == "not_ready"
    service.ha.stats.synced = True
    feed(service, NOW)

    async def go():
        assert await service.request_solve("event:resync") == "ok"  # a catch-up plan
        clock.advance(1)  # the store keeps the newest report, and _live_world reported at NOW
        service.store.apply(entity_state(ENTS.battery_soc, 15.0, clock.now()))
        return await service.actuator.tick(service.actuation_context(clock.now()))

    record = asyncio.run(go())
    assert service._slot_open[SLOT].decision == "late_intent"
    context = service.actuation_context(clock.now())
    assert context.soc_floor_pct == 20.0 and context.intent is not None
    assert record["guard_reasons"] == []
    assert record["desired"]["reason"] == "below_soc_floor"
    assert record["desired"]["mode"] == "charge"
    assert service.actuator.view()["reserve"] == {
        "floor_pct": 20.0,
        "engaged": True,
        "recovery_w": 200.0,
        "hysteresis_pct": 1.0,
    }


def test_reserve_defaults_are_modest_and_the_committed_config_keeps_them(tmp_path):
    committed = load_service_config(REPO / "config" / "service.yaml")
    assert committed.actuation.soc_floor_recovery_w == 200.0
    assert committed.actuation.soc_floor_hysteresis_pct == 1.0
    on = load_service_config(
        write_service_config(
            tmp_path,
            **{
                "actuation.soc_floor_recovery_w": 300,
                "actuation.soc_floor_hysteresis_pct": 2,
            },
        )
    )
    assert on.actuation.soc_floor_recovery_w == 300.0
    assert on.actuation.soc_floor_hysteresis_pct == 2.0
    assert on.config_hash() == committed.config_hash()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"actuation.soc_floor_recovery_w": 0}, "soc_floor_recovery_w"),
        ({"actuation.soc_floor_recovery_w": -200}, "soc_floor_recovery_w"),
        ({"actuation.soc_floor_recovery_w": 5000}, "must not exceed import_cap_w"),
        ({"actuation.soc_floor_hysteresis_pct": 0}, "soc_floor_hysteresis_pct must be > 0"),
        ({"actuation.soc_floor_hysteresis_pct": 11}, "soc_floor_hysteresis_pct must be <= 10"),
        ({"actuation.soc_floor_bogus": 1}, "unknown actuation keys"),
    ],
)
def test_invalid_reserve_config_is_refused(tmp_path, override, message):
    with pytest.raises(ValueError, match=message):
        load_service_config(write_service_config(tmp_path, **{**LIVE, **override}))


def test_the_recovery_only_writes_allowlisted_entities(tmp_path):
    from battery_optimizer.service.ha_guard import actuation_allowlist, actuation_request

    config = load_service_config(write_service_config(tmp_path, **LIVE))
    allowlist = actuation_allowlist(config)
    assert allowlist is not None
    # the recovery uses only the two entities the charge row already uses, inside their bounds
    frame = actuation_request(1, "number", "set_value", ENTS.pd_target, {"value": 600}, allowlist)
    assert frame["service_data"] == {"value": 600}
    actuation_request(1, "switch", "turn_on", ENTS.allow_charge, None, allowlist)
    with pytest.raises(ForbiddenHACallError):
        actuation_request(1, "switch", "turn_on", ENTS.kill_switch, None, allowlist)
