"""Strict loader of ``config/climate-mpc.yaml`` (the price-blind comfort MPC, track B1).

Same rules as :mod:`battery_optimizer.climate.config`: an unknown, missing or duplicated key, a
wrong type, a boolean where a number belongs, a non-finite number or an inconsistent combination
is an error naming the key.  The result is a read-only tree reachable by attribute and its SHA-256
identifies the file in every result.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from battery_optimizer.climate.config import (
    KNOWN_ROOMS,
    ClimateConfigError,
    Section,
    Spec,
    _StrictLoader,
    boolean,
    integer,
    num,
    section,
    seq,
    text,
    timezone,
)

__all__ = [
    "MPC_CONFIG_VERSION",
    "MpcConfig",
    "MpcConfigError",
    "load_mpc_config",
    "override",
]

MPC_CONFIG_VERSION = 1
MODEL_KINDS = ("b2d", "c2")
WEATHER_MODES = ("perfect", "persistence")
REESTIMABLE = ("ua", "q0", "g_s")


class MpcConfigError(ClimateConfigError):
    """The MPC configuration is malformed; the message names the offending key."""


def _fail(path: str, message: str) -> MpcConfigError:
    return MpcConfigError(f"climate-mpc config: {path}: {message}")


def _point(value: Any, path: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise _fail(path, f"expected [outdoor, water], got {value!r}")
    return num(-40.0, 40.0)(value[0], f"{path}[0]"), num(15.0, 60.0)(value[1], f"{path}[1]")


def _hex_prefix(value: Any, path: str) -> str:
    v = text()(value, path)
    if len(v) < 6 or any(c not in "0123456789abcdef" for c in v):
        raise _fail(path, f"expected a model id or a prefix of at least 6 hex digits, got {v!r}")
    return v


def _room_map(value: Spec) -> Spec:
    """A possibly empty ``room -> value`` mapping (an unknown room is an error)."""
    room = text(*KNOWN_ROOMS)

    def check(raw: Any, path: str) -> Section:
        if not isinstance(raw, dict):
            raise _fail(path, "expected a mapping of room to degC")
        return Section({room(k, f"{path}.{k}"): value(v, f"{path}.{k}") for k, v in raw.items()})

    return check


_COMFORT_REQUIRED: Spec = section(
    {
        "day_room": text(*KNOWN_ROOMS),
        "day_min_c": num(15.0, 28.0),
        "day_start_weekday_hour": integer(0, 23),
        "day_start_weekend_hour": integer(0, 23),
        "day_end_hour": integer(1, 24),
        "floor_c": num(10.0, 25.0),
        "penalty_kwh_per_kh": num(1e-6),
        "setpoint_margin_k": num(0.0, 3.0),
    }
)
#: Optional per-room schedule (the controller's ``comfort_floor_<room>`` and
#: ``comfort_day_minimum_<room>`` helpers).  Absent, a room has ``floor_c``, and ``day_min_c`` for
#: the day room (``floor_c`` for the others), exactly the single schedule of config version 1.
_COMFORT_OPTIONAL: dict[str, Spec] = {
    "floor_c_by_room": _room_map(num(10.0, 25.0)),
    "day_min_c_by_room": _room_map(num(15.0, 28.0)),
}


def _comfort(raw: Any, path: str) -> Section:
    if not isinstance(raw, dict):
        raise _fail(path, "expected a mapping")
    base = _COMFORT_REQUIRED({k: v for k, v in raw.items() if k not in _COMFORT_OPTIONAL}, path)
    optional = {
        k: spec(raw[k], f"{path}.{k}") if k in raw else Section({})
        for k, spec in _COMFORT_OPTIONAL.items()
    }
    return Section({**base, **optional})


SCHEMA: Spec = section(
    {
        "config_version": integer(MPC_CONFIG_VERSION, MPC_CONFIG_VERSION),
        "timezone": timezone,
        "horizon_hours": integer(1, 72),
        "replan_every_minutes": integer(15, 1440),
        "comfort": _comfort,
        "optimum_start": section(
            {
                "band_edges_c": seq(num(-30.0, 30.0), min_len=0, unique=True),
                "lead_weekday_minutes": seq(integer(0, 24 * 60), min_len=1),
                "lead_weekend_minutes": seq(integer(0, 24 * 60), min_len=1),
            }
        ),
        "machine": section(
            {
                "min_on_minutes": integer(0, 240),
                "min_off_minutes": integer(0, 240),
                "water_min_c": integer(15, 60),
                "water_max_c": integer(15, 60),
                "start_penalty_kwh": num(0.0, 10.0),
            }
        ),
        "water": section(
            {
                "curve": seq(_point, min_len=2),
                "offsets_k": seq(integer(-10, 10), min_len=1, unique=True),
            }
        ),
        "model": section(
            {
                "kind": text(*MODEL_KINDS),
                "c2_model_id": _hex_prefix,
                "diurnal_harmonics": integer(0, 4),
                "reestimate": section(
                    {
                        "window_days": integer(1, 120),
                        "every_days": integer(1, 60),
                        "params": seq(text(*REESTIMABLE), unique=True),
                    }
                ),
            }
        ),
        "terminal": section({"value_stored_heat": boolean}),
        "solver": section(
            {
                "time_limit_s": num(0.1, 3600.0),
                "mip_rel_gap": num(1e-9, 0.5),
                "relinearize_iterations": integer(0, 5),
            }
        ),
        "replay": section(
            {
                "seasons": seq(text("S1", "S2", "S3"), min_len=1, unique=True),
                "months": seq(integer(1, 12), min_len=1, unique=True),
                "weather": text(*WEATHER_MODES),
                "accuracy_horizons_hours": seq(integer(1, 72), min_len=1, unique=True),
                "hdd_base_c": num(-10.0, 30.0),
                "material": section({"kwh_rel": num(0.0, 10.0), "on_overlap_min": num(0.0, 1.0)}),
            }
        ),
    }
)


def _cross_checks(cfg: Section) -> None:
    c, m = cfg.comfort, cfg.machine
    for key in ("day_start_weekday_hour", "day_start_weekend_hour"):
        if not c[key] < c.day_end_hour:
            raise _fail(f"comfort.{key}", "must be before comfort.day_end_hour")
    if c.floor_c > c.day_min_c:
        raise _fail("comfort.floor_c", "must not exceed comfort.day_min_c")
    if not m.water_min_c < m.water_max_c:
        raise _fail("machine.water_min_c", "must be below machine.water_max_c")
    xs = [p[0] for p in cfg.water.curve]
    if any(b <= a for a, b in zip(xs, xs[1:], strict=False)):
        raise _fail("water.curve", "outdoor temperatures must increase strictly")
    if cfg.replan_every_minutes % 15:
        raise _fail("replan_every_minutes", "must be a whole number of 15-minute slots")
    o = cfg.optimum_start
    edges = list(o.band_edges_c)
    if edges != sorted(edges):
        raise _fail("optimum_start.band_edges_c", "must increase")
    for key in ("lead_weekday_minutes", "lead_weekend_minutes"):
        if len(o[key]) != len(edges) + 1:
            raise _fail(f"optimum_start.{key}", "needs one lead per band (len(band_edges_c) + 1)")
    if max(cfg.replay.accuracy_horizons_hours) > cfg.horizon_hours:
        raise _fail("replay.accuracy_horizons_hours", "must not exceed horizon_hours")


class MpcConfig(Section):
    """The validated configuration; ``sha256`` identifies the exact file."""

    sha256: str
    path: Path


def load_mpc_config(path: str | Path = "config/climate-mpc.yaml") -> MpcConfig:
    """Read, strictly validate and hash the MPC configuration file."""
    p = Path(path).expanduser()
    try:
        payload = p.read_bytes()
    except OSError as exc:
        raise MpcConfigError(f"climate-mpc config: cannot read {p}: {exc.strerror}") from exc
    try:
        raw = yaml.load(payload, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
        tree = SCHEMA(raw, "")
    except yaml.YAMLError as exc:
        raise MpcConfigError(f"climate-mpc config: {p}: not valid YAML ({exc})") from exc
    except MpcConfigError:
        raise
    except ClimateConfigError as exc:
        raise MpcConfigError(str(exc).replace("climate config:", "climate-mpc config:", 1)) from exc
    cfg = MpcConfig(tree)
    object.__setattr__(cfg, "sha256", hashlib.sha256(payload).hexdigest())
    object.__setattr__(cfg, "path", p)
    _cross_checks(cfg)
    return cfg


def override(cfg: MpcConfig, path: tuple[str, ...], value: Any) -> MpcConfig:
    """A copy of the read-only tree with one leaf replaced; ``sha256`` and ``path`` still name the
    file, so a caller records the override next to them."""
    raw = cfg.to_dict()
    node = raw
    for key in path[:-1]:
        node = node[key]
    if path[-1] not in node:
        raise MpcConfigError(f"climate-mpc config: no key {'.'.join(path)}")
    node[path[-1]] = value

    def wrap(x: Any) -> Any:
        return Section({k: wrap(v) for k, v in x.items()}) if isinstance(x, dict) else x

    out = type(cfg)(wrap(raw))
    object.__setattr__(out, "sha256", cfg.sha256)
    object.__setattr__(out, "path", cfg.path)
    return out
