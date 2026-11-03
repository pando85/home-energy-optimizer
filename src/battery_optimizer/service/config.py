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
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from battery_optimizer._env import get_env

__all__ = [
    "BATTERY_ROLES",
    "ENTITY_ROLES",
    "SERVICE_MODES",
    "SOC_SOURCES",
    "ActuationEntities",
    "ActuationSettings",
    "ArchiveSettings",
    "ClimateShadowSettings",
    "EntityMap",
    "HomeAssistantSettings",
    "ObservationSettings",
    "PlanningSettings",
    "PrometheusSettings",
    "PvpcSnapshotSettings",
    "Secret",
    "ServiceConfig",
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
    replan_soc_change_frac: float = 0.05
    replan_debounce_s: float = 30.0
    replan_min_interval_s: float = 120.0


@dataclass(frozen=True)
class ObservationSettings:
    slot_close_grace_s: float = 20.0
    counter_max_age_s: float = 300.0


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

    def entities(self) -> tuple[str, ...]:
        return tuple(e for e in (self.regime_entity, self.dhw_plan_entity, self.away_entity) if e)


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

#: Entities that may stay ``null`` even outside shadow mode.
_OPTIONAL_ACTUATION_ENTITIES = frozenset({"battery_liveness"})


@dataclass(frozen=True)
class ActuationEntities:
    """The Omnibattery control surface plus the owner's kill switch and heartbeat helper.

    Written (live mode only, through :mod:`.ha_guard`): the two direction switches, the PD
    grid target, the system power-limit switch, the system discharge cap and the heartbeat.
    Only read: the kill switch, the integration status, battery AC power and SOC, and the
    optional ``battery_liveness`` sensor (Omnibattery's non-responsive-batteries count, whose
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
    #: AC power keep their old ``last_reported``; they turn ``unavailable`` when Modbus fails.
    battery_soc_max_age_s: float | None = None
    battery_ac_power_max_age_s: float | None = None

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


@dataclass(frozen=True)
class ServiceConfig:
    """Fully resolved service configuration."""

    entities: EntityMap
    home_assistant: HomeAssistantSettings
    prometheus: PrometheusSettings
    planning: PlanningSettings
    observation: ObservationSettings
    freshness_s: Mapping[str, float]
    archive: ArchiveSettings = field(default_factory=ArchiveSettings)
    climate_shadow: ClimateShadowSettings = field(default_factory=ClimateShadowSettings)
    actuation: ActuationSettings = field(default_factory=ActuationSettings)
    timezone: str = "Europe/Madrid"
    mode: str = "shadow"
    source_path: str | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def freshness_for(self, role: str) -> float:
        """Maximum age for a role; power roles share ``battery_power``'s limit."""
        if role in self.freshness_s:
            return float(self.freshness_s[role])
        if role in ("battery_charge_power", "battery_discharge_power"):
            return float(self.freshness_s.get("battery_power", 300.0))
        if role in ("battery_charge_energy", "battery_discharge_energy"):
            return float(self.freshness_s.get("grid_energy", 900.0))
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
)


def _parse_climate_shadow(raw: Mapping[str, Any]) -> ClimateShadowSettings:
    _keys(raw, "climate_shadow", _CLIMATE_SHADOW_KEYS)
    name = "climate_shadow"
    ents = _section(raw, "entities")
    _keys(ents, f"{name}.entities", ("regime", "dhw_plan", "away"))
    entities: dict[str, str | None] = {}
    defaults = ClimateShadowSettings()
    for key, default in (
        ("regime", defaults.regime_entity),
        ("dhw_plan", defaults.dhw_plan_entity),
        ("away", defaults.away_entity),
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
)


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
    )
    cap = settings.import_cap_w
    for key in ("neutral_pd_target_w", "discharge_margin_w"):
        if getattr(settings, key) > cap:
            raise ValueError(f"service config: {name}.{key} must not exceed import_cap_w")
    if settings.min_discharge_cap_w > settings.max_discharge_cap_w:
        raise ValueError(
            f"service config: {name}.min_discharge_cap_w must not exceed max_discharge_cap_w"
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
        counter_max_age_s=_positive(obs_raw.get("counter_max_age_s", 300), "counter_max_age_s"),
    )
    archive = _parse_archive(_section(raw, "archive"))
    climate_shadow = _parse_climate_shadow(_section(raw, "climate_shadow"))
    actuation = _parse_actuation(_section(raw, "actuation"), mode)
    if mode == "live" and soc_source != "entity":
        raise ValueError(
            "service config: mode=live needs planning.soc_source=entity: a plan made from a "
            "simulated SOC must never drive the real battery"
        )
    fresh_raw = _section(raw, "freshness_s")
    freshness = {str(k): _positive(v, f"freshness_s.{k}") for k, v in fresh_raw.items()}
    return ServiceConfig(
        entities=entities,
        home_assistant=ha,
        prometheus=prom,
        planning=planning,
        observation=observation,
        archive=archive,
        climate_shadow=climate_shadow,
        actuation=actuation,
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
