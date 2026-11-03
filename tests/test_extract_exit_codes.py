"""``extract`` exit codes for coverage gaps, and the CLI's httpx log quieting.

Offline only: ``httpx.MockTransport`` for the real HA extractor, stubbed extractors for the
Prometheus/``--source all`` paths.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from battery_optimizer import cli
from battery_optimizer._logging import quiet_http_loggers
from battery_optimizer.cli import main
from battery_optimizer.coverage import (
    COVERAGE_GAP_PREFIXES,
    EXIT_COVERAGE_GAPS,
    coverage_gap_flags,
    format_gap_summary,
)
from battery_optimizer.extract_ha import (
    CARRY_IN_DISAGREEMENT_FLAG_PREFIX,
    CHUNK_ACCOUNTING_FLAG_PREFIX,
    DUPLICATE_ROWS_FLAG_PREFIX,
    EMPTY_CHUNK_FLAG_PREFIX,
    FILTERED_CHUNK_FLAG_PREFIX,
    PREEXISTING_DUPLICATE_ROWS_FLAG_PREFIX,
    PRIOR_PART_UNREADABLE_FLAG_PREFIX,
)
from battery_optimizer.manifest import new_manifest, write_manifest

CFG = str(Path(__file__).resolve().parent.parent / "config" / "tariff.yaml")
ENTITY = "sensor.lavadora_current_status"

GAP_FLAGS = [
    f"{EMPTY_CHUNK_FLAG_PREFIX}:[2026-01-02T00:00:00Z, 2026-01-03T00:00:00Z): 0 rows kept",
    f"{FILTERED_CHUNK_FLAG_PREFIX}:[2026-01-02T00:00:00Z, 2026-01-03T00:00:00Z): 0 rows kept",
    f"{CHUNK_ACCOUNTING_FLAG_PREFIX}: declared=2 parts_written=1 windows=none",
    "no_rows_for_entity:sensor.x",
    "no_rows_extracted:all",
    "no_samples_extracted:sensor.x",
    "no_samples_extracted:all",
    "no_series_chunks:sensor.x:2/9",
    "series_without_samples_chunks:sensor.x:1/9",
    "omie_missing:2026-01-15: HTML response instead of a price file",
    "omie_parse_error:2026-01-15: 23 periods do not fit",
    "omie_fetch_failed:2026-01-15: HTTP 503 after 4 attempt(s)",
    "omie_part_unreadable:2026-01-15: omie-day-x.parquet: OSError",
]
INFORMATIONAL_FLAGS = [
    f"{DUPLICATE_ROWS_FLAG_PREFIX}:total: rows_seen=3",
    f"{CARRY_IN_DISAGREEMENT_FLAG_PREFIX}:[a, b): 1 row",
    f"{PREEXISTING_DUPLICATE_ROWS_FLAG_PREFIX}:12: 51 row(s)",
    f"{PRIOR_PART_UNREADABLE_FLAG_PREFIX}:part.parquet: OSError",
    "duplicate_samples_dropped:sensor.x:3",
    "boundary_duplicates_dropped:sensor.x:1",
    "non_finite_samples_dropped:sensor.x:2",
    "overlapping_target_samples:sensor.x:1",
    "dead_duplicate_entity:sensor.esios_pvpc_2",
    "retired_entity_chunks_skipped:sensor.shellypmmini_servers_power:1/3",
    "omie_not_published_yet:2026-01-16: HTML response instead of a price file",
    "omie_orphan_part_adopted:2026-01-15: omie-day-x.parquet",
    "omie_price_outside_expected_range:2026-01-15: 1 price(s) outside [-500, 4000] EUR/MWh",
]


class TestCoverageClassification:
    def test_gap_flags_are_gaps(self):
        assert coverage_gap_flags(GAP_FLAGS) == GAP_FLAGS

    def test_informational_flags_are_not_gaps(self):
        assert coverage_gap_flags(INFORMATIONAL_FLAGS) == []

    def test_every_gap_prefix_is_exercised(self):
        assert {f.partition(":")[0] for f in GAP_FLAGS} == set(COVERAGE_GAP_PREFIXES)

    def test_prefix_match_is_at_a_colon_boundary(self):
        assert coverage_gap_flags(["empty_chunks_extra:x", "no_rows_for_entity_x:y"]) == []

    def test_known_dead_duplicate_entity_is_expected_to_be_empty(self):
        flags = [
            "no_samples_extracted:sensor.esios_pvpc_2",
            "no_series_chunks:sensor.esios_pvpc_2:9/9",
            "no_samples_extracted:sensor.real",
        ]
        assert coverage_gap_flags(flags) == ["no_samples_extracted:sensor.real"]

    def test_summary_names_every_flag_with_its_window(self):
        text = format_gap_summary("ha-history", "/m/x.json", GAP_FLAGS[:2])
        assert "2 coverage gap flag(s)" in text
        assert text.count("2026-01-02T00:00:00Z") == 2


def _ha_handler(request: httpx.Request) -> httpx.Response:
    """Rows only for the chunk starting 2026-01-01; every later chunk is an empty payload."""
    if "/2026-01-01T00:00:00" in request.url.path:
        ts = "2026-01-01T12:00:00Z"
        row = {
            "entity_id": ENTITY,
            "state": "idle",
            "attributes": {},
            "last_changed": ts,
            "last_updated": ts,
        }
        return httpx.Response(200, json=[[row]])
    return httpx.Response(200, json=[])


@pytest.fixture
def ha_env(tmp_path, monkeypatch):
    import battery_optimizer.extract_ha as eh

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("BATTERY_OPTIMIZER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("BATTERY_OPTIMIZER_HA_TOKEN", "mock-token-xyz")
    real_client = httpx.Client

    def mock_client(**kw):
        return real_client(transport=httpx.MockTransport(_ha_handler), timeout=kw.get("timeout"))

    monkeypatch.setattr(eh.httpx, "Client", mock_client)
    return data_dir


def _extract(*extra: str, end: str) -> list[str]:
    return [
        "extract",
        "--source",
        "ha-history",
        "--config",
        CFG,
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        end,
        "--entities",
        ENTITY,
        *extra,
    ]


def _manifests(data_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((data_dir / "manifests").glob("*.json"))]


class TestHaExtractExitCode:
    def test_clean_run_exits_zero(self, ha_env, capsys):
        assert main(_extract(end="2026-01-02T00:00:00Z")) == 0
        assert coverage_gap_flags(_manifests(ha_env)[0]["quality_flags"]) == []
        assert "coverage gap" not in capsys.readouterr().err

    def test_empty_chunk_exits_with_coverage_code_and_keeps_data(self, ha_env, capsys):
        rc = main(_extract(end="2026-01-03T00:00:00Z"))
        assert rc == EXIT_COVERAGE_GAPS == 3
        err = capsys.readouterr().err
        assert "ha-history recorded" in err
        assert "empty_chunk:[2026-01-02T00:00:00Z, 2026-01-03T00:00:00Z)" in err
        assert "chunk_accounting_mismatch" in err
        assert "--allow-coverage-gaps" in err
        # The manifest and the part are still committed, so the run stays inspectable.
        (manifest,) = _manifests(ha_env)
        assert manifest["row_count"] == 1
        assert manifest["part_files"]
        assert any(f.startswith("empty_chunk:") for f in manifest["quality_flags"])

    def test_allow_coverage_gaps_exits_zero_but_still_reports(self, ha_env, capsys):
        rc = main(_extract("--allow-coverage-gaps", end="2026-01-03T00:00:00Z"))
        assert rc == 0
        err = capsys.readouterr().err
        assert "empty_chunk:" in err
        assert "ignored (--allow-coverage-gaps)" in err
        assert len(_manifests(ha_env)) == 1


class TestSourceAll:
    @staticmethod
    def _stub(monkeypatch, tmp_path, *, prom_flags, ha_flags):
        import battery_optimizer.extract_ha as eh
        import battery_optimizer.extract_prom as ep
        from battery_optimizer.config import resolve_config_path

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("BATTERY_OPTIMIZER_DATA_DIR", str(data_dir))
        calls: list[str] = []

        def make(source, flags):
            def run(**_kw):
                calls.append(source)
                m = new_manifest(
                    source=source,
                    config_path=resolve_config_path(CFG),
                    params={},
                    entities=["sensor.x"],
                    source_window_start="2026-01-01T00:00:00Z",
                    source_window_end="2026-01-02T00:00:00Z",
                    started_at="2026-01-02T00:00:00Z",
                )
                m.finished_at = "2026-01-02T00:01:00Z"
                m.quality_flags = list(flags)
                return write_manifest(data_dir, m)

            return run

        monkeypatch.setattr(ep, "extract_prometheus", make("prometheus", prom_flags))
        monkeypatch.setattr(eh, "extract_ha_history", make("ha_history", ha_flags))
        return calls

    ARGV = ["extract", "--source", "all", "--lookback-days", "1", "--config", CFG]

    def test_prometheus_gap_still_runs_ha_then_fails(self, monkeypatch, tmp_path, capsys):
        calls = self._stub(
            monkeypatch,
            tmp_path,
            prom_flags=["no_series_chunks:sensor.x:1/1"],
            ha_flags=[],
        )
        assert main(self.ARGV) == EXIT_COVERAGE_GAPS
        assert calls == ["prometheus", "ha_history"]
        err = capsys.readouterr().err
        assert "prometheus recorded 1 coverage gap" in err
        assert "ha-history recorded" not in err

    def test_informational_flags_exit_zero(self, monkeypatch, tmp_path):
        self._stub(
            monkeypatch,
            tmp_path,
            prom_flags=["duplicate_samples_dropped:sensor.x:3", "non_finite_samples_dropped:a:1"],
            ha_flags=[f"{DUPLICATE_ROWS_FLAG_PREFIX}:total: rows_seen=3"],
        )
        assert main(self.ARGV) == 0

    def test_both_sources_listed_and_opt_out(self, monkeypatch, tmp_path, capsys):
        self._stub(
            monkeypatch,
            tmp_path,
            prom_flags=["no_samples_extracted:all"],
            ha_flags=["no_rows_for_entity:sensor.x"],
        )
        assert main([*self.ARGV, "--allow-coverage-gaps"]) == 0
        err = capsys.readouterr().err
        assert "prometheus recorded" in err and "ha-history recorded" in err


@pytest.fixture
def restore_http_loggers():
    names = ("httpx", "httpcore")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


class TestHttpLoggers:
    def test_helper_quiets_both_loggers(self, restore_http_loggers):
        for n in ("httpx", "httpcore"):
            logging.getLogger(n).setLevel(logging.NOTSET)
        quiet_http_loggers()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING

    def test_helper_verbose_leaves_loggers_alone(self, restore_http_loggers):
        for n in ("httpx", "httpcore"):
            logging.getLogger(n).setLevel(logging.NOTSET)
        quiet_http_loggers(verbose=True)
        assert logging.getLogger("httpx").level == logging.NOTSET
        assert logging.getLogger("httpcore").level == logging.NOTSET

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["extract", "--source", "prometheus"], logging.WARNING),
            (["extract", "--source", "prometheus", "--verbose"], logging.NOTSET),
            (["extract", "--source", "prometheus", "-v"], logging.NOTSET),
        ],
    )
    def test_cli_sets_http_logger_level(self, monkeypatch, restore_http_loggers, argv, expected):
        for n in ("httpx", "httpcore"):
            logging.getLogger(n).setLevel(logging.NOTSET)
        seen: dict[str, int] = {}

        def handler(_args):
            seen["httpx"] = logging.getLogger("httpx").level
            seen["httpcore"] = logging.getLogger("httpcore").level
            return 0

        monkeypatch.setitem(cli._HANDLERS, "extract", handler)
        assert main(argv) == 0
        assert seen == {"httpx": expected, "httpcore": expected}
