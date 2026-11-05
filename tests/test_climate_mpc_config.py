"""Strict validation of ``config/climate-mpc.yaml`` (the comfort MPC, track B1)."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from battery_optimizer.climate.mpc import PlanSettings
from battery_optimizer.climate.mpc_config import MpcConfigError, load_mpc_config

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = REPO_ROOT / "config" / "climate-mpc.yaml"


def _raw() -> dict[str, Any]:
    return yaml.safe_load(SHIPPED.read_text())


def _write(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "climate-mpc.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def test_shipped_config_holds_the_design_values():
    cfg = load_mpc_config(SHIPPED)
    assert cfg.sha256 == hashlib.sha256(SHIPPED.read_bytes()).hexdigest()
    assert (cfg.horizon_hours, cfg.replan_every_minutes) == (24, 60)
    c = cfg.comfort
    assert (c.day_room, c.day_min_c, c.floor_c) == ("hall_kitchen", 23.5, 21.0)
    assert (c.day_start_weekday_hour, c.day_start_weekend_hour, c.day_end_hour) == (12, 9, 22)
    assert (cfg.machine.min_on_minutes, cfg.machine.min_off_minutes) == (30, 20)
    assert (cfg.machine.water_min_c, cfg.machine.water_max_c) == (20, 35)
    assert cfg.water.curve == ((-5.0, 32.0), (15.0, 25.0))
    assert cfg.water.offsets_k == (0, 1, 2, 3, 4, 5, 6)
    assert cfg.optimum_start.lead_weekday_minutes == (675, 545, 275)  # design section 6, p75
    assert cfg.optimum_start.lead_weekend_minutes == (530, 430, 215)
    settings = PlanSettings.from_config(cfg)
    assert (settings.min_on_slots, settings.min_off_slots) == (2, 2)  # 20 min rounds up


def test_the_climate_config_is_not_touched():
    """The C2 config hash is part of recorded results: the MPC has its own file."""
    text = (REPO_ROOT / "config" / "climate.yaml").read_text()
    assert "mpc" not in text.lower()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.update(extra=1), "extra: unknown key"),
        (lambda r: r["comfort"].pop("floor_c"), "comfort.floor_c: missing key"),
        (lambda r: r["machine"].update(min_on_minutes=True), "expected an integer"),
        (lambda r: r["comfort"].update(day_min_c="23.5"), "expected a number"),
        (lambda r: r["model"].update(kind="mlp"), "is not one of"),
        (lambda r: r["replay"].update(weather="forecast"), "is not one of"),
        (lambda r: r["comfort"].update(day_start_weekday_hour=22), "before comfort.day_end_hour"),
        (lambda r: r["comfort"].update(floor_c=24.0), "must not exceed"),
        (lambda r: r["machine"].update(water_min_c=35), "below machine.water_max_c"),
        (lambda r: r["water"].update(curve=[[15, 25], [-5, 32]]), "increase strictly"),
        (lambda r: r.update(replan_every_minutes=50), "whole number of 15-minute"),
        (lambda r: r["replay"].update(accuracy_horizons_hours=[1, 48]), "horizon_hours"),
        (lambda r: r["water"].update(offsets_k=[0, 0]), "unique"),
        (lambda r: r["model"].update(c2_model_id="zz"), "hex"),
        (lambda r: r.update(config_version=2), "above the maximum"),
        (lambda r: r["optimum_start"].update(lead_weekday_minutes=[600, 300]), "one lead per band"),
        (lambda r: r["optimum_start"].update(band_edges_c=[12.0, 8.0]), "must increase"),
    ],
)
def test_malformed_configs_are_refused(tmp_path, mutate, message):
    raw = copy.deepcopy(_raw())
    mutate(raw)
    with pytest.raises(MpcConfigError, match="climate-mpc config") as err:
        load_mpc_config(_write(tmp_path, raw))
    assert message in str(err.value)


def test_a_duplicated_key_is_refused(tmp_path):
    path = tmp_path / "dup.yaml"
    path.write_text(SHIPPED.read_text() + "\nhorizon_hours: 12\n")
    with pytest.raises(MpcConfigError, match="duplicate key"):
        load_mpc_config(path)


def test_a_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(MpcConfigError, match="cannot read"):
        load_mpc_config(tmp_path / "absent.yaml")


def test_the_per_room_comfort_maps_are_optional_and_default_to_empty():
    c = load_mpc_config(SHIPPED).comfort
    assert "floor_c_by_room" not in SHIPPED.read_text()  # the shipped file (and hash) is unchanged
    assert dict(c.floor_c_by_room) == {} and dict(c.day_min_c_by_room) == {}


def test_the_per_room_comfort_maps_load(tmp_path):
    raw = _raw()
    raw["comfort"]["floor_c_by_room"] = {"bedroom": 20.0, "office_t": 22.0}
    raw["comfort"]["day_min_c_by_room"] = {"hall_kitchen": 24.0, "office_t": 21.5}
    c = load_mpc_config(_write(tmp_path, raw)).comfort
    assert dict(c.floor_c_by_room) == {"bedroom": 20.0, "office_t": 22.0}
    assert dict(c.day_min_c_by_room) == {"hall_kitchen": 24.0, "office_t": 21.5}
    assert (c.day_min_c, c.floor_c) == (23.5, 21.0)  # the old keys keep working


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("floor_c_by_room", {"attic": 21.0}, "is not one of"),
        ("floor_c_by_room", {"bedroom": 5.0}, "below the minimum"),
        ("day_min_c_by_room", {"bedroom": "21"}, "expected a number"),
        ("day_min_c_by_room", {"bedroom": 30.0}, "above the maximum"),
        ("day_min_c_by_room", [21.0], "expected a mapping"),
    ],
)
def test_malformed_per_room_comfort_maps_are_refused(tmp_path, key, value, message):
    raw = _raw()
    raw["comfort"][key] = value
    with pytest.raises(MpcConfigError, match="climate-mpc config") as err:
        load_mpc_config(_write(tmp_path, raw))
    assert f"comfort.{key}" in str(err.value) and message in str(err.value)
