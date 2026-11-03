"""Live battery control through Omnibattery's public Home Assistant entities (ADR-0007).

Each tick (``actuation.tick_interval_s``, plus at every slot open and every new plan) the
actuator takes the intent the runtime froze for the current slot and turns it into a desired
Omnibattery state:

* ``idle``: charge off, discharge off, PD grid target ``neutral_pd_target_w``;
* ``charge`` at P W: discharge off, charge on, PD grid target
  ``min(house load + P, import_cap_w)`` (never negative);
* ``discharge`` at P W: charge off, discharge on, PD grid target ``discharge_margin_w``
  (a small import, never export), system power limits on and the system discharge cap
  ``max(P, min_discharge_cap_w)`` (never 0, which Omnibattery reads as *unlimited*).

House load = grid power - battery AC power (charge positive), from fresh readings.

**Safe idle** (both direction switches off, nothing else) replaces the mapping whenever the
guard fails: no current intent, an intent from a plan this process did not solve, a stale
grid/SOC/battery-power/price input, an Omnibattery status outside ``healthy_states``, or a
measured import above ``import_cap_w + import_guard_margin_w`` while charging (that last one
also holds charge off for ``import_trip_hold_s``).

Writes are ordered restrictive first: switch off the direction(s) being removed, set the
discharge cap, enable the power limits, set the PD target, and only then switch on the
direction being enabled.  One write is in flight at a time and each is confirmed by reading
the entity back from the entity store (WebSocket events, refreshed by REST GET) within
``readback_timeout_s``; a failed send or readback is retried up to ``max_attempts``, then the
actuator falls back to safe idle and raises an alert.  Only the charge-mode PD target, while
no direction is being switched on, is rate limited: it is rewritten when it moves by
``min_setpoint_change_w`` and not more often than every ``min_command_interval_s``.  Switch
edges, mode changes, the discharge margin and the discharge cap are always written.  Every
write but a ``turn_off`` re-checks the kill switch first and aborts to safe idle when it is off.

Numeric targets are bounded by the entity's own ``min``/``max`` as well as ours: Omnibattery
bounds the PD target by the batteries' summed charge limit (±2500 W here), so the effective
charge ceiling is ``min(import_cap_w, entity max)``; ``Desired`` carries the value actually
written and ``clamped`` says when it was cut.

The owner's kill switch gates everything: while it is off nothing is written, except one
safe idle when it goes from on to off.  Each live tick that ends reconciled publishes a
heartbeat (``input_datetime.set_datetime``) that an HA watchdog checks; a failing actuator
stops the heartbeat, so the watchdog takes over.  ``dry_run`` runs the same logic and records
every write it would make, but sends nothing.

The actuator never talks Modbus and never builds a raw HA frame: sends go through the
runtime to :meth:`~.ha_client.HAWebSocketClient.actuate`, which :mod:`.ha_guard` checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .clock import Clock
from .config import ActuationSettings
from .entity_store import EntityState, EntityStore, parse_ha_state
from .ha_guard import ForbiddenHACallError

if TYPE_CHECKING:
    from .planner import ShadowIntent

__all__ = [
    "ACTUATOR_MODES",
    "GUARD_REASONS",
    "SAFE_IDLE_REASONS",
    "WRITE_FAILURE_REASONS",
    "WRITE_ROLES",
    "ActuationContext",
    "Actuator",
    "Desired",
    "Write",
    "safe_idle",
]

log = logging.getLogger(__name__)

ACTUATOR_MODES: tuple[str, ...] = ("dry_run", "live")

#: Every reason the guard can report (fixed label set), most urgent first.
GUARD_REASONS: tuple[str, ...] = (
    "import_over_cap",
    "ha_not_synced",
    "kill_switch_off",
    "no_plan",
    "plan_expired",
    "inputs_not_ready",
    "intent_expired",
    "plan_not_from_this_process",
    "grid_power_stale",
    "battery_soc_stale",
    "battery_power_stale",
    "battery_unresponsive",
    "price_stale",
    "integration_unhealthy",
)
SAFE_IDLE_REASONS: tuple[str, ...] = (*GUARD_REASONS, "write_failed", "shutdown")
WRITE_FAILURE_REASONS: tuple[str, ...] = ("ha_error", "readback_timeout", "forbidden")
WRITE_ROLES: tuple[str, ...] = (
    "allow_charge",
    "allow_discharge",
    "max_discharge_power",
    "power_limits",
    "pd_target",
    "heartbeat",
)

#: Slot decision (runtime ``_SlotOpen.decision``) -> guard reason when there is no intent.
_DECISION_REASON = {
    "no_plan": "no_plan",
    "plan_expired": "plan_expired",
    "not_ready": "inputs_not_ready",
}
#: The healthy state of Omnibattery's non-responsive-batteries sensor (verbatim, case included).
LIVENESS_OK_STATE = "None"
_READBACK_POLL_S = 0.25
_READBACK_REFRESH_EVERY = 8  # polls: one REST GET of the entity every 2 s while waiting
_RETRY_PAUSE_S = 1.0

Sender = Callable[[str, str, str, Mapping[str, Any] | None], Awaitable[None]]
Refresher = Callable[[tuple[str, ...]], Awaitable[None]]
Recorder = Callable[[datetime, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ActuationContext:
    """What the runtime knows at one tick (the actuator reads sensors itself)."""

    now: datetime
    ha_synced: bool
    intent: ShadowIntent | None
    #: The slot decision when there is no intent: ``no_plan``, ``plan_expired``, ``not_ready``.
    decision: str
    #: The intent's plan was solved by this process (never a persisted or earlier plan).
    plan_from_this_process: bool
    #: A PVPC price is known for the current slot.
    price_fresh: bool


@dataclass(frozen=True)
class Desired:
    """The Omnibattery state one tick wants; ``None`` fields are left untouched."""

    mode: str  # idle | charge | discharge | safe_idle
    reason: str  # "intent", or a SAFE_IDLE_REASONS member
    allow_charge: bool = False
    allow_discharge: bool = False
    pd_target_w: float | None = None
    power_limits: bool | None = None
    max_discharge_w: float | None = None
    house_load_w: float | None = None
    clamped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_idle(reason: str) -> Desired:
    """Both direction switches off; nothing else is written."""
    if reason not in SAFE_IDLE_REASONS:
        raise ValueError(f"unknown safe-idle reason {reason!r}")
    return Desired(mode="safe_idle", reason=reason)


@dataclass(frozen=True)
class Write:
    """One HA service call on one entity."""

    role: str
    entity_id: str
    domain: str
    service: str
    value: float | None = None

    @property
    def service_data(self) -> dict[str, Any] | None:
        if self.domain == "number":
            return {"value": self.value}
        if self.domain == "input_datetime":
            return {"timestamp": self.value}
        return None

    def matches(self, state: EntityState | None, tolerance: float = 0.5) -> bool:
        """Whether *state* already shows this write's result (the readback test)."""
        if state is None or not state.available:
            return False
        if self.domain == "switch":
            wanted = "on" if self.service == "turn_on" else "off"
            return state.state.strip().lower() == wanted
        value = state.numeric()
        return value is not None and self.value is not None and abs(value - self.value) <= tolerance

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "entity_id": self.entity_id,
            "service": f"{self.domain}.{self.service}",
        }
        if self.value is not None:
            out["value"] = self.value
        return out


def _attr(state: EntityState | None, key: str) -> float | None:
    if state is None:
        return None
    value = state.attributes.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _quantize(value: float, state: EntityState | None, low: float, high: float) -> float:
    """*value* on the entity's step grid, inside its min/max and our ``[low, high]``.

    Rounds down (the restrictive side), and up only when down would leave ``low``.
    """
    lo = max(low, _attr(state, "min") if _attr(state, "min") is not None else low)
    hi = min(high, _attr(state, "max") if _attr(state, "max") is not None else high)
    value = min(max(value, lo), hi)
    step = _attr(state, "step")
    if step is not None and step > 0:
        base = _attr(state, "min") or 0.0
        down = base + math.floor((value - base) / step + 1e-9) * step
        value = down if down >= lo - 1e-9 else base + math.ceil((value - base) / step - 1e-9) * step
    return float(round(value, 6))


def _tolerance(state: EntityState | None) -> float:
    step = _attr(state, "step")
    return step / 2 + 1e-6 if step is not None and step > 0 else 0.5


class Actuator:
    """The single writer of the Omnibattery control entities (``dry_run`` or ``live``)."""

    def __init__(
        self,
        *,
        settings: ActuationSettings,
        mode: str,
        store: EntityStore,
        clock: Clock,
        grid_entity: str | None,
        send: Sender,
        refresh: Refresher | None = None,
        record: Recorder | None = None,
        metrics: Any = None,
    ) -> None:
        if mode not in ACTUATOR_MODES:
            raise ValueError(f"actuator mode must be one of {ACTUATOR_MODES}, got {mode!r}")
        if settings.battery_ac_power_sign not in (1, -1):
            raise ValueError("actuation.battery_ac_power_sign must be 1 or -1")
        self.settings = settings
        self.mode = mode
        self.policy = settings.command_policy()
        self._store = store
        self._clock = clock
        self._grid = grid_entity
        self._send = send
        self._refresh = refresh
        self._record = record
        self._metrics = metrics
        self._lock = asyncio.Lock()

        self.kill_switch: bool | None = None
        #: The kill switch has been seen on: an off edge or a shutdown must idle once.
        self.in_control = False
        self.reasons: tuple[str, ...] = ()
        self.desired: Desired | None = None
        self._applied_mode: str | None = None
        self._last_write_at: dict[str, datetime] = {}
        #: dry_run only: the state each would-be write leaves, so writes are planned against it.
        self._simulated: dict[str, EntityState] = {}
        self._aborted: str | None = None
        self.readings: dict[str, Any] = {}
        self.alert: str | None = None
        self.last_error: str | None = None
        self.trip_until: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_success_write_at: datetime | None = None
        self.last_heartbeat_at: datetime | None = None
        self._signature: Any = None

    # ------------------------------------------------------------------ inputs

    def _fresh(self, entity: str | None, max_age_s: float | None, now: datetime) -> float | None:
        """Numeric value; with no *max_age_s* only availability counts (no age test)."""
        if max_age_s is None:
            state = self._store.get(entity)
            return state.numeric() if state is not None else None
        return self._store.fresh_value(entity, now, max_age_s)

    def _observed(self, entity: str | None) -> EntityState | None:
        """The state writes are planned against: the store, or in dry_run the simulated one."""
        if entity is not None and entity in self._simulated:
            return self._simulated[entity]
        return self._store.get(entity)

    def _number_target(
        self, entity: str | None, value: float, low: float, high: float
    ) -> tuple[float, bool]:
        """``(value to write, clamped)`` inside our ``[low, high]`` and the entity's own bounds."""
        state = self._observed(entity)
        ceiling = min(high, _attr(state, "max") if _attr(state, "max") is not None else high)
        written = _quantize(value, state, low, high)
        return written, value > ceiling + 1e-9

    def _switch_state(self, entity: str | None) -> bool | None:
        state = self._store.get(entity)
        if state is None or not state.available:
            return None
        return state.state.strip().lower() == "on"

    def _kill_switch_on(self) -> bool:
        """Only an explicit ``on`` hands control over; unknown or missing means off."""
        return self._switch_state(self.settings.entities.kill_switch) is True

    def _read(self, now: datetime) -> dict[str, Any]:
        s, e = self.settings, self.settings.entities
        grid = self._fresh(self._grid, s.grid_power_max_age_s, now)
        ac_raw = self._fresh(e.battery_ac_power, s.battery_ac_power_max_age_s, now)
        ac = None if ac_raw is None else float(s.battery_ac_power_sign or 1) * ac_raw
        soc = self._fresh(e.battery_soc, s.battery_soc_max_age_s, now)
        if soc is not None and not 0.0 <= soc <= 100.0:
            soc = None
        status = self._store.get(e.integration_status)
        liveness = self._store.get(e.battery_liveness)

        def number(entity: str | None) -> float | None:
            state = self._store.get(entity)
            return state.numeric() if state is not None else None

        return {
            "grid_power_w": grid,
            "battery_ac_power_w": ac,
            "battery_soc_pct": soc,
            "house_load_w": None if grid is None or ac is None else grid - ac,
            "integration_status": status.state if status is not None else None,
            "battery_liveness": liveness.state if liveness is not None else None,
            "allow_charge": self._switch_state(e.allow_charge),
            "allow_discharge": self._switch_state(e.allow_discharge),
            "power_limits": self._switch_state(e.power_limits),
            "pd_target_w": number(e.pd_target),
            "max_discharge_w": number(e.max_discharge_power),
        }

    async def _refresh_entities(self, entities: tuple[str | None, ...]) -> None:
        if self._refresh is None:
            return
        wanted = tuple(e for e in entities if e)
        try:
            await self._refresh(wanted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the WebSocket stays authoritative
            log.debug("actuator refresh failed: %s", exc)

    # ------------------------------------------------------------------ guard and mapping

    def guard(self, ctx: ActuationContext, readings: Mapping[str, Any]) -> list[str]:
        """Every reason to hold safe idle right now (empty == the intent may be applied)."""
        s, now, intent = self.settings, ctx.now, ctx.intent
        reasons: list[str] = []
        grid = readings.get("grid_power_w")
        if (
            grid is not None
            and grid > s.import_cap_w + s.import_guard_margin_w
            and readings.get("allow_charge")
        ):
            self.trip_until = now + timedelta(seconds=s.import_trip_hold_s)
            reasons.append("import_over_cap")
        elif (
            self.trip_until is not None
            and now < self.trip_until
            and intent is not None
            and intent.mode == "charge"
        ):
            reasons.append("import_over_cap")
        if intent is None:
            reasons.append(_DECISION_REASON.get(ctx.decision, "no_plan"))
        else:
            if not intent.is_current(now) or now >= intent.expires_at:
                reasons.append("intent_expired")
            if not ctx.plan_from_this_process:
                reasons.append("plan_not_from_this_process")
        if grid is None:
            reasons.append("grid_power_stale")
        if readings.get("battery_soc_pct") is None:
            reasons.append("battery_soc_stale")
        if readings.get("battery_ac_power_w") is None:
            reasons.append("battery_power_stale")
        # Raw state on purpose: the healthy text "None" is what EntityState.available rejects.
        if s.entities.battery_liveness and readings.get("battery_liveness") != LIVENESS_OK_STATE:
            reasons.append("battery_unresponsive")
        if not ctx.price_fresh:
            reasons.append("price_stale")
        status = readings.get("integration_status")
        if status is None or status.strip() not in s.healthy_states:
            reasons.append("integration_unhealthy")
        return reasons

    def desired_for(self, intent: ShadowIntent, readings: Mapping[str, Any]) -> Desired:
        """The mapping table of the module docstring (call only after a passing guard)."""
        s, e = self.settings, self.settings.entities
        load = readings.get("house_load_w")
        power = float(intent.target_power_w)
        if intent.mode == "charge":
            if load is None:
                return safe_idle("battery_power_stale")
            # Effective ceiling min(import_cap_w, entity max): Omnibattery bounds the target by
            # the batteries' charge limit, so the charge is at most that ceiling - house load.
            target, clamped = self._number_target(
                e.pd_target, float(math.floor(max(load + power, 0.0))), 0.0, s.import_cap_w
            )
            return Desired(
                mode="charge",
                reason="intent",
                allow_charge=True,
                pd_target_w=target,
                house_load_w=load,
                clamped=clamped,
            )
        if intent.mode == "discharge":
            cap, clamped = self._number_target(
                e.max_discharge_power,
                max(power, s.min_discharge_cap_w),
                s.min_discharge_cap_w,
                s.max_discharge_cap_w,
            )
            margin, _ = self._number_target(e.pd_target, s.discharge_margin_w, 0.0, s.import_cap_w)
            return Desired(
                mode="discharge",
                reason="intent",
                allow_discharge=True,
                pd_target_w=margin,
                power_limits=True,
                max_discharge_w=cap,
                house_load_w=load,
                clamped=clamped,
            )
        neutral, _ = self._number_target(e.pd_target, s.neutral_pd_target_w, 0.0, s.import_cap_w)
        return Desired(mode="idle", reason="intent", pd_target_w=neutral, house_load_w=load)

    def plan_writes(self, desired: Desired, now: datetime) -> list[Write]:
        """The writes that take the observed state to *desired*, restrictive first."""
        s, e = self.settings, self.settings.entities
        mode_changed = desired.mode != self._applied_mode
        # A reconcile that switches a direction on always writes its numbers first, in full.
        enabling = any(
            on and self._switch_state_observed(entity) is not True
            for on, entity in (
                (desired.allow_charge, e.allow_charge),
                (desired.allow_discharge, e.allow_discharge),
            )
        )
        writes: list[Write] = []

        def switch(role: str, entity: str | None, on: bool) -> None:
            assert entity is not None
            write = Write(role, entity, "switch", "turn_on" if on else "turn_off")
            if not write.matches(self._observed(entity)):
                writes.append(write)

        def number(role: str, entity: str | None, value: float, low: float, high: float) -> None:
            assert entity is not None
            state = self._observed(entity)
            write = Write(role, entity, "number", "set_value", _quantize(value, state, low, high))
            if write.matches(state, _tolerance(state)):
                return
            current = state.numeric() if state is not None else None
            # Only the charge target's load tracking is rate limited: never the discharge margin
            # (export risk) or cap, and never while a direction is being switched on.
            limited = role == "pd_target" and desired.mode == "charge"
            if limited and not mode_changed and not enabling and current is not None:
                if not self.policy.change_is_worth_sending(current, write.value):
                    return
                last = self._last_write_at.get(entity)
                spacing = self.policy.min_command_interval_seconds
                if last is not None and (now - last).total_seconds() < spacing:
                    return
            writes.append(write)

        if not desired.allow_charge:
            switch("allow_charge", e.allow_charge, False)
        if not desired.allow_discharge:
            switch("allow_discharge", e.allow_discharge, False)
        if desired.max_discharge_w is not None:
            number(
                "max_discharge_power",
                e.max_discharge_power,
                desired.max_discharge_w,
                s.min_discharge_cap_w,
                s.max_discharge_cap_w,
            )
        if desired.power_limits:
            switch("power_limits", e.power_limits, True)
        if desired.pd_target_w is not None:
            number("pd_target", e.pd_target, desired.pd_target_w, 0.0, s.import_cap_w)
        if desired.allow_discharge:
            switch("allow_discharge", e.allow_discharge, True)
        if desired.allow_charge:
            switch("allow_charge", e.allow_charge, True)
        return writes

    def _switch_state_observed(self, entity: str | None) -> bool | None:
        state = self._observed(entity)
        if state is None or not state.available:
            return None
        return state.state.strip().lower() == "on"

    def _simulate(self, write: Write) -> None:
        """dry_run: remember the state this write would leave (attributes kept)."""
        old = self._observed(write.entity_id)
        value = write.value if write.domain == "number" else write.service.removeprefix("turn_")
        now = self._clock.now()
        self._simulated[write.entity_id] = parse_ha_state(
            {
                "entity_id": write.entity_id,
                "state": str(value),
                "attributes": dict(old.attributes) if old is not None else {},
                "last_updated": now.isoformat(),
            }
        )

    # ------------------------------------------------------------------ writing

    def _count(self, family: str, **labels: str) -> None:
        if self._metrics is not None:
            getattr(self._metrics, family).labels(**labels).inc()

    def _fail(self, write: Write, reason: str, exc: BaseException | None) -> None:
        detail = f": {type(exc).__name__}: {exc}" if exc is not None else ""
        self.last_error = f"{write.role} {write.domain}.{write.service} {reason}{detail}"[:500]
        log.warning("actuator write failed: %s", self.last_error)
        self._count("actuator_write_failures", reason=reason)

    async def _readback(self, write: Write) -> bool:
        deadline = self._clock.now() + timedelta(seconds=self.policy.lease_seconds)
        polls = 0
        while True:
            state = self._store.get(write.entity_id)
            if write.matches(state, _tolerance(state)):
                return True
            if self._clock.now() >= deadline:
                return False
            await self._clock.sleep(_READBACK_POLL_S)
            polls += 1
            if polls % _READBACK_REFRESH_EVERY == 0:
                await self._refresh_entities((write.entity_id,))

    def _succeeded(self, write: Write) -> None:
        now = self._clock.now()
        self.last_success_write_at = now
        self._count("actuator_writes", role=write.role, result="ok")
        if self._metrics is not None:
            self._metrics.actuator_last_write.set(now.timestamp())

    async def _execute(self, write: Write) -> bool:
        """Send one write and confirm it, within the retry budget; ``dry_run`` only counts.

        Every write that is not a ``turn_off`` re-checks the kill switch from the store first: a
        reconcile can take tens of seconds of readbacks, and the owner may switch it off meanwhile.
        """
        if write.service != "turn_off" and not self._kill_switch_on():
            self._aborted = "kill_switch_off"
            return False
        if self.mode == "dry_run":
            self._count("actuator_writes", role=write.role, result="dry_run")
            self._simulate(write)
            self._last_write_at[write.entity_id] = self._clock.now()
            return True
        for attempt in range(self.policy.max_attempts):
            if attempt:
                await self._clock.sleep(_RETRY_PAUSE_S)
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
                self._succeeded(write)
                return True
            self._fail(write, "readback_timeout", None)
        self._count("actuator_writes", role=write.role, result="failed")
        return False

    async def _reconcile(
        self, desired: Desired, now: datetime
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Apply the planned writes in order; stop at the first one that fails."""
        done: list[dict[str, Any]] = []
        self._aborted = None
        for write in self.plan_writes(desired, now):
            ok = await self._execute(write)
            if not ok and self._aborted:
                result = "aborted"
            else:
                result = "dry_run" if self.mode == "dry_run" else "ok" if ok else "failed"
            done.append({**write.to_dict(), "result": result})
            if not ok:
                return False, done
        self._applied_mode = desired.mode
        return True, done

    async def _heartbeat(self, now: datetime) -> str:
        entity = self.settings.entities.heartbeat
        assert entity is not None
        write = Write("heartbeat", entity, "input_datetime", "set_datetime", now.timestamp())
        if self.mode == "dry_run":
            self._count("actuator_heartbeats", result="dry_run")
            return "dry_run"
        try:
            await self._send(write.domain, write.service, write.entity_id, write.service_data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the watchdog notices a missing heartbeat
            self._fail(write, "ha_error", exc)
            self._count("actuator_heartbeats", result="failed")
            return "failed"
        self.last_heartbeat_at = now
        self._count("actuator_heartbeats", result="ok")
        return "ok"

    # ------------------------------------------------------------------ the tick

    async def tick(self, ctx: ActuationContext) -> dict[str, Any]:
        """Evaluate the guard, reconcile, heartbeat; return the tick record."""
        async with self._lock:
            return await self._tick(ctx)

    async def _tick(self, ctx: ActuationContext) -> dict[str, Any]:
        now = ctx.now
        e = self.settings.entities
        self.last_tick_at = now
        await self._refresh_entities(
            (
                self._grid,
                e.battery_ac_power,
                e.battery_soc,
                e.battery_liveness,
                e.integration_status,
                e.kill_switch,
            )
        )
        readings = self._read(now)
        self.readings = readings
        self.kill_switch = self._kill_switch_on()
        writes: list[dict[str, Any]] = []
        desired: Desired | None = None
        heartbeat: str | None = None
        if not ctx.ha_synced:
            reasons = ["ha_not_synced"]  # nothing can be sent; the HA watchdog covers this
        elif not self.kill_switch:
            reasons = ["kill_switch_off"]
            if self.in_control:  # the on -> off edge: idle once, then hands off
                desired = safe_idle("kill_switch_off")
                ok, writes = await self._reconcile(desired, now)
                self.in_control = not ok
                self.alert = None if ok else "safe_idle_failed"
        else:
            self.in_control = True
            reasons = self.guard(ctx, readings)
            if reasons:
                desired = safe_idle(reasons[0])
            else:
                assert ctx.intent is not None
                desired = self.desired_for(ctx.intent, readings)
            ok, writes = await self._reconcile(desired, now)
            if self._aborted:  # the kill switch went off mid-reconcile: idle and hand off
                reasons = [self._aborted]
                self.kill_switch = False
                desired = safe_idle(self._aborted)
                ok, more = await self._reconcile(desired, now)
                writes += more
                self.in_control = not ok
                self.alert = None if ok else "safe_idle_failed"
                ok = False  # no heartbeat: control is handed back
            elif ok:
                self.alert = None
            elif desired.mode != "safe_idle":
                desired = safe_idle("write_failed")
                ok, more = await self._reconcile(desired, now)
                writes += more
                self.alert = "write_failed" if ok else "safe_idle_failed"
            else:
                self.alert = "safe_idle_failed"
            if ok:  # reconciled: tell the watchdog we are alive and in control
                heartbeat = await self._heartbeat(now)
        previous = self.desired
        if (
            desired is not None
            and desired.mode == "safe_idle"
            and (previous is None or previous.reason != desired.reason)
        ):
            self._count("actuator_safe_idles", reason=desired.reason)
        self.reasons = tuple(reasons)
        self.desired = desired
        record = self._tick_record(ctx, readings, desired, writes, heartbeat)
        await self._maybe_record(now, record, bool(writes))
        return record

    async def shutdown(self) -> bool:
        """Safe idle before the process exits (when in control); ``False`` if it failed."""
        try:
            async with asyncio.timeout(self.settings.shutdown_timeout_s), self._lock:
                if not self.in_control:
                    return True
                now = self._clock.now()
                desired = safe_idle("shutdown")
                ok, writes = await self._reconcile(desired, now)
                self.in_control = not ok
                self.desired = desired
                self._count("actuator_safe_idles", reason="shutdown")
                record = self._tick_record(None, self.readings, desired, writes, None)
                await self._maybe_record(now, record, True)
                return ok
        except TimeoutError:
            log.error("actuator: safe idle at shutdown timed out")
            return False

    def _tick_record(
        self,
        ctx: ActuationContext | None,
        readings: Mapping[str, Any],
        desired: Desired | None,
        writes: list[dict[str, Any]],
        heartbeat: str | None,
    ) -> dict[str, Any]:
        intent = ctx.intent if ctx is not None else None
        return {
            "kind": "actuation_tick" if ctx is not None else "actuation_shutdown",
            "at": (ctx.now if ctx is not None else self._clock.now()).isoformat(),
            "mode": self.mode,
            "kill_switch": self.kill_switch,
            "in_control": self.in_control,
            "guard_reasons": list(self.reasons) if ctx is not None else ["shutdown"],
            "intent": None
            if intent is None
            else {
                "plan_id": intent.plan_id,
                "plan_sequence": intent.plan_sequence,
                "sequence_id": intent.sequence_id,
                "mode": intent.mode,
                "target_power_w": intent.target_power_w,
                "valid_until": intent.valid_until.isoformat(),
            },
            "desired": desired.to_dict() if desired is not None else None,
            "observed": dict(readings),
            "writes": writes,
            "sent": self.mode == "live",
            "heartbeat": heartbeat,
            "alert": self.alert,
            "last_error": self.last_error,
        }

    async def _maybe_record(self, at: datetime, record: dict[str, Any], wrote: bool) -> None:
        desired = record["desired"]
        if desired is not None:
            desired = {k: v for k, v in desired.items() if k != "house_load_w"}
        signature = (tuple(record["guard_reasons"]), repr(desired), record["kill_switch"])
        signature += (record["alert"],)
        changed = signature != self._signature
        self._signature = signature
        if self._record is None or not (wrote or changed):
            return
        with contextlib.suppress(Exception):
            await self._record(at, record)

    # ------------------------------------------------------------------ views

    def view(self) -> dict[str, Any]:
        """The actuator state for ``GET /status``."""

        def iso(at: datetime | None) -> str | None:
            return at.isoformat() if at is not None else None

        return {
            "mode": self.mode,
            "sends": self.mode == "live",
            "kill_switch": self.kill_switch,
            "in_control": self.in_control,
            "guard": {"ok": not self.reasons, "reasons": list(self.reasons)},
            "desired": self.desired.to_dict() if self.desired is not None else None,
            "observed": dict(self.readings),
            "alert": self.alert,
            "last_error": self.last_error,
            "import_trip_until": iso(self.trip_until),
            "last_tick_at": iso(self.last_tick_at),
            "last_successful_write_at": iso(self.last_success_write_at),
            "last_heartbeat_at": iso(self.last_heartbeat_at),
        }

    def snapshot(self) -> dict[str, Any]:
        """Scrape-time values for the metrics collector."""
        desired = self.desired

        def flag(value: bool | None) -> float | None:
            return None if value is None else (1.0 if value else 0.0)

        return {
            "mode": self.mode,
            "kill_switch": flag(self.kill_switch) if self.last_tick_at is not None else None,
            "in_control": flag(self.in_control),
            "reasons": self.reasons,
            "ticked": self.last_tick_at is not None,
            "alert": 1.0 if self.alert else 0.0,
            "desired_pd_target_w": desired.pd_target_w if desired else None,
            "desired_allow_charge": flag(desired.allow_charge) if desired else None,
            "desired_allow_discharge": flag(desired.allow_discharge) if desired else None,
            "desired_max_discharge_w": desired.max_discharge_w if desired else None,
            "observed_battery_ac_power_w": self.readings.get("battery_ac_power_w"),
            "observed_grid_power_w": self.readings.get("grid_power_w"),
            "house_load_w": self.readings.get("house_load_w"),
            "observed_pd_target_w": self.readings.get("pd_target_w"),
        }
