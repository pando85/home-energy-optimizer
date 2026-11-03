"""HA WebSocket adapter: resync, reconnect/backoff, ordering, failed acknowledgements."""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta

import pytest
from service_fakes import FakeHAServer, ha_state, utc, wait_until

from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import HomeAssistantSettings, Secret
from battery_optimizer.service.entity_store import EntityStore
from battery_optimizer.service.ha_client import HAWebSocketClient, backoff_delay, websocket_url
from battery_optimizer.service.ha_guard import ALLOWED_WS_TYPES

T0 = utc(2026, 10, 20, 10, 0)
SETTINGS = HomeAssistantSettings(
    url="https://ha.test",
    token=Secret("t"),
    request_timeout_s=0.3,
    reconnect_initial_delay_s=0.01,
    reconnect_max_delay_s=0.05,
    reconnect_jitter_frac=0.0,
    heartbeat_interval_s=0.05,
    heartbeat_timeout_s=0.1,
)


def _client(server: FakeHAServer, **kw):
    store = EntityStore.for_entities(["sensor.grid", "sensor.soc"])
    client = HAWebSocketClient(
        settings=kw.pop("settings", SETTINGS),
        store=store,
        clock=FakeClock(T0),
        transport=server,
        rng=random.Random(0),
        **kw,
    )
    return client, store


async def _run(client, body):
    task = asyncio.create_task(client.run())
    try:
        await body()
    finally:
        await client.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_websocket_url():
    assert websocket_url("https://ha.example/") == "wss://ha.example/api/websocket"
    assert websocket_url("http://ha:8123") == "ws://ha:8123/api/websocket"


def test_connect_resync_and_events():
    server = FakeHAServer([ha_state("sensor.grid", 100, T0), ha_state("sensor.other", 1, T0)])
    synced = []
    client, store = _client(server, on_synced=lambda: synced.append(1))

    async def body():
        await wait_until(lambda: client.stats.synced)
        assert store.get("sensor.grid").state == "100"
        assert store.get("sensor.other") is None  # untracked entities are ignored
        conn = server.connections[0]
        conn.push_state(ha_state("sensor.grid", 250, T0 + timedelta(seconds=10)))
        await wait_until(lambda: store.get("sensor.grid").state == "250")

    asyncio.run(_run(client, body))
    assert synced == [1]
    first = server.connections[0].sent
    assert [m["type"] for m in first[:3]] == ["auth", "subscribe_events", "get_states"]
    assert first[1]["event_type"] == "state_changed"


def test_disconnect_reconnect_and_full_resync():
    server = FakeHAServer([ha_state("sensor.grid", 100, T0)])
    disconnects = []
    client, store = _client(server, on_disconnected=lambda: disconnects.append(1))

    async def body():
        await wait_until(lambda: client.stats.synced)
        server.states["sensor.grid"] = ha_state("sensor.grid", 999, T0 + timedelta(minutes=5))
        server.connections[0].drop()
        await wait_until(lambda: client.stats.resyncs == 2)
        assert store.get("sensor.grid").state == "999"  # resync picked up the missed change
        assert disconnects == [1]  # (stopping the client later ends the second session too)

    asyncio.run(_run(client, body))
    assert client.stats.reconnects >= 1
    assert client.stats.failures.get("disconnect") == 1
    assert len(server.connections) == 2
    assert [m["type"] for m in server.connections[1].sent[:3]] == [
        "auth",
        "subscribe_events",
        "get_states",
    ]


def test_connect_sync_and_disconnect_are_logged(caplog):
    caplog.set_level("INFO", logger="battery_optimizer.service.ha_client")
    server = FakeHAServer([ha_state("sensor.grid", 100, T0)])
    client, _ = _client(server)

    async def body():
        await wait_until(lambda: client.stats.synced)
        server.connections[0].drop()
        await wait_until(lambda: client.stats.resyncs == 2)

    asyncio.run(_run(client, body))
    lines = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert ("INFO", "home assistant connected connects=1 reconnects=0") in lines
    assert ("INFO", "home assistant synced states=1 tracked_seen=1 resyncs=1") in lines
    assert any(
        level == "WARNING" and m.startswith("home assistant disconnected: disconnect:")
        for level, m in lines
    )
    assert not any("access_token" in m for _, m in lines)


def test_snapshot_older_than_event_is_rejected_out_of_order():
    """Subscribe-then-snapshot: an event arriving before the get_states result wins."""
    server = FakeHAServer([ha_state("sensor.grid", 100, T0)])
    server.behaviours = [
        {"events_before_states": [ha_state("sensor.grid", 300, T0 + timedelta(seconds=30))]}
    ]
    client, store = _client(server)

    async def body():
        await wait_until(lambda: client.stats.synced)

    asyncio.run(_run(client, body))
    assert store.get("sensor.grid").state == "300"
    assert store.counts["sensor.grid"] == {"accepted": 1, "out_of_order": 1}


def test_duplicate_events_are_counted_not_reapplied():
    server = FakeHAServer([ha_state("sensor.grid", 100, T0)])
    client, store = _client(server)

    async def body():
        await wait_until(lambda: client.stats.synced)
        conn = server.connections[0]
        conn.push_state(ha_state("sensor.grid", 100, T0))
        conn.push_state(ha_state("sensor.grid", 50, T0 - timedelta(seconds=1)))
        await wait_until(lambda: sum(store.counts["sensor.grid"].values()) == 3)

    asyncio.run(_run(client, body))
    assert store.counts["sensor.grid"] == {"accepted": 1, "duplicate": 1, "out_of_order": 1}
    assert store.get("sensor.grid").state == "100"


@pytest.mark.parametrize(
    ("behaviour", "stage"),
    [
        ({"subscribe_fail": True}, "subscribe"),
        ({"get_states_silent": True}, "get_states"),
        ({"auth_invalid": True}, "auth"),
        ({"refuse": True}, "connect"),
    ],
)
def test_failed_acknowledgements_reconnect(behaviour, stage):
    server = FakeHAServer([ha_state("sensor.grid", 100, T0)])
    server.behaviours = [behaviour]
    settings = SETTINGS
    if stage == "auth":
        # auth_invalid backs off at the cap; keep the cap tiny for the test.
        settings = HomeAssistantSettings(**{**SETTINGS.__dict__, "reconnect_max_delay_s": 0.02})
    client, store = _client(server, settings=settings)

    async def body():
        await wait_until(lambda: client.stats.synced, timeout=5)

    asyncio.run(_run(client, body))
    assert client.stats.failures.get(stage) == 1
    assert client.stats.resyncs == 1  # the second connection synced
    assert store.get("sensor.grid").state == "100"


def test_missing_pong_ends_the_session():
    server = FakeHAServer([ha_state("sensor.grid", 100, T0)])
    server.behaviours = [{"no_pong": True}]
    client, _ = _client(server)

    async def body():
        await wait_until(lambda: client.stats.resyncs == 2, timeout=5)

    asyncio.run(_run(client, body))
    assert client.stats.failures.get("heartbeat") == 1


def test_client_only_ever_sends_allowlisted_types():
    server = FakeHAServer([ha_state("sensor.grid", 100, T0)])
    server.behaviours = [{"no_pong": True}, {}]
    client, _ = _client(server)

    async def body():
        await wait_until(lambda: client.stats.resyncs == 2, timeout=5)
        await asyncio.sleep(0.12)  # let a few heartbeats through

    asyncio.run(_run(client, body))
    types = {m["type"] for m in server.all_sent}
    assert types <= ALLOWED_WS_TYPES
    assert {"auth", "subscribe_events", "get_states", "ping"} <= types


def test_backoff_is_capped_with_bounded_jitter():
    rng = random.Random(1)
    delays = [backoff_delay(a, initial=1.0, maximum=60.0, jitter=0.2, rng=rng) for a in range(12)]
    assert delays[0] == pytest.approx(1.0, rel=0.2)
    assert all(d <= 72.0 + 1e-9 for d in delays)
    assert max(delays) >= 48.0
    exact = [backoff_delay(a, initial=1, maximum=8, jitter=0, rng=rng) for a in range(6)]
    assert exact == [1, 2, 4, 8, 8, 8]
