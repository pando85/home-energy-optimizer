"""Home Assistant adapters: read-only WebSocket subscription and REST GET.

WebSocket session (``/api/websocket``)::

    auth_required -> auth -> auth_ok
    subscribe_events(state_changed)      # ack awaited with a timeout
    get_states                           # full resync, result awaited with a timeout
    ... events + ping/pong heartbeat until the socket drops ...

The subscription is placed **before** the snapshot so no change can fall between
them; the :class:`~battery_optimizer.service.entity_store.EntityStore` resolves the
overlap by source time (a snapshot older than an already-applied event is rejected
as out of order).  Every (re)connect repeats the full resync.  A failed or timed-out
acknowledgement, a missed pong or a dropped socket ends the session; the client
reconnects with capped exponential backoff plus jitter.  Invalid credentials are not
retried in a tight loop: they back off at the maximum delay.

Every outgoing frame passes :func:`~battery_optimizer.service.ha_guard.check_ws_frame`
inside :class:`GuardedConnection`, and the REST client checks every request in an
``httpx`` request hook, so neither adapter can send anything but the read-only
allowlist — whatever the caller asks for.  The single exception is
:meth:`HAWebSocketClient.get_weather_forecast` (``weather.get_forecasts`` with a
response, a query that changes nothing; see :mod:`~battery_optimizer.service.ha_guard`).

In ``mode: live`` only, the WebSocket client is built with an
:class:`~battery_optimizer.service.ha_guard.ActuationAllowlist` and
:meth:`HAWebSocketClient.actuate` can send the allowlisted Omnibattery writes (ADR-0007);
without one (shadow, dry_run) it raises before anything is sent.  The REST client stays
GET-only in every mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .clock import Clock
from .config import HomeAssistantSettings, Secret
from .entity_store import EntityStore, UpdateResult, parse_ha_state
from .ha_guard import (
    ActuationAllowlist,
    actuation_request,
    check_rest_request,
    check_ws_frame,
    weather_forecast_request,
)

__all__ = [
    "GuardedConnection",
    "HAAuthError",
    "HAClientStats",
    "HACommandError",
    "HAConnectionClosedError",
    "HAProtocolError",
    "HARestClient",
    "HAWebSocketClient",
    "WSConnection",
    "WSTransport",
    "WebsocketsTransport",
    "backoff_delay",
    "websocket_url",
]

log = logging.getLogger(__name__)


class HAConnectionClosedError(ConnectionError):
    """The WebSocket closed (cleanly or not)."""


class HAProtocolError(RuntimeError):
    """Home Assistant sent something the protocol does not allow at this point."""


class HAAuthError(RuntimeError):
    """``auth_invalid``: the token was rejected."""


class HACommandError(RuntimeError):
    """A command was refused (``success: false``) or not acknowledged in time."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage


class WSConnection(Protocol):
    async def send(self, text: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


class WSTransport(Protocol):
    async def connect(self, url: str, *, timeout: float) -> WSConnection: ...


class GuardedConnection:
    """Wraps a connection so every outgoing frame is checked against the allowlist."""

    def __init__(self, inner: WSConnection, actuation: ActuationAllowlist | None = None) -> None:
        self._inner = inner
        self._actuation = actuation

    async def send(self, text: str) -> None:
        check_ws_frame(text, self._actuation)
        await self._inner.send(text)

    async def recv(self) -> str:
        return await self._inner.recv()

    async def close(self) -> None:
        await self._inner.close()


class _WebsocketsConnection:
    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, text: str) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            await self._ws.send(text)
        except ConnectionClosed as exc:
            raise HAConnectionClosedError(str(exc)) from exc

    async def recv(self) -> str:
        from websockets.exceptions import ConnectionClosed

        try:
            data = await self._ws.recv()
        except ConnectionClosed as exc:
            raise HAConnectionClosedError(str(exc)) from exc
        return data.decode() if isinstance(data, bytes) else data

    async def close(self) -> None:
        await self._ws.close()


class WebsocketsTransport:
    """Real transport over the ``websockets`` library (protocol pings disabled:
    the HA-level ``ping`` exercises HA's own event loop, which is what matters)."""

    async def connect(self, url: str, *, timeout: float) -> WSConnection:
        from websockets.asyncio.client import connect

        ws = await connect(url, open_timeout=timeout, ping_interval=None, max_size=32 * 2**20)
        return _WebsocketsConnection(ws)


def websocket_url(base_url: str) -> str:
    """``https://host`` -> ``wss://host/api/websocket``."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/api/websocket"


def backoff_delay(
    attempt: int, *, initial: float, maximum: float, jitter: float, rng: random.Random
) -> float:
    """Capped exponential backoff with symmetric multiplicative jitter."""
    base = min(maximum, initial * (2 ** max(0, attempt)))
    if jitter > 0:
        base *= 1.0 + rng.uniform(-jitter, jitter)
    return max(0.0, min(base, maximum * (1.0 + jitter)))


@dataclass
class HAClientStats:
    """Monotonic counters and current state; read by the metrics collector."""

    connected: bool = False
    synced: bool = False
    connects: int = 0
    reconnects: int = 0
    resyncs: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    events_received: int = 0
    last_error: str | None = None
    last_connected_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_message_at: datetime | None = None
    next_retry_delay_s: float | None = None

    def fail(self, stage: str, error: BaseException) -> None:
        self.failures[stage] = self.failures.get(stage, 0) + 1
        self.last_error = f"{stage}: {type(error).__name__}: {error}"[:500]


class HAWebSocketClient:
    """Resident WebSocket consumer feeding an :class:`EntityStore`."""

    def __init__(
        self,
        *,
        settings: HomeAssistantSettings,
        store: EntityStore,
        clock: Clock,
        transport: WSTransport | None = None,
        on_synced: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        on_update: Callable[[UpdateResult], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        actuation: ActuationAllowlist | None = None,
    ) -> None:
        if not settings.url or not settings.token:
            raise ValueError("Home Assistant URL and token are required")
        self._url = websocket_url(settings.url)
        self._token: Secret = settings.token
        self._settings = settings
        self._store = store
        self._clock = clock
        self._transport = transport or WebsocketsTransport()
        self._on_synced = on_synced
        self._on_disconnected = on_disconnected
        self._on_update = on_update
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._actuation = actuation
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        self._subscription_id: int | None = None
        self._conn: GuardedConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.stats = HAClientStats()

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Connect, resync and consume until :meth:`stop`; reconnect on any failure."""
        attempt = 0
        while not self._stopping.is_set():
            stage = "connect"
            try:
                conn = await asyncio.wait_for(
                    self._transport.connect(self._url, timeout=self._settings.request_timeout_s),
                    timeout=self._settings.request_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any connect failure is retried
                self.stats.fail(stage, exc)
            else:
                synced_before = self.stats.resyncs
                try:
                    await self._session(GuardedConnection(conn, self._actuation))
                except asyncio.CancelledError:
                    raise
                except HAAuthError as exc:
                    self.stats.fail("auth", exc)
                    attempt = max(attempt, 30)  # back off at the cap, never hammer
                except HACommandError as exc:
                    self.stats.fail(exc.stage, exc)
                except HAConnectionClosedError as exc:
                    self.stats.fail("disconnect", exc)
                except (HAProtocolError, TimeoutError, OSError, ValueError) as exc:
                    self.stats.fail("protocol", exc)
                finally:
                    self._end_session()
                    with contextlib.suppress(Exception):
                        await conn.close()
                if self.stats.resyncs > synced_before:
                    attempt = 0  # the session got fully in sync: start backoff over
            if self._stopping.is_set():
                break
            delay = backoff_delay(
                attempt,
                initial=self._settings.reconnect_initial_delay_s,
                maximum=self._settings.reconnect_max_delay_s,
                jitter=self._settings.reconnect_jitter_frac,
                rng=self._rng,
            )
            attempt += 1
            self.stats.next_retry_delay_s = delay
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            self.stats.reconnects += 1

    async def stop(self) -> None:
        self._stopping.set()

    # -- session -------------------------------------------------------------

    def _end_session(self) -> None:
        was_connected = self.stats.connected
        self.stats.connected = False
        self.stats.synced = False
        self._subscription_id = None
        self._conn = None
        self._reader_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HAConnectionClosedError("session ended"))
                future.exception()  # mark retrieved
        self._pending.clear()
        if was_connected and self._on_disconnected is not None:
            self._on_disconnected()

    async def _recv_json(self, conn: GuardedConnection) -> Any:
        text = await asyncio.wait_for(conn.recv(), timeout=self._settings.request_timeout_s)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise HAProtocolError("non-JSON frame from Home Assistant") from exc

    async def _session(self, conn: GuardedConnection) -> None:
        hello = await self._recv_json(conn)
        if not isinstance(hello, Mapping) or hello.get("type") != "auth_required":
            raise HAProtocolError(f"expected auth_required, got {_type_of(hello)}")
        await conn.send(json.dumps({"type": "auth", "access_token": self._token.reveal()}))
        reply = await self._recv_json(conn)
        reply_type = reply.get("type") if isinstance(reply, Mapping) else None
        if reply_type == "auth_invalid":
            raise HAAuthError("Home Assistant rejected the token (auth_invalid)")
        if reply_type != "auth_ok":
            raise HAProtocolError(f"expected auth_ok, got {_type_of(reply)}")
        self.stats.connected = True
        self.stats.connects += 1
        self.stats.last_connected_at = self._clock.now()

        reader = asyncio.create_task(self._reader(conn), name="ha-reader")
        heartbeat: asyncio.Task[None] | None = None
        try:
            sub_id = next(self._ids)
            self._subscription_id = sub_id
            await self._request(
                conn,
                {"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"},
                stage="subscribe",
                reader=reader,
            )
            states = await self._request(
                conn,
                {"id": next(self._ids), "type": "get_states"},
                stage="get_states",
                reader=reader,
            )
            if not isinstance(states, list):
                raise HACommandError("get_states", "result is not a list")
            self._apply_snapshot(states)
            self.stats.synced = True
            self.stats.resyncs += 1
            self.stats.last_synced_at = self._clock.now()
            self._conn, self._reader_task = conn, reader
            if self._on_synced is not None:
                self._on_synced()
            heartbeat = asyncio.create_task(self._heartbeat(conn, reader), name="ha-heartbeat")
            done, _ = await asyncio.wait({reader, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raise the reason the session ended
            raise HAConnectionClosedError("session ended")
        finally:
            for task in (reader, heartbeat):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    async def _request(
        self,
        conn: GuardedConnection,
        message: dict[str, Any],
        *,
        stage: str,
        reader: asyncio.Task[None],
        timeout: float | None = None,
    ) -> Any:
        """Send one command and wait for its ``result``/``pong`` (or fail)."""
        msg_id = int(message["id"])
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await conn.send(json.dumps(message))
            wait = timeout or self._settings.request_timeout_s
            done, _ = await asyncio.wait(
                {future, reader}, timeout=wait, return_when=asyncio.FIRST_COMPLETED
            )
            if future in done:
                reply = future.result()
            elif reader in done:
                reader.result()
                raise HAConnectionClosedError(f"{stage}: reader stopped")
            else:
                raise HACommandError(stage, f"no acknowledgement within {wait:.1f}s")
        finally:
            self._pending.pop(msg_id, None)
        if reply.get("type") == "pong":
            return None
        if reply.get("success") is not True:
            error = reply.get("error") or {}
            raise HACommandError(stage, f"refused: {error.get('code')}: {error.get('message')}")
        return reply.get("result")

    async def get_weather_forecast(
        self, entity_id: str, forecast_type: str = "hourly"
    ) -> list[Any]:
        """``weather.get_forecasts`` over the live session: the raw forecast list.

        The one permitted service call (see :mod:`~battery_optimizer.service.ha_guard`).
        Raises :class:`HACommandError` when there is no synced session, HA refuses the
        call or the response lacks a forecast list for *entity_id*;
        :class:`HAConnectionClosedError` when the session ends meanwhile.
        """
        conn, reader = self._conn, self._reader_task
        if conn is None or reader is None or not self.stats.synced:
            raise HACommandError("weather_forecast", "no synced Home Assistant session")
        message = weather_forecast_request(next(self._ids), entity_id, forecast_type)
        try:
            result = await self._request(conn, message, stage="weather_forecast", reader=reader)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # we are being cancelled
            raise HAConnectionClosedError("weather_forecast: session ended") from None
        response = result.get("response") if isinstance(result, Mapping) else None
        entry = response.get(entity_id) if isinstance(response, Mapping) else None
        forecast = entry.get("forecast") if isinstance(entry, Mapping) else None
        if not isinstance(forecast, list):
            raise HACommandError("weather_forecast", f"no forecast list for {entity_id}")
        return forecast

    async def actuate(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: Mapping[str, Any] | None = None,
    ) -> None:
        """One allowlisted live write over the synced session (``mode: live`` only).

        Raises :class:`~battery_optimizer.service.ha_guard.ForbiddenHACallError` when the
        frame is outside the allowlist (always, without one), :class:`HACommandError` when
        there is no synced session or HA refuses or does not acknowledge the call, and
        :class:`HAConnectionClosedError` when the session ends meanwhile.  Success means HA
        executed the service, not that the entity (or the battery) reached the new state.
        """
        message = actuation_request(
            next(self._ids), domain, service, entity_id, service_data, self._actuation
        )
        conn, reader = self._conn, self._reader_task
        if conn is None or reader is None or not self.stats.synced:
            raise HACommandError("actuation", "no synced Home Assistant session")
        try:
            await self._request(conn, message, stage="actuation", reader=reader)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            raise HAConnectionClosedError("actuation: session ended") from None

    async def _reader(self, conn: GuardedConnection) -> None:
        while True:
            text = await conn.recv()
            self.stats.last_message_at = self._clock.now()
            try:
                payload = json.loads(text)
            except ValueError as exc:
                raise HAProtocolError("non-JSON frame from Home Assistant") from exc
            for message in payload if isinstance(payload, list) else [payload]:
                if isinstance(message, Mapping):
                    self._dispatch(message)

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        msg_type = message.get("type")
        msg_id = message.get("id")
        if msg_type in ("result", "pong") and isinstance(msg_id, int):
            future = self._pending.get(msg_id)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if msg_type == "event" and msg_id == self._subscription_id:
            event = message.get("event") or {}
            if event.get("event_type") != "state_changed":
                return
            new_state = (event.get("data") or {}).get("new_state")
            if not isinstance(new_state, Mapping):
                return  # entity removed; the next resync reflects it
            self.stats.events_received += 1
            self._apply_one(new_state)

    def _apply_one(self, payload: Mapping[str, Any]) -> None:
        if payload.get("entity_id") not in self._store.tracked:
            return
        try:
            state = parse_ha_state(payload, received_at=self._clock.now())
        except ValueError as exc:
            log.warning("ignoring malformed state for %s: %s", payload.get("entity_id"), exc)
            return
        result = self._store.apply(state)
        if self._on_update is not None:
            self._on_update(result)

    def _apply_snapshot(self, states: list[Any]) -> None:
        for payload in states:
            if isinstance(payload, Mapping):
                self._apply_one(payload)

    async def _heartbeat(self, conn: GuardedConnection, reader: asyncio.Task[None]) -> None:
        while True:
            await self._sleep(self._settings.heartbeat_interval_s)
            await self._request(
                conn,
                {"id": next(self._ids), "type": "ping"},
                stage="heartbeat",
                reader=reader,
                timeout=self._settings.heartbeat_timeout_s,
            )


def _type_of(message: Any) -> str:
    if isinstance(message, Mapping):
        return repr(message.get("type"))
    return type(message).__name__


class HARestClient:
    """GET-only REST client; any other method or path is refused by a request hook."""

    def __init__(
        self,
        *,
        settings: HomeAssistantSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.url or not settings.token:
            raise ValueError("Home Assistant URL and token are required")
        self._client = httpx.AsyncClient(
            base_url=settings.url,
            headers={"Authorization": f"Bearer {settings.token.reveal()}"},
            timeout=settings.request_timeout_s,
            transport=transport,
            event_hooks={"request": [self._guard]},
            follow_redirects=False,
        )

    @staticmethod
    async def _guard(request: httpx.Request) -> None:
        check_rest_request(request.method, request.url.path)

    async def _get(self, path: str) -> httpx.Response:
        return await self._client.get(path)

    async def get_state(self, entity_id: str) -> Mapping[str, Any] | None:
        """``GET /api/states/<entity_id>``; ``None`` when HA does not know it."""
        response = await self._get(f"/api/states/{quote(entity_id, safe='._')}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, Mapping) else None

    async def get_config(self) -> Mapping[str, Any]:
        response = await self._get("/api/config")
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
