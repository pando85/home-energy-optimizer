"""Live climate control through the heat pump's Home Assistant entities (ADR-0009).

The optimizer is the only writer of space heating: the operating mode (``Heat+DHW`` heating on,
``DHW`` off) and the Z1 water target.  Each tick (``climate_actuation.tick_interval_s``, 60 s by
default, plus a wake on a new plan or a change of the owner's select or the legacy toggle) the
actuator takes the 15-minute slot of the climate shadow's latest plan **solved by this process** and
turns it into a desired state:

* the plan's slot: heating on at T degC -> ``Heat+DHW`` and water T (integer, 20 to 35); off ->
  ``DHW`` and the water target untouched;
* a comfort room below its floor forces ``Heat+DHW`` whatever the plan says (decision 6), exempt
  from the minimum off, released at the floor plus ``floor_margin_c``;
* any of a stale, missing, invalid or foreign plan, a failed solve, an observer without state, no
  usable room reading or a mode sensor that cannot be read is a **fallback**: the safe heating
  state (``Heat+DHW`` and ``safe_water_target_c``, water only in direct mode) is written once, then
  nothing is written until a valid plan returns (decision 7).  The worst case is extra kWh, never
  no heating.
* **Vacation (ADR-0010).**  While the owner's window is in ``[start, ready_at)`` every room's
  floor is the building guard ``climate_shadow.vacation.guard_c`` (a room below it forces
  ``Heat+DHW``, counted as a guard override); from ``ready_at`` the normal floors.  Inside
  ``[start, ready_at - lookahead_hours)`` a fallback caused by the plan (none, stale, failed,
  invalid, no observer state) is a **vacation hold** instead: heating off (``DHW``), the guard still
  enforced, no safe state.  A room reading or mode sensor that is missing keeps the normal
  fallback, and from ``ready_at - lookahead_hours`` on the normal fallback applies again.
* A Home Assistant restart is not a loss of control: after the session drops and syncs again, or
  while the mode sensor is unavailable (the room readings too, right after a restart), the actuator
  **holds** (writes nothing but the heartbeat, guard reason ``restart_grace``) for
  ``ha_restart_grace_s`` (600 s by default) and only then falls back, if the condition persists.
  A write that fails because the mode sensor or the session went away is held the same way, and so
  is an owner select that is unavailable or missing (an explicit ``shadow``/``off`` is immediate).
  The heartbeat continues while the session is synced: the optimizer is alive and holding on
  purpose, so the HA watchdog acts only if HA is really down.

Writes are possible only when the deployed ``climate_actuation.mode`` is ``live`` (``dry_run`` runs
all of this and records the writes it would make, sending nothing) **and** the owner's
``input_select.climate_optimizer_mode`` reads ``live`` **and** the legacy controller's
``input_boolean.appdaemon_climate_enable`` is not ``on``.  While the legacy controller is active the
actuator writes nothing at all (no plan write, no safe state, no heartbeat: one writer only); the
select leaving ``live`` writes the safe state once and then nothing, heartbeat included.

Machine limits are code, not settings: the heat-mode command keeps the compressor's **minimum on
30 min** and **minimum off 20 min** (clocks from the observed ``sensor.aquarea_mode`` changes, so
they survive a restart; the safe state and a floor breach are exempt from the minimum off only);
the water target is written only in direct mode (``sensor.aquarea_heating_mode`` = 1), as an
integer clamped to 20 to 35, on a change of at least 1 K and at most once per 15 min (the safe
state is exempt from the 15 minutes); while heating already runs it moves only when the desired
target differs by at least 1 K plus ``water_hysteresis_k`` (1 K, so 2 K), which keeps a plan that
flip-flops by 1 K between replans from writing, and a start of heating writes the desired value
exactly; at least 10 s separate two writes to one entity; a payload that is not a finite integer
in range never leaves the process.  Never DHW, the heater, boost,
sterilization, cooling or a thermostat setpoint.

Writes are ordered restrictive first: turning heating **off** writes the mode; turning it **on**
writes the water target, then the mode.  Each write is confirmed by reading the unit's own state
back (``sensor.aquarea_mode`` for the mode, ``sensor.aquarea_z1_heat_request_temperature`` for the
water request) within ``readback_timeout_s``; an unconfirmed write is retried up to
``max_attempts``, a guard refusal never.  Every tick that ends reconciled (the desired state, or at
least the safe state, confirmed) writes the heartbeat helper that an HA watchdog checks.

The actuator never builds a raw HA frame: sends go through the runtime to
:meth:`~.ha_client.HAWebSocketClient.actuate`, which :mod:`.ha_guard` checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from battery_optimizer.climate import vacation as vac

from .clock import Clock
from .config import ClimateActuationSettings
from .entity_store import EntityState, EntityStore, parse_ha_state
from .ha_guard import (
    CLIMATE_HEAT_MODE_OPTIONS,
    CLIMATE_WATER_RANGE_C,
    ForbiddenHACallError,
)

__all__ = [
    "ACTUATOR_MODES",
    "FALLBACK_REASONS",
    "GUARD_REASONS",
    "HEAT_OFF",
    "HEAT_ON",
    "HOLD_REASONS",
    "MIN_OFF_S",
    "MIN_ON_S",
    "OWNER_STATES",
    "WATER_MIN_INTERVAL_S",
    "WRITE_FAILURE_REASONS",
    "WRITE_ROLES",
    "ClimateActuator",
    "ClimateContext",
    "Desired",
    "Write",
]

log = logging.getLogger(__name__)

ACTUATOR_MODES: tuple[str, ...] = ("dry_run", "live")

#: The two heat-mode options the actuator writes: space heating on (with hot water) and off.
HEAT_ON = "Heat+DHW"
HEAT_OFF = "DHW"
assert frozenset({HEAT_ON, HEAT_OFF}) == CLIMATE_HEAT_MODE_OPTIONS

#: Compressor minimum on and minimum off, applied to the heat-mode command (ADR-0009 decision 5).
MIN_ON_S = 30 * 60.0
MIN_OFF_S = 20 * 60.0
#: The water target moves only by at least this many K and at most this often.
WATER_MIN_STEP_K = 1.0
WATER_MIN_INTERVAL_S = 15 * 60.0
#: Least time between two writes to one entity (the HA automations mute their mirror for 5 s).
MIN_WRITE_GAP_S = 10.0
#: The per-room comfort-floor helper is used when available and in this range (degC); it is
#: static owner configuration that never re-reports, so its age is not tested.
FLOOR_HELPER_RANGE_C = (18.0, 23.0)
#: A thermostat reading outside this range is a sensor fault, not a temperature.
_ROOM_PLAUSIBLE_C = (-10.0, 50.0)
_READBACK_POLL_S = 0.25
_READBACK_REFRESH_EVERY = 8  # polls: one REST GET of the entity every 2 s while waiting
_RETRY_PAUSE_S = 1.0

#: The guards that stop every write (fixed label set), most urgent first.
GUARD_REASONS: tuple[str, ...] = (
    "ha_not_synced",
    "legacy_writer_active",
    "select_not_live",
    "restart_grace",
)
#: The fallbacks a Home Assistant restart produces: held for the grace period, not written at once.
_RESTART_REASONS = frozenset({"no_room_reading", "mode_sensor_unavailable"})
#: Every reason for the safe heating state (fixed label set).
FALLBACK_REASONS: tuple[str, ...] = (
    "no_plan",
    "plan_not_from_this_process",
    "plan_expired",
    "plan_invalid",
    "plan_failed",
    "observer_no_state",
    "no_room_reading",
    "mode_sensor_unavailable",
    "select_left_live",
    "write_failed",
    "shutdown",
)
#: Why a plan-driven mode or water write was held back by a machine limit.
HOLD_REASONS: tuple[str, ...] = ("min_on", "min_off", "water_interval")
WRITE_ROLES: tuple[str, ...] = ("heat_mode", "water_target")
WRITE_FAILURE_REASONS: tuple[str, ...] = ("ha_error", "readback_timeout", "forbidden")
#: Values of the owner-select label on the metrics (anything else is ``other``).
OWNER_STATES: tuple[str, ...] = ("live", "shadow", "off", "other", "missing")

Sender = Callable[[str, str, str, Mapping[str, Any] | None], Awaitable[None]]
Refresher = Callable[[tuple[str, ...]], Awaitable[None]]
Recorder = Callable[[datetime, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ClimateContext:
    """What the runtime knows at one tick (the actuator reads Home Assistant itself)."""

    now: datetime
    ha_synced: bool
    #: The climate shadow's latest ClimatePlan (contract v1), or ``None`` before the first.
    plan: Mapping[str, Any] | None
    #: The plan was solved by this process (never one rebuilt from the store at start).
    plan_own: bool
    #: The status of the decision record behind the plan (``planned``, ``no_state``, ``no_inputs``,
    #: ``plan_failed``, ``error``): what names the fallback.
    plan_status: str | None


@dataclass(frozen=True)
class Desired:
    """The heating state one tick wants (``water_c`` ``None``: the water target is left alone)."""

    #: ``plan`` (the slot), ``floor`` (a room below its floor) or ``safe_state``.
    source: str
    reason: str
    heating: bool
    water_c: int | None = None
    plan_id: str | None = None
    #: Exempt from the minimum off (the safe state and a floor breach).
    exempt_min_off: bool = False
    #: Exempt from the 15-minute water limit (the safe state only).
    exempt_water_limit: bool = False
    rooms: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reason": self.reason,
            "heating": self.heating,
            "water_c": self.water_c,
            "plan_id": self.plan_id,
            "rooms": list(self.rooms),
        }


@dataclass(frozen=True)
class Write:
    """One HA service call on one entity."""

    role: str
    entity_id: str
    domain: str
    service: str
    #: ``input_select.select_option`` only.
    option: str | None = None
    #: ``input_number.set_value``: an integer degC; ``input_datetime.set_datetime``: a timestamp.
    value: float | None = None
    reason: str = ""
    old: Any = None
    plan_id: str | None = None

    @property
    def service_data(self) -> dict[str, Any]:
        if self.domain == "input_select":
            return {"option": self.option}
        if self.domain == "input_number":
            return {"value": self.value}
        return {"timestamp": self.value}

    def validate(self) -> None:
        """Raise :class:`ForbiddenHACallError` unless the payload may leave the process (an empty or
        garbage water payload would become 0 degC in the unit)."""
        if self.domain == "input_select":
            if self.option not in CLIMATE_HEAT_MODE_OPTIONS:
                raise ForbiddenHACallError(f"climate actuator: option {self.option!r} refused")
        elif self.domain == "input_number":
            low, high = CLIMATE_WATER_RANGE_C
            value = self.value
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ForbiddenHACallError(f"climate actuator: water value {value!r} refused")
        elif self.domain == "input_datetime":
            value = self.value
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            if not ok or not math.isfinite(value) or value <= 0:
                raise ForbiddenHACallError(f"climate actuator: timestamp {value!r} refused")
        else:
            raise ForbiddenHACallError(f"climate actuator: domain {self.domain!r} refused")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "entity_id": self.entity_id,
            "service": f"{self.domain}.{self.service}",
            "old": self.old,
            "reason": self.reason,
        }
        if self.option is not None:
            out["new"] = self.option
        elif self.role != "heartbeat":
            out["new"] = self.value
        return out


@dataclass(frozen=True)
class _Plan:
    """The validated slot of the plan in force."""

    plan_id: str
    heating: bool
    water_c: int | None


@dataclass(frozen=True)
class _Readings:
    """What the tick reads from the entity store."""

    owner: str | None
    legacy_on: bool
    mode: str | None  # "on" (Heat+DHW), "off" (DHW), "other", or None (unreadable)
    mode_raw: str | None
    mode_since_s: float | None
    direct: bool | None  # Heating_Mode = 1; None: sensor unavailable
    water: float | None
    rooms: Mapping[str, float | None]  # usable current temperature per comfort room
    floors: Mapping[str, float]
    #: The owner's valid vacation window (ADR-0010), ``None`` without one.
    vacation: vac.VacationWindow | None = None
    #: Inside ``[start, ready_at)``: the floors above are the building guard.
    absent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_select": self.owner,
            "legacy_enable": self.legacy_on,
            "observed_mode": self.mode_raw,
            "observed_mode_since_s": None
            if self.mode_since_s is None
            else round(self.mode_since_s, 1),
            "direct_mode": self.direct,
            "observed_water_c": self.water,
            "rooms": dict(self.rooms),
            "floors": dict(self.floors),
            "vacation": self.vacation.to_dict() if self.vacation is not None else None,
            "vacation_absent": self.absent,
        }


class ClimateActuator:
    """The single writer of the heat pump's operating mode and water target (ADR-0009)."""

    def __init__(
        self,
        *,
        settings: ClimateActuationSettings,
        owner_entity: str,
        store: EntityStore,
        clock: Clock,
        send: Sender,
        refresh: Refresher | None = None,
        record: Recorder | None = None,
        metrics: Any = None,
        synced: Callable[[], bool] | None = None,
        vacation: vac.VacationSettings | None = None,
        tz: ZoneInfo | None = None,
    ) -> None:
        if settings.mode not in ACTUATOR_MODES:
            raise ValueError(f"climate actuator mode must be one of {ACTUATOR_MODES}")
        self.settings = settings
        self.mode = settings.mode
        self.ents = settings.entities
        self._owner_entity = owner_entity
        self._store = store
        self._clock = clock
        self._send = send
        self._refresh = refresh
        self._record = record
        self._metrics = metrics
        self._synced = synced
        self._vacation = vacation
        self._tz = tz or ZoneInfo("UTC")
        self._lock = asyncio.Lock()
        #: The plan fallback reason held off by the vacation (ADR-0010), ``None`` when not held.
        self.vacation_hold: str | None = None
        #: The last valid vacation window read, kept while the helpers are unavailable.
        self._last_window: vac.VacationWindow | None = None

        #: The last tick found itself permitted to write (select live, legacy off, synced): only
        #: then does a select leaving ``live`` or a shutdown write the safe state.
        self.in_control = False
        self.reasons: tuple[str, ...] = ()
        #: The active fallback ``(reason, confirmed)``: the safe state was (or is being) written.
        self.fallback: tuple[str, bool] | None = None
        self.desired: Desired | None = None
        self.readings: _Readings | None = None
        self.breached: set[str] = set()  # rooms under a floor override (latched until release)
        self.holds: tuple[str, ...] = ()
        self.not_direct_mode = False
        self.alert: str | None = None
        self.last_error: str | None = None
        self.last_tick_at: datetime | None = None
        self.last_heartbeat_at: datetime | None = None
        self.last_confirmed_write_at: dict[str, datetime] = {}
        self._last_write_at: dict[str, datetime] = {}
        self._last_water_write_at: datetime | None = None
        #: dry_run only: the state each would-be write leaves, on the entity read back.
        self._simulated: dict[str, EntityState] = {}
        self._signature: Any = None
        #: The HA session dropped and has not yet been seen synced again by a tick.
        self._was_unsynced = False
        #: Start of the restart grace (a resync, or the mode sensor lost); ``None``: no episode.
        self._grace_since: datetime | None = None
        #: The restart-caused condition this tick held back instead of writing the safe state.
        self._grace_hold: str | None = None

    # ------------------------------------------------------------------ inputs

    def _state(self, entity: str | None) -> EntityState | None:
        """The state writes are planned against: the store, or in dry_run the simulated one while
        no newer real state has arrived."""
        real = self._store.get(entity)
        sim = self._simulated.get(entity) if entity else None
        if sim is not None and (real is None or sim.last_updated >= real.last_updated):
            return sim
        return real

    def _numeric(self, entity: str | None) -> float | None:
        state = self._state(entity)
        return state.numeric() if state is not None else None

    def _read(self, now: datetime) -> _Readings:
        e = self.ents
        owner_state = self._store.get(self._owner_entity)
        owner = (
            owner_state.state.strip().lower()
            if owner_state is not None and owner_state.available
            else None
        )
        legacy = self._store.get(e.legacy_enable)
        legacy_on = legacy is not None and legacy.available and legacy.state.strip().lower() == "on"
        mode_state = self._state(e.observed_mode)
        mode_raw = (
            mode_state.state.strip() if mode_state is not None and mode_state.available else None
        )
        mode = (
            None
            if mode_raw is None
            else "on"
            if mode_raw == HEAT_ON
            else "off"
            if mode_raw == HEAT_OFF
            else "other"
        )
        since = (
            None
            if mode_state is None or mode_raw is None
            else max(0.0, (now - mode_state.last_changed).total_seconds())
        )
        heating_mode = self._numeric(e.heating_mode)
        water = self._numeric(e.water_request)
        if water is None:  # the helper the automation mirrors the unit's request into
            water = self._numeric(e.water_target)
        window = self._vacation_window(now)
        absent = window is not None and window.absent(now)
        guard = self._vacation.guard_c if self._vacation is not None else None
        rooms: dict[str, float | None] = {}
        floors: dict[str, float] = {}
        for room, entity in e.rooms.items():
            rooms[room] = self._room_temperature(entity, now)
            floors[room] = guard if absent and guard is not None else self._floor(room)
        return _Readings(
            owner=owner,
            legacy_on=legacy_on,
            mode=mode,
            mode_raw=mode_raw,
            mode_since_s=since,
            direct=None if heating_mode is None else heating_mode == 1,
            water=water,
            rooms=rooms,
            floors=floors,
            vacation=window,
            absent=absent,
        )

    def _vacation_window(self, now: datetime) -> vac.VacationWindow | None:
        """The owner's valid vacation window from the store (read only), ``None`` without one."""
        v = self._vacation
        if v is None or not v.enabled:
            return None

        def value(entity: str) -> str | None:
            state = self._store.get(entity)
            return state.state if state is not None and state.available else None

        read = vac.read_window(
            value(v.start_entity), value(v.end_entity), tz=self._tz, now=now, settings=v
        )
        read = vac.hold_last(read, self._last_window, now)  # an HA restart keeps the window
        self._last_window = read.window
        return read.window

    def _vacation_holds(self, r: _Readings, reason: str, now: datetime) -> bool:
        """Whether a fallback for *reason* is a vacation hold: the house is empty, the return is
        further than the look-ahead, the reason is the plan's (a missing room reading or mode
        sensor keeps the normal fallback) and the unit and a room can be read."""
        v, w = self._vacation, r.vacation
        if v is None or w is None or not r.absent or reason in _RESTART_REASONS:
            return False
        if now >= w.ready_at - timedelta(hours=v.lookahead_hours):
            return False
        return r.mode is not None and any(t is not None for t in r.rooms.values())

    def _room_temperature(self, entity: str, now: datetime) -> float | None:
        """The thermostat's current temperature, or ``None`` when it is missing, unavailable,
        implausible or past the ``room_max_age_s`` backstop (a room that cannot be proven below its
        floor is skipped).

        HA publishes a thermostat's state only when it changes, so a steady room keeps an old
        ``observed_at`` while healthy; an available reading in a synced session stays usable and the
        backstop (hours) only catches a feed that went silent without HA marking it unavailable."""
        state = self._store.get(entity)
        if state is None or not state.available:
            return None
        if (now - state.observed_at).total_seconds() > self.settings.room_max_age_s:
            return None
        raw = state.attributes.get("current_temperature")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return None
        low, high = _ROOM_PLAUSIBLE_C
        return float(raw) if low <= raw <= high else None

    def _floor(self, room: str) -> float:
        """The room's comfort floor: its helper if available and in range, else the config.

        An owner-set ``input_number`` never changes or re-reports on its own, so its age says
        nothing; availability in the HA session is the test (like ``heating_mode``). Nothing is
        written outside a synced session (``ha_not_synced``), so the floor is only acted on then.
        """
        state = self._store.get(self.ents.floor_helper(room))
        value = state.numeric() if state is not None else None
        low, high = FLOOR_HELPER_RANGE_C
        if value is not None and low <= value <= high:
            return value
        return self.settings.floor_c

    async def _refresh_entities(self) -> None:
        if self._refresh is None:
            return
        e = self.ents
        wanted = (
            e.observed_mode,
            e.heating_mode,
            e.water_request,
            self._owner_entity,
            e.legacy_enable,
        )
        try:
            await self._refresh(wanted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the WebSocket stays authoritative
            log.debug("climate actuator refresh failed: %s", exc)

    # ------------------------------------------------------------------ the plan

    def _slot(self, ctx: ClimateContext) -> tuple[_Plan | None, str | None]:
        """The plan's slot for ``ctx.now``, or ``(None, fallback reason)``."""
        plan = ctx.plan
        if plan is None:
            return None, "no_plan"
        if not ctx.plan_own:
            return None, "plan_not_from_this_process"
        try:
            if ctx.now >= datetime.fromisoformat(plan["valid_until"]):
                return None, "plan_expired"
            status = plan.get("status")
            if status == "skipped":
                return (
                    None,
                    "observer_no_state" if ctx.plan_status == "no_state" else "plan_invalid",
                )
            if status == "error":
                return None, "plan_failed" if ctx.plan_status == "plan_failed" else "plan_invalid"
            if status != "planned" or plan.get("version") != 1:
                return None, "plan_invalid"
            slots = plan["slots"]
            start = datetime.fromisoformat(slots["start"])
            step = timedelta(minutes=int(slots["step_minutes"]))
            n = int(slots["n"])
            heating, waters = slots["heating"], slots["water_c"]
            if step <= timedelta(0) or len(heating) != n or len(waters) != n:
                return None, "plan_invalid"
            idx = int((ctx.now - start) // step)
            if not 0 <= idx < n or heating[idx] not in (0, 1) or isinstance(heating[idx], bool):
                return None, "plan_invalid"
            water: int | None = None
            if heating[idx]:
                water = waters[idx]
                low, high = CLIMATE_WATER_RANGE_C
                if (
                    isinstance(water, bool)
                    or not isinstance(water, int)
                    or not low <= water <= high
                ):
                    return None, "plan_invalid"
            return _Plan(str(plan["plan_id"]), bool(heating[idx]), water), None
        except KeyError, TypeError, ValueError, AttributeError:
            return None, "plan_invalid"

    def _floor_override(self, r: _Readings) -> tuple[str, ...]:
        """Update the latch and return the rooms under a floor override.

        A room below its floor enters; it releases at the floor plus ``floor_margin_c``.  A room
        without a usable reading is skipped: it can neither enter nor release."""
        margin = self.settings.floor_margin_c
        entered = []
        for room, temperature in r.rooms.items():
            if temperature is None:
                continue
            floor = r.floors[room]
            if room in self.breached:
                if temperature >= floor + margin:
                    self.breached.discard(room)
            elif temperature < floor:
                self.breached.add(room)
                entered.append(room)
        if entered:
            kind = "guard" if r.absent else "floor"
            log.warning("climate actuator %s override rooms=%s", kind, ",".join(entered))
            if self._metrics is not None and not r.absent:
                self._metrics.climate_actuator_floor_overrides.inc(len(entered))
        return tuple(sorted(self.breached))

    def _safe_state(self, reason: str) -> Desired:
        return Desired(
            source="safe_state",
            reason=reason,
            heating=True,
            water_c=self.settings.safe_water_target_c,
            exempt_min_off=True,
            exempt_water_limit=True,
        )

    # ------------------------------------------------------------------ writes

    def _plan_writes(
        self, desired: Desired, r: _Readings, now: datetime
    ) -> tuple[list[Write], list[str], bool]:
        """``(writes in order, holds, not_direct)`` that move the unit to *desired*.

        Turning heating off: the mode only.  Turning it on: the water target, then the mode.  The
        minimum on always holds a switch to ``DHW``; the minimum off holds a switch to ``Heat+DHW``
        unless *desired* is exempt; the water target needs direct mode, a change of 1 K and 15 min
        since the last water write (the exempt skip the 15 minutes).  While heating already runs the
        change must exceed 1 K by ``water_hysteresis_k`` (no write for a 1 K flip-flop); a start of
        heating and the safe state write the desired value exactly."""
        e = self.ents
        writes: list[Write] = []
        holds: list[str] = []
        mode_write: Write | None = None
        since = r.mode_since_s if r.mode_since_s is not None else math.inf
        if desired.heating:
            if r.mode != "on":
                if r.mode == "off" and not desired.exempt_min_off and since < MIN_OFF_S:
                    holds.append("min_off")
                else:
                    mode_write = Write(
                        "heat_mode", e.heat_mode, "input_select", "select_option", option=HEAT_ON
                    )
        elif r.mode != "off":
            if r.mode == "on" and since < MIN_ON_S:
                holds.append("min_on")
            else:
                mode_write = Write(
                    "heat_mode", e.heat_mode, "input_select", "select_option", option=HEAT_OFF
                )
        if mode_write is not None:
            mode_write = replace(
                mode_write, reason=desired.reason, old=r.mode_raw, plan_id=desired.plan_id
            )
        not_direct = False
        water_write: Write | None = None
        heating_after = desired.heating and (mode_write is not None or r.mode == "on")
        if heating_after and desired.water_c is not None:
            if r.direct is not True:
                not_direct = True  # curve mode or unreadable: no water target is ever written
            elif r.water is None or abs(r.water - desired.water_c) >= (
                WATER_MIN_STEP_K
                if mode_write is not None or desired.exempt_water_limit
                else WATER_MIN_STEP_K + self.settings.water_hysteresis_k
            ):
                last = self._last_water_write_at
                due = last is None or (now - last).total_seconds() >= WATER_MIN_INTERVAL_S
                if desired.exempt_water_limit or due:
                    water_write = Write(
                        "water_target",
                        e.water_target,
                        "input_number",
                        "set_value",
                        value=desired.water_c,
                        reason=desired.reason,
                        old=r.water,
                        plan_id=desired.plan_id,
                    )
                else:
                    holds.append("water_interval")
        if desired.heating:
            writes = [w for w in (water_write, mode_write) if w is not None]
        else:
            writes = [mode_write] if mode_write is not None else []
        return writes, holds, not_direct

    def _count(self, family: str, **labels: str) -> None:
        if self._metrics is not None:
            getattr(self._metrics, family).labels(**labels).inc()

    def _fail(self, write: Write, reason: str, exc: BaseException | None) -> None:
        detail = f": {type(exc).__name__}: {exc}" if exc is not None else ""
        self.last_error = f"{write.role} {write.domain}.{write.service} {reason}{detail}"[:500]
        log.warning("climate actuator write failed: %s", self.last_error)
        self._count("climate_actuator_write_failures", reason=reason)

    def _confirmed(self, write: Write) -> bool:
        """Whether the unit's own state shows the write's result (the readback test)."""
        e = self.ents
        if write.role == "heat_mode":
            state = self._state(e.observed_mode)
            return state is not None and state.available and state.state.strip() == write.option
        value = self._numeric(e.water_request)
        return value is not None and write.value is not None and abs(value - write.value) <= 0.5

    async def _readback(self, write: Write) -> bool:
        entity = self.ents.observed_mode if write.role == "heat_mode" else self.ents.water_request
        deadline = self._clock.now() + timedelta(seconds=self.settings.readback_timeout_s)
        polls = 0
        while True:
            if self._confirmed(write):
                return True
            if self._clock.now() >= deadline:
                return False
            await self._clock.sleep(_READBACK_POLL_S)
            polls += 1
            if polls % _READBACK_REFRESH_EVERY == 0 and self._refresh is not None:
                with contextlib.suppress(Exception):
                    await self._refresh((entity,))

    def _simulate(self, write: Write) -> None:
        """dry_run: remember the state this write would leave on the entity that is read back."""
        entity = self.ents.observed_mode if write.role == "heat_mode" else self.ents.water_request
        old = self._state(entity)
        now = self._clock.now()
        value = write.option if write.role == "heat_mode" else write.value
        self._simulated[entity] = parse_ha_state(
            {
                "entity_id": entity,
                "state": str(value),
                "attributes": dict(old.attributes) if old is not None else {},
                "last_updated": now.isoformat(),
                "last_changed": now.isoformat(),
            }
        )

    async def _log_write(self, write: Write, result: str, at: datetime) -> None:
        log.log(
            logging.WARNING if result == "failed" else logging.INFO,
            "climate actuator %s entity=%s old=%s new=%s reason=%s result=%s",
            "dry_run write" if self.mode == "dry_run" else "write",
            write.entity_id,
            write.old,
            write.option if write.option is not None else write.value,
            write.reason,
            result,
        )
        if self._record is not None and write.role != "heartbeat":
            row = {
                "kind": "climate_actuation_write",
                "at": at.isoformat(),
                "mode": self.mode,
                "sent": self.mode == "live",
                "result": result,
                **write.to_dict(),
            }
            with contextlib.suppress(Exception):
                await self._record(at, row)

    async def _execute(self, write: Write) -> str:
        """Validate, send and confirm one write within the retry budget (``dry_run`` records).

        Returns ``ok`` (sent and read back), ``dry_run`` (recorded, not sent) or ``failed``."""
        try:
            write.validate()
        except ForbiddenHACallError as exc:
            self._fail(write, "forbidden", exc)  # a bug: never sent, never retried
            self._count("climate_actuator_writes", entity=write.role, result="failed")
            await self._log_write(write, "failed", self._clock.now())
            return "failed"
        if write.role == "water_target":
            self._last_water_write_at = self._clock.now()  # sent or not, never again within 15 min
        if self.mode == "dry_run":
            self._count("climate_actuator_writes", entity=write.role, result="dry_run")
            self._simulate(write)
            await self._log_write(write, "dry_run", self._clock.now())
            return "dry_run"
        for attempt in range(self.settings.max_attempts):
            if attempt:
                await self._clock.sleep(_RETRY_PAUSE_S)
            last = self._last_write_at.get(write.entity_id)
            if last is not None:
                wait = MIN_WRITE_GAP_S - (self._clock.now() - last).total_seconds()
                if wait > 0:
                    await self._clock.sleep(wait)
            try:
                await self._send(write.domain, write.service, write.entity_id, write.service_data)
            except ForbiddenHACallError as exc:
                self._fail(write, "forbidden", exc)  # a bug, not a transient: never retried
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - HA refused, timed out or disconnected
                self._fail(write, "ha_error", exc)
                continue
            self._last_write_at[write.entity_id] = self._clock.now()
            if await self._readback(write):
                now = self._clock.now()
                self.last_confirmed_write_at[write.role] = now
                self._count("climate_actuator_writes", entity=write.role, result="ok")
                await self._log_write(write, "ok", now)
                return "ok"
            self._fail(write, "readback_timeout", None)
        self._count("climate_actuator_writes", entity=write.role, result="failed")
        await self._log_write(write, "failed", self._clock.now())
        return "failed"

    async def _apply(self, writes: list[Write]) -> tuple[bool, list[dict[str, Any]]]:
        """Apply in order and stop at the first write that fails; ``(all ok, what was done)``."""
        done: list[dict[str, Any]] = []
        for write in writes:
            result = await self._execute(write)
            done.append({**write.to_dict(), "result": result})
            if result == "failed":
                return False, done
        return True, done

    async def _heartbeat(self, now: datetime) -> str:
        write = Write(
            "heartbeat",
            self.ents.heartbeat,
            "input_datetime",
            "set_datetime",
            value=now.timestamp(),
            reason="reconciled",
        )
        if self.mode == "dry_run":
            self._count("climate_actuator_heartbeats", result="dry_run")
            return "dry_run"
        try:
            write.validate()
            await self._send(write.domain, write.service, write.entity_id, write.service_data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the watchdog notices a missing heartbeat
            self._fail(
                write, "forbidden" if isinstance(exc, ForbiddenHACallError) else "ha_error", exc
            )
            self._count("climate_actuator_heartbeats", result="failed")
            return "failed"
        self.last_heartbeat_at = now
        self._count("climate_actuator_heartbeats", result="ok")
        return "ok"

    async def _drive_safe_state(
        self, reason: str, r: _Readings, now: datetime
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Write the safe heating state where the unit does not already show it."""
        desired = self._safe_state(reason)
        self.desired = desired
        writes, self.holds, self.not_direct_mode = self._plan_writes(desired, r, now)
        return await self._apply(writes)

    # ------------------------------------------------------------------ the tick

    async def tick(self, ctx: ClimateContext) -> dict[str, Any]:
        """Evaluate the guards and the plan, reconcile, heartbeat; return the tick record."""
        async with self._lock:
            return await self._tick(ctx)

    async def _tick(self, ctx: ClimateContext) -> dict[str, Any]:
        now = ctx.now
        self.last_tick_at = now
        await self._refresh_entities()
        r = self._read(now)
        self.readings = r
        before = (self.in_control, self.reasons, self.fallback, self.desired, self.alert)
        reasons: list[str] = []
        writes: list[dict[str, Any]] = []
        heartbeat: str | None = None
        self.holds = ()
        self._grace_hold = None
        if ctx.ha_synced and self._was_unsynced:  # a resync: the grace for a restarting HA starts
            self._was_unsynced = False
            self._grace_since = now
        if not ctx.ha_synced:
            self._was_unsynced = True
            reasons.append("ha_not_synced")  # nothing can be sent; the HA watchdog covers this
        elif r.legacy_on:
            # One writer only: nothing at all (no safe state, no heartbeat) while the legacy
            # controller is active.  Deliberately not a fallback trigger.
            reasons.append("legacy_writer_active")
            self._stand_down()
        elif r.owner != "live":
            reasons.append("select_not_live")
            unreadable = r.owner is None and self.fallback is None
            if self.in_control and unreadable and self._start_grace(now):
                # the select is unreadable (HA restarting), not set to something else: hold
                self._grace_hold = "select_unavailable"
                heartbeat = await self._hold_heartbeat(now)
            elif self.in_control:  # the live -> not live edge: the safe state once, then hands off
                self._grace_since = None
                self._count("climate_actuator_fallbacks", reason="select_left_live")
                log.warning("climate actuator fallback reason=select_left_live owner=%s", r.owner)
                ok, writes = await self._drive_safe_state("select_left_live", r, now)
                self.in_control = not ok  # unconfirmed: keep trying while the select stays off
                self.alert = None if ok else "safe_state_failed"
                self.fallback = None
            else:
                self.desired = None
        else:
            self.in_control = True
            heartbeat, writes = await self._tick_in_control(ctx, r)
        if self._grace_hold is not None:
            reasons.append("restart_grace")
        if not self.in_control:
            self._grace_since = None
            self.vacation_hold = None
        self.reasons = tuple(reasons)
        self._log_transitions(before, r)
        record = self._tick_record(ctx, r, writes, heartbeat)
        await self._maybe_record(now, record)
        return record

    def _stand_down(self) -> None:
        """Hand control back without writing (the legacy controller is active)."""
        self.in_control = False
        self.fallback = None
        self.desired = None
        self.alert = None
        self._grace_since = None
        self.breached.clear()
        self.not_direct_mode = False

    async def _tick_in_control(
        self, ctx: ClimateContext, r: _Readings
    ) -> tuple[str | None, list[dict[str, Any]]]:
        now = ctx.now
        slot, reason = self._slot(ctx)
        forced: tuple[str, ...] = ()
        if reason is None:
            forced = self._floor_override(r)
            if all(t is None for t in r.rooms.values()):
                reason = "no_room_reading"
            elif r.mode is None:
                reason = "mode_sensor_unavailable"
            if r.mode is None and self._grace_since is None:
                self._grace_since = now  # HA restarting: the unit's own sensor is gone
        if reason not in _RESTART_REASONS:
            self._grace_since = None
        if reason is not None and self._vacation_holds(r, reason, now):
            return await self._tick_vacation_hold(reason, r, now)
        if self.vacation_hold is not None:
            log.info("climate actuator vacation hold cleared reason=%s", self.vacation_hold)
            self.vacation_hold = None
        if reason is not None:  # the fallback: the safe state once, then nothing until a plan
            writes: list[dict[str, Any]] = []
            if self.fallback is None and reason in _RESTART_REASONS and self._in_grace(now):
                # HA restarting: keep the last applied state (heartbeat only); the safe state only
                # if the condition outlasts the grace
                self._grace_hold = reason
                return await self._hold_heartbeat(now), writes
            if self.fallback is None:
                self._count("climate_actuator_fallbacks", reason=reason)
                log.warning("climate actuator fallback reason=%s", reason)
                self.fallback = (reason, False)
            if not self.fallback[1]:
                ok, writes = await self._drive_safe_state(self.fallback[0], r, now)
                self.fallback = (self.fallback[0], ok)
                self.alert = None if ok else "safe_state_failed"
            else:
                self.desired = self._safe_state(self.fallback[0])
                ok = True
            return (await self._heartbeat(now) if ok else None), writes
        if self.fallback is not None:
            log.info("climate actuator fallback cleared reason=%s", self.fallback[0])
            self.fallback = None
        assert slot is not None
        if forced:  # the planned water target, or without one the safe target
            water = slot.water_c if slot.heating else self.settings.safe_water_target_c
            desired = Desired(
                "vacation_guard" if r.absent else "floor",
                "vacation_guard" if r.absent else "floor_override",
                True,
                water,
                slot.plan_id,
                exempt_min_off=True,
                rooms=forced,
            )
        else:
            desired = Desired("plan", "plan", slot.heating, slot.water_c, slot.plan_id)
        return await self._reconcile(desired, r, now)

    async def _tick_vacation_hold(
        self, reason: str, r: _Readings, now: datetime
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """No usable plan while the house is empty and the return is far (ADR-0010): heating off,
        the building guard still forced, no safe state (an outage must not heat an empty house for
        a day).  Counted apart from the safe-state fallbacks."""
        if self.fallback is not None:  # a safe state written before the hold: leave it behind
            self.fallback = None
        if self.vacation_hold != reason:
            if self.vacation_hold is None:
                self._count("climate_actuator_vacation_fallbacks", reason=reason)
            log.warning("climate actuator vacation hold reason=%s (heating held off)", reason)
            self.vacation_hold = reason
        forced = self._floor_override(r)
        if forced:
            desired = Desired(
                "vacation_guard",
                "vacation_guard",
                True,
                self.settings.safe_water_target_c,
                exempt_min_off=True,
                rooms=forced,
            )
        else:
            desired = Desired("vacation_hold", f"vacation_hold:{reason}", False)
        return await self._reconcile(desired, r, now)

    async def _reconcile(
        self, desired: Desired, r: _Readings, now: datetime
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Move the unit to *desired*; on a failed write at least the safe state, this tick only."""
        self.desired = desired
        planned, self.holds, self.not_direct_mode = self._plan_writes(desired, r, now)
        ok, writes = await self._apply(planned)
        if ok:
            self.alert = None
        elif self._restart_failure():  # the unit or HA went away mid-write: not a fault
            self._grace_hold = "write_failed"
            return await self._hold_heartbeat(now), writes
        else:  # the desired state failed: at least the safe state, this tick only
            self._count("climate_actuator_fallbacks", reason="write_failed")
            log.warning("climate actuator fallback reason=write_failed")
            later = self._clock.now()
            ok, more = await self._drive_safe_state("write_failed", self._read(later), later)
            writes += more
            self.alert = "write_failed" if ok else "safe_state_failed"
        return (await self._heartbeat(now) if ok else None), writes

    def _start_grace(self, now: datetime) -> bool:
        """Start the restart grace if none runs; whether *now* is inside it."""
        if self._grace_since is None:
            self._grace_since = now
        return self._in_grace(now)

    async def _hold_heartbeat(self, now: datetime) -> str | None:
        """The heartbeat of a hold: the optimizer is alive and holding on purpose, so the watchdog
        must not act while the session is synced (without a session it cannot be written)."""
        if self._synced is not None and not self._synced():
            return None
        return await self._heartbeat(now)

    def _in_grace(self, now: datetime) -> bool:
        since = self._grace_since
        return since is not None and (now - since).total_seconds() < (
            self.settings.ha_restart_grace_s
        )

    def _restart_failure(self) -> bool:
        """Whether a failed write is explained by a restarting HA (the mode sensor unreadable or
        the session gone) and the grace has not run out.  Starts the grace if none is running."""
        later = self._clock.now()
        gone = self._read(later).mode is None or (self._synced is not None and not self._synced())
        if not gone:
            return False
        if self._grace_since is None:
            self._grace_since = later
        held = self._in_grace(later)
        if held:
            log.warning("climate actuator write failed while HA restarts: hold (restart_grace)")
        return held

    async def shutdown(self) -> bool:
        """The safe state before the process exits (when in control); ``False`` if it failed."""
        try:
            async with asyncio.timeout(self.settings.shutdown_timeout_s), self._lock:
                if not self.in_control:
                    return True
                now = self._clock.now()
                r = self._read(now)
                if r.legacy_on:  # never a second writer
                    return True
                self._count("climate_actuator_fallbacks", reason="shutdown")
                ok, writes = await self._drive_safe_state("shutdown", r, now)
                self.in_control = not ok
                self.fallback = ("shutdown", ok)
                log.log(
                    logging.INFO if ok else logging.ERROR,
                    "climate actuator shutdown safe state ok=%s writes=%d",
                    ok,
                    len(writes),
                )
                record = self._tick_record(None, r, writes, None)
                await self._maybe_record(now, record, force=True)
                return ok
        except TimeoutError:
            log.error("climate actuator: safe state at shutdown timed out")
            return False

    # ------------------------------------------------------------------ records and views

    def _log_transitions(self, before: tuple[Any, ...], r: _Readings) -> None:
        in_control, reasons, fallback, desired, alert = before
        if self.in_control != in_control:
            log.info(
                "climate actuator %s (owner_select=%s)",
                "in control" if self.in_control else "hands off",
                r.owner,
            )
        if self.reasons != reasons and self.reasons:
            log.info("climate actuator no writes reasons=%s", ",".join(self.reasons))
        if self.alert != alert:
            if self.alert is None:
                log.info("climate actuator alert cleared previous=%s", alert)
            else:
                level = logging.ERROR if self.alert == "safe_state_failed" else logging.WARNING
                log.log(
                    level, "climate actuator alert=%s last_error=%s", self.alert, self.last_error
                )
        if self.desired is not None and (desired is None or self.desired != desired):
            log.info(
                "climate actuator desired source=%s heating=%s water_c=%s holds=%s",
                self.desired.source,
                self.desired.heating,
                self.desired.water_c,
                ",".join(self.holds) or "-",
            )

    def _tick_record(
        self,
        ctx: ClimateContext | None,
        r: _Readings,
        writes: list[dict[str, Any]],
        heartbeat: str | None,
    ) -> dict[str, Any]:
        return {
            "kind": "climate_actuation_tick" if ctx is not None else "climate_actuation_shutdown",
            "at": (ctx.now if ctx is not None else self._clock.now()).isoformat(),
            "mode": self.mode,
            "in_control": self.in_control,
            "guard_reasons": list(self.reasons) if ctx is not None else ["shutdown"],
            "fallback": None if self.fallback is None else self.fallback[0],
            "vacation_hold": self.vacation_hold,
            "plan_id": None if ctx is None or ctx.plan is None else ctx.plan.get("plan_id"),
            "desired": self.desired.to_dict() if self.desired is not None else None,
            "holds": list(self.holds),
            "not_direct_mode": self.not_direct_mode,
            "observed": r.to_dict(),
            "writes": writes,
            "sent": self.mode == "live",
            "heartbeat": heartbeat,
            "alert": self.alert,
            "last_error": self.last_error,
        }

    async def _maybe_record(
        self, at: datetime, record: dict[str, Any], force: bool = False
    ) -> None:
        """One row per tick that changed the state (writes have their own rows)."""
        signature = (
            tuple(record["guard_reasons"]),
            record["fallback"],
            repr(record["desired"]),
            record["alert"],
            record["in_control"],
            tuple(record["holds"]),
        )
        changed = signature != self._signature
        self._signature = signature
        if self._record is None or not (changed or force):
            return
        with contextlib.suppress(Exception):
            await self._record(at, record)

    def view(self) -> dict[str, Any]:
        """The actuator state for ``GET /status``."""

        def iso(at: datetime | None) -> str | None:
            return at.isoformat() if at is not None else None

        return {
            "mode": self.mode,
            "sends": self.mode == "live",
            "in_control": self.in_control,
            "guard": {
                "ok": not self.reasons and self.fallback is None,
                "reasons": list(self.reasons),
            },
            "fallback": None
            if self.fallback is None
            else {"reason": self.fallback[0], "safe_state_confirmed": self.fallback[1]},
            "desired": self.desired.to_dict() if self.desired is not None else None,
            "holds": list(self.holds),
            "not_direct_mode": self.not_direct_mode,
            "floor_override_rooms": sorted(self.breached),
            "vacation_hold": self.vacation_hold,
            "observed": self._read(self._clock.now()).to_dict() if self.last_tick_at else None,
            "alert": self.alert,
            "last_error": self.last_error,
            "last_tick_at": iso(self.last_tick_at),
            "last_heartbeat_at": iso(self.last_heartbeat_at),
            "last_confirmed_write_at": {
                k: v.isoformat() for k, v in self.last_confirmed_write_at.items()
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """Scrape-time values for the metrics collector."""
        r = self._read(self._clock.now()) if self.last_tick_at is not None else None
        desired = self.desired

        def flag(value: bool | None) -> float | None:
            return None if value is None else (1.0 if value else 0.0)

        owner = "missing"
        if r is not None and r.owner is not None:
            owner = r.owner if r.owner in OWNER_STATES[:3] else "other"
        return {
            "mode": self.mode,
            "ticked": self.last_tick_at is not None,
            "owner_select": owner,
            "in_control": flag(self.in_control),
            "reasons": self.reasons,
            "guard_ok": not self.reasons and self.fallback is None,
            "alert": 1.0 if self.alert else 0.0,
            "fallback": None if self.fallback is None else self.fallback[0],
            "holds": self.holds,
            "not_direct_mode": 1.0 if self.not_direct_mode else 0.0,
            "floor_override_rooms": 0 if r is not None and r.absent else len(self.breached),
            "guard_override_rooms": len(self.breached) if r is not None and r.absent else 0,
            "vacation_fallback_active": 1.0 if self.vacation_hold is not None else 0.0,
            "desired_heating": flag(desired.heating) if desired else None,
            "desired_water_celsius": desired.water_c if desired else None,
            "observed_heating": None
            if r is None or r.mode is None
            else (1.0 if r.mode == "on" else 0.0),
            "observed_water_celsius": r.water if r is not None else None,
            "last_confirmed_write": {
                k: v.timestamp() for k, v in self.last_confirmed_write_at.items()
            },
        }
