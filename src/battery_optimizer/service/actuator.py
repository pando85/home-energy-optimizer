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

**Load-following discharge** (ADR-0007 amendment 2026-11-07; ``actuation.load_following``, off by
default): in a discharge intent, while the measured SOC is at or above the plan's end-of-slot SOC
(``soc_target_frac``), the discharge cap is raised to the load-following ceiling
(``load_following.max_w``, at most ``max_discharge_cap_w``), so the battery covers the real house
load up to that ceiling instead of the forecast one; the PD target stays the discharge margin, so
the battery still never exports.  Hysteresis: it engages at ``plan SOC + soc_hysteresis_pct`` and
releases below the plan SOC; the latch carries over between consecutive discharge intents and
resets on anything else (charge, idle, safe idle, kill switch).  Same entity, same bounds, same
write ordering as the planned cap.  With ``load_following.floor: storage_value`` (amendment
2026-11-11) the plan's SOC is not the floor: the cap is raised while the slot's price less the wear
cost is at least the plan's marginal value of stored energy plus a margin (the SOC stays clear of
the tariff floor, and a plan without that value uses the plan-SOC rule), and the energy discharged
above the planned cap while engaged is counted.  The default floor is ``plan_soc``: records,
metrics and writes are then exactly as before.

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

**Periodic full charge** (ADR-0007 amendment 2026-11-04; off unless the runtime hands the actuator
enabled :class:`~.config.FullChargeSettings` *and* a :class:`FullChargeDirective` in the context):
the directive says whether Omnibattery's weekly full charge is armed.  Arming writes the day
select (the local weekday), then turns the weekly switch on, both before the direction on-writes
of the same reconcile; disarming turns the switch off first, in the restrictive block.  Arming is
refused (never written, the switch is driven off) while the weekly delay switch is on or
unreadable, the select does not offer the seven weekdays, the directive is not for the current
local weekday, or a write of the pair failed; the reason is ``full_charge_refusal``.
``weekly_full_charge`` counts as a healthy ``integration_status`` only while a directive is
effectively armed for today.  A guard-failure safe idle neither arms nor disarms, but the kill
switch's on -> off edge and shutdown disarm once, best effort, after their safe idle.

**Settle hold** (amendment A2): once Omnibattery has completed the run, the directive carries
``settle_until``.  Until then the actuator maps the slot to the existing idle intent (charge off,
discharge off, neutral PD target, ``reason: settle_hold``) whatever the plan says, so the 60 s
cell-spread measurement Omnibattery takes after the charge stops is not taken under load, and the
weekly switch stays on.  The scheduler ends the hold early (a new reading) or by timeout by
sending ``armed: false``; the switch then goes off in the restrictive block of the reconcile that
resumes the normal intent.  The guard wins: a guard-failure safe idle ends the hold's effect (it
is idle anyway), and no new entity or service is involved.

**Reserve protection** (ADR-0007 amendment 2026-11-08; ``actuation.soc_floor_recovery_w``, the
floor from ``config/tariff.yaml``): while the **measured** SOC sits at or below the tariff's SOC
floor, a tick that the plan maps to idle or discharge becomes a bounded recovery charge
(``reason: below_soc_floor``), because the measured residual drain continues even in idle (about
26 W on 2026-11-08) and only opposing it holds the floor.  The charge at or above
``soc_floor_recovery_w`` the plan already asked for is preserved untouched, and a weaker planned
charge is raised to it through the same charge row.  It reuses the charge mapping and the existing
allowlist entries (the PD target and the charge allow switch) with the same
``min(import_cap_w, entity max)`` ceiling, ordering, dwell and readback: no entity, service or
bound is added or widened.  Everything else keeps its priority: the kill switch, ``ha_not_synced``,
every guard reason (a stale SOC, an unresponsive battery, an expired or foreign plan, a stale
price, an unhealthy ``integration_status``) and the import trip, then the settle hold, then the
reserve recovery, then the normal mapping.  During the trip's ``import_trip_hold_s`` the recovery
sends no charge, and because the guard scopes that refusal to *charge* intents the recovery refuses
the planned discharge as well: the tick holds the idle mapping (both directions off) rather than
draining the floor while no charge is allowed.  Likewise, when the ceiling leaves less than
``soc_floor_recovery_w`` above the house load it does not synthesize a charge: an idle or discharge
tick holds the idle mapping, and a planned charge below the recovery power keeps its own row
(``reason: intent``, clamped by that ceiling as usual) instead of being raised to a target the cap
would only clamp away.  Hysteresis: it engages at the floor and releases at
floor + ``actuation.soc_floor_hysteresis_pct`` (1.0 SOC point).  The floor is never configured
twice: ``ActuationContext.soc_floor_pct`` comes from ``battery.soc_window_min``, which the planner
also uses, and the planner's own clamp of a measured SOC into the window is left as it is (a plan
must stay feasible); the protection reads the meter, not the plan's assumption.  It applies in
``dry_run`` (recorded, nothing sent) and ``live``; ``shadow`` has no actuator, and nothing here
makes a service live.

Numeric targets are bounded by the entity's own ``min``/``max`` as well as ours: Omnibattery
bounds the PD target by the batteries' summed charge limit (±2500 W here), so the effective
charge ceiling is ``min(import_cap_w, entity max)``; ``Desired`` carries the value actually
written, ``clamped`` says when it was cut and ``clamped_w`` how much planned battery power
the cut removed (exported as ``battery_optimizer_actuator_clamped_watts``).  With
Omnibattery's system power limits on, the PD target's max is the system max charge, so a
charge plan above ``system max charge - house load`` is under-delivered.

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
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from .clock import Clock
from .config import ActuationSettings, FullChargeSettings
from .entity_store import EntityState, EntityStore, UpdateResult, parse_ha_state
from .ha_guard import WEEKDAY_OPTIONS, ForbiddenHACallError
from .metrics import power_band

if TYPE_CHECKING:
    from .planner import ShadowIntent

__all__ = [
    "ACTUATOR_MODES",
    "GUARD_REASONS",
    "SAFE_IDLE_REASONS",
    "WRITE_FAILURE_REASONS",
    "WEEKLY_FULL_CHARGE_STATUS",
    "WRITE_ROLES",
    "ActuationContext",
    "Actuator",
    "Desired",
    "FullChargeDirective",
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
    "full_charge_day",
    "full_charge_enable",
)
#: The two roles of the periodic full charge (never touched without an enabled directive).
FULL_CHARGE_ROLES: tuple[str, ...] = ("full_charge_day", "full_charge_enable")
#: ``integration_status`` while Omnibattery's weekly full charge is running.
WEEKLY_FULL_CHARGE_STATUS = "weekly_full_charge"

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
#: A tick older than this many tick intervals no longer vouches for the mode (stalled loop).
_TICK_FRESH_FACTOR = 3.0

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
    #: The periodic full charge: ``None`` = feature off, the two roles are never touched.
    full_charge: FullChargeDirective | None = None
    #: The tariff's SOC floor (%); ``None`` disables reserve protection.
    soc_floor_pct: float | None = None
    #: ``planning.throughput_cost_eur_per_kwh``: the wear cost the ``storage_value`` load-following
    #: rule nets off the slot price (EUR per AC kWh), as the planner does.
    throughput_cost_eur_per_kwh: float = 0.0


@dataclass(frozen=True)
class FullChargeDirective:
    """Whether Omnibattery's weekly full charge should be armed (built by the scheduler)."""

    armed: bool  # the actuator drives the weekly switch to this
    weekday: str | None = None  # "monday".."sunday": weekday of the run's local date
    #: Settle hold: hold idle (no charge, no discharge) until this instant (amendment A2).
    settle_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.armed and self.weekday not in WEEKDAY_OPTIONS:
            raise ValueError(f"an armed directive needs a weekday of {WEEKDAY_OPTIONS}")
        if self.settle_until is not None and not self.armed:
            raise ValueError("a settle hold needs an armed directive")
        if self.settle_until is not None and self.settle_until.tzinfo is None:
            raise ValueError("settle_until must be timezone-aware")


@dataclass(frozen=True)
class Desired:
    """The Omnibattery state one tick wants; ``None`` fields are left untouched."""

    mode: str  # idle | charge | discharge | safe_idle
    reason: str  # "intent", "settle_hold", "below_soc_floor", or a SAFE_IDLE_REASONS member
    allow_charge: bool = False
    allow_discharge: bool = False
    pd_target_w: float | None = None
    power_limits: bool | None = None
    max_discharge_w: float | None = None
    house_load_w: float | None = None
    clamped: bool = False
    #: Planned battery power (W) the clamp removed; 0 when nothing was cut.
    clamped_w: float = 0.0
    #: Periodic full charge: ``True`` arm (select, then switch on), ``False`` disarm, ``None``
    #: leave both entities alone.  Recorded only when set.
    full_charge_armed: bool | None = None
    full_charge_weekday: str | None = None
    #: Load-following discharge (feature on, discharge intent only; ``None`` otherwise, and not
    #: recorded): whether it is engaged (the cap is the ceiling, never below the planned cap), the
    #: cap the plan alone gives, and the SOC floor (%, the plan's end-of-slot SOC) it is held to.
    load_following: bool | None = None
    planned_max_discharge_w: float | None = None
    load_following_floor_pct: float | None = None
    #: Only with ``load_following.floor: storage_value`` (``None`` otherwise, and not recorded):
    #: the rule that decided (``storage_value``, or ``plan_soc`` when the plan had no value to
    #: use), the plan's value of stored energy it used (EUR per AC kWh) and the spread it
    #: compared, ``price - throughput cost - value``.
    load_following_rule: str | None = None
    load_following_storage_value_eur_per_kwh: float | None = None
    load_following_spread_eur_per_kwh: float | None = None

    @property
    def load_following_raise_w(self) -> float:
        """How far load-following raised the discharge cap above the plan's (W; 0 when not)."""
        if not self.load_following:
            return 0.0
        assert self.max_discharge_w is not None and self.planned_max_discharge_w is not None
        return self.max_discharge_w - self.planned_max_discharge_w

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if self.full_charge_armed is None:
            del out["full_charge_armed"], out["full_charge_weekday"]
        if self.load_following is None:
            del out["load_following"], out["planned_max_discharge_w"]
            del out["load_following_floor_pct"]
        if self.load_following_rule is None:
            del out["load_following_rule"], out["load_following_storage_value_eur_per_kwh"]
            del out["load_following_spread_eur_per_kwh"]
        return out


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
    #: ``select.select_option`` only.
    option: str | None = None

    @property
    def service_data(self) -> dict[str, Any] | None:
        if self.domain == "number":
            return {"value": self.value}
        if self.domain == "select":
            return {"option": self.option}
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
        if self.domain == "select":
            return state.state.strip() == self.option
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
        if self.option is not None:
            out["option"] = self.option
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
        full_charge: FullChargeSettings | None = None,
        tz: tzinfo | None = None,
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
        #: The periodic full charge's settings; ``None`` while the feature is off.
        self._fc = full_charge if full_charge is not None and full_charge.enabled else None
        if self._fc is not None and tz is None:
            raise ValueError("the periodic full charge needs the service time zone (tz)")
        self._tz = tz
        #: Why the directive's arming was refused at the last tick (``None``: not refused).
        self.full_charge_refusal: str | None = None
        #: The armed directive whose write failed: refused until the directive changes.
        self._fc_failed: FullChargeDirective | None = None
        #: A directive has been seen: only then do the kill-switch edge and shutdown disarm.
        self._fc_driven = False

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
        #: Load-following hysteresis latch: the last applied discharge intent had it engaged.
        self._lf_engaged = False
        #: ``storage_value`` floor only: the previous tick while it was engaged, as ``(at, planned
        #: cap W, discharge W, rule, spread)``, and the energy discharged above the planned cap
        #: since start (kWh, per deciding rule) and its value against the storage value (EUR).
        self._lf_prev: tuple[datetime, float, float | None, str, float | None] | None = None
        self._lf_extra_kwh: dict[str, float] = {"storage_value": 0.0, "plan_soc": 0.0}
        self._lf_extra_value_eur = 0.0
        #: Reserve-protection hysteresis latch: SOC at/below the tariff floor.
        self._floor_engaged = False
        #: The authoritative floor (%) the last tick came from the tariff; ``None`` if no floor.
        self._floor_pct: float | None = None
        #: The previous fresh grid-power update: ``(observed_at, grid W, discharging)``; ``None``
        #: after a stale or unusable one, so an outage or a gap never counts as export.
        self._grid_prev: tuple[datetime, float, bool] | None = None

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
    ) -> tuple[float, float]:
        """``(value to write, W cut)`` inside our ``[low, high]`` and the entity's own bounds."""
        state = self._observed(entity)
        ceiling = min(high, _attr(state, "max") if _attr(state, "max") is not None else high)
        written = _quantize(value, state, low, high)
        cut = value - ceiling
        return written, cut if cut > 1e-9 else 0.0

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

        readings: dict[str, Any] = {
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
        if self._fc is not None:  # the weekly readings, for the scheduler (what we believe)
            f = self._fc.entities
            day = self._observed(f.day_select)
            readings["weekly_enable"] = self._switch_state_observed(f.enable_switch)
            usable = day is not None and day.available
            readings["weekly_day"] = day.state.strip() if usable else None
            readings["weekly_delay"] = self._switch_state(f.delay_switch)
        return readings

    def _full_charge_entities(self) -> tuple[str | None, ...]:
        """The weekly entities the actuator reads (none while the feature is off)."""
        if self._fc is None:
            return ()
        f = self._fc.entities
        return (f.enable_switch, f.day_select, f.delay_switch)

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
        healthy = status is not None and status.strip() in s.healthy_states
        if status is not None and status.strip() == WEEKLY_FULL_CHARGE_STATUS:
            # The weekly run is healthy only while this service has armed one for today.
            healthy = self._full_charge_target(ctx, readings)[0] is True
        if not healthy:
            reasons.append("integration_unhealthy")
        return reasons

    def _below_floor(self, ctx: ActuationContext, readings: Mapping[str, Any]) -> bool:
        """Reserve protection: True while SOC is at/below the tariff floor (latched, hysteresis).

        Engages when the measured SOC is at or below the floor, releases at floor plus
        ``soc_floor_hysteresis_pct``, and holds in between, so a recovery that only just crosses
        the floor does not re-engage on the next tick.  It uses the tick's fresh measured SOC,
        never the planner's clamped SOC: the 2026-11-08 night the plan started from a clamped
        0.200 while the battery read 0.150, so nothing in the plan could see the shortfall.
        A missing floor or a non-finite SOC leaves the latch as it is (the guard already holds
        safe idle on a stale SOC, and clearing the latch on a bad read would drop protection).
        """
        floor = ctx.soc_floor_pct
        self._floor_pct = floor
        soc = readings.get("battery_soc_pct")
        if floor is None or soc is None or not math.isfinite(soc):
            return self._floor_engaged
        hysteresis = self.settings.soc_floor_hysteresis_pct
        engaged = (
            soc <= floor + 1e-9 if not self._floor_engaged else soc < floor + hysteresis - 1e-9
        )
        self._floor_engaged = engaged
        return engaged

    def reserve_desired(self, ctx: ActuationContext, readings: Mapping[str, Any]) -> Desired | None:
        """The recovery the floor protection wants, or ``None`` when the planned mapping stands.

        Call only after a passing guard (``ha_not_synced``, the kill switch and every guard reason
        keep their priority) and after the settle hold.  It never weakens a planned charge: a charge
        at or above ``soc_floor_recovery_w`` returns ``None`` so :meth:`desired_for` maps the plan
        as usual, and a weaker planned charge is raised to the recovery power through the *same*
        charge row.  An idle or discharge tick below the floor becomes a bounded recovery charge:
        forcing idle would not stop the measured residual drain, so the only way to hold the floor
        is to oppose it.

        The recovery reuses ``number.set_value`` on the PD target and the charge allow switch
        (``allow_charge``), with the existing ``min(import_cap_w, entity max)`` ceiling, the
        ordering, dwell and readback of :meth:`_charge_desired`; no entity, service or bound is
        widened.  Two cases deliberately do not command a charge.  During the import trip's
        ``import_trip_hold_s`` the tick holds the idle mapping (``reason: below_soc_floor``): the
        guard scopes the trip's refusal to *charge* intents, so this layer has to refuse the
        planned discharge as well instead of letting it drain the floor further while no charge is
        allowed.  And when the ceiling leaves less than ``soc_floor_recovery_w`` above the house
        load the recovery holds back rather than pushing the setpoint to the cap the plan never
        asked for: a non-charge tick holds the idle mapping, a planned charge keeps its own
        (smaller, already clamped) row.
        """
        if not self._below_floor(ctx, readings):
            return None
        if self.trip_until is not None and ctx.now < self.trip_until:
            # The trip refuses charge for import_trip_hold_s, and the guard scopes that refusal to
            # charge intents: a discharge or idle intent passes it.  Returning None here would hand
            # the tick to the normal mapping and let the discharge drain the floor further while the
            # reserve is latched, so the hold keeps both directions off: no recovery charge (the
            # refusal stands) and no discharge (the floor stands).
            return self.idle_desired(readings, "below_soc_floor")
        power = self.settings.soc_floor_recovery_w
        intent = ctx.intent
        planned_charge = intent is not None and intent.mode == "charge"
        if planned_charge and float(intent.target_power_w) >= power:
            return None  # the plan already charges at least as hard: preserve it
        if self._charge_headroom_w(readings) < power:
            # The whole recovery does not fit under min(import_cap_w, entity max).  Raising anyway
            # would push the target to that ceiling, i.e. import at the cap while the house load is
            # already near it, so the recovery holds back: a non-charge tick keeps the idle mapping
            # and a planned charge keeps its own (smaller, already clamped) row untouched.
            return None if planned_charge else self.idle_desired(readings, "below_soc_floor")
        return self._charge_desired(power, readings, "below_soc_floor")

    def _charge_headroom_w(self, readings: Mapping[str, Any]) -> float:
        """W the charge mapping can still ask for above the house load (``0.0`` when none).

        The same effective ceiling the mapping uses: ``min(import_cap_w, entity max)``.  A missing
        load or ceiling is no headroom, so the recovery never invents one.
        """
        s, e = self.settings, self.settings.entities
        load = readings.get("house_load_w")
        if load is None:
            return 0.0
        entity_max = _attr(self._observed(e.pd_target), "max")
        ceiling = s.import_cap_w if entity_max is None else min(s.import_cap_w, entity_max)
        return max(0.0, ceiling - load)

    def _local_weekday(self, now: datetime) -> str:
        assert self._tz is not None
        return WEEKDAY_OPTIONS[now.astimezone(self._tz).weekday()]

    def _full_charge_target(
        self, ctx: ActuationContext, readings: Mapping[str, Any]
    ) -> tuple[bool | None, str | None, str | None]:
        """``(armed, weekday, refusal)``; ``armed`` is ``None`` when the feature is not driven.

        An armed directive that is refused becomes ``False`` (the switch is driven off) with the
        refusal reason; it is never written.
        """
        directive = ctx.full_charge
        if self._fc is None or directive is None:
            return None, None, None
        if not directive.armed:
            return False, None, None
        f = self._fc.entities
        refusal: str | None = None
        enable, day = self._observed(f.enable_switch), self._observed(f.day_select)
        options = day.attributes.get("options") if day is not None else None
        if directive == self._fc_failed:
            refusal = "write_failed"
        elif directive.weekday != self._local_weekday(ctx.now):
            refusal = "not_today"
        elif not self._kill_switch_on():
            refusal = "kill_switch_off"
        elif enable is None or not enable.available or day is None or not day.available:
            refusal = "entities_missing"
        elif not isinstance(options, list) or not set(WEEKDAY_OPTIONS) <= set(map(str, options)):
            refusal = "day_options_invalid"
        elif readings.get("weekly_delay") is None:
            refusal = "delay_switch_unreadable"
        elif readings["weekly_delay"]:
            refusal = "delay_switch_on"
        if refusal is not None:
            return False, None, refusal
        return True, directive.weekday, None

    def desired_for(
        self, intent: ShadowIntent, readings: Mapping[str, Any], ctx: ActuationContext | None = None
    ) -> Desired:
        """The mapping table of the module docstring (call only after a passing guard)."""
        s, e = self.settings, self.settings.entities
        load = readings.get("house_load_w")
        power = float(intent.target_power_w)
        if intent.mode == "charge":
            return self._charge_desired(power, readings, "intent")
        if intent.mode == "discharge":
            cap, cut = self._number_target(
                e.max_discharge_power,
                max(power, s.min_discharge_cap_w),
                s.min_discharge_cap_w,
                s.max_discharge_cap_w,
            )
            margin, _ = self._number_target(e.pd_target, s.discharge_margin_w, 0.0, s.import_cap_w)
            desired = Desired(
                mode="discharge",
                reason="intent",
                allow_discharge=True,
                pd_target_w=margin,
                power_limits=True,
                max_discharge_w=cap,
                house_load_w=load,
                clamped=cut > 0,
                clamped_w=round(min(cut, power), 3),
            )
            return self._load_following(desired, intent, readings, ctx)
        return self.idle_desired(readings, "intent")

    def _charge_desired(self, power: float, readings: Mapping[str, Any], reason: str) -> Desired:
        """The charge row of the mapping table, shared by the plan and the reserve protection.

        ``power`` is the AC charge power wanted; the PD target is ``house load + power`` bounded by
        ``min(import_cap_w, entity max)``: Omnibattery bounds the target by the batteries' charge
        limit, so the charge is at most that ceiling minus the house load.  Needs a fresh load;
        the floor or no-load cases never reach here (the guard holds safe idle first).
        """
        s, e = self.settings, self.settings.entities
        load = readings.get("house_load_w")
        if load is None:
            return safe_idle("battery_power_stale")
        target, cut = self._number_target(
            e.pd_target, float(math.floor(max(load + power, 0.0))), 0.0, s.import_cap_w
        )
        return Desired(
            mode="charge",
            reason=reason,
            allow_charge=True,
            pd_target_w=target,
            house_load_w=load,
            clamped=cut > 0,
            clamped_w=round(min(cut, power), 3),
        )

    def _load_following(
        self,
        desired: Desired,
        intent: ShadowIntent,
        readings: Mapping[str, Any],
        ctx: ActuationContext | None = None,
    ) -> Desired:
        """Raise the planned discharge cap to the load-following ceiling while the floor allows.

        ``floor: plan_soc`` (the default): engages when the measured SOC is at least the plan's
        end-of-slot SOC plus ``soc_hysteresis_pct``, stays engaged while it is at or above the
        plan's end-of-slot SOC, and releases below it (the planned cap again).  The latch carries
        over between consecutive discharge intents (each judged against its own SOC) and resets on
        anything else.  The raised cap is bounded like every cap write
        (``[min, max]_discharge_cap_w`` and the entity's own max) and never falls below the
        planned cap.

        ``floor: storage_value`` (ADR-0007 amendment 2026-11-11): see :meth:`_storage_value_floor`.
        """
        s, lf = self.settings, self.settings.load_following
        if not lf.enabled:
            return desired
        assert desired.max_discharge_w is not None
        soc = readings.get("battery_soc_pct")
        floor = 100.0 * intent.soc_target_frac
        rule = value = spread = None
        if lf.floor == "storage_value":
            rule, value, spread, floor, engaged = self._storage_value_floor(intent, soc, ctx, floor)
        else:
            threshold = floor if self._lf_engaged else floor + lf.soc_hysteresis_pct
            engaged = soc is not None and soc >= threshold - 1e-9
        cap = desired.max_discharge_w
        if engaged:
            raised, _ = self._number_target(
                s.entities.max_discharge_power,
                s.load_following_ceiling_w,
                s.min_discharge_cap_w,
                s.max_discharge_cap_w,
            )
            cap = max(cap, raised)
        return replace(
            desired,
            max_discharge_w=cap,
            load_following=engaged,
            planned_max_discharge_w=desired.max_discharge_w,
            load_following_floor_pct=round(floor, 3),
            load_following_rule=rule,
            load_following_storage_value_eur_per_kwh=value,
            load_following_spread_eur_per_kwh=None if spread is None else round(spread, 6),
        )

    def _storage_value_floor(
        self,
        intent: ShadowIntent,
        soc: float | None,
        ctx: ActuationContext | None,
        plan_floor_pct: float,
    ) -> tuple[str, float | None, float | None, float, bool]:
        """``(rule, value, spread, floor %, engaged)`` of the ``storage_value`` floor.

        The plan's marginal value of stored energy (``intent.storage_value_eur_per_kwh``, EUR per
        AC kWh discharged, wear and terminal value included) says what one kWh kept is worth; the
        slot's price net of the wear cost, ``price - throughput cost``, says what discharging it
        now earns.  While ``spread = price - throughput cost - value`` is at least
        ``storage_value_margin_eur_per_kwh`` the raised cap is engaged, so the battery covers the
        real load instead of the energy the forecast budgeted; otherwise the planned cap stands.
        Price, wear cost and value are constants of the slot, so the rule cannot flap inside it.

        The SOC is held away from the tariff's floor, because the plan's end-of-slot SOC no longer
        is the floor: it engages at ``floor + soc_floor_hysteresis_pct + soc_hysteresis_pct`` and
        releases below ``floor + soc_floor_hysteresis_pct``, the point at which reserve
        protection would release again, so the two never trade places.  With no value in the plan,
        no price, or no tariff floor the ``plan_soc`` rule decides (``rule == "plan_soc"``).
        """
        s, lf = self.settings, self.settings.load_following
        value = intent.storage_value_eur_per_kwh
        price = intent.expected_buy_price_eur_per_kwh
        reserve = ctx.soc_floor_pct if ctx is not None else None
        wear = ctx.throughput_cost_eur_per_kwh if ctx is not None else 0.0
        if (
            value is None
            or price is None
            or reserve is None
            or not all(math.isfinite(x) for x in (value, price, reserve, wear))
        ):
            plan_threshold = (
                plan_floor_pct if self._lf_engaged else plan_floor_pct + lf.soc_hysteresis_pct
            )
            engaged = soc is not None and soc >= plan_threshold - 1e-9
            return "plan_soc", None, None, plan_floor_pct, engaged
        spread = price - wear - value
        release = reserve + s.soc_floor_hysteresis_pct
        threshold = release if self._lf_engaged else release + lf.soc_hysteresis_pct
        engaged = (
            soc is not None
            and soc >= threshold - 1e-9
            and spread >= lf.storage_value_margin_eur_per_kwh - 1e-9
        )
        return "storage_value", value, spread, release, engaged

    def idle_desired(self, readings: Mapping[str, Any], reason: str) -> Desired:
        """Both directions off and the neutral PD target (the planned idle, or the settle hold)."""
        s, e = self.settings, self.settings.entities
        neutral, _ = self._number_target(e.pd_target, s.neutral_pd_target_w, 0.0, s.import_cap_w)
        return Desired(
            mode="idle",
            reason=reason,
            pd_target_w=neutral,
            house_load_w=readings.get("house_load_w"),
        )

    def _settling(self, ctx: ActuationContext) -> bool:
        """The directive holds the battery idle for the settle window (feature on only)."""
        directive = ctx.full_charge
        return (
            self._fc is not None
            and directive is not None
            and directive.settle_until is not None
            and ctx.now < directive.settle_until
        )

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

        fc = self._fc.entities if self._fc is not None else None
        if fc is not None and desired.full_charge_armed is False:
            switch("full_charge_enable", fc.enable_switch, False)  # disarm: restrictive, first
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
        if fc is not None and desired.full_charge_armed:
            # Arm: the day first (a select change while a run is active would abort it), then the
            # switch, both before any direction is switched on.
            assert fc.day_select is not None and desired.full_charge_weekday in WEEKDAY_OPTIONS
            day = Write(
                "full_charge_day",
                fc.day_select,
                "select",
                "select_option",
                option=desired.full_charge_weekday,
            )
            if not day.matches(self._observed(fc.day_select)):
                writes.append(day)
            switch("full_charge_enable", fc.enable_switch, True)
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
        value = (
            write.value
            if write.domain == "number"
            else write.option
            if write.domain == "select"
            else write.service.removeprefix("turn_")
        )
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
        self._aborted = None
        ok, done = await self._apply(self.plan_writes(desired, now))
        if ok:
            self._applied_mode = desired.mode
        return ok, done

    async def _apply(self, writes: list[Write]) -> tuple[bool, list[dict[str, Any]]]:
        done: list[dict[str, Any]] = []
        for write in writes:
            ok = await self._execute(write)
            if not ok and self._aborted:
                result = "aborted"
            else:
                result = "dry_run" if self.mode == "dry_run" else "ok" if ok else "failed"
            done.append({**write.to_dict(), "result": result})
            log.log(
                logging.WARNING if result == "failed" else logging.INFO,
                "actuator %s role=%s service=%s.%s value=%s result=%s",
                "dry_run write" if self.mode == "dry_run" else "write",
                write.role,
                write.domain,
                write.service,
                write.option or ("-" if write.value is None else f"{write.value:g}"),
                result,
            )
            if not ok:
                return False, done
        return True, done

    async def _disarm(self) -> list[dict[str, Any]]:
        """Best effort: turn the weekly switch off unless it already reads off (feature on only)."""
        if self._fc is None or not self._fc_driven or self._fc.entities.enable_switch is None:
            return []
        entity = self._fc.entities.enable_switch
        write = Write("full_charge_enable", entity, "switch", "turn_off")
        if write.matches(self._observed(entity)):
            return []
        _, done = await self._apply([write])
        return done

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

    # ------------------------------------------------------------------ discharge-time grid

    def _discharging_in_control(self, now: datetime) -> bool:
        """The last tick holds control, found the guard ok and wants discharge (and is recent)."""
        desired = self.desired
        return (
            self.in_control
            and not self.reasons
            and desired is not None
            and desired.mode == "discharge"
            and self.last_tick_at is not None
            and (now - self.last_tick_at).total_seconds() <= _TICK_FRESH_FACTOR * self._tick_s()
        )

    def _tick_s(self) -> float:
        return max(float(self.settings.tick_interval_s), 1.0)

    def observe_grid_power(self, state: EntityState, result: UpdateResult) -> None:
        """Feed ``actuator_discharge_grid_watts`` and ``actuator_export_seconds_total``.

        Called by the runtime for every accepted or re-confirmed grid-power state.  A sample
        counts only while the last tick was in control, guard ok and in discharge mode, the
        reading is fresh (``grid_power_max_age_s``) and the battery power is known (it gives the
        ``power_band``).  The histogram takes new values only (``ACCEPTED``); a re-confirmed
        value (``REFRESHED``) just keeps the export integration continuous.  Export seconds are
        the time between two consecutive fresh discharge-time samples, no further apart than
        ``grid_power_max_age_s`` (source time), when the earlier reading was below 0 W; a stale
        reading or a longer gap (an HA outage) resets the chain and counts nothing.
        """
        if self._metrics is None or result not in (UpdateResult.ACCEPTED, UpdateResult.REFRESHED):
            return
        now = self._clock.now()
        max_age = self.settings.grid_power_max_age_s
        value = state.numeric()
        fresh = value is not None and (now - state.observed_at).total_seconds() <= max_age
        discharging = fresh and self._discharging_in_control(now)
        previous = self._grid_prev
        self._grid_prev = (
            (state.observed_at, value, True) if discharging and value is not None else None
        )
        if not discharging or value is None:
            return
        if previous is not None and previous[1] < 0.0:
            gap = (state.observed_at - previous[0]).total_seconds()
            if 0.0 < gap <= max_age:
                self._metrics.actuator_export_seconds.inc(gap)
        if result is not UpdateResult.ACCEPTED:
            return
        e = self.settings.entities
        ac_raw = self._fresh(e.battery_ac_power, self.settings.battery_ac_power_max_age_s, now)
        if ac_raw is None:
            return  # no measured discharge power, no band
        discharge_w = max(0.0, -float(self.settings.battery_ac_power_sign or 1) * ac_raw)
        self._metrics.actuator_discharge_grid.labels(power_band=power_band(discharge_w)).observe(
            value
        )

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
                *self._full_charge_entities(),
            )
        )
        readings = self._read(now)
        self.readings = readings
        self._accrue_extra_discharge(now, readings)
        before = (self.kill_switch, self.in_control, self.reasons, self.desired, self.alert)
        self.kill_switch = self._kill_switch_on()
        self._fc_driven = self._fc_driven or (self._fc is not None and ctx.full_charge is not None)
        if self._fc_failed is not None and ctx.full_charge != self._fc_failed:
            self._fc_failed = None  # a new directive gets a fresh try
        refused_before = self.full_charge_refusal
        fc_armed, fc_weekday, self.full_charge_refusal = self._full_charge_target(ctx, readings)
        if self.full_charge_refusal not in (None, refused_before):
            log.warning("actuator full-charge arming refused reason=%s", self.full_charge_refusal)
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
                writes += await self._disarm()  # best effort, after the safe idle
                self.in_control = not ok
                self.alert = None if ok else "safe_idle_failed"
        else:
            self.in_control = True
            reasons = self.guard(ctx, readings)
            if reasons:
                desired = safe_idle(reasons[0])
            else:
                assert ctx.intent is not None
                if self._settling(ctx):
                    desired = self.idle_desired(readings, "settle_hold")
                else:
                    # Reserve protection (ADR-0007 amendment 2026-11-08): a bounded recovery
                    # charge while the measured SOC sits at or below the tariff floor.  The guard
                    # and the settle hold keep their priority; the recovery itself keeps no charge
                    # during the import trip's hold and refuses the planned discharge there too.
                    # The normal mapping runs when it does not apply, so a plan is only ever
                    # overridden near the floor.
                    recovery = self.reserve_desired(ctx, readings)
                    desired = (
                        recovery
                        if recovery is not None
                        else self.desired_for(ctx.intent, readings, ctx)
                    )
            if fc_armed is False or (fc_armed and desired.mode != "safe_idle"):
                # A safe idle never arms; it disarms only when the directive says so.
                desired = replace(
                    desired, full_charge_armed=fc_armed, full_charge_weekday=fc_weekday
                )
            ok, writes = await self._reconcile(desired, now)
            if self._aborted:  # the kill switch went off mid-reconcile: idle and hand off
                reasons = [self._aborted]
                self.kill_switch = False
                desired = safe_idle(self._aborted)
                ok, more = await self._reconcile(desired, now)
                writes += more + await self._disarm()  # the on -> off edge, mid-reconcile
                self.in_control = not ok
                self.alert = None if ok else "safe_idle_failed"
                ok = False  # no heartbeat: control is handed back
            elif ok:
                self.alert = None
            elif desired.mode != "safe_idle":
                if fc_armed and any(
                    w["role"] in FULL_CHARGE_ROLES and w["result"] == "failed" for w in writes
                ):
                    self._fc_failed = ctx.full_charge  # do not retry the pair every tick
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
        self._lf_engaged = desired is not None and desired.load_following is True
        self._note_engaged_tick(now, readings, desired)
        self._log_transitions(*before, readings)
        record = self._tick_record(ctx, readings, desired, writes, heartbeat)
        await self._maybe_record(now, record, bool(writes))
        return record

    def _note_engaged_tick(
        self, now: datetime, readings: Mapping[str, Any], desired: Desired | None
    ) -> None:
        """Remember an engaged ``storage_value``-floor tick so the next one can book its energy."""
        self._lf_prev = None
        if (
            self.settings.load_following.floor != "storage_value"
            or desired is None
            or not desired.load_following
            or desired.load_following_rule is None
        ):
            return
        assert desired.planned_max_discharge_w is not None
        ac = readings.get("battery_ac_power_w")
        self._lf_prev = (
            now,
            desired.planned_max_discharge_w,
            None if ac is None else max(0.0, -ac),
            desired.load_following_rule,
            desired.load_following_spread_eur_per_kwh,
        )

    def _accrue_extra_discharge(self, now: datetime, readings: Mapping[str, Any]) -> None:
        """Book the energy discharged above the planned cap since the previous engaged tick.

        Only with ``load_following.floor: storage_value``.  Over the interval the previous tick
        had load-following engaged, the battery could deliver the planned cap without it, so what
        it delivered above that cap is the raise's doing.  Trapezoid of the two measured discharge
        powers (kWh); a gap longer than twice ``tick_interval_s`` (a missed tick) is capped.  The
        value is ``spread`` (price - wear - storage value, EUR per kWh) times that energy, booked
        only when the ``storage_value`` rule decided.
        """
        prev, self._lf_prev = self._lf_prev, None
        if prev is None:
            return
        at, cap_w, before_w, rule, spread = prev
        ac = readings.get("battery_ac_power_w")
        dt = (now - at).total_seconds()
        if ac is None or before_w is None or dt <= 0.0:
            return
        dt = min(dt, 2.0 * self.settings.tick_interval_s)
        extra_w = (max(0.0, before_w - cap_w) + max(0.0, -ac - cap_w)) / 2.0
        kwh = extra_w * dt / 3_600_000.0
        self._lf_extra_kwh[rule] += kwh
        if rule == "storage_value" and spread is not None:
            self._lf_extra_value_eur += kwh * spread

    def _log_transitions(
        self,
        kill_switch: bool | None,
        in_control: bool,
        reasons: tuple[str, ...],
        desired: Desired | None,
        alert: str | None,
        readings: Mapping[str, Any],
    ) -> None:
        """One line per state change since the previous tick; nothing on an unchanged tick."""

        def onoff(value: bool | None) -> str:
            return "unknown" if value is None else "on" if value else "off"

        if self.kill_switch != kill_switch:
            log.info(
                "actuator kill_switch=%s previous=%s", onoff(self.kill_switch), onoff(kill_switch)
            )
        if self.in_control != in_control:
            log.info("actuator in_control=%s", self.in_control)
        if set(self.reasons) != set(reasons):
            log.info("actuator guard reasons=%s", ",".join(self.reasons) or "ok")
        tripped, was_tripped = "import_over_cap" in self.reasons, "import_over_cap" in reasons
        if tripped and not was_tripped:
            log.warning(
                "actuator import trip grid_power_w=%s cap_w=%.0f margin_w=%.0f hold_until=%s",
                readings.get("grid_power_w"),
                self.settings.import_cap_w,
                self.settings.import_guard_margin_w,
                self.trip_until.isoformat() if self.trip_until is not None else "-",
            )
        elif was_tripped and not tripped:
            log.warning("actuator import trip ended")
        new = self.desired
        if new is not None and (
            desired is None or (new.mode, new.reason) != (desired.mode, desired.reason)
        ):
            log.info(
                "actuator desired mode=%s reason=%s pd_target_w=%s max_discharge_w=%s clamped_w=%g",
                new.mode,
                new.reason,
                "-" if new.pd_target_w is None else f"{new.pd_target_w:g}",
                "-" if new.max_discharge_w is None else f"{new.max_discharge_w:g}",
                new.clamped_w,
            )
        engaged = bool(new is not None and new.load_following)
        if engaged != bool(desired is not None and desired.load_following):

            def num(value: float | None) -> str:
                return "-" if value is None else f"{value:g}"

            rule = new.load_following_rule if new is not None else None
            extra = ""
            if new is not None and rule is not None:
                stored = num(new.load_following_storage_value_eur_per_kwh)
                extra = (
                    f" rule={rule} storage_value={stored}"
                    f" spread={num(new.load_following_spread_eur_per_kwh)}"
                )
            log.info(
                "actuator load_following=%s max_discharge_w=%s planned_max_discharge_w=%s "
                "soc_pct=%s floor_pct=%s%s",
                "on" if engaged else "off",
                num(new.max_discharge_w if new is not None else None),
                num(new.planned_max_discharge_w if new is not None else None),
                num(readings.get("battery_soc_pct")),
                num(new.load_following_floor_pct if new is not None else None),
                extra,
            )
        if self.alert != alert:
            if self.alert is None:
                log.info("actuator alert cleared previous=%s", alert)
            else:
                level = logging.ERROR if self.alert == "safe_idle_failed" else logging.WARNING
                log.log(level, "actuator alert=%s last_error=%s", self.alert, self.last_error)

    async def shutdown(self) -> bool:
        """Safe idle before the process exits (when in control); ``False`` if it failed."""
        try:
            async with asyncio.timeout(self.settings.shutdown_timeout_s), self._lock:
                if not self.in_control:
                    return True
                now = self._clock.now()
                desired = safe_idle("shutdown")
                ok, writes = await self._reconcile(desired, now)
                writes += await self._disarm()  # best effort, after the safe idle
                self.in_control = not ok
                self.desired = desired
                self._lf_engaged = False
                self._lf_prev = None
                self._count("actuator_safe_idles", reason="shutdown")
                log.log(
                    logging.INFO if ok else logging.ERROR,
                    "actuator shutdown safe_idle ok=%s writes=%d",
                    ok,
                    len(writes),
                )
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
            "reserve": {
                "floor_pct": self._floor_pct,
                "engaged": self._floor_engaged,
                "recovery_w": self.settings.soc_floor_recovery_w,
                "hysteresis_pct": self.settings.soc_floor_hysteresis_pct,
            },
            "last_tick_at": iso(self.last_tick_at),
            "last_successful_write_at": iso(self.last_success_write_at),
            "last_heartbeat_at": iso(self.last_heartbeat_at),
            **({"full_charge": self._full_charge_view()} if self._fc is not None else {}),
        }

    def _full_charge_view(self) -> dict[str, Any]:
        """The weekly readings and the refusal, for ``GET /status`` and the scheduler."""
        r = self.readings
        return {
            "enable_switch": r.get("weekly_enable"),
            "day": r.get("weekly_day"),
            "delay_switch": r.get("weekly_delay"),
            "integration_status": r.get("integration_status"),
            "driven_armed": self.desired.full_charge_armed if self.desired is not None else None,
            "settle_hold": self.desired is not None and self.desired.reason == "settle_hold",
            "refusal": self.full_charge_refusal,
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
            "clamped_w": desired.clamped_w if desired else None,
            "observed_battery_ac_power_w": self.readings.get("battery_ac_power_w"),
            "observed_grid_power_w": self.readings.get("grid_power_w"),
            "house_load_w": self.readings.get("house_load_w"),
            "observed_pd_target_w": self.readings.get("pd_target_w"),
            "reserve_engaged": 1.0 if self._floor_engaged else 0.0,
            "discharge_margin_w": self.settings.discharge_margin_w,
            **({"full_charge": self._full_charge_snapshot()} if self._fc is not None else {}),
            **(
                {"load_following": self._load_following_snapshot()}
                if self.settings.load_following.enabled
                else {}
            ),
        }

    def _load_following_snapshot(self) -> dict[str, Any]:
        """Engaged (1/0) and the cap raise (W) of the last tick; ``None`` before the first."""
        desired = self.desired
        if self.last_tick_at is None:
            return {"engaged": None, "raise_w": None}
        engaged = desired is not None and desired.load_following is True
        out: dict[str, Any] = {
            "engaged": 1.0 if engaged else 0.0,
            "raise_w": desired.load_following_raise_w if desired is not None else 0.0,
        }
        if self.settings.load_following.floor == "storage_value":
            rule = desired.load_following_rule if desired is not None else None
            out.update(
                {
                    "storage_value_eur_per_kwh": (
                        desired.load_following_storage_value_eur_per_kwh if desired else None
                    ),
                    "spread_eur_per_kwh": (
                        desired.load_following_spread_eur_per_kwh if desired else None
                    ),
                    "value_rule": None
                    if rule is None
                    else (1.0 if rule == "storage_value" else 0.0),
                    "extra_kwh": dict(self._lf_extra_kwh),
                    "extra_value_eur": self._lf_extra_value_eur,
                }
            )
        return out

    def _full_charge_snapshot(self) -> dict[str, Any]:
        r = self.readings

        def flag(value: bool | None) -> float | None:
            return None if value is None else (1.0 if value else 0.0)

        return {
            "enable_switch": flag(r.get("weekly_enable")),
            "day": r.get("weekly_day"),
            "delay_switch": flag(r.get("weekly_delay")),
            "integration_status": r.get("integration_status"),
            "refusal": self.full_charge_refusal,
        }
