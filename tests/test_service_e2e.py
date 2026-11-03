"""End to end through the app lifespan: fake HA -> resync -> replan -> plan -> HTTP -> stop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from service_fakes import (
    TARIFF,
    TZ,
    FakeHAServer,
    flat_history,
    ha_state,
    pvpc_attributes,
    service_config,
    wait_until,
)

from battery_optimizer.service.api import create_app
from battery_optimizer.service.clock import SystemClock
from battery_optimizer.service.runtime import ShadowService
from battery_optimizer.service.timegrid import local_date


def test_service_runs_end_to_end_and_stops_cleanly(tmp_path):
    now = datetime.now(UTC)
    day = local_date(now, TZ)
    server = FakeHAServer(
        [
            ha_state("sensor.shellyem_main_channel_1_power", 420, now,
                     attributes={"unit_of_measurement": "W"}),
            ha_state("sensor.shellyem_main_channel_1_energy", 1234.5, now,
                     attributes={"unit_of_measurement": "kWh"}),
            ha_state("sensor.esios_pvpc", 0.15, now,
                     attributes=pvpc_attributes(day, tomorrow=True)),
            # The committed config plans from the SOC entity, trusted while every battery responds.
            ha_state("sensor.venus_battery_soc", 50, now, attributes={"unit_of_measurement": "%"}),
            ha_state("sensor.omnibattery_non_responsive_batteries", "None", now),
        ]
    )  # fmt: skip
    config = service_config(
        tmp_path, **{"planning.replan.debounce_s": 0, "planning.replan.min_interval_s": 0}
    )
    service = ShadowService(
        config=config,
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=SystemClock(),
        code_version="e2e",
        ws_transport=server,
        history_fetcher=lambda at, d: flat_history(d),
    )
    app = create_app(service)

    async def go():
        async with app.router.lifespan_context(app):
            await wait_until(lambda: service.plans, timeout=10)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                ready = await client.get("/health/ready")
                assert ready.status_code == 200, ready.json()
                status = (await client.get("/status")).json()
                assert status["home_assistant"]["resyncs"] == 1
                assert status["entity_updates"]["sensor.shellyem_main_channel_1_power"] == {
                    "accepted": 1
                }
        assert all(t.done() for t in service._tasks) or not service._tasks

    asyncio.run(go())
    sent = {m["type"] for m in server.all_sent}
    assert sent <= {"auth", "subscribe_events", "get_states", "ping"}
    events = (tmp_path / "data" / "service" / "v1" / "events").rglob("events.jsonl")
    kinds = [line for path in events for line in path.read_text().splitlines()]
    assert any('"service_started"' in k for k in kinds)
    assert any('"service_stopped"' in k for k in kinds)
    assert any('"ha_synced"' in k for k in kinds)
