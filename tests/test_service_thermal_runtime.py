"""The thermal shadow: tracker, config, plan record, /status, /plan, metrics, scoring, restart."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
from service_fakes import (
    ENTITY_MODE,
    NOW,
    TARIFF,
    FakeHAServer,
    entity_state,
    feed,
    service_config,
    write_service_config,
)
from test_service_thermal import TRUTH, make_buffer

from battery_optimizer.service import runtime as runtime_module
from battery_optimizer.service.api import create_app
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import ThermalSettings, load_service_config
from battery_optimizer.service.ha_guard import (
    ForbiddenHACallError,
    actuation_allowlist,
    check_ws_message,
)
from battery_optimizer.service.runtime import ShadowService
from battery_optimizer.service.thermal import DEFAULT_PARAMS, RefitResult, ThermalParams
from battery_optimizer.service.thermal_tracker import ThermalTracker

TEMP = "sensor.venus_internal_temperature"
POWER = "sensor.venus_ac_power"
SWITCH = "switch.omnibattery_temp_charge_limit"
DISCHARGE = "switch.omnibattery_temp_charge_limit_discharge"
LIMIT = "number.omnibattery_temp_charge_limit_c"
BAND = "number.omnibattery_temp_charge_limit_band_c"
FLOOR = "number.omnibattery_temp_charge_limit_floor_pct"
SYS_CAP = "number.omnibattery_system_max_charge_power"
VENUS_CAP = "number.venus_max_charge_power"
THERMAL_ENTITIES = (TEMP, SWITCH, DISCHARGE, LIMIT, BAND, FLOOR, SYS_CAP, VENUS_CAP)
SHADOW = {**ENTITY_MODE, "planning.thermal.mode": "shadow"}
MADRID = ZoneInfo("Europe/Madrid")


def make_service(tmp_path, clock, **overrides):
    service = ShadowService(
        config=service_config(tmp_path, **overrides),
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="test-sha",
        ws_transport=FakeHAServer(),
    )
    service.ha.stats.connected = True
    service.ha.stats.synced = True
    service.observer.mark_synced(clock.now())
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
        service._thermal_pool.shutdown(wait=True)


def apply(service, entity, state, at):
    service.store.apply(entity_state(entity, state, at))


def set_thermal(
    service,
    at,
    *,
    switch="on",
    limit=40,
    band=10,
    floor=40,
    temp=None,
    power=None,
    discharge="off",
):
    apply(service, SWITCH, switch, at)
    apply(service, LIMIT, limit, at)
    apply(service, BAND, band, at)
    apply(service, FLOOR, floor, at)
    apply(service, DISCHARGE, discharge, at)
    if temp is not None:
        apply(service, TEMP, temp, at)
    if power is not None:
        apply(service, POWER, power, at)


def samples(text: str) -> str:
    """The sample lines of an exposition (no HELP / TYPE comments)."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def plan_files(tmp_path):
    return sorted((tmp_path / "data" / "service" / "v1" / "plans").rglob("*.json"))


def state_dir(tmp_path):
    return tmp_path / "data" / "service" / "v1" / "state"


# ---------------------------------------------------------------------------
# mode off: nothing changes
# ---------------------------------------------------------------------------


def test_mode_off_reads_nothing_records_nothing_and_exposes_nothing(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **ENTITY_MODE)
    cleanup(service)
    assert service.config.planning.thermal.mode == "off"
    assert service.thermal is None
    for entity in (SWITCH, DISCHARGE, LIMIT, BAND, FLOOR, SYS_CAP, VENUS_CAP):
        assert entity not in service.store.tracked
    feed(service, NOW)
    assert run(service.request_solve("test")) == "ok"
    record = json.loads(plan_files(tmp_path)[0].read_text())
    assert "thermal" not in record
    assert "thermal" not in service.status()
    assert "thermal" not in service.metric_snapshot()
    assert "thermal" not in service.plan_view()["plan"]
    assert "thermal" not in service.plan_view()["current_slot"]
    assert "battery_optimizer_thermal_" not in service.metrics.render().decode()
    assert not hasattr(service.metrics, "thermal_refits")
    assert not state_dir(tmp_path).exists()


def test_the_thermal_block_never_joins_the_config_hash(tmp_path):
    off = load_service_config(write_service_config(tmp_path, **ENTITY_MODE))
    shadow = load_service_config(write_service_config(tmp_path, **SHADOW))
    other = load_service_config(
        write_service_config(
            tmp_path,
            **{**SHADOW, "planning.thermal.prior": {"tau_min": 60.0, "k_c": 9.0}},
        )
    )
    assert off.config_hash() == shadow.config_hash() == other.config_hash()
    assert shadow.public_dict()["planning"]["thermal"]["mode"] == "shadow"


def test_shadow_changes_neither_the_plan_nor_the_intents(tmp_path, cleanup):
    results = {}
    for name, extra in (("off", {}), ("shadow", {"planning.thermal.mode": "shadow"})):
        clock = FakeClock(NOW)
        (tmp_path / name).mkdir()
        service = make_service(tmp_path / name, clock, **ENTITY_MODE, **extra)
        cleanup(service)
        feed(service, NOW)
        if service.thermal is not None:
            set_thermal(service, NOW, temp=47.0, power=-2000)
        assert run(service.request_solve("test")) == "ok"
        plan = service.plans[-1]
        results[name] = (
            plan.plan.content_hash,
            plan.inputs_hash,
            [i.to_dict() for i in plan.intents],
            plan.snapshot.config_hash,
        )
    assert results["off"] == results["shadow"]


def test_the_plan_record_only_gains_a_thermal_key(tmp_path, cleanup):
    records = {}
    for name, extra in (("off", {}), ("shadow", {"planning.thermal.mode": "shadow"})):
        (tmp_path / name).mkdir()
        service = make_service(tmp_path / name, FakeClock(NOW), **ENTITY_MODE, **extra)
        cleanup(service)
        feed(service, NOW)
        assert run(service.request_solve("test")) == "ok"
        records[name] = json.loads(plan_files(tmp_path / name)[0].read_text())
        records[name]["storage_value"].pop("seconds")  # wall-clock time of the extra solves
    assert set(records["shadow"]) - set(records["off"]) == {"thermal"}
    volatile = {"thermal", "solve_seconds"}
    assert {k: v for k, v in records["shadow"].items() if k not in volatile} == {
        k: v for k, v in records["off"].items() if k != "solve_seconds"
    }


# ---------------------------------------------------------------------------
# shadow: subscription, unknown, plan record
# ---------------------------------------------------------------------------


def test_the_inputs_and_the_ac_power_are_tracked_in_shadow_mode_and_never_allowlisted(tmp_path):
    service = make_service(tmp_path, FakeClock(NOW), **SHADOW)
    assert service.config.mode == "shadow"
    for entity in (*THERMAL_ENTITIES, POWER):
        assert entity in service.store.tracked
    assert service.thermal is not None
    assert actuation_allowlist(service.config) is None


def test_the_live_allowlist_is_unchanged_and_refuses_every_thermal_entity(tmp_path):
    config = load_service_config(
        write_service_config(
            tmp_path,
            **{
                "mode": "live",
                "actuation.battery_ac_power_sign": 1,
                "planning.soc_source": "entity",
                "entities.battery_soc": "sensor.test_soc",
                "planning.thermal.mode": "shadow",
            },
        )
    )
    allow = actuation_allowlist(config)
    assert allow is not None
    named = {*allow.switches, *allow.numbers, *allow.datetimes, *allow.selects}
    assert not named & set(THERMAL_ENTITIES)
    baseline = load_service_config(
        write_service_config(
            tmp_path,
            **{
                "mode": "live",
                "actuation.battery_ac_power_sign": 1,
                "planning.soc_source": "entity",
                "entities.battery_soc": "sensor.test_soc",
            },
        )
    )
    assert actuation_allowlist(baseline) == allow
    for entity in (SWITCH, DISCHARGE):
        call = {
            "id": 1,
            "type": "call_service",
            "domain": "switch",
            "service": "turn_off",
            "target": {"entity_id": entity},
        }
        with pytest.raises(ForbiddenHACallError):
            check_ws_message(call, allow)
    for entity in (LIMIT, BAND, FLOOR, SYS_CAP, VENUS_CAP):
        call = {
            "id": 1,
            "type": "call_service",
            "domain": "number",
            "service": "set_value",
            "target": {"entity_id": entity},
            "service_data": {"value": 1000},
        }
        with pytest.raises(ForbiddenHACallError):
            check_ws_message(call, allow)


def test_without_any_thermal_state_the_derate_is_unknown_and_planning_goes_on(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    feed(service, NOW)
    assert run(service.request_solve("test")) == "ok"  # never blocks planning
    block = json.loads(plan_files(tmp_path)[0].read_text())["thermal"]
    assert block["derate"]["state"] == "unknown"
    assert block["cold_start"] is True and block["temperature_start_c"] is None
    assert set(block["slots"]["derate_factor"]) == {1.0}
    status = service.status()["thermal"]
    assert status["derate"]["state"] == "unknown" and status["derate"]["derating_now"] is None
    assert status["temperature_c"] is None and status["model"]["status"] == "prior"
    text = service.metrics.render().decode()
    assert 'battery_optimizer_thermal_derate_state{state="unknown"} 1.0' in text
    assert "battery_optimizer_thermal_temperature_celsius " not in samples(text)  # never 0


@pytest.mark.parametrize("state", ["unavailable", "unknown", "garbage"])
def test_unavailable_readings_are_unknown_not_zero(tmp_path, state):
    service = make_service(tmp_path, FakeClock(NOW), **SHADOW)
    set_thermal(service, NOW, temp=45.0, power=-1500)
    apply(service, TEMP, state, NOW + timedelta(seconds=1))
    apply(service, LIMIT, state, NOW + timedelta(seconds=1))
    view = service.status()["thermal"]
    assert view["temperature_c"] is None
    assert view["derate"]["state"] == "unknown"
    assert view["derate"]["limit_c"] is None


def test_the_plan_record_carries_the_per_slot_prediction(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    feed(service, NOW)
    set_thermal(service, NOW, temp=44.0, power=-2000)
    apply(service, SYS_CAP, 2500, NOW)
    apply(service, VENUS_CAP, 2500, NOW)
    assert run(service.request_solve("test")) == "ok"
    plan = service.plans[-1]
    record = json.loads(plan_files(tmp_path)[0].read_text())
    block = record["thermal"]
    n = len(plan.snapshot.slot_starts)
    assert block["mode"] == "shadow" and block["interval_seconds"] == 900
    assert block["derate"]["state"] == "on" and block["derate"]["limit_kw"] == 2.5
    assert block["temperature_start_c"] == 44.0 and block["cold_start"] is False
    assert block["model"]["status"] == "prior"
    assert block["model"]["k_c"] == DEFAULT_PARAMS.k_c
    slots = block["slots"]
    assert all(len(v) == n for v in slots.values())
    planned = np.asarray(slots["charge_planned_kw"])
    achievable = np.asarray(slots["charge_kw"])
    assert (achievable <= planned + 1e-9).all()
    # the planned charge is the plan's own request, slot by slot
    by_start = {i.valid_from: i for i in plan.intents}
    for k, start in enumerate(plan.snapshot.slot_starts):
        intent = by_start[start]
        expected = intent.target_power_w / 1000.0 if intent.mode == "charge" else 0.0
        assert planned[k] == pytest.approx(expected, abs=1e-3)
    assert block["summary"]["max_temp_c"] >= 44.0
    json.dumps(record, allow_nan=False)  # the record stays strict JSON


def test_a_prediction_that_fails_never_blocks_planning(tmp_path, cleanup, monkeypatch, caplog):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    feed(service, NOW)

    def boom(*_a, **_k):
        raise RuntimeError("thermal model exploded")

    monkeypatch.setattr(service.thermal, "predict", boom)
    assert run(service.request_solve("test")) == "ok"
    assert service.plans
    assert "thermal" not in json.loads(plan_files(tmp_path)[0].read_text())
    assert service.metrics.thermal_predictions.labels(result="error")._value.get() == 1


def test_status_plan_and_metrics_show_the_state(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    feed(service, NOW)
    set_thermal(service, NOW, temp=48.0, power=-2000)
    assert run(service.request_solve("test")) == "ok"
    status = service.status()["thermal"]
    assert status["mode"] == "shadow" and status["actuation"].startswith("none")
    assert status["temperature_c"] == 48.0
    assert status["derate"]["state"] == "on"
    assert status["derate"]["factor_now"] == pytest.approx(0.52)
    assert status["derate"]["derating_now"] is True
    assert status["derate"]["limit_c"] == 40.0 and status["derate"]["floor_pct"] == 40.0
    assert status["model"]["status"] == "prior"
    assert status["model"]["prior"] == DEFAULT_PARAMS.to_dict()
    assert status["power_kw"] == 2.0  # committed sign -1: -2000 W is charging
    plan = service.plan_view()
    assert plan["plan"]["thermal"]["derate"]["state"] == "on"
    current = plan["current_slot"]["thermal"]
    assert current is None or current["plan_id"] == service.plans[-1].plan_id
    text = service.metrics.render().decode()
    assert 'battery_optimizer_thermal_derate_state{state="on"} 1.0' in text
    assert "battery_optimizer_thermal_derate_factor 0.52" in text
    assert "battery_optimizer_thermal_derating 1.0" in text
    assert 'battery_optimizer_thermal_model_status{status="prior"} 1.0' in text
    assert 'battery_optimizer_thermal_parameter{name="k_c"} 11.0' in text
    assert 'battery_optimizer_thermal_limit_setting{name="band_c"} 10.0' in text
    assert "battery_optimizer_thermal_temperature_celsius 48.0" in text
    assert "battery_optimizer_thermal_predicted_temperature_celsius" in text
    assert 'battery_optimizer_thermal_refits_total{result="accepted"} 0.0' in text
    assert "thermal_last_fit_timestamp_seconds" not in samples(text)  # still the prior


def test_the_http_surface_serves_the_block_as_strict_json(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    app = create_app(service, manage_lifecycle=False)

    async def go():
        feed(service, NOW)
        set_thermal(service, NOW, temp=41.0, power=0)
        assert await service.request_solve("test") == "ok"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bo.test") as client:
            status = await client.get("/status")
            plan = await client.get("/plan")
            metrics = await client.get("/metrics")
        return status, plan, metrics

    status, plan, metrics = run(go())
    assert status.json()["thermal"]["derate"]["state"] == "on"
    slots = plan.json()["plan"]["thermal"]["slots"]
    assert {"temp_end_c", "charge_kw", "charge_cap_kw", "derate_factor"} <= set(slots)
    assert b"battery_optimizer_thermal_parameter" in metrics.content


# ---------------------------------------------------------------------------
# scoring at slot close
# ---------------------------------------------------------------------------


def test_the_slot_close_scores_the_predicted_against_the_measured_temperature(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    feed(service, NOW)
    set_thermal(service, NOW, temp=40.0, power=0)
    assert run(service.request_solve("test")) == "ok"
    first = service.plans[-1].plan.horizon_start
    clock.set(first)
    feed(service, first)
    set_thermal(service, first, temp=40.0, power=0)
    opened = run(service.open_slot(first))
    assert opened.final is not None
    end = first + timedelta(minutes=15)
    for minute in (5, 10, 14):
        clock.set(first + timedelta(minutes=minute))
        service.thermal.advance(clock.now())
        apply(service, TEMP, 41.5, clock.now())
    clock.set(end + timedelta(seconds=20))
    record = run(service.close_slot(first))
    scored = record["thermal"]
    assert scored["measured_temp_end_c"] == 41.5
    final = scored["leads"]["final"]
    assert final["plan_id"] == opened.final.plan_id
    assert final["error_c"] == pytest.approx(final["predicted_temp_end_c"] - 41.5, abs=1e-2)
    text = service.metrics.render().decode()
    assert 'battery_optimizer_thermal_scored_slots_total{lead="final"} 1.0' in text
    name = 'battery_optimizer_thermal_temperature_abs_error_celsius_total{lead="final"} '
    (line,) = [x for x in text.splitlines() if x.startswith(name)]
    assert float(line.removeprefix(name)) == pytest.approx(abs(final["error_c"]), abs=2e-3)
    assert 'temperature_error_sum_celsius{lead="final"}' in text
    rows = [
        json.loads(line)
        for f in (tmp_path / "data" / "service" / "v1" / "slots").rglob("*.jsonl")
        for line in f.read_text().splitlines()
    ]
    assert rows[0]["thermal"]["leads"]["final"]["plan_id"] == opened.final.plan_id


def test_without_a_final_plan_or_a_measurement_nothing_is_scored(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    feed(service, NOW)
    first = NOW.replace(minute=0, second=0) + timedelta(hours=1)
    clock.set(first)
    run(service.open_slot(first))
    clock.set(first + timedelta(minutes=15, seconds=20))
    record = run(service.close_slot(first))
    assert record["thermal"]["leads"] == {} and record["thermal"]["measured_temp_end_c"] is None
    text = service.metrics.render().decode()
    assert 'battery_optimizer_thermal_scored_slots_total{lead="final"} 0.0' in text


# ---------------------------------------------------------------------------
# the tracker
# ---------------------------------------------------------------------------

T0 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def tracker(sign: int = 1, **kw) -> ThermalTracker:
    return ThermalTracker(ThermalSettings(mode="shadow"), ac_power_sign=sign, **kw)


def test_the_buffer_holds_values_by_the_minute_and_unknown_stays_nan():
    t = tracker()
    t.advance(at(0))
    t.observe("temperature", "30.0", at(10))
    t.observe("charge_power", "2000", at(70))
    t.advance(at(250))
    temp, power = t.arrays()
    assert temp.size == power.size == 5  # minutes 0..4
    assert np.isnan(temp[0]) and temp[1:].tolist() == [30.0] * 4
    assert np.isnan(power[:2]).all() and power[2:].tolist() == [2.0] * 3
    t.observe("charge_power", "unavailable", at(250))
    t.advance(at(330))
    assert np.isnan(t.arrays()[1][-1])  # unavailable power is unknown, never idle


def test_the_sign_corrects_the_power_so_that_charging_is_positive():
    plus, minus = tracker(1), tracker(-1)
    for tr, raw in ((plus, "800"), (minus, "-800")):
        tr.advance(at(0))
        tr.observe("charge_power", raw, at(5))
        tr.advance(at(65))
        assert tr.arrays()[1][-1] == pytest.approx(0.8)
    with pytest.raises(ValueError):
        tracker(0)


def test_a_temperature_older_than_the_age_limit_is_missing():
    t = tracker(temperature_max_age_s=300.0)
    t.advance(at(0))
    t.observe("temperature", "36.0", at(0))
    assert t.temperature_now(at(200)) == 36.0
    assert t.temperature_now(at(400)) is None
    t.advance(at(900))
    temp = t.arrays()[0]
    assert temp[1:6].tolist() == [36.0] * 5 and np.isnan(temp[7:]).all()


def test_minutes_while_not_synced_are_unknown_not_idle():
    t = tracker()
    t.advance(at(0))
    t.observe("temperature", "33.0", at(0))
    t.observe("charge_power", "1000", at(0))
    t.advance(at(60))
    t.advance(at(240), observing=False)  # minutes 2 to 4: the session was not synced
    t.advance(at(300))
    temp, power = t.arrays()
    assert temp[1] == 33.0 and power[1] == 1.0
    assert np.isnan(temp[2:5]).all() and np.isnan(power[2:5]).all()
    assert temp[5] == 33.0 and power[5] == 1.0


def test_a_long_gap_clears_the_buffer_instead_of_inventing_idle_time():
    t = tracker(buffer_days=0.01)  # 14 minutes
    t.advance(at(0))
    t.observe("temperature", "33.0", at(0))
    t.advance(at(3600))
    assert t.arrays()[0].size == 1


def test_switch_numbers_and_caps_make_the_derate_settings():
    t = tracker()
    assert t.derate_settings().state == "unknown"
    t.observe("limit_switch", "on", at(0))
    for role, value in (("limit_c", "40"), ("band_c", "10"), ("floor_pct", "40")):
        t.observe(role, value, at(0))
    d = t.derate_settings()
    assert d.state == "on" and d.limit_kw == 2.5 and not d.discharge_enabled
    t.observe("system_max_charge_power", "2000", at(1))
    t.observe("venus_max_charge_power", "2500", at(1))
    assert t.derate_settings().limit_kw == 2.0
    t.observe("system_max_charge_power", "unavailable", at(2))
    assert t.derate_settings().limit_kw == 2.5  # only the Venus cap is known
    t.observe("venus_max_charge_power", "unknown", at(2))
    assert t.derate_settings().limit_kw == 2.5  # neither: the configured limit
    t.observe("discharge_switch", "on", at(3))
    assert t.derate_settings().discharge_enabled
    t.observe("limit_switch", "off", at(4))
    assert t.derate_settings().state == "off" and not t.derate_settings().discharge_enabled
    with pytest.raises(KeyError):
        t.observe("mystery", "1", at(5))


def _predicted(t: ThermalTracker, charge, discharge=None, plan_id="p1", generated=None):
    slots = [at(900 * i) for i in range(len(charge))]
    return t.predict(
        plan_id=plan_id,
        generated_at=generated or at(-300),
        slot_starts=slots,
        interval_s=900.0,
        charge_kw=charge,
        discharge_kw=discharge or [0.0] * len(charge),
        now=at(0),
    )


def test_scoring_compares_the_predicted_end_temperature_with_the_measured_one():
    t = tracker()
    t.observe("limit_switch", "off", at(0))
    t.advance(at(0))
    t.observe("temperature", "34.0", at(0))
    block = _predicted(t, [2.0, 2.0])
    predicted_end = block["slots"]["temp_end_c"][0]
    for minute in range(1, 16):
        t.observe("temperature", "36.0", at(60 * minute - 5))
    t.advance(at(900 + 20))
    out = t.score_slot(at(0), 900.0, "p1")
    assert out["measured_temp_end_c"] == 36.0
    assert out["leads"]["final"]["error_c"] == pytest.approx(predicted_end - 36.0, abs=1e-3)
    assert set(out["leads"]) == {"final"}  # no plan is old enough for the 1 h and 3 h leads
    assert t.scores["final"]["n"] == 1
    stats = t.snapshot(at(1000))["scoring"]["temperature"]["final"]
    assert stats["n"] == 1 and stats["mean_abs_error_c"] == pytest.approx(
        abs(predicted_end - 36.0), abs=1e-3
    )


def test_older_plans_are_scored_at_their_own_lead():
    t = tracker()
    t.observe("limit_switch", "off", at(0))
    t.advance(at(0))
    t.observe("temperature", "34.0", at(0))
    _predicted(t, [1.0], plan_id="old", generated=at(-4 * 3600))
    _predicted(t, [1.0], plan_id="mid", generated=at(-2 * 3600))
    _predicted(t, [1.0], plan_id="new", generated=at(-120))
    t.observe("temperature", "35.0", at(850))
    t.advance(at(930))
    out = t.score_slot(at(0), 900.0, "new")
    assert out["leads"]["final"]["plan_id"] == "new"
    assert out["leads"]["1h"]["plan_id"] == "mid"  # the newest plan made at least an hour before
    assert out["leads"]["3h"]["plan_id"] == "old"


def test_the_achievable_charge_is_scored_only_where_a_derate_was_predicted_or_measured():
    t = tracker()
    for role, value in (
        ("limit_switch", "on"),
        ("limit_c", "40"),
        ("band_c", "10"),
        ("floor_pct", "40"),
    ):
        t.observe(role, value, at(0))
    t.advance(at(0))
    t.observe("temperature", "49.0", at(0))
    block = _predicted(t, [2.5])
    assert block["slots"]["charge_kw"][0] < 2.5  # hot: derated
    for minute in range(1, 16):
        t.observe("temperature", "49.0", at(60 * minute - 5))
        t.observe("charge_power", "1100", at(60 * minute - 5))
    t.advance(at(930))
    out = t.score_slot(at(0), 900.0, "p1")["charge"]
    assert out["derate_active"] is True and out["observed_kw"] == pytest.approx(1.1, abs=0.1)
    assert out["error_kw"] == pytest.approx(
        out["predicted_achievable_kw"] - out["observed_kw"], abs=2e-3
    )
    assert t.power_scores["n"] == 1
    # a cool battery: not scored, even if the plan charged
    cool = tracker()
    cool.observe("limit_switch", "off", at(0))
    cool.advance(at(0))
    cool.observe("temperature", "30.0", at(0))
    _predicted(cool, [2.0])
    for minute in range(1, 16):
        cool.observe("charge_power", "2000", at(60 * minute - 5))
    cool.advance(at(930))
    assert cool.score_slot(at(0), 900.0, "p1")["charge"]["derate_active"] is False
    assert cool.power_scores["n"] == 0


def test_a_refit_is_due_once_per_local_day_after_the_configured_hour():
    t = tracker()
    local = datetime(2026, 11, 4, 5, 0, tzinfo=MADRID)  # 04:00 is the default hour
    assert t.refit_due(local, MADRID)  # never attempted
    t.apply_refit(RefitResult("insufficient_data", "x"), None, local)
    assert not t.refit_due(local + timedelta(hours=10), MADRID)
    assert not t.refit_due(datetime(2026, 11, 5, 3, 59, tzinfo=MADRID), MADRID)
    assert t.refit_due(datetime(2026, 11, 5, 4, 0, tzinfo=MADRID), MADRID)
    # a restart that finds the last attempt older than today's slot refits at once
    late = datetime(2026, 11, 7, 9, 0, tzinfo=MADRID)
    assert t.refit_due(late, MADRID)


def test_only_an_accepted_refit_changes_the_parameters():
    t = tracker()
    better = ThermalParams(50.0, 31.0, 9.0, 5.0)
    assert t.apply_refit(RefitResult("rejected", "worse", better), None, at(0)) == "rejected"
    assert t.params == t.prior and t.status == "degraded"
    assert (
        t.apply_refit(RefitResult("insufficient_data", "thin"), None, at(1)) == "insufficient_data"
    )
    assert t.params == t.prior and t.status == "degraded"  # nothing was judged: status unchanged
    assert t.apply_refit(None, "ValueError: boom", at(2)) == "error"
    assert t.params == t.prior and t.last_refit["reason"] == "ValueError: boom"
    assert (
        t.apply_refit(RefitResult("accepted", "ok", better, {"skill": 0.9}), None, at(3))
        == "accepted"
    )
    assert t.params == better and t.status == "fitted" and t.fitted_at == at(3)
    assert t.model_view()["tau_min"] == 50.0


# ---------------------------------------------------------------------------
# persistence and restart
# ---------------------------------------------------------------------------


def inject(service, temp, power, now):
    """Put a synthetic buffer into the tracker's history, ending at *now*."""
    tr = service.thermal
    tr._temps.extend(temp.tolist())
    tr._powers.extend(power.tolist())
    tr._end_minute = int(now.timestamp() // 60)


def test_an_accepted_refit_is_persisted_and_survives_a_restart(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    temp, power = make_buffer(7, TRUTH, noise=0.05, seed=3)
    inject(service, temp, power, NOW - timedelta(minutes=1))
    assert run(service.thermal_refit()) == "accepted"
    tracker_ = service.thermal
    assert tracker_.status == "fitted" and tracker_.params != DEFAULT_PARAMS
    assert tracker_.params.k_c == pytest.approx(9.0, rel=0.2)
    saved = json.loads((state_dir(tmp_path) / "thermal.json").read_text())
    assert saved["status"] == "fitted" and saved["params"] == tracker_.params.to_dict()
    assert (state_dir(tmp_path) / "thermal_samples.json").is_file()
    text = service.metrics.render().decode()
    assert 'battery_optimizer_thermal_refits_total{result="accepted"} 1.0' in text
    assert 'battery_optimizer_thermal_model_status{status="fitted"} 1.0' in text
    assert "battery_optimizer_thermal_last_fit_timestamp_seconds" in text

    clock2 = FakeClock(NOW + timedelta(minutes=10))
    again = make_service(tmp_path, clock2, **SHADOW)
    cleanup(again)
    assert again.thermal.status == "fitted"
    assert again.thermal.params == tracker_.params
    assert again.thermal.fitted_at == tracker_.fitted_at
    temp2, _ = again.thermal.arrays()
    assert temp2.size >= temp.size  # the buffer is back, plus the minutes of the downtime
    assert np.isnan(temp2[temp.size + 1 :]).all()  # unknown while the process was away
    assert again.status()["thermal"]["model"]["status"] == "fitted"


def test_a_rejected_refit_keeps_the_parameters_and_is_recorded(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    rng = np.random.default_rng(5)
    _, power = make_buffer(7, TRUTH)
    temp = 32.0 + np.cumsum(rng.normal(0.0, 0.2, power.size))
    inject(service, temp, power, NOW - timedelta(minutes=1))
    assert run(service.thermal_refit()) == "rejected"
    assert service.thermal.params == DEFAULT_PARAMS and service.thermal.status == "degraded"
    saved = json.loads((state_dir(tmp_path) / "thermal.json").read_text())
    assert saved["status"] == "degraded" and saved["params"] == DEFAULT_PARAMS.to_dict()
    assert saved["last_refit"]["result"] == "rejected"
    again = make_service(tmp_path, FakeClock(NOW), **SHADOW)
    cleanup(again)
    assert again.thermal.status == "degraded" and again.thermal.params == DEFAULT_PARAMS
    text = service.metrics.render().decode()
    assert 'battery_optimizer_thermal_refits_total{result="rejected"} 1.0' in text


def test_a_failing_refit_is_counted_and_changes_nothing(tmp_path, cleanup, monkeypatch):
    service = make_service(tmp_path, FakeClock(NOW), **SHADOW)
    cleanup(service)

    def boom(*_a, **_k):
        raise RuntimeError("fit exploded")

    monkeypatch.setattr(runtime_module, "refit_thermal_model", boom)
    assert run(service.thermal_refit()) == "error"
    assert service.thermal.params == DEFAULT_PARAMS and service.thermal.status == "prior"
    assert service.thermal.last_refit["result"] == "error"
    assert 'refits_total{result="error"} 1.0' in service.metrics.render().decode()


def test_a_corrupt_or_foreign_state_is_ignored_and_the_prior_stays(tmp_path, cleanup):
    directory = state_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "thermal.json").write_text("{not json")
    (directory / "thermal_samples.json").write_text(json.dumps({"version": 99}))
    service = make_service(tmp_path, FakeClock(NOW), **SHADOW)
    cleanup(service)
    assert service.thermal.params == DEFAULT_PARAMS and service.thermal.status == "prior"
    assert (
        "unreadable" in service.thermal.restore_problem
        or "ignored" in service.thermal.restore_problem
    )
    assert service.status()["thermal"]["model"]["restore_problem"] is not None


@pytest.mark.parametrize(
    "state",
    [
        {"version": 2},
        {"version": 1, "status": "bogus", "params": DEFAULT_PARAMS.to_dict()},
        {"version": 1, "status": "fitted", "params": {"tau_min": 1.0}},
        {"version": 1, "status": "fitted", "params": {**DEFAULT_PARAMS.to_dict(), "k_c": 99.0}},
        {"version": 1, "status": "fitted", "params": DEFAULT_PARAMS.to_dict(), "fitted_at": "x"},
        {
            "version": 1,
            "status": "fitted",
            "params": DEFAULT_PARAMS.to_dict(),
            "fitted_at": "2026-01-01T00:00:00",
        },
    ],
)
def test_an_invalid_state_never_replaces_the_parameters(state):
    t = tracker()
    t.restore_state(state)
    assert t.params == t.prior and t.status == "prior" and t.restore_problem


def test_a_state_that_was_never_fitted_follows_the_configured_prior():
    t = ThermalTracker(
        ThermalSettings(mode="shadow", prior=ThermalSettings().prior.__class__(k_c=9.5)),
        ac_power_sign=1,
    )
    t.restore_state({"version": 1, "status": "prior", "params": DEFAULT_PARAMS.to_dict()})
    assert t.params.k_c == 9.5 and t.status == "prior"


def test_samples_round_trip_and_unusable_samples_are_ignored():
    t = tracker()
    t.advance(at(0))
    t.observe("temperature", "33.25", at(0))
    t.observe("charge_power", "1500", at(0))
    t.advance(at(300))
    saved = json.loads(json.dumps(t.samples_state()))
    other = tracker()
    other.restore_samples(saved, at(300))
    assert np.allclose(other.arrays()[0], t.arrays()[0], equal_nan=True)
    assert np.allclose(other.arrays()[1], t.arrays()[1], equal_nan=True)
    for bad in (
        {"version": 1, "step_s": 60, "end_minute": "x", "temp": [], "power_kw": []},
        {"version": 1, "step_s": 60, "end_minute": 5, "temp": [1.0], "power_kw": []},
        {"version": 1, "step_s": 60, "end_minute": 5, "temp": ["a"], "power_kw": [1.0]},
        {"version": 1, "step_s": 30, "end_minute": 5, "temp": [], "power_kw": []},
    ):
        broken = tracker()
        broken.restore_samples(bad, at(0))
        assert broken.arrays()[0].size == 0 and broken.restore_problem
    assert tracker().samples_state() is None


def test_the_thermal_tick_records_saves_and_refits_once(tmp_path, cleanup):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock, **SHADOW)
    cleanup(service)
    run(service.thermal_tick())
    assert service.thermal.last_attempt_at == NOW  # nothing attempted yet: due, too little data
    assert service.thermal.last_refit["result"] == "insufficient_data"
    assert (state_dir(tmp_path) / "thermal.json").is_file()
    clock.advance(120)
    run(service.thermal_tick())
    text = service.metrics.render().decode()
    assert 'battery_optimizer_thermal_refits_total{result="insufficient_data"} 1.0' in text


def test_the_loop_survives_a_failing_tick(tmp_path, cleanup, monkeypatch):
    service = make_service(tmp_path, FakeClock(NOW), **SHADOW)
    cleanup(service)
    calls = []

    async def bad_tick():
        calls.append(1)
        raise RuntimeError("tick failed")

    monkeypatch.setattr(service, "thermal_tick", bad_tick)

    async def go():
        task = asyncio.create_task(service._thermal_loop())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()

    run(go())
    assert calls  # it ran, logged and went on to sleep


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def load(tmp_path, **overrides):
    return load_service_config(write_service_config(tmp_path, **overrides))


def test_the_committed_config_names_the_entities_and_is_off(tmp_path):
    thermal = load(tmp_path).planning.thermal
    assert thermal.mode == "off" and not thermal.active
    e = thermal.entities
    assert (e.temperature, e.limit_switch, e.discharge_switch) == (TEMP, SWITCH, DISCHARGE)
    assert (e.limit_c, e.band_c, e.floor_pct) == (LIMIT, BAND, FLOOR)
    assert (e.system_max_charge_power, e.venus_max_charge_power) == (SYS_CAP, VENUS_CAP)
    assert thermal.prior.k_c == DEFAULT_PARAMS.k_c and thermal.prior.k_d == DEFAULT_PARAMS.k_d
    assert thermal.charge_limit_kw == 2.5 and thermal.driver_min_kw == 0.8
    assert thermal.refit_hour_local == 4
    assert set(e.all()) == set(THERMAL_ENTITIES)


def test_without_the_section_the_feature_is_off_and_valid(tmp_path):
    path = write_service_config(tmp_path, **{"planning.thermal": None})
    thermal = load_service_config(path).planning.thermal
    assert not thermal.active and thermal.prior == ThermalSettings().prior


def test_mode_off_does_not_need_the_entities(tmp_path):
    config = load(tmp_path, **{"planning.thermal.entities": {}, "battery_health": None})
    assert config.planning.thermal.entities.all() == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"planning.thermal.mode": "active"}, "mode must be one of"),
        ({"planning.thermal.mystery": 1}, "unknown planning.thermal keys"),
        ({"planning.thermal.entities.nope": "sensor.x"}, "unknown planning.thermal.entities"),
        ({"planning.thermal.entities.limit_switch": "number.x"}, "must be a switch"),
        ({"planning.thermal.entities.limit_c": "switch.x"}, "must be a number"),
        ({"planning.thermal.entities.temperature": "switch.x"}, "must be a sensor"),
        ({"planning.thermal.entities.limit_c": "number"}, "entity ID or null"),
        ({"planning.thermal.entities.limit_c": 5}, "entity ID or null"),
        ({"planning.thermal.entities.band_c": LIMIT}, "distinct entities"),
        (
            {
                "planning.thermal.mode": "shadow",
                "planning.thermal.entities.temperature": "sensor.other_temp",
            },
            "must name the same",
        ),
        (
            {"planning.thermal.entities.limit_c": "number.omnibattery_pd_target_grid_power"},
            "must not reuse",
        ),
        (
            {"planning.thermal.entities.limit_switch": "switch.venus_battery_allow_charge"},
            "must not reuse",
        ),
        (
            {"planning.thermal.entities.limit_switch": "switch.omnibattery_charge_delay"},
            "must not reuse",
        ),
        ({"planning.thermal.prior": {"k_c": 99}}, "prior.k_c must be within"),
        ({"planning.thermal.prior": {"tau_min": 5}}, "prior.tau_min must be within"),
        ({"planning.thermal.prior": {"k_x": 1}}, "unknown planning.thermal.prior keys"),
        ({"planning.thermal.prior": {"k_c": "hot"}}, "must be a number"),
        ({"planning.thermal.refit_hour_local": 24}, "hour 0 to 23"),
        ({"planning.thermal.refit_hour_local": True}, "hour 0 to 23"),
        ({"planning.thermal.charge_limit_kw": 0}, "charge_limit_kw must be > 0"),
        ({"planning.thermal.driver_min_kw": -1}, "driver_min_kw must be"),
    ],
)
def test_bad_thermal_config_is_refused(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        load(tmp_path, **overrides)


@pytest.mark.parametrize("key", ["temperature", "limit_switch", "limit_c", "band_c", "floor_pct"])
def test_a_shadow_service_needs_the_required_entities(tmp_path, key):
    with pytest.raises(ValueError, match=f"entities.{key}"):
        load(
            tmp_path,
            **{
                "planning.thermal.mode": "shadow",
                f"planning.thermal.entities.{key}": None,
                "battery_health": None,
            },
        )


def test_the_optional_entities_may_be_left_out(tmp_path):
    config = load(
        tmp_path,
        **{
            "planning.thermal.mode": "shadow",
            "planning.thermal.entities.discharge_switch": None,
            "planning.thermal.entities.system_max_charge_power": None,
            "planning.thermal.entities.venus_max_charge_power": None,
        },
    )
    assert config.planning.thermal.active
    assert DISCHARGE not in config.planning.thermal.entities.all()


def test_the_temperature_follows_battery_health_or_must_agree_with_it(tmp_path):
    # either may be omitted ...
    config = load(
        tmp_path,
        **{"planning.thermal.mode": "shadow", "planning.thermal.entities.temperature": None},
    )
    assert config.planning.thermal.entities.temperature == TEMP
    # ... but without battery_health the thermal section has to name it
    with pytest.raises(ValueError, match="entities.temperature"):
        load(
            tmp_path,
            **{
                "planning.thermal.mode": "shadow",
                "planning.thermal.entities.temperature": None,
                "battery_health": None,
            },
        )
    # ... and a standalone temperature is fine when battery_health has none
    config = load(
        tmp_path,
        **{"planning.thermal.mode": "shadow", "battery_health": {"hot_thresholds_c": [35]}},
    )
    assert config.planning.thermal.entities.temperature == TEMP


def test_a_shadow_service_needs_the_ac_power_and_its_sign(tmp_path):
    with pytest.raises(ValueError, match="battery_ac_power_sign"):
        load(
            tmp_path, **{"planning.thermal.mode": "shadow", "actuation.battery_ac_power_sign": None}
        )
    with pytest.raises(ValueError, match="actuation.entities.battery_ac_power"):
        load(
            tmp_path,
            **{"planning.thermal.mode": "shadow", "actuation.entities.battery_ac_power": None},
        )
    # mode off asks for neither
    assert not load(tmp_path, **{"actuation.battery_ac_power_sign": None}).planning.thermal.active


def test_the_cap_entities_may_also_be_observed_context(tmp_path):
    config = load(
        tmp_path,
        **{"planning.thermal.mode": "shadow", "entities.observed": [SYS_CAP, VENUS_CAP]},
    )
    assert config.planning.thermal.active
    service = ShadowService(
        config=service_config(
            tmp_path,
            **{"planning.thermal.mode": "shadow", "entities.observed": [SYS_CAP, VENUS_CAP]},
        ),
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=FakeClock(NOW),
        code_version="t",
        ws_transport=FakeHAServer(),
    )
    assert SYS_CAP in service.store.tracked
    service._thermal_pool.shutdown()
    service._io.shutdown()
    service._executor.shutdown()
    service._history_pool.shutdown()


def test_math_is_finite_everywhere_in_the_block(tmp_path, cleanup):
    service = make_service(tmp_path, FakeClock(NOW), **SHADOW)
    cleanup(service)
    set_thermal(service, NOW, temp=51.0, power=-2500)
    view = service.status()["thermal"]

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, float):
            assert math.isfinite(node)

    walk(view)
    json.dumps(view, allow_nan=False)
