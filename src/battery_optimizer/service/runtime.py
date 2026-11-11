"""The resident shadow service: adapters, MPC loop, intents and shadow sink.

Concurrency model
-----------------
One asyncio event loop owns all state.  Blocking work leaves it:

* the CPU-bound solve runs in a ``ThreadPoolExecutor(max_workers=1)`` with a
  timeout.  A solve that times out keeps the single worker until its thread
  really ends (``asyncio.wait_for`` cannot kill a thread), so a new solve is
  refused as ``busy`` instead of starting a second worker — solves never overlap;
* every store write runs on a second single-thread executor, which keeps
  appends in order, and Prometheus history fetches on a third, so a slow
  Prometheus never delays a slot close or a plan write.

Scheduling
----------
For every slot boundary ``B`` (UTC grid, ``interval_seconds``):

* ``B - solve_lead_s``: scheduled solve; the plan starts at ``B``;
* ``B``: the slot opens — the decision for ``[B, B + interval)`` is frozen: the
  intent valid at ``B`` from the newest unexpired plan, or no decision with a
  reason (no plan, plan expired, inputs not ready);
* while a slot that opened **without** an intent runs, it is re-evaluated every
  ``_ADOPT_RETRY_S`` and after every new plan: once the inputs are ready and an
  unexpired plan of this process covers it, it adopts that intent for the rest of
  the slot (``late_intent``) and is frozen from then on.  A solve made inside such a
  slot (before ``B + interval - solve_lead_s``) plans from the slot in progress (a
  catch-up plan), and the scheduler asks for one such solve per slot when the inputs
  are ready but no plan covers it.  A slot that opened with an intent is never
  re-decided;
* ``B + slot_close_grace_s``: the previous slot closes — its measurements, its
  decision and the virtual-battery replay step are appended to the store.

Significant state changes (SOC moved, next-day prices published, a completed
resync) request an extra solve after a debounce, rate-limited.

Passive archives
----------------
Two optional append-only archives ride on the same sink (:mod:`.archive`): a snapshot
of every price-entity state change and a periodic ``weather.get_forecasts`` fetch.
They are collected on the event loop but never read by the planner; a failure is
counted and logged and never touches readiness, a solve or a slot close.

Climate shadow
--------------
With ``climate_shadow.enabled`` the track-B2 job (:mod:`.climate_shadow`) runs hourly on its own
worker thread: B0's thermal model and the comfort MPC on live data, recorded and scored, never
sent.  It reads three optional HA helpers through the entity store and the weather archive; a
failure is counted and reported and never touches readiness or the battery planner.  Each decision
is also shaped into an advisory ClimatePlan (:mod:`battery_optimizer.climate.plan_export`) that
``GET /climate/plan`` serves while the owner's mode select (read-only, like the battery kill switch;
else ``climate_shadow.optimizer_mode``, default ``off``) says ``shadow`` or ``live``: ``live`` is a
request only, the effective mode is never ``live``, and nothing is written anywhere.

Safety
------
In ``mode: shadow`` (the default) there is no actuation path: the HA adapters are
read-only by construction (:mod:`.ha_guard`; its single exception is the
``weather.get_forecasts`` query for the archive), and intents are records, not commands.
On start, after every reconnect and whenever an input is stale, no decision is taken
until a fresh, complete snapshot exists; a persisted plan is never resumed.

Periodic full charge
--------------------
With ``full_charge.enabled`` the pure scheduler (:mod:`.full_charge`) decides on which day
Omnibattery's weekly full charge runs.  The runtime owns the I/O around it: a 10 s loop builds an
observation from the entity store and the actuator, steps the scheduler and hands the actuator its
directive (nothing until the first step after a Home Assistant sync and none while suspended: the
feature is hands-off then); after a regular plan is published, one evaluation solve prices tomorrow
(:func:`~.planner.evaluate_full_charge`); once a day is chosen the snapshot carries a ``commit``
spec until the run ends.  State and records go to the sink; ``shadow`` and ``dry_run`` use the
simulated writer (nothing runs on the battery).  A time zone check against Home Assistant's
``get_config`` gates the feature.

Live control (ADR-0007)
-----------------------
In ``dry_run`` and ``live`` an :class:`~.actuator.Actuator` maps the intent frozen for the
current slot to the Omnibattery entities every ``actuation.tick_interval_s`` and at every
slot open and new plan.  ``dry_run`` records what it would write and sends nothing; only
``live`` builds the HA client with an :class:`~.ha_guard.ActuationAllowlist`, the one path
by which a write can leave the process.  Only plans solved by this process are acted on (a
late-adopted intent included), and a graceful stop sets safe idle before the HA session closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from battery_optimizer.climate import plan_export
from battery_optimizer.climate.shadow import LiveInputs, detect_regime
from battery_optimizer.config import TariffConfig

from .actuator import LIVENESS_OK_STATE, ActuationContext, Actuator
from .archive import (
    next_fetch_time,
    pvpc_snapshot_record,
    retry_delay_s,
    weather_forecast_record,
)
from .battery_health import BatteryHealth, HealthParams, parse_text, parse_timestamp
from .climate_shadow import ClimateShadowJob, build_job, latest_forecast, next_run_time
from .clock import Clock
from .config import ServiceConfig
from .entity_store import EntityState, EntityStore, UpdateResult
from .full_charge import (
    ALERT_REASONS,
    COMMIT_STATES,
    REFUSAL_ALERTS,
    FullChargeScheduler,
    FullChargeState,
    Observation,
    PlanSlot,
    arm_due,
    day_window,
)
from .ha_client import HACommandError, HARestClient, HAWebSocketClient, WSTransport
from .ha_guard import actuation_allowlist
from .history import ForecastUnavailableError, LoadHistory, forecast_load
from .metrics import ServiceMetrics
from .observer import SlotMeasurements, SlotObserver, normalise_value
from .plan_tracker import (
    DayAheadTracker,
    SlotPlan,
    revision_kwh,
    slot_plans_from_result,
)
from .planner import (
    FullChargeSpec,
    PlanningSnapshot,
    PlanResult,
    ShadowIntent,
    battery_spec_from_tariff,
    clamp_soc,
    evaluate_full_charge,
    run_planner,
)
from .pvpc import PriceCurve, PriceParseError, parse_pvpc_state
from .sink import ShadowSink
from .thermal import refit as refit_thermal_model
from .thermal_tracker import ThermalTracker
from .timegrid import floor_slot, local_date, local_day_bounds_utc, next_slot_boundary
from .virtual_battery import SlotStep, intent_energy_kwh, soc_violation, step_virtual_battery

__all__ = ["NotReadyError", "Readiness", "ShadowService"]

log = logging.getLogger(__name__)

HistoryFetcher = Callable[[datetime, date], LoadHistory]
Planner = Callable[[PlanningSnapshot], PlanResult]

_HISTORY_RETRY_S = 600.0
_ACTUATION_TEXT = {
    "shadow": "none (read-only adapters; intents are recorded, never sent)",
    "dry_run": "none (writes are computed and recorded under actuation/, never sent)",
    "live": "Omnibattery entities through the ha_guard allowlist, gated by the kill switch",
}
_PLAN_MEMORY = 16
#: How often a slot that opened without an intent is re-evaluated for a late adoption.
_ADOPT_RETRY_S = 15.0
#: Period of the battery-health clock advance (well inside ``battery_health.max_gap_s``).
_HEALTH_TICK_S = 30.0
#: Period of the thermal shadow's buffer tick and the interval between saves of its buffer.
_THERMAL_TICK_S = 60.0
_THERMAL_SAVE_S = 1800.0
#: Period of the periodic full charge's observation loop (inside ``ack_timeout_s``).
_FULL_CHARGE_TICK_S = 10.0
#: Retry period of the Home Assistant time-zone check.
_TZ_CHECK_RETRY_S = 60.0
#: Longest the startup catch-up of the climate shadow waits for the first Home Assistant sync.
CLIMATE_SYNC_TIMEOUT_S = 60.0


def _fmt(value: float | None, digits: int = 0) -> str:
    """A number for a ``key=value`` log field (``-`` when unknown)."""
    return "-" if value is None else f"{value:.{digits}f}"


def _intent_text(intent: ShadowIntent | None) -> str:
    if intent is None:
        return "mode=- power_w=- plan_id=-"
    return f"mode={intent.mode} power_w={intent.target_power_w:.0f} plan_id={intent.plan_id}"


def _soc_floor_pct(tariff: TariffConfig | None) -> float | None:
    """The authoritative SOC floor in percent for reserve protection, or ``None`` (protection off).

    Single source: ``battery.soc_window_min`` of ``config/tariff.yaml``, the same value the
    planner clamps to.  A tariff that does not parse as a finite fraction of nameplate gives
    ``None``: the actuator then protects nothing rather than acting on a bogus floor (the spec
    build in :meth:`ShadowService._base_spec` is where a bad window is reported).
    """
    if tariff is None:
        return None
    try:
        floor = float(tariff.battery.soc_window_min)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    if not math.isfinite(floor) or not 0.0 <= floor <= 1.0:
        return None
    return 100.0 * floor


def _log_plan(result: PlanResult, trigger: str, elapsed: float) -> None:
    snap = result.snapshot
    first = result.intent_for_slot(snap.first_slot)
    log.info(
        "plan generated plan_id=%s trigger=%s status=%s solve_s=%.2f slots=%d first_slot=%s "
        "first_mode=%s first_power_w=%s soc=%s soc_measured=%s soc_clamped=%s soc_source=%s",
        result.plan_id,
        trigger,
        result.solver.get("status"),
        elapsed,
        len(snap.slot_starts),
        snap.first_slot.isoformat(),
        first.mode if first is not None else "-",
        _fmt(first.target_power_w if first is not None else None),
        _fmt(snap.soc_frac, 3),
        _fmt(snap.soc_measured_frac, 3),
        snap.soc_clamped,
        snap.soc_source,
    )


class NotReadyError(Exception):
    """Planning refused: an input is missing or stale."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Readiness:
    ready: bool
    inputs_ready: bool
    reasons: tuple[str, ...]
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class _SlotOpen:
    slot_start: datetime
    intent: ShadowIntent | None
    decision: str
    inputs_ready: bool
    reasons: tuple[str, ...]
    opened_at: datetime
    #: The decision frozen at open; ``decision`` differs only after a late adoption.
    decision_at_open: str = ""
    #: When a slot that opened without an intent adopted one (``None``: it did not).
    adopted_at: datetime | None = None
    #: The first plan that covered the slot (day-ahead) and the plan of record (final), as the
    #: plans said it: frozen at open (final also at a late adoption), ``None`` when absent.
    day_ahead: SlotPlan | None = None
    final: SlotPlan | None = None

    def intent_share(self, interval_seconds: int) -> float:
        """The share of the slot the intent was in force: 1, or less once adopted late."""
        if self.adopted_at is None:
            return 1.0
        end = self.slot_start + timedelta(seconds=interval_seconds)
        return min(1.0, max(0.0, (end - self.adopted_at).total_seconds() / interval_seconds))


class ShadowService:
    def __init__(
        self,
        *,
        config: ServiceConfig,
        tariff: TariffConfig | None,
        data_dir: Path,
        clock: Clock,
        code_version: str,
        tariff_error: str | None = None,
        ws_transport: WSTransport | None = None,
        rest_transport: Any = None,
        history_fetcher: HistoryFetcher | None = None,
        planner: Planner = run_planner,
        enable_ha: bool = True,
        climate_job_factory: Callable[[], ClimateShadowJob] | None = None,
        climate_sync_timeout_s: float = CLIMATE_SYNC_TIMEOUT_S,
    ) -> None:
        self.config = config
        self.tariff = tariff
        self.tariff_error = tariff_error
        self.clock = clock
        self.code_version = code_version
        self.data_dir = data_dir
        self.sink = ShadowSink(data_dir)
        self._planner = planner
        self._history_fetcher = history_fetcher
        self._interval = config.planning.interval_seconds
        self._tz = config.tz

        climate_entities = config.climate_shadow.entities() if config.climate_shadow.enabled else ()
        acting = config.mode != "shadow"
        actuation_entities = config.actuation.entities.all() if acting else ()
        if config.planning.soc_source == "entity" and config.actuation.entities.battery_liveness:
            # The entity SOC is only trusted while Omnibattery reports the battery responsive.
            actuation_entities = (*actuation_entities, config.actuation.entities.battery_liveness)
        # Read-only battery health telemetry: the sensors and, for charge sessions, the AC power
        # entity and sign the actuator uses (tracked in every mode, never written).
        self.health: BatteryHealth | None = None
        self._health_roles: dict[str, str] = {}
        health_cfg = config.battery_health
        if health_cfg.configured:
            ac_entity = config.actuation.entities.battery_ac_power
            sign = config.actuation.battery_ac_power_sign
            self._health_roles = health_cfg.entities.by_entity()
            if ac_entity and sign is not None:
                self._health_roles.setdefault(ac_entity, "charge_power")
            self.health = BatteryHealth(
                HealthParams(
                    hot_thresholds_c=health_cfg.hot_thresholds_c,
                    charge_threshold_w=health_cfg.charge_threshold_w,
                    charge_power_sign=sign if sign is not None else 1,
                    session_end_hold_s=health_cfg.session_end_hold_s,
                    max_gap_s=health_cfg.max_gap_s,
                )
            )
        # Read-only thermal state and derate prediction (planning.thermal.mode shadow): the
        # Omnibattery temperature-limit entities, the temperature sensor and the AC power (the
        # same entity and sign the actuator uses).  Tracked in every service mode, never written.
        self.thermal: ThermalTracker | None = None
        self._thermal_roles: dict[str, str] = {}
        thermal_cfg = config.planning.thermal
        if thermal_cfg.active:
            sign = config.actuation.battery_ac_power_sign
            ac_entity = config.actuation.entities.battery_ac_power
            assert sign is not None and ac_entity  # validated by the config
            self._thermal_roles = thermal_cfg.entities.by_entity()
            self._thermal_roles[ac_entity] = "charge_power"
            self.thermal = ThermalTracker(thermal_cfg, ac_power_sign=sign)
        # The periodic full charge reads its balance sensors, the weekly entities and Omnibattery's
        # status; an entity named by battery_health too keeps one store entry (dict.fromkeys).
        fc_cfg = config.full_charge
        full_charge_entities = (
            (
                *fc_cfg.entities.all(),
                *(e for e in (config.actuation.entities.integration_status,) if e),
            )
            if fc_cfg.enabled
            else ()
        )
        self.store = EntityStore.for_entities(
            tuple(
                dict.fromkeys(
                    (
                        *config.entities.all_entities(),
                        *climate_entities,
                        *actuation_entities,
                        *self._health_roles,
                        *self._thermal_roles,
                        *full_charge_entities,
                    )
                )
            )
        )
        self.observer = SlotObserver(config)
        self.store.add_listener(self.observer.on_state)
        self.store.add_listener(self._on_state)

        self.metrics = ServiceMetrics(
            snapshot=self.metric_snapshot,
            actuation=acting,
            full_charge=fc_cfg.enabled,
            thermal=self.thermal is not None,
        )
        from battery_optimizer import __version__

        self.metrics.build_info.labels(
            code_version=code_version,
            version=__version__,
            soc_source=config.planning.soc_source,
            mode=config.mode,
        ).set(1)
        for override in config.overrides:
            self.metrics.config_override_info.labels(path=override.path).set(1)

        ha = config.home_assistant
        self.ha: HAWebSocketClient | None = None
        self.rest: HARestClient | None = None
        if enable_ha and ha.url and ha.token:
            self.ha = HAWebSocketClient(
                settings=ha,
                store=self.store,
                clock=clock,
                transport=ws_transport,
                on_synced=self._on_synced,
                on_disconnected=self._on_disconnected,
                actuation=actuation_allowlist(config),  # None unless mode: live
            )
            self.rest = HARestClient(settings=ha, transport=rest_transport)

        self.price_curve: PriceCurve | None = None
        self.price_error: str | None = None
        self.history: LoadHistory | None = None
        self.history_error: str | None = None
        self._history_last_attempt: float | None = None
        self.forecast_error: str | None = None

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bo-planner")
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bo-io")
        # History fetches can take minutes when Prometheus is slow; they get their own
        # thread so they never delay a slot close or a plan write queued behind them.
        self._history_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bo-history")
        self._worker_busy = False
        self._generation = 0
        self._plan_sequence = int(clock.now().timestamp())
        self.plans: deque[PlanResult] = deque(maxlen=_PLAN_MEMORY)
        #: The day-ahead plan of each slot, rebuilt from the plan files on start.
        self.day_ahead = DayAheadTracker()
        self.last_solve: dict[str, Any] | None = None
        self._last_solve_started: datetime | None = None
        #: The last skip reason logged at INFO: a skip repeating every slot is logged once.
        self._logged_skip: str | None = None
        self._replan_task: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_at = clock.now()

        self._pvpc_snapshot_seen = False
        self.archive_last_success: dict[str, datetime] = {}
        self.weather_error: str | None = None

        self._slot_open: dict[datetime, _SlotOpen] = {}
        self._opened_slot: datetime | None = None
        self._closed_slot: datetime | None = None
        self._solved_for: datetime | None = None
        self._caught_up_for: datetime | None = None

        self.virtual_soc: float | None = None
        self.virtual_soc_origin: str | None = None
        self._init_virtual_battery()

        #: ``(plan_id, thermal block)`` of the last plans: the shadow prediction next to each plan.
        self._thermal_blocks: deque[tuple[str, dict[str, Any]]] = deque(maxlen=_PLAN_MEMORY)
        self._thermal_saved_at: datetime | None = None
        self._thermal_refit_running = False
        self._thermal_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bo-thermal")
        self._init_thermal()

        # Periodic full charge (off by default).  ``_fc_directive`` is ``None`` (hands off) until
        # first step after a sync, and while the feature is suspended.
        self.full_charge: FullChargeScheduler | None = None
        self._fc_directive: Any = None
        self._fc_alerts: set[str] = set()  # runtime-owned alerts (the scheduler owns the rest)
        self._fc_state_problem: str | None = None
        self._fc_tz_ok: bool | None = None
        self._fc_ha_time_zone: str | None = None
        self._fc_entities_problem: str | None = None
        self._fc_save_failed = False
        self._init_full_charge()

        self._climate_sync_timeout_s = climate_sync_timeout_s
        self._ha_synced = asyncio.Event()  # set by the first HA sync; the catch-up run waits on it
        self.climate: ClimateShadowJob | None = None
        self.climate_error: str | None = None
        self._climate_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bo-climate")
        self.actuator: Actuator | None = None
        self._actuator_task: asyncio.Task[None] | None = None
        self._actuator_wake = asyncio.Event()
        self._overcap_wake_at: datetime | None = None
        if acting:
            self.actuator = Actuator(
                settings=config.actuation,
                mode=config.mode,
                store=self.store,
                clock=clock,
                grid_entity=config.entities.get("grid_power"),
                send=self._send_actuation,
                refresh=self._refresh_entities,
                record=self._record_actuation,
                metrics=self.metrics,
                # With the feature in shadow beside live control the actuator never hears of it:
                # the weekly entities are never touched and the weekly status is never healthy.
                full_charge=None if config.full_charge_isolated else config.full_charge,
                tz=self._tz,
            )
        self._climate_factory = climate_job_factory or (
            lambda: build_job(
                config,
                data_dir=data_dir,
                sink=self.sink,
                code_version=code_version,
                metrics=self.metrics,
            )
        )

    # ------------------------------------------------------------------ lifecycle

    async def rebuild_day_ahead(self) -> int:
        """Rebuild the day-ahead tracker from the plan files of the last two UTC days.

        Plans are immutable and ordered by ``(generated_at, plan_sequence)``, so this gives
        what an uninterrupted process would hold.  A failure is logged and leaves the tracker
        to the plans of this process: observability only, never a reason not to start.
        """
        now = self.clock.now()
        try:
            files = await asyncio.get_running_loop().run_in_executor(
                self._io, self.day_ahead.rebuild, self.data_dir, now
            )
        except Exception as exc:  # noqa: BLE001 - never blocks the start
            log.warning("day-ahead rebuild failed error=%s: %s", type(exc).__name__, exc)
            return 0
        log.info("day-ahead rebuilt plan_files=%d slots=%d", files, len(self.day_ahead))
        return files

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.rebuild_day_ahead()
        await self._record_event("service_started", {"soc_source": self.config.planning.soc_source})
        if self.ha is not None:
            self._tasks.append(asyncio.create_task(self.ha.run(), name="ha-websocket"))
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="ha-rest-poll"))
        self._tasks.append(asyncio.create_task(self._history_loop(), name="history"))
        self._tasks.append(asyncio.create_task(self._scheduler_loop(), name="scheduler"))
        if self.ha is not None and self.config.archive.weather_forecast.enabled:
            self._tasks.append(
                asyncio.create_task(self._weather_archive_loop(), name="weather-archive")
            )
        if self.config.climate_shadow.enabled:
            self._tasks.append(asyncio.create_task(self._climate_loop(), name="climate-shadow"))
        if self.health is not None:
            self._tasks.append(asyncio.create_task(self._health_loop(), name="battery-health"))
        if self.thermal is not None:
            self._tasks.append(asyncio.create_task(self._thermal_loop(), name="thermal-shadow"))
        if self.full_charge is not None:
            self._tasks.append(
                asyncio.create_task(self._full_charge_loop(), name="full-charge-scheduler")
            )
        if self.actuator is not None:
            self._actuator_task = asyncio.create_task(self._actuator_loop(), name="actuator")

    async def stop(self) -> None:
        self._generation += 1  # fence any in-flight solve
        if self.actuator is not None:
            # No tick may race the shutdown write; then safe idle while HA is still connected.
            if self._actuator_task is not None:
                self._actuator_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._actuator_task
                self._actuator_task = None
            with contextlib.suppress(Exception):
                await self.actuator.shutdown()
        if self.ha is not None:
            await self.ha.stop()
        tasks = [*self._tasks, *([self._replan_task] if self._replan_task else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self.thermal is not None:
            with contextlib.suppress(Exception):
                await self._save_thermal(self.clock.now(), samples=True)
        self._thermal_pool.shutdown(wait=False, cancel_futures=True)
        if self.rest is not None:
            with contextlib.suppress(Exception):
                await self.rest.aclose()
        with contextlib.suppress(Exception):
            await self._record_event("service_stopped", {})
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._climate_pool.shutdown(wait=False, cancel_futures=True)
        self._io.shutdown(wait=True)
        self._history_pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ HA hooks

    def _on_synced(self) -> None:
        self.observer.mark_synced(self.clock.now())
        self._ha_synced.set()
        self._wake_actuator()
        self._spawn(
            self._record_event("ha_synced", {"resyncs": self.ha.stats.resyncs if self.ha else 0})
        )
        self._request_replan("resync")

    def _on_disconnected(self) -> None:
        # A silently dead session is only noticed by the heartbeat: back the end of the
        # sync window off to the last moment the session was proven alive.
        ha = self.config.home_assistant
        unproven = timedelta(seconds=ha.heartbeat_interval_s + ha.heartbeat_timeout_s)
        self.observer.mark_unsynced(self.clock.now() - unproven)
        detail = self.ha.stats.last_error if self.ha else None
        self._spawn(self._record_event("ha_disconnected", {"error": detail}))

    def _spawn(self, coro: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        task = loop.create_task(coro)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _on_state(self, state: EntityState, result: UpdateResult) -> None:
        health_role = self._health_roles.get(state.entity_id)
        if self.health is not None and health_role is not None:
            self.health.observe(health_role, state.state, self.clock.now())
        thermal_role = self._thermal_roles.get(state.entity_id)
        if self.thermal is not None and thermal_role is not None:
            self.thermal.observe(thermal_role, state.state, self.clock.now())
        role = self.config.entities.role_of(state.entity_id)
        if self.actuator is not None:
            self._actuation_wake_on(state, role)
        if (
            role == "price"
            and result is UpdateResult.ACCEPTED
            and self.config.archive.pvpc_snapshots.enabled
        ):
            self._archive_pvpc_snapshot(state)
        if role == "price":
            had_tomorrow = bool(self.price_curve and self.price_curve.tomorrow_published)
            try:
                self.price_curve = parse_pvpc_state(
                    state, self._tz, interval_seconds=self._interval
                )
                self.price_error = None
            except PriceParseError as exc:
                self.price_curve = None
                self.price_error = str(exc)
                return
            if self.price_curve.tomorrow_published and not had_tomorrow:
                log.info(
                    "prices published reference_day=%s slots=%d",
                    self.price_curve.reference_day.isoformat(),
                    len(self.price_curve.prices),
                )
                self._spawn(self._record_event("prices_published", self.price_curve.summary()))
                self._request_replan("prices_published")
        elif role == "battery_soc" and result is UpdateResult.ACCEPTED:
            value = normalise_value("battery_soc", state)
            last = self.plans[-1].snapshot.soc_frac if self.plans else None
            if value is not None and last is not None:
                if abs(value - last) >= self.config.planning.replan_soc_change_frac:
                    self._request_replan("soc_change")

    # ------------------------------------------------------------------ passive archives

    def _archive_pvpc_snapshot(self, state: EntityState) -> None:
        """Queue one snapshot of a price state change; never raises into planning.

        ``ACCEPTED`` only: a ``REFRESHED`` observation is the same state re-confirmed
        (the 60 s REST poll), not a change.  The first state this process sees is the
        startup record (``trigger: initial``).
        """
        trigger = "change" if self._pvpc_snapshot_seen else "initial"
        self._pvpc_snapshot_seen = True
        try:
            observed_at = self.clock.now()
            record = pvpc_snapshot_record(
                state,
                observed_at=observed_at,
                tz=self._tz,
                trigger=trigger,
                code_version=self.code_version,
            )
            self._spawn(self._write_pvpc_snapshot(observed_at, record))
        except Exception:  # noqa: BLE001 - collection must never reach planning
            self.metrics.pvpc_snapshots.labels(result="failed").inc()
            log.exception("PVPC snapshot could not be built")

    async def _write_pvpc_snapshot(self, at: datetime, record: dict[str, Any]) -> None:
        ok = await self._archive_write("pvpc_snapshots", self.sink.append_pvpc_snapshot, at, record)
        self.metrics.pvpc_snapshots.labels(result="written" if ok else "failed").inc()

    async def _archive_write(
        self, archive: str, fn: Callable[..., Any], at: datetime, record: dict[str, Any]
    ) -> bool:
        """Append one archive record on the store thread; ``False`` (logged) on any failure."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._io, fn, at, record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - OSError, bad JSON value, executor shut down
            log.error("archive %s write failed: %s: %s", archive, type(exc).__name__, exc)
            return False
        now = self.clock.now()
        self.archive_last_success[archive] = now
        self.metrics.archive_last_success.labels(archive=archive).set(now.timestamp())
        return True

    async def archive_weather_forecast(self) -> bool:
        """Fetch and append one forecast; ``False`` (counted, logged) on any failure."""
        cfg = self.config.archive.weather_forecast
        requested_at = self.clock.now()
        try:
            if self.ha is None:
                raise HACommandError("weather_forecast", "Home Assistant is not configured")
            forecast = await self.ha.get_weather_forecast(cfg.entity_id, cfg.type)
            if not forecast:
                raise HACommandError("weather_forecast", f"empty forecast for {cfg.entity_id}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, backed off, never fatal
            self.weather_error = f"{type(exc).__name__}: {exc}"[:500]
            self.metrics.weather_forecast_fetches.labels(result="failed").inc()
            log.warning("weather forecast fetch failed: %s", self.weather_error)
            return False
        record = weather_forecast_record(
            requested_at=requested_at,
            received_at=self.clock.now(),
            entity_id=cfg.entity_id,
            forecast_type=cfg.type,
            forecast=forecast,
            code_version=self.code_version,
        )
        ok = await self._archive_write(
            "weather_forecast", self.sink.append_weather_forecast, requested_at, record
        )
        self.metrics.weather_forecast_fetches.labels(result="ok" if ok else "failed").inc()
        self.weather_error = None if ok else "store write failed"
        if ok:
            log.info(
                "weather forecast archived entity=%s type=%s entries=%d",
                cfg.entity_id,
                cfg.type,
                len(forecast),
            )
        return ok

    async def _weather_archive_loop(self) -> None:
        """Fetch on the UTC grid (``offset_minutes`` past every ``interval_minutes``);
        after a failure retry with capped backoff, never past the next grid instant."""
        cfg = self.config.archive.weather_forecast
        due = next_fetch_time(self.clock.now(), cfg.interval_minutes, cfg.offset_minutes)
        retry_at: datetime | None = None
        failures = 0
        while True:
            target = due if retry_at is None else min(due, retry_at)
            await self.clock.sleep(max(0.0, (target - self.clock.now()).total_seconds()))
            if target == due:  # the grid instant is being served (or overdue)
                due = next_fetch_time(
                    max(due, self.clock.now() - timedelta(seconds=1)),
                    cfg.interval_minutes,
                    cfg.offset_minutes,
                )
            try:
                ok = await self.archive_weather_forecast()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive one bad fetch
                log.exception("weather archive iteration failed")
                self.metrics.weather_forecast_fetches.labels(result="failed").inc()
                ok = False
            now = self.clock.now()
            if ok:
                failures, retry_at = 0, None
            else:
                failures += 1
                retry_at = now + timedelta(seconds=retry_delay_s(failures))

    # ------------------------------------------------------------------ climate shadow

    def climate_inputs(self, now: datetime) -> LiveInputs:
        """The HA states the climate job reads (on the loop: the store is the loop's)."""
        s = self.config.climate_shadow
        synced = self.ha is not None and self.ha.stats.synced

        def state(entity: str | None) -> EntityState | None:
            return self.store.get(entity) if entity else None

        reg, dhw, away = state(s.regime_entity), state(s.dhw_plan_entity), state(s.away_entity)
        if s.regime_entity:
            regime = detect_regime(
                reg.state if reg else None, exists=reg is not None, synced=synced
            )
        else:
            regime = "legacy" if synced else "unknown"
        return LiveInputs(
            regime=regime,
            regime_state=reg.state if reg else None,
            dhw_plan=dhw.state if dhw is not None and dhw.available else None,
            away=(away.state.strip().lower() == "on")
            if away is not None and away.available
            else None,
        )

    async def run_climate_shadow(self) -> dict[str, Any] | None:
        """Load the job if needed and run it once on its worker; ``None`` when it cannot run."""
        loop = asyncio.get_running_loop()
        if self.climate is None:
            try:
                self.climate = await loop.run_in_executor(self._climate_pool, self._climate_factory)
                self.climate_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported and retried at the next run
                self.climate_error = f"{type(exc).__name__}: {exc}"[:500]
                self.metrics.climate_runs.labels(result="error").inc()
                log.error("climate shadow not loaded: %s", self.climate_error)
                await self._record_event("climate_shadow_error", {"error": self.climate_error})
                return None
        job = self.climate
        now = self.clock.now()
        inputs = self.climate_inputs(now)
        weather = self.config.archive.weather_forecast
        max_age = timedelta(minutes=self.config.climate_shadow.forecast_max_age_minutes)

        def work() -> dict[str, Any]:
            forecast = None
            if weather.enabled:  # issued at or before t0: nothing after the decision
                t0 = job.decision_time(now)
                forecast = latest_forecast(self.data_dir, weather.entity_id, t0, max_age)
            return job.run_once(now, replace(inputs, forecast=forecast))

        try:
            summary = await loop.run_in_executor(self._climate_pool, work)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the loop
            self.climate_error = f"{type(exc).__name__}: {exc}"[:500]
            self.metrics.climate_runs.labels(result="error").inc()
            log.exception("climate shadow run failed")
            return None
        self.climate_error = None
        log.info(
            "climate shadow run t0=%s status=%s regime=%s",
            summary.get("t0"),
            summary.get("status"),
            summary.get("regime", "-"),
        )
        return summary

    async def _climate_loop(self) -> None:
        s = self.config.climate_shadow
        offset = timedelta(minutes=s.offset_minutes)
        first = True
        while True:
            now = self.clock.now()
            catch_up = False
            # at start, catch up the current boundary once its offset has passed (the job skips a
            # boundary it already decided)
            if first and now >= floor_slot(now, 3600) + offset:
                due = now
                catch_up = True
            elif self.climate is not None:
                due = self.climate.next_run(now)
            else:
                due = next_run_time(now, timedelta(minutes=60), s.offset_minutes)
            first = False
            await self.clock.sleep(max(0.0, (due - now).total_seconds()))
            if catch_up:
                await self._wait_for_ha_sync()
            await self.run_climate_shadow()

    async def _wait_for_ha_sync(self) -> None:
        """Hold the startup catch-up run until HA has synced, so its regime is not ``unknown``.

        Does not wait without HA or once synced; on timeout the run goes ahead (and records
        ``unknown``, which is then true).
        """
        if self.ha is None or self.ha.stats.synced:
            return
        try:
            await asyncio.wait_for(self._ha_synced.wait(), self._climate_sync_timeout_s)
        except TimeoutError:
            log.warning(
                "climate shadow catch-up: Home Assistant not synced after %.0f s, running anyway",
                self._climate_sync_timeout_s,
            )

    def climate_mode(self) -> plan_export.ModeState:
        """The owner's mode: the HA select's state (read only, from the entity store), else the
        configured fallback.  Never ``live`` in effect (see :mod:`.plan_export`)."""
        s = self.config.climate_shadow
        entity = self.store.get(s.mode_entity) if s.mode_entity else None
        state = entity.state if entity is not None and entity.available else None
        return plan_export.resolve_mode(state, s.optimizer_mode)

    def _climate_plan_now(self) -> tuple[dict[str, Any] | None, plan_export.ModeState, datetime]:
        plan = self.climate.plan if self.climate is not None else None
        return plan, self.climate_mode(), self.clock.now()

    def climate_plan_response(self) -> tuple[int, dict[str, Any]]:
        """``GET /climate/plan``: ``(status code, body)``; counts what was served."""
        plan, mode, now = self._climate_plan_now()
        code, body = plan_export.plan_response(
            plan, enabled=self.config.climate_shadow.enabled, mode=mode, now=now
        )
        self.metrics.climate_plan_served.labels(
            endpoint="plan", result="ok" if code == 200 else "unavailable"
        ).inc()
        return code, body

    def climate_plan_summary(self) -> dict[str, Any]:
        """``GET /climate/plan/summary``: the flat dashboard document (always served)."""
        plan, mode, now = self._climate_plan_now()
        body = plan_export.summary_response(
            plan, enabled=self.config.climate_shadow.enabled, mode=mode, now=now
        )
        self.metrics.climate_plan_served.labels(
            endpoint="summary",
            result="ok" if body["state"] in plan_export.PLAN_STATES else "unavailable",
        ).inc()
        return body

    def _climate_plan_view(self, now: datetime) -> dict[str, Any] | None:
        """The export's state for ``/metrics`` and ``/status`` (no counters touched)."""
        s = self.config.climate_shadow
        if not s.enabled:
            return None
        plan = self.climate.plan if self.climate is not None else None
        mode = self.climate_mode()
        state = plan_export.plan_state(plan, enabled=True, mode=mode, now=now)
        summary = None
        if plan is not None and state in plan_export.PLAN_STATES:
            summary = plan_export.summarise(plan, now)
        return {
            "mode": mode,
            "state": state,
            "plan": plan,
            "summary": summary,
            "age_s": None
            if plan is None
            else max(0.0, (now - datetime.fromisoformat(plan["generated_at"])).total_seconds()),
        }

    def _climate_plan_status(self, now: datetime) -> dict[str, Any] | None:
        view = self._climate_plan_view(now)
        if view is None:
            return None
        plan, summary, mode = view["plan"], view["summary"], view["mode"]
        return {
            "advisory": True,
            "actuation": "none (the optimizer only publishes an advisory plan; ADR-0008 proposed)",
            "state": view["state"],
            **mode.fields(),
            "live_allowed": plan_export.LIVE_ALLOWED,
            "plan_id": plan["plan_id"] if plan else None,
            "status": plan["status"] if plan else None,
            "generated_at": plan["generated_at"] if plan else None,
            "valid_until": plan["valid_until"] if plan else None,
            "age_s": None if view["age_s"] is None else round(view["age_s"]),
            "heating_now": summary["heating_now"] if summary else None,
            "water_c_now": summary["water_c_now"] if summary else None,
            "ttl_minutes": self.config.climate_shadow.plan_ttl_minutes,
        }

    def _climate_view(self) -> dict[str, Any] | None:
        if not self.config.climate_shadow.enabled:
            return None
        view: dict[str, Any] = {"error": self.climate_error, "loaded": False}
        if self.climate is not None:
            view.update(self.climate.snapshot())
        return view

    def _request_replan(self, reason: str) -> None:
        if self._loop is None or (self._replan_task and not self._replan_task.done()):
            return
        self._replan_task = self._loop.create_task(self._debounced_replan(reason))

    async def _debounced_replan(self, reason: str) -> None:
        await self.clock.sleep(self.config.planning.replan_debounce_s)
        last = self._last_solve_started
        if last is not None:
            since = (self.clock.now() - last).total_seconds()
            if since < self.config.planning.replan_min_interval_s:
                return
        await self.request_solve(f"event:{reason}")

    # ------------------------------------------------------------------ inputs

    def _role_fresh(self, role: str, now: datetime) -> tuple[bool, float | None]:
        entity = self.config.entities.get(role)
        if entity is None:
            return False, None
        state = self.store.get(entity)
        if state is None:
            return False, None
        age = self.store.age_s(entity, now)
        value = normalise_value(role, state)
        limit = self.config.freshness_for(role)
        if limit is None:  # availability only (battery_soc): no age test
            return value is not None, age
        return value is not None and age is not None and age <= limit, age

    def _battery_responsive(self) -> bool:
        """Omnibattery's liveness sensor reads exactly ``None`` (or none is configured).

        Omnibattery keeps the last SOC when Modbus polling fails, so an available SOC alone
        does not prove the battery answers; the actuator guard applies the same test.
        """
        entity = self.config.actuation.entities.battery_liveness
        if entity is None:
            return True
        state = self.store.get(entity)
        # Raw state on purpose: the healthy text "None" is what EntityState.available rejects.
        return state is not None and state.state == LIVENESS_OK_STATE

    def _soc_now(self, now: datetime) -> tuple[float | None, datetime | None, str]:
        """``(soc_frac, observed_at, source)`` or a ``None`` SOC when unusable."""
        if self.config.planning.soc_source == "entity":
            entity = self.config.entities.get("battery_soc")
            fresh, _ = self._role_fresh("battery_soc", now)
            state = self.store.get(entity)
            if not fresh or state is None or not self._battery_responsive():
                return None, state.observed_at if state else None, "entity"
            value = normalise_value("battery_soc", state)
            if value is None or not 0.0 <= value <= 1.0:
                return None, state.observed_at, "entity"
            return value, state.observed_at, "entity"
        return self._projected_virtual_soc(now), None, "simulated"

    def _projected_virtual_soc(self, now: datetime) -> float | None:
        """Virtual SOC at the next boundary: the current slot's decision applied
        against the forecast load (closing the slot later uses the measured one)."""
        if self.virtual_soc is None or self.tariff is None:
            return self.virtual_soc
        current = floor_slot(now, self._interval)
        opened = self._slot_open.get(current)
        if opened is None or opened.intent is None:
            return self.virtual_soc
        load = None
        if self.history is not None:
            with contextlib.suppress(ForecastUnavailableError, ValueError):
                load = forecast_load(
                    self.history,
                    [current],
                    tz=self._tz,
                    window_days=self.config.planning.forecast_window_days,
                    interval_seconds=self._interval,
                    forecaster=self.config.planning.load_forecaster,
                ).load_kwh[0]
        try:
            spec, limit, _ = battery_spec_from_tariff(self.tariff, self.virtual_soc)
        except ValueError:
            return self.virtual_soc
        step = step_virtual_battery(
            spec,
            soc_start_frac=self.virtual_soc,
            mode=opened.intent.mode,
            target_power_w=opened.intent.target_power_w * opened.intent_share(self._interval),
            load_kwh=load,
            interval_seconds=self._interval,
            import_limit_kw=limit,
        )
        return step.soc_end_frac

    def _terminal_value(self) -> tuple[float | None, str]:
        configured = self.config.planning.terminal_value
        if configured != "trailing_mean":
            return float(configured), "config: explicit terminal_value"
        if self.history is not None and self.history.mean_price_eur_per_kwh is not None:
            return (
                self.history.mean_price_eur_per_kwh,
                f"default:trailing_mean: mean of {self.history.price_samples} PVPC samples over "
                f"the {self.history.window_days} local days before {self.history.cutoff_day}",
            )
        if self.price_curve is not None and self.price_curve.prices:
            values = list(self.price_curve.prices.values())
            return (
                sum(values) / len(values),
                "fallback: mean of the published curve (trailing history price unavailable)",
            )
        return None, "unavailable"

    def evaluate_inputs(self, now: datetime) -> list[tuple[str, str]]:
        """Every reason planning would be refused right now (empty == ready)."""
        reasons: list[tuple[str, str]] = []
        if self.config.home_assistant.url is None or self.config.home_assistant.token is None:
            reasons.append(("ha_unconfigured", "BATTERY_OPTIMIZER_HA_URL/TOKEN not set"))
        elif self.ha is None or not self.ha.stats.connected:
            reasons.append(("ha_disconnected", (self.ha.stats.last_error or "") if self.ha else ""))
        elif not self.ha.stats.synced:
            reasons.append(("ha_not_synced", "waiting for get_states"))
        fresh, age = self._role_fresh("grid_power", now)
        if not fresh:
            reasons.append(("grid_power_stale", f"age={age}"))
        if self.config.planning.soc_source == "entity":
            if not self.config.entities.get("battery_soc"):
                reasons.append(("soc_unconfigured", "entities.battery_soc is null"))
            elif self._soc_now(now)[0] is None:
                detail = "battery SOC missing, stale or out of range, or battery unresponsive"
                reasons.append(("soc_stale", detail))
        elif self.virtual_soc is None:
            reasons.append(("soc_unconfigured", "virtual battery not initialised"))
        if self.tariff is None:
            reasons.append(("tariff_invalid", self.tariff_error or "tariff config not loaded"))
        first = next_slot_boundary(now, self._interval)
        if self.price_curve is None:
            reasons.append(("prices_missing", self.price_error or "no PVPC state yet"))
        else:
            n = len(self.price_curve.contiguous_from(first))
            if n < self.config.planning.min_horizon_slots:
                reasons.append(
                    ("prices_insufficient", f"{n} published slot(s) from {first.isoformat()}")
                )
        today = local_date(now, self._tz)
        if self.history is None or self.history.cutoff_day != today:
            reasons.append(
                ("history_unavailable", self.history_error or "load history not fetched for today")
            )
        elif self.forecast_error:
            reasons.append(("forecast_unavailable", self.forecast_error))
        return reasons

    def readiness(self, now: datetime | None = None) -> Readiness:
        now = now or self.clock.now()
        reasons = self.evaluate_inputs(now)
        inputs_ready = not reasons
        plan = self.plans[-1] if self.plans else None
        max_age = self._interval + self.config.planning.solve_lead_s + 60.0
        if (
            plan is None
            or (now - plan.plan.generated_at).total_seconds() > max_age
            or plan.plan.is_expired(now)
        ):
            reasons.append(("no_recent_plan", "no unexpired plan generated recently"))
        readiness = Readiness(
            ready=not reasons,
            inputs_ready=inputs_ready,
            reasons=tuple(r for r, _ in reasons),
            details={r: d for r, d in reasons},
        )
        return readiness

    # ------------------------------------------------------------------ planning

    def build_snapshot(self, now: datetime, trigger: str) -> PlanningSnapshot:
        reasons = self.evaluate_inputs(now)
        blocking = [r for r in reasons if r[0] != "forecast_unavailable"]
        if blocking:
            raise NotReadyError(blocking[0][0], blocking[0][1])
        assert self.tariff is not None and self.price_curve is not None and self.history is not None
        first = next_slot_boundary(now, self._interval)
        current = floor_slot(now, self._interval)
        lead = timedelta(seconds=self.config.planning.solve_lead_s)
        if (
            self._awaiting_slot(now) is not None
            and now < first - lead
            and self.price_curve.price_at(current) is not None
        ):
            # Catch-up plan: the slot in progress has no intent, so plan from it for a late
            # adoption.  Its first slot is modelled whole; the intent is a power, held only for
            # the rest of the slot, so the battery moves less than planned, never more.
            first = current
        prices = self.price_curve.contiguous_from(first, self.config.planning.max_horizon_slots)
        step = timedelta(seconds=self._interval)
        starts = tuple(first + i * step for i in range(len(prices)))
        try:
            forecast = forecast_load(
                self.history,
                starts,
                tz=self._tz,
                window_days=self.config.planning.forecast_window_days,
                interval_seconds=self._interval,
                forecaster=self.config.planning.load_forecaster,
            )
        except (ForecastUnavailableError, ValueError) as exc:
            self.forecast_error = str(exc)
            raise NotReadyError("forecast_unavailable", str(exc)) from exc
        self.forecast_error = None
        soc_raw, soc_at, soc_source = self._soc_now(now)
        if soc_raw is None:
            raise NotReadyError("soc_stale", "SOC unavailable at snapshot time")
        commit_window = self._commit_window(starts)
        soc, clamped = clamp_soc(
            soc_raw,
            self.tariff,
            # A run pending inside the snapshot: a measured 98% is not planned as 95%.
            ceiling_frac=1.0 if commit_window is not None and commit_window[0] == 0 else None,
        )
        terminal, terminal_source = self._terminal_value()
        if terminal is None:
            raise NotReadyError("prices_missing", "no terminal value")
        try:
            # The spec's initial SOC stays inside the window; the snapshot carries a lifted SOC.
            window_max = float(self.tariff.battery.soc_window_max or 1.0)
            spec, import_limit, _ = battery_spec_from_tariff(self.tariff, min(soc, window_max))
        except ValueError as exc:
            raise NotReadyError("tariff_invalid", str(exc)) from exc
        self._plan_sequence += 1
        return PlanningSnapshot(
            decision_time=now,
            interval_seconds=self._interval,
            slot_starts=starts,
            prices=tuple(prices),
            load_kwh=forecast.load_kwh,
            soc_frac=soc,
            soc_measured_frac=soc_raw,
            soc_clamped=clamped,
            soc_source=soc_source,
            soc_observed_at=soc_at,
            spec=spec,
            import_limit_kw=import_limit,
            terminal_value_eur_per_kwh=terminal,
            terminal_value_source=terminal_source,
            throughput_cost_eur_per_kwh=self.config.planning.throughput_cost_eur_per_kwh,
            high_soc_threshold_frac=self.config.planning.high_soc_threshold_frac,
            high_soc_cost_eur_per_kwh_h=self.config.planning.high_soc_cost_eur_per_kwh_h,
            charge_soft_cap_kw=self.config.planning.charge_soft_cap_kw,
            charge_soft_cap_eur_per_kwh=self.config.planning.charge_soft_cap_eur_per_kwh,
            discharge_margin_kw=(
                self.config.actuation.discharge_margin_w / 1000.0
                if self.config.planning.discharge_margin_bound
                else 0.0
            ),
            solver_time_limit_s=self.config.planning.solver_time_limit_s,
            plan_sequence=self._plan_sequence,
            expires_at=first + self.config.planning.plan_ttl_slots * step,
            trigger=trigger,
            code_version=self.code_version,
            config_hash=self.config.config_hash(),
            forecast_meta=forecast.meta,
            price_meta=self.price_curve.summary(),
            full_charge=None
            if commit_window is None
            else self._full_charge_spec(spec.soc_max_frac, commit_window, "commit"),
        )

    def _worker_finished(self, generation: int) -> None:
        # Runs on the loop thread once the planner thread has really ended.
        self._worker_busy = False

    def _log_skip(self, trigger: str, reason: str, detail: str) -> None:
        """INFO the first skip of a run of identical reasons, DEBUG the repeats."""
        level = logging.DEBUG if reason == self._logged_skip else logging.INFO
        self._logged_skip = reason
        log.log(
            level, "solve skipped trigger=%s reason=%s detail=%s", trigger, reason, detail or "-"
        )

    async def request_solve(self, trigger: str) -> str:
        """Run one solve if the worker is free and the inputs are ready.

        Returns the outcome: ``ok``, ``skipped:<reason>``, ``timeout``, ``error``
        or ``discarded:<why>``.
        """
        now = self.clock.now()
        if self._worker_busy:
            self.metrics.skipped.labels(reason="busy").inc()
            self._log_skip(trigger, "busy", "")
            await self._record_event("solve_skipped", {"trigger": trigger, "reason": "busy"})
            return "skipped:busy"
        try:
            snapshot = self.build_snapshot(now, trigger)
        except NotReadyError as exc:
            self.metrics.skipped.labels(reason=exc.reason).inc()
            self._log_skip(trigger, exc.reason, exc.detail)
            self.last_solve = {
                "at": now.isoformat(),
                "trigger": trigger,
                "outcome": f"skipped:{exc.reason}",
                "detail": exc.detail,
            }
            await self._record_event(
                "solve_skipped", {"trigger": trigger, "reason": exc.reason, "detail": exc.detail}
            )
            return f"skipped:{exc.reason}"

        loop = asyncio.get_running_loop()
        self._worker_busy = True
        self._last_solve_started = now
        generation = self._generation
        future = self._executor.submit(self._planner, snapshot)

        def _done(_f: Any, gen: int = generation) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._worker_finished, gen)

        future.add_done_callback(_done)
        started = time.perf_counter()
        timeout = self.config.planning.solve_timeout_s
        try:
            result: PlanResult = await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=timeout
            )
        except TimeoutError:
            self.metrics.solves.labels(result="timeout").inc()
            log.warning("solve timed out trigger=%s timeout_s=%s", trigger, timeout)
            self.last_solve = {
                "at": now.isoformat(),
                "trigger": trigger,
                "outcome": "timeout",
                "timeout_s": timeout,
            }
            await self._record_event("solve_timeout", {"trigger": trigger, "timeout_s": timeout})
            return "timeout"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - solver/contract errors are reported, not fatal
            self.metrics.solves.labels(result="error").inc()
            detail = f"{type(exc).__name__}: {exc}"[:500]
            log.error("solve failed trigger=%s error=%s", trigger, detail)
            self.last_solve = {
                "at": now.isoformat(),
                "trigger": trigger,
                "outcome": "error",
                "error": detail,
            }
            await self._record_event("solve_error", {"trigger": trigger, "error": detail})
            return "error"
        elapsed = time.perf_counter() - started
        completed = self.clock.now()
        if generation != self._generation:
            self.metrics.solves.labels(result="discarded").inc()
            return "discarded:stopped"
        deadline = snapshot.first_slot
        if deadline <= snapshot.decision_time:
            # A catch-up plan starts in the slot it was made in: obsolete when that slot ends.
            deadline += timedelta(seconds=self._interval)
        if deadline <= completed:
            # The first slot has begun: the plan is obsolete.  A slot that opened with an
            # intent keeps it; a partly elapsed slot is only ever filled by a catch-up plan.
            self.metrics.solves.labels(result="discarded").inc()
            log.warning(
                "plan discarded plan_id=%s trigger=%s reason=obsolete solve_s=%.2f",
                result.plan_id,
                trigger,
                elapsed,
            )
            self.last_solve = {
                "at": now.isoformat(),
                "trigger": trigger,
                "outcome": "discarded:obsolete",
            }
            await self._record_event(
                "solve_discarded",
                {"trigger": trigger, "reason": "obsolete", "plan_id": result.plan_id},
            )
            return "discarded:obsolete"
        self.metrics.solves.labels(result="ok").inc()
        self.metrics.solve_duration.observe(elapsed)
        self.plans.append(result)
        self.day_ahead.observe(slot_plans_from_result(result), completed)
        self._wake_actuator()
        self._logged_skip = None
        _log_plan(result, trigger, elapsed)
        thermal_block = self._predict_thermal(result, completed)
        self.adopt_late(completed)
        self.last_solve = {
            "at": now.isoformat(),
            "trigger": trigger,
            "outcome": "ok",
            "plan_id": result.plan_id,
            "duration_s": round(elapsed, 4),
            "solver_status": result.solver.get("status"),
        }
        record = result.to_record()
        if thermal_block is not None:
            record["thermal"] = thermal_block  # additive: absent unless planning.thermal is on
        await self._write(self.sink.write_plan, record)
        await self._after_solve_full_charge(snapshot, result)
        return "ok"

    # ------------------------------------------------------------------ decisions

    def decision_for_slot(self, slot_start: datetime) -> tuple[ShadowIntent | None, str]:
        """The intent valid at *slot_start* from the newest plan made before it."""
        candidates = [p for p in self.plans if p.plan.generated_at <= slot_start]
        if not candidates:
            return None, "no_plan"
        for plan in sorted(candidates, key=lambda p: p.plan.plan_sequence, reverse=True):
            if plan.plan.is_expired(slot_start):
                continue
            intent = plan.intent_for_slot(slot_start)
            if intent is not None:
                return intent, "intent"
        return None, "plan_expired"

    def current_intent(self, now: datetime | None = None) -> ShadowIntent | None:
        now = now or self.clock.now()
        opened = self._slot_open.get(floor_slot(now, self._interval))
        if opened is None or opened.intent is None or not opened.intent.is_current(now):
            return None
        return opened.intent

    async def open_slot(self, slot_start: datetime) -> _SlotOpen:
        now = self.clock.now()
        reasons = self.evaluate_inputs(now)
        intent, decision = self.decision_for_slot(slot_start)
        if reasons:
            intent, decision = None, "not_ready"
        opened = _SlotOpen(
            slot_start=slot_start,
            intent=intent,
            decision=decision,
            inputs_ready=not reasons,
            reasons=tuple(r for r, _ in reasons),
            opened_at=now,
            decision_at_open=decision,
            day_ahead=self.day_ahead.get(slot_start),
            final=self._final_slot_plan(intent),
        )
        self._slot_open[slot_start] = opened
        self._opened_slot = slot_start
        log.info(
            "slot opened start=%s decision=%s %s not_ready=%s",
            slot_start.isoformat(),
            decision,
            _intent_text(intent),
            ",".join(opened.reasons) or "-",
        )
        self._wake_actuator()
        for old in [s for s in self._slot_open if s < slot_start - timedelta(hours=2)]:
            del self._slot_open[old]
        self.day_ahead.prune(now)
        return opened

    def _final_slot_plan(self, intent: ShadowIntent | None) -> SlotPlan | None:
        """What the plan behind *intent* said about the intent's slot (load forecast included)."""
        if intent is None:
            return None
        for result in reversed(self.plans):
            if result.plan_id == intent.plan_id:
                for slot_plan in slot_plans_from_result(result):
                    if slot_plan.slot_start == intent.valid_from:
                        return slot_plan
        # The plan left the in-memory window: keep the intent's own numbers, no load forecast.
        return SlotPlan(
            plan_id=intent.plan_id,
            plan_sequence=intent.plan_sequence,
            generated_at=intent.generated_at,
            slot_start=intent.valid_from,
            interval_seconds=self._interval,
            mode=intent.mode,
            signed_power_w=intent.signed_power_w,
            soc_target_frac=intent.soc_target_frac,
            load_kwh=None,
            expected_buy_price_eur_per_kwh=intent.expected_buy_price_eur_per_kwh,
        )

    def _awaiting_slot(self, now: datetime) -> _SlotOpen | None:
        """The slot in progress when it opened without an intent and has not adopted one."""
        opened = self._slot_open.get(floor_slot(now, self._interval))
        return opened if opened is not None and opened.intent is None else None

    def adopt_late(self, now: datetime | None = None) -> bool:
        """Give the slot in progress an intent if it opened without one and can have one now.

        The inputs must be ready now, and the intent comes from the newest unexpired plan of
        this process that covers the slot, made before or after the slot opened (a catch-up
        plan).  The slot is frozen from then on: a slot that opened with an intent, or already
        adopted one, is never re-decided.  The actuator's guard still applies in full.
        """
        now = now or self.clock.now()
        opened = self._awaiting_slot(now)
        if opened is None or self.evaluate_inputs(now):
            return False
        intent = None
        for plan in sorted(self.plans, key=lambda p: p.plan.plan_sequence, reverse=True):
            if plan.plan.is_expired(now):
                continue
            candidate = plan.intent_for_slot(opened.slot_start)
            if candidate is not None and candidate.is_current(now):
                intent = candidate
                break
        if intent is None:
            return False
        opened.intent, opened.decision, opened.adopted_at = intent, "late_intent", now
        opened.final = self._final_slot_plan(intent)
        log.info(
            "slot adopted start=%s %s after_s=%.0f decision_at_open=%s",
            opened.slot_start.isoformat(),
            _intent_text(intent),
            (now - opened.slot_start).total_seconds(),
            opened.decision_at_open,
        )
        self._wake_actuator()
        return True

    async def close_slot(self, slot_start: datetime) -> dict[str, Any]:
        slot_end = slot_start + timedelta(seconds=self._interval)
        measured = self.observer.measure(slot_start, slot_end)
        self.observer.prune(self.clock.now())
        opened = self._slot_open.get(slot_start)
        price = self.price_curve.price_at(slot_start) if self.price_curve else None
        if price is None:
            price = self._recorded_price(slot_start)
        record = self._slot_record(slot_start, slot_end, measured, opened, price)
        if self.thermal is not None:
            scored = self._score_thermal(slot_start, opened)
            if scored is not None:
                record["thermal"] = scored
        self._closed_slot = slot_start
        log.info(
            "slot closed start=%s decision=%s grid_kwh=%s battery_charge_kwh=%s "
            "battery_discharge_kwh=%s soc_start=%s soc_end=%s",
            slot_start.isoformat(),
            record["decision"],
            _fmt(measured.grid_kwh, 3),
            _fmt(measured.battery_charge_kwh, 3),
            _fmt(measured.battery_discharge_kwh, 3),
            _fmt(measured.soc_start_frac, 3),
            _fmt(measured.soc_end_frac, 3),
        )
        await self._write(self.sink.append_slot, slot_start, record)
        return record

    def _recorded_price(self, slot_start: datetime) -> float | None:
        for plan in reversed(self.plans):
            try:
                index = plan.snapshot.slot_starts.index(slot_start)
            except ValueError:
                continue
            return plan.snapshot.prices[index]
        return None

    def _slot_record(
        self,
        slot_start: datetime,
        slot_end: datetime,
        measured: SlotMeasurements,
        opened: _SlotOpen | None,
        price: float | None,
    ) -> dict[str, Any]:
        intent = opened.intent if opened else None
        decision = opened.decision if opened else "not_observed"
        battery_mapped = self.config.entities.has_battery
        load = measured.gross_load_kwh(battery_mapped)
        self.metrics.shadow_slots.labels(decision=decision).inc()

        mode = intent.mode if intent else None
        # A late intent was in force for part of the slot only: its energy is pro-rated.
        share = opened.intent_share(self._interval) if opened else 1.0
        target = intent.target_power_w * share if intent else 0.0
        planned_c, planned_d = intent_energy_kwh(mode, target, self._interval)
        self.metrics.shadow_planned_energy.labels(direction="charge").inc(planned_c)
        self.metrics.shadow_planned_energy.labels(direction="discharge").inc(planned_d)
        if measured.battery_charge_kwh is not None:
            self.metrics.shadow_actual_energy.labels(direction="charge").inc(
                measured.battery_charge_kwh
            )
        if measured.battery_discharge_kwh is not None:
            self.metrics.shadow_actual_energy.labels(direction="discharge").inc(
                measured.battery_discharge_kwh
            )

        day_ahead = opened.day_ahead if opened else None
        final = opened.final if opened else None
        revision = self._score_plans(day_ahead, final, load)

        # SOC-limit check of the request against the *measured* SOC (entity mode).
        violation: bool | None = None
        step: SlotStep | None = None
        base = self._base_spec()
        if base is not None and self.tariff is not None:
            spec, limit = base
            if intent is not None and measured.soc_start_frac is not None:
                violation = soc_violation(spec, measured.soc_start_frac, planned_c, planned_d)
                if violation:
                    self.metrics.shadow_soc_violations.inc()
            if self.virtual_soc is None and measured.soc_start_frac is not None:
                self.virtual_soc = clamp_soc(measured.soc_start_frac, self.tariff)[0]
                self.virtual_soc_origin = f"measured SOC at {slot_start.isoformat()}"
            if self.virtual_soc is not None:
                step = step_virtual_battery(
                    spec,
                    soc_start_frac=self.virtual_soc,
                    mode=mode,
                    target_power_w=target,
                    load_kwh=load,
                    interval_seconds=self._interval,
                    import_limit_kw=limit,
                )
                self.virtual_soc = step.soc_end_frac
                self.metrics.shadow_clipped.inc(
                    step.clipped_charge_kwh + step.clipped_discharge_kwh
                )
                self._spawn(self._save_virtual_battery(slot_end))

        native_cost = cf_cost = None
        if (
            price is not None
            and measured.grid_kwh is not None
            and step is not None
            and step.grid_kwh is not None
        ):
            native_cost = price * measured.grid_kwh
            cf_cost = price * step.grid_kwh
            # Gauges, not counters: PVPC can be negative, so these sums can fall.
            self.metrics.shadow_native_cost.inc(native_cost)
            self.metrics.shadow_cf_cost.inc(cf_cost)
            self.metrics.shadow_comparable.inc()

        return {
            "kind": "shadow_slot",
            "shadow": True,
            "slot_start": slot_start.isoformat(),
            "slot_end": slot_end.isoformat(),
            "observed_from": max(slot_start, self._started_at).isoformat(),
            "local_day": local_date(slot_start, self._tz).isoformat(),
            "interval_seconds": self._interval,
            "soc_source": self.config.planning.soc_source,
            "battery_mapped": battery_mapped,
            "price_eur_per_kwh": price,
            "decision": decision,
            "decision_at_open": opened.decision_at_open if opened else None,
            "adopted_at": opened.adopted_at.isoformat() if opened and opened.adopted_at else None,
            "adopted_after_s": (opened.adopted_at - slot_start).total_seconds()
            if opened and opened.adopted_at
            else None,
            "inputs_ready_at_open": opened.inputs_ready if opened else None,
            "not_ready_reasons": list(opened.reasons) if opened else [],
            "intent": intent.to_dict() if intent else None,
            "day_ahead": day_ahead.to_dict() if day_ahead else None,
            "final": final.to_dict() if final else None,
            "plan_revision_kwh": revision,
            "planned_charge_kwh": planned_c,
            "planned_discharge_kwh": planned_d,
            "measured": measured.to_dict(),
            "gross_load_kwh": load,
            "soc_violation": violation,
            "virtual": None
            if step is None
            else {
                "soc_start_frac": step.soc_start_frac,
                "soc_end_frac": step.soc_end_frac,
                "charge_kwh": step.charge_kwh,
                "discharge_kwh": step.discharge_kwh,
                "clipped_charge_kwh": step.clipped_charge_kwh,
                "clipped_discharge_kwh": step.clipped_discharge_kwh,
                "grid_kwh": step.grid_kwh,
                "applied": step.applied,
            },
            "native_cost_eur": native_cost,
            "counterfactual_cost_eur": cf_cost,
            "code_version": self.code_version,
        }

    def _score_plans(
        self, day_ahead: SlotPlan | None, final: SlotPlan | None, load_kwh: float | None
    ) -> float | None:
        """Update the day-ahead / final KPI counters of a closing slot; returns the revision.

        The load forecast is scored only when the slot's gross load was measured.  The final
        plan's battery energy is the ``shadow_planned_energy`` counter already, so only the
        day-ahead energy and the revision between the two are new.
        """
        m = self.metrics
        if day_ahead is not None:
            charge, discharge = day_ahead.energy_kwh()
            m.shadow_day_ahead_energy.labels(direction="charge").inc(charge)
            m.shadow_day_ahead_energy.labels(direction="discharge").inc(discharge)
        for label, slot_plan in (("day_ahead", day_ahead), ("final", final)):
            if slot_plan is None or slot_plan.load_kwh is None or load_kwh is None:
                continue
            m.load_forecast_scored_slots.labels(plan=label).inc()
            m.load_forecast_kwh.labels(plan=label).inc(slot_plan.load_kwh)
            m.load_actual_kwh.labels(plan=label).inc(load_kwh)
            m.load_forecast_abs_error_kwh.labels(plan=label).inc(abs(load_kwh - slot_plan.load_kwh))
        revision = revision_kwh(day_ahead, final)
        if revision is not None:
            m.plan_revision_energy.inc(revision)
        return revision

    # ------------------------------------------------------------------ virtual battery

    def _base_spec(self) -> tuple[Any, float | None] | None:
        """Battery spec from the tariff (initial SOC irrelevant: callers re-anchor)."""
        if self.tariff is None:
            return None
        floor = self.tariff.battery.soc_window_min or 0.0
        try:
            spec, limit, _ = battery_spec_from_tariff(self.tariff, float(floor))
        except ValueError:
            return None
        return spec, limit

    def _init_virtual_battery(self) -> None:
        if self.tariff is None:
            return
        saved = self.sink.load_virtual_battery()
        now = self.clock.now()
        if saved and saved.get("soc_frac") is not None and saved.get("at"):
            with contextlib.suppress(ValueError, TypeError):
                at = datetime.fromisoformat(saved["at"])
                if (now - at).total_seconds() <= 6 * 3600:
                    self.virtual_soc = clamp_soc(float(saved["soc_frac"]), self.tariff)[0]
                    self.virtual_soc_origin = f"restored from store ({saved['at']})"
                    return
        if self.config.planning.soc_source == "simulated":
            initial = self.tariff.battery.initial_soc_frac
            if initial is None:
                initial = self.tariff.battery.soc_window_min
            if initial is not None:
                self.virtual_soc = clamp_soc(float(initial), self.tariff)[0]
                self.virtual_soc_origin = "tariff.yaml battery.initial_soc_frac"

    async def _save_virtual_battery(self, at: datetime) -> None:
        if self.virtual_soc is None:
            return
        await self._write(
            self.sink.save_virtual_battery,
            {"soc_frac": self.virtual_soc, "at": at.isoformat(), "origin": self.virtual_soc_origin},
        )

    # ------------------------------------------------------------------ loops

    async def _write(self, fn: Callable[..., Any], *args: Any) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._io, fn, *args)
        except RuntimeError as exc:  # executor shut down
            log.warning("store write skipped: %s", exc)
        except OSError as exc:
            self.sink.write_errors += 1
            self.metrics.sink_errors.inc()
            log.error("store write failed: %s", exc)

    async def _record_event(self, kind: str, details: dict[str, Any]) -> None:
        now = self.clock.now()
        await self._write(
            self.sink.append_event,
            now,
            {"kind": kind, "at": now.isoformat(), "code_version": self.code_version, **details},
        )

    async def refresh_history(self) -> bool:
        if self._history_fetcher is None:
            self.history_error = "no Prometheus URL configured"
            return False
        now = self.clock.now()
        today = local_date(now, self._tz)
        self._history_last_attempt = time.monotonic()
        loop = asyncio.get_running_loop()
        try:
            history = await loop.run_in_executor(
                self._history_pool, self._history_fetcher, now, today
            )
        except Exception as exc:  # noqa: BLE001 - reported as not-ready, retried later
            self.history_error = f"{type(exc).__name__}: {exc}"[:500]
            self.metrics.history_refresh.labels(result="error").inc()
            log.warning(
                "history refresh failed day=%s error=%s (retry in %.0f s)",
                today.isoformat(),
                self.history_error,
                _HISTORY_RETRY_S,
            )
            await self._record_event("history_error", {"error": self.history_error})
            return False
        self.history = history
        self.history_error = None
        self.forecast_error = None
        self.metrics.history_refresh.labels(result="ok").inc()
        log.info(
            "history refreshed cutoff_day=%s window_days=%d slots=%d usable_slots=%d "
            "price_samples=%d",
            history.cutoff_day.isoformat(),
            history.window_days,
            len(history.starts),
            history.usable_slots,
            history.price_samples,
        )
        await self._record_event("history_refreshed", history.summary())
        return True

    async def _history_loop(self) -> None:
        """Fetch history once per local day (the forecast cutoff moves at midnight),
        retrying a failed fetch every ``_HISTORY_RETRY_S``."""
        while True:
            today = local_date(self.clock.now(), self._tz)
            due = self.history is None or self.history.cutoff_day != today
            retry_ok = (
                self._history_last_attempt is None
                or time.monotonic() - self._history_last_attempt >= _HISTORY_RETRY_S
            )
            if due and retry_ok and await self.refresh_history():
                self._request_replan("history_refreshed")
            await self.clock.sleep(60.0)

    async def _poll_loop(self) -> None:
        assert self.rest is not None
        roles = [r for r in self.config.entities.configured() if r != "grid_energy"]
        while True:
            await self.clock.sleep(self.config.home_assistant.poll_interval_s)
            for role in roles:
                entity = self.config.entities.get(role)
                if entity is None:
                    continue
                try:
                    payload = await self.rest.get_state(entity)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the WebSocket stays authoritative
                    self.metrics.rest_polls.labels(result="error").inc()
                    continue
                if payload is None:
                    self.metrics.rest_polls.labels(result="missing").inc()
                    continue
                from .entity_store import parse_ha_state

                try:
                    self.store.apply(parse_ha_state(payload, received_at=self.clock.now()))
                    self.metrics.rest_polls.labels(result="ok").inc()
                except ValueError:
                    self.metrics.rest_polls.labels(result="error").inc()

    async def _health_loop(self) -> None:
        """Advance the battery-health clock: a steady temperature reports nothing for hours."""
        assert self.health is not None
        while True:
            await self.clock.sleep(_HEALTH_TICK_S)
            self.health.advance(self.clock.now())

    async def scheduler_tick(self) -> float:
        """Run whatever is due now; return seconds until the next due action."""
        now = self.clock.now()
        interval = timedelta(seconds=self._interval)
        current = floor_slot(now, self._interval)
        boundary = current + interval
        lead = timedelta(seconds=self.config.planning.solve_lead_s)
        grace = timedelta(seconds=self.config.observation.slot_close_grace_s)

        if self._opened_slot != current:
            await self.open_slot(current)
        previous = current - interval
        close_due = now >= current + grace
        if close_due and (self._closed_slot is None or self._closed_slot < previous):
            if previous >= floor_slot(self._started_at, self._interval):
                await self.close_slot(previous)
            else:
                self._closed_slot = previous
        if (
            self._awaiting_slot(now) is not None
            and not self.adopt_late(now)
            and now < boundary - lead
            and self._caught_up_for != current
            and not self.evaluate_inputs(now)
        ):
            # Inputs are ready but no plan covers the slot (e.g. right after a start): one
            # catch-up solve per slot, whose intent is adopted when it lands.
            self._caught_up_for = current
            await self.request_solve("catch_up")
        retry_soon = False
        if now >= boundary - lead and self._solved_for != boundary:
            self._solved_for = boundary
            outcome = await self.request_solve("scheduled")
            if outcome == "skipped:busy":
                # An event-triggered solve holds the worker; try again shortly.
                self._solved_for = None
                retry_soon = True

        now = self.clock.now()
        candidates = [boundary]
        if self._solved_for != boundary:
            candidates.append(boundary - lead)
        if self._closed_slot is None or self._closed_slot < previous:
            candidates.append(current + grace)
        if self._awaiting_slot(now) is not None:
            candidates.append(now + timedelta(seconds=_ADOPT_RETRY_S))
        upcoming = [c for c in candidates if c > now]
        wake = min(upcoming) if upcoming else boundary
        delay = (wake - now).total_seconds()
        if retry_soon:
            delay = min(delay, 2.0)
        return max(0.05, delay)

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                delay = await self.scheduler_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive one bad tick
                log.exception("scheduler tick failed")
                delay = 5.0
            await self.clock.sleep(delay)

    # ------------------------------------------------------------------ thermal shadow

    def _thermal_observing(self) -> bool:
        """The buffer records real minutes only while the Home Assistant session is synced."""
        return self.ha is None or self.ha.stats.synced

    def _init_thermal(self) -> None:
        """Load the saved model and buffer (anything unusable is ignored, the prior stays)."""
        tracker = self.thermal
        if tracker is None:
            return
        now = self.clock.now()
        for name, load in (
            ("thermal", tracker.restore_state),
            ("thermal_samples", lambda state: tracker.restore_samples(state, now)),
        ):
            state, problem = self.sink.load_state(name)
            if problem is not None:
                tracker.restore_problem = problem
                log.warning("thermal state not usable problem=%s", problem)
            load(state)
        tracker.advance(now, observing=False)

    def _predict_thermal(self, result: PlanResult, now: datetime) -> dict[str, Any] | None:
        """The shadow prediction of a published plan, or ``None`` (off, or it failed: never
        blocks planning)."""
        tracker = self.thermal
        if tracker is None:
            return None
        try:
            snap = result.snapshot
            hours = snap.interval_seconds / 3600.0
            by_slot = {p.slot_start: p for p in slot_plans_from_result(result)}
            charge, discharge = [], []
            for start in snap.slot_starts:
                plan = by_slot.get(start)
                c, d = plan.energy_kwh() if plan is not None else (0.0, 0.0)
                charge.append(c / hours)
                discharge.append(d / hours)
            tracker.advance(now, observing=self._thermal_observing())
            block = tracker.predict(
                plan_id=result.plan_id,
                generated_at=result.plan.generated_at,
                slot_starts=snap.slot_starts,
                interval_s=snap.interval_seconds,
                charge_kw=charge,
                discharge_kw=discharge,
                now=now,
            )
        except Exception:  # noqa: BLE001 - observability must never reach planning
            self.metrics.thermal_predictions.labels(result="error").inc()
            log.exception("thermal prediction failed plan_id=%s", result.plan_id)
            return None
        self.metrics.thermal_predictions.labels(result="ok").inc()
        self._thermal_blocks.append((result.plan_id, block))
        return block

    def _score_thermal(
        self, slot_start: datetime, opened: _SlotOpen | None
    ) -> dict[str, Any] | None:
        """Score the closed slot's predictions (counters and the slot record's block)."""
        tracker = self.thermal
        assert tracker is not None
        try:
            tracker.advance(self.clock.now(), observing=self._thermal_observing())
            final = opened.final if opened else None
            scored = tracker.score_slot(
                slot_start, self._interval, final.plan_id if final is not None else None
            )
        except Exception:  # noqa: BLE001 - scoring is observability only
            log.exception("thermal scoring failed slot=%s", slot_start.isoformat())
            return None
        m = self.metrics
        for lead, entry in scored["leads"].items():
            error = entry["error_c"]
            if error is None:
                continue
            m.thermal_scored_slots.labels(lead=lead).inc()
            m.thermal_abs_error.labels(lead=lead).inc(abs(error))
            m.thermal_squared_error.labels(lead=lead).inc(error * error)
            m.thermal_error_sum.labels(lead=lead).inc(error)
        charge = scored["charge"]
        if charge is not None and "error_kw" in charge:
            m.thermal_power_scored_slots.inc()
            m.thermal_power_abs_error.inc(abs(charge["error_kw"]))
            m.thermal_power_error_sum.inc(charge["error_kw"])
        return scored

    async def _save_thermal(self, now: datetime, *, samples: bool) -> None:
        tracker = self.thermal
        assert tracker is not None
        await self._write(self.sink.save_thermal, tracker.to_state())
        if samples:
            state = tracker.samples_state()
            if state is not None:
                await self._write(self.sink.save_thermal_samples, state)
            self._thermal_saved_at = now

    async def thermal_refit(self) -> str | None:
        """Run the guarded refit on a copy of the buffer (own thread) and apply the verdict.

        Returns the outcome (``accepted``, ``rejected``, ``insufficient_data``, ``error``), or
        ``None`` when the thermal shadow is off or a refit is already running.  Only an accepted
        refit changes the parameters; the result is persisted either way.
        """
        tracker = self.thermal
        if tracker is None or self._thermal_refit_running:
            return None
        self._thermal_refit_running = True
        try:
            now = self.clock.now()
            tracker.advance(now, observing=self._thermal_observing())
            temp, power = tracker.arrays()
            current, prior = tracker.params, tracker.prior
            result, error = None, None
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    self._thermal_pool,
                    lambda: refit_thermal_model(temp, power, current=current, prior=prior),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed refit keeps the parameters
                error = f"{type(exc).__name__}: {exc}"[:300]
                log.exception("thermal refit failed")
            outcome = tracker.apply_refit(result, error, now)
        finally:
            self._thermal_refit_running = False
        self.metrics.thermal_refits.labels(result=outcome).inc()
        log.info(
            "thermal refit outcome=%s status=%s tau_min=%.1f t_amb_c=%.2f k_c=%.2f k_d=%.2f",
            outcome,
            tracker.status,
            tracker.params.tau_min,
            tracker.params.t_amb_c,
            tracker.params.k_c,
            tracker.params.k_d,
        )
        await self._save_thermal(now, samples=True)
        return outcome

    async def thermal_tick(self) -> None:
        """One pass of the thermal loop: record the minute, save the buffer, refit when due."""
        tracker = self.thermal
        assert tracker is not None
        now = self.clock.now()
        tracker.advance(now, observing=self._thermal_observing())
        saved = self._thermal_saved_at
        if saved is None or (now - saved).total_seconds() >= _THERMAL_SAVE_S:
            await self._save_thermal(now, samples=True)
        if tracker.refit_due(now, self._tz):
            await self.thermal_refit()

    async def _thermal_loop(self) -> None:
        while True:
            try:
                await self.thermal_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive one bad tick
                log.exception("thermal tick failed")
            await self.clock.sleep(_THERMAL_TICK_S)

    def _thermal_view(self, now: datetime) -> dict[str, Any]:
        assert self.thermal is not None
        return {
            **self.thermal.snapshot(now),
            "current_slot": self.thermal.current_slot(floor_slot(now, self._interval)),
        }

    def _thermal_block_for(self, plan_id: str) -> dict[str, Any] | None:
        for known, block in reversed(self._thermal_blocks):
            if known == plan_id:
                return block
        return None

    # ------------------------------------------------------------------ periodic full charge

    def _init_full_charge(self) -> None:
        """Load the scheduler's state (bootstrap when missing or corrupt); off unless enabled."""
        cfg = self.config.full_charge
        if not cfg.enabled:
            return
        now = self.clock.now()
        raw, problem = self.sink.load_full_charge(now)
        state: FullChargeState | None = None
        if raw is not None:
            try:
                state = FullChargeState.from_json(raw)
            except (ValueError, KeyError, TypeError, AssertionError) as exc:
                problem = f"invalid: {exc}"
                self.sink.quarantine_full_charge(now)
        if problem is not None:
            self._fc_state_problem = problem
            log.warning("full-charge state not usable, bootstrapping problem=%s", problem)
        earliest = None
        if self.config.source_path:
            from battery_optimizer.battery_telemetry import load_battery_telemetry

            with contextlib.suppress(ValueError, OSError):
                earliest = load_battery_telemetry(self.config.source_path).commissioned_at
        self.full_charge = FullChargeScheduler(
            cfg,
            self._tz,
            writer_mode=self.config.full_charge_writer_mode,
            now=now,
            state=state,
            earliest_reading=earliest,
        )
        # No decision, evaluation or commit plan until Home Assistant's time zone is verified.
        self.full_charge.suspend("timezone_unverified")
        if self.full_charge.reset_reason:
            log.info("full-charge state discarded reason=%s", self.full_charge.reset_reason)

    def _commit_window(self, starts: tuple[datetime, ...]) -> tuple[int, int] | None:
        """The run's day in a snapshot's slots while its target plan is the plan of record."""
        fc = self.full_charge
        if fc is None or fc.suspended is not None or fc.state.state not in COMMIT_STATES:
            return None
        if self.config.full_charge_isolated:
            return None  # feature shadow beside live control: the regular plan stays the plan
        run = fc.state.run
        assert run is not None
        margin = self.config.full_charge.completion_margin_slots
        return day_window(starts, run.day, self._tz, margin)

    def _full_charge_spec(
        self, ceiling_before_frac: float, window: tuple[int, int], mode: str
    ) -> FullChargeSpec:
        cfg = self.config.full_charge
        return FullChargeSpec(
            mode=mode,  # type: ignore[arg-type]
            window_start=window[0],
            checkpoint_end=window[1],
            ceiling_before_frac=ceiling_before_frac,
            target_soc_frac=cfg.target_soc_frac,
            shortfall_penalty_eur_per_kwh=cfg.shortfall_penalty_eur_per_kwh,
            taper_start_soc_frac=cfg.taper.start_soc_frac,
            taper_power_kw=cfg.taper.power_kw,
        )

    async def _after_solve_full_charge(
        self, snapshot: PlanningSnapshot, result: PlanResult
    ) -> None:
        """After the regular plan is published: note a committed plan, or price tomorrow once.

        The evaluation is a separate worker job so the plan of record is never delayed by it; any
        failure is recorded and retried at the next solve.
        """
        fc = self.full_charge
        if fc is None:
            return
        if snapshot.full_charge is not None:
            fc.note_plan(result.plan_id)
            if "full_charge_error" in result.solver:
                log.warning(
                    "full-charge commit solve failed: %s", result.solver["full_charge_error"]
                )
            return
        now = self.clock.now()
        tomorrow = bool(self.price_curve and self.price_curve.tomorrow_published)
        day = fc.evaluation_wanted(now, tomorrow)
        if day is None:
            return
        margin = self.config.full_charge.completion_margin_slots
        window = day_window(snapshot.slot_starts, day, self._tz, margin)
        if window is None or self._worker_busy:
            return
        spec = self._full_charge_spec(snapshot.spec.soc_max_frac, window, "evaluate")
        loop = asyncio.get_running_loop()
        generation = self._generation
        self._worker_busy = True
        future = self._executor.submit(
            evaluate_full_charge, replace(snapshot, full_charge=spec), result
        )

        def _done(_f: Any, gen: int = generation) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._worker_finished, gen)

        future.add_done_callback(_done)
        try:
            evaluation = await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=self.config.planning.solve_timeout_s
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an evaluation never costs the plan; retried
            detail = f"{type(exc).__name__}: {exc}"[:500]
            log.warning("full-charge evaluation failed day=%s error=%s", day.isoformat(), detail)
            await self._record_event(
                "full_charge_evaluation_failed", {"day": day.isoformat(), "error": detail}
            )
            return
        if generation != self._generation:
            return
        fc.record_evaluation(day, evaluation.to_dict(), self.clock.now())
        await self._fc_flush(self.clock.now())

    # -- gating --------------------------------------------------------------------------------

    async def verify_ha_timezone(self) -> bool | None:
        """One check of Home Assistant's ``time_zone`` against the service's (``None``: no answer).

        Omnibattery decides the weekday with the container's local time, which Home Assistant
        reports as ``time_zone``: a different one would arm the wrong day, so the feature stays
        suspended and alerts.
        """
        if self.rest is None:
            return None
        try:
            config = await self.rest.get_config()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retried by the caller
            log.warning("full-charge time-zone check failed: %s", exc)
            return None
        zone = config.get("time_zone") if isinstance(config, Mapping) else None
        self._fc_ha_time_zone = zone if isinstance(zone, str) else None
        self._fc_tz_ok = zone == self.config.timezone
        if self._fc_tz_ok:
            self._fc_alerts.discard("timezone_mismatch")
            log.info("full-charge time zone verified time_zone=%s", zone)
        else:
            self._fc_alerts.add("timezone_mismatch")
            log.error(
                "full-charge disabled: Home Assistant time_zone=%r differs from the service's %r",
                zone,
                self.config.timezone,
            )
            await self._record_event(
                "full_charge_timezone_mismatch",
                {"ha_time_zone": zone, "service_time_zone": self.config.timezone},
            )
        return self._fc_tz_ok

    def _entities_problem(self) -> str | None:
        """The weekly-charge contract on the entities as Home Assistant reports them."""
        from .ha_guard import WEEKDAY_OPTIONS

        f = self.config.full_charge.entities
        for name in ("enable_switch", "day_select", "delay_switch"):
            state = self.store.get(getattr(f, name))
            if state is None or not state.available:
                return f"{name} missing or unavailable"
        day = self.store.get(f.day_select)
        options = day.attributes.get("options") if day is not None else None
        if not isinstance(options, list) or not set(WEEKDAY_OPTIONS) <= set(map(str, options)):
            return "day_select does not offer the seven weekdays"
        return None

    def _update_full_charge_suspension(self, synced: bool) -> None:
        fc = self.full_charge
        assert fc is not None
        reason: str | None = None
        if self._fc_tz_ok is False:
            reason = "timezone_mismatch"
        elif self._fc_tz_ok is None:
            reason = "timezone_unverified"
        else:
            if synced:
                self._fc_entities_problem = self._entities_problem()
            if self._fc_entities_problem:
                reason = "entities_missing"
        if reason != fc.suspended:
            log.log(
                logging.WARNING if reason else logging.INFO,
                "full-charge %s reason=%s detail=%s",
                "suspended" if reason else "resumed",
                reason or fc.suspended,
                self._fc_entities_problem or "-",
            )
        fc.suspend(reason)

    # -- the loop ------------------------------------------------------------------------------

    async def _full_charge_loop(self) -> None:
        while self._fc_tz_ok is None:
            if await self.verify_ha_timezone() is not None:
                break
            await self.clock.sleep(_TZ_CHECK_RETRY_S)
        while True:
            try:
                await self.full_charge_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive one bad tick
                log.exception("full-charge tick failed")
            await self.clock.sleep(_FULL_CHARGE_TICK_S)

    async def full_charge_tick(self) -> None:
        """Step the scheduler once and hand the actuator its directive."""
        fc = self.full_charge
        if fc is None:
            return
        now = self.clock.now()
        synced = self.ha is not None and self.ha.stats.synced
        self._update_full_charge_suspension(synced)
        if not synced and fc.suspended is None:
            return  # nothing observed is trustworthy; the directive stays as it was
        directive = fc.step(self._full_charge_observation(now))
        isolated = self.config.full_charge_isolated  # never a directive: nothing is driven
        new = None if fc.suspended is not None or isolated else directive
        if new != self._fc_directive:
            self._fc_directive = new
            self._wake_actuator()
        await self._fc_flush(now)

    def _full_charge_observation(self, now: datetime) -> Observation:
        fc = self.full_charge
        assert fc is not None
        e = self.config.full_charge.entities

        def text(entity: str | None) -> str | None:
            state = self.store.get(entity)
            return parse_text(state.state) if state is not None else None

        soc, _, _ = self._soc_now(now)
        status = text(self.config.actuation.entities.integration_status)
        balance = text(e.balance_status)
        trend = text(e.delta_trend)
        read_state = self.store.get(e.last_balance_read)
        driven = refusal = None
        if self.actuator is not None:
            refusal = self.actuator.full_charge_refusal
            desired = self.actuator.desired
            driven = desired is not None and desired.full_charge_armed is True
        tomorrow = bool(self.price_curve and self.price_curve.tomorrow_published)
        return Observation(
            now=now,
            soc_pct=None if soc is None else soc * 100.0,
            integration_status=status,
            read_at=parse_timestamp(read_state.state) if read_state is not None else None,
            balance_status=None if balance is None else balance.lower(),
            delta_trend=None if trend is None else trend.lower(),
            driven_armed=driven,
            refusal=refusal,
            tomorrow_published=tomorrow,
            arm_due=self._full_charge_arm_due(now),
            evidence=self._full_charge_evidence(soc),
        )

    def _full_charge_arm_due(self, now: datetime) -> bool:
        fc = self.full_charge
        assert fc is not None
        run = fc.state.run
        if run is None or fc.state.state != "scheduled" or not self.plans:
            return False
        plan = self.plans[-1]
        if plan.plan.is_expired(now):
            return False
        cfg = self.config.full_charge
        return arm_due(
            now,
            local_day_bounds_utc(run.day, self._tz)[0],
            [PlanSlot(i.valid_from, i.mode == "charge", i.soc_target_frac) for i in plan.intents],
            interval_seconds=self._interval,
            arm_lead_slots=cfg.arm_lead_slots,
            arm_before_soc_frac=cfg.arm_before_soc_frac,
        )

    def _full_charge_evidence(self, soc: float | None) -> dict[str, Any]:
        """Read-only sensors recorded next to a transition or an outcome."""
        e = self.config.full_charge.entities

        def raw(entity: str | None) -> str | None:
            state = self.store.get(entity)
            return state.state if state is not None and state.available else None

        def number(entity: str | None) -> float | None:
            state = self.store.get(entity)
            return state.numeric() if state is not None else None

        return {
            "soc_pct": None if soc is None else round(soc * 100.0, 2),
            "cell_delta_mv": number(e.cell_delta),
            "delta_avg_4w_mv": number(e.delta_avg_4w),
            "balancing_mode": raw(e.balancing_mode),
            "weekly_switch": raw(e.enable_switch),
            "weekly_day": raw(e.day_select),
            "delay_switch": raw(e.delay_switch),
            "charge_delay_switch": raw(e.charge_delay_switch),
            "integration_status": raw(self.config.actuation.entities.integration_status),
        }

    async def _fc_flush(self, now: datetime) -> None:
        """Log and record the scheduler's events, count outcomes, save the state when it changed."""
        fc = self.full_charge
        assert fc is not None
        replan = False
        for event in fc.drain_events():
            kind = event["event"]
            if kind == "decision":
                log.info(
                    "full-charge decision day=%s d=%s class=%s forced=%s accepted=%s "
                    "forcing_cost=%s vs_normal=%s threshold=%s shortfall=%s checkpoint=%s",
                    event["day"],
                    event["d"],
                    event["cadence_class"],
                    event["forced"],
                    event["accepted"],
                    _fmt(event.get("forcing_cost_eur"), 4),
                    _fmt(event.get("vs_normal_eur"), 4),
                    _fmt(event.get("threshold_eur"), 4),
                    _fmt(event.get("shortfall_kwh"), 3),
                    event.get("checkpoint_slot") or "-",
                )
            elif kind == "transition":
                log.info(
                    "full-charge state %s -> %s reason=%s day=%s",
                    event["from_state"],
                    event["to_state"],
                    event["reason"],
                    event["day"],
                )
                replan = replan or event["to_state"] in ("scheduled", "idle")
            elif kind == "outcome":
                self.metrics.full_charge_runs.labels(outcome=event["outcome"]).inc()
                log.log(
                    logging.INFO if event["outcome"].startswith("done") else logging.WARNING,
                    "full-charge outcome=%s reason=%s day=%s soc_peak=%s",
                    event["outcome"],
                    event["reason"],
                    event["day"],
                    _fmt(event.get("soc_peak_pct"), 1),
                )
            elif kind == "paused":
                log.warning("full-charge paused until %s", event["paused_until"])
            await self._write(self.sink.append_full_charge, now, event)
        if fc.dirty:
            fc.dirty = False
            await self._save_full_charge()
        if replan:
            self._request_replan("full_charge")

    async def _save_full_charge(self) -> None:
        fc = self.full_charge
        assert fc is not None
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._io, self.sink.save_full_charge, fc.state.to_json())
        except (OSError, RuntimeError, ValueError) as exc:
            self._fc_save_failed = True
            self.sink.write_errors += 1
            self.metrics.sink_errors.inc()
            log.error("full-charge state not saved: %s", exc)
        else:
            self._fc_save_failed = False

    def _full_charge_alerts(self, now: datetime) -> set[str]:
        fc = self.full_charge
        assert fc is not None
        alerts = fc.alerts(now) | self._fc_alerts
        if self._fc_entities_problem and fc.suspended == "entities_missing":
            alerts.add("entities_missing")
        if self._fc_save_failed:
            alerts.add("state_unwritable")
        refusal = self.actuator.full_charge_refusal if self.actuator is not None else None
        alert = REFUSAL_ALERTS.get(refusal) if refusal is not None else None
        if alert:
            alerts.add(alert)
        return alerts & set(ALERT_REASONS)

    def _full_charge_metric(self, now: datetime) -> dict[str, Any]:
        fc = self.full_charge
        assert fc is not None
        view = fc.view(now)
        st = fc.state
        run = st.run
        decision = st.last_decision or {}
        threshold = decision.get("threshold_eur")
        finite = threshold is not None and abs(threshold) != float("inf")
        return {
            "mode": self.config.full_charge.mode,
            "state": st.state,
            "days_since": view["days_since"],
            "last_timestamp": None if st.last_full_at is None else st.last_full_at.timestamp(),
            "scheduled_day_timestamp": local_day_bounds_utc(run.day, self._tz)[0].timestamp()
            if run is not None
            else None,
            "forcing_cost": decision.get("forcing_cost_eur"),
            "threshold": threshold if finite else None,
            "soc_peak_pct": run.soc_peak_pct
            if run is not None
            else (st.last_outcome or {}).get("soc_peak_pct"),
            "cadence_class": view["cadence"]["class"],
            "min_days": view["cadence"]["min_days"],
            "max_days": view["cadence"]["max_days"],
            "alerts": sorted(self._full_charge_alerts(now)),
        }

    def _full_charge_status(self, now: datetime) -> dict[str, Any]:
        fc = self.full_charge
        assert fc is not None
        actuator = self.actuator.view().get("full_charge") if self.actuator is not None else None
        directive = self._fc_directive
        return {
            "enabled": True,
            "mode": self.config.full_charge.mode,
            "writer": "live"
            if fc.writer_mode == "live"
            else "simulated (nothing is sent)"
            if not self.config.full_charge_isolated
            else "simulated (beside live control: no target plan, no directive, no write)",
            **fc.view(now),
            "alerts": sorted(self._full_charge_alerts(now)),
            "state_problem": self._fc_state_problem,
            "time_zone": {
                "service": self.config.timezone,
                "home_assistant": self._fc_ha_time_zone,
                "ok": self._fc_tz_ok,
            },
            "entities_problem": self._fc_entities_problem,
            "directive": None
            if directive is None
            else {
                "armed": directive.armed,
                "weekday": directive.weekday,
                "settle_until": directive.settle_until.isoformat()
                if directive.settle_until
                else None,
            },
            "evidence": self._full_charge_evidence(self._soc_now(now)[0]),
            "actuator": actuator,
        }

    # ------------------------------------------------------------------ actuation

    def _wake_actuator(self) -> None:
        if self.actuator is not None:
            self._actuator_wake.set()

    def _actuation_wake_on(self, state: EntityState, role: str | None) -> None:
        """Wake the actuator on a kill-switch change, and on over-cap import while charging.

        The over-cap wake fires at most once per tick interval, so a long high load does not
        turn into a tick (and its REST refreshes) every 2 s.
        """
        assert self.actuator is not None
        act = self.config.actuation
        if state.entity_id == act.entities.kill_switch:
            self._wake_actuator()
            return
        if role != "grid_power":
            return
        value = state.numeric()
        if value is None or value <= act.import_cap_w + act.import_guard_margin_w:
            return
        desired = self.actuator.desired
        charging = self.actuator.readings.get("allow_charge") or (
            desired is not None and desired.mode == "charge"
        )
        now = self.clock.now()
        last = self._overcap_wake_at
        if charging and (last is None or (now - last).total_seconds() >= act.tick_interval_s):
            self._overcap_wake_at = now
            self._wake_actuator()  # the import hard rule must not wait for the next tick

    def actuation_context(self, now: datetime) -> ActuationContext:
        """The intent frozen for the current slot and what the guard needs about it."""
        current = floor_slot(now, self._interval)
        opened = self._slot_open.get(current)
        intent = opened.intent if opened is not None else None
        decision = opened.decision if opened is not None else "no_plan"
        own = intent is not None and any(
            p.plan_id == intent.plan_id and p.plan.generated_at >= self._started_at
            for p in self.plans
        )
        curve = self.price_curve
        price_fresh = curve is not None and curve.price_at(current) is not None
        soc_floor_pct = _soc_floor_pct(self.tariff)
        return ActuationContext(
            now=now,
            ha_synced=self.ha is not None and self.ha.stats.synced,
            intent=intent,
            decision=decision,
            plan_from_this_process=own,
            price_fresh=price_fresh,
            full_charge=self._fc_directive,
            soc_floor_pct=soc_floor_pct,
        )

    async def _send_actuation(
        self, domain: str, service: str, entity_id: str, data: Mapping[str, Any] | None
    ) -> None:
        if self.ha is None:
            raise HACommandError("actuation", "Home Assistant is not configured")
        await self.ha.actuate(domain, service, entity_id, data)

    async def _refresh_entities(self, entities: tuple[str, ...]) -> None:
        """REST GET of *entities* into the store (sensors HA re-reports without an event)."""
        if self.rest is None:
            return
        from .entity_store import parse_ha_state

        for entity in entities:
            try:
                payload = await self.rest.get_state(entity)
                if payload is not None:
                    self.store.apply(parse_ha_state(payload, received_at=self.clock.now()))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the WebSocket stays authoritative
                self.metrics.rest_polls.labels(result="error").inc()

    async def _record_actuation(self, at: datetime, record: dict[str, Any]) -> None:
        await self._write(
            self.sink.append_actuation, at, {**record, "code_version": self.code_version}
        )

    async def _actuator_loop(self) -> None:
        assert self.actuator is not None
        while True:
            self._actuator_wake.clear()
            try:
                await self.actuator.tick(self.actuation_context(self.clock.now()))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive one bad tick
                log.exception("actuator tick failed")
            sleep = asyncio.ensure_future(self.clock.sleep(self.config.actuation.tick_interval_s))
            wake = asyncio.ensure_future(self._actuator_wake.wait())
            try:
                await asyncio.wait({sleep, wake}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (sleep, wake):
                    task.cancel()

    # ------------------------------------------------------------------ views

    def _inputs_view(self, now: datetime) -> list[dict[str, Any]]:
        out = []
        for role, entity in self.config.entities.configured().items():
            state = self.store.get(entity)
            age = self.store.age_s(entity, now)
            limit = self.config.freshness_for(role) if role != "price" else None
            fresh = state is not None and state.available
            if limit is not None:
                fresh = fresh and age is not None and age <= limit
            out.append(
                {
                    "role": role,
                    "entity": entity,
                    "seen": state is not None,
                    "available": state.available if state else None,
                    "age_s": None if age is None else round(age, 3),
                    "max_age_s": limit,
                    "fresh": bool(fresh),
                }
            )
        return out

    def metric_snapshot(self) -> dict[str, Any]:
        now = self.clock.now()
        readiness = self.readiness(now)
        plan = self.plans[-1] if self.plans else None
        intent = self.current_intent(now)
        ha = self.ha.stats if self.ha else None
        return {
            "ready": 1.0 if readiness.ready else 0.0,
            "inputs_ready": 1.0 if readiness.inputs_ready else 0.0,
            "reasons": readiness.reasons,
            "ha": {}
            if ha is None
            else {
                "connected": 1.0 if ha.connected else 0.0,
                "synced": 1.0 if ha.synced else 0.0,
                "reconnects": ha.reconnects,
                "resyncs": ha.resyncs,
                "events": ha.events_received,
                "failures": dict(ha.failures),
            },
            "updates": self.store.totals(),
            "inputs": self._inputs_view(now),
            "price_horizon_s": (self.price_curve.end - now).total_seconds()
            if self.price_curve and self.price_curve.end
            else None,
            "tomorrow_published": None
            if self.price_curve is None
            else (1.0 if self.price_curve.tomorrow_published else 0.0),
            "plan_age_s": (now - plan.plan.generated_at).total_seconds() if plan else None,
            "plan_slots": plan.plan.n if plan else None,
            "current_intent_w": intent.signed_power_w if intent else None,
            "current_intent_active": 1.0 if intent else 0.0,
            "slot_plans": self._current_slot_plans(now),
            "worker_busy": 1.0 if self._worker_busy else 0.0,
            "history_usable_slots": self.history.usable_slots if self.history else None,
            "history_age_s": (now - self.history.fetched_at).total_seconds()
            if self.history
            else None,
            "terminal_value": plan.snapshot.terminal_value_eur_per_kwh if plan else None,
            "virtual_soc": self.virtual_soc,
            "late_samples": self.observer.late_samples,
            "climate_shadow": self._climate_view(),
            "climate_plan": self._climate_plan_view(now),
            **({"battery_health": self.health.snapshot(now)} if self.health is not None else {}),
            **({"thermal": self._thermal_view(now)} if self.thermal is not None else {}),
            **({"actuator": self.actuator.snapshot()} if self.actuator is not None else {}),
            **(
                {"full_charge": self._full_charge_metric(now)}
                if self.full_charge is not None
                else {}
            ),
        }

    def status(self) -> dict[str, Any]:
        now = self.clock.now()
        readiness = self.readiness(now)
        ha = self.ha.stats if self.ha else None
        return {
            "service": "battery-optimizer",
            "mode": self.config.mode,
            "actuation": _ACTUATION_TEXT[self.config.mode],
            "code_version": self.code_version,
            "now": now.isoformat(),
            "started_at": self._started_at.isoformat(),
            "ready": readiness.ready,
            "inputs_ready": readiness.inputs_ready,
            "not_ready": readiness.details,
            "soc_source": self.config.planning.soc_source,
            "home_assistant": None
            if ha is None
            else {
                "connected": ha.connected,
                "synced": ha.synced,
                "connects": ha.connects,
                "reconnects": ha.reconnects,
                "resyncs": ha.resyncs,
                "failures": dict(ha.failures),
                "last_error": ha.last_error,
                "last_connected_at": ha.last_connected_at.isoformat()
                if ha.last_connected_at
                else None,
                "last_synced_at": ha.last_synced_at.isoformat() if ha.last_synced_at else None,
                "next_retry_delay_s": ha.next_retry_delay_s,
            },
            "entity_updates": self.store.counts,
            "inputs": self._inputs_view(now),
            "prices": self.price_curve.summary()
            if self.price_curve
            else {"error": self.price_error},
            "history": self.history.summary() if self.history else {"error": self.history_error},
            "forecast_error": self.forecast_error,
            "solver": {
                "worker_busy": self._worker_busy,
                "last_solve": self.last_solve,
                "plans_in_memory": len(self.plans),
            },
            "virtual_battery": {"soc_frac": self.virtual_soc, "origin": self.virtual_soc_origin},
            "store": {"root": str(self.sink.root), "write_errors": self.sink.write_errors},
            "archives": {
                "pvpc_snapshots": {
                    "enabled": self.config.archive.pvpc_snapshots.enabled,
                    "last_success_at": self._archive_success_at("pvpc_snapshots"),
                },
                "weather_forecast": {
                    "enabled": self.config.archive.weather_forecast.enabled,
                    "entity_id": self.config.archive.weather_forecast.entity_id,
                    "last_success_at": self._archive_success_at("weather_forecast"),
                    "last_error": self.weather_error,
                },
            },
            "climate_shadow": self._climate_status(),
            "climate_plan": self._climate_plan_status(now),
            **({"battery_health": self._health_status(now)} if self.health is not None else {}),
            **({"thermal": self._thermal_view(now)} if self.thermal is not None else {}),
            "config_hash": self.config.config_hash(),
            "config_overrides": self.config.override_values(),
            **({"actuator": self.actuator.view()} if self.actuator is not None else {}),
            **(
                {"full_charge": self._full_charge_status(now)}
                if self.full_charge is not None
                else {}
            ),
        }

    def _health_status(self, now: datetime) -> dict[str, Any]:
        assert self.health is not None
        return {
            "actuation": "none (read-only telemetry)",
            "charge_sessions_tracked": "charge_power" in self._health_roles.values(),
            **self.health.snapshot(now),
        }

    def _climate_status(self) -> dict[str, Any]:
        view = self._climate_view()
        if view is None:
            return {"enabled": False}
        report = view.pop("report", None) or {}
        for key in ("last_run_at", "last_refit_at"):
            if view.get(key) is not None:
                view[key] = view[key].isoformat()
        return {
            "enabled": True,
            "actuation": "none (plans are recorded, never sent)",
            **view,
            "go_live": report.get("go_live"),
            "verdicts": {w: v["verdict"] for w, v in (report.get("windows") or {}).items()},
        }

    def _archive_success_at(self, archive: str) -> str | None:
        at = self.archive_last_success.get(archive)
        return at.isoformat() if at else None

    def _current_slot_plans(self, now: datetime) -> dict[str, SlotPlan | None] | None:
        """The day-ahead and final plan of the slot in progress; ``None`` before it has opened."""
        opened = self._slot_open.get(floor_slot(now, self._interval))
        if opened is None:
            return None
        return {"day_ahead": opened.day_ahead, "final": opened.final}

    def _slot_plans_view(self, now: datetime) -> dict[str, Any]:
        """``current_slot`` and ``day_ahead`` of ``/plan``: day-ahead vs final, per slot."""
        slot = floor_slot(now, self._interval)
        opened = self._slot_open.get(slot)
        day_ahead = opened.day_ahead if opened else self.day_ahead.get(slot)
        final = opened.final if opened else None
        newest: dict[datetime, SlotPlan] = {}
        if self.plans:
            newest = {p.slot_start: p for p in slot_plans_from_result(self.plans[-1])}

        def latest(slot_plan: SlotPlan) -> dict[str, Any] | None:
            current = newest.get(slot_plan.slot_start)
            if current is None:
                return None
            return {
                "plan_id": current.plan_id,
                "generated_at": current.generated_at.isoformat(),
                "signed_power_w": current.signed_power_w,
                "soc_target_frac": current.soc_target_frac,
                "load_forecast_w": current.load_forecast_w,
            }

        return {
            "current_slot": {
                "slot_start": slot.isoformat(),
                "slot_end": (slot + timedelta(seconds=self._interval)).isoformat(),
                "decision": opened.decision if opened else None,
                "day_ahead": day_ahead.to_dict() if day_ahead else None,
                "final": final.to_dict() if final else None,
                **({"thermal": self.thermal.current_slot(slot)} if self.thermal else {}),
            },
            "day_ahead": {
                "slots": [
                    {"slot_start": p.slot_start.isoformat(), **p.to_dict(), "latest": latest(p)}
                    for p in self.day_ahead.upcoming(now)
                ]
            },
        }

    def plan_view(self) -> dict[str, Any]:
        now = self.clock.now()
        plan = self.plans[-1] if self.plans else None
        intent = self.current_intent(now)
        slots = self._slot_plans_view(now)
        if plan is None:
            return {
                "shadow": True,
                "plan": None,
                "current_intent": None,
                "now": now.isoformat(),
                **slots,
            }
        record = plan.to_record()
        record.pop("inputs", None)
        if self.thermal is not None and (block := self._thermal_block_for(plan.plan_id)):
            record["thermal"] = block
        return {
            "shadow": True,
            "now": now.isoformat(),
            "expired": plan.plan.is_expired(now),
            "current_intent": intent.to_dict() if intent else None,
            **slots,
            "plan": record,
        }


def utc_now() -> datetime:
    return datetime.now(UTC)
