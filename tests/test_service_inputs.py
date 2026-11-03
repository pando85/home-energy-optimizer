"""Entity ordering/freshness, PVPC parsing (incl. DST), slot grid and config."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from service_fakes import (
    REPO,
    TZ,
    entity_state,
    ha_state,
    pvpc_attributes,
    pvpc_state,
    utc,
    write_service_config,
)

from battery_optimizer.service.config import load_service_config
from battery_optimizer.service.entity_store import EntityStore, UpdateResult, parse_ha_state
from battery_optimizer.service.pvpc import PriceParseError, parse_pvpc_state
from battery_optimizer.service.timegrid import (
    floor_slot,
    local_day_slots,
    next_slot_boundary,
)

T0 = utc(2026, 10, 20, 10, 0)


# ---------------------------------------------------------------------------
# Entity store
# ---------------------------------------------------------------------------


def _store():
    return EntityStore.for_entities(["sensor.grid", "sensor.soc"])


def test_store_accepts_newer_and_rejects_out_of_order():
    store = _store()
    assert store.apply(entity_state("sensor.grid", 100, T0)) is UpdateResult.ACCEPTED
    assert store.apply(entity_state("sensor.grid", 200, T0 + timedelta(seconds=5))) is (
        UpdateResult.ACCEPTED
    )
    # A late snapshot (e.g. get_states answered after an event landed) is older.
    assert store.apply(entity_state("sensor.grid", 100, T0)) is UpdateResult.OUT_OF_ORDER
    assert store.get("sensor.grid").state == "200"
    assert store.counts["sensor.grid"] == {"accepted": 2, "out_of_order": 1}


def test_store_duplicate_and_refresh():
    store = _store()
    store.apply(entity_state("sensor.soc", 40, T0))
    assert store.apply(entity_state("sensor.soc", 40, T0)) is UpdateResult.DUPLICATE
    later = T0 + timedelta(minutes=10)
    refreshed = entity_state("sensor.soc", 40, T0, reported=later)
    assert store.apply(refreshed) is UpdateResult.REFRESHED
    assert store.age_s("sensor.soc", later + timedelta(seconds=3)) == pytest.approx(3.0)
    # An older last_reported with the same last_updated is not news.
    assert store.apply(entity_state("sensor.soc", 40, T0, reported=T0 + timedelta(minutes=1))) is (
        UpdateResult.OUT_OF_ORDER
    )


def test_store_freshness_and_unavailable():
    store = _store()
    store.apply(entity_state("sensor.grid", "unavailable", T0))
    assert store.fresh_value("sensor.grid", T0, 60) is None
    store.apply(entity_state("sensor.grid", 321.5, T0 + timedelta(seconds=1)))
    assert store.fresh_value("sensor.grid", T0 + timedelta(seconds=30), 60) == 321.5
    assert store.fresh_value("sensor.grid", T0 + timedelta(seconds=120), 60) is None
    assert store.apply(entity_state("sensor.other", 1, T0)) is UpdateResult.IGNORED


def test_parse_without_last_reported_uses_last_updated():
    payload = ha_state("sensor.grid", 1, T0)
    payload.pop("last_reported")
    state = parse_ha_state(payload)
    assert state.last_reported == T0 == state.observed_at


# ---------------------------------------------------------------------------
# Slot grid and DST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "slots"),
    [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100), (date(2026, 10, 20), 96)],
)
def test_local_day_slot_counts(day, slots):
    starts = local_day_slots(day, TZ)
    assert len(starts) == slots
    assert all(b - a == timedelta(minutes=15) for a, b in zip(starts, starts[1:], strict=False))


def test_slot_flooring_is_utc():
    at = utc(2026, 10, 25, 1, 7, 30)  # 02:07 CET, inside the repeated hour
    assert floor_slot(at) == utc(2026, 10, 25, 1, 0)
    assert next_slot_boundary(at) == utc(2026, 10, 25, 1, 15)
    assert next_slot_boundary(utc(2026, 10, 25, 1, 15)) == utc(2026, 10, 25, 1, 30)
    with pytest.raises(ValueError):
        floor_slot(at.replace(tzinfo=None))


# ---------------------------------------------------------------------------
# PVPC
# ---------------------------------------------------------------------------


def test_pvpc_fall_back_day_maps_repeated_hour():
    day = date(2026, 10, 25)
    attrs = pvpc_attributes(day)
    assert "price_02h_d" in attrs  # the second 02:00 (CET)
    attrs["price_02h"] = 0.5  # 02:00 CEST == 00:00 UTC
    attrs["price_02h_d"] = 0.7  # 02:00 CET  == 01:00 UTC
    state = entity_state("sensor.esios_pvpc", 0.1, utc(2026, 10, 25, 1, 0), attributes=attrs)
    curve = parse_pvpc_state(state, TZ)
    assert curve.expected_hours["today"] == 25 == curve.parsed_hours["today"]
    assert len(curve.prices) == 100
    assert curve.price_at(utc(2026, 10, 25, 0, 45)) == 0.5
    assert curve.price_at(utc(2026, 10, 25, 1, 0)) == 0.7
    assert curve.start == utc(2026, 10, 24, 22, 0)
    assert curve.end == utc(2026, 10, 25, 23, 0)


def test_pvpc_spring_forward_day_has_no_02h():
    day = date(2026, 3, 29)
    attrs = pvpc_attributes(day)
    assert "price_02h" not in attrs
    state = entity_state("sensor.esios_pvpc", 0.1, utc(2026, 3, 29, 8, 0), attributes=attrs)
    curve = parse_pvpc_state(state, TZ)
    assert curve.expected_hours["today"] == 23 == curve.parsed_hours["today"]
    assert len(curve.prices) == 92
    assert curve.contiguous_from(utc(2026, 3, 28, 23, 0)) == list(curve.prices.values())


def test_pvpc_tomorrow_publication_detected():
    day = date(2026, 10, 20)
    without = parse_pvpc_state(pvpc_state(day, utc(2026, 10, 20, 17, 0)), TZ)
    assert not without.tomorrow_published
    assert without.end == utc(2026, 10, 20, 22, 0)
    with_tomorrow = parse_pvpc_state(pvpc_state(day, utc(2026, 10, 20, 19, 0), tomorrow=True), TZ)
    assert with_tomorrow.tomorrow_published
    assert with_tomorrow.end == utc(2026, 10, 21, 22, 0)
    assert len(with_tomorrow.prices) == 192


def test_pvpc_negative_prices_kept_and_bad_values_skipped():
    day = date(2026, 10, 20)
    attrs = pvpc_attributes(day, lambda h: -0.01)
    attrs["price_05h"] = "garbage"
    state = entity_state("sensor.esios_pvpc", 0.1, utc(2026, 10, 20, 8, 0), attributes=attrs)
    curve = parse_pvpc_state(state, TZ)
    assert curve.parsed_hours["today"] == 23
    assert min(curve.prices.values()) == -0.01
    # The hole stops a contiguous run at 05:00 local (03:00 UTC).
    assert len(curve.contiguous_from(utc(2026, 10, 19, 22, 0))) == 5 * 4


def test_pvpc_unavailable_raises():
    state = entity_state("sensor.esios_pvpc", "unavailable", T0)
    with pytest.raises(PriceParseError):
        parse_pvpc_state(state, TZ)


def test_pvpc_matches_live_attribute_shape_of_fall_back_day():
    """The live sensor on 2026-10-25 exposed exactly these 25 keys."""
    keys = [k for k in pvpc_attributes(date(2026, 10, 25)) if k.startswith("price_")]
    expected = [f"price_{h:02d}h" for h in range(24)] + ["price_02h_d"]
    assert sorted(keys) == sorted(expected)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_committed_service_config_loads(monkeypatch):
    monkeypatch.setenv("BATTERY_OPTIMIZER_HA_URL", "https://ha.example/")
    monkeypatch.setenv("BATTERY_OPTIMIZER_HA_TOKEN", "secret-token")
    monkeypatch.setenv("BATTERY_OPTIMIZER_PROMETHEUS_URL", "http://prom.example:9090")
    config = load_service_config(REPO / "config" / "service.yaml")
    assert config.mode == "shadow"
    # The wear cost is the knee of the rolling-horizon sweep (docs/components/battery-model.md);
    # 0.0 would take every marginal cycle.  Change it only with a new sweep.
    assert config.planning.throughput_cost_eur_per_kwh == 0.01
    assert config.home_assistant.url == "https://ha.example"
    assert config.home_assistant.token.reveal() == "secret-token"
    assert config.entities.get("grid_power") == "sensor.shellyem_main_channel_1_power"
    assert config.entities.get("battery_soc") is None
    assert not config.entities.has_battery
    public = config.public_dict()
    assert "secret-token" not in repr(public)
    assert public["home_assistant"]["token_configured"] is True
    # URLs do not change the planning identity.
    monkeypatch.setenv("BATTERY_OPTIMIZER_HA_URL", "https://other.example")
    assert load_service_config(REPO / "config" / "service.yaml").config_hash() == (
        config.config_hash()
    )


def test_token_file(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("from-file\n")
    monkeypatch.delenv("BATTERY_OPTIMIZER_HA_TOKEN", raising=False)
    monkeypatch.setenv("BATTERY_OPTIMIZER_HA_TOKEN_FILE", str(token))
    config = load_service_config(write_service_config(tmp_path))
    assert config.home_assistant.token.reveal() == "from-file"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"mode": "autopilot"}, "is not one of"),
        ({"planning.soc_source": "entity"}, "needs entities.battery_soc"),
        ({"planning.solve_timeout_s": 500}, "solve_timeout_s"),
        ({"entities.bogus": "sensor.x"}, "unknown entity roles"),
        ({"entities.grid_power": None}, "grid_power is required"),
        (
            {"entities.battery_power": "sensor.a", "entities.battery_charge_power": "sensor.b"},
            "not both",
        ),
    ],
)
def test_invalid_config_is_refused(tmp_path, override, message):
    with pytest.raises(ValueError, match=message):
        load_service_config(write_service_config(tmp_path, **override))


def test_load_forecaster_defaults_to_lf0(tmp_path):
    config = load_service_config(write_service_config(tmp_path))
    assert config.planning.load_forecaster == "trailing_mean"
    assert config.public_dict()["planning"]["load_forecaster"] == "trailing_mean"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("perfect", "offline oracle"),
        ("nope", "not registered"),
        (3, "must be one of"),
    ],
)
def test_load_forecaster_rejects_oracles_and_unknown_names(tmp_path, value, message):
    with pytest.raises(ValueError, match=message):
        load_service_config(write_service_config(tmp_path, **{"planning.load_forecaster": value}))
