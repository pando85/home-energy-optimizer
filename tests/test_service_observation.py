"""Slot measurements, Prometheus history/forecast and the virtual battery."""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from urllib.parse import parse_qs

import httpx
import numpy as np
import pytest
from service_fakes import ENTITY_MODE, TARIFF, TZ, entity_state, flat_history, service_config, utc

from battery_optimizer.service.entity_store import UpdateResult
from battery_optimizer.service.history import (
    BASIS_GRID_ONLY,
    BASIS_GRID_PLUS_BATTERY,
    ForecastUnavailableError,
    PrometheusHistory,
    forecast_load,
)
from battery_optimizer.service.observer import SlotObserver, normalise_value
from battery_optimizer.service.planner import battery_spec_from_tariff
from battery_optimizer.service.timegrid import local_day_bounds_utc, local_day_slots
from battery_optimizer.service.virtual_battery import soc_violation, step_virtual_battery

T0 = utc(2026, 10, 20, 10, 0)
END = T0 + timedelta(minutes=15)


CHARGE = "sensor.venus_total_charging_energy"
DISCHARGE = "sensor.venus_total_discharging_energy"


def _observer(tmp_path, synced_from=T0 - timedelta(hours=4)):
    observer = SlotObserver(
        service_config(
            tmp_path,
            **ENTITY_MODE,
            **{
                "entities.grid_energy": "sensor.shellyem_main_channel_1_energy",
            },
        )
    )
    if synced_from is not None:
        observer.mark_synced(synced_from)
    return observer


def _feed(observer, entity, value, at, unit):
    observer.on_state(
        entity_state(entity, value, at, attributes={"unit_of_measurement": unit}),
        UpdateResult.ACCEPTED,
    )


def test_counter_edges_power_average_and_soc(tmp_path):
    obs = _observer(tmp_path)
    energy = "sensor.shellyem_main_channel_1_energy"
    for at, value in [(-30, 100.0), (300, 100.1), (890, 100.25), (910, 100.26)]:
        _feed(obs, energy, value, T0 + timedelta(seconds=at), "kWh")
    for k in range(-60, 450, 90):
        _feed(obs, "sensor.shellyem_main_channel_1_power", 1000, T0 + timedelta(seconds=k), "W")
    for k in range(450, 900, 90):
        _feed(obs, "sensor.shellyem_main_channel_1_power", 2, T0 + timedelta(seconds=k), "kW")
    _feed(obs, "sensor.test_soc", 40, T0 - timedelta(seconds=10), "%")
    _feed(obs, "sensor.test_soc", 42, T0 + timedelta(seconds=800), "%")
    m = obs.measure(T0, END)
    assert m.grid_energy_source == "counter"
    assert m.grid_kwh == pytest.approx(0.25)
    assert m.grid_power_avg_w == pytest.approx(1500.0)
    assert m.grid_power_coverage == pytest.approx(1.0)
    assert (m.soc_start_frac, m.soc_end_frac) == (pytest.approx(0.40), pytest.approx(0.42))


def test_unsynced_counter_edge_falls_back_to_labelled_power_integral(tmp_path):
    obs = _observer(tmp_path, synced_from=T0 + timedelta(seconds=60))  # started mid-slot
    energy = "sensor.shellyem_main_channel_1_energy"
    _feed(obs, energy, 100.0, T0 - timedelta(seconds=600), "kWh")  # not vouched for at T0
    _feed(obs, energy, 100.3, T0 + timedelta(seconds=890), "kWh")
    for k in range(0, 900, 60):
        _feed(obs, "sensor.shellyem_main_channel_1_power", 800, T0 + timedelta(seconds=k), "W")
    m = obs.measure(T0, END)
    assert m.grid_energy_source == "power_integral"
    assert m.grid_kwh == pytest.approx(0.2)


def test_counter_reset_is_unknown_not_negative(tmp_path):
    obs = _observer(tmp_path)
    energy = "sensor.shellyem_main_channel_1_energy"
    _feed(obs, energy, 100.0, T0 - timedelta(seconds=10), "kWh")
    _feed(obs, energy, 0.1, T0 + timedelta(seconds=890), "kWh")
    m = obs.measure(T0, END)
    assert m.grid_kwh is None and m.grid_energy_source is None


def test_unchanged_counters_keep_their_value_at_the_edges(tmp_path):
    """Live discharge at ~260 W: the idle charge counter has not changed for hours."""
    obs = _observer(tmp_path)
    energy = "sensor.shellyem_main_channel_1_energy"
    _feed(obs, energy, 50.0, T0 - timedelta(hours=2), "kWh")  # zero grid import for hours
    _feed(obs, energy, 50.05, T0 + timedelta(seconds=700), "kWh")
    _feed(obs, CHARGE, 3.21, T0 - timedelta(hours=3), "kWh")
    for k in range(-400, 900, 140):  # 0.01 kWh about every 140 s
        _feed(obs, DISCHARGE, 1.0 + 0.01 * ((k + 400) // 140), T0 + timedelta(seconds=k), "kWh")
    m = obs.measure(T0, END)
    assert m.grid_energy_source == "counter" and m.grid_kwh == pytest.approx(0.05)
    assert m.battery_energy_source == "counter"
    assert m.battery_charge_kwh == 0
    assert m.battery_discharge_kwh == pytest.approx(0.07)
    assert m.gross_load_kwh(battery_mapped=True) == pytest.approx(0.12)


def test_edges_need_the_session_in_sync_and_the_entity_available(tmp_path):
    obs = _observer(tmp_path, synced_from=None)
    _feed(obs, CHARGE, 3.0, T0 - timedelta(hours=1), "kWh")
    _feed(obs, DISCHARGE, 1.0, T0 - timedelta(hours=1), "kWh")
    _feed(obs, "sensor.test_soc", 40, T0 - timedelta(hours=1), "%")
    obs.mark_synced(T0 + timedelta(seconds=5))  # e.g. the service started just after T0
    m = obs.measure(T0, END)
    assert (m.battery_charge_kwh, m.battery_discharge_kwh) == (None, None)
    assert (m.soc_start_frac, m.soc_end_frac) == (None, pytest.approx(0.40))

    obs.mark_unsynced(END - timedelta(seconds=30))  # disconnected across the next start
    obs.mark_synced(END + timedelta(seconds=30))
    nxt = obs.measure(END, END + timedelta(minutes=15))
    assert nxt.battery_charge_kwh is None and nxt.soc_start_frac is None
    assert nxt.soc_end_frac == pytest.approx(0.40)

    later = END + timedelta(minutes=15)
    gone = entity_state(DISCHARGE, "unavailable", later - timedelta(minutes=1))
    obs.on_state(gone, UpdateResult.ACCEPTED)
    m = obs.measure(later, later + timedelta(minutes=15))
    assert m.battery_charge_kwh is None and m.battery_discharge_kwh is None
    assert m.soc_start_frac == pytest.approx(0.40)


def test_soc_age_test_still_applies_when_configured(tmp_path):
    obs = SlotObserver(service_config(tmp_path, **ENTITY_MODE, **{"freshness_s.battery_soc": 600}))
    obs.mark_synced(T0 - timedelta(hours=4))
    _feed(obs, "sensor.test_soc", 40, T0 - timedelta(hours=1), "%")
    _feed(obs, "sensor.test_soc", 41, T0 + timedelta(seconds=600), "%")
    m = obs.measure(T0, END)
    assert (m.soc_start_frac, m.soc_end_frac) == (None, pytest.approx(0.41))


def test_a_numeric_counter_freshness_restores_the_edge_age_test(tmp_path):
    config = service_config(tmp_path, **ENTITY_MODE, **{"freshness_s.battery_charge_energy": 900})
    obs = SlotObserver(config)
    obs.mark_synced(T0 - timedelta(hours=4))
    _feed(obs, CHARGE, 3.0, T0 - timedelta(hours=1), "kWh")  # older than 900 s at both edges
    _feed(obs, DISCHARGE, 1.0, T0 - timedelta(hours=1), "kWh")  # default: availability only
    assert obs._counter_delta("battery_discharge_energy", T0, END) == 0
    assert obs._counter_delta("battery_charge_energy", T0, END) is None
    m = obs.measure(T0, END)
    assert (m.battery_charge_kwh, m.battery_discharge_kwh) == (None, None)


def test_bounded_hold_limits_coverage(tmp_path):
    obs = _observer(tmp_path)
    _feed(obs, "sensor.test_battery_power", -500, T0, "W")  # held at most 300 s
    m = obs.measure(T0, END)
    assert m.battery_power_avg_w == pytest.approx(-500)
    assert m.battery_power_coverage == pytest.approx(1 / 3, abs=1e-3)
    assert m.battery_charge_kwh is None  # coverage too low to claim energy
    assert m.gross_load_kwh(battery_mapped=True) is None


def test_battery_flows_reconstruct_gross_load(tmp_path):
    obs = _observer(tmp_path)
    energy = "sensor.shellyem_main_channel_1_energy"
    _feed(obs, energy, 10.0, T0, "kWh")
    _feed(obs, energy, 10.5, END, "kWh")
    for k in range(0, 900, 120):
        _feed(obs, "sensor.test_battery_power", 1200, T0 + timedelta(seconds=k), "W")
    m = obs.measure(T0, END)
    assert m.battery_charge_kwh == pytest.approx(0.3)
    assert m.battery_discharge_kwh == 0
    assert m.gross_load_kwh(battery_mapped=True) == pytest.approx(0.2)
    assert m.gross_load_kwh(battery_mapped=False) == pytest.approx(0.5)


def test_normalise_units():
    s = entity_state("sensor.x", 2.5, T0, attributes={"unit_of_measurement": "kW"})
    assert normalise_value("grid_power", s) == 2500
    s = entity_state("sensor.x", 1500, T0, attributes={"unit_of_measurement": "Wh"})
    assert normalise_value("grid_energy", s) == 1.5
    s = entity_state("sensor.x", 55, T0, attributes={"unit_of_measurement": "%"})
    assert normalise_value("battery_soc", s) == 0.55
    s = entity_state("sensor.x", 55, T0, attributes={"unit_of_measurement": "V"})
    assert normalise_value("battery_soc", s) is None


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


def test_forecast_reproduces_flat_history_and_handles_fall_back_day():
    day = date(2026, 10, 25)
    history = flat_history(day, load_kwh=0.2)
    targets = local_day_slots(day, TZ)
    fc = forecast_load(history, targets, tz=TZ, window_days=28)
    assert len(fc.load_kwh) == 100
    assert np.allclose(fc.load_kwh, 0.2)  # positions 96..99 use the pooled mean
    assert fc.meta["days"][0]["slots_pooled_fallback"] == 4


def test_forecast_records_the_forecaster_and_refuses_the_oracle():
    day = date(2026, 10, 20)
    history = flat_history(day, load_kwh=0.2)
    targets = local_day_slots(day, TZ)[:8]
    fc = forecast_load(history, targets, tz=TZ, window_days=28)
    assert fc.meta["load_forecaster"] == {
        "name": "trailing_mean",
        "params": {"window_days": 28},
        "oracle": False,
    }
    with pytest.raises(ValueError, match="oracle"):
        forecast_load(history, targets, tz=TZ, window_days=28, forecaster="perfect")


def test_forecast_uses_nothing_from_the_decision_day():
    day = date(2026, 10, 20)
    history = flat_history(day)
    with pytest.raises(ForecastUnavailableError):
        forecast_load(
            history,
            [local_day_bounds_utc(day, TZ)[0] - timedelta(minutes=15)],
            tz=TZ,
            window_days=28,
        )


def test_forecast_refuses_without_history():
    day = date(2026, 10, 20)
    history = flat_history(day)
    empty = type(history)(**{**history.__dict__, "usable": np.zeros_like(history.usable)})
    with pytest.raises(ForecastUnavailableError, match="no usable load history"):
        forecast_load(empty, local_day_slots(day, TZ)[:4], tz=TZ, window_days=28)


def test_prometheus_history_from_raw_counter_samples():
    """Raw range-vector samples -> stitched counter -> 15-min gross load (offline primitives)."""
    rate_kwh_per_s = 0.4 / 3600  # 400 W
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        query, at = params["query"][0], float(params["time"][0])
        queries.append(query)
        window = int(re.search(r"\[(\d+)s\]", query).group(1))
        start = at - window
        ts = [t for t in range(int(math.ceil(start / 60)) * 60, int(at) + 1, 60) if t > start]
        if "energy" in query:
            values = [[t, str(1000 + rate_kwh_per_s * (t - 1_790_000_000))] for t in ts]
        else:
            values = [[t, "0.2"] for t in ts]
        metric = {"__name__": "x", "entity": "e", "job": "home-assistant", "instance": "10.0.0.1"}
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "matrix", "result": [{"metric": metric, "values": values}]},
            },
        )

    fetcher = PrometheusHistory(
        base_url="http://prom.test:9090",
        timeout_s=5,
        tz=TZ,
        window_days=2,
        grid_energy_entity="sensor.shellyem_main_channel_1_energy",
        price_entity="sensor.esios_pvpc",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    day = date(2026, 10, 20)
    history = fetcher.fetch(utc(2026, 10, 20, 9, 0), day)
    assert history.cutoff == local_day_bounds_utc(day, TZ)[0]
    assert len(history.starts) == 2 * 96
    assert history.basis == BASIS_GRID_ONLY
    assert history.usable.mean() > 0.95
    assert np.nanmedian(history.load_kwh) == pytest.approx(0.1, rel=1e-3)
    assert history.mean_price_eur_per_kwh == pytest.approx(0.2)
    assert all("[" in q and "query_range" not in q for q in queries)


@pytest.mark.parametrize("commissioned", [True, False])
def test_prometheus_history_uses_the_grid_alone_before_commissioning(commissioned):
    """Counters that only exist from commissioning on must not void the older history."""
    day = date(2026, 10, 20)
    cutoff = local_day_bounds_utc(day, TZ)[0]
    commissioned_at = cutoff - timedelta(hours=12)
    t0 = commissioned_at.timestamp()

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        query, at = params["query"][0], float(params["time"][0])
        window = int(re.search(r"\[(\d+)s\]", query).group(1))
        start = at - window
        ts = [t for t in range(int(math.ceil(start / 60)) * 60, int(at) + 1, 60) if t > start]
        if "shellyem" in query:  # grid import, 400 W
            values = [[t, str(1000 + 0.4 / 3600 * (t - 1_790_000_000))] for t in ts]
        elif "discharging" in query:
            values = [[t, "0.0"] for t in ts if t >= t0]
        elif "charging" in query:  # charge counter from commissioning on, 100 W
            values = [[t, str(0.1 / 3600 * (t - t0))] for t in ts if t >= t0]
        else:
            values = [[t, "0.2"] for t in ts]
        metric = {"__name__": "x", "entity": "e", "job": "home-assistant", "instance": "10.0.0.1"}
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "matrix", "result": [{"metric": metric, "values": values}]},
            },
        )

    fetcher = PrometheusHistory(
        base_url="http://prom.test:9090",
        timeout_s=5,
        tz=TZ,
        window_days=2,
        grid_energy_entity="sensor.shellyem_main_channel_1_energy",
        price_entity="sensor.esios_pvpc",
        battery_charge_energy_entity="sensor.venus_total_charging_energy",
        battery_discharge_energy_entity="sensor.venus_total_discharging_energy",
        battery_commissioned_at=commissioned_at if commissioned else None,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    history = fetcher.fetch(utc(2026, 10, 20, 9, 0), day)
    assert history.basis == BASIS_GRID_PLUS_BATTERY
    step = timedelta(minutes=15)
    before = np.array([s + step <= commissioned_at for s in history.starts])
    # The last slot before the cutoff is partial in this fake (no sample at the cutoff).
    after = np.array([commissioned_at + step <= s < cutoff - step for s in history.starts])
    if commissioned:
        assert history.usable[before].all()
        assert history.load_kwh[before] == pytest.approx(0.1)
    else:
        assert not history.usable[before].any()
    assert history.usable[after].all()
    assert history.load_kwh[after] == pytest.approx(0.075)


def test_serve_passes_the_commissioning_instant_to_the_history(tmp_path, monkeypatch):
    from service_fakes import REPO

    from battery_optimizer.service.serve import build_service

    monkeypatch.setenv("BATTERY_OPTIMIZER_PROMETHEUS_URL", "http://prom.test:9090")
    service, _ = build_service(
        service_config=str(REPO / "config" / "service.yaml"),
        tariff_config=str(REPO / "config" / "tariff.yaml"),
        data_dir=str(tmp_path / "data"),
    )
    fetcher = service._history_fetcher.__self__
    assert fetcher._commissioned_at == utc(2026, 11, 3, 19, 14, 32, 572000)
    assert fetcher._charge == "sensor.venus_total_charging_energy"


@pytest.mark.parametrize("mode", ["shadow", "dry_run"])
def test_serve_logs_the_real_mode_at_startup(tmp_path, monkeypatch, caplog, mode):
    import uvicorn
    from service_fakes import REPO, write_service_config

    from battery_optimizer.service.serve import build_service, run_server

    for var in ("HA_URL", "HA_TOKEN", "HA_TOKEN_FILE", "PROMETHEUS_URL"):
        monkeypatch.delenv(f"BATTERY_OPTIMIZER_{var}", raising=False)

    class NoServer:
        def __init__(self, config):
            pass

        def run(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", NoServer)
    config = write_service_config(tmp_path, mode=mode, **{"actuation.battery_ac_power_sign": -1})
    service, _ = build_service(
        service_config=str(config),
        tariff_config=str(REPO / "config" / "tariff.yaml"),
        data_dir=str(tmp_path / "data"),
    )
    caplog.set_level("INFO", logger="battery_optimizer.service.serve")
    run_server(service, host="127.0.0.1", port=8080)
    (line,) = [r.getMessage() for r in caplog.records if r.name.endswith(".serve")]
    assert line.startswith(f"serving mode={mode} soc_source=entity version=")
    assert f"code_version={service.code_version} on 127.0.0.1:8080" in line
    assert "shadow service" not in line


# ---------------------------------------------------------------------------
# Virtual battery
# ---------------------------------------------------------------------------


def _spec():
    return battery_spec_from_tariff(TARIFF, 0.2)


def test_virtual_battery_clips_discharge_to_measured_load():
    spec, limit, _ = _spec()
    step = step_virtual_battery(
        spec,
        soc_start_frac=0.5,
        mode="discharge",
        target_power_w=2000,
        load_kwh=0.1,
        interval_seconds=900,
        import_limit_kw=limit,
    )
    assert step.discharge_kwh == pytest.approx(0.1)
    assert step.clipped_discharge_kwh == pytest.approx(0.4)
    assert step.grid_kwh == pytest.approx(0.0)
    assert step.soc_end_frac < 0.5


def test_virtual_battery_respects_soc_ceiling_and_unknown_load():
    spec, limit, _ = _spec()
    full = step_virtual_battery(
        spec,
        soc_start_frac=0.95,
        mode="charge",
        target_power_w=2500,
        load_kwh=0.2,
        interval_seconds=900,
        import_limit_kw=limit,
    )
    assert full.charge_kwh == 0 and full.clipped_charge_kwh == pytest.approx(0.625)
    held = step_virtual_battery(
        spec,
        soc_start_frac=0.5,
        mode="charge",
        target_power_w=1000,
        load_kwh=None,
        interval_seconds=900,
        import_limit_kw=limit,
    )
    assert not held.applied and held.soc_end_frac == 0.5 and held.grid_kwh is None


def test_soc_violation_detection():
    spec, _, _ = _spec()
    assert soc_violation(spec, 0.94, 0.625, 0.0)
    assert not soc_violation(spec, 0.5, 0.625, 0.0)
    assert soc_violation(spec, 0.21, 0.0, 0.625)
