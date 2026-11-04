"""Read-only battery health telemetry: the pure tracker, config parsing, /status and metrics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from service_fakes import (
    NOW,
    TARIFF,
    FakeHAServer,
    entity_state,
    feed,
    service_config,
    write_service_config,
)

from battery_optimizer.service.api import create_app
from battery_optimizer.service.battery_health import (
    BALANCE_STATUSES,
    BatteryHealth,
    HealthParams,
    parse_number,
    parse_timestamp,
)
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import load_service_config
from battery_optimizer.service.runtime import ShadowService

T0 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def tracker(**kw) -> BatteryHealth:
    return BatteryHealth(HealthParams(**kw))


# ---------------------------------------------------------------------------
# Parsing: unknown is never zero
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [None, "unknown", "unavailable", "None", "", "  ", "abc", "nan"])
def test_an_unknown_reading_is_none_never_zero(state):
    assert parse_number(state) is None


def test_numbers_and_timestamps_parse():
    assert parse_number("36.5") == 36.5
    assert parse_number("-0") == 0.0
    assert parse_timestamp("2026-11-04T08:00:00+00:00") == T0
    assert parse_timestamp("2026-11-04T09:00:00+01:00") == T0
    assert parse_timestamp("2026-11-04T08:00:00Z") == T0
    for bad in (None, "unknown", "unavailable", "yesterday", "2026-11-04T08:00:00"):
        assert parse_timestamp(bad) is None


def test_every_field_is_none_before_anything_is_observed():
    snap = tracker().snapshot(at(10))
    assert snap["temperature_c"] is None
    assert snap["hot_seconds"] == {"35": 0.0, "40": 0.0}
    assert snap["cell_voltage"] == {"max_v": None, "min_v": None, "spread_mv": None}
    assert snap["balance"] == {
        "cell_delta_mv": None,
        "status": None,
        "delta_trend": None,
        "delta_avg_4w_mv": None,
        "balancing_mode": None,
        "last_read_at": None,
        "last_read_age_s": None,
    }
    assert snap["charge_session"] == {
        "active": False,
        "current": None,
        "last": None,
        "completed": 0,
    }


def test_the_unknown_balance_monitor_reads_unknown_or_none_not_zero():
    h = tracker()
    for role, state in (
        ("cell_delta", "unknown"),
        ("balance_status", "unknown"),
        ("last_balance_read", "unknown"),
        ("delta_trend", "unknown"),
        ("delta_avg_4w", "unavailable"),
        ("balancing_mode", "unavailable"),
    ):
        h.observe(role, state, at(0))
    balance = h.snapshot(at(5))["balance"]
    assert balance["status"] == "unknown"  # seen, and HA says unknown
    assert all(v is None for k, v in balance.items() if k != "status")


# ---------------------------------------------------------------------------
# Hot-time integration
# ---------------------------------------------------------------------------


def test_hot_seconds_hold_the_last_temperature_between_observations():
    h = tracker()
    h.observe("temperature", "30", at(0))
    h.observe("temperature", "36", at(100))  # 100 s at 30: nothing
    h.observe("temperature", "41", at(250))  # 150 s at 36: above 35
    h.observe("temperature", "34", at(400))  # 150 s at 41: above 35 and 40
    snap = h.snapshot(at(500))  # 100 s at 34: nothing
    assert snap["hot_seconds"] == {"35": 300.0, "40": 150.0}
    assert snap["temperature_c"] == 34.0


def test_a_threshold_is_exclusive():
    h = tracker()
    h.observe("temperature", "35", at(0))
    assert h.snapshot(at(100))["hot_seconds"]["35"] == 0.0


def test_a_steady_silent_temperature_is_counted_by_the_periodic_advance():
    h = tracker()
    h.observe("temperature", "36", at(0))
    for i in range(1, 121):  # a 30 s tick for an hour; HA reports nothing
        h.advance(at(30 * i))
    snap = h.snapshot(at(3600))
    assert snap["hot_seconds"]["35"] == 3600.0
    assert snap["uncounted_s"] == 0.0


def test_a_stalled_advance_credits_only_the_gap_cap():
    h = tracker(max_gap_s=300.0)
    h.observe("temperature", "41", at(0))
    snap = h.snapshot(at(3600))  # nobody advanced the clock for an hour
    assert snap["hot_seconds"] == {"35": 300.0, "40": 300.0}
    assert snap["uncounted_s"] == 3300.0


def test_unavailable_temperature_counts_nothing_and_resumes():
    h = tracker()
    h.observe("temperature", "41", at(0))
    h.observe("temperature", "unavailable", at(60))  # 60 s hot
    assert h.temperature_c is None
    h.advance(at(120))  # unknown: nothing
    h.observe("temperature", "37", at(120))
    snap = h.snapshot(at(180))  # 60 s at 37
    assert snap["hot_seconds"] == {"35": 120.0, "40": 60.0}


def test_time_going_backwards_credits_nothing_and_does_not_corrupt_the_clock():
    h = tracker()
    h.observe("temperature", "41", at(100))
    h.observe("temperature", "41", at(50))  # out of order
    assert h.snapshot(at(100))["hot_seconds"]["35"] == 0.0
    assert h.snapshot(at(160))["hot_seconds"]["35"] == 60.0


def test_the_first_observation_does_not_count_the_time_before_it():
    h = tracker()
    h.observe("temperature", "41", at(5000))
    assert h.snapshot(at(5000))["hot_seconds"] == {"35": 0.0, "40": 0.0}


def test_custom_thresholds_label_the_counters():
    h = tracker(hot_thresholds_c=(30.0, 37.5))
    h.observe("temperature", "38", at(0))
    assert h.snapshot(at(60))["hot_seconds"] == {"30": 60.0, "37.5": 60.0}


# ---------------------------------------------------------------------------
# Charge sessions (sign: charge = positive after the sign)
# ---------------------------------------------------------------------------


def test_a_session_records_peak_temperature_and_duration():
    h = tracker(charge_power_sign=-1, session_end_hold_s=0)
    h.observe("temperature", "30", at(0))
    h.observe("charge_power", "-800", at(10))  # negative = charging with sign -1
    h.observe("temperature", "33", at(100))
    h.observe("temperature", "38", at(200))
    h.observe("temperature", "36", at(300))
    h.observe("charge_power", "0", at(400))
    snap = h.snapshot(at(500))["charge_session"]
    assert snap["active"] is False and snap["current"] is None and snap["completed"] == 1
    assert snap["last"]["peak_temperature_c"] == 38.0
    assert snap["last"]["duration_s"] == 390.0
    assert snap["last"]["end_reason"] == "below_threshold"
    assert snap["last"]["started_at"] == at(10).isoformat()


def test_the_peak_includes_the_temperature_held_when_the_session_starts():
    h = tracker(session_end_hold_s=0)
    h.observe("temperature", "39", at(0))
    h.observe("charge_power", "500", at(10))
    h.observe("temperature", "31", at(20))
    h.observe("charge_power", "0", at(30))
    assert h.snapshot(at(40))["charge_session"]["last"]["peak_temperature_c"] == 39.0


def test_the_wrong_sign_is_not_charging():
    h = tracker(charge_power_sign=-1)
    h.observe("charge_power", "797", at(0))  # discharging with sign -1
    assert h.snapshot(at(10))["charge_session"]["active"] is False


def test_power_at_the_threshold_is_not_charging():
    h = tracker(charge_threshold_w=100.0)
    h.observe("charge_power", "100", at(0))
    assert h.snapshot(at(1))["charge_session"]["active"] is False
    h.observe("charge_power", "101", at(2))
    assert h.snapshot(at(3))["charge_session"]["active"] is True


def test_a_running_session_reports_its_current_duration_and_peak():
    h = tracker()
    h.observe("temperature", "32", at(0))
    h.observe("charge_power", "1500", at(10))
    h.observe("temperature", "34", at(70))
    snap = h.snapshot(at(130))["charge_session"]
    assert snap["active"] is True and snap["completed"] == 0
    assert snap["current"]["duration_s"] == 120.0
    assert snap["current"]["peak_temperature_c"] == 34.0
    assert snap["current"]["ended_at"] is None and snap["last"] is None


def test_the_taper_below_the_threshold_is_one_session_within_the_hold():
    h = tracker(session_end_hold_s=300.0)
    h.observe("charge_power", "2000", at(0))
    h.observe("charge_power", "50", at(1000))  # taper
    h.observe("charge_power", "400", at(1100))  # back above within the hold: same session
    h.observe("charge_power", "20", at(1500))
    mid = h.snapshot(at(1700))["charge_session"]  # 200 s below: still running
    assert mid["active"] is True and mid["completed"] == 0
    done = h.snapshot(at(1900))["charge_session"]  # 400 s below: over, dated to 1500
    assert done["active"] is False and done["completed"] == 1
    assert done["last"]["ended_at"] == at(1500).isoformat()
    assert done["last"]["duration_s"] == 1500.0


def test_a_new_session_starts_after_the_hold_expired():
    h = tracker(session_end_hold_s=60.0)
    h.observe("charge_power", "900", at(0))
    h.observe("charge_power", "0", at(100))
    h.observe("charge_power", "900", at(500))  # the clock advance closes the first one
    snap = h.snapshot(at(600))["charge_session"]
    assert snap["completed"] == 1 and snap["active"] is True
    assert snap["last"]["duration_s"] == 100.0
    assert snap["current"]["started_at"] == at(500).isoformat()


def test_an_unavailable_power_reading_ends_the_session_at_once():
    h = tracker(session_end_hold_s=300.0)
    h.observe("charge_power", "900", at(0))
    h.observe("charge_power", "unavailable", at(40))
    snap = h.snapshot(at(41))["charge_session"]
    assert snap["active"] is False and snap["last"]["end_reason"] == "unavailable"
    assert snap["last"]["duration_s"] == 40.0


def test_an_unavailable_power_reading_never_starts_a_session():
    h = tracker()
    h.observe("charge_power", "unavailable", at(0))
    assert h.snapshot(at(1))["charge_session"]["active"] is False


def test_a_silent_constant_charge_stays_one_session():
    h = tracker()
    h.observe("charge_power", "800", at(0))
    for i in range(1, 100):  # the sensor reports nothing while the power is constant
        h.advance(at(30 * i))
    assert h.snapshot(at(3000))["charge_session"]["active"] is True


def test_sessions_are_kept_newest_last_and_bounded():
    h = tracker(session_end_hold_s=0)
    for i in range(25):
        h.observe("charge_power", "900", at(i * 100))
        h.observe("charge_power", "0", at(i * 100 + 50))
    snap = h.snapshot(at(5000))["charge_session"]
    assert snap["completed"] == 25 and len(h.sessions) == 20
    assert snap["last"]["started_at"] == at(2400).isoformat()


# ---------------------------------------------------------------------------
# Cells and the balance monitor
# ---------------------------------------------------------------------------


def test_cell_voltages_and_spread():
    h = tracker()
    h.observe("cell_voltage_max", "3.352", at(0))
    assert h.snapshot(at(1))["cell_voltage"]["spread_mv"] is None  # min unknown
    h.observe("cell_voltage_min", "3.341", at(0))
    cells = h.snapshot(at(1))["cell_voltage"]
    assert cells == {"max_v": 3.352, "min_v": 3.341, "spread_mv": pytest.approx(11.0)}
    h.observe("cell_voltage_min", "unavailable", at(2))
    cells = h.snapshot(at(3))["cell_voltage"]
    assert cells["min_v"] is None and cells["spread_mv"] is None and cells["max_v"] == 3.352


def test_an_inconsistent_pair_has_no_spread():
    h = tracker()
    h.observe("cell_voltage_max", "3.30", at(0))
    h.observe("cell_voltage_min", "3.31", at(0))  # reads were not simultaneous
    assert h.snapshot(at(1))["cell_voltage"]["spread_mv"] is None


def test_the_balance_monitor_fields_and_staleness():
    h = tracker()
    h.observe("cell_delta", "18", at(0))
    h.observe("balance_status", "Yellow", at(0))
    h.observe("last_balance_read", (T0 - timedelta(days=3)).isoformat(), at(0))
    h.observe("delta_trend", "improving", at(0))
    h.observe("delta_avg_4w", "21.5", at(0))
    h.observe("balancing_mode", "off", at(0))
    balance = h.snapshot(at(600))["balance"]
    assert balance["cell_delta_mv"] == 18.0
    assert balance["status"] == "yellow"
    assert balance["delta_trend"] == "improving"
    assert balance["delta_avg_4w_mv"] == 21.5
    assert balance["balancing_mode"] is False
    assert balance["last_read_at"] == (T0 - timedelta(days=3)).isoformat()
    assert balance["last_read_age_s"] == 3 * 86400 + 600


def test_the_last_read_age_grows_and_never_goes_negative():
    h = tracker()
    h.observe("last_balance_read", (T0 + timedelta(seconds=30)).isoformat(), at(0))
    assert h.snapshot(at(0))["balance"]["last_read_age_s"] == 0.0
    assert h.snapshot(at(90))["balance"]["last_read_age_s"] == 60.0


@pytest.mark.parametrize("status", BALANCE_STATUSES)
def test_every_listed_balance_status_is_kept(status):
    h = tracker()
    h.observe("balance_status", status.upper(), at(0))
    assert h.snapshot(at(1))["balance"]["status"] == status


def test_an_unexpected_balance_status_reads_unknown():
    h = tracker()
    h.observe("balance_status", "purple", at(0))
    assert h.snapshot(at(1))["balance"]["status"] == "unknown"


def test_a_balancing_flag_that_is_neither_on_nor_off_is_none():
    h = tracker()
    h.observe("balancing_mode", "on", at(0))
    assert h.snapshot(at(1))["balance"]["balancing_mode"] is True
    h.observe("balancing_mode", "unavailable", at(2))
    assert h.snapshot(at(3))["balance"]["balancing_mode"] is None


def test_an_unknown_role_is_refused():
    with pytest.raises(KeyError):
        tracker().observe("soc", "50", at(0))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENTITY_KEYS = (
    "temperature",
    "cell_voltage_max",
    "cell_voltage_min",
    "cell_delta",
    "balance_status",
    "last_balance_read",
    "delta_trend",
    "delta_avg_4w",
    "balancing_mode",
)


def test_the_committed_config_names_the_health_sensors_and_defaults_hold(tmp_path):
    health = load_service_config(write_service_config(tmp_path)).battery_health
    assert health.configured
    assert health.entities.temperature == "sensor.venus_internal_temperature"
    assert health.entities.balancing_mode == "binary_sensor.venus_balancing_mode"
    assert health.hot_thresholds_c == (35.0, 40.0)
    assert health.charge_threshold_w == 100.0


def test_everything_is_optional_and_defaults_to_null(tmp_path):
    config = load_service_config(write_service_config(tmp_path, battery_health=None))
    health = config.battery_health
    assert not health.configured
    assert all(getattr(health.entities, k) is None for k in ENTITY_KEYS)
    assert health.entities.by_entity() == {}
    assert config.public_dict()["battery_health"]["entities"]["temperature"] is None


def test_a_partial_set_of_roles_is_valid(tmp_path):
    config = load_service_config(
        write_service_config(
            tmp_path,
            battery_health={"entities": {"temperature": "sensor.t"}},
        )
    )
    assert config.battery_health.entities.all() == ("sensor.t",)
    assert config.battery_health.entities.by_entity() == {"sensor.t": "temperature"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"mystery": 1}, "unknown battery_health keys"),
        ({"entities": {"nope": "sensor.x"}}, "unknown battery_health.entities keys"),
        ({"entities": {"temperature": "switch.x"}}, "must be a sensor.* entity ID"),
        ({"entities": {"balancing_mode": "sensor.x"}}, "must be a binary_sensor.*"),
        ({"entities": {"temperature": 5}}, "must be a sensor.* entity ID"),
        ({"entities": {"temperature": "sensor.x", "cell_delta": "sensor.x"}}, "distinct"),
        ({"hot_thresholds_c": []}, "list of numbers"),
        ({"hot_thresholds_c": "35"}, "list of numbers"),
        ({"hot_thresholds_c": [True]}, "list of numbers"),
        ({"hot_thresholds_c": [40, 35]}, "strictly increasing"),
        ({"hot_thresholds_c": [35, 35]}, "strictly increasing"),
        ({"hot_thresholds_c": [0, 35]}, "strictly increasing"),
        ({"charge_threshold_w": 0}, "charge_threshold_w"),
        ({"max_gap_s": -1}, "max_gap_s"),
        ({"session_end_hold_s": -1}, "session_end_hold_s"),
    ],
)
def test_bad_battery_health_config_is_refused(tmp_path, overrides, message):
    path = write_service_config(tmp_path, battery_health=overrides)
    with pytest.raises(ValueError, match=message):
        load_service_config(path)


def test_zero_session_hold_is_allowed(tmp_path):
    cfg = load_service_config(
        write_service_config(tmp_path, battery_health={"session_end_hold_s": 0})
    )
    assert cfg.battery_health.session_end_hold_s == 0.0


def test_the_health_block_does_not_change_the_config_hash(tmp_path):
    base = load_service_config(write_service_config(tmp_path, battery_health=None))
    other = load_service_config(write_service_config(tmp_path))
    assert base.config_hash() == other.config_hash()


def test_the_health_entities_may_overlap_the_full_charge_evidence(tmp_path):
    config = load_service_config(write_service_config(tmp_path))
    fc = config.full_charge.entities
    assert fc.cell_delta == config.battery_health.entities.cell_delta
    assert fc.last_balance_read == config.battery_health.entities.last_balance_read


# ---------------------------------------------------------------------------
# Runtime: subscription, /status and metrics
# ---------------------------------------------------------------------------

TEMP = "sensor.venus_internal_temperature"
POWER = "sensor.venus_ac_power"


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
    return service


def apply(service, entity, state, when):
    service.store.apply(entity_state(entity, state, when))


def test_the_health_entities_and_the_ac_power_are_tracked_in_shadow_mode(tmp_path):
    service = make_service(tmp_path, FakeClock(NOW))
    assert service.config.mode == "shadow"
    cfg = service.config.battery_health.entities
    for entity in (*cfg.all(), POWER):
        assert entity in service.store.tracked
    assert service.health is not None


def test_without_health_entities_nothing_is_tracked_and_the_block_is_absent(tmp_path):
    service = make_service(tmp_path, FakeClock(NOW), battery_health=None)
    assert service.health is None
    assert TEMP not in service.store.tracked
    assert "battery_health" not in service.status()
    assert "battery_health" not in service.metric_snapshot()
    assert "battery_optimizer_battery_" not in service.metrics.render().decode()


def test_without_the_ac_power_sign_sessions_are_not_tracked(tmp_path):
    service = make_service(tmp_path, FakeClock(NOW), **{"actuation.battery_ac_power_sign": None})
    assert POWER not in service.store.tracked
    assert service.status()["battery_health"]["charge_sessions_tracked"] is False


def test_status_reports_the_block_from_observed_states(tmp_path):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    feed(service, NOW)
    apply(service, TEMP, "36.5", NOW)
    apply(service, "sensor.venus_max_cell_voltage", "3.35", NOW)
    apply(service, "sensor.venus_min_cell_voltage", "3.34", NOW)
    apply(service, "sensor.venus_cell_delta", "unknown", NOW)
    apply(service, "sensor.venus_balance_status", "unknown", NOW)
    clock.advance(120)
    block = service.status()["battery_health"]
    assert block["actuation"].startswith("none")
    assert block["charge_sessions_tracked"] is True
    assert block["temperature_c"] == 36.5
    assert block["hot_seconds"] == {"35": 120.0, "40": 0.0}
    assert block["cell_voltage"]["spread_mv"] == pytest.approx(10.0)
    assert block["balance"]["cell_delta_mv"] is None
    assert block["balance"]["status"] == "unknown"
    assert block["balance"]["last_read_at"] is None


def test_a_charge_session_comes_from_the_actuation_power_entity_and_sign(tmp_path):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    assert service.config.actuation.battery_ac_power_sign == -1
    apply(service, TEMP, "31", NOW)
    clock.advance(10)
    apply(service, POWER, "-800", clock.now())  # charging, committed sign convention
    clock.advance(60)
    apply(service, TEMP, "37", clock.now())
    session = service.status()["battery_health"]["charge_session"]
    assert session["active"] is True
    assert session["current"]["peak_temperature_c"] == 37.0


def test_a_positive_power_is_discharging_with_the_committed_sign(tmp_path):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    apply(service, POWER, "797", NOW)
    assert service.status()["battery_health"]["charge_session"]["active"] is False


def test_the_health_tick_advances_the_clock_without_any_event(tmp_path):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    apply(service, TEMP, "41", NOW)

    async def go():
        task = asyncio.create_task(service._health_loop())
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(go())
    assert service.health.hot_seconds[40.0] > 0.0


def metric_lines(service) -> list[str]:
    return [
        line
        for line in service.metrics.render().decode().splitlines()
        if line.startswith("battery_optimizer_battery_")
    ]


def test_metrics_for_known_readings(tmp_path):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    apply(service, TEMP, "41", NOW)
    apply(service, "sensor.venus_max_cell_voltage", "3.35", NOW)
    apply(service, "sensor.venus_min_cell_voltage", "3.34", NOW)
    apply(service, "sensor.venus_cell_delta", "14", NOW)
    apply(service, "sensor.venus_delta_avg_4w", "16.5", NOW)
    apply(service, "sensor.venus_balance_status", "Green", NOW)
    apply(service, "binary_sensor.venus_balancing_mode", "off", NOW)
    apply(service, "sensor.venus_last_balance_read", (NOW - timedelta(hours=1)).isoformat(), NOW)
    apply(service, POWER, "-900", NOW)
    clock.advance(100)
    apply(service, TEMP, "43", clock.now())
    text = "\n".join(metric_lines(service))
    assert "battery_optimizer_battery_temperature_celsius 43.0" in text
    assert 'battery_optimizer_battery_hot_seconds_total{threshold_celsius="35"} 100.0' in text
    assert 'battery_optimizer_battery_hot_seconds_total{threshold_celsius="40"} 100.0' in text
    assert "battery_optimizer_battery_charge_session_active 1.0" in text
    assert "battery_optimizer_battery_charge_session_peak_temperature_celsius 43.0" in text
    assert "battery_optimizer_battery_charge_session_duration_seconds 100.0" in text
    assert "battery_optimizer_battery_charge_sessions_total 0.0" in text
    assert "battery_optimizer_battery_cell_voltage_max_volts 3.35" in text
    assert "battery_optimizer_battery_cell_voltage_min_volts 3.34" in text
    assert "battery_optimizer_battery_cell_voltage_spread_millivolts 10.0" in text
    assert "battery_optimizer_battery_cell_delta_millivolts 14.0" in text
    assert "battery_optimizer_battery_cell_delta_avg_4w_millivolts 16.5" in text
    assert 'battery_optimizer_battery_balance_status{status="green"} 1.0' in text
    for other in ("yellow", "orange", "red", "unknown"):
        assert f'battery_optimizer_battery_balance_status{{status="{other}"}} 0.0' in text
    assert "battery_optimizer_battery_balancing_mode 0.0" in text
    stamp = next(
        line for line in metric_lines(service) if "last_balance_read_timestamp_seconds " in line
    )
    assert float(stamp.split()[-1]) == (NOW - timedelta(hours=1)).timestamp()
    assert "battery_optimizer_battery_last_balance_read_age_seconds 3700.0" in text


def test_unknown_readings_are_absent_samples_never_zeros(tmp_path):
    service = make_service(tmp_path, FakeClock(NOW))
    for entity in service.config.battery_health.entities.all():
        apply(service, entity, "unknown", NOW)
    text = service.metrics.render().decode()
    for name in (
        "temperature_celsius",
        "cell_voltage_max_volts",
        "cell_voltage_min_volts",
        "cell_voltage_spread_millivolts",
        "cell_delta_millivolts",
        "cell_delta_avg_4w_millivolts",
        "balancing_mode",
        "last_balance_read_timestamp_seconds",
        "last_balance_read_age_seconds",
        "charge_session_peak_temperature_celsius",
        "charge_session_duration_seconds",
    ):
        assert f"# TYPE battery_optimizer_battery_{name} gauge" in text
        assert f"\nbattery_optimizer_battery_{name} " not in f"\n{text}"
    # counters and the activity flag are real zeros: nothing was seen hot or charging
    assert 'battery_optimizer_battery_hot_seconds_total{threshold_celsius="35"} 0.0' in text
    assert "battery_optimizer_battery_charge_session_active 0.0" in text
    # an unknown status is the `unknown` state of the enum
    assert 'battery_optimizer_battery_balance_status{status="unknown"} 1.0' in text
    assert 'battery_optimizer_battery_balance_status{status="green"} 0.0' in text


def test_a_status_never_seen_has_no_enum_samples(tmp_path):
    service = make_service(tmp_path, FakeClock(NOW))
    text = "\n".join(metric_lines(service))
    assert "battery_optimizer_battery_balance_status{" not in text


def test_the_status_endpoint_carries_the_block(tmp_path):
    clock = FakeClock(NOW)
    service = make_service(tmp_path, clock)
    apply(service, TEMP, "36", NOW)
    app = create_app(service, manage_lifecycle=False)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bo.test") as client:
            status = (await client.get("/status")).json()
            metrics = (await client.get("/metrics")).text
        return status, metrics

    status, metrics = asyncio.run(go())
    assert set(status["battery_health"]) == {
        "actuation",
        "charge_sessions_tracked",
        "temperature_c",
        "hot_seconds",
        "uncounted_s",
        "charge_session",
        "cell_voltage",
        "balance",
    }
    assert status["battery_health"]["temperature_c"] == 36.0
    assert "battery_optimizer_battery_temperature_celsius 36.0" in metrics
