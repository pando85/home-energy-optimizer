"""Tests for the sub-load feature table: integration rule, quality, placement, immutability."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from battery_optimizer import features
from battery_optimizer.cli import main
from battery_optimizer.extract_prom import (
    CLIMATE_ENTITY_IDS,
    CLIMATE_ROOMS,
    ENTITY_SPECS,
    FEATURE_ENTITY_IDS,
    compute_series_key,
    resolve_entity_specs,
)
from battery_optimizer.features import (
    FeatureError,
    Signal,
    assemble_table,
    build_features,
    cop_check,
    integrate_slots,
    load_features,
    main_meter_check,
    merge_samples,
    signal_columns,
)
from battery_optimizer.paths import parts_dir
from battery_optimizer.schemas import RAW_SAMPLE

T0 = datetime(2026, 1, 5, tzinfo=UTC)
SERVERS = "sensor.shellypmmini_servers_power"
HEAT = "sensor.aquarea_heat_power_consumed"
TEMP = "sensor.aquarea_outside_ambient_temperature"
ENABLE = "input_boolean.appdaemon_climate_enable"
POWER_SIGNAL = Signal(SERVERS, "servers", features.KIND_POWER)
STATE_SIGNAL = Signal(ENABLE, "climate_enable", features.KIND_STATE)
TEMP_SIGNAL = Signal(TEMP, "outdoor_temp", features.KIND_TEMPERATURE)


def edges(n_slots: int, step: float = 900.0) -> np.ndarray:
    return np.arange(n_slots + 1, dtype=float) * step


def us_edges(n_slots: int) -> np.ndarray:
    base = int(T0.timestamp()) * 1_000_000
    return base + np.arange(n_slots + 1, dtype=np.int64) * 900_000_000


# ---------------------------------------------------------------------------
# Numerics
# ---------------------------------------------------------------------------


class TestMergeSamples:
    def test_sorts_and_drops_repeated_timestamps_keeping_the_first(self) -> None:
        t, v, dropped, conflicts = merge_samples(
            np.array([3, 1, 2, 2], dtype=np.int64), np.array([30.0, 10.0, 20.0, 20.0])
        )
        assert t.tolist() == [1, 2, 3]
        assert v.tolist() == [10.0, 20.0, 30.0]
        assert (dropped, conflicts) == (1, 0)

    def test_a_conflicting_duplicate_is_counted(self) -> None:
        _t, v, dropped, conflicts = merge_samples(
            np.array([1, 1], dtype=np.int64), np.array([5.0, 6.0])
        )
        assert v.tolist() == [5.0]
        assert (dropped, conflicts) == (1, 1)

    def test_empty_input(self) -> None:
        t, v, dropped, conflicts = merge_samples(np.zeros(0, dtype=np.int64), np.zeros(0))
        assert t.size == v.size == 0
        assert (dropped, conflicts) == (0, 0)


class TestIntegrateSlots:
    def test_sample_and_hold_is_exact_across_a_slot_edge(self) -> None:
        # 100 W held from 0 to 450 s, 300 W from 450 s on; one 900 s slot.
        t = np.array([0.0, 450.0, 900.0])
        v = np.array([100.0, 300.0, 300.0])
        integral, covered, at_start = integrate_slots(t, v, edges(1), 1000.0)
        assert integral[0] == pytest.approx(100 * 450 + 300 * 450)
        assert covered[0] == pytest.approx(900.0)
        assert at_start[0] == 100.0

    def test_a_span_is_split_in_proportion_to_time_at_the_slot_edge(self) -> None:
        t = np.array([0.0, 600.0, 1800.0])
        v = np.array([200.0, 400.0, 400.0])
        integral, covered, _ = integrate_slots(t, v, edges(2), 2000.0)
        assert integral.tolist() == pytest.approx([200 * 600 + 400 * 300, 400 * 900])
        assert covered.tolist() == pytest.approx([900.0, 900.0])

    def test_a_span_longer_than_the_max_gap_is_not_held(self) -> None:
        t = np.array([0.0, 60.0, 120.0, 1200.0, 1260.0])
        v = np.array([500.0] * 5)
        _integral, covered, at_start = integrate_slots(t, v, edges(2), 300.0)
        # slot 0: spans 0-60 and 60-120 covered (120 s); 120-1200 is a gap.
        assert covered[0] == pytest.approx(120.0)
        assert covered[1] == pytest.approx(60.0)
        assert np.isnan(at_start[1])  # the slot starts inside the gap

    def test_the_time_after_the_last_sample_is_not_covered(self) -> None:
        t = np.array([0.0, 60.0])
        v = np.array([10.0, 10.0])
        _integral, covered, _ = integrate_slots(t, v, edges(1), 300.0)
        assert covered[0] == pytest.approx(60.0)

    def test_fewer_than_two_samples_cover_nothing(self) -> None:
        integral, covered, at_start = integrate_slots(
            np.array([5.0]), np.array([1.0]), edges(2), 300.0
        )
        assert integral.tolist() == [0.0, 0.0]
        assert covered.tolist() == [0.0, 0.0]
        assert np.isnan(at_start).all()


class TestSignalColumns:
    def _minute_samples(self, watts: float, minutes: int) -> tuple[np.ndarray, np.ndarray]:
        base = int(T0.timestamp()) * 1_000_000
        t = base + np.arange(minutes + 1, dtype=np.int64) * 60_000_000
        return t, np.full(t.size, watts)

    def test_constant_power_gives_kwh_and_full_quality(self) -> None:
        t, v = self._minute_samples(400.0, 60)
        cols = signal_columns(
            POWER_SIGNAL, t, v, us_edges(3), max_gap_seconds=300.0, min_coverage=0.8
        )
        assert cols["servers_kwh"].tolist() == pytest.approx([0.1, 0.1, 0.1])
        assert cols["servers_coverage"].tolist() == pytest.approx([1.0, 1.0, 1.0])
        assert cols["servers_quality"].tolist() == ["measured"] * 3

    def test_partial_slot_is_scaled_and_labelled_and_a_thin_slot_is_unknown_not_zero(
        self,
    ) -> None:
        base = int(T0.timestamp()) * 1_000_000
        # slot 0: samples each minute for 12 minutes then a 3 minute pause (still held, gap of
        # 180 s <= 300 s) -> full; slot 1: samples for the first 2 minutes only, then a gap.
        minutes = list(range(0, 16)) + [16, 17, 18, 45, 46, 47, 48]
        t = base + np.array(minutes, dtype=np.int64) * 60_000_000
        v = np.full(t.size, 600.0)
        cols = signal_columns(
            POWER_SIGNAL, t, v, us_edges(3), max_gap_seconds=300.0, min_coverage=0.8
        )
        # slot 1 (15-30 min): covered 15-18 min = 3 min of 15 -> unknown.
        assert cols["servers_quality"][1] == "unknown"
        assert np.isnan(cols["servers_kwh"][1])
        assert cols["servers_coverage"][1] == pytest.approx(3 / 15)
        # slot 0 is fully covered.
        assert cols["servers_quality"][0] == "measured"

    def test_partial_coverage_above_the_threshold_is_scaled_to_the_full_slot(self) -> None:
        base = int(T0.timestamp()) * 1_000_000
        # 12 of 15 minutes covered, 500 W: energy is the mean power times the full slot.
        minutes = list(range(0, 12)) + [12.0]
        t = base + (np.array(minutes) * 60_000_000).astype(np.int64)
        v = np.full(t.size, 500.0)
        # a second slot's worth of samples far away so the 12 -> 40 min span is a gap
        t = np.concatenate([t, base + np.array([40 * 60_000_000, 41 * 60_000_000])])
        v = np.concatenate([v, [500.0, 500.0]])
        cols = signal_columns(
            POWER_SIGNAL, t, v, us_edges(1), max_gap_seconds=300.0, min_coverage=0.8
        )
        assert cols["servers_coverage"][0] == pytest.approx(12 / 15)
        assert cols["servers_quality"][0] == "partial"
        assert cols["servers_kwh"][0] == pytest.approx(500 * 0.25 / 1000)

    def test_no_samples_is_unknown_never_zero(self) -> None:
        cols = signal_columns(
            POWER_SIGNAL,
            np.zeros(0, dtype=np.int64),
            np.zeros(0),
            us_edges(2),
            max_gap_seconds=300.0,
            min_coverage=0.8,
        )
        assert np.isnan(cols["servers_kwh"]).all()
        assert cols["servers_coverage"].tolist() == [0.0, 0.0]
        assert cols["servers_quality"].tolist() == ["unknown", "unknown"]

    def test_state_has_value_at_start_and_fraction_on(self) -> None:
        base = int(T0.timestamp()) * 1_000_000
        # off for the first 6 minutes of the slot, then on.
        minutes = np.arange(0, 31)
        t = base + minutes.astype(np.int64) * 60_000_000
        v = np.where(minutes < 6, 0.0, 1.0)
        cols = signal_columns(
            STATE_SIGNAL, t, v, us_edges(2), max_gap_seconds=300.0, min_coverage=0.8
        )
        assert cols["climate_enable_at_start"].tolist() == [0.0, 1.0]
        assert cols["climate_enable_mean"].tolist() == pytest.approx([9 / 15, 1.0])

    def test_temperature_is_the_time_weighted_mean(self) -> None:
        base = int(T0.timestamp()) * 1_000_000
        t = base + np.array([0, 450, 900], dtype=np.int64) * 1_000_000
        v = np.array([10.0, 20.0, 20.0])
        cols = signal_columns(
            TEMP_SIGNAL, t, v, us_edges(1), max_gap_seconds=1000.0, min_coverage=0.8
        )
        assert cols["outdoor_temp_mean_c"][0] == pytest.approx(15.0)


class TestAssembleTable:
    def test_missing_series_yield_nulls_and_the_schema_is_stable(self) -> None:
        table = assemble_table({}, us_edges(4))
        assert table.schema.equals(features.feature_schema())
        assert table.num_rows == 4
        assert table.column("servers_kwh").null_count == 4
        assert table.column("climate_enable_at_start").null_count == 4
        assert set(table.column("servers_quality").to_pylist()) == {"unknown"}

    def test_slots_are_a_15_minute_utc_grid(self) -> None:
        table = assemble_table({}, us_edges(3))
        starts = table.column("interval_start").to_pylist()
        ends = table.column("interval_end").to_pylist()
        assert starts[1] - starts[0] == timedelta(minutes=15)
        assert ends[0] == starts[1]
        assert starts[0].tzinfo is not None


class TestSlotEdges:
    def test_unaligned_bounds_are_refused(self) -> None:
        with pytest.raises(FeatureError, match="15 minutes"):
            features.slot_edges_us(T0 + timedelta(minutes=7), T0 + timedelta(hours=1))

    def test_empty_window_is_refused(self) -> None:
        with pytest.raises(FeatureError, match="before end"):
            features.slot_edges_us(T0, T0)


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------


def _feature_table(**columns: list[float]) -> pa.Table:
    n = len(next(iter(columns.values())))
    base = {
        name: pa.array(
            [None if x is None or x != x else x for x in values],  # NaN -> null
            pa.float64(),
        )
        for name, values in columns.items()
    }
    starts = [T0 + timedelta(minutes=15 * i) for i in range(n)]
    return pa.table(
        {
            "interval_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "interval_end": pa.array(
                [s + timedelta(minutes=15) for s in starts], pa.timestamp("us", tz="UTC")
            ),
            **base,
        }
    )


def _dataset_table(gross: list[float], quality: str = "measured") -> pa.Table:
    n = len(gross)
    starts = [T0 + timedelta(minutes=15 * i) for i in range(n)]
    return pa.table(
        {
            "interval_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "gross_kwh": pa.array(gross, pa.float64()),
            "energy_quality": pa.array([quality] * n),
            "allocation_quality": pa.array(["estimated_boundary"] * n),
        }
    )


class TestMainMeterCheck:
    def _table(self) -> pa.Table:
        nan = float("nan")
        return _feature_table(
            servers_kwh=[0.05, 0.05, 0.05, 0.05, nan],
            hp_heat_kwh=[0.10, 0.20, 0.0, 0.0, 0.0],
            hp_dhw_kwh=[0.0, 0.0, 0.0, 0.0, 0.0],
            hp_cool_kwh=[0.0, 0.0, 0.0, 0.0, 0.0],
        )

    def test_counts_violations_and_their_size_per_tolerance(self) -> None:
        dataset = _dataset_table([0.30, 0.20, 0.0, 0.07, 0.30])
        check = main_meter_check(self._table(), dataset)
        # slot 4 has no servers value -> not compared.
        assert check["slots_compared"] == 4
        by_tol = check["violations_by_tolerance"]
        # excess: 0.15-0.30=-0.15 ok; 0.25-0.20=0.05 viol; 0.05-0=0.05 viol; 0.05-0.07<0.
        assert by_tol["0"]["count"] == 2
        assert by_tol["0.02"]["count"] == 2
        assert by_tol["0.05"]["count"] == 0
        assert by_tol["0"]["excess_kwh_max"] == pytest.approx(0.05)

    def test_slots_with_unusable_main_are_not_compared(self) -> None:
        dataset = _dataset_table([0.0, 0.0, 0.0, 0.0, 0.0], quality="gap_total_unknown")
        check = main_meter_check(self._table(), dataset)
        assert check["slots_compared"] == 0

    def test_a_missing_component_is_unknown_not_zero(self) -> None:
        nan = float("nan")
        table = _feature_table(
            servers_kwh=[0.05],
            hp_heat_kwh=[nan],
            hp_dhw_kwh=[0.0],
            hp_cool_kwh=[0.0],
        )
        assert np.isnan(features.heat_pump_input(table)).all()
        assert main_meter_check(table, _dataset_table([0.0]))["slots_compared"] == 0


class TestCopCheck:
    def test_ratio_of_produced_to_consumed_is_a_cop(self) -> None:
        table = _feature_table(
            hp_heat_kwh=[0.1, 0.2, 0.0],
            hp_heat_out_kwh=[0.4, 0.8, 0.0],
            hp_dhw_kwh=[0.1, 0.0, 0.0],
            hp_dhw_out_kwh=[0.25, 0.0, 0.0],
            hp_cool_kwh=[0.0, 0.0, 0.0],
            hp_cool_out_kwh=[0.0, 0.0, 0.0],
            hp_total_reported_kwh=[0.2, 0.2, 0.0],
        )
        out = cop_check(table)
        assert out["heat"]["cop_ratio_of_sums"] == pytest.approx(4.0)
        assert out["dhw"]["cop_ratio_of_sums"] == pytest.approx(2.5)
        assert out["heat"]["slots_active"] == 2
        assert out["reported_total_vs_components"]["ratio_of_sums"] == pytest.approx(1.0)


class TestConstantRuns:
    def test_a_long_non_zero_constant_run_is_listed_and_zero_is_ignored(self) -> None:
        minutes = np.arange(0, 600)
        t = minutes.astype(np.int64) * 60_000_000
        v = np.where(minutes < 100, 3000.0, np.where(minutes < 300, 0.0, 41.0 + minutes % 7))
        runs = features.constant_runs(t, v, min_hours=1.5)
        assert runs["count"] == 1
        assert runs["longest"][0]["value"] == 3000.0
        assert runs["longest"][0]["hours"] == pytest.approx(99 / 60)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def _raw_rows(entity: str, metric: str, unit: str, start: datetime, values: list[float]):
    labels = json.dumps({"__name__": metric, "entity": entity, "job": "home-assistant"})
    key = compute_series_key({"__name__": metric, "entity": entity, "job": "home-assistant"})
    return [
        {
            "entity": entity,
            "metric": metric,
            "unit": unit,
            "series_key": key,
            "ts": start + timedelta(seconds=60 * i + 0.25),
            "value": value,
            "scrape_instance": "10.0.0.1:8080",
            "extraction_id": "11111111-1111-1111-1111-111111111111",
            "labels_json": labels,
        }
        for i, value in enumerate(values)
    ]


def _write_raw(data_dir: Path, rows: list[dict[str, Any]], *, entities: list[str]) -> str:
    table = pa.table(
        {k: [r[k] for r in rows] for k in RAW_SAMPLE.names},
        schema=RAW_SAMPLE,
    )
    name = f"prom-samples-20260105T000000Z-20260106T000000Z-{len(entities):08x}.parquet"
    path = parts_dir(data_dir, "prometheus") / name
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "manifest_version": 1,
        "extraction_id": f"ex-{sha[:12]}",
        "finished_at": "2026-01-06T00:00:00Z",
        "source": "prometheus",
        "entities": sorted(entities),
        "source_window_start": "2026-01-05T00:00:00Z",
        "source_window_end": "2026-01-06T00:00:00Z",
        "row_count": table.num_rows,
        "part_files": [
            {
                "path": f"raw/prometheus/v1/parts/{name}",
                "name": name,
                "sha256": sha,
                "rows": table.num_rows,
                "chunk_start": "2026-01-05T00:00:00Z",
                "chunk_end": "2026-01-06T00:00:00Z",
            }
        ],
    }
    manifests = data_dir / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / f"ex-{sha[:12]}.json").write_text(json.dumps(manifest))
    return sha


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    rows = _raw_rows(SERVERS, "hass_sensor_power_w", "W", T0, [240.0] * 121)
    rows += _raw_rows(HEAT, "hass_sensor_power_w", "W", T0, [0.0] * 60 + [800.0] * 61)
    rows += _raw_rows(ENABLE, "hass_input_boolean_state", "bool", T0, [1.0] * 121)
    _write_raw(directory, rows, entities=[SERVERS, HEAT, ENABLE])
    return directory


class TestBuildFeatures:
    WINDOW = ("2026-01-05T00:00:00Z", "2026-01-05T02:00:00Z")

    def test_writes_a_separate_feature_set_with_hashed_inputs(self, data_dir: Path) -> None:
        out = build_features(data_dir=str(data_dir), start=self.WINDOW[0], end=self.WINDOW[1])
        assert out.parent == data_dir / "analysis" / "v1" / "features"
        assert not (out / "build_manifest.json").exists()
        table, manifest = load_features(out)
        assert out.name == manifest["feature_set_id"]
        assert table.num_rows == 8
        # first hour: servers 240 W -> 0.06 kWh per slot, heat pump idle.
        assert table.column("servers_kwh").to_pylist()[0] == pytest.approx(0.06)
        assert table.column("hp_heat_kwh").to_pylist()[0] == pytest.approx(0.0)
        # second hour at 800 W -> 0.2 kWh per slot; the last sample ends the data, so the final
        # slot loses the last minute but stays above the coverage threshold.
        assert table.column("hp_heat_kwh").to_pylist()[5] == pytest.approx(0.2)
        # series that were not extracted stay unknown, not zero
        assert table.column("hp_dhw_kwh").null_count == 8
        assert manifest["inputs"]["raw_parts"][0]["sha256"]
        assert manifest["code_version"]
        assert manifest["params"]["max_gap_seconds"] == 300.0
        assert "Sample-and-hold" in manifest["integration_rule"]
        assert manifest["quality"]["series"]["servers"]["cadence_seconds"]["median"] == 60.0
        assert (
            "sensor.aquarea_dhw_power_consumed" in manifest["quality"]["entities_without_samples"]
        )

    def test_rebuilding_the_same_inputs_is_idempotent(self, data_dir: Path) -> None:
        first = build_features(data_dir=str(data_dir), start=self.WINDOW[0], end=self.WINDOW[1])
        second = build_features(data_dir=str(data_dir), start=self.WINDOW[0], end=self.WINDOW[1])
        assert first == second

    def test_a_different_parameter_gives_a_different_id(self, data_dir: Path) -> None:
        first = build_features(data_dir=str(data_dir), start=self.WINDOW[0], end=self.WINDOW[1])
        second = build_features(
            data_dir=str(data_dir),
            start=self.WINDOW[0],
            end=self.WINDOW[1],
            max_gap_seconds=120.0,
        )
        assert first != second

    def test_a_tampered_part_is_refused_on_load(self, data_dir: Path) -> None:
        out = build_features(data_dir=str(data_dir), start=self.WINDOW[0], end=self.WINDOW[1])
        part = next((out / "parts").glob("*.parquet"))
        part.write_bytes(part.read_bytes() + b"x")
        with pytest.raises(FeatureError, match="sha256"):
            load_features(out)

    def test_a_tampered_raw_part_is_refused(self, data_dir: Path) -> None:
        raw = next(parts_dir(data_dir, "prometheus").glob("*.parquet"))
        raw.write_bytes(raw.read_bytes() + b"x")
        with pytest.raises(FeatureError, match="sha256"):
            build_features(data_dir=str(data_dir), start=self.WINDOW[0], end=self.WINDOW[1])

    def test_no_raw_input_is_an_error(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(FeatureError, match="no raw parts"):
            build_features(data_dir=str(empty), start=self.WINDOW[0], end=self.WINDOW[1])

    def test_bad_parameters_are_refused(self, data_dir: Path) -> None:
        with pytest.raises(FeatureError, match="min_coverage"):
            build_features(
                data_dir=str(data_dir), start=self.WINDOW[0], end=self.WINDOW[1], min_coverage=0.0
            )
        with pytest.raises(FeatureError, match="max_gap"):
            build_features(
                data_dir=str(data_dir),
                start=self.WINDOW[0],
                end=self.WINDOW[1],
                max_gap_seconds=0,
            )


class TestSourceSwitch:
    """A signal with successors reads each entity only inside its own window."""

    NEW = "sensor.venus_ac_offgrid_power"
    SWITCH = T0 + timedelta(hours=1)
    SIGNAL = Signal(SERVERS, "servers", features.KIND_POWER, successors=((SWITCH, NEW),))
    WINDOW = ("2026-01-05T00:00:00Z", "2026-01-05T02:00:00Z")

    def _signals(self) -> tuple[Signal, ...]:
        """The default set with this test's switch instead of the real one."""
        return tuple(self.SIGNAL if s.alias == "servers" else s for s in features.SIGNALS)

    def _archive(self, tmp_path: Path) -> Path:
        directory = tmp_path / "data"
        directory.mkdir()
        # The old entity reads the servers (240 W) for an hour, then another load (150 W);
        # the new one reads something else (40 W) before the switch and the servers (300 W)
        # from it on.  Both sample every minute, 0.25 s past the minute.
        rows = _raw_rows(SERVERS, "hass_sensor_power_w", "W", T0, [240.0] * 60 + [150.0] * 61)
        rows += _raw_rows(self.NEW, "hass_sensor_power_w", "W", T0, [40.0] * 60 + [300.0] * 61)
        _write_raw(directory, rows, entities=[SERVERS, self.NEW])
        return directory

    def test_entity_windows_split_at_each_switch(self) -> None:
        later = self.SWITCH + timedelta(days=1)
        sig = Signal(SERVERS, "servers", features.KIND_POWER, successors=((self.SWITCH, self.NEW),))
        assert sig.entity_windows() == ((SERVERS, None, self.SWITCH), (self.NEW, self.SWITCH, None))
        three = Signal("a", "x", features.KIND_POWER, successors=((self.SWITCH, "b"), (later, "c")))
        assert [w[0] for w in three.entity_windows()] == ["a", "b", "c"]
        assert three.entity_windows()[1] == ("b", self.SWITCH, later)
        assert POWER_SIGNAL.entity_windows() == ((SERVERS, None, None),)

    @pytest.mark.parametrize(
        "successors",
        [
            ((datetime(2026, 1, 5, 1), "b"),),  # naive
            ((T0 + timedelta(hours=2), "b"), (T0 + timedelta(hours=1), "c")),  # out of order
            ((T0, "b"), (T0, "c")),  # repeated
        ],
    )
    def test_bad_successors_are_refused(self, successors: Any) -> None:
        with pytest.raises(ValueError, match="successors"):
            Signal("a", "x", features.KIND_POWER, successors=successors)

    def test_each_entity_feeds_the_alias_only_inside_its_window(self, tmp_path: Path) -> None:
        data_dir = self._archive(tmp_path)
        series, _parts, _ids = features.load_signal_samples(
            data_dir,
            (self.SIGNAL,),
            window_start=datetime(2026, 1, 5, tzinfo=UTC),
            window_end=datetime(2026, 1, 5, 2, tzinfo=UTC),
        )
        raw = series[SERVERS]
        switch_us = int(self.SWITCH.timestamp() * 1_000_000)
        assert set(raw.values[raw.t_us < switch_us]) == {240.0}
        assert set(raw.values[raw.t_us >= switch_us]) == {300.0}
        assert raw.t_us.size == 120  # the 02:00:00.25 sample is past the window
        assert raw.entity == SERVERS and raw.series_keys == 2

    def test_the_table_is_continuous_across_the_switch(self, tmp_path: Path) -> None:
        data_dir = self._archive(tmp_path)
        out = build_features(
            data_dir=str(data_dir),
            start=self.WINDOW[0],
            end=self.WINDOW[1],
            signals=self._signals(),
        )
        table, manifest = load_features(out)
        kwh = table.column("servers_kwh").to_pylist()
        # The last old reading (00:59:00.25) holds until the first new one (01:00:00.25).
        assert kwh[:4] == pytest.approx([0.06] * 4)
        assert kwh[4] == pytest.approx((240 * 0.25 + 300 * 899.75) / 3.6e6)
        assert kwh[5:7] == pytest.approx([0.075] * 2)
        # Slot 0 lacks the 0.25 s before the first sample; the switch costs no coverage.
        assert table.column("servers_quality").to_pylist()[1:7] == ["measured"] * 6
        assert manifest["signals"][0]["successors"] == [
            {"since": "2026-01-05T01:00:00Z", "entity": self.NEW}
        ]

    def test_a_window_before_the_switch_reads_only_the_first_entity(self, tmp_path: Path) -> None:
        data_dir = self._archive(tmp_path)
        out = build_features(
            data_dir=str(data_dir),
            start="2026-01-05T00:00:00Z",
            end="2026-01-05T00:45:00Z",
            signals=self._signals(),
        )
        table, _ = load_features(out)
        assert table.column("servers_kwh").to_pylist() == pytest.approx([0.06] * 3)

    def test_the_successors_are_part_of_the_feature_set_id(self) -> None:
        kwargs: dict[str, Any] = {
            "part_hashes": [],
            "code_ver": "v",
            "window_start": T0,
            "window_end": T0 + timedelta(hours=1),
            "params": {},
        }
        later = Signal(
            SERVERS, "servers", features.KIND_POWER, successors=((T0 + timedelta(hours=2), "b"),)
        )
        ids = {
            features.compute_feature_set_id(**kwargs, signals=(s,))
            for s in (POWER_SIGNAL, self.SIGNAL, later)
        }
        assert len(ids) == 3

    def test_the_servers_moved_to_the_battery_backup_port(self) -> None:
        sig = features.SERVERS_SIGNAL
        assert sig.alias == "servers"
        assert sig.entity_windows() == (
            (SERVERS, None, features.SERVERS_ON_BACKUP_PORT_SINCE),
            (self.NEW, features.SERVERS_ON_BACKUP_PORT_SINCE, None),
        )
        # Inside the Prometheus scrape gap that brackets the rewiring.
        assert (
            datetime(2026, 11, 3, 22, 4, 31, tzinfo=UTC)
            < features.SERVERS_ON_BACKUP_PORT_SINCE
            <= datetime(2026, 11, 3, 22, 11, 43, tzinfo=UTC)
        )
        assert sig in features.SIGNALS and sig in features.CLIMATE_SIGNALS
        assert self.NEW in FEATURE_ENTITY_IDS and self.NEW not in CLIMATE_ENTITY_IDS
        assert ENTITY_SPECS[self.NEW].metric == "hass_sensor_power_w"
        assert ENTITY_SPECS[self.NEW].unit == "W"


class TestCli:
    def test_build_features_subcommand(
        self, data_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "build-features",
                "--data-dir",
                str(data_dir),
                "--start",
                "2026-01-05T00:00:00Z",
                "--end",
                "2026-01-05T02:00:00Z",
            ]
        )
        assert rc == 0
        assert "feature set directory" in capsys.readouterr().out

    def test_user_errors_are_one_line_exit_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "build-features",
                "--data-dir",
                str(tmp_path),
                "--start",
                "2026-01-05T00:00:00Z",
                "--end",
                "2026-01-05T02:00:00Z",
            ]
        )
        assert rc == 1
        assert "no raw parts" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The default signal set is frozen; the climate set is separate
# ---------------------------------------------------------------------------


class TestDefaultSetIsPinned:
    def test_signal_list_and_feature_set_id_are_unchanged(self) -> None:
        assert [(s.entity, s.alias, s.kind) for s in features.SIGNALS] == [
            ("sensor.shellypmmini_servers_power", "servers", "power"),
            ("sensor.aquarea_heat_power_consumed", "hp_heat", "power"),
            ("sensor.aquarea_dhw_power_consumed", "hp_dhw", "power"),
            ("sensor.aquarea_cool_power_consumed", "hp_cool", "power"),
            ("sensor.aquarea_heat_power_produced", "hp_heat_out", "power"),
            ("sensor.aquarea_dhw_power_produced", "hp_dhw_out", "power"),
            ("sensor.aquarea_cool_power_produced", "hp_cool_out", "power"),
            ("sensor.aquarea_power_consumed", "hp_total_reported", "power"),
            ("sensor.smart_plug_v2_terrace_power", "washer_plug", "power"),
            ("sensor.aquarea_outside_ambient_temperature", "outdoor_temp", "temperature"),
            ("input_boolean.appdaemon_climate_enable", "climate_enable", "state"),
            ("input_boolean.appdaemon_dhw_enable", "dhw_enable", "state"),
            ("input_number.appdaemon_climate_min_hours_per_day", "climate_min_hours", "state"),
            ("switch.aquarea_force_dhw_mode", "dhw_force", "state"),
            ("sensor.terrace_lavadora_energy_today", "washer_energy_today_wh", "counter"),
        ]
        assert all(s.metric is None and s.source == s.entity for s in features.SIGNALS)
        # A fixed input gives this id.  Last changed when the servers signal gained its
        # successor (servers on the battery backup port from 2026-11-03); before that it was
        # 8cd8263e440a..., computed with the code before the climate set existed.
        fixed = features.compute_feature_set_id(
            part_hashes=[{"part": "p1", "sha256": "aa"}, {"part": "p2", "sha256": "bb"}],
            code_ver="test-version",
            window_start=T0,
            window_end=T0 + timedelta(days=1),
            params={
                "interval_seconds": 900,
                "max_gap_seconds": 300.0,
                "min_coverage": 0.8,
                "lookback_seconds": 3600.0,
            },
            signals=features.SIGNALS,
        )
        assert fixed == "89ba8f272b22fb9ee2b6472557aa10dbc97d95ae26361d7aa2b70f63338047fe"

    def test_the_selector_defaults_to_the_default_set(self) -> None:
        assert features.SIGNAL_SETS["default"] is features.SIGNALS
        assert features.SIGNAL_SETS["climate"] is features.CLIMATE_SIGNALS
        assert build_features.__kwdefaults__["signals"] is features.SIGNALS


class TestClimateSignals:
    def test_aliases_and_columns_are_unique(self) -> None:
        aliases = [s.alias for s in features.CLIMATE_SIGNALS]
        assert len(set(aliases)) == len(aliases)
        columns = features.feature_schema(features.CLIMATE_SIGNALS).names
        assert len(set(columns)) == len(columns)

    def test_rooms_are_named_generically_from_the_one_room_mapping(self) -> None:
        by_alias = {s.alias: s for s in features.CLIMATE_SIGNALS}
        for room, entity in CLIMATE_ROOMS.items():
            temp, setpoint = by_alias[f"room_{room}_temp"], by_alias[f"room_{room}_setpoint"]
            assert temp.entity == setpoint.entity == entity
            assert temp.metric != setpoint.metric
            assert temp.source != setpoint.source
        assert features.COMFORT_EXCLUDED_ROOMS == {"office_a"}
        assert features.COMFORT_EXCLUDED_ROOMS <= set(CLIMATE_ROOMS)

    def test_every_climate_entity_is_in_the_climate_group_or_shared_with_the_default_set(
        self,
    ) -> None:
        default_entities = {s.entity for s in features.SIGNALS}
        for sig in features.CLIMATE_SIGNALS:
            assert (sig.entity in CLIMATE_ENTITY_IDS) != (sig.entity in default_entities)

    def test_every_climate_entity_is_a_registered_feature_entity(self) -> None:
        registered = {(s.entity_id, s.metric) for s in ENTITY_SPECS.values()}
        for sig in features.CLIMATE_SIGNALS:
            assert sig.entity in FEATURE_ENTITY_IDS
            if sig.metric is not None:
                assert (sig.entity, sig.metric) in registered

    def test_cooling_power_and_energy_are_included(self) -> None:
        aliases = {s.alias for s in features.CLIMATE_SIGNALS}
        assert {"hp_cool", "hp_cool_out", "hp_cool_energy", "hp_cool_energy_out"} <= aliases

    def test_the_id_depends_on_the_signal_set_and_a_pinned_metric(self) -> None:
        kwargs: dict[str, Any] = {
            "part_hashes": [],
            "code_ver": "v",
            "window_start": T0,
            "window_end": T0 + timedelta(hours=1),
            "params": {},
        }
        base = Signal(SERVERS, "servers", features.KIND_TEMPERATURE)
        pinned = Signal(SERVERS, "servers", features.KIND_TEMPERATURE, "m")
        ids = {
            features.compute_feature_set_id(**kwargs, signals=sigs)
            for sigs in (features.SIGNALS, features.CLIMATE_SIGNALS, (base,), (pinned,))
        }
        assert len(ids) == 4


class TestCounterDelta:
    COUNTER = Signal("sensor.x", "starts", features.KIND_COUNTER_DELTA)

    def _minutes(self, values: list[float], start_minute: int = 0) -> tuple[np.ndarray, np.ndarray]:
        base = int(T0.timestamp()) * 1_000_000
        t = base + (start_minute + np.arange(len(values), dtype=np.int64)) * 60_000_000
        return t, np.array(values, dtype=np.float64)

    def test_rates_treat_a_decrease_as_a_reset_to_zero(self) -> None:
        rates, resets, rollbacks = features.counter_rates(
            np.array([0.0, 60.0, 120.0, 180.0]), np.array([10.0, 11.0, 2.0, 3.0])
        )
        # 10 -> 11: +1; 11 -> 2: counter restarted, +2 since the restart; 2 -> 3: +1
        assert (rates[:3] * 60.0).tolist() == pytest.approx([1.0, 2.0, 1.0])
        assert (resets, rollbacks) == (1, 0)

    def test_a_small_decrease_is_a_rollback_that_adds_nothing(self) -> None:
        # the live energy totals slip back by tenths of a kWh; that is not a restart from zero
        rates, resets, rollbacks = features.counter_rates(
            np.array([0.0, 60.0, 120.0, 180.0]), np.array([2100.2, 2100.7, 2100.6, 2101.0])
        )
        assert (rates[:3] * 60.0).tolist() == pytest.approx([0.5, 0.0, 0.4])
        assert (resets, rollbacks) == (0, 1)

    def test_a_single_sample_has_no_increase(self) -> None:
        rates, resets, rollbacks = features.counter_rates(np.array([0.0]), np.array([5.0]))
        assert rates.tolist() == [0.0]
        assert (resets, rollbacks) == (0, 0)

    def test_a_steady_counter_gives_its_increase_per_slot(self) -> None:
        t, v = self._minutes([100.0 + 0.01 * i for i in range(61)])
        cols = signal_columns(
            self.COUNTER, t, v, us_edges(4), max_gap_seconds=300.0, min_coverage=0.8
        )
        assert cols["starts_delta"].tolist() == pytest.approx([0.15] * 4)
        assert cols["starts_quality"].tolist() == ["measured"] * 4

    def test_the_total_increase_survives_a_reset(self) -> None:
        values = [10.0] * 30 + [11.0] * 60 + [2.0] * 31
        t, v = self._minutes(values)
        cols = signal_columns(
            self.COUNTER, t, v, us_edges(8), max_gap_seconds=300.0, min_coverage=0.8
        )
        # +1 at minute 30, then a reset at minute 90 worth the 2 starts counted since.
        assert np.nansum(cols["starts_delta"]) == pytest.approx(3.0, abs=0.02)

    def test_a_gap_is_unknown_not_zero_and_not_the_whole_increase(self) -> None:
        # the counter is read for 15 minutes, then not for 30 minutes, then read again
        first, v1 = self._minutes([float(i) for i in range(16)])
        second, v2 = self._minutes([500.0 + i for i in range(10)], start_minute=45)
        cols = signal_columns(
            self.COUNTER,
            np.concatenate([first, second]),
            np.concatenate([v1, v2]),
            us_edges(4),
            max_gap_seconds=300.0,
            min_coverage=0.8,
        )
        assert cols["starts_quality"][1] == "unknown"
        assert np.isnan(cols["starts_delta"][1])
        # slot 0 holds the 15 increases of its 15 covered minutes; slot 3 has 9 of its 15
        # minutes covered (unknown), and the 485 increase accrued across the gap is in no slot
        assert np.nansum(cols["starts_delta"]) == pytest.approx(15.0)
        assert cols["starts_delta"][0] == pytest.approx(15.0)

    def test_no_samples_is_unknown(self) -> None:
        cols = signal_columns(
            self.COUNTER,
            np.zeros(0, dtype=np.int64),
            np.zeros(0),
            us_edges(2),
            max_gap_seconds=300.0,
            min_coverage=0.8,
        )
        assert np.isnan(cols["starts_delta"]).all()
        assert cols["starts_quality"].tolist() == ["unknown"] * 2

    def test_gauge_is_the_time_weighted_mean(self) -> None:
        base = int(T0.timestamp()) * 1_000_000
        t = base + np.array([0, 450, 900], dtype=np.int64) * 1_000_000
        cols = signal_columns(
            Signal("sensor.f", "freq", features.KIND_GAUGE),
            t,
            np.array([10.0, 30.0, 30.0]),
            us_edges(1),
            max_gap_seconds=1000.0,
            min_coverage=0.8,
        )
        assert cols["freq_mean"][0] == pytest.approx(20.0)
        assert cols["freq_coverage"][0] == pytest.approx(1.0)


ROOM = "climate.thermostat_bedroom"
OFFICE_A = "climate.thermostat_office_a"
STARTS = "sensor.aquarea_compressor_start_stop_counter"
HEAT_ENERGY = "sensor.aquarea_heat_energy_consumed"
FREQ = "sensor.aquarea_compressor_frequency"
CURRENT = "hass_climate_current_temperature_celsius"
TARGET = "hass_climate_target_temperature_celsius"


CLIMATE_ENTITIES = [ROOM, OFFICE_A, STARTS, HEAT_ENERGY, FREQ]


def _climate_rows() -> list[dict[str, Any]]:
    """Raw rows of the climate extraction group only (none of the earlier feature entities)."""
    n = 121
    degc = "\u00b0C"
    rows = _raw_rows(ROOM, CURRENT, degc, T0, [20.0] * n)
    rows += _raw_rows(ROOM, TARGET, degc, T0, [22.0] * n)
    rows += _raw_rows(OFFICE_A, CURRENT, degc, T0, [24.0] * n)  # no setpoint series
    starts = [10.0] * 30 + [11.0] * 60 + [2.0] * 31
    rows += _raw_rows(STARTS, "hass_sensor_state", "count", T0, starts)
    energy = [100.0 + 0.01 * i for i in range(n)]
    rows += _raw_rows(HEAT_ENERGY, "hass_sensor_energy_kwh", "kWh", T0, energy)
    rows += _raw_rows(FREQ, "hass_sensor_unit_hz", "Hz", T0, [40.0] * n)
    return rows


@pytest.fixture
def climate_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "climate-data"
    directory.mkdir()
    _write_raw(directory, _climate_rows(), entities=CLIMATE_ENTITIES)
    return directory


class TestBuildClimateFeatures:
    WINDOW = ("2026-01-05T00:00:00Z", "2026-01-05T02:00:00Z")

    def _build(self, data_dir: Path, **kwargs: Any) -> Path:
        return build_features(
            data_dir=str(data_dir),
            start=self.WINDOW[0],
            end=self.WINDOW[1],
            signals=features.CLIMATE_SIGNALS,
            **kwargs,
        )

    def test_both_series_of_one_thermostat_land_in_their_own_columns(
        self, climate_dir: Path
    ) -> None:
        table, manifest = load_features(self._build(climate_dir))
        assert table.schema.equals(features.feature_schema(features.CLIMATE_SIGNALS))
        assert table.column("room_bedroom_temp_mean_c").to_pylist()[1] == pytest.approx(20.0)
        assert table.column("room_bedroom_setpoint_mean_c").to_pylist()[1] == pytest.approx(22.0)
        assert table.column("room_office_a_temp_mean_c").to_pylist()[1] == pytest.approx(24.0)
        assert table.column("compressor_freq_mean").to_pylist()[1] == pytest.approx(40.0)
        assert table.column("hp_heat_energy_delta").to_pylist()[2] == pytest.approx(0.15)
        assert manifest["quality"]["series"]["room_bedroom_setpoint"]["metric"] == TARGET
        assert manifest["quality"]["series"]["room_bedroom_temp"]["metric"] == CURRENT
        by_alias = {s["alias"]: s for s in manifest["signals"]}
        assert by_alias["room_bedroom_setpoint"]["unit"] == "°C"
        assert by_alias["compressor_starts"]["unit"] == "count"

    def test_missing_series_are_unknown_and_named_not_zero_filled(self, climate_dir: Path) -> None:
        table, manifest = load_features(self._build(climate_dir))
        assert table.column("room_office_a_setpoint_mean_c").null_count == table.num_rows
        assert table.column("hp_cool_kwh").null_count == table.num_rows
        assert set(table.column("hp_cool_quality").to_pylist()) == {"unknown"}
        missing = manifest["quality"]["entities_without_samples"]
        assert f"{OFFICE_A}#setpoint" in missing
        # every name is a token `extract --entities` accepts
        assert resolve_entity_specs(missing)
        assert "sensor.aquarea_cool_power_consumed" in missing
        assert ROOM not in missing

    def test_counter_resets_are_reported_and_deltas_keep_the_total(self, climate_dir: Path) -> None:
        table, manifest = load_features(self._build(climate_dir))
        quality = manifest["quality"]["series"]["compressor_starts"]
        assert (quality["counter_resets"], quality["counter_rollbacks"]) == (1, 0)
        assert quality["slots_measured"] + quality["slots_partial"] > 0
        deltas = table.column("compressor_starts_delta").to_pylist()
        assert sum(d for d in deltas if d is not None) == pytest.approx(3.0, abs=0.02)

    def test_room_roles_are_recorded_as_data_only(self, climate_dir: Path) -> None:
        _table, manifest = load_features(self._build(climate_dir))
        rooms = manifest["climate"]["rooms"]
        assert rooms["office_a"]["comfort"] is False
        assert rooms["bedroom"] == {"entity": ROOM, "comfort": True}
        assert rooms["office_t"]["comfort"] is True

    def test_a_climate_extraction_leaves_the_default_set_unchanged(self, data_dir: Path) -> None:
        window = ("2026-01-05T00:00:00Z", "2026-01-05T02:00:00Z")
        before = build_features(data_dir=str(data_dir), start=window[0], end=window[1])
        table_before, manifest_before = load_features(before)

        # a climate extraction over an overlapping window, into the same store
        _write_raw(data_dir, _climate_rows(), entities=CLIMATE_ENTITIES)
        after = build_features(data_dir=str(data_dir), start=window[0], end=window[1])
        table_after, manifest_after = load_features(after)

        assert after == before  # same feature_set_id, so the same directory
        assert manifest_after["feature_set_id"] == manifest_before["feature_set_id"]
        assert manifest_after["inputs"] == manifest_before["inputs"]
        assert table_after.equals(table_before)

    def test_the_climate_set_reads_both_extractions(self, data_dir: Path) -> None:
        _write_raw(data_dir, _climate_rows(), entities=CLIMATE_ENTITIES)
        out = self._build(data_dir)
        table, manifest = load_features(out)
        # from the climate extraction
        assert table.column("room_bedroom_temp_mean_c").to_pylist()[1] == pytest.approx(20.0)
        assert table.column("compressor_freq_mean").to_pylist()[1] == pytest.approx(40.0)
        # from the earlier feature extraction (heat power 0 W, then 800 W; climate_enable on)
        assert table.column("hp_heat_kwh").to_pylist()[5] == pytest.approx(0.2)
        assert table.column("servers_kwh").to_pylist()[1] == pytest.approx(0.06)
        assert table.column("climate_enable_at_start").to_pylist()[1] == 1.0
        assert len(manifest["inputs"]["raw_extraction_ids"]) == 2
        assert (
            "sensor.aquarea_heat_power_consumed"
            not in (manifest["quality"]["entities_without_samples"])
        )

    def test_the_climate_set_has_its_own_id_and_no_default_manifest_key(
        self, data_dir: Path, climate_dir: Path
    ) -> None:
        default_out = build_features(
            data_dir=str(data_dir), start="2026-01-05T00:00:00Z", end="2026-01-05T02:00:00Z"
        )
        _table, default_manifest = load_features(default_out)
        assert "climate" not in default_manifest
        climate_out = self._build(climate_dir)
        assert climate_out.name != default_out.name

    def test_the_cop_check_runs_without_the_reported_total(self, climate_dir: Path) -> None:
        _table, manifest = load_features(self._build(climate_dir))
        assert set(manifest["quality"]["cop_check"]) == {"heat", "dhw", "cool"}

    def test_the_main_meter_check_needs_the_servers_and_heat_pump_signals(
        self, climate_dir: Path
    ) -> None:
        needed = {"servers_kwh", "hp_heat_kwh", "hp_dhw_kwh", "hp_cool_kwh"}
        assert needed <= set(features.feature_schema(features.CLIMATE_SIGNALS).names)
        with pytest.raises(FeatureError, match="servers and heat-pump power"):
            build_features(
                data_dir=str(climate_dir),
                start=self.WINDOW[0],
                end=self.WINDOW[1],
                signals=features.CLIMATE_SIGNALS[:2],
                dataset=str(climate_dir),
            )

    def test_cli_selects_the_climate_set(
        self, climate_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["build-features", "--data-dir", str(climate_dir)]
        argv += ["--start", self.WINDOW[0], "--end", self.WINDOW[1], "--signal-set", "climate"]
        assert main(argv) == 0
        out = capsys.readouterr().out.strip().split(": ", 2)[-1]
        _table, manifest = load_features(out)
        assert "climate" in manifest
        assert out.endswith(manifest["feature_set_id"])
