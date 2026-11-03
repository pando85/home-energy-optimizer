"""Guard for every byte the service sends to Home Assistant.

In ``shadow`` and ``dry_run`` the service must be **unable** to operate a device, write
a state, fire an event or change configuration — not merely unwilling.  So the
allowlist is enforced at the lowest layer that sends anything:

* WebSocket: every outgoing frame is parsed and its ``type`` checked against
  :data:`ALLOWED_WS_TYPES` (authentication, heartbeat, subscriptions and reads).
  ``fire_event``, ``execute_script``, config writes and every other type raise
  :class:`ForbiddenHACallError` before the frame reaches the socket.
  ``subscribe_events`` is further restricted to ``state_changed``.
* ``call_service`` is refused, with **one** exception: ``weather.get_forecasts``
  with ``return_response: true`` (:func:`check_weather_forecast_call`).  It is a
  query, not an action: the service is registered ``SupportsResponse.ONLY``, so HA
  rejects it without a response and it only returns the forecast the weather
  entity already holds; it changes no state and operates no device.  The frame is
  accepted only when it is exactly::

      {"id": <int>, "type": "call_service", "domain": "weather",
       "service": "get_forecasts", "return_response": true,
       "service_data": {"type": "hourly" | "daily" | "twice_daily"},
       "target": {"entity_id": "weather.<name>" | ["weather.<name>", ...]}}

  Any other domain, service or ``return_response``, any additional field, and any
  entity outside the ``weather.`` domain raise.  The only caller is the passive
  forecast archive, which builds its frame with :func:`weather_forecast_request`
  (so no other module needs the literal service-call frame).
* REST: only ``GET`` on ``/api/``, ``/api/config``, ``/api/states`` and
  ``/api/states/<entity_id>``.  ``POST``/``PUT``/``DELETE`` and any other path raise.

Live mode (ADR-0007) adds **one** narrow write path, and only when the caller hands the
guard an :class:`ActuationAllowlist` — the runtime builds one only for ``mode: live``
(:func:`actuation_allowlist`); ``shadow`` and ``dry_run`` pass none, so every write is
refused exactly as before.  With an allowlist, a ``call_service`` frame passes only when it
is exactly::

    {"id": <int>, "type": "call_service", "domain": <d>, "service": <s>,
     "target": {"entity_id": "<one entity>"}, "service_data": {...}}

with ``(<d>, <s>)`` one of :data:`ACTUATION_SERVICES`, one single entity (in ``target`` or
``service_data``, never a list) that the allowlist names *for that service*, and
``service_data`` holding only what that service needs: nothing for ``switch.turn_on/off``,
a finite ``value`` inside the entity's configured bounds for ``number.set_value`` (so the
Omnibattery caps can never receive the ``0`` that means *unlimited*, and the grid-import
target can never exceed the import cap), a finite positive ``timestamp`` for
``input_datetime.set_datetime`` (the heartbeat).  ``return_response`` and any other field
raise.  The only builder is :func:`actuation_request`.

The guard holds no credentials and does no I/O; the transports call it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ACTUATION_SERVICES",
    "ALLOWED_REST_METHODS",
    "ALLOWED_SUBSCRIBE_EVENT_TYPES",
    "ALLOWED_WEATHER_FORECAST_TYPES",
    "ALLOWED_WS_TYPES",
    "ActuationAllowlist",
    "ForbiddenHACallError",
    "actuation_allowlist",
    "actuation_request",
    "check_actuation_call",
    "check_rest_request",
    "check_weather_forecast_call",
    "check_ws_frame",
    "check_ws_message",
    "weather_forecast_request",
]

#: The complete set of WebSocket message types the service may send.
ALLOWED_WS_TYPES: frozenset[str] = frozenset(
    {
        "auth",
        "ping",
        "subscribe_events",
        "subscribe_entities",
        "subscribe_trigger",
        "unsubscribe_events",
        "get_states",
        "get_config",
    }
)

#: ``subscribe_events`` may only follow state changes.
ALLOWED_SUBSCRIBE_EVENT_TYPES: frozenset[str] = frozenset({"state_changed"})

#: ``weather.get_forecasts`` forecast types (the service's ``type`` field).
ALLOWED_WEATHER_FORECAST_TYPES: frozenset[str] = frozenset({"hourly", "daily", "twice_daily"})

ALLOWED_REST_METHODS: frozenset[str] = frozenset({"GET"})

_WEATHER_CALL_KEYS = frozenset(
    {"id", "type", "domain", "service", "service_data", "target", "return_response"}
)
_WEATHER_ENTITY = re.compile(r"^weather\.[a-z0-9_]+$")
_MAX_WEATHER_ENTITIES = 8

_REST_PATH = re.compile(r"^/api/(?:config|states(?:/[a-z_]+\.[A-Za-z0-9_]+)?)?$")


class ForbiddenHACallError(PermissionError):
    """An attempt to send something outside the read-only allowlist."""


def _weather_entity_ids(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    ids = value if isinstance(value, list) else [value]
    if not ids or len(ids) > _MAX_WEATHER_ENTITIES:
        raise ForbiddenHACallError(f"weather.get_forecasts: {where}.entity_id has a bad length")
    for entity_id in ids:
        if not isinstance(entity_id, str) or not _WEATHER_ENTITY.match(entity_id):
            raise ForbiddenHACallError(
                f"weather.get_forecasts: {where}.entity_id {entity_id!r} is not a weather entity"
            )
    return list(ids)


def check_weather_forecast_call(message: Mapping[str, Any]) -> None:
    """Raise unless *message* is exactly the one allowed ``call_service`` frame.

    Only ``weather.get_forecasts`` with ``return_response: true``, a ``type`` in
    :data:`ALLOWED_WEATHER_FORECAST_TYPES` and ``weather.*`` entities (in ``target``
    or ``service_data``) passes.  Everything else, including any extra field, raises.
    """
    extra = sorted(str(k) for k in set(message) - _WEATHER_CALL_KEYS)
    if extra:
        raise ForbiddenHACallError(f"call_service: unexpected field(s) {extra}")
    if message.get("domain") != "weather" or message.get("service") != "get_forecasts":
        raise ForbiddenHACallError(
            f"call_service {message.get('domain')!r}.{message.get('service')!r} is not allowed: "
            "the only permitted service call is weather.get_forecasts"
        )
    if message.get("return_response") is not True:
        raise ForbiddenHACallError("weather.get_forecasts is only allowed with return_response")
    if "id" in message and (isinstance(message["id"], bool) or not isinstance(message["id"], int)):
        raise ForbiddenHACallError("weather.get_forecasts: id must be an integer")
    service_data = message.get("service_data")
    if not isinstance(service_data, Mapping) or not set(service_data) <= {"type", "entity_id"}:
        raise ForbiddenHACallError(
            "weather.get_forecasts: service_data may only contain type and entity_id"
        )
    if service_data.get("type") not in ALLOWED_WEATHER_FORECAST_TYPES:
        raise ForbiddenHACallError(
            f"weather.get_forecasts: type must be one of {sorted(ALLOWED_WEATHER_FORECAST_TYPES)}"
        )
    target = message.get("target", {})
    if not isinstance(target, Mapping) or not set(target) <= {"entity_id"}:
        raise ForbiddenHACallError("weather.get_forecasts: target may only contain entity_id")
    entities = _weather_entity_ids(service_data.get("entity_id"), "service_data")
    entities += _weather_entity_ids(target.get("entity_id"), "target")
    if not entities:
        raise ForbiddenHACallError("weather.get_forecasts: no weather entity named")


def weather_forecast_request(
    msg_id: int, entity_id: str, forecast_type: str = "hourly"
) -> dict[str, Any]:
    """The one permitted ``call_service`` frame (validated before it is returned)."""
    message: dict[str, Any] = {
        "id": msg_id,
        "type": "call_service",
        "domain": "weather",
        "service": "get_forecasts",
        "service_data": {"type": forecast_type},
        "target": {"entity_id": entity_id},
        "return_response": True,
    }
    check_ws_message(message)
    return message


#: The complete set of services the live write path may call.
ACTUATION_SERVICES: frozenset[tuple[str, str]] = frozenset(
    {
        ("switch", "turn_on"),
        ("switch", "turn_off"),
        ("number", "set_value"),
        ("input_datetime", "set_datetime"),
    }
)

_ACTUATION_CALL_KEYS = frozenset({"id", "type", "domain", "service", "service_data", "target"})
_ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


@dataclass(frozen=True)
class ActuationAllowlist:
    """The entities the live writer may operate, per service.

    ``switches``: ``switch.turn_on``/``turn_off``; ``numbers``: ``number.set_value`` with
    inclusive ``(low, high)`` bounds; ``datetimes``: ``input_datetime.set_datetime``.
    """

    switches: frozenset[str] = frozenset()
    numbers: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    datetimes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for domain, entities in (
            ("switch", self.switches),
            ("number", self.numbers),
            ("input_datetime", self.datetimes),
        ):
            for entity in entities:
                if not isinstance(entity, str) or not _ENTITY.match(entity):
                    raise ValueError(f"actuation allowlist: bad entity ID {entity!r}")
                if not entity.startswith(f"{domain}."):
                    raise ValueError(f"actuation allowlist: {entity} is not a {domain} entity")
        for entity, (low, high) in self.numbers.items():
            if not (math.isfinite(low) and math.isfinite(high) and low <= high):
                raise ValueError(f"actuation allowlist: bad bounds for {entity}: {low}..{high}")


def actuation_allowlist(config: Any) -> ActuationAllowlist | None:
    """The allowlist for a :class:`~.config.ServiceConfig`; ``None`` unless ``mode: live``.

    The two direction switches and the power-limit switch; the PD grid target bounded to
    ``[0, import_cap_w]`` (never an export target, never above the cap); the system
    discharge cap bounded to ``[min_discharge_cap_w, max_discharge_cap_w]`` (never 0, which
    Omnibattery reads as *unlimited*); the heartbeat helper.  Nothing else — in particular
    not the kill switch, which only the owner flips.
    """
    if getattr(config, "mode", None) != "live":
        return None
    act = config.actuation
    ents = act.entities
    return ActuationAllowlist(
        switches=frozenset({ents.allow_charge, ents.allow_discharge, ents.power_limits}),
        numbers={
            ents.pd_target: (0.0, float(act.import_cap_w)),
            ents.max_discharge_power: (
                float(act.min_discharge_cap_w),
                float(act.max_discharge_cap_w),
            ),
        },
        datetimes=frozenset({ents.heartbeat}),
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def check_actuation_call(message: Mapping[str, Any], allowlist: ActuationAllowlist) -> None:
    """Raise unless *message* is one allowlisted write on one allowlisted entity."""
    extra = sorted(str(k) for k in set(message) - _ACTUATION_CALL_KEYS)
    if extra:
        raise ForbiddenHACallError(f"call_service: unexpected field(s) {extra}")
    domain, service = message.get("domain"), message.get("service")
    if (domain, service) not in ACTUATION_SERVICES:
        raise ForbiddenHACallError(f"call_service {domain!r}.{service!r} is not allowed")
    if "id" in message and (isinstance(message["id"], bool) or not isinstance(message["id"], int)):
        raise ForbiddenHACallError("call_service: id must be an integer")
    target = message.get("target", {})
    service_data = message.get("service_data", {})
    if not isinstance(target, Mapping) or not set(target) <= {"entity_id"}:
        raise ForbiddenHACallError("call_service: target may only contain entity_id")
    if not isinstance(service_data, Mapping):
        raise ForbiddenHACallError("call_service: service_data must be an object")
    entities = [m["entity_id"] for m in (target, service_data) if "entity_id" in m]
    if len(entities) != 1 or not isinstance(entities[0], str):
        raise ForbiddenHACallError("call_service: exactly one entity_id (a string) is required")
    entity = entities[0]
    data = {k: v for k, v in service_data.items() if k != "entity_id"}
    if domain == "switch":
        if entity not in allowlist.switches:
            raise ForbiddenHACallError(f"switch.{service}: {entity!r} is not allowlisted")
        if data:
            raise ForbiddenHACallError(f"switch.{service}: no service_data allowed")
    elif domain == "number":
        if entity not in allowlist.numbers:
            raise ForbiddenHACallError(f"number.set_value: {entity!r} is not allowlisted")
        if set(data) != {"value"}:
            raise ForbiddenHACallError("number.set_value: service_data must be exactly value")
        value = _finite_number(data["value"])
        low, high = allowlist.numbers[entity]
        if value is None or not low <= value <= high:
            raise ForbiddenHACallError(
                f"number.set_value: {entity} value {data['value']!r} outside [{low}, {high}]"
            )
    else:  # input_datetime.set_datetime: the heartbeat
        if entity not in allowlist.datetimes:
            raise ForbiddenHACallError(f"input_datetime.set_datetime: {entity!r} not allowlisted")
        if set(data) != {"timestamp"}:
            raise ForbiddenHACallError(
                "input_datetime.set_datetime: service_data must be exactly timestamp"
            )
        stamp = _finite_number(data["timestamp"])
        if stamp is None or stamp <= 0:
            raise ForbiddenHACallError("input_datetime.set_datetime: bad timestamp")


def actuation_request(
    msg_id: int,
    domain: str,
    service: str,
    entity_id: str,
    service_data: Mapping[str, Any] | None,
    allowlist: ActuationAllowlist | None,
) -> dict[str, Any]:
    """Build one live write frame and validate it against *allowlist* before returning it."""
    message: dict[str, Any] = {
        "id": msg_id,
        "type": "call_service",
        "domain": domain,
        "service": service,
        "target": {"entity_id": entity_id},
    }
    if service_data:
        message["service_data"] = dict(service_data)
    check_ws_message(message, allowlist)
    return message


def check_ws_message(
    message: Mapping[str, Any], actuation: ActuationAllowlist | None = None
) -> None:
    """Raise :class:`ForbiddenHACallError` unless *message* is an allowed command.

    Without *actuation* (shadow, dry_run) only read-only commands and the weather query
    pass; with it (live) also the allowlisted writes of :func:`check_actuation_call`.
    """
    if not isinstance(message, Mapping):
        raise ForbiddenHACallError(
            f"WebSocket message must be an object, got {type(message).__name__}"
        )
    msg_type = message.get("type")
    if msg_type == "call_service":
        if actuation is not None and message.get("domain") != "weather":
            check_actuation_call(message, actuation)  # live mode only
            return
        check_weather_forecast_call(message)  # the single, narrow, non-mutating exception
        return
    if msg_type not in ALLOWED_WS_TYPES:
        raise ForbiddenHACallError(
            f"WebSocket message type {msg_type!r} is not in the read-only allowlist "
            f"{sorted(ALLOWED_WS_TYPES)}: the shadow service has no actuation path"
        )
    if msg_type == "subscribe_events":
        event_type = message.get("event_type")
        if event_type not in ALLOWED_SUBSCRIBE_EVENT_TYPES:
            raise ForbiddenHACallError(
                f"subscribe_events for event_type={event_type!r} is not allowed; only "
                f"{sorted(ALLOWED_SUBSCRIBE_EVENT_TYPES)}"
            )
    if msg_type == "subscribe_trigger":
        trigger = message.get("trigger")
        triggers = trigger if isinstance(trigger, list) else [trigger]
        for item in triggers:
            platform = (
                item.get("platform", item.get("trigger")) if isinstance(item, Mapping) else None
            )
            if platform != "state":
                raise ForbiddenHACallError("subscribe_trigger is limited to state triggers")


def check_ws_frame(frame: str | bytes, actuation: ActuationAllowlist | None = None) -> None:
    """Check a serialized outgoing frame (a JSON object or a coalesced JSON array)."""
    try:
        payload = json.loads(frame)
    except (TypeError, ValueError) as exc:
        raise ForbiddenHACallError("outgoing WebSocket frame is not JSON") from exc
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        check_ws_message(message, actuation)


def check_rest_request(method: str, path: str) -> None:
    """Raise :class:`ForbiddenHACallError` unless this is a GET of an allowed read path."""
    if method.upper() not in ALLOWED_REST_METHODS:
        raise ForbiddenHACallError(f"REST method {method!r} is not allowed: GET only")
    if not _REST_PATH.match(path):
        raise ForbiddenHACallError(f"REST path {path!r} is not in the read-only allowlist")
