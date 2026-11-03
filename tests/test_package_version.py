"""The package reports the version ``pyproject.toml`` declares (no second pin)."""

import tomllib
from pathlib import Path

import battery_optimizer

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_is_the_pyproject_version() -> None:
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert battery_optimizer.__version__ == declared
