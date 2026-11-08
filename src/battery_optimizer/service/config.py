"""Service configuration: ``config/service.yaml`` plus environment overrides.

Endpoints and credentials never live in the YAML: ``BATTERY_OPTIMIZER_HA_URL``,
``BATTERY_OPTIMIZER_HA_TOKEN`` (or ``BATTERY_OPTIMIZER_HA_TOKEN_FILE``, the form a
mounted Kubernetes secret takes) and ``BATTERY_OPTIMIZER_PROMETHEUS_URL``.  The
token is held in a :class:`Secret` whose ``repr`` never shows the value.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from battery_optimizer._env import get_env

from .thermal import DEFAULT_PARAMS

__all__ = [
    "BATTERY_HEALTH_DOMAINS",
    "BATTERY_ROLES",
    "ENTITY_ROLES",
    "SERVICE_MODES",
    "SOC_SOURCES",
    "THERMAL_DOMAINS",
    "THERMAL_MODES",
    "ActuationEntities",
    "ActuationSettings",
    "ArchiveSettings",
    "BatteryHealthEntities",
    "BatteryHealthSettings",
    "ClimateShadowSettings",
    "EntityMap",
    "FULL_CHARGE_MODES",
    "CadenceRow",
    "FullChargeCadence",
    "FullChargeEntities",
    "FullChargeSettings",
    "FullChargeTaper",
    "HomeAssistantSettings",
    "LoadFollowingSettings",
    "ObservationSettings",
    "PlanningSettings",
    "PrometheusSettings",
    "PvpcSnapshotSettings",
    "Secret",
    "ServiceConfig",
    "ThermalEntities",
    "ThermalPrior",
    "ThermalSettings",
    "WeatherForecastSettings",
    "load_service_config",
    "resolve_service_config_path",
]

#: Roles the service understands.  Each maps to at most one HA entity ID.
ENTITY_ROLES: tuple[str, ...] = (
    "grid_power",
    "grid_energy",
    "price",
    "battery_soc",
    "battery_power",
    "battery_charge_power",
    "battery_discharge_power",
    "battery_charge_energy",
    "battery_discharge_energy",
)

#: The roles that describe the physical battery.
BATTERY_ROLES: tuple[str, ...] = tuple(r for r in ENTITY_ROLES if r.startswith("battery_"))

SOC_SOURCES: tuple[str, ...] = ("entity", "simulated")

#: Roles whose ``freshness_s`` may be ``null`` and are by default: availability only, no age
#: test.  Omnibattery reports these only on change, so an idle battery's SOC and AC energy
#: counters keep an old ``last_reported`` while healthy (verified 2026-11-03).
AVAILABILITY_ONLY_ROLES: tuple[str, ...] = (
    "battery_soc",
    "battery_charge_energy",
    "battery_discharge_energy",
)

#: ``shadow`` records intents only; ``dry_run`` also computes and records every Omnibattery
#: write it would make; ``live`` sends them through the guarded allowlist (ADR-0007).
SERVICE_MODES: tuple[str, ...] = ("shadow", "dry_run", "live")

DEFAULT_PORT = 8080


class Secret:
    """A string that never appears in ``repr``/``str``/logs."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class EntityMap:
    """Role -> entity ID mapping plus read-only context entities."""

    roles: Mapping[str, str | None]
    observed: tuple[str, ...] = ()

    def get(self, role: str) -> str | None:
        if role not in ENTITY_ROLES:
            raise KeyError(f"unknown entity role {role!r}")
        return self.roles.get(role)

    def configured(self) -> dict[str, str]:
        """Roles that have an entity, in canonical role order."""
        return {r: e for r in ENTITY_ROLES if (e := self.roles.get(r))}

    def all_entities(self) -> tuple[str, ...]:
        """Every entity the service subscribes to (roles first, then observed)."""
        seen: dict[str, None] = {}
        for entity in (*self.configured().values(), *self.observed):
            seen.setdefault(entity, None)
        return tuple(seen)

    def role_of(self, entity_id: str) -> str | None:
        for role, entity in self.configured().items():
            if entity == entity_id:
                return role
        return None

    @property
    def has_battery(self) -> bool:
        return any(self.roles.get(r) for r in BATTERY_ROLES)


@dataclass(frozen=True)
class HomeAssistantSettings:
    url: str | None
    token: Secret | None = field(repr=False, default=None)
    request_timeout_s: float = 15.0
    reconnect_initial_delay_s: float = 1.0
    reconnect_max_delay_s: float = 60.0
    reconnect_jitter_frac: float = 0.2
    heartbeat_interval_s: float = 30.0
    heartbeat_timeout_s: float = 10.0
    poll_interval_s: float = 60.0


@dataclass(frozen=True)
class PrometheusSettings:
    url: str | None
    timeout_s: float = 60.0
    history_days: int = 28


#: ``planning.thermal.mode``: ``off`` reads nothing and changes nothing; ``shadow`` reads the
#: Omnibattery temperature-limit entities, keeps the thermal state and records a prediction beside
#: every plan (observability only: no plan, hash or actuation changes).
THERMAL_MODES: tuple[str, ...] = ("off", "shadow")

#: Thermal entity -> required HA domain.  Every one is only read (never written, never in the
#: ``ha_guard`` allowlist).
THERMAL_DOMAINS: dict[str, str] = {
    "temperature": "sensor",
    "limit_switch": "switch",
    "discharge_switch": "switch",
    "limit_c": "number",
    "band_c": "number",
    "floor_pct": "number",
    "system_max_charge_power": "number",
    "venus_max_charge_power": "number",
}
#: The entities an active feature cannot work without; the rest only refine it.
THERMAL_REQUIRED: tuple[str, ...] = (
    "temperature",
    "limit_switch",
    "limit_c",
    "band_c",
    "floor_pct",
)


@dataclass(frozen=True)
class ThermalEntities:
    """Read-only inputs of the thermal state (Omnibattery's Temperature Charge Limit).

    ``temperature`` is the sensor ``battery_health.entities.temperature`` already reads (one entity
    store entry); if both name one they must agree, and either may be left to the other.  The
    discharge switch and the two cap numbers are optional: without the switch the discharge side
    is never derated in the prediction, without the caps ``planning.thermal.charge_limit_kw``
    stands in for ``min(system cap, Venus cap)``.
    """

    temperature: str | None = None
    limit_switch: str | None = None
    discharge_switch: str | None = None
    limit_c: str | None = None
    band_c: str | None = None
    floor_pct: str | None = None
    system_max_charge_power: str | None = None
    venus_max_charge_power: str | None = None

    def all(self) -> tuple[str, ...]:
        return tuple(e for k in THERMAL_DOMAINS if (e := getattr(self, k)))

    def by_entity(self) -> dict[str, str]:
        """Entity ID -> role, for the configured roles."""
        return {e: k for k in THERMAL_DOMAINS if (e := getattr(self, k))}


@dataclass(frozen=True)
class ThermalPrior:
    """Thermal parameters before any refit (the calibration record of ``battery-thermal``)."""

    tau_min: float = DEFAULT_PARAMS.tau_min
    t_amb_c: float = DEFAULT_PARAMS.t_amb_c
    k_c: float = DEFAULT_PARAMS.k_c
    k_d: float = DEFAULT_PARAMS.k_d


@dataclass(frozen=True)
class ThermalSettings:
    """Read-only thermal state and derate prediction (``service/thermal.py``, design in
    ``docs/planning/thermal-derate-aware-planning.md``).

    ``mode: off`` (default) is today's service, byte for byte.  ``shadow`` needs the required
    entities and ``actuation.entities.battery_ac_power`` with its sign (the measured AC power the
    refit and the scoring use); it never changes a plan or an actuation.
    """

    mode: str = "off"
    entities: ThermalEntities = field(default_factory=ThermalEntities)
    #: ``min(system cap, Venus cap)`` in kW when the cap entities are not configured or unknown.
    charge_limit_kw: float = 2.5  # the Venus E V3's rated power
    #: Omnibattery's driver minimum power (kW): the derate never takes the cap below it.
    driver_min_kw: float = 0.8
    prior: ThermalPrior = field(default_factory=ThermalPrior)
    #: Local hour at (and after) which the daily refit runs; it also runs at start-up when due.
    refit_hour_local: int = 4

    @property
    def active(self) -> bool:
        return self.mode != "off"


@dataclass(frozen=True)
class PlanningSettings:
    soc_source: str = "simulated"
    interval_seconds: int = 900
    solve_lead_s: float = 120.0
    solve_timeout_s: float = 60.0
    solver_time_limit_s: float = 20.0
    plan_ttl_slots: int = 2
    min_horizon_slots: int = 4
    max_horizon_slots: int = 192
    forecast_window_days: int = 28
    load_forecaster: str = "trailing_mean"
    terminal_value: str | float = "trailing_mean"
    throughput_cost_eur_per_kwh: float = 0.0
    #: High-SOC dwell cost: EUR per kWh of SOC above ``high_soc_threshold_frac`` per hour held.
    #: 0 (the default) is off and leaves the plan, and :meth:`ServiceConfig.config_hash`, as before.
    high_soc_threshold_frac: float = 0.90
    high_soc_cost_eur_per_kwh_h: float = 0.0
    #: Thermal soft cap on charge power: EUR per AC kWh charged above ``charge_soft_cap_kw`` in a
    #: slot.  0 (the default) is off and leaves the plan, and :meth:`ServiceConfig.config_hash`, as
    #: before.
    charge_soft_cap_kw: float = 1.5
    charge_soft_cap_eur_per_kwh: float = 0.0
    #: Bound each slot's planned discharge by ``forecast load - actuation.discharge_margin_w``, the
    #: most the live actuator can deliver (it keeps that much grid import).  False (the default) is
    #: off and leaves the plan, and :meth:`ServiceConfig.config_hash`, as before.
    discharge_margin_bound: bool = False
    replan_soc_change_frac: float = 0.05
    replan_debounce_s: float = 30.0
    replan_min_interval_s: float = 120.0
    #: Read-only thermal state and derate prediction; ``mode: off`` by default.  Observability only
    #: in every mode, so it never joins :meth:`ServiceConfig.config_hash`.
    thermal: ThermalSettings = field(default_factory=ThermalSettings)


@dataclass(frozen=True)
class ObservationSettings:
    slot_close_grace_s: float = 20.0


@dataclass(frozen=True)
class PvpcSnapshotSettings:
    """Append a record for every state change of the price entity (PO-B)."""

    enabled: bool = False


@dataclass(frozen=True)
class WeatherForecastSettings:
    """Periodic ``weather.get_forecasts`` archive (Load Forecaster P4).

    Fetches run at ``offset_minutes`` past every ``interval_minutes`` of the UTC day.
    """

    enabled: bool = False
    entity_id: str = "weather.forecast_home"
    type: str = "hourly"
    interval_minutes: int = 60
    offset_minutes: int = 5


@dataclass(frozen=True)
class ArchiveSettings:
    """Passive, append-only data collection; never part of the planning inputs."""

    pvpc_snapshots: PvpcSnapshotSettings = field(default_factory=PvpcSnapshotSettings)
    weather_forecast: WeatherForecastSettings = field(default_factory=WeatherForecastSettings)


@dataclass(frozen=True)
class ClimateShadowSettings:
    """Live shadow of the thermal model and the comfort MPC (track B2); reads only.

    ``climate_config`` and ``mpc_config`` are relative to the service config's directory.
    The three entities are read from Home Assistant if they exist and are never written.
    """

    enabled: bool = False
    climate_config: str = "climate.yaml"
    mpc_config: str = "climate-mpc.yaml"
    #: Id (or unique prefix) of ``<data_dir>/analysis/v1/climate-shadow/priors/<id>/``.
    prior: str | None = None
    #: Minutes after each decision boundary the job runs (data of the last slot has landed).
    offset_minutes: int = 10
    forecast_max_age_minutes: int = 180
    #: Unscored decisions older than this are dropped from the scoring queue.
    score_lookback_days: int = 7
    #: Local day of the comfort controller's go-live; ``None`` = the first ``comfort`` record.
    go_live: date | None = None
    regime_entity: str | None = "input_boolean.comfort_control_enable"
    dhw_plan_entity: str | None = "input_text.comfort_dhw_plan"
    away_entity: str | None = "input_boolean.comfort_away"
    #: The owner's mode select (read only): ``off`` / ``shadow`` / ``live`` (ADR-0008, proposed).
    mode_entity: str | None = "input_select.climate_optimizer_mode"
    #: The advisory ClimatePlan export's mode when the helper is missing, unavailable or garbage.
    #: ``off`` (the default): ``/climate/plan`` answers 404.  ``live`` is a request only: the
    #: effective mode is never ``live`` in this code version (``climate/plan_export.py``).
    optimizer_mode: str = "off"
    #: A plan is valid this long after its decision time; consumers treat an older one as absent.
    plan_ttl_minutes: int = 90

    def entities(self) -> tuple[str, ...]:
        return tuple(
            e
            for e in (
                self.regime_entity,
                self.dhw_plan_entity,
                self.away_entity,
                self.mode_entity,
            )
            if e
        )


#: Actuation entity -> required HA domain.  ``kill_switch`` and the sensors are only read.
ACTUATION_DOMAINS: dict[str, str] = {
    "kill_switch": "input_boolean",
    "heartbeat": "input_datetime",
    "allow_charge": "switch",
    "allow_discharge": "switch",
    "pd_target": "number",
    "power_limits": "switch",
    "max_discharge_power": "number",
    "integration_status": "sensor",
    "battery_ac_power": "sensor",
    "battery_soc": "sensor",
    "battery_liveness": "sensor",
}

#: Contracted import (W); the meter trips instantly above it.
CONTRACTED_IMPORT_W = 5000.0
#: Venus E V3 rated power (W): the discharge cap is never written above it.
BATTERY_RATED_POWER_W = 2500.0
#: Longest tick: well inside the HA watchdog's 5-minute heartbeat window.
MAX_TICK_INTERVAL_S = 120.0

#: Entities that may stay ``null`` even outside shadow mode. None: ``battery_liveness`` is required
#: because Omnibattery keeps the last SOC/AC power values when Modbus fails (seen 2026-11-03).
_OPTIONAL_ACTUATION_ENTITIES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ActuationEntities:
    """The Omnibattery control surface plus the owner's kill switch and heartbeat helper.

    Written (live mode only, through :mod:`.ha_guard`): the two direction switches, the PD
    grid target, the system power-limit switch, the system discharge cap and the heartbeat.
    Only read: the kill switch, the integration status, battery AC power and SOC, and the
    ``battery_liveness`` sensor (Omnibattery's non-responsive-batteries count, whose
    healthy state is the text ``None``).
    """

    kill_switch: str | None = "input_boolean.battery_optimizer_live"
    heartbeat: str | None = "input_datetime.battery_optimizer_heartbeat"
    allow_charge: str | None = None
    allow_discharge: str | None = None
    pd_target: str | None = None
    power_limits: str | None = None
    max_discharge_power: str | None = None
    integration_status: str | None = None
    battery_ac_power: str | None = None
    battery_soc: str | None = None
    battery_liveness: str | None = None

    def all(self) -> tuple[str, ...]:
        return tuple(e for k in ACTUATION_DOMAINS if (e := getattr(self, k)))


@dataclass(frozen=True)
class LoadFollowingSettings:
    """Load-following discharge (ADR-0007 amendment 2026-11-07); **off** by default.

    In a planned discharge slot, while the measured SOC is at or above the plan's end-of-slot SOC,
    the system discharge cap is raised from the planned power to ``max_w`` (``None``: the
    actuation's ``max_discharge_cap_w``), so the battery covers the real load and not only the
    forecast one; the PD grid target (the discharge margin) still prevents export.  It engages at
    ``plan SOC + soc_hysteresis_pct`` and releases below the plan SOC.
    """

    enabled: bool = False
    max_w: float | None = None
    soc_hysteresis_pct: float = 1.0


@dataclass(frozen=True)
class ActuationSettings:
    """Live control through Omnibattery (ADR-0007); ignored in ``shadow`` mode."""

    entities: ActuationEntities = field(default_factory=ActuationEntities)
    tick_interval_s: float = 30.0
    #: Highest grid-import target ever written (W); below the 5 kW contracted import.
    import_cap_w: float = 4500.0
    #: Measured import above ``import_cap_w + import_guard_margin_w`` while charging trips charge.
    import_guard_margin_w: float = 200.0
    #: After a trip, charge intents stay refused this long.
    import_trip_hold_s: float = 300.0
    neutral_pd_target_w: float = 0.0
    discharge_margin_w: float = 50.0
    #: The system discharge cap is never written below this (0 would mean UNLIMITED).
    min_discharge_cap_w: float = 100.0
    max_discharge_cap_w: float = 2500.0
    #: ``+1``: the AC power sensor is + while charging; ``-1``: + while discharging.
    #: Required outside shadow mode (no default: the sign must be measured).
    battery_ac_power_sign: int | None = None
    healthy_states: tuple[str, ...] = ("balanced", "charging", "discharging")
    max_attempts: int = 3
    readback_timeout_s: float = 10.0
    min_setpoint_change_w: float = 100.0
    min_command_interval_s: float = 30.0
    shutdown_timeout_s: float = 15.0
    grid_power_max_age_s: float = 30.0
    #: ``None`` = availability only (present, not unavailable/unknown, numeric), no age test:
    #: Omnibattery's coordinator entities report only on change, so an idle battery's SOC and
    #: AC power keep their old ``last_reported``; they also keep their last value when Modbus
    #: fails, so ``battery_liveness`` is the check that catches a non-responsive battery.
    battery_soc_max_age_s: float | None = None
    battery_ac_power_max_age_s: float | None = None
    load_following: LoadFollowingSettings = field(default_factory=LoadFollowingSettings)
    #: Reserve protection (ADR-0007 amendment 2026-11-08): the AC charge power (W) the actuator
    #: requests when the *measured* SOC is at or below the tariff's floor and the plan does not
    #: already ask for more.  Modest and bounded, never above ``import_cap_w``; a control setting,
    #: not an energy-model claim about the standby drain's cause.
    soc_floor_recovery_w: float = 200.0
    #: SOC points above the floor at which the reserve protection releases (anti-chatter).
    soc_floor_hysteresis_pct: float = 1.0

    @property
    def load_following_ceiling_w(self) -> float:
        """The cap load-following asks for: ``max_w``, never above ``max_discharge_cap_w``."""
        max_w = self.load_following.max_w
        return self.max_discharge_cap_w if max_w is None else min(max_w, self.max_discharge_cap_w)

    def command_policy(self) -> Any:
        """The writer knobs as the validated :class:`~battery_optimizer.dispatch.CommandPolicy`.

        ``lease_seconds`` is the readback timeout: how long one write may stay unconfirmed.
        """
        from battery_optimizer.dispatch import CommandPolicy

        return CommandPolicy(
            max_attempts=self.max_attempts,
            min_setpoint_change_w=self.min_setpoint_change_w,
            min_command_interval_seconds=self.min_command_interval_s,
            lease_seconds=self.readback_timeout_s,
        )


#: Full-charge entity -> required HA domain.  Only ``enable_switch`` and ``day_select`` are
#: written (``live`` mode with ``full_charge.enabled``, through :mod:`.ha_guard`); the rest is read.
FULL_CHARGE_DOMAINS: dict[str, str] = {
    "enable_switch": "switch",
    "day_select": "select",
    "delay_switch": "switch",
    "charge_delay_switch": "switch",
    "last_balance_read": "sensor",
    "balancing_mode": "binary_sensor",
    "cell_delta": "sensor",
    "balance_status": "sensor",
    "delta_trend": "sensor",
    "delta_avg_4w": "sensor",
}


@dataclass(frozen=True)
class FullChargeEntities:
    """Omnibattery's weekly-full-charge surface (periodic full charge for cell balancing).

    Written: ``enable_switch`` and ``day_select``.  Only read: ``delay_switch`` (must be off),
    ``charge_delay_switch`` (logged) and the balance sensors (the scheduler reads
    ``last_balance_read``, ``balance_status`` and ``delta_trend``; the rest is evidence).  All
    required when enabled.  The balance sensors may name the same entities as
    ``battery_health.entities`` (one entity store entry each); the two sections must then agree.
    """

    enable_switch: str | None = None
    day_select: str | None = None
    delay_switch: str | None = None
    charge_delay_switch: str | None = None
    last_balance_read: str | None = None
    balancing_mode: str | None = None
    cell_delta: str | None = None
    balance_status: str | None = None
    delta_trend: str | None = None
    delta_avg_4w: str | None = None

    def all(self) -> tuple[str, ...]:
        return tuple(e for k in FULL_CHARGE_DOMAINS if (e := getattr(self, k)))


#: Battery-health entity -> required HA domain.  Every one is only read (never written).
BATTERY_HEALTH_DOMAINS: dict[str, str] = {
    "temperature": "sensor",
    "cell_voltage_max": "sensor",
    "cell_voltage_min": "sensor",
    "cell_delta": "sensor",
    "balance_status": "sensor",
    "last_balance_read": "sensor",
    "delta_trend": "sensor",
    "delta_avg_4w": "sensor",
    "balancing_mode": "binary_sensor",
}


@dataclass(frozen=True)
class BatteryHealthEntities:
    """Read-only battery health sensors (Omnibattery's balance monitor and the cell data).

    All optional.  They may name entities that ``full_charge.entities`` also reads (the balance
    evidence): both subscriptions share one entity store entry.
    """

    temperature: str | None = None
    cell_voltage_max: str | None = None
    cell_voltage_min: str | None = None
    cell_delta: str | None = None
    balance_status: str | None = None
    last_balance_read: str | None = None
    delta_trend: str | None = None
    delta_avg_4w: str | None = None
    balancing_mode: str | None = None

    def all(self) -> tuple[str, ...]:
        return tuple(e for k in BATTERY_HEALTH_DOMAINS if (e := getattr(self, k)))

    def by_entity(self) -> dict[str, str]:
        """Entity ID -> role, for the configured roles."""
        return {e: k for k in BATTERY_HEALTH_DOMAINS if (e := getattr(self, k))}


@dataclass(frozen=True)
class BatteryHealthSettings:
    """Read-only battery health telemetry (``service/battery_health.py``).

    Active when at least one entity is configured; no write path.  Charge sessions need
    ``actuation.entities.battery_ac_power`` and ``actuation.battery_ac_power_sign`` (the same
    entity and sign convention the actuator uses), otherwise only the other fields are reported.
    """

    entities: BatteryHealthEntities = field(default_factory=BatteryHealthEntities)
    #: Temperatures (degC) whose exposure is counted in seconds strictly above each.
    hot_thresholds_c: tuple[float, ...] = (35.0, 40.0)
    #: Sign-corrected battery AC power above which the battery counts as charging (W).
    charge_threshold_w: float = 100.0
    #: Seconds the power must stay at or below the threshold before a session is over.
    session_end_hold_s: float = 300.0
    #: Longest span one advance of the clock credits to the held temperature (s).
    max_gap_s: float = 300.0

    @property
    def configured(self) -> bool:
        return bool(self.entities.all())


@dataclass(frozen=True)
class FullChargeTaper:
    """The convex taper model of the top of the charge; ``start_soc_frac: None`` disables it."""

    start_soc_frac: float | None = 0.90
    power_kw: float = 0.2


#: ``full_charge.mode``: ``shadow`` simulates the feature (nothing about the real control path
#: changes), ``live`` lets it arm Omnibattery's weekly full charge (service ``mode: live`` only).
FULL_CHARGE_MODES: tuple[str, ...] = ("shadow", "live")


@dataclass(frozen=True)
class CadenceRow:
    """Days between full charges for one cadence class: never before ``min``, forced at ``max``."""

    min_days: int
    max_days: int


@dataclass(frozen=True)
class FullChargeCadence:
    """The adaptive cadence table (amendment A3), keyed by the class of the latest valid reading."""

    unknown: CadenceRow = CadenceRow(10, 14)  # also: fewer than min_comparable_readings
    green: CadenceRow = CadenceRow(
        14, 28
    )  # enough comparable readings, all green, trend not rising
    yellow: CadenceRow = CadenceRow(7, 14)  # also a single orange or red after a better reading
    alert: CadenceRow = CadenceRow(5, 7)  # orange or red on alert_consecutive readings in a row

    def row(self, name: str) -> CadenceRow:
        return getattr(self, name)


@dataclass(frozen=True)
class FullChargeSettings:
    """Periodic full charge for cell balancing (``docs/planning/periodic-full-charge.md``).

    Off by default.  ``enabled`` works in every service mode.  ``mode`` is the feature's own
    switch: ``shadow`` (default) decides and records with the simulated writer and never touches
    the real control path; ``live`` (service ``mode: live`` only) arms Omnibattery's weekly full
    charge, and only then do the two writable entities join the live allowlist.
    """

    enabled: bool = False
    mode: str = "shadow"
    cadence: FullChargeCadence = field(default_factory=FullChargeCadence)
    min_comparable_readings: int = 4
    alert_consecutive: int = 2
    settle_timeout_s: float = 300.0
    threshold_min_eur: float = 0.01
    threshold_scale_eur: float = 0.01
    fixed_cost_eur: float = 0.0
    target_soc_frac: float = 1.0
    reached_soc_pct: float = 99.0
    shortfall_penalty_eur_per_kwh: float = 10.0
    completion_margin_slots: int = 2
    arm_lead_slots: int = 1
    arm_before_soc_frac: float = 0.93
    ack_timeout_s: float = 90.0
    top_hold_timeout_min: float = 120.0
    taper: FullChargeTaper = field(default_factory=FullChargeTaper)
    max_consecutive_failures: int = 3
    failure_pause_days: int = 5
    bootstrap_last_full: date | None = None
    entities: FullChargeEntities = field(default_factory=FullChargeEntities)


@dataclass(frozen=True)
class ServiceConfig:
    """Fully resolved service configuration."""

    entities: EntityMap
    home_assistant: HomeAssistantSettings
    prometheus: PrometheusSettings
    planning: PlanningSettings
    observation: ObservationSettings
    #: :data:`AVAILABILITY_ONLY_ROLES` may be ``None``: availability only, no age test (see
    #: :meth:`freshness_for`).
    freshness_s: Mapping[str, float | None]
    archive: ArchiveSettings = field(default_factory=ArchiveSettings)
    climate_shadow: ClimateShadowSettings = field(default_factory=ClimateShadowSettings)
    actuation: ActuationSettings = field(default_factory=ActuationSettings)
    full_charge: FullChargeSettings = field(default_factory=FullChargeSettings)
    battery_health: BatteryHealthSettings = field(default_factory=BatteryHealthSettings)
    timezone: str = "Europe/Madrid"
    mode: str = "shadow"
    source_path: str | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def full_charge_writer_mode(self) -> str:
        """``live`` only for the live service with ``full_charge.mode: live``, else simulated."""
        return "live" if self.mode == "live" and self.full_charge.mode == "live" else "simulated"

    @property
    def full_charge_isolated(self) -> bool:
        """The feature simulates beside live control: no plan of record, no directive, no write."""
        return self.mode == "live" and self.full_charge.mode != "live"

    def freshness_for(self, role: str) -> float | None:
        """Maximum age for a role; power roles share ``battery_power``'s limit.

        ``None`` (the default of :data:`AVAILABILITY_ONLY_ROLES`, and only allowed for them) =
        availability only, no age test: Omnibattery reports the SOC and the AC energy counters
        only on change, so on an idle battery they keep an old ``last_reported`` while healthy,
        exactly as the actuator's ``freshness_s`` assumes.  A number restores the age test.
        """
        if role in self.freshness_s:
            value = self.freshness_s[role]
            return None if value is None else float(value)
        if role in AVAILABILITY_ONLY_ROLES:
            return None
        if role in ("battery_charge_power", "battery_discharge_power"):
            return float(self.freshness_s.get("battery_power", 300.0))
        return 900.0

    def public_dict(self) -> dict[str, Any]:
        """Configuration without secrets, for ``/status`` and hashing."""
        ha = asdict(self.home_assistant)
        ha.pop("token", None)
        ha["token_configured"] = bool(self.home_assistant.token)
        return {
            "mode": self.mode,
            "timezone": self.timezone,
            "entities": {
                "roles": dict(self.entities.roles),
                "observed": list(self.entities.observed),
            },
            "home_assistant": ha,
            "prometheus": asdict(self.prometheus),
            "planning": asdict(self.planning),
            "observation": asdict(self.observation),
            "archive": asdict(self.archive),
            "climate_shadow": {
                **asdict(self.climate_shadow),
                "go_live": self.climate_shadow.go_live.isoformat()
                if self.climate_shadow.go_live
                else None,
            },
            "freshness_s": dict(self.freshness_s),
            "actuation": asdict(self.actuation),
            "full_charge": {
                **asdict(self.full_charge),
                "bootstrap_last_full": self.full_charge.bootstrap_last_full.isoformat()
                if self.full_charge.bootstrap_last_full
                else None,
            },
            "battery_health": {
                **asdict(self.battery_health),
                "hot_thresholds_c": list(self.battery_health.hot_thresholds_c),
            },
            "source_path": self.source_path,
        }

    def config_hash(self) -> str:
        """SHA-256 of the planning-relevant configuration (URLs excluded)."""
        public = self.public_dict()
        public["home_assistant"].pop("url", None)
        public["prometheus"].pop("url", None)
        public.pop("source_path", None)
        public.pop("archive", None)  # passive collection never changes a plan
        public.pop("climate_shadow", None)  # nor does the climate shadow (it plans no battery)
        public.pop("actuation", None)  # nor does the way a plan is sent
        public.pop("full_charge", None)  # a committed plan carries its spec in its own inputs
        public.pop("battery_health", None)  # read-only telemetry never changes a plan
        public["planning"].pop("thermal", None)  # nor does the thermal shadow (observability only)
        if not self.planning.high_soc_cost_eur_per_kwh_h:
            # An inactive dwell cost leaves every plan alone: keep the hash from before it existed.
            public["planning"].pop("high_soc_threshold_frac", None)
            public["planning"].pop("high_soc_cost_eur_per_kwh_h", None)
        if not self.planning.charge_soft_cap_eur_per_kwh:
            # Likewise an inactive thermal soft cap.
            public["planning"].pop("charge_soft_cap_kw", None)
            public["planning"].pop("charge_soft_cap_eur_per_kwh", None)
        if self.planning.discharge_margin_bound:
            # The bound makes the actuator's margin a planning input (``actuation`` is otherwise
            # dropped above), so the value joins the hash.
            public["planning"]["discharge_margin_w"] = self.actuation.discharge_margin_w
        else:
            public["planning"].pop("discharge_margin_bound", None)
        text = json.dumps(public, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def resolve_service_config_path(cli_arg: str | None = None) -> Path:
    """``--service-config`` > ``BATTERY_OPTIMIZER_SERVICE_CONFIG`` > ``config/service.yaml``."""
    if cli_arg:
        return Path(cli_arg).expanduser().resolve()
    env = get_env("BATTERY_OPTIMIZER_SERVICE_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "config" / "service.yaml").resolve()


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, Mapping):
        raise ValueError(f"service config: {name} must be a mapping, got {type(value).__name__}")
    return value


def _positive(value: Any, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"service config: {name} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"service config: {name} must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"service config: {name} must be {'>=' if allow_zero else '>'} 0")
    return number


def _int(value: Any, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"service config: {name} must be an integer >= {minimum}, got {value!r}")
    return value


def _load_forecaster(value: Any) -> str:
    """A registered, non-oracle load forecaster name (``perfect`` is offline only)."""
    from battery_optimizer.load_forecast import forecaster_names, get_forecaster

    allowed = tuple(
        n for n in forecaster_names(include_oracle=False) if get_forecaster(n).side_loader is None
    )
    if not isinstance(value, str):
        raise ValueError(f"service config: planning.load_forecaster must be one of {allowed}")
    try:
        forecaster = get_forecaster(value)
    except ValueError:
        raise ValueError(
            f"service config: planning.load_forecaster={value!r} is not registered; "
            f"one of {allowed}"
        ) from None
    if forecaster.oracle:
        raise ValueError(
            f"service config: planning.load_forecaster={value!r} is an offline oracle (it "
            "reads measurements from after the decision) and can never plan live; one of "
            f"{allowed}"
        )
    if forecaster.side_loader is not None:
        raise ValueError(
            f"service config: planning.load_forecaster={value!r} needs offline side inputs "
            "(a sub-metered heat-pump feature table) that the service does not have yet; "
            "it is evaluated offline only"
        )
    return value


_WEATHER_ENTITY = re.compile(r"^weather\.[a-z0-9_]+$")


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"service config: {name} must be true or false, got {value!r}")
    return value


def _keys(raw: Mapping[str, Any], name: str, allowed: tuple[str, ...]) -> None:
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        raise ValueError(f"service config: unknown {name} keys {unknown}")


def _parse_archive(raw: Mapping[str, Any]) -> ArchiveSettings:
    from .ha_guard import ALLOWED_WEATHER_FORECAST_TYPES

    _keys(raw, "archive", ("pvpc_snapshots", "weather_forecast"))
    pvpc_raw = _section(raw, "pvpc_snapshots")
    _keys(pvpc_raw, "archive.pvpc_snapshots", ("enabled",))
    weather_raw = _section(raw, "weather_forecast")
    _keys(
        weather_raw,
        "archive.weather_forecast",
        ("enabled", "entity_id", "type", "interval_minutes", "offset_minutes"),
    )
    entity_id = weather_raw.get("entity_id", "weather.forecast_home")
    if not isinstance(entity_id, str) or not _WEATHER_ENTITY.match(entity_id):
        raise ValueError(
            f"service config: archive.weather_forecast.entity_id must be a weather.* entity, "
            f"got {entity_id!r}"
        )
    forecast_type = weather_raw.get("type", "hourly")
    if forecast_type not in ALLOWED_WEATHER_FORECAST_TYPES:
        raise ValueError(
            f"service config: archive.weather_forecast.type must be one of "
            f"{sorted(ALLOWED_WEATHER_FORECAST_TYPES)}"
        )
    interval = _int(
        weather_raw.get("interval_minutes", 60), "archive.weather_forecast.interval_minutes"
    )
    if 1440 % interval:
        raise ValueError(
            "service config: archive.weather_forecast.interval_minutes must divide 1440 "
            "(a UTC day) so fetches stay on a fixed grid"
        )
    offset = _int(
        weather_raw.get("offset_minutes", 5), "archive.weather_forecast.offset_minutes", 0
    )
    if offset >= interval:
        raise ValueError(
            "service config: archive.weather_forecast.offset_minutes must be below interval_minutes"
        )
    return ArchiveSettings(
        pvpc_snapshots=PvpcSnapshotSettings(
            enabled=_bool(pvpc_raw.get("enabled", False), "archive.pvpc_snapshots.enabled")
        ),
        weather_forecast=WeatherForecastSettings(
            enabled=_bool(weather_raw.get("enabled", False), "archive.weather_forecast.enabled"),
            entity_id=entity_id,
            type=forecast_type,
            interval_minutes=interval,
            offset_minutes=offset,
        ),
    )


_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_CLIMATE_SHADOW_KEYS = (
    "enabled",
    "climate_config",
    "mpc_config",
    "prior",
    "offset_minutes",
    "forecast_max_age_minutes",
    "score_lookback_days",
    "go_live",
    "entities",
    "optimizer_mode",
    "plan_ttl_minutes",
)


def _parse_climate_shadow(raw: Mapping[str, Any]) -> ClimateShadowSettings:
    _keys(raw, "climate_shadow", _CLIMATE_SHADOW_KEYS)
    name = "climate_shadow"
    ents = _section(raw, "entities")
    _keys(ents, f"{name}.entities", ("regime", "dhw_plan", "away", "mode"))
    entities: dict[str, str | None] = {}
    defaults = ClimateShadowSettings()
    for key, default in (
        ("regime", defaults.regime_entity),
        ("dhw_plan", defaults.dhw_plan_entity),
        ("away", defaults.away_entity),
        ("mode", defaults.mode_entity),
    ):
        value = ents.get(key, default)
        if value is not None and (not isinstance(value, str) or not _ENTITY_ID.match(value)):
            raise ValueError(f"service config: {name}.entities.{key} must be an entity ID or null")
        entities[key] = value
    go_live = raw.get("go_live")
    if go_live is not None:
        if isinstance(go_live, str):
            try:
                go_live = date.fromisoformat(go_live)
            except ValueError:
                go_live = None
        if not isinstance(go_live, date):
            raise ValueError(f"service config: {name}.go_live must be a local date or null")
    prior = raw.get("prior")
    if prior is not None and (
        not isinstance(prior, str) or not re.fullmatch(r"[0-9a-f]{6,}", prior)
    ):
        raise ValueError(f"service config: {name}.prior must be a prior id (hex) or null")
    enabled = _bool(raw.get("enabled", False), f"{name}.enabled")
    if enabled and prior is None:
        raise ValueError(f"service config: {name}.enabled needs {name}.prior")
    paths = {}
    for key in ("climate_config", "mpc_config"):
        value = raw.get(key, getattr(defaults, key))
        if not isinstance(value, str) or not value:
            raise ValueError(f"service config: {name}.{key} must be a path")
        paths[key] = value
    offset = _int(raw.get("offset_minutes", 10), f"{name}.offset_minutes", 0)
    if offset >= 60:
        raise ValueError(f"service config: {name}.offset_minutes must be below 60")
    mode = raw.get("optimizer_mode", defaults.optimizer_mode)
    if mode is False:  # an unquoted YAML `off`
        mode = "off"
    if mode not in ("off", "shadow", "live"):
        raise ValueError(
            f'service config: {name}.optimizer_mode must be "off", "shadow" or "live" '
            "(quote it: a bare YAML off is a boolean)"
        )
    ttl = _int(
        raw.get("plan_ttl_minutes", defaults.plan_ttl_minutes), f"{name}.plan_ttl_minutes", 1
    )
    if ttl > 360:
        raise ValueError(f"service config: {name}.plan_ttl_minutes must be at most 360")
    return ClimateShadowSettings(
        enabled=enabled,
        climate_config=paths["climate_config"],
        mpc_config=paths["mpc_config"],
        prior=prior,
        offset_minutes=offset,
        forecast_max_age_minutes=_int(
            raw.get("forecast_max_age_minutes", 180), f"{name}.forecast_max_age_minutes"
        ),
        score_lookback_days=_int(raw.get("score_lookback_days", 7), f"{name}.score_lookback_days"),
        go_live=go_live,
        regime_entity=entities["regime"],
        dhw_plan_entity=entities["dhw_plan"],
        away_entity=entities["away"],
        mode_entity=entities["mode"],
        optimizer_mode=mode,
        plan_ttl_minutes=ttl,
    )


_ACTUATION_KEYS = (
    "entities",
    "tick_interval_s",
    "import_cap_w",
    "import_guard_margin_w",
    "import_trip_hold_s",
    "neutral_pd_target_w",
    "discharge_margin_w",
    "min_discharge_cap_w",
    "max_discharge_cap_w",
    "battery_ac_power_sign",
    "healthy_states",
    "command_policy",
    "readback_timeout_s",
    "shutdown_timeout_s",
    "freshness_s",
    "load_following",
    "soc_floor_recovery_w",
    "soc_floor_hysteresis_pct",
)
#: Widest ``actuation.load_following.soc_hysteresis_pct`` (SOC points).
MAX_LOAD_FOLLOWING_HYSTERESIS_PCT = 10.0
#: Widest ``actuation.soc_floor_hysteresis_pct`` (SOC points): reserve-protection release band.
MAX_SOC_FLOOR_HYSTERESIS_PCT = 10.0


def _parse_actuation(raw: Mapping[str, Any], mode: str) -> ActuationSettings:
    """Validate ``actuation:``; outside ``shadow`` every entity and the sign are required."""
    name = "actuation"
    _keys(raw, name, _ACTUATION_KEYS)
    defaults = ActuationSettings()
    ents_raw = _section(raw, "entities")
    _keys(ents_raw, f"{name}.entities", tuple(ACTUATION_DOMAINS))
    ents: dict[str, str | None] = {}
    for key, domain in ACTUATION_DOMAINS.items():
        value = ents_raw.get(key, getattr(defaults.entities, key))
        if value is not None and (
            not isinstance(value, str)
            or not _ENTITY_ID.match(value)
            or not value.startswith(f"{domain}.")
        ):
            raise ValueError(
                f"service config: {name}.entities.{key} must be a {domain}.* entity ID or null"
            )
        if value is None and mode != "shadow" and key not in _OPTIONAL_ACTUATION_ENTITIES:
            raise ValueError(f"service config: mode={mode} needs {name}.entities.{key}")
        ents[key] = value
    named = [e for e in ents.values() if e]
    if len(named) != len(set(named)):
        raise ValueError(f"service config: {name}.entities must name distinct entities")

    policy_raw = _section(raw, "command_policy")
    _keys(
        policy_raw,
        f"{name}.command_policy",
        ("max_attempts", "min_setpoint_change_w", "min_command_interval_s"),
    )
    fresh_raw = _section(raw, "freshness_s")
    _keys(fresh_raw, f"{name}.freshness_s", ("grid_power", "battery_soc", "battery_ac_power"))

    def num(
        key: str,
        *,
        allow_zero: bool = False,
        section: Mapping[str, Any] = raw,
        prefix: str = "",
        attr: str = "",
    ) -> float:
        default = getattr(defaults, attr or key)
        label = f"{name}.{prefix}{key}"
        return _positive(section.get(key, default), label, allow_zero=allow_zero)

    def optional_age(key: str, attr: str) -> float | None:
        """A max age in seconds, or ``null`` for an availability-only check."""
        value = fresh_raw.get(key, getattr(defaults, attr))
        if value is None:
            return None
        return _positive(value, f"{name}.freshness_s.{key}")

    sign = raw.get("battery_ac_power_sign")
    if sign is not None and (isinstance(sign, bool) or sign not in (1, -1)):
        raise ValueError(f"service config: {name}.battery_ac_power_sign must be 1, -1 or null")
    if sign is None and mode != "shadow":
        raise ValueError(
            f"service config: mode={mode} needs {name}.battery_ac_power_sign (+1 when the AC "
            "power sensor is positive while charging, -1 when positive while discharging); "
            "measure it before leaving shadow mode"
        )
    healthy = raw.get("healthy_states", list(defaults.healthy_states))
    if (
        not isinstance(healthy, list)
        or not healthy
        or not all(isinstance(h, str) and h for h in healthy)
    ):
        raise ValueError(f"service config: {name}.healthy_states must be a non-empty list")
    if "weekly_full_charge" in healthy:
        raise ValueError(
            f"service config: {name}.healthy_states must not list weekly_full_charge: the "
            "actuator accepts it only while a full-charge run is armed for today"
        )
    settings = ActuationSettings(
        entities=ActuationEntities(**ents),
        tick_interval_s=num("tick_interval_s"),
        import_cap_w=num("import_cap_w"),
        import_guard_margin_w=num("import_guard_margin_w", allow_zero=True),
        import_trip_hold_s=num("import_trip_hold_s", allow_zero=True),
        neutral_pd_target_w=num("neutral_pd_target_w", allow_zero=True),
        discharge_margin_w=num("discharge_margin_w", allow_zero=True),
        min_discharge_cap_w=num("min_discharge_cap_w"),
        max_discharge_cap_w=num("max_discharge_cap_w"),
        battery_ac_power_sign=sign,
        healthy_states=tuple(healthy),
        max_attempts=_int(
            policy_raw.get("max_attempts", defaults.max_attempts),
            f"{name}.command_policy.max_attempts",
        ),
        readback_timeout_s=num("readback_timeout_s"),
        min_setpoint_change_w=num(
            "min_setpoint_change_w", allow_zero=True, section=policy_raw, prefix="command_policy."
        ),
        min_command_interval_s=num(
            "min_command_interval_s", allow_zero=True, section=policy_raw, prefix="command_policy."
        ),
        shutdown_timeout_s=num("shutdown_timeout_s"),
        grid_power_max_age_s=num(
            "grid_power", section=fresh_raw, prefix="freshness_s.", attr="grid_power_max_age_s"
        ),
        battery_soc_max_age_s=optional_age("battery_soc", "battery_soc_max_age_s"),
        battery_ac_power_max_age_s=optional_age("battery_ac_power", "battery_ac_power_max_age_s"),
        load_following=_parse_load_following(_section(raw, "load_following"), f"{name}."),
        soc_floor_recovery_w=num("soc_floor_recovery_w"),
        soc_floor_hysteresis_pct=_parse_soc_floor_hysteresis(raw, name, defaults),
    )
    cap = settings.import_cap_w
    for key in ("neutral_pd_target_w", "discharge_margin_w"):
        if getattr(settings, key) > cap:
            raise ValueError(f"service config: {name}.{key} must not exceed import_cap_w")
    if settings.discharge_margin_w <= 0:
        raise ValueError(
            f"service config: {name}.discharge_margin_w must be > 0 (a positive import while "
            "discharging, never export)"
        )
    if cap + settings.import_guard_margin_w >= CONTRACTED_IMPORT_W:
        raise ValueError(
            f"service config: {name}.import_cap_w + import_guard_margin_w must stay below the "
            f"{CONTRACTED_IMPORT_W:.0f} W contract (the meter trips instantly)"
        )
    if settings.tick_interval_s > MAX_TICK_INTERVAL_S:
        raise ValueError(
            f"service config: {name}.tick_interval_s must be <= {MAX_TICK_INTERVAL_S:.0f} s, well "
            "inside the 5-minute heartbeat watchdog"
        )
    if settings.max_discharge_cap_w > BATTERY_RATED_POWER_W:
        raise ValueError(
            f"service config: {name}.max_discharge_cap_w must not exceed the battery's "
            f"{BATTERY_RATED_POWER_W:.0f} W"
        )
    if settings.min_discharge_cap_w > settings.max_discharge_cap_w:
        raise ValueError(
            f"service config: {name}.min_discharge_cap_w must not exceed max_discharge_cap_w"
        )
    max_w = settings.load_following.max_w
    if max_w is not None and not (
        settings.min_discharge_cap_w <= max_w <= settings.max_discharge_cap_w
    ):
        # The guard allowlists the cap only inside [min, max]_discharge_cap_w: never widened here.
        raise ValueError(
            f"service config: {name}.load_following.max_w must be within "
            "[min_discharge_cap_w, max_discharge_cap_w]"
        )
    if settings.soc_floor_recovery_w > settings.import_cap_w:
        raise ValueError(
            f"service config: {name}.soc_floor_recovery_w must not exceed import_cap_w "
            f"(the recovery charge shares the PD target's import bound)"
        )
    return settings


def _parse_load_following(raw: Mapping[str, Any], prefix: str) -> LoadFollowingSettings:
    name = f"{prefix}load_following"
    _keys(raw, name, ("enabled", "max_w", "soc_hysteresis_pct"))
    defaults = LoadFollowingSettings()
    max_w = raw.get("max_w", defaults.max_w)
    hysteresis = _positive(
        raw.get("soc_hysteresis_pct", defaults.soc_hysteresis_pct), f"{name}.soc_hysteresis_pct"
    )
    if hysteresis > MAX_LOAD_FOLLOWING_HYSTERESIS_PCT:
        raise ValueError(
            f"service config: {name}.soc_hysteresis_pct must be <= "
            f"{MAX_LOAD_FOLLOWING_HYSTERESIS_PCT:g}"
        )
    return LoadFollowingSettings(
        enabled=_bool(raw.get("enabled", defaults.enabled), f"{name}.enabled"),
        max_w=None if max_w is None else _positive(max_w, f"{name}.max_w"),
        soc_hysteresis_pct=hysteresis,
    )


def _parse_soc_floor_hysteresis(
    raw: Mapping[str, Any], name: str, defaults: ActuationSettings
) -> float:
    """Validate ``actuation.soc_floor_hysteresis_pct`` (reserve protection)."""
    value = raw.get("soc_floor_hysteresis_pct", defaults.soc_floor_hysteresis_pct)
    hysteresis = _positive(value, f"{name}.soc_floor_hysteresis_pct")
    if hysteresis > MAX_SOC_FLOOR_HYSTERESIS_PCT:
        raise ValueError(
            f"service config: {name}.soc_floor_hysteresis_pct must be <= "
            f"{MAX_SOC_FLOOR_HYSTERESIS_PCT:g}"
        )
    return hysteresis


_FULL_CHARGE_KEYS = (
    "enabled",
    "mode",
    "cadence",
    "min_comparable_readings",
    "alert_consecutive",
    "settle_timeout_s",
    "threshold_min_eur",
    "threshold_scale_eur",
    "fixed_cost_eur",
    "target_soc_frac",
    "reached_soc_pct",
    "shortfall_penalty_eur_per_kwh",
    "completion_margin_slots",
    "arm_lead_slots",
    "arm_before_soc_frac",
    "ack_timeout_s",
    "top_hold_timeout_min",
    "taper",
    "max_consecutive_failures",
    "failure_pause_days",
    "bootstrap_last_full",
    "entities",
)


_CADENCE_CLASSES = ("unknown", "green", "yellow", "alert")


def _parse_cadence(raw: Mapping[str, Any], name: str) -> FullChargeCadence:
    """Validate the cadence table: per row ``0 < min < max <= 30``, healthier is never sooner."""
    _keys(raw, name, _CADENCE_CLASSES)
    defaults = FullChargeCadence()
    rows: dict[str, CadenceRow] = {}
    for key in _CADENCE_CLASSES:
        row_raw = raw.get(key)
        if row_raw is None:
            rows[key] = defaults.row(key)
            continue
        if not isinstance(row_raw, Mapping):
            raise ValueError(f"service config: {name}.{key} must be a mapping")
        _keys(row_raw, f"{name}.{key}", ("min_days", "max_days"))
        low = _int(row_raw.get("min_days", defaults.row(key).min_days), f"{name}.{key}.min_days")
        high = _int(row_raw.get("max_days", defaults.row(key).max_days), f"{name}.{key}.max_days")
        if not low < high <= 30:
            raise ValueError(f"service config: {name}.{key} needs 0 < min_days < max_days <= 30")
        rows[key] = CadenceRow(low, high)
    mins = [rows[k].min_days for k in ("green", "unknown", "yellow", "alert")]
    if mins != sorted(mins, reverse=True):
        raise ValueError(
            f"service config: {name} needs green.min_days >= unknown.min_days >= "
            "yellow.min_days >= alert.min_days (a healthier battery is never measured more often)"
        )
    return FullChargeCadence(**rows)


def _parse_full_charge(
    raw: Mapping[str, Any],
    *,
    actuation: ActuationSettings,
    entities: EntityMap,
    soc_source: str,
    battery_health: BatteryHealthSettings,
    service_mode: str,
) -> FullChargeSettings:
    """Validate ``full_charge:`` (whole schema, ``docs/planning/periodic-full-charge.md``)."""
    name = "full_charge"
    _keys(raw, name, _FULL_CHARGE_KEYS)
    defaults = FullChargeSettings()
    enabled = _bool(raw.get("enabled", defaults.enabled), f"{name}.enabled")
    feature_mode = raw.get("mode", defaults.mode)
    if feature_mode not in FULL_CHARGE_MODES:
        raise ValueError(f"service config: {name}.mode must be one of {FULL_CHARGE_MODES}")
    if feature_mode == "live" and service_mode != "live":
        raise ValueError(
            f"service config: {name}.mode=live needs the service mode=live (the weekly switch "
            "is never written by a shadow or dry_run service)"
        )

    def num(key: str, *, allow_zero: bool = False) -> float:
        value = raw.get(key, getattr(defaults, key))
        return _positive(value, f"{name}.{key}", allow_zero=allow_zero)

    def count(key: str, minimum: int = 1) -> int:
        return _int(raw.get(key, getattr(defaults, key)), f"{name}.{key}", minimum)

    cadence = _parse_cadence(_section(raw, "cadence"), f"{name}.cadence")
    comparable = count("min_comparable_readings")
    alert_consecutive = count("alert_consecutive", 2)
    settle_timeout = num("settle_timeout_s")
    if settle_timeout > 900:
        raise ValueError(f"service config: {name}.settle_timeout_s must be <= 900")
    target = num("target_soc_frac")
    arm_before = num("arm_before_soc_frac")
    if not arm_before < target <= 1:
        raise ValueError(
            f"service config: {name} needs 0 < arm_before_soc_frac < target_soc_frac <= 1"
        )
    reached = num("reached_soc_pct")
    if reached > 100:
        raise ValueError(f"service config: {name}.reached_soc_pct must be <= 100")

    taper_raw = _section(raw, "taper")
    _keys(taper_raw, f"{name}.taper", ("start_soc_frac", "power_kw"))
    start = taper_raw.get("start_soc_frac", defaults.taper.start_soc_frac)
    if start is not None:
        start = _positive(start, f"{name}.taper.start_soc_frac")
        if not 0.5 < start < 1:
            raise ValueError(f"service config: {name}.taper.start_soc_frac must be in (0.5, 1)")
    power = _positive(taper_raw.get("power_kw", defaults.taper.power_kw), f"{name}.taper.power_kw")
    if power > actuation.max_discharge_cap_w / 1000.0:
        raise ValueError(
            f"service config: {name}.taper.power_kw must not exceed "
            "actuation.max_discharge_cap_w / 1000"
        )

    bootstrap = raw.get("bootstrap_last_full")
    if isinstance(bootstrap, str):
        try:
            bootstrap = date.fromisoformat(bootstrap)
        except ValueError:
            pass  # still a str: refused below
    is_date = isinstance(bootstrap, date) and not isinstance(bootstrap, datetime)
    if bootstrap is not None and not is_date:
        raise ValueError(f"service config: {name}.bootstrap_last_full must be a local date or null")

    ents_raw = _section(raw, "entities")
    _keys(ents_raw, f"{name}.entities", tuple(FULL_CHARGE_DOMAINS))
    ents: dict[str, str | None] = {}
    for key, domain in FULL_CHARGE_DOMAINS.items():
        value = ents_raw.get(key)
        if value is not None and (
            not isinstance(value, str)
            or not _ENTITY_ID.match(value)
            or not value.startswith(f"{domain}.")
        ):
            raise ValueError(
                f"service config: {name}.entities.{key} must be a {domain}.* entity ID or null"
            )
        if value is None and enabled:
            raise ValueError(f"service config: {name}.enabled needs {name}.entities.{key}")
        ents[key] = value
    named = [e for e in ents.values() if e]
    taken = {*actuation.entities.all(), *entities.all_entities()}
    if len(named) != len(set(named)) or taken & set(named):
        raise ValueError(
            f"service config: {name}.entities must name distinct entities, none of them used "
            "by entities or actuation.entities"
        )
    # The balance sensors are shared with the read-only telemetry (one store entry each): the two
    # sections must name the same entity for a role, never two different ones.
    for key in BATTERY_HEALTH_DOMAINS:
        mine, theirs = ents.get(key), getattr(battery_health.entities, key)
        if mine and theirs and mine != theirs:
            raise ValueError(
                f"service config: {name}.entities.{key} and battery_health.entities.{key} must "
                "name the same entity"
            )
    if enabled:
        if soc_source != "entity":
            raise ValueError(f"service config: {name}.enabled needs planning.soc_source=entity")
        for key in ("integration_status", "battery_soc"):
            if not getattr(actuation.entities, key):
                raise ValueError(f"service config: {name}.enabled needs actuation.entities.{key}")
    return FullChargeSettings(
        enabled=enabled,
        mode=feature_mode,
        cadence=cadence,
        min_comparable_readings=comparable,
        alert_consecutive=alert_consecutive,
        settle_timeout_s=settle_timeout,
        threshold_min_eur=num("threshold_min_eur", allow_zero=True),
        threshold_scale_eur=num("threshold_scale_eur", allow_zero=True),
        fixed_cost_eur=num("fixed_cost_eur", allow_zero=True),
        target_soc_frac=target,
        reached_soc_pct=reached,
        shortfall_penalty_eur_per_kwh=num("shortfall_penalty_eur_per_kwh"),
        completion_margin_slots=count("completion_margin_slots"),
        arm_lead_slots=count("arm_lead_slots", 0),
        arm_before_soc_frac=arm_before,
        ack_timeout_s=num("ack_timeout_s"),
        top_hold_timeout_min=num("top_hold_timeout_min"),
        taper=FullChargeTaper(start_soc_frac=start, power_kw=power),
        max_consecutive_failures=count("max_consecutive_failures"),
        failure_pause_days=count("failure_pause_days"),
        bootstrap_last_full=bootstrap,
        entities=FullChargeEntities(**ents),
    )


_BATTERY_HEALTH_KEYS = (
    "entities",
    "hot_thresholds_c",
    "charge_threshold_w",
    "session_end_hold_s",
    "max_gap_s",
)


def _parse_battery_health(raw: Mapping[str, Any]) -> BatteryHealthSettings:
    """Validate ``battery_health:`` (all optional, read only)."""
    name = "battery_health"
    _keys(raw, name, _BATTERY_HEALTH_KEYS)
    defaults = BatteryHealthSettings()
    ents_raw = _section(raw, "entities")
    _keys(ents_raw, f"{name}.entities", tuple(BATTERY_HEALTH_DOMAINS))
    ents: dict[str, str | None] = {}
    for key, domain in BATTERY_HEALTH_DOMAINS.items():
        value = ents_raw.get(key)
        if value is not None and (
            not isinstance(value, str)
            or not _ENTITY_ID.match(value)
            or not value.startswith(f"{domain}.")
        ):
            raise ValueError(
                f"service config: {name}.entities.{key} must be a {domain}.* entity ID or null"
            )
        ents[key] = value
    named = [e for e in ents.values() if e]
    if len(named) != len(set(named)):
        raise ValueError(f"service config: {name}.entities must name distinct entities")

    thresholds_raw = raw.get("hot_thresholds_c", list(defaults.hot_thresholds_c))
    if (
        not isinstance(thresholds_raw, list)
        or not thresholds_raw
        or any(isinstance(t, bool) or not isinstance(t, (int, float)) for t in thresholds_raw)
    ):
        raise ValueError(f"service config: {name}.hot_thresholds_c must be a list of numbers")
    thresholds = tuple(float(t) for t in thresholds_raw)
    if not all(math.isfinite(t) and 0 < t < 100 for t in thresholds) or list(thresholds) != sorted(
        set(thresholds)
    ):
        raise ValueError(
            f"service config: {name}.hot_thresholds_c must be strictly increasing, in (0, 100)"
        )
    return BatteryHealthSettings(
        entities=BatteryHealthEntities(**ents),
        hot_thresholds_c=thresholds,
        charge_threshold_w=_positive(
            raw.get("charge_threshold_w", defaults.charge_threshold_w),
            f"{name}.charge_threshold_w",
        ),
        session_end_hold_s=_positive(
            raw.get("session_end_hold_s", defaults.session_end_hold_s),
            f"{name}.session_end_hold_s",
            allow_zero=True,
        ),
        max_gap_s=_positive(raw.get("max_gap_s", defaults.max_gap_s), f"{name}.max_gap_s"),
    )


_THERMAL_KEYS = (
    "mode",
    "entities",
    "charge_limit_kw",
    "driver_min_kw",
    "prior",
    "refit_hour_local",
)


def _parse_thermal(
    raw: Mapping[str, Any],
    *,
    entities: EntityMap,
    actuation: ActuationSettings,
    battery_health: BatteryHealthSettings,
    full_charge: FullChargeSettings,
) -> ThermalSettings:
    """Validate ``planning.thermal:`` (read only; ``mode: off`` accepts and ignores the rest)."""
    from .thermal import BOUNDS

    name = "planning.thermal"
    _keys(raw, name, _THERMAL_KEYS)
    defaults = ThermalSettings()
    mode = raw.get("mode", defaults.mode)
    if mode is False:  # YAML 1.1 reads an unquoted ``off`` as a boolean
        mode = "off"
    if mode not in THERMAL_MODES:
        raise ValueError(f"service config: {name}.mode must be one of {THERMAL_MODES}")

    ents_raw = _section(raw, "entities")
    _keys(ents_raw, f"{name}.entities", tuple(THERMAL_DOMAINS))
    ents: dict[str, str | None] = {}
    for key, domain in THERMAL_DOMAINS.items():
        value = ents_raw.get(key)
        if value is not None and (
            not isinstance(value, str)
            or not _ENTITY_ID.match(value)
            or not value.startswith(f"{domain}.")
        ):
            raise ValueError(
                f"service config: {name}.entities.{key} must be a {domain}.* entity ID or null"
            )
        ents[key] = value
    # The temperature sensor is battery_health's (one entity store entry): the two sections agree.
    health_temperature = battery_health.entities.temperature
    if (
        mode == "shadow"
        and ents["temperature"]
        and health_temperature
        and ents["temperature"] != health_temperature
    ):
        raise ValueError(
            f"service config: {name}.entities.temperature and battery_health.entities.temperature "
            "must name the same entity"
        )
    ents["temperature"] = ents["temperature"] or health_temperature
    named = [e for e in ents.values() if e]
    if len(named) != len(set(named)):
        raise ValueError(f"service config: {name}.entities must name distinct entities")
    # Only the temperature may be shared (with battery_health); the rest is thermal's alone.
    others = [e for k, e in ents.items() if e and k != "temperature"]
    # ``entities.observed`` is read only context and may name the same (one store entry).
    taken = {
        *actuation.entities.all(),
        *entities.configured().values(),
        *full_charge.entities.all(),
    }
    if (taken & set(others)) or (ents["temperature"] in taken):
        raise ValueError(
            f"service config: {name}.entities must not reuse entities of entities (roles), "
            "actuation.entities or full_charge.entities"
        )

    prior_raw = _section(raw, "prior")
    _keys(prior_raw, f"{name}.prior", tuple(BOUNDS))
    prior_values: dict[str, float] = {}
    for key, (lo, hi) in BOUNDS.items():
        value = _positive(prior_raw.get(key, getattr(defaults.prior, key)), f"{name}.prior.{key}")
        if not lo <= value <= hi:
            raise ValueError(f"service config: {name}.prior.{key} must be within {lo} and {hi}")
        prior_values[key] = value
    hour = raw.get("refit_hour_local", defaults.refit_hour_local)
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError(f"service config: {name}.refit_hour_local must be an hour 0 to 23")
    settings = ThermalSettings(
        mode=mode,
        entities=ThermalEntities(**ents),
        charge_limit_kw=_positive(
            raw.get("charge_limit_kw", defaults.charge_limit_kw), f"{name}.charge_limit_kw"
        ),
        driver_min_kw=_positive(
            raw.get("driver_min_kw", defaults.driver_min_kw), f"{name}.driver_min_kw"
        ),
        prior=ThermalPrior(**prior_values),
        refit_hour_local=hour,
    )
    if settings.active:
        missing = [k for k in THERMAL_REQUIRED if not ents[k]]
        if missing:
            raise ValueError(
                f"service config: {name}.mode={mode} needs {name}.entities.{missing[0]} "
                f"(missing: {', '.join(missing)})"
            )
        if not actuation.entities.battery_ac_power:
            raise ValueError(
                f"service config: {name}.mode={mode} needs actuation.entities.battery_ac_power"
            )
        if actuation.battery_ac_power_sign is None:
            raise ValueError(
                f"service config: {name}.mode={mode} needs actuation.battery_ac_power_sign "
                "(+1 or -1, measured): the refit reads the signed battery power"
            )
    return settings


def _read_token() -> Secret | None:
    token = get_env("BATTERY_OPTIMIZER_HA_TOKEN")
    if token:
        return Secret(token.strip())
    token_file = get_env("BATTERY_OPTIMIZER_HA_TOKEN_FILE")
    if token_file:
        text = Path(token_file).expanduser().read_text().strip()
        return Secret(text) if text else None
    return None


def _parse_entities(raw: Mapping[str, Any]) -> EntityMap:
    unknown = sorted(k for k in raw if k not in (*ENTITY_ROLES, "observed"))
    if unknown:
        raise ValueError(f"service config: unknown entity roles {unknown}")
    roles: dict[str, str | None] = {}
    for role in ENTITY_ROLES:
        value = raw.get(role)
        if value is not None and (not isinstance(value, str) or "." not in value):
            raise ValueError(f"service config: entities.{role} must be an entity ID or null")
        roles[role] = value
    for required in ("grid_power", "price"):
        if not roles[required]:
            raise ValueError(f"service config: entities.{required} is required")
    observed_raw = raw.get("observed") or []
    if not isinstance(observed_raw, list) or not all(isinstance(e, str) for e in observed_raw):
        raise ValueError("service config: entities.observed must be a list of entity IDs")
    if roles["battery_power"] and (
        roles["battery_charge_power"] or roles["battery_discharge_power"]
    ):
        raise ValueError(
            "service config: map either battery_power (signed) or the "
            "battery_charge_power/battery_discharge_power pair, not both"
        )
    if bool(roles["battery_charge_power"]) != bool(roles["battery_discharge_power"]):
        raise ValueError(
            "service config: battery_charge_power and battery_discharge_power go together"
        )
    return EntityMap(roles=roles, observed=tuple(observed_raw))


def load_service_config(path: Path | None = None) -> ServiceConfig:
    """Load the YAML at *path* (or the resolved default) and apply env overrides."""
    path = path or resolve_service_config_path()
    if not path.is_file():
        raise ValueError(
            f"service config not found: {path} (set BATTERY_OPTIMIZER_SERVICE_CONFIG or run "
            "from a directory containing config/service.yaml)"
        )
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, Mapping):
        raise ValueError(f"service config must be a mapping, got {type(raw).__name__}")
    mode = raw.get("mode", "shadow")
    if mode not in SERVICE_MODES:
        raise ValueError(f"service config: mode={mode!r} is not one of {SERVICE_MODES}")
    timezone = str(raw.get("timezone", "Europe/Madrid"))
    ZoneInfo(timezone)  # validate early

    ha_raw = _section(raw, "home_assistant")
    reconnect = _section(ha_raw, "reconnect")
    heartbeat = _section(ha_raw, "heartbeat")
    ha = HomeAssistantSettings(
        url=(get_env("BATTERY_OPTIMIZER_HA_URL") or "").rstrip("/") or None,
        token=_read_token(),
        request_timeout_s=_positive(ha_raw.get("request_timeout_s", 15), "request_timeout_s"),
        reconnect_initial_delay_s=_positive(reconnect.get("initial_delay_s", 1), "initial_delay_s"),
        reconnect_max_delay_s=_positive(reconnect.get("max_delay_s", 60), "max_delay_s"),
        reconnect_jitter_frac=_positive(
            reconnect.get("jitter_frac", 0.2), "jitter_frac", allow_zero=True
        ),
        heartbeat_interval_s=_positive(heartbeat.get("interval_s", 30), "heartbeat.interval_s"),
        heartbeat_timeout_s=_positive(heartbeat.get("timeout_s", 10), "heartbeat.timeout_s"),
        poll_interval_s=_positive(ha_raw.get("poll_interval_s", 60), "poll_interval_s"),
    )
    prom_raw = _section(raw, "prometheus")
    prom = PrometheusSettings(
        url=(get_env("BATTERY_OPTIMIZER_PROMETHEUS_URL") or "").rstrip("/") or None,
        timeout_s=_positive(prom_raw.get("timeout_s", 60), "prometheus.timeout_s"),
        history_days=_int(prom_raw.get("history_days", 28), "prometheus.history_days"),
    )

    pl_raw = _section(raw, "planning")
    replan = _section(pl_raw, "replan")
    soc_source = pl_raw.get("soc_source", "simulated")
    if soc_source not in SOC_SOURCES:
        raise ValueError(f"service config: planning.soc_source must be one of {SOC_SOURCES}")
    terminal_value = pl_raw.get("terminal_value", "trailing_mean")
    if terminal_value != "trailing_mean":
        if isinstance(terminal_value, bool) or not isinstance(terminal_value, (int, float)):
            raise ValueError("service config: terminal_value must be 'trailing_mean' or a number")
        if not math.isfinite(float(terminal_value)):
            raise ValueError("service config: terminal_value must be finite")
        terminal_value = float(terminal_value)
    load_forecaster = _load_forecaster(pl_raw.get("load_forecaster", "trailing_mean"))
    interval_seconds = _int(pl_raw.get("interval_seconds", 900), "interval_seconds", 60)
    if 3600 % interval_seconds:
        raise ValueError("service config: interval_seconds must divide an hour")
    high_soc_threshold = _positive(
        pl_raw.get("high_soc_threshold_frac", 0.90), "high_soc_threshold_frac"
    )
    if high_soc_threshold >= 1.0:
        raise ValueError("service config: high_soc_threshold_frac must be below 1")
    planning = PlanningSettings(
        soc_source=soc_source,
        interval_seconds=interval_seconds,
        solve_lead_s=_positive(pl_raw.get("solve_lead_s", 120), "solve_lead_s"),
        solve_timeout_s=_positive(pl_raw.get("solve_timeout_s", 60), "solve_timeout_s"),
        solver_time_limit_s=_positive(pl_raw.get("solver_time_limit_s", 20), "solver_time_limit_s"),
        plan_ttl_slots=_int(pl_raw.get("plan_ttl_slots", 2), "plan_ttl_slots"),
        min_horizon_slots=_int(pl_raw.get("min_horizon_slots", 4), "min_horizon_slots"),
        max_horizon_slots=_int(pl_raw.get("max_horizon_slots", 192), "max_horizon_slots"),
        forecast_window_days=_int(pl_raw.get("forecast_window_days", 28), "forecast_window_days"),
        load_forecaster=load_forecaster,
        terminal_value=terminal_value,
        throughput_cost_eur_per_kwh=_positive(
            pl_raw.get("throughput_cost_eur_per_kwh", 0.0), "throughput_cost", allow_zero=True
        ),
        high_soc_threshold_frac=high_soc_threshold,
        high_soc_cost_eur_per_kwh_h=_positive(
            pl_raw.get("high_soc_cost_eur_per_kwh_h", 0.0),
            "high_soc_cost_eur_per_kwh_h",
            allow_zero=True,
        ),
        charge_soft_cap_kw=_positive(pl_raw.get("charge_soft_cap_kw", 1.5), "charge_soft_cap_kw"),
        charge_soft_cap_eur_per_kwh=_positive(
            pl_raw.get("charge_soft_cap_eur_per_kwh", 0.0),
            "charge_soft_cap_eur_per_kwh",
            allow_zero=True,
        ),
        discharge_margin_bound=_bool(
            pl_raw.get("discharge_margin_bound", False), "planning.discharge_margin_bound"
        ),
        replan_soc_change_frac=_positive(
            replan.get("soc_change_frac", 0.05), "replan.soc_change_frac"
        ),
        replan_debounce_s=_positive(
            replan.get("debounce_s", 30), "replan.debounce_s", allow_zero=True
        ),
        replan_min_interval_s=_positive(
            replan.get("min_interval_s", 120), "replan.min_interval_s", allow_zero=True
        ),
    )
    if planning.solve_timeout_s >= planning.solve_lead_s:
        raise ValueError(
            "service config: solve_timeout_s must be below solve_lead_s, or a solve could "
            "finish after its first slot began"
        )
    if planning.solver_time_limit_s > planning.solve_timeout_s:
        raise ValueError("service config: solver_time_limit_s must not exceed solve_timeout_s")
    if planning.min_horizon_slots > planning.max_horizon_slots:
        raise ValueError("service config: min_horizon_slots must not exceed max_horizon_slots")

    entities = _parse_entities(_section(raw, "entities"))
    if soc_source == "entity" and not entities.get("battery_soc"):
        raise ValueError(
            "service config: planning.soc_source=entity needs entities.battery_soc; use "
            "soc_source=simulated until the battery is integrated in Home Assistant"
        )

    obs_raw = _section(raw, "observation")
    observation = ObservationSettings(
        slot_close_grace_s=_positive(obs_raw.get("slot_close_grace_s", 20), "slot_close_grace_s"),
    )
    archive = _parse_archive(_section(raw, "archive"))
    climate_shadow = _parse_climate_shadow(_section(raw, "climate_shadow"))
    actuation = _parse_actuation(_section(raw, "actuation"), mode)
    battery_health = _parse_battery_health(_section(raw, "battery_health"))
    full_charge = _parse_full_charge(
        _section(raw, "full_charge"),
        actuation=actuation,
        entities=entities,
        soc_source=soc_source,
        battery_health=battery_health,
        service_mode=mode,
    )
    dwell_per_kwh = (
        planning.high_soc_cost_eur_per_kwh_h * planning.max_horizon_slots * interval_seconds / 3600
    )
    if full_charge.enabled and full_charge.shortfall_penalty_eur_per_kwh <= dwell_per_kwh:
        raise ValueError(
            "service config: full_charge.shortfall_penalty_eur_per_kwh must exceed "
            "planning.high_soc_cost_eur_per_kwh_h times the longest horizon in hours "
            f"({dwell_per_kwh:.6g}), or the dwell cost could outweigh reaching the target"
        )
    # One more stored kWh costs at most 1/eta_c AC kWh above the cap; the config does not know the
    # efficiency, so it checks with 0.5, which no real battery is below (the solver checks exactly).
    soft_cap_per_kwh = dwell_per_kwh + planning.charge_soft_cap_eur_per_kwh / 0.5
    if full_charge.enabled and full_charge.shortfall_penalty_eur_per_kwh <= soft_cap_per_kwh:
        raise ValueError(
            "service config: full_charge.shortfall_penalty_eur_per_kwh must exceed the dwell "
            "cost over the longest horizon plus planning.charge_soft_cap_eur_per_kwh per stored "
            f"kWh ({soft_cap_per_kwh:.6g}), or the soft cap could outweigh reaching the target"
        )
    if mode == "live" and soc_source != "entity":
        raise ValueError(
            "service config: mode=live needs planning.soc_source=entity: a plan made from a "
            "simulated SOC must never drive the real battery"
        )
    planning = replace(
        planning,
        thermal=_parse_thermal(
            _section(pl_raw, "thermal"),
            entities=entities,
            actuation=actuation,
            battery_health=battery_health,
            full_charge=full_charge,
        ),
    )
    fresh_raw = _section(raw, "freshness_s")
    freshness: dict[str, float | None] = {
        str(k): None
        if k in AVAILABILITY_ONLY_ROLES and v is None
        else _positive(v, f"freshness_s.{k}")
        for k, v in fresh_raw.items()
    }
    return ServiceConfig(
        entities=entities,
        home_assistant=ha,
        prometheus=prom,
        planning=planning,
        observation=observation,
        archive=archive,
        climate_shadow=climate_shadow,
        actuation=actuation,
        full_charge=full_charge,
        battery_health=battery_health,
        freshness_s=freshness,
        timezone=timezone,
        mode=mode,
        source_path=str(path),
    )


def resolve_port(cli_arg: int | None = None) -> int:
    """``--port`` > ``BATTERY_OPTIMIZER_PORT`` > 8080."""
    if cli_arg is not None:
        return cli_arg
    env = get_env("BATTERY_OPTIMIZER_PORT")
    if env:
        port = int(env)
        if not 0 < port < 65536:
            raise ValueError(f"BATTERY_OPTIMIZER_PORT out of range: {port}")
        return port
    return DEFAULT_PORT
