"""Site overrides layered over the shipped service config (deploys never overwrite a value)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from service_fakes import write_service_config

from battery_optimizer.battery_telemetry import load_battery_telemetry
from battery_optimizer.cli import main
from battery_optimizer.service.climate_shadow import config_paths
from battery_optimizer.service.config import (
    load_overrides_file,
    load_service_config,
    merge_service_overrides,
    resolve_overrides_path,
)

REPO = Path(__file__).resolve().parents[1]


def write_overrides(tmp_path: Path, content: Any, name: str = "overrides.yaml") -> Path:
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else yaml.safe_dump(content))
    return path


@pytest.fixture(autouse=True)
def _no_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BATTERY_OPTIMIZER", "ENERGY_BASELINE"):
        monkeypatch.delenv(f"{name}_SERVICE_CONFIG_OVERRIDES", raising=False)
        monkeypatch.delenv(f"{name}_SERVICE_CONFIG", raising=False)


class TestMerge:
    def test_nested_mappings_merge_and_untouched_siblings_survive(self) -> None:
        base = {"a": {"x": 1, "y": {"p": 1, "q": 2}}, "b": 3}
        merged, applied = merge_service_overrides(base, {"a": {"y": {"q": 9}}})
        assert merged == {"a": {"x": 1, "y": {"p": 1, "q": 9}}, "b": 3}
        assert [(o.path, o.base, o.value) for o in applied] == [("a.y.q", 2, 9)]
        assert base == {"a": {"x": 1, "y": {"p": 1, "q": 2}}, "b": 3}  # input untouched

    @pytest.mark.parametrize(
        ("base_value", "override"),
        [(True, False), (5, 0), ("shadow", ""), ("live", None), (None, 3), (1.5, 0.0)],
    )
    def test_falsy_and_null_values_replace_the_base(self, base_value: Any, override: Any) -> None:
        merged, applied = merge_service_overrides({"k": base_value}, {"k": override})
        assert merged["k"] == override
        assert type(merged["k"]) is type(override)
        assert len(applied) == 1

    def test_lists_are_replaced_not_merged(self) -> None:
        merged, _ = merge_service_overrides({"k": [1, 2, 3]}, {"k": [9]})
        assert merged == {"k": [9]}
        merged, _ = merge_service_overrides({"k": [1, 2, 3]}, {"k": []})
        assert merged == {"k": []}

    def test_an_empty_mapping_override_changes_nothing(self) -> None:
        merged, applied = merge_service_overrides({"a": {"x": 1}}, {"a": {}})
        assert merged == {"a": {"x": 1}}
        assert applied == ()

    def test_a_top_level_unknown_path_is_refused_naming_it(self) -> None:
        with pytest.raises(ValueError, match="'modee' does not exist"):
            merge_service_overrides({"mode": "shadow"}, {"modee": "live"})

    def test_a_nested_unknown_path_is_refused_naming_the_full_path(self) -> None:
        with pytest.raises(ValueError, match=r"'a\.y\.z' does not exist"):
            merge_service_overrides({"a": {"y": {"q": 1}}}, {"a": {"y": {"z": 1}}})

    def test_mapping_against_non_mapping_is_refused_both_ways(self) -> None:
        with pytest.raises(ValueError, match="'a' is a mapping but the base value is int"):
            merge_service_overrides({"a": 1}, {"a": {"x": 1}})
        with pytest.raises(ValueError, match="'a' is a mapping in the base config"):
            merge_service_overrides({"a": {"x": 1}}, {"a": None})
        with pytest.raises(ValueError, match="'a' is a mapping but the base value is NoneType"):
            merge_service_overrides({"a": None}, {"a": {"x": 1}})


class TestOverridesFile:
    @pytest.mark.parametrize("content", ["- a\n- b\n", "just a string\n", "", "# only a comment\n"])
    def test_a_non_mapping_file_is_refused(self, tmp_path: Path, content: str) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            load_overrides_file(write_overrides(tmp_path, content))

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            load_overrides_file(tmp_path / "absent.yaml")

    def test_the_path_comes_from_the_argument_then_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert resolve_overrides_path() is None
        monkeypatch.setenv("BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES", str(tmp_path / "env.yaml"))
        assert resolve_overrides_path() == (tmp_path / "env.yaml").resolve()
        assert (
            resolve_overrides_path(str(tmp_path / "cli.yaml")) == (tmp_path / "cli.yaml").resolve()
        )


class TestLoadServiceConfig:
    def test_without_overrides_nothing_changes(self, tmp_path: Path) -> None:
        base = write_service_config(tmp_path)
        plain = load_service_config(base)
        explicit_none = load_service_config(base, None)
        assert plain == explicit_none
        assert plain.overrides == ()
        assert plain.overrides_path is None
        assert plain.override_values() == {}
        assert plain.config_hash() == explicit_none.config_hash()

    def test_overrides_equal_an_edited_full_file_and_hash_identically(self, tmp_path: Path) -> None:
        edits = {
            "planning.high_soc_cost_eur_per_kwh_h": 0.01,
            "planning.discharge_margin_bound": True,
            "climate_shadow.enabled": True,
            "climate_shadow.prior": "abc123",
            "full_charge.enabled": True,
        }
        full = load_service_config(write_service_config(tmp_path, **edits))
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        base = write_service_config(base_dir)
        overrides = write_overrides(
            tmp_path,
            {
                "planning": {
                    "high_soc_cost_eur_per_kwh_h": 0.01,
                    "discharge_margin_bound": True,
                },
                "climate_shadow": {"enabled": True, "prior": "abc123"},
                "full_charge": {"enabled": True},
            },
        )
        layered = load_service_config(base, overrides)
        assert layered.config_hash() == full.config_hash()
        assert layered.planning == full.planning
        assert layered.climate_shadow == full.climate_shadow
        assert layered.full_charge == full.full_charge
        assert layered.config_hash() != load_service_config(base).config_hash()
        assert layered.override_values() == {
            "planning.high_soc_cost_eur_per_kwh_h": 0.01,
            "planning.discharge_margin_bound": True,
            "climate_shadow.enabled": True,
            "climate_shadow.prior": "abc123",
            "full_charge.enabled": True,
        }

    def test_an_override_equal_to_the_base_keeps_the_hash(self, tmp_path: Path) -> None:
        base = write_service_config(tmp_path)
        pinned = load_service_config(base, write_overrides(tmp_path, {"mode": "shadow"}))
        assert pinned.config_hash() == load_service_config(base).config_hash()
        assert [o.path for o in pinned.overrides] == ["mode"]

    def test_a_false_override_wins_over_a_true_base(self, tmp_path: Path) -> None:
        base = write_service_config(
            tmp_path, **{"climate_shadow.enabled": True, "climate_shadow.prior": "abc123"}
        )
        config = load_service_config(
            base, write_overrides(tmp_path, {"climate_shadow": {"enabled": False}})
        )
        assert config.climate_shadow.enabled is False

    def test_an_unknown_key_fails_the_load_naming_it(self, tmp_path: Path) -> None:
        base = write_service_config(tmp_path)
        overrides = write_overrides(tmp_path, {"climate_shadow": {"enabeld": True}})
        with pytest.raises(ValueError, match=r"'climate_shadow\.enabeld' does not exist"):
            load_service_config(base, overrides)

    def test_the_overrides_environment_variable_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = write_service_config(tmp_path)
        overrides = write_overrides(tmp_path, {"observation": {}, "timezone": "Europe/Lisbon"})
        monkeypatch.setenv("BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES", str(overrides))
        config = load_service_config(base)
        assert config.timezone == "Europe/Lisbon"
        assert config.overrides_path == str(overrides.resolve())

    def test_overridden_values_are_still_validated(self, tmp_path: Path) -> None:
        base = write_service_config(tmp_path)
        with pytest.raises(ValueError, match="mode"):
            load_service_config(base, write_overrides(tmp_path, {"mode": "yolo"}))

    def test_climate_paths_stay_relative_to_the_base_files_directory(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "app" / "config"
        base_dir.mkdir(parents=True)
        base = write_service_config(base_dir)
        overrides_dir = tmp_path / "site"
        overrides_dir.mkdir()
        overrides = write_overrides(
            overrides_dir, {"climate_shadow": {"enabled": True, "prior": "abc123"}}
        )
        config = load_service_config(base, overrides)
        climate, mpc = config_paths(config)
        assert climate == base_dir / "climate.yaml"
        assert mpc == base_dir / "climate-mpc.yaml"

    def test_secret_looking_paths_are_not_shown(self) -> None:
        _, applied = merge_service_overrides({"a": {"api_token": "x"}}, {"a": {"api_token": "y"}})
        assert applied[0].shown_value == "<redacted>"
        assert "y" not in applied[0].describe()


class TestOfflineReaders:
    def _base(self, tmp_path: Path) -> Path:
        return write_service_config(tmp_path)

    def test_battery_telemetry_honours_the_argument(self, tmp_path: Path) -> None:
        overrides = write_overrides(
            tmp_path, {"battery": {"commissioned_at": "2026-12-01T00:00:00+01:00"}}
        )
        loaded = load_battery_telemetry(self._base(tmp_path), overrides)
        assert loaded.commissioned_at == datetime(2026, 11, 30, 23, tzinfo=UTC)

    def test_battery_telemetry_honours_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = self._base(tmp_path)
        before = load_battery_telemetry(base).commissioned_at
        overrides = write_overrides(
            tmp_path,
            {
                "battery": {"commissioned_at": "2026-12-01T00:00:00+01:00"},
                "entities": {"battery_charge_energy": None, "battery_discharge_energy": None},
            },
        )
        monkeypatch.setenv("BATTERY_OPTIMIZER_SERVICE_CONFIG", str(base))
        monkeypatch.setenv("BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES", str(overrides))
        loaded = load_battery_telemetry()
        assert loaded.commissioned_at == datetime(2026, 11, 30, 23, tzinfo=UTC)
        assert loaded.commissioned_at != before
        assert not loaded.has_counters

    def test_battery_telemetry_refuses_an_unknown_override_path(self, tmp_path: Path) -> None:
        overrides = write_overrides(tmp_path, {"battery": {"commisioned_at": "x"}})
        with pytest.raises(ValueError, match=r"'battery\.commisioned_at' does not exist"):
            load_battery_telemetry(self._base(tmp_path), overrides)

    def test_battery_telemetry_without_overrides_is_unchanged(self, tmp_path: Path) -> None:
        base = self._base(tmp_path)
        assert load_battery_telemetry(base) == load_battery_telemetry(base, None)


class TestCheckCommand:
    def test_success_prints_the_applied_overrides(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        base = write_service_config(tmp_path)
        overrides = write_overrides(tmp_path, {"climate_shadow": {"prior": "abc123"}})
        code = main(
            ["service-config", "--service-config", str(base), "--overrides", str(overrides)]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "OK" in out
        assert "override climate_shadow.prior: None -> 'abc123'" in out
        assert load_service_config(base, overrides).config_hash() in out

    def test_success_without_overrides(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["service-config", "--service-config", str(write_service_config(tmp_path))])
        out = capsys.readouterr().out
        assert code == 0
        assert "overrides=none" in out
        assert "override " not in out.replace("overrides=", "")

    def test_the_long_option_name_works_too(self, tmp_path: Path) -> None:
        base = write_service_config(tmp_path)
        overrides = write_overrides(tmp_path, {"mode": "shadow"})
        assert (
            main(
                [
                    "service-config",
                    "--service-config",
                    str(base),
                    "--service-config-overrides",
                    str(overrides),
                ]
            )
            == 0
        )

    @pytest.mark.parametrize("content", [{"nope": 1}, {"mode": "yolo"}, "- not a mapping\n", ""])
    def test_failure_exits_non_zero_with_one_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: Any
    ) -> None:
        base = write_service_config(tmp_path)
        code = main(
            [
                "service-config",
                "--service-config",
                str(base),
                "--overrides",
                str(write_overrides(tmp_path, content)),
            ]
        )
        err = capsys.readouterr().err
        assert code == 1
        assert err.startswith("service-config: ")

    def test_a_missing_overrides_file_fails(self, tmp_path: Path) -> None:
        base = write_service_config(tmp_path)
        assert (
            main(
                [
                    "service-config",
                    "--service-config",
                    str(base),
                    "--overrides",
                    str(tmp_path / "absent.yaml"),
                ]
            )
            == 1
        )

    def test_the_shipped_config_loads_without_overrides(self) -> None:
        assert (
            main(["service-config", "--service-config", str(REPO / "config" / "service.yaml")]) == 0
        )


def _service(tmp_path: Path, config: Any = None) -> Any:
    from service_fakes import NOW, TARIFF, FakeHAServer, service_config

    from battery_optimizer.service.clock import FakeClock
    from battery_optimizer.service.runtime import ShadowService

    return ShadowService(
        config=config or service_config(tmp_path),
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=FakeClock(NOW),
        code_version="test-sha",
        ws_transport=FakeHAServer(),
    )


def _close(service: Any) -> None:
    service._executor.shutdown(wait=True)
    service._io.shutdown(wait=True)
    service._history_pool.shutdown(wait=True)


class TestVisibility:
    def test_status_and_metrics_expose_the_overridden_paths(self, tmp_path: Path) -> None:
        from dataclasses import replace

        from prometheus_client import generate_latest

        _, applied = merge_service_overrides(
            {"planning": {"high_soc_cost_eur_per_kwh_h": 0.0}},
            {"planning": {"high_soc_cost_eur_per_kwh_h": 0.01}},
        )
        from service_fakes import service_config

        service = _service(tmp_path, replace(service_config(tmp_path), overrides=applied))
        try:
            status = service.status()
            text = generate_latest(service.metrics.registry).decode()
        finally:
            _close(service)
        assert status["config_overrides"] == {"planning.high_soc_cost_eur_per_kwh_h": 0.01}
        series = "battery_optimizer_config_override_info"
        path = "planning.high_soc_cost_eur_per_kwh_h"
        assert f'{series}{{path="{path}"}} 1.0' in text

    def test_the_status_of_a_service_without_overrides_lists_none(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        try:
            assert service.status()["config_overrides"] == {}
        finally:
            _close(service)


class TestImage:
    def test_the_image_ships_the_base_config_at_the_default_lookup_location(self) -> None:
        dockerfile = (REPO / "Dockerfile").read_text()
        assert "WORKDIR /app" in dockerfile
        assert "COPY config ./config" in dockerfile
        assert "!config/" in (REPO / ".dockerignore").read_text().splitlines()
        for name in ("service.yaml", "tariff.yaml", "climate.yaml", "climate-mpc.yaml"):
            assert (REPO / "config" / name).is_file()
