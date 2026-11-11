"""Battery AC energy counters and the commissioning instant, for the offline pipeline.

The offline build must know two things about the house battery that the data alone cannot
tell it: which Home Assistant entities carry the AC charge and discharge energy counters, and
from which instant the battery may exchange energy (after which the whole-home feed no longer
equals the household load).  Both live in ``config/service.yaml``, next to the entity roles the
live service already uses, so the build and the service history read one set of names:

``entities.battery_charge_energy`` / ``entities.battery_discharge_energy``
    The counters.  The exact OmniBattery entity ids are unknown until installation, so the
    committed values are ``null`` placeholders; nothing here invents an id.
``battery.commissioned_at``
    ISO-8601 instant with an explicit UTC offset, ``null`` while no battery is installed.

The file is deliberately **not** ``config/tariff.yaml``: the hash of that file is part of every
``dataset_id``, so a date recorded there would re-mint every dataset (ADR-0006, "record dates
in ``docs/planning/``, never in ``config/tariff.yaml``").  The service config is not hashed into
a dataset, and a window that ends before ``commissioned_at`` ignores this configuration
altogether (see :func:`battery_optimizer.build.build_dataset`).

The module imports nothing from the build or the extractor, so both can use it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from battery_optimizer._env import get_env

__all__ = [
    "BATTERY_ENERGY_METRIC",
    "BATTERY_ENERGY_UNIT",
    "BatteryTelemetry",
    "load_battery_telemetry",
    "parse_commissioned_at",
]

BATTERY_ENERGY_METRIC = "hass_sensor_energy_kwh"
"""Prometheus metric of an HA energy sensor in kWh (what ``service/history.py`` reads)."""

BATTERY_ENERGY_UNIT = "kWh"

_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


@dataclass(frozen=True)
class BatteryTelemetry:
    """The configured battery counters and commissioning instant (all optional)."""

    charge_entity: str | None = None
    discharge_entity: str | None = None
    commissioned_at: datetime | None = None
    """Aware UTC instant from which the battery may exchange energy; ``None`` = not installed."""
    source: str | None = None

    @property
    def entities(self) -> tuple[str, ...]:
        """The configured counter entity ids (charge first), empty when none are configured."""
        return tuple(e for e in (self.charge_entity, self.discharge_entity) if e)

    @property
    def has_counters(self) -> bool:
        return bool(self.charge_entity and self.discharge_entity)


def parse_commissioned_at(value: object) -> datetime | None:
    """Normalise ``battery.commissioned_at`` to an aware UTC instant, or ``None``.

    Raises:
        ValueError: a naive timestamp, a bare date, or anything that is not an ISO-8601
            instant.  The instant decides which side of a possibly ambiguous DST hour an
            interval falls on, so a local time without an offset is refused.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        raise ValueError(
            f"battery.commissioned_at {value.isoformat()!r} is a bare date; give an instant "
            "with an explicit UTC offset, e.g. '2026-11-05T00:00:00+01:00'"
        )
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(
                f"battery.commissioned_at {value!r} is not an ISO-8601 instant: {exc}"
            ) from exc
    else:
        raise ValueError(f"battery.commissioned_at must be an ISO-8601 string, got {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"battery.commissioned_at {value!r} has no UTC offset; give an explicit one, "
            "e.g. '2026-11-05T00:00:00+01:00'"
        )
    return parsed.astimezone(UTC)


def _entity(raw: Mapping[str, Any], role: str) -> str | None:
    value = raw.get(role)
    if value is None:
        return None
    if not isinstance(value, str) or not _ENTITY_ID.match(value):
        raise ValueError(f"entities.{role} must be an entity id such as 'sensor.x' or null")
    return value


def load_battery_telemetry(
    path: str | Path | None = None, overrides_path: str | Path | None = None
) -> BatteryTelemetry:
    """Read the battery counters and commissioning instant from the service config.

    *path* defaults to the service config resolution (``BATTERY_OPTIMIZER_SERVICE_CONFIG``,
    else ``config/service.yaml`` under the working directory).  A missing **default** file
    means "nothing configured", the behaviour before this module existed; an explicitly
    given path, or one named by ``BATTERY_OPTIMIZER_SERVICE_CONFIG``, that does not exist is
    an error (a typo must not silently switch the commissioning guard off).

    The site overrides file (*overrides_path*, else ``BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES``)
    is layered over the file first, as ``load_service_config`` does, so the offline commands see
    the same effective values as the service.

    Raises:
        ValueError: an unreadable or malformed file, a counter given without its pair, an
            invalid entity id or a naive commissioning timestamp.
    """
    explicit = path is not None or bool(get_env("BATTERY_OPTIMIZER_SERVICE_CONFIG"))
    if path is None:
        from battery_optimizer.service.config import resolve_service_config_path

        resolved = resolve_service_config_path()
    else:
        resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        if explicit:
            raise ValueError(f"battery telemetry config not found: {resolved}")
        return BatteryTelemetry()
    try:
        raw = yaml.safe_load(resolved.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"battery telemetry config {resolved} is unreadable: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"battery telemetry config {resolved} must be a mapping")
    from battery_optimizer.service.config import (
        load_overrides_file,
        merge_service_overrides,
        resolve_overrides_path,
    )

    overrides = resolve_overrides_path(str(overrides_path) if overrides_path else None)
    if overrides is not None:
        raw, _applied = merge_service_overrides(
            raw, load_overrides_file(overrides), where=f"overrides {overrides}"
        )
    entities = raw.get("entities") or {}
    battery = raw.get("battery") or {}
    if not isinstance(entities, Mapping) or not isinstance(battery, Mapping):
        raise ValueError(f"{resolved}: 'entities' and 'battery' must be mappings")
    unknown = sorted(str(k) for k in battery if k != "commissioned_at")
    if unknown:
        raise ValueError(f"{resolved}: unknown battery keys {unknown}")
    charge = _entity(entities, "battery_charge_energy")
    discharge = _entity(entities, "battery_discharge_energy")
    if bool(charge) != bool(discharge):
        raise ValueError(
            "entities.battery_charge_energy and entities.battery_discharge_energy go together"
        )
    return BatteryTelemetry(
        charge_entity=charge,
        discharge_entity=discharge,
        commissioned_at=parse_commissioned_at(battery.get("commissioned_at")),
        source=str(resolved),
    )
