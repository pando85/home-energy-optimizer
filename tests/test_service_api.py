"""HTTP surface through httpx's ASGI transport (no sockets)."""

from __future__ import annotations

import asyncio

import httpx
from service_fakes import NOW, TARIFF, FakeHAServer, feed, service_config

from battery_optimizer.service.api import create_app
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.runtime import ShadowService


def _service(tmp_path):
    service = ShadowService(
        config=service_config(tmp_path),
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=FakeClock(NOW),
        code_version="test-sha",
        ws_transport=FakeHAServer(),
    )
    service.ha.stats.connected = True
    service.ha.stats.synced = True
    return service


async def _get(app, path, method="GET"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bo.test") as client:
        return await client.request(method, path)


def test_endpoints_not_ready_then_ready(tmp_path):
    service = _service(tmp_path)
    app = create_app(service, manage_lifecycle=False)

    async def go():
        live = await _get(app, "/health/live")
        assert live.status_code == 200 and live.json() == {"status": "alive"}

        ready = await _get(app, "/health/ready")
        assert ready.status_code == 503
        body = ready.json()
        assert body["ready"] is False
        assert {"grid_power_stale", "prices_missing", "no_recent_plan"} <= set(body["reasons"])

        plan = await _get(app, "/plan")
        assert plan.status_code == 200 and plan.json()["plan"] is None

        feed(service, NOW)
        assert await service.request_solve("test") == "ok"
        ready = await _get(app, "/health/ready")
        assert ready.status_code == 200 and ready.json()["ready"] is True

        status = (await _get(app, "/status")).json()
        assert status["mode"] == "shadow"
        assert status["code_version"] == "test-sha"
        assert status["home_assistant"]["synced"] is True
        assert status["solver"]["last_solve"]["outcome"] == "ok"
        assert "test-token" not in str(status)

        plan = (await _get(app, "/plan")).json()
        assert plan["shadow"] is True
        assert plan["plan"]["shadow"] is True
        assert plan["plan"]["intents"][0]["shadow"] is True
        assert plan["current_intent"] is None  # the plan starts at the next boundary

        metrics = await _get(app, "/metrics")
        text = metrics.text
        assert metrics.headers["content-type"].startswith("text/plain")
        for name in (
            'battery_optimizer_build_info{code_version="test-sha"',
            "battery_optimizer_ready 1.0",
            "battery_optimizer_ha_connected 1.0",
            "battery_optimizer_solve_duration_seconds_count 1.0",
            'battery_optimizer_solves_total{result="ok"} 1.0',
            'battery_optimizer_input_age_seconds{entity="sensor.shellyem_main_channel_1_power"',
            'battery_optimizer_not_ready_reason{reason="soc_stale"} 0.0',
            "battery_optimizer_plan_age_seconds",
            "battery_optimizer_ha_reconnects_total",
        ):
            assert name in text, name
        assert "_created" not in text
        assert "plan_id" not in text  # no high-cardinality identities in metrics

    try:
        asyncio.run(go())
    finally:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)


def test_no_writable_routes(tmp_path):
    service = _service(tmp_path)
    app = create_app(service, manage_lifecycle=False)

    async def go():
        for path in (
            "/plan",
            "/status",
            "/health/ready",
            "/metrics",
            "/climate/plan",
            "/climate/plan/summary",
        ):
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                response = await _get(app, path, method)
                assert response.status_code == 405, (method, path)
        assert (await _get(app, "/docs")).status_code == 404
        assert (await _get(app, "/openapi.json")).status_code == 404

    try:
        asyncio.run(go())
    finally:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)


def test_lifespan_starts_and_stops_the_service(tmp_path):
    calls = []

    class View:
        metrics = None

        def readiness(self):
            raise AssertionError

        def status(self):
            return {}

        def plan_view(self):
            return {}

        async def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

    app = create_app(View())

    async def go():
        async with app.router.lifespan_context(app):
            assert calls == ["start"]

    asyncio.run(go())
    assert calls == ["start", "stop"]
