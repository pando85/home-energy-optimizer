"""The advisory climate plan inside the service: endpoints, owner mode, metrics, restart, safety."""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from climate_shadow_fakes import CCFG, MPC, SETTINGS, SITE, FakeSamples, forecast_record, prior
from jsonschema import Draft202012Validator
from service_fakes import FakeHAServer, entity_state, ha_state, write_service_config
from test_climate_shadow_service import (
    MIDNIGHT,
    REGIME,
    RUN_AT,
    make_service,
    metric,
    run_synced,
    shutdown,
)

from battery_optimizer.climate import plan_export
from battery_optimizer.service.api import create_app
from battery_optimizer.service.climate_shadow import ClimateShadowJob
from battery_optimizer.service.config import load_service_config
from battery_optimizer.service.ha_guard import check_ws_message
from battery_optimizer.service.sink import ShadowSink

REPO = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "docs" / "schemas" / "climate-plan-v1.json").read_text())
MODE = "input_select.climate_optimizer_mode"


def states(mode: str | None):
    out = [ha_state(REGIME, "on", MIDNIGHT)]
    if mode is not None:
        out.append(ha_state(MODE, mode, MIDNIGHT))
    return out


def run_once(tmp_path, mode: str | None, **overrides):
    server = FakeHAServer(states(mode))
    service = make_service(tmp_path, server, **overrides)
    fc_at = MIDNIGHT - timedelta(minutes=55)
    service.sink.append_weather_forecast(fc_at, forecast_record(fc_at))
    try:
        (summary,) = asyncio.run(run_synced(service, service.run_climate_shadow))
    finally:
        shutdown(service)
    assert summary["status"] == "planned"
    return service, server


async def get(service, path, method="GET"):
    app = create_app(service, manage_lifecycle=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bo.test") as client:
        return await client.request(method, path)


def fetch(service, path):
    return asyncio.run(get(service, path))


# ---------------------------------------------------------------------------
# Defaults: off
# ---------------------------------------------------------------------------


def test_the_committed_config_exports_nothing_by_default():
    s = load_service_config(REPO / "config" / "service.yaml").climate_shadow
    assert s.optimizer_mode == "off" and s.plan_ttl_minutes == 90
    assert s.mode_entity == MODE and MODE in s.entities()
    assert not s.enabled  # and the shadow itself is off in the committed file


def test_with_the_shadow_disabled_the_endpoint_is_404_and_status_says_so(tmp_path):
    server = FakeHAServer()
    service = make_service(tmp_path, server, **{"climate_shadow.enabled": False})
    try:
        r = fetch(service, "/climate/plan")
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
        assert r.json()["state"] == "climate_shadow_disabled"
        summary = fetch(service, "/climate/plan/summary")
        assert summary.status_code == 200 and summary.json()["state"] == "climate_shadow_disabled"
        assert service.status()["climate_plan"] is None
        assert "climate_plan_age" not in fetch(service, "/metrics").text
    finally:
        shutdown(service)


def test_enabled_but_off_publishes_nothing_yet_the_shadow_keeps_measuring(tmp_path):
    service, _server = run_once(tmp_path, None)  # no helper: the configured fallback, off
    r = fetch(service, "/climate/plan")
    assert r.status_code == 404
    body = r.json()
    assert body["state"] == "mode_off" and "off" in body["reason"]
    assert (body["requested_mode"], body["effective_mode"], body["mode_source"]) == (
        "off",
        "off",
        "config",
    )
    assert "slots" not in body
    summary = fetch(service, "/climate/plan/summary").json()
    assert summary["state"] == "mode_off" and "plan_id" not in summary
    # the shadow's own run, record and scoring queue are untouched by the export mode
    assert metric(service, "climate_shadow_runs_total", result="planned") == 1.0
    assert len(service.climate.queue) == 1 and service.climate.plan is not None
    assert metric(service, "climate_plan_state", state="mode_off") == 1.0
    assert metric(service, "climate_plan_mode_effective", mode="off") == 1.0
    assert metric(service, "climate_plan_heating_planned_now") is None  # absent, not 0
    served = metric(service, "climate_plan_served_total", endpoint="plan", result="unavailable")
    assert served == 1.0


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def test_shadow_publishes_the_advisory_plan(tmp_path):
    service, server = run_once(tmp_path, "shadow")
    r = fetch(service, "/climate/plan")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    errors = list(Draft202012Validator(SCHEMA).iter_errors(body))
    assert not errors, errors[:3]
    assert body["status"] == "planned" and body["advisory"] is True and body["version"] == 1
    assert (body["requested_mode"], body["effective_mode"]) == ("shadow", "shadow")
    assert body["live_locked"] is False and body["mode_source"] == "helper"
    assert body["code_version"] == "test-sha" and body["regime"] == "comfort"
    assert body["slots"]["n"] == 96 and body["summary"]["heating_now"] in (True, False)
    assert len(r.content) < 8000
    summary = fetch(service, "/climate/plan/summary").json()
    assert summary["state"] == "planned" and summary["plan_id"] == body["plan_id"]
    assert len(json.dumps(summary)) < 3000
    # status
    st = service.status()["climate_plan"]
    assert st["state"] == "planned" and st["plan_id"] == body["plan_id"]
    assert st["effective_mode"] == "shadow" and st["live_allowed"] is False
    assert st["actuation"].startswith("none")
    # metrics
    assert metric(service, "climate_plan_state", state="planned") == 1.0
    assert metric(service, "climate_plan_mode_requested", mode="shadow") == 1.0
    assert metric(service, "climate_plan_mode_effective", mode="shadow") == 1.0
    assert metric(service, "climate_plan_live_locked") == 0.0
    assert metric(service, "climate_plan_valid") == 1.0
    assert metric(service, "climate_plan_age_seconds") == 0.0
    assert metric(service, "climate_plan_heating_planned_now") == float(
        body["summary"]["heating_now"]
    )
    if body["summary"]["heating_now"]:
        water = metric(service, "climate_plan_water_target_celsius")
        assert water == body["summary"]["water_c_now"]
    assert metric(service, "climate_plan_served_total", endpoint="plan", result="ok") == 1.0
    assert metric(service, "climate_plan_served_total", endpoint="summary", result="ok") == 1.0
    # nothing was sent to Home Assistant by the export: no service call at all (no archive here)
    for message in server.all_sent:
        check_ws_message(message)
    assert server.service_calls == []


def test_live_is_a_request_only_and_the_plan_says_so(tmp_path):
    service, _ = run_once(tmp_path, "live")
    body = fetch(service, "/climate/plan").json()
    assert body["requested_mode"] == "live" and body["effective_mode"] == "shadow"
    assert body["live_locked"] is True and "ADR-0008" in body["live_lock_reason"]
    assert "optimizer_live_allowed=false" in body["live_lock_reason"]
    assert body["advisory"] is True and body["status"] == "planned"
    Draft202012Validator(SCHEMA).validate(body)
    st = service.status()["climate_plan"]
    assert (st["requested_mode"], st["effective_mode"], st["live_locked"]) == (
        "live",
        "shadow",
        True,
    )
    assert metric(service, "climate_plan_live_locked") == 1.0
    assert metric(service, "climate_plan_mode_requested", mode="live") == 1.0
    assert metric(service, "climate_plan_mode_effective", mode="live") == 0.0
    assert metric(service, "climate_plan_mode_effective", mode="shadow") == 1.0
    summary = fetch(service, "/climate/plan/summary").json()
    assert summary["live_locked"] is True and summary["effective_mode"] == "shadow"


def test_the_helper_is_followed_live_and_a_garbage_helper_falls_back_to_the_config(tmp_path):
    service, _ = run_once(tmp_path, "shadow")
    assert fetch(service, "/climate/plan").status_code == 200
    for i, (state, code, requested) in enumerate(
        (
            ("off", 404, "off"),
            ("live", 200, "live"),
            ("unavailable", 404, "off"),  # config fallback: off
            ("banana", 404, "off"),
            ("shadow", 200, "shadow"),
        )
    ):
        service.store.apply(entity_state(MODE, state, MIDNIGHT + timedelta(minutes=i + 1)))
        r = fetch(service, "/climate/plan")
        assert r.status_code == code, state
        assert r.json()["requested_mode"] == requested
        assert r.json()["effective_mode"] in ("off", "shadow")
    shadow_fallback = {"climate_shadow.optimizer_mode": "shadow"}
    (tmp_path / "b").mkdir()
    service2, _ = run_once(tmp_path / "b", None, **shadow_fallback)
    body = fetch(service2, "/climate/plan").json()
    assert (body["requested_mode"], body["mode_source"]) == ("shadow", "config")
    service2.store.apply(entity_state(MODE, "off", MIDNIGHT + timedelta(minutes=1)))
    assert fetch(service2, "/climate/plan").status_code == 404
    service2.store.apply(entity_state(MODE, "garbage", MIDNIGHT + timedelta(minutes=2)))
    assert fetch(service2, "/climate/plan").status_code == 200


def test_a_plan_expires_and_is_then_absent(tmp_path):
    service, _ = run_once(tmp_path, "shadow")
    assert fetch(service, "/climate/plan").status_code == 200
    service.clock.set(MIDNIGHT + timedelta(minutes=89))
    assert fetch(service, "/climate/plan").status_code == 200
    service.clock.set(MIDNIGHT + timedelta(minutes=91))
    r = fetch(service, "/climate/plan")
    assert r.status_code == 404 and r.json()["state"] == "expired"
    assert fetch(service, "/climate/plan/summary").json()["state"] == "expired"
    assert metric(service, "climate_plan_valid") == 0.0
    assert metric(service, "climate_plan_state", state="expired") == 1.0
    assert metric(service, "climate_plan_heating_planned_now") is None


def test_the_ttl_is_configurable(tmp_path):
    service, _ = run_once(tmp_path, "shadow", **{"climate_shadow.plan_ttl_minutes": 75})
    body = fetch(service, "/climate/plan").json()
    assert body["valid_until"] == (MIDNIGHT + timedelta(minutes=75)).isoformat()


def test_a_restart_inside_the_validity_serves_the_same_plan(tmp_path):
    service, _ = run_once(tmp_path, "shadow")
    first = fetch(service, "/climate/plan").json()
    # a new process over the same store: the boundary is already decided, the plan is rebuilt
    again = ClimateShadowJob(
        settings=service.config.climate_shadow,
        climate_config=CCFG,
        mpc_config=MPC,
        prior=prior(),
        site=SITE,
        sink=ShadowSink(tmp_path / "data"),
        data_dir=tmp_path / "data",
        samples=FakeSamples(MIDNIGHT - timedelta(days=10), RUN_AT),
        code_version="test-sha",
        plan_settings=SETTINGS,
    )
    again.load(RUN_AT)
    assert again.plan is not None and again.plan["plan_id"] == first["plan_id"]
    assert again.plan["slots"] == first["slots"]


def test_a_failed_export_becomes_an_error_plan_and_never_stops_the_shadow(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(plan_export, "_water", boom)
    service, _ = run_once(tmp_path, "shadow")  # the decision itself still plans and is recorded
    body = fetch(service, "/climate/plan").json()
    assert body["status"] == "error" and body["slots"] is None
    assert "RuntimeError" in body["reason"] and "secret" not in body["reason"]
    Draft202012Validator(SCHEMA).validate(body)
    assert metric(service, "climate_shadow_runs_total", result="planned") == 1.0


def test_a_skipped_decision_is_served_without_slots(tmp_path):
    service = make_service(tmp_path, FakeHAServer(states("shadow")))
    # a house observed for a few hours only: the observer cannot warm up, the run is no_state
    job = service._climate_factory()
    job._samples = FakeSamples(RUN_AT - timedelta(hours=3), RUN_AT)
    service._climate_factory = lambda: job
    try:
        (summary,) = asyncio.run(run_synced(service, service.run_climate_shadow))
    finally:
        shutdown(service)
    assert summary["status"] in ("no_state", "no_inputs", "error")
    body = fetch(service, "/climate/plan").json()
    assert body["status"] in ("skipped", "error") and body["slots"] is None and body["reason"]
    assert body["summary"]["heating_now"] is None
    Draft202012Validator(SCHEMA).validate(body)
    assert metric(service, "climate_plan_state", state=body["status"]) == 1.0
    assert metric(service, "climate_plan_heating_planned_now") is None


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_the_climate_routes_are_read_only(tmp_path):
    service, _ = run_once(tmp_path, "shadow")
    for path in ("/climate/plan", "/climate/plan/summary"):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert asyncio.run(get(service, path, method)).status_code == 405, (method, path)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"climate_shadow.optimizer_mode": "on"}, "optimizer_mode"),
        ({"climate_shadow.optimizer_mode": True}, "optimizer_mode"),
        ({"climate_shadow.optimizer_mode": "Live "}, "optimizer_mode"),
        ({"climate_shadow.plan_ttl_minutes": 0}, "plan_ttl_minutes"),
        ({"climate_shadow.plan_ttl_minutes": 600}, "plan_ttl_minutes"),
        ({"climate_shadow.entities.mode": "not an entity"}, "entities.mode"),
    ],
)
def test_bad_plan_config_is_refused(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        load_service_config(write_service_config(tmp_path, **overrides))


def test_an_unquoted_yaml_off_is_the_off_mode_and_live_is_accepted_as_a_request(tmp_path):
    key = "climate_shadow.optimizer_mode"
    cfg = load_service_config(write_service_config(tmp_path, **{key: False}))
    assert cfg.climate_shadow.optimizer_mode == "off"
    cfg = load_service_config(write_service_config(tmp_path, **{key: "live"}))
    assert cfg.climate_shadow.optimizer_mode == "live"
    assert plan_export.resolve_mode(None, "live").effective == "shadow"
    # the export mode never changes a battery plan
    base = load_service_config(write_service_config(tmp_path))
    assert cfg.config_hash() == base.config_hash()


def test_the_mode_entity_is_subscribed_only_with_the_shadow_enabled(tmp_path):
    server = FakeHAServer()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    on = make_service(tmp_path / "a", server)
    off = make_service(tmp_path / "b", server, **{"climate_shadow.enabled": False})
    try:
        assert MODE in on.store.tracked and MODE not in off.store.tracked
    finally:
        shutdown(on)
        shutdown(off)


# ---------------------------------------------------------------------------
# Static guard: the export is a pure read
# ---------------------------------------------------------------------------


def test_plan_export_has_no_io_and_the_guard_allowlist_is_untouched():
    path = REPO / "src" / "battery_optimizer" / "climate" / "plan_export.py"
    tree = ast.parse(path.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "hashlib",
        "json",
        "math",
        "typing",
    }, imported
    assert "call_service" not in path.read_text()
    guard = (REPO / "src" / "battery_optimizer" / "service" / "ha_guard.py").read_text()
    assert "climate_optimizer_mode" not in guard  # the select is read, never writable
