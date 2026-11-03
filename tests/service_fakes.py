"""Fakes and builders shared by the shadow-service tests (no network, no real HA)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import yaml

from battery_optimizer.config import load_config
from battery_optimizer.service.config import ServiceConfig, load_service_config
from battery_optimizer.service.entity_store import EntityState
from battery_optimizer.service.ha_client import HAConnectionClosedError
from battery_optimizer.service.history import BASIS_GRID_ONLY, LoadHistory
from battery_optimizer.service.pvpc import pvpc_hour_keys
from battery_optimizer.service.timegrid import local_date, local_day_bounds_utc, slot_range

REPO = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Madrid")
TARIFF = load_config(REPO / "config" / "tariff.yaml")

_CLOSE = object()

WEATHER_FORECAST = [
    {
        "datetime": "2026-10-20T11:00:00+00:00",
        "condition": "sunny",
        "temperature": 21.5,
        "templow": None,
        "precipitation_probability": 0,
    },
    {
        "datetime": "2026-10-20T12:00:00+00:00",
        "condition": "cloudy",
        "temperature": 22.0,
        "precipitation_probability": 10,
    },
]


async def wait_until(predicate, timeout: float = 3.0, step: float = 0.005) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(step)


def ha_state(
    entity_id: str,
    state: Any,
    updated: datetime,
    *,
    reported: datetime | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An HA state dict as REST / WebSocket deliver it."""
    return {
        "entity_id": entity_id,
        "state": str(state),
        "attributes": attributes or {},
        "last_changed": updated.isoformat(),
        "last_updated": updated.isoformat(),
        "last_reported": (reported or updated).isoformat(),
    }


def entity_state(entity_id: str, state: Any, updated: datetime, **kw: Any) -> EntityState:
    from battery_optimizer.service.entity_store import parse_ha_state

    return parse_ha_state(ha_state(entity_id, state, updated, **kw))


def pvpc_attributes(day: date, price_fn=None, *, tomorrow: bool = False) -> dict[str, Any]:
    """``price_HHh`` attributes for *day* (and ``price_next_day_HHh`` for day+1)."""
    price_fn = price_fn or (lambda utc_hour: 0.10 + 0.01 * (utc_hour.hour % 12))
    attrs: dict[str, Any] = {"unit_of_measurement": "€/kWh"}
    for key, hour in pvpc_hour_keys(day, TZ, next_day=False):
        attrs[key] = price_fn(hour)
    if tomorrow:
        for key, hour in pvpc_hour_keys(day + timedelta(days=1), TZ, next_day=True):
            attrs[key] = price_fn(hour)
    return attrs


def pvpc_state(
    day: date, updated: datetime, *, tomorrow: bool = False, price_fn=None
) -> EntityState:
    return entity_state(
        "sensor.esios_pvpc",
        0.15,
        updated,
        attributes=pvpc_attributes(day, price_fn, tomorrow=tomorrow),
    )


def flat_history(day: date, *, load_kwh: float = 0.1, days: int = 28) -> LoadHistory:
    """28 days of constant gross load cut at *day*'s local midnight."""
    cutoff, _ = local_day_bounds_utc(day, TZ)
    start, _ = local_day_bounds_utc(day - timedelta(days=days), TZ)
    starts = slot_range(start, cutoff)
    return LoadHistory(
        starts=tuple(starts),
        load_kwh=np.full(len(starts), load_kwh),
        usable=np.ones(len(starts), dtype=bool),
        cutoff=cutoff,
        cutoff_day=day,
        window_days=days,
        basis=BASIS_GRID_ONLY,
        fetched_at=cutoff,
        mean_price_eur_per_kwh=0.15,
        price_samples=1000,
    )


def write_service_config(tmp_path: Path, **overrides: Any) -> Path:
    raw = yaml.safe_load((REPO / "config" / "service.yaml").read_text())
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    path = tmp_path / "service.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def service_config(tmp_path: Path, **overrides: Any) -> ServiceConfig:
    from battery_optimizer.service.config import Secret

    config = load_service_config(write_service_config(tmp_path, **overrides))
    return replace(
        config,
        home_assistant=replace(
            config.home_assistant,
            url="http://ha.test",
            token=Secret("test-token"),
            request_timeout_s=0.3,
            reconnect_initial_delay_s=0.01,
            reconnect_max_delay_s=0.05,
            heartbeat_interval_s=0.05,
            heartbeat_timeout_s=0.1,
        ),
    )


#: The committed config maps the battery (commissioned 2026-11-03); this is the house without it.
NO_BATTERY = {
    "entities.battery_soc": None,
    "entities.battery_charge_energy": None,
    "entities.battery_discharge_energy": None,
}

ENTITY_MODE = {
    "planning.soc_source": "entity",
    "entities.battery_soc": "sensor.test_soc",
    "entities.battery_power": "sensor.test_battery_power",
}


# ---------------------------------------------------------------------------
# Fake Home Assistant WebSocket server
# ---------------------------------------------------------------------------


class FakeConnection:
    def __init__(self, server: FakeHAServer, behaviour: dict[str, Any]) -> None:
        self.server = server
        self.behaviour = behaviour
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.subscription_id: int | None = None
        self.closed = False

    async def send(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        self.server.all_sent.append(message)
        await self.server.handle(self, message)

    async def recv(self) -> str:
        item = await self.inbox.get()
        if item is _CLOSE:
            self.closed = True
            raise HAConnectionClosedError("fake server closed the socket")
        return item

    async def close(self) -> None:
        self.inbox.put_nowait(_CLOSE)

    def push(self, message: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(message))

    def push_state(self, payload: dict[str, Any]) -> None:
        self.push(
            {
                "id": self.subscription_id,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {"entity_id": payload["entity_id"], "new_state": payload},
                },
            }
        )

    def drop(self) -> None:
        self.inbox.put_nowait(_CLOSE)


class FakeHAServer:
    """Scripted HA: ``behaviours[i]`` configures the i-th connection.

    Keys: ``refuse`` (connect raises), ``auth_invalid``, ``subscribe_fail``,
    ``get_states_silent`` (never answers), ``no_pong``, ``events_before_states``
    (payloads pushed as events before the get_states result); ``actuation_silent`` (a
    live write is acknowledged but its state never changes); for the weather archive
    ``weather_forecast`` (the list returned), ``weather_response`` (the whole response
    object), ``weather_error`` (``success: false``), ``weather_silent`` (no answer).
    """

    def __init__(self, states: list[dict[str, Any]] | None = None) -> None:
        self.states = {s["entity_id"]: s for s in states or []}
        self.behaviours: list[dict[str, Any]] = []
        self.connections: list[FakeConnection] = []
        self.all_sent: list[dict[str, Any]] = []
        self.service_calls: list[dict[str, Any]] = []
        self.connect_attempts = 0

    async def connect(self, url: str, *, timeout: float) -> FakeConnection:
        index = self.connect_attempts
        self.connect_attempts += 1
        behaviour = self.behaviours[index] if index < len(self.behaviours) else {}
        if behaviour.get("refuse"):
            raise OSError("connection refused (fake)")
        conn = FakeConnection(self, behaviour)
        self.connections.append(conn)
        conn.push({"type": "auth_required", "ha_version": "2026.10.2"})
        return conn

    async def handle(self, conn: FakeConnection, message: dict[str, Any]) -> None:
        kind = message.get("type")
        b = conn.behaviour
        if kind == "auth":
            conn.push({"type": "auth_invalid" if b.get("auth_invalid") else "auth_ok"})
        elif kind == "subscribe_events":
            conn.subscription_id = message["id"]
            if b.get("subscribe_fail"):
                conn.push(
                    {
                        "id": message["id"],
                        "type": "result",
                        "success": False,
                        "error": {"code": "unauthorized", "message": "nope"},
                    }
                )
            else:
                conn.push({"id": message["id"], "type": "result", "success": True, "result": None})
        elif kind == "get_states":
            if b.get("get_states_silent"):
                return
            for payload in b.get("events_before_states", []):
                conn.push_state(payload)
            conn.push(
                {
                    "id": message["id"],
                    "type": "result",
                    "success": True,
                    "result": list(self.states.values()),
                }
            )
        elif kind == "ping":
            if not b.get("no_pong"):
                conn.push({"id": message["id"], "type": "pong"})
        elif kind == "call_service" and message.get("domain") != "weather":
            # A live Omnibattery write: acknowledge it and echo the new state like HA does.
            self.service_calls.append(message)
            conn.push({"id": message["id"], "type": "result", "success": True, "result": None})
            if b.get("actuation_silent"):
                return
            entity = message["target"]["entity_id"]
            data = message.get("service_data") or {}
            at = datetime.now(UTC)
            if message["domain"] == "switch":
                value: Any = "on" if message["service"] == "turn_on" else "off"
            elif message["domain"] == "number":
                value = data["value"]
            else:
                value = datetime.fromtimestamp(data["timestamp"], UTC).isoformat()
            old = self.states.get(entity, {})
            payload = ha_state(entity, value, at, attributes=old.get("attributes"))
            self.states[entity] = payload
            conn.push_state(payload)
        elif kind == "call_service":
            self.service_calls.append(message)
            if b.get("weather_error"):
                conn.push(
                    {
                        "id": message["id"],
                        "type": "result",
                        "success": False,
                        "error": {"code": "unauthorized", "message": "nope"},
                    }
                )
            elif not b.get("weather_silent"):
                entity = message["target"]["entity_id"]
                forecast = b.get("weather_forecast", WEATHER_FORECAST)
                response = {entity: {"forecast": forecast}}
                conn.push(
                    {
                        "id": message["id"],
                        "type": "result",
                        "success": True,
                        "result": {
                            "context": {"id": "01", "parent_id": None, "user_id": "u"},
                            "response": b.get("weather_response", response),
                        },
                    }
                )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


NOW = utc(2026, 10, 20, 10, 50)  # 12:50 CEST; next boundary 11:00Z


def feed(service, at, *, soc=0.5, grid=400.0, energy=None, tomorrow=False, history=True):
    store = service.store
    store.apply(
        entity_state(
            "sensor.shellyem_main_channel_1_power",
            grid,
            at,
            attributes={"unit_of_measurement": "W"},
        )
    )
    if energy is not None:
        store.apply(
            entity_state(
                "sensor.shellyem_main_channel_1_energy",
                energy,
                at,
                attributes={"unit_of_measurement": "kWh"},
            )
        )
    if service.config.entities.get("battery_soc"):
        store.apply(
            entity_state("sensor.test_soc", soc * 100, at, attributes={"unit_of_measurement": "%"})
        )
    day = local_date(at, TZ)
    hour = at.replace(minute=0, second=0, microsecond=0)
    if service.price_curve is None or service.price_curve.reference_day != day or tomorrow:
        # Publishing tomorrow's prices changes the attributes, so HA bumps last_updated.
        store.apply(pvpc_state(day, at if tomorrow else hour, tomorrow=tomorrow))
    if history:
        service.history = flat_history(day)
