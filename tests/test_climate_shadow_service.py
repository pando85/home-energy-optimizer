"""The climate shadow inside the resident service: config, read-only guard, metrics, status."""

from __future__ import annotations

import ast
import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from climate_shadow_fakes import CCFG, MPC, SETTINGS, SITE, FakeSamples, forecast_record, prior
from service_fakes import (
    TARIFF,
    FakeHAServer,
    entity_state,
    ha_state,
    service_config,
    wait_until,
    write_service_config,
)

from battery_optimizer._env import legacy_name
from battery_optimizer.climate import shadow
from battery_optimizer.climate.data import ClimateDataError
from battery_optimizer.service.climate_shadow import ClimateShadowJob, build_job
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import load_service_config
from battery_optimizer.service.ha_guard import check_ws_message
from battery_optimizer.service.runtime import ShadowService
from battery_optimizer.service.sink import ShadowSink, iter_records

REPO = Path(__file__).resolve().parents[1]
MIDNIGHT = shadow.datetime(2026, 11, 15, 23, tzinfo=shadow.UTC)  # local Monday 00:00
RUN_AT = MIDNIGHT + timedelta(minutes=10)
REGIME = "input_boolean.comfort_control_enable"
DHW_PLAN = "input_text.comfort_dhw_plan"
ENABLED = {
    "climate_shadow.enabled": True,
    "climate_shadow.prior": "abcdef",
    "archive.weather_forecast.enabled": True,
}


def make_service(tmp_path, server, *, factory=None, sync_timeout=0.05, **overrides):
    holder: list[ShadowService] = []

    def default_factory():
        service = holder[0]
        return ClimateShadowJob(
            settings=service.config.climate_shadow,
            climate_config=CCFG,
            mpc_config=MPC,
            prior=prior(),
            site=SITE,
            sink=service.sink,
            data_dir=service.data_dir,
            samples=FakeSamples(MIDNIGHT - timedelta(days=10), RUN_AT),
            code_version="test-sha",
            plan_settings=SETTINGS,
            metrics=service.metrics,
        )

    service = ShadowService(
        config=service_config(tmp_path, **{**ENABLED, **overrides}),
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=FakeClock(RUN_AT),
        code_version="test-sha",
        ws_transport=server,
        climate_job_factory=factory or default_factory,
        climate_sync_timeout_s=sync_timeout,
    )
    holder.append(service)
    return service


def shutdown(service):
    service._executor.shutdown(wait=True)
    service._climate_pool.shutdown(wait=True)
    service._io.shutdown(wait=True)
    service._history_pool.shutdown(wait=True)


async def run_synced(service, *steps):
    task = asyncio.create_task(service.ha.run())
    await wait_until(lambda: service.ha.stats.synced)
    out = [await step() for step in steps]
    await service.ha.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return out


def metric(service, name, **labels):
    return service.metrics.registry.get_sample_value(f"battery_optimizer_{name}", labels)


def test_the_job_runs_read_only_with_the_comfort_regime_and_the_dhw_plan(tmp_path):
    states = [
        ha_state(REGIME, "on", MIDNIGHT),
        ha_state(
            DHW_PLAN,
            '{"day":"2026-11-16","start":"2026-11-16T02:00:00+01:00",'
            '"end":"2026-11-16T03:00:00+01:00","forced":true,"target_written":true,"done":null}',
            MIDNIGHT,
        ),
    ]
    server = FakeHAServer(states)
    service = make_service(tmp_path, server)
    fc_at = MIDNIGHT - timedelta(minutes=55)  # the fetch of the hour before t0
    service.sink.append_weather_forecast(fc_at, forecast_record(fc_at))
    try:
        _ok, summary = asyncio.run(
            run_synced(service, service.archive_weather_forecast, service.run_climate_shadow)
        )
    finally:
        shutdown(service)
    assert summary["status"] == "planned" and summary["regime"] == "comfort"
    root = tmp_path / "data"
    (dec,) = iter_records(root, "climate_decisions", MIDNIGHT, RUN_AT + timedelta(hours=1))
    assert dec["regime"] == "comfort" and dec["regime_state"] == "on"
    assert dec["inputs"]["dhw_source"] == "plan" and dec["inputs"]["dhw_slots"] == [8, 9, 10, 11]
    assert dec["inputs"]["weather"] == "forecast"
    # the newest forecast issued by t0 (23:00): the archive's own fetch at the run is later
    assert dec["inputs"]["forecast_requested_at"] == fc_at.isoformat()
    # every byte sent to HA passed the read-only guard; the only service call is the forecast
    # archive's own weather.get_forecasts query, none from the climate job
    for message in server.all_sent:
        check_ws_message(message)
    calls = [m for m in server.all_sent if m.get("type") == "call_service"]
    assert len(calls) == 1 and calls[0]["domain"] == "weather"
    assert server.service_calls == calls
    assert metric(service, "climate_shadow_runs_total", result="planned") == 1.0
    assert metric(service, "climate_shadow_regime", regime="comfort") == 1.0
    assert metric(service, "climate_shadow_up") == 1.0
    assert metric(service, "climate_shadow_refits_total", result="refitted") == 1.0
    solves = sum(
        metric(service, "climate_shadow_solves_total", status=s) or 0.0
        for s in ("optimal", "gap", "time_limit", "infeasible", "error")
    )
    assert solves >= 1 and metric(service, "climate_shadow_solve_duration_seconds_count") == solves
    gate = metric(
        service, "climate_shadow_gate", window="legacy", criterion="verdict", status="UNDETERMINED"
    )
    assert gate == 1.0
    status = service.status()["climate_shadow"]
    assert status["enabled"] and status["verdicts"] == {
        "legacy": "UNDETERMINED",
        "new": "UNDETERMINED",
    }
    assert status["actuation"].startswith("none")


def test_up_is_zero_until_the_job_is_loaded(tmp_path):
    service = make_service(tmp_path, FakeHAServer())
    try:
        assert service.climate is None and service.climate_error is None
        assert metric(service, "climate_shadow_up") == 0.0
        assert service.status()["climate_shadow"]["loaded"] is False
    finally:
        shutdown(service)


@pytest.mark.parametrize(("minutes", "immediately"), [(25, True), (10, True), (5, False)])
def test_on_start_the_current_boundary_is_caught_up_once_its_offset_passed(
    tmp_path, minutes, immediately
):
    service = make_service(tmp_path, FakeHAServer())
    service.clock.set(MIDNIGHT + timedelta(minutes=minutes))
    runs = []

    async def fake_run():
        runs.append(service.clock.now())
        raise asyncio.CancelledError

    service.run_climate_shadow = fake_run

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await service._climate_loop()

    try:
        asyncio.run(go())
    finally:
        shutdown(service)
    expected = MIDNIGHT + timedelta(minutes=minutes if immediately else 10)
    assert runs == [expected]


async def _cancel(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_the_catch_up_run_waits_for_the_ha_sync_and_records_legacy(tmp_path):
    service = make_service(tmp_path, FakeHAServer(), sync_timeout=30.0)
    entered = []
    run = service.run_climate_shadow

    async def spy():
        entered.append(service.ha.stats.synced)
        return await run()

    service.run_climate_shadow = spy

    async def go():
        loop_task = asyncio.create_task(service._climate_loop())
        for _ in range(20):  # the loop is at the catch-up run but HA has not synced
            await asyncio.sleep(0)
        assert entered == [] and service.climate is None and not service.ha.stats.synced
        ha_task = asyncio.create_task(service.ha.run())

        def records():
            end = RUN_AT + timedelta(hours=1)
            return list(iter_records(tmp_path / "data", "climate_decisions", MIDNIGHT, end))

        await wait_until(lambda: records(), timeout=10.0)
        await service.ha.stop()
        await _cancel(loop_task, ha_task)
        return records()

    try:
        dec = asyncio.run(go())[0]
    finally:
        shutdown(service)
    assert entered[0] is True and dec["regime"] == "legacy"


def test_the_catch_up_run_does_not_wait_without_ha(tmp_path):
    service = make_service(tmp_path, FakeHAServer(), sync_timeout=30.0)
    service.ha = None  # not configured: nothing to wait for
    regimes = []

    async def fake_run():
        regimes.append(service.climate_inputs(service.clock.now()).regime)
        raise asyncio.CancelledError

    service.run_climate_shadow = fake_run

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(service._climate_loop(), timeout=2.0)

    try:
        asyncio.run(go())
    finally:
        shutdown(service)
    assert regimes == ["unknown"]


def test_the_catch_up_run_goes_ahead_after_the_timeout_and_records_unknown(tmp_path, caplog):
    service = make_service(tmp_path, FakeHAServer(), sync_timeout=0.02)  # HA is never started
    regimes = []

    async def fake_run():
        regimes.append(service.climate_inputs(service.clock.now()).regime)
        raise asyncio.CancelledError

    service.run_climate_shadow = fake_run

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await service._climate_loop()

    try:
        with caplog.at_level("WARNING"):
            asyncio.run(go())
    finally:
        shutdown(service)
    assert regimes == ["unknown"]
    assert "Home Assistant not synced" in caplog.text


def test_the_catch_up_run_does_not_wait_when_ha_is_already_synced(tmp_path):
    service = make_service(tmp_path, FakeHAServer(), sync_timeout=30.0)
    service.ha.stats.synced = True
    regimes = []

    async def fake_run():
        regimes.append(service.climate_inputs(service.clock.now()).regime)
        raise asyncio.CancelledError

    service.run_climate_shadow = fake_run

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(service._climate_loop(), timeout=2.0)

    try:
        asyncio.run(go())
    finally:
        shutdown(service)
    assert regimes == ["legacy"]


def test_regime_inputs_from_the_store(tmp_path):
    service = make_service(tmp_path, FakeHAServer())
    try:
        assert service.climate_inputs(RUN_AT).regime == "unknown"  # HA never synced
        service.ha.stats.synced = True
        inputs = service.climate_inputs(RUN_AT)
        assert (inputs.regime, inputs.dhw_plan, inputs.away) == ("legacy", None, None)
        service.store.apply(entity_state(REGIME, "off", MIDNIGHT))
        service.store.apply(entity_state(DHW_PLAN, "unknown", MIDNIGHT))
        inputs = service.climate_inputs(RUN_AT)
        assert (inputs.regime, inputs.regime_state, inputs.dhw_plan) == ("comfort_off", "off", None)
        assert REGIME in service.store.counts or service.store.get(REGIME) is not None
    finally:
        shutdown(service)


def test_a_job_that_cannot_load_is_reported_and_retried(tmp_path):
    def broken():
        raise ValueError("climate shadow prior 'abcdef' matches 0")

    service = make_service(tmp_path, FakeHAServer(), factory=broken)
    try:
        assert asyncio.run(service.run_climate_shadow()) is None
    finally:
        shutdown(service)
    assert "matches 0" in service.climate_error
    assert metric(service, "climate_shadow_up") == 0.0
    assert metric(service, "climate_shadow_runs_total", result="error") == 1.0
    assert service.status()["climate_shadow"]["error"].startswith("ValueError")
    assert service.readiness().reasons == service.readiness().reasons  # unaffected, no raise
    events = list(iter_records(tmp_path / "data", "events", MIDNIGHT, RUN_AT + timedelta(hours=1)))
    assert any(e["kind"] == "climate_shadow_error" for e in events)


def test_build_job_loads_the_configs_the_prior_and_the_site(tmp_path, monkeypatch):
    for name in (
        "BATTERY_OPTIMIZER_PROMETHEUS_URL",
        legacy_name("BATTERY_OPTIMIZER_PROMETHEUS_URL"),
    ):
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    sp = prior()
    shadow.save_prior(data, sp)
    overrides = {
        **ENABLED,
        "climate_shadow.prior": sp.prior_id[:8],
        "climate_shadow.climate_config": str(REPO / "config" / "climate.yaml"),
        "climate_shadow.mpc_config": str(REPO / "config" / "climate-mpc.yaml"),
    }
    config = load_service_config(write_service_config(tmp_path, **overrides))
    with pytest.raises(ClimateDataError, match="site file"):
        build_job(config, data_dir=data, sink=ShadowSink(data), code_version="t", samples=print)
    (data / "climate").mkdir()
    (data / "climate" / "site.yaml").write_text("latitude: 40.0\nlongitude: 0.0\n")
    job = build_job(config, data_dir=data, sink=ShadowSink(data), code_version="t", samples=print)
    assert job.prior.prior_id == sp.prior_id and job.window == timedelta(days=30)
    with pytest.raises(ValueError, match="PROMETHEUS_URL"):
        build_job(config, data_dir=data, sink=ShadowSink(data), code_version="t")
    missing = load_service_config(
        write_service_config(tmp_path, **{**overrides, "climate_shadow.prior": "ffffff"})
    )
    with pytest.raises(ValueError, match="matches 0"):
        build_job(missing, data_dir=data, sink=ShadowSink(data), code_version="t", samples=print)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_the_committed_config_keeps_the_job_off_with_placeholder_entities():
    config = load_service_config(REPO / "config" / "service.yaml")
    s = config.climate_shadow
    assert not s.enabled and s.prior is None and s.go_live is None
    assert s.regime_entity == REGIME and s.dhw_plan_entity == DHW_PLAN
    assert s.away_entity == "input_boolean.comfort_away"
    raw = yaml.safe_load((REPO / "config" / "service.yaml").read_text())
    assert set(raw["climate_shadow"]) <= set(
        __import__("battery_optimizer.service.config", fromlist=["x"])._CLIMATE_SHADOW_KEYS
    )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"climate_shadow.enabled": True}, "needs climate_shadow.prior"),
        ({"climate_shadow.surprise": 1}, "unknown climate_shadow keys"),
        ({"climate_shadow.entities.regime": "not an entity"}, "entities.regime"),
        ({"climate_shadow.entities.extra": "a.b"}, "unknown climate_shadow.entities keys"),
        ({"climate_shadow.gate_rooms": ["bedroom"]}, "unknown climate_shadow keys"),
        ({"climate_shadow.go_live": "soon"}, "go_live"),
        ({"climate_shadow.prior": "XYZ"}, "prior id"),
        ({"climate_shadow.offset_minutes": 60}, "offset_minutes"),
    ],
)
def test_bad_climate_shadow_config_is_refused(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        load_service_config(write_service_config(tmp_path, **overrides))


def test_go_live_and_the_battery_config_hash(tmp_path):
    base = load_service_config(write_service_config(tmp_path))
    on = load_service_config(
        write_service_config(tmp_path, **ENABLED, **{"climate_shadow.go_live": "2026-11-09"})
    )
    assert on.climate_shadow.go_live == date(2026, 11, 9)
    assert on.config_hash() == base.config_hash()  # the climate shadow plans no battery
    assert on.public_dict()["climate_shadow"]["go_live"] == "2026-11-09"


# ---------------------------------------------------------------------------
# Static guard
# ---------------------------------------------------------------------------

CLIMATE = REPO / "src" / "battery_optimizer" / "climate"
CLIMATE_SHADOW_MODULES = [
    *sorted(CLIMATE.glob("mpc*.py")),
    CLIMATE / "b2d.py",
    CLIMATE / "plan_export.py",
    CLIMATE / "shadow.py",
    CLIMATE / "shadow_gates.py",
    REPO / "src" / "battery_optimizer" / "service" / "climate_shadow.py",
]


@pytest.mark.parametrize("path", CLIMATE_SHADOW_MODULES, ids=lambda p: p.name)
def test_the_climate_shadow_has_no_write_path(path):
    """Same rules as the service package's static guard (test_service_safety)."""
    banned_imports = {"pymodbus", "paho", "homeassistant", "aiohttp", "subprocess", "websockets"}
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        assert not {n.split(".")[0] for n in names} & banned_imports, names
        assert not any(n.endswith("ha_client") for n in names), names
        if isinstance(node, ast.Attribute) and node.attr in ("post", "put", "delete", "patch"):
            pytest.fail(f"{path.name}: HTTP write method .{node.attr} referenced")
    assert "call_service" not in path.read_text()
