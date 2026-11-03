"""CLI entry point — argparse subcommands for the battery-optimizer toolkit."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from battery_optimizer._logging import quiet_http_loggers

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    """Add common options shared by data subcommands."""
    p.add_argument(
        "--data-dir",
        default=None,
        help="Data directory (default: $BATTERY_OPTIMIZER_DATA_DIR or ~/data/energy-baseline)",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to tariff config YAML (required for real runs; "
        "default: $BATTERY_OPTIMIZER_CONFIG or config/tariff.yaml relative to CWD)",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging, including httpx/httpcore per-request lines "
        "(hidden by default: ~1 INFO line per HTTP request)",
    )


def _add_time_args(p: argparse.ArgumentParser) -> None:
    """Add --start/--end or --lookback-days time window arguments."""
    time_group = p.add_mutually_exclusive_group()
    time_group.add_argument("--start", default=None, help="Start time (ISO 8601 UTC)")
    time_group.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Extract the last N full days (end=now UTC, start=N days ago rounded to day)",
    )
    p.add_argument("--end", default=None, help="End time (ISO 8601 UTC, default: now)")


def _add_forecast_eval_parser(subs: Any) -> None:
    """The ``forecast-eval`` subcommand (P1 of the load-forecaster plan)."""
    p = subs.add_parser(
        "forecast-eval",
        help="Pre-registered load-forecaster evaluation: accuracy and paired EUR against LF-0",
    )
    _add_common(p)
    p.add_argument("--dataset", required=True, help="Built dataset directory")
    p.add_argument(
        "--name",
        required=True,
        help="Run name; the run is written to <dataset>/forecast-eval/<name>-<UTC timestamp>",
    )
    p.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="NAME[:KEY=VALUE,...]",
        help="Registered load forecaster to evaluate, with optional param overrides "
        "(repeatable). trailing_mean (LF-0) is always the reference",
    )
    p.add_argument(
        "--period",
        action="append",
        default=None,
        metavar="LABEL",
        help="Evaluation period label (repeatable; default: every P0 period: 2025, 2026-01..09)",
    )
    p.add_argument(
        "--p0-summary",
        default=None,
        help="P0 summary.json: its LF-0 minus perfect gaps feed the gate and the share of "
        "the gap captured, and an in-run perfect candidate is checked against it",
    )
    p.add_argument(
        "--gate",
        choices=["G2", "G4"],
        default="G2",
        help="Gate to pre-register: G2 (default) or G4, the structured forecaster against a "
        "reference per season instance",
    )
    p.add_argument(
        "--gate-candidate", default=None, metavar="LABEL", help="G4: the candidate label"
    )
    p.add_argument(
        "--gate-reference",
        default=None,
        metavar="LABEL",
        help="G4: the reference candidate label (default: trailing_mean)",
    )
    p.add_argument(
        "--compare",
        action="append",
        default=[],
        metavar="CANDIDATE=REFERENCE",
        help="Secondary, non-gating paired comparison by candidate label (repeatable)",
    )
    p.add_argument(
        "--label",
        default=None,
        help="Free text recorded in the pre-registration: the role of the run (for example a "
        "second look at periods that were already seen)",
    )
    p.add_argument(
        "--note",
        action="append",
        default=[],
        metavar="TEXT",
        help="Free text recorded in the pre-registration (repeatable): what a result may and "
        "may not be used for",
    )
    p.add_argument("--resamples", type=int, default=None, help="Bootstrap resamples (10000)")
    p.add_argument("--seed", type=int, default=None, help="Bootstrap seed (20261025)")


def _add_price_eval_parser(subs: Any) -> None:
    """The ``price-eval`` subcommand (PO-A of the price-outlook plan)."""
    p = subs.add_parser(
        "price-eval",
        help="Pre-registered price-outlook evaluation: paired EUR against exact and "
        "published_only, action differences and estimate metrics",
    )
    _add_common(p)
    p.add_argument("--dataset", required=True, help="Built dataset directory")
    p.add_argument(
        "--name",
        required=True,
        help="Run name; the run is written to <dataset>/price-eval/<name>-<UTC timestamp>",
    )
    p.add_argument(
        "--outlook",
        action="append",
        default=None,
        metavar="NAME",
        help="Registered price outlook to evaluate (repeatable; default: exact, "
        "published_only, persistence). exact and published_only are always the references",
    )
    p.add_argument(
        "--period",
        action="append",
        default=None,
        metavar="LABEL",
        help="Evaluation period label (repeatable; default: every P0 period: 2025, 2026-01..09)",
    )
    p.add_argument(
        "--reference-summary",
        default=None,
        help="forecast-eval summary.json: its trailing_mean benchmark runs are checked for "
        "bit-identity against this run's exact rolling-horizon schedule",
    )
    p.add_argument(
        "--features",
        default=None,
        metavar="ID",
        help="Feature set id (build-features): adds the replayed AppDaemon climate-mask "
        "overlap of each outlook's estimate against the realized prices",
    )
    p.add_argument(
        "--label",
        default=None,
        help="Free text recorded in the pre-registration (for example the role of the run)",
    )
    p.add_argument("--resamples", type=int, default=None, help="Bootstrap resamples (10000)")
    p.add_argument("--seed", type=int, default=None, help="Bootstrap seed (20261025)")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="battery-optimizer",
        description="Reproducible energy measurement and baseline toolkit (ADR-0003)",
    )
    subs = parser.add_subparsers(dest="command")

    # -- extract ----------------------------------------------------------
    p_extract = subs.add_parser(
        "extract", help="Ingest from Prometheus / HA history / OMIE prices to Parquet"
    )
    _add_common(p_extract)
    _add_time_args(p_extract)
    p_extract.add_argument(
        "--source",
        required=True,
        choices=["prometheus", "ha-history", "omie", "all"],
        help="Data source to extract from.  'all' is prometheus + ha-history; omie (public "
        "OMIE day-ahead prices) is always named explicitly and takes local delivery dates "
        "(YYYY-MM-DD, both bounds inclusive) in --start/--end",
    )
    p_extract.add_argument(
        "--entities",
        action="append",
        default=None,
        help="Entity IDs — repeatable or comma-separated",
    )
    p_extract.add_argument(
        "--allow-coverage-gaps",
        action="store_true",
        help="Exit 0 even when a run's manifest records coverage gaps (empty_chunk*, "
        "chunk_accounting_mismatch, no_rows_*/no_samples_extracted, no_series_chunks, "
        "series_without_samples_chunks, omie_missing/omie_parse_error/omie_fetch_failed).  "
        "For interactive/backfill use.  By default such a run "
        "keeps its data and manifest but exits 3 (EXIT_COVERAGE_GAPS) with a summary on stderr; "
        "1 is a failed run, 2 a usage error",
    )

    # -- validate ---------------------------------------------------------
    p_validate = subs.add_parser("validate", help="Validate raw Parquet against schemas")
    _add_common(p_validate)
    p_validate.add_argument(
        "--source",
        default="all",
        choices=["prometheus", "ha-history", "omie", "all"],
        help="Source to validate (default: all = prometheus + ha-history; omie is explicit)",
    )

    # -- build ------------------------------------------------------------
    p_build = subs.add_parser("build", help="Build 15-min analysis dataset")
    _add_common(p_build)
    p_build.add_argument("--start", required=True, help="Start time (ISO 8601 UTC)")
    p_build.add_argument("--end", required=True, help="End time (ISO 8601 UTC)")
    p_build.add_argument(
        "--assume-no-battery",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Assume no battery is dispatching, so gross load may equal the whole-home feed "
        "counter (default).  This is a recorded assumption, NOT a detection: missing battery "
        "telemetry cannot prove a battery is absent.  Use --no-assume-no-battery to refuse the "
        "assumption, which then requires registered battery telemetry to establish gross load.  "
        "It never covers an interval at or after battery.commissioned_at (config/service.yaml)",
    )

    # -- build-features ---------------------------------------------------
    p_feat = subs.add_parser(
        "build-features",
        help="Build the slot-aligned sub-load feature table (separate from the canonical dataset)",
    )
    _add_common(p_feat)
    p_feat.add_argument("--start", required=True, help="Start time (ISO 8601 UTC, 15-min aligned)")
    p_feat.add_argument("--end", required=True, help="End time (ISO 8601 UTC, 15-min aligned)")
    p_feat.add_argument(
        "--dataset",
        default=None,
        help="Canonical dataset directory: also write the servers + heat pump <= main-meter check",
    )
    p_feat.add_argument(
        "--signal-set",
        choices=["default", "climate"],
        default="default",
        help="Signals to build: 'default' (sub-loads, the load-forecaster feature set) or "
        "'climate' (rooms, heat-pump water side, compressor, thermal/electric energy; a "
        "separate feature set with its own id)",
    )
    p_feat.add_argument(
        "--max-gap-seconds",
        type=float,
        default=None,
        help="A span between two samples longer than this is a gap, not held (default 300)",
    )
    p_feat.add_argument(
        "--min-coverage",
        type=float,
        default=None,
        help="Below this covered fraction of a slot, a value is unknown (default 0.8)",
    )

    # -- decompose-load ---------------------------------------------------
    p_dec = subs.add_parser(
        "decompose-load",
        help="Private report: share of load and of LF-0 error per sub-load (P3 of the plan)",
    )
    _add_common(p_dec)
    p_dec.add_argument("--features", required=True, help="Feature-set directory (build-features)")
    p_dec.add_argument("--dataset", required=True, help="Canonical dataset directory")
    p_dec.add_argument(
        "--window-days", type=int, default=28, help="LF-0 trailing window in local days (28)"
    )

    # -- climate-fit ------------------------------------------------------
    p_cfit = subs.add_parser(
        "climate-fit",
        help="Private: fit and validate the thermal models and heat response per fold (C2)",
    )
    p_cfit.add_argument(
        "--data-dir",
        default=None,
        help="Data directory (default: $BATTERY_OPTIMIZER_DATA_DIR or ~/data/energy-baseline)",
    )
    p_cfit.add_argument(
        "--feature-set", required=True, help="Climate feature set id (or a unique id prefix)"
    )
    p_cfit.add_argument(
        "--dataset", required=True, help="Canonical dataset directory (PVPC prices for u)"
    )
    p_cfit.add_argument(
        "--climate-config",
        default="config/climate.yaml",
        help="Climate configuration (default: config/climate.yaml)",
    )
    p_cfit.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    # -- forecast-eval ----------------------------------------------------
    _add_forecast_eval_parser(subs)

    # -- price-eval -------------------------------------------------------
    _add_price_eval_parser(subs)

    # -- benchmark --------------------------------------------------------
    p_bench = subs.add_parser("benchmark", help="Run policy benchmarks on a dataset")
    _add_common(p_bench)
    p_bench.add_argument(
        "--dataset",
        required=True,
        help="Path to analysis dataset directory (analysis/v<schema>/<dataset_id>)",
    )
    p_bench.add_argument(
        "--policies",
        default="all",
        help="Comma-separated policy names or 'all' (default: all). The set is "
        "no_battery, causal_price, day_ahead, rolling_horizon, oracle — one rung per "
        "information level, from measured history only up to perfect foresight",
    )
    p_bench.add_argument(
        "--train-end",
        required=True,
        help="Absolute train/eval cut (ISO 8601 with an offset).  Required with no default: "
        "thresholds are fitted on the earlier split and frozen before evaluation, so the cut "
        "must be declared explicitly and is never inherited",
    )
    p_bench.add_argument(
        "--train-days",
        type=int,
        default=None,
        help="How far back before --train-end the thresholds may look "
        "(default: the benchmark module's train_days)",
    )
    p_bench.add_argument(
        "--eval-end",
        default=None,
        help="Exclusive end of the evaluation window (ISO 8601 with an offset).  The dataset "
        "is cut there first, so the run equals a run on a dataset ending at that instant "
        "(default: the end of the dataset)",
    )
    p_bench.add_argument(
        "--charge-pct",
        type=float,
        default=None,
        help="Training-price percentile for the charge threshold "
        "(default: the benchmark module's charge_pct)",
    )
    p_bench.add_argument(
        "--discharge-pct",
        type=float,
        default=None,
        help="Training-price percentile for the discharge threshold "
        "(default: the benchmark module's discharge_pct)",
    )
    p_bench.add_argument(
        "--initial-soc-frac",
        type=float,
        default=None,
        help="Starting SOC as a fraction of nameplate.  Omitted means soc_window_min, and the "
        "run records that as an assumption rather than a measurement",
    )
    p_bench.add_argument(
        "--import-limit-kw",
        type=float,
        default=None,
        help="Override battery.import_limit_kw with a confirmed site import ceiling.  Omitted "
        "keeps the config value; a null config value leaves the ceiling unapplied and reports it",
    )
    p_bench.add_argument(
        "--min-usable-fraction",
        type=float,
        default=None,
        help="Declared monthly usable-interval fraction, evaluated over EVALUATION intervals "
        "only (a train/eval-straddling month is judged on the dispatched part, not the whole "
        "calendar total). 0.0 (default) DISABLES the gate — nothing is assessed or rejected, "
        "measured fractions still reported; a positive threshold activates it and must be in "
        "(0, 1], finite, never a bool",
    )
    p_bench.add_argument(
        "--quality-policy",
        choices=["reject", "report"],
        default=None,
        help="What the monthly quality gate does with an evaluation month below "
        "--min-usable-fraction when the gate is active. 'reject' (default) aborts the run "
        "before any artifact is written; 'report' retains the month, marks it not-accepted, "
        "and flags the run incomplete. There is no silent exclude: excluding a month would "
        "move the evaluation window without moving the measured-cost basis. Hashed into the "
        "run identity",
    )
    p_bench.add_argument(
        "--time-limit-s",
        type=float,
        default=None,
        help="Solver time limit in seconds (default: the benchmark module's time_limit_s)",
    )
    p_bench.add_argument(
        "--mip-rel-gap",
        type=float,
        default=None,
        help="Solver relative MIP gap tolerance (default: the benchmark module's mip_rel_gap)",
    )
    p_bench.add_argument(
        "--mip-abs-gap",
        type=float,
        default=None,
        help="Solver absolute MIP gap tolerance (default: the benchmark module's mip_abs_gap)",
    )
    p_bench.add_argument(
        "--allow-reconstructed-quality",
        action="store_true",
        help="Admit reconstructed values: forward-filled prices and partially gapped energy "
        "totals.  Allocation boundaries (estimated_boundary) are admitted by default and are "
        "NOT gated by this flag.  unknown_gap/gap_total_unknown allocation and missing/ambiguous "
        "prices are never admitted, with or without this flag",
    )
    p_bench.add_argument(
        "--terminal-mode",
        choices=["free", "fixed"],
        default="free",
        help="Oracle terminal SOC accounting: 'free' leaves ending SOC unvalued (not a closed "
        "cycle, the default), 'fixed' closes the cycle (default: free). The day-ahead policy "
        "is always per-day free with a declared terminal SOC value — that valuation is what "
        "stops a myopic daily solve from emptying the battery at midnight",
    )
    p_bench.add_argument(
        "--day-ahead-forecast-window-days",
        type=int,
        default=None,
        help="Trailing window, in local days, of the day-ahead policy's strictly causal load "
        "forecast (default: the benchmark module's forecast window). Only intervals before the "
        "target day are ever read; the window may reach back past --train-end because "
        "strictly-past measured load is causally available at that day's midnight",
    )
    p_bench.add_argument(
        "--day-ahead-terminal-value",
        type=float,
        default=None,
        help="EUR per DC-kWh credited for end-of-day SOC in each daily solve — the day-ahead "
        "policy's only cross-day coupling. Omitted derives it from the frozen training window "
        "(mean of its usable prices) and records that provenance; passing a number records it "
        "as a hand-picked cli override. Pass 0.0 for the documented pure cash-flow variant, "
        "which empties the battery by midnight on purpose",
    )
    p_bench.add_argument(
        "--day-ahead-time-limit-s",
        type=float,
        default=None,
        help="Solver time limit PER LOCAL DAY for the rolling day-ahead MILPs (default: the "
        "benchmark module's day_ahead_time_limit_s). This is not --time-limit-s, which is the "
        "whole-horizon oracle's limit; days that consumed this one are counted in the artifact",
    )
    p_bench.add_argument(
        "--rolling-horizon-replan-interval-slots",
        type=int,
        default=None,
        help="How often to re-solve (in intervals). Omit for the legacy two-event schedule "
        "(14:00 / 20:30 local), which is the default and what the golden-value tests pin "
        "bit-for-bit. A positive integer is a uniform cadence (e.g. 1 for 15-minute "
        "replanning). ADR-0001's 15-minute cadence is reachable but NOT the default because "
        "it costs roughly 48x the solve count and roughly 37x the wall time (the ratios differ "
        "because a fine-cadence solve is cheaper than a legacy one) and has not yet been "
        "validated on real data",
    )
    p_bench.add_argument(
        "--rolling-horizon-horizon-slots",
        type=int,
        default=None,
        help="How far ahead each solve plans (in intervals). Omit for the legacy schedule "
        "(derives its horizon from the local-day partition: ~34 h at 14:00, ~27.5 h at 20:30). "
        "A positive integer is required when --rolling-horizon-replan-interval-slots is set "
        "(e.g. 96 for 24 h at 15-min intervals). Passing this without a cadence is refused: "
        "the legacy schedule does not accept an explicit horizon length",
    )
    p_bench.add_argument(
        "--rolling-horizon-sigma",
        type=float,
        default=None,
        help="Per-slot price noise standard deviation (>= 0). Default 0.0 is exact prices: "
        "the ladder is about information, not synthetic forecast error. Non-zero values are "
        "an experiment reachable through this flag",
    )
    p_bench.add_argument(
        "--rolling-horizon-bias-sigma",
        type=float,
        default=None,
        help="Per-local-day multiplicative bias standard deviation (>= 0). Default 0.0 is "
        "exact prices. Non-zero values are an experiment",
    )
    p_bench.add_argument(
        "--rolling-horizon-seed",
        type=int,
        default=None,
        help="Random seed for price noise generation. Default 0",
    )
    p_bench.add_argument(
        "--rolling-horizon-time-limit-s",
        type=float,
        default=None,
        help="Solver time limit PER SOLVE for the rolling-horizon MILPs (default: the "
        "benchmark module's rolling_horizon_time_limit_s). This is not --time-limit-s, which "
        "is the whole-horizon oracle's limit; events that consumed this one are counted in "
        "the artifact",
    )
    p_bench.add_argument(
        "--rolling-horizon-load-forecast-mode",
        choices=["frozen", "causal"],
        default=None,
        help="Load forecast mode for the rolling-horizon rung. 'causal' (default) uses the "
        "SAME trailing-slot-mean function as the day-ahead policy, so the two rungs share a "
        "like-for-like load basis — each local day's forecast advances using only "
        "observations strictly before that day. 'frozen' computes the profile once from "
        "training data at the cut and reuses it for every solve (the legacy mode the "
        "golden-value tests pin bit-for-bit)",
    )
    p_bench.add_argument(
        "--load-forecaster",
        default=None,
        metavar="NAME",
        help="Registered load forecaster the day-ahead and causal rolling-horizon rungs plan "
        "on (default: trailing_mean, the 28-day same-slot mean LF-0). 'perfect' is an OFFLINE "
        "ORACLE that hands the planner the measured load, for headroom measurement only. "
        "--day-ahead-forecast-window-days feeds any forecaster with a window. Recorded in "
        "the run lineage, and in the run identity whenever it is not the default",
    )
    p_bench.add_argument(
        "--rolling-horizon-price-outlook",
        default=None,
        metavar="NAME",
        help="Registered price outlook for the unpublished part of each rolling-horizon plan "
        "(default: exact, tomorrow's realized prices at 14:00, an upper bound). "
        "'published_only' plans only through the last published price, like the live "
        "service; 'persistence' estimates tomorrow as today's price at the same local time; "
        "'omie_period' and 'omie_period_month' estimate it from OMIE's day-ahead price (usable "
        "from 13:30 local, read from the verified OMIE store). "
        "Non-default outlooks need the legacy cadence and zero sigma/bias, and enter the run "
        "identity",
    )
    p_bench.add_argument(
        "--throughput-cost-eur-per-kwh",
        type=float,
        default=None,
        help="Per-kWh AC-throughput penalty added to the oracle, day-ahead and rolling-horizon "
        "planning objectives on a shared basis (default 0.0). 0.0 leaves every plan "
        "bit-identical to a run without this flag; a non-zero value is a planning-only term "
        "that never enters the reported true-price cost. Recorded in the run identity",
    )

    # -- report -----------------------------------------------------------
    p_report = subs.add_parser("report", help="Generate monthly reconciliation report")
    _add_common(p_report)
    p_report.add_argument(
        "--dataset",
        default=None,
        help="Dataset directory to report on; must contain exactly one benchmark run.  "
        "Ambiguity lists the available run ids instead of picking one",
    )
    p_report.add_argument(
        "--benchmark",
        default=None,
        help="Benchmark run directory (or its benchmark.json) to report on; overrides --dataset",
    )

    # -- inspect-sql ------------------------------------------------------
    p_sql = subs.add_parser("inspect-sql", help="Inspect DuckDB SQL queries")
    _add_common(p_sql)
    p_sql.add_argument(
        "--dataset",
        default=None,
        help="Dataset directory to query (required for --sql and --query)",
    )
    action = p_sql.add_mutually_exclusive_group()
    action.add_argument(
        "--sql",
        default=None,
        help="Ad-hoc read-only SQL: a single SELECT, WITH ... SELECT or EXPLAIN SELECT",
    )
    action.add_argument(
        "--query",
        default=None,
        metavar="NAME",
        help="Run a curated query by name (see --list-queries)",
    )
    p_sql.add_argument(
        "--list-queries",
        action="store_true",
        help="List the curated query names and exit (exactly one action is required: this, "
        "--sql or --query)",
    )
    p_sql.add_argument(
        "--benchmark",
        default=None,
        help="Benchmark run directory: also expose its benchmark_results and benchmark_schedules "
        "views",
    )
    p_sql.add_argument("--json", action="store_true", help="Print results as JSON")

    # -- ha-states --------------------------------------------------------
    p_ha = subs.add_parser(
        "ha-states",
        help="Read and query Home Assistant enum state history (ADR-0002 Phase 1 ingest)",
    )
    _add_common(p_ha)
    p_ha.add_argument(
        "--entity",
        default=None,
        help="Entity ID to query (default: list all entities in the store)",
    )
    p_ha.add_argument(
        "--as-of",
        default=None,
        help="As-of query: ISO-8601 UTC instant.  Returns the state of --entity "
        "at that instant (last_changed <= instant, inclusive boundary)",
    )
    p_ha.add_argument(
        "--list-states",
        action="store_true",
        help="List all deduplicated states for --entity (requires --entity)",
    )

    # -- serve (resident shadow service) ---------------------------------
    p_serve = subs.add_parser(
        "serve",
        help="Run the resident SHADOW service (read-only HA/Prometheus, plans never sent)",
    )
    _add_common(p_serve)
    p_serve.add_argument(
        "--service-config",
        default=None,
        help="Service YAML (default: $BATTERY_OPTIMIZER_SERVICE_CONFIG or config/service.yaml)",
    )
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    p_serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port (default: $BATTERY_OPTIMIZER_PORT or 8080)",
    )

    # -- shadow-report -----------------------------------------------------
    p_shadow = subs.add_parser(
        "shadow-report",
        help="Compare recorded shadow plans against the native battery behaviour",
    )
    _add_common(p_shadow)
    p_shadow.add_argument(
        "--service-config",
        default=None,
        help="Service YAML, for the timezone and slot length (default as for serve)",
    )
    p_shadow.add_argument(
        "--since",
        default=None,
        help="Window start: ISO instant with offset, or a local date (default: 14 days ago)",
    )
    p_shadow.add_argument(
        "--until",
        default=None,
        help="Window end (exclusive): ISO instant or local date (default: today's midnight)",
    )
    p_shadow.add_argument("--format", choices=("md", "json"), default="md")

    # -- climate-shadow-report ---------------------------------------------
    p_cshadow = subs.add_parser(
        "climate-shadow-report",
        help="Preregistered gate report of the climate shadow (track B2) from the store",
    )
    _add_common(p_cshadow)
    p_cshadow.add_argument(
        "--service-config",
        default=None,
        help="Service YAML: go_live, the prior and the MPC config (default as for serve)",
    )
    p_cshadow.add_argument(
        "--prior", default=None, help="Prior id or prefix (default: climate_shadow.prior)"
    )
    p_cshadow.add_argument(
        "--mpc-config-sha256",
        default=None,
        help="MPC config hash (default: the hash of the configured climate-mpc.yaml)",
    )
    p_cshadow.add_argument(
        "--all-lineages",
        action="store_true",
        help="Evaluate the records of every prior and MPC config together (diagnostic only)",
    )
    p_cshadow.add_argument("--format", choices=("md", "json"), default="md")

    # -- mirror (deferred) ------------------------------------------------
    p_mirror = subs.add_parser("mirror", help="Mirror dataset to SQL (deferred in Phase 1)")
    _add_common(p_mirror)

    return parser


# ---------------------------------------------------------------------------
# Entity parsing
# ---------------------------------------------------------------------------


def parse_entities(values: list[str] | None) -> list[str]:
    """Flatten repeatable / comma-separated entity arguments."""
    if not values:
        return []
    result: list[str] = []
    for v in values:
        result.extend(e.strip() for e in v.split(",") if e.strip())
    return result


# ---------------------------------------------------------------------------
# Time window resolution
# ---------------------------------------------------------------------------


def resolve_time_window(
    start: str | None, end: str | None, lookback_days: int | None
) -> tuple[str, str]:
    """Resolve start/end from explicit args or --lookback-days.

    Returns (start_iso, end_iso) strings.

    Raises:
        ValueError: neither --start/--end nor --lookback-days provided,
            or --start without --end.
    """
    if lookback_days is not None:
        if lookback_days < 1:
            raise ValueError(f"--lookback-days must be >= 1, got {lookback_days}")
        now = datetime.now(UTC)
        end_dt = now.replace(microsecond=0)
        start_dt = (now - timedelta(days=lookback_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start_dt.isoformat(), end_dt.isoformat()

    if start is None:
        raise ValueError("either --start/--end or --lookback-days is required")
    if end is None:
        end = datetime.now(UTC).isoformat()
    return start, end


# ---------------------------------------------------------------------------
# Error redaction
# ---------------------------------------------------------------------------

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE)
#: Credential-bearing parameter names, matched in both ``key=value`` and ``key: value``
#: form.  Applied after :data:`_BEARER_RE` so ``Authorization: Bearer x`` is already
#: reduced to ``Authorization: Bearer <REDACTED>`` when this runs.
_SECRET_KEYS = (
    r"(?:password|passwd|pwd|token|access_token|refresh_token|id_token|secret"
    r"|api_?key|authorization|client_?secret)"
)
_SECRET_PARAM_RE = re.compile(rf"({_SECRET_KEYS}\s*[=:]\s*)[^\s,;&'\"]+", re.IGNORECASE)
#: Common prefixed API-key / token shapes, redacted whole.
_CREDENTIAL_PREFIX_RE = re.compile(r"\b(?:sk|pat|ghp|gho|xox[baprs])[-_][A-Za-z0-9_\-]{8,}")


def _redact_error(message: str) -> str:
    """Redact potential secrets from error messages."""
    message = _BEARER_RE.sub(r"\1<REDACTED>", message)
    message = _SECRET_PARAM_RE.sub(r"\1<REDACTED>", message)
    message = _CREDENTIAL_PREFIX_RE.sub("<REDACTED>", message)
    return message


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract source data to raw Parquet."""
    from battery_optimizer.coverage import (
        EXIT_COVERAGE_GAPS,
        format_gap_summary,
        manifest_coverage_gaps,
    )
    from battery_optimizer.extract_ha import extract_ha_history
    from battery_optimizer.extract_omie import extract_omie, resolve_omie_dates
    from battery_optimizer.extract_prom import extract_prometheus
    from battery_optimizer.paths import resolve_data_dir

    entities = parse_entities(args.entities)
    source: str = args.source

    omie_dates = None
    try:
        if source == "omie":
            if entities:
                raise ValueError("--entities does not apply to --source omie")
            omie_dates = resolve_omie_dates(args.start, args.end, args.lookback_days)
            start_str, end_str = (d.isoformat() for d in omie_dates)
        else:
            start_str, end_str = resolve_time_window(args.start, args.end, args.lookback_days)
    except ValueError as exc:
        print(f"extract: {_redact_error(str(exc))}", file=sys.stderr)
        return 1

    data_dir = resolve_data_dir(args.data_dir)
    log.info("extract: source=%s data_dir=%s", source, data_dir)
    log.info("extract: window=[%s, %s)", start_str, end_str)
    if entities:
        log.info("extract: entities=%s", ",".join(entities))
    else:
        log.info("extract: entities=default (source-specific)")

    manifests: list[Path] = []
    gap_summaries: list[str] = []

    def _record_gaps(label: str, manifest_path: Path) -> None:
        gaps = manifest_coverage_gaps(manifest_path)
        if gaps:
            gap_summaries.append(format_gap_summary(label, manifest_path, gaps))

    if source in ("prometheus", "all"):
        try:
            manifest_path = extract_prometheus(
                data_dir=str(data_dir),
                config_path=args.config,
                start=start_str,
                end=end_str,
                entities=entities or None,
            )
            manifests.append(manifest_path)
            log.info("extract: prometheus manifest=%s", manifest_path)
        except Exception as exc:
            msg = _redact_error(str(exc))
            log.error("extract: prometheus failed: %s", msg)
            print(f"extract: prometheus failed: {msg}", file=sys.stderr)
            return 1
        else:
            _record_gaps("prometheus", manifest_path)

    if source in ("ha-history", "all"):
        try:
            manifest_path = extract_ha_history(
                data_dir=str(data_dir),
                config_path=args.config,
                start=start_str,
                end=end_str,
                entities=entities,
            )
            manifests.append(manifest_path)
            log.info("extract: ha-history manifest=%s", manifest_path)
        except Exception as exc:
            msg = _redact_error(str(exc))
            log.error("extract: ha-history failed: %s", msg)
            print(f"extract: ha-history failed: {msg}", file=sys.stderr)
            return 1
        else:
            _record_gaps("ha-history", manifest_path)

    if source == "omie" and omie_dates is not None:
        try:
            manifest_path = extract_omie(
                data_dir=str(data_dir),
                config_path=args.config,
                start=omie_dates[0],
                end=omie_dates[1],
            )
            manifests.append(manifest_path)
            log.info("extract: omie manifest=%s", manifest_path)
        except Exception as exc:
            msg = _redact_error(str(exc))
            log.error("extract: omie failed: %s", msg)
            print(f"extract: omie failed: {msg}", file=sys.stderr)
            return 1
        else:
            _record_gaps("omie", manifest_path)

    print(f"extract: completed — {len(manifests)} manifest(s) written")
    for mp in manifests:
        print(f"  manifest: {mp}")
    if gap_summaries:
        # Judged after every manifest is written: the data stays, the exit code tells a
        # scheduler that it is incomplete.  --allow-coverage-gaps downgrades it to a warning.
        for summary in gap_summaries:
            print(summary, file=sys.stderr)
        if args.allow_coverage_gaps:
            print("extract: coverage gaps ignored (--allow-coverage-gaps)", file=sys.stderr)
            return 0
        print(
            f"extract: exiting {EXIT_COVERAGE_GAPS}: coverage gaps recorded "
            "(pass --allow-coverage-gaps to accept them)",
            file=sys.stderr,
        )
        return EXIT_COVERAGE_GAPS
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate raw Parquet against schemas and manifest integrity."""
    from battery_optimizer.paths import resolve_data_dir

    source_choice: str = args.source
    data_dir = resolve_data_dir(args.data_dir)
    log.info("validate: source=%s data_dir=%s", source_choice, data_dir)

    results: dict[str, dict] = {}
    all_ok = True

    sources_to_validate: list[str] = []
    if source_choice in ("prometheus", "all"):
        sources_to_validate.append("prometheus")
    if source_choice in ("ha-history", "all"):
        sources_to_validate.append("ha_history")
    if source_choice == "omie":
        sources_to_validate.append("omie")

    for src in sources_to_validate:
        result = _validate_source(data_dir, src)
        results[src] = result
        if not result.get("ok", False):
            all_ok = False

    manifest_results = _validate_manifests(data_dir, sources_to_validate)
    if not manifest_results.get("ok", False):
        all_ok = False

    print(f"validate: data_dir={data_dir}")
    for src, res in results.items():
        status = "OK" if res.get("ok") else "FAIL"
        print(
            f"  {src}: {status} — parts={res.get('file_count', 0)} "
            f"rows={res.get('row_count', 0)} violations={len(res.get('violations', []))}"
        )
        for v in res.get("violations", []):
            print(f"    VIOLATION: {v}")

    print(f"  manifests: {'OK' if manifest_results.get('ok') else 'FAIL'}")
    for v in manifest_results.get("violations", []):
        print(f"    VIOLATION: {v}")

    return 0 if all_ok else 1


def _validate_source(data_dir: Path, source: str) -> dict:
    """Validate parts for a single source against its schema."""
    import pyarrow.parquet as pq

    from battery_optimizer.paths import iter_parts, parts_dir
    from battery_optimizer.schemas import (
        RAW_ENUM_STATE,
        RAW_OMIE_DAY_AHEAD,
        RAW_SAMPLE,
        validate_table,
    )

    schema_map = {
        "prometheus": ("RAW_SAMPLE", RAW_SAMPLE),
        "ha_history": ("RAW_ENUM_STATE", RAW_ENUM_STATE),
        "omie": ("RAW_OMIE_DAY_AHEAD", RAW_OMIE_DAY_AHEAD),
    }
    if source not in schema_map:
        return {
            "ok": False,
            "violations": [f"unknown source: {source}"],
            "file_count": 0,
            "row_count": 0,
        }

    schema_name, _ = schema_map[source]
    pdir = parts_dir(data_dir, source)
    files = iter_parts(data_dir, source)

    summary: dict = {
        "source": source,
        "parts_dir": str(pdir),
        "file_count": len(files),
        "row_count": 0,
        "violations": [],
        "ok": True,
    }

    if not files:
        summary["violations"].append(f"no part files found in {pdir}")
        summary["ok"] = False
        return summary

    total_rows = 0
    for path in files:
        declared_hash = path.stem.rsplit("-", 1)[-1]
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{8}", declared_hash) or declared_hash != actual_hash[:8]:
            summary["violations"].append(
                f"{path.name}: filename hash {declared_hash!r} != actual {actual_hash[:8]}"
            )

        try:
            table = pq.read_table(path)
        except Exception as exc:
            summary["violations"].append(f"{path.name}: unreadable: {type(exc).__name__}: {exc}")
            continue

        try:
            validate_table(schema_name, table)
        except (TypeError, ValueError) as exc:
            summary["violations"].append(f"{path.name}: schema: {exc}")
            continue

        n = table.num_rows
        total_rows += n
        if n == 0:
            summary["violations"].append(f"{path.name}: zero rows")

    if source == "omie":
        from battery_optimizer.extract_omie import validate_omie_store

        summary["violations"].extend(validate_omie_store(data_dir))

    summary["row_count"] = total_rows
    summary["ok"] = not summary["violations"]
    return summary


def _validate_manifests(data_dir: Path, sources: list[str]) -> dict:
    """Validate manifest files and their part hashes against actual files."""
    manifests_dir = data_dir / "manifests"
    result: dict = {"ok": True, "violations": []}

    if not manifests_dir.exists():
        result["violations"].append(f"manifests directory not found: {manifests_dir}")
        result["ok"] = False
        return result

    manifest_files = sorted(manifests_dir.glob("*.json"))
    if not manifest_files:
        result["violations"].append("no manifest files found")
        result["ok"] = False
        return result

    for mf in manifest_files:
        try:
            data = json.loads(mf.read_text())
        except Exception as exc:
            result["violations"].append(f"{mf.name}: unreadable: {exc}")
            result["ok"] = False
            continue

        # Only check manifests matching our sources
        manifest_source = data.get("source", "")
        if sources and manifest_source not in sources:
            continue

        for pf in data.get("part_files", []):
            rel_path = pf.get("path")
            expected_sha = pf.get("sha256")
            if not rel_path or not expected_sha:
                continue
            full_path = data_dir / rel_path
            if not full_path.exists():
                result["violations"].append(f"{mf.name}: part missing: {rel_path}")
                result["ok"] = False
                continue
            actual_sha = hashlib.sha256(full_path.read_bytes()).hexdigest()
            if actual_sha != expected_sha:
                result["violations"].append(
                    f"{mf.name}: hash mismatch {rel_path}: "
                    f"expected={expected_sha[:8]} actual={actual_sha[:8]}"
                )
                result["ok"] = False

    return result


def cmd_build(args: argparse.Namespace) -> int:
    """Build 15-min analysis dataset."""
    from battery_optimizer import build as build_module
    from battery_optimizer.battery_telemetry import load_battery_telemetry

    battery_absent: bool = args.assume_no_battery
    if battery_absent:
        print(
            "build: WARNING: assuming no battery is dispatching, so gross load equals the "
            "whole-home feed counter. This is a RECORDED ASSUMPTION, NOT A DETECTION: absent "
            "battery telemetry cannot prove a battery does not exist. The assumption is pinned "
            "in the build manifest; a build still refuses if registered battery telemetry exists",
            file=sys.stderr,
        )
    else:
        print(
            "build: WARNING: --no-assume-no-battery — gross load will not be equated with the "
            "whole-home feed. This requires registered battery telemetry to reconstruct it; "
            "without it the build refuses, because presence cannot be invented from missing "
            "data either",
            file=sys.stderr,
        )

    dataset_dir = build_module.build_dataset(
        data_dir=args.data_dir,
        config_path=args.config,
        start=args.start,
        end=args.end,
        battery_absent=battery_absent,
    )
    print(f"build: dataset directory: {dataset_dir}")
    print(f"build: battery_absent={battery_absent} (assumption, not detection)")
    commissioned_at = load_battery_telemetry().commissioned_at
    print(
        "build: battery.commissioned_at="
        + (commissioned_at.isoformat() if commissioned_at else "null (no battery recorded)")
        + " (config/service.yaml); from that instant the assumption is refused and gross load is "
        "grid - charge + discharge"
    )
    return 0


def cmd_build_features(args: argparse.Namespace) -> int:
    """Build the sub-load feature table next to (never inside) the canonical datasets."""
    from battery_optimizer import features

    kwargs: dict[str, Any] = {"signals": features.SIGNAL_SETS[args.signal_set]}
    if args.max_gap_seconds is not None:
        kwargs["max_gap_seconds"] = args.max_gap_seconds
    if args.min_coverage is not None:
        kwargs["min_coverage"] = args.min_coverage
    try:
        directory = features.build_features(
            data_dir=args.data_dir,
            start=args.start,
            end=args.end,
            dataset=args.dataset,
            **kwargs,
        )
    except features.FeatureError as exc:
        raise ValueError(str(exc)) from exc
    print(f"build-features: feature set directory: {directory}")
    return 0


def cmd_decompose_load(args: argparse.Namespace) -> int:
    """Write the private decomposition report for a feature set and a canonical dataset."""
    from battery_optimizer import features, load_decomposition

    try:
        out_dir = load_decomposition.write_report(
            features_dir=args.features,
            dataset_dir=args.dataset,
            data_dir=args.data_dir,
            window_days=args.window_days,
        )
    except features.FeatureError as exc:
        raise ValueError(str(exc)) from exc
    print(f"decompose-load: report directory: {out_dir}")
    return 0


def cmd_climate_fit(args: argparse.Namespace) -> int:
    """Fit, validate and write a climate model directory into the private data dir."""
    from battery_optimizer.climate import fit

    out = fit.run_climate_fit(
        feature_set=args.feature_set,
        dataset=args.dataset,
        config_path=args.climate_config,
        data_dir=args.data_dir,
    )
    selection = json.loads((out / "validation.json").read_text())["selection"]
    print(f"climate-fit: model directory: {out}")
    print(
        f"climate-fit: verdict {selection['verdict']} "
        f"(selected model: {selection['selected'] or 'none'})"
    )
    return 0


def cmd_forecast_eval(args: argparse.Namespace) -> int:
    """Run a pre-registered load-forecaster evaluation into the private data dir."""
    from battery_optimizer import forecast_eval
    from battery_optimizer.config import resolve_config_path

    known = {p.label: p for p in forecast_eval.DEFAULT_PERIODS}
    labels = args.period or list(known)
    unknown = [label for label in labels if label not in known]
    if unknown:
        raise ValueError(f"unknown period(s) {unknown}; known: {sorted(known)}")
    run_dir = forecast_eval.run_forecast_eval(
        dataset=args.dataset,
        config_path=resolve_config_path(args.config),
        name=args.name,
        candidates=args.candidate,
        periods=[known[label] for label in labels],
        p0_summary=args.p0_summary,
        resamples=_or_default(args.resamples, forecast_eval.DEFAULT_RESAMPLES),
        seed=_or_default(args.seed, forecast_eval.DEFAULT_SEED),
        data_dir=args.data_dir,
        gate=args.gate,
        gate_candidate=args.gate_candidate,
        gate_reference=args.gate_reference,
        compare=[tuple(item.split("=", 1)) for item in args.compare],
        label=args.label,
        notes=args.note,
    )
    summary = json.loads((run_dir / forecast_eval.SUMMARY_JSON_NAME).read_text())
    print(f"forecast-eval: run directory: {run_dir}")
    if "g4" in summary:
        print(f"forecast-eval: gate G4: {'PASS' if summary['g4']['passed'] else 'FAIL'}")
    for label, gate in summary["gate"].items():
        if gate.get("reason") == "the reference itself":
            continue
        verdict = {True: "PASS", False: "FAIL", None: "UNDETERMINED"}[gate["passed"]]
        suffix = "" if gate["eligible"] else " (oracle, not eligible)"
        print(f"forecast-eval: gate G2 {label}: {verdict}{suffix}")
    return 0


def cmd_price_eval(args: argparse.Namespace) -> int:
    """Run a pre-registered price-outlook evaluation into the private data dir."""
    from battery_optimizer import forecast_eval, price_eval
    from battery_optimizer.config import resolve_config_path

    known = {p.label: p for p in forecast_eval.DEFAULT_PERIODS}
    labels = args.period or list(known)
    unknown = [label for label in labels if label not in known]
    if unknown:
        raise ValueError(f"unknown period(s) {unknown}; known: {sorted(known)}")
    run_dir = price_eval.run_price_eval(
        dataset=args.dataset,
        config_path=resolve_config_path(args.config),
        name=args.name,
        outlooks=args.outlook or list(price_eval.DEFAULT_OUTLOOKS),
        periods=[known[label] for label in labels],
        reference_summary=args.reference_summary,
        resamples=_or_default(args.resamples, forecast_eval.DEFAULT_RESAMPLES),
        seed=_or_default(args.seed, forecast_eval.DEFAULT_SEED),
        data_dir=args.data_dir,
        features=args.features,
        label=args.label,
    )
    print(f"price-eval: run directory: {run_dir}")
    return 0


def _or_default(value: float | int | None, default: float | int) -> float | int:
    """Take the module's default when the flag was not given (0 is a real value)."""
    return default if value is None else value


def _benchmark_kwargs(args: argparse.Namespace, benchmark: Any) -> dict[str, Any]:
    """Map ``benchmark`` flags onto ``benchmark_dataset`` keywords.

    Knobs the caller left at their default are filled from the benchmark module's own
    constants rather than a second copy of the numbers, so the CLI cannot drift away
    from the contract that gets hashed into the run id.  ``initial_soc_frac`` and
    ``import_limit_kw`` are passed through as ``None`` when unset: for those two, "not
    supplied" is itself a meaningful value the run must record as an assumption.  So is
    ``day_ahead_terminal_value``: unset means "derive it from the frozen training
    window", which is a different measurement from any number an operator could type,
    and the artifact records which of the two happened.
    """
    from battery_optimizer.config import resolve_config_path

    return {
        "dataset": args.dataset,
        "config_path": resolve_config_path(args.config),
        "train_end": args.train_end,
        "policies": args.policies,
        "train_days": _or_default(args.train_days, benchmark.DEFAULT_TRAIN_DAYS),
        "charge_pct": _or_default(args.charge_pct, benchmark.DEFAULT_CHARGE_PCT),
        "discharge_pct": _or_default(args.discharge_pct, benchmark.DEFAULT_DISCHARGE_PCT),
        "initial_soc_frac": args.initial_soc_frac,
        "import_limit_kw": args.import_limit_kw,
        "min_usable_fraction": _or_default(args.min_usable_fraction, 0.0),
        "quality_policy": (
            args.quality_policy
            if args.quality_policy is not None
            else benchmark.QUALITY_POLICY_REJECT
        ),
        "allow_reconstructed_quality": args.allow_reconstructed_quality,
        "time_limit_s": _or_default(args.time_limit_s, benchmark.DEFAULT_TIME_LIMIT_S),
        "mip_rel_gap": _or_default(args.mip_rel_gap, benchmark.DEFAULT_MIP_REL_GAP),
        "mip_abs_gap": _or_default(args.mip_abs_gap, benchmark.DEFAULT_MIP_ABS_GAP),
        "terminal_mode": args.terminal_mode,
        "day_ahead_forecast_window_days": _or_default(
            args.day_ahead_forecast_window_days, benchmark.DEFAULT_FORECAST_WINDOW_DAYS
        ),
        "day_ahead_terminal_value_eur_per_kwh": args.day_ahead_terminal_value,
        "day_ahead_time_limit_s": _or_default(
            args.day_ahead_time_limit_s, benchmark.DEFAULT_DAY_AHEAD_TIME_LIMIT_S
        ),
        "rolling_horizon_replan_interval_slots": args.rolling_horizon_replan_interval_slots,
        "rolling_horizon_horizon_slots": args.rolling_horizon_horizon_slots,
        "rolling_horizon_sigma": _or_default(
            args.rolling_horizon_sigma, benchmark.DEFAULT_ROLLING_HORIZON_SIGMA
        ),
        "rolling_horizon_bias_sigma": _or_default(
            args.rolling_horizon_bias_sigma, benchmark.DEFAULT_ROLLING_HORIZON_BIAS_SIGMA
        ),
        "rolling_horizon_seed": _or_default(
            args.rolling_horizon_seed, benchmark.DEFAULT_ROLLING_HORIZON_SEED
        ),
        "rolling_horizon_time_limit_s": _or_default(
            args.rolling_horizon_time_limit_s, benchmark.DEFAULT_ROLLING_HORIZON_TIME_LIMIT_S
        ),
        "rolling_horizon_load_forecast_mode": (
            args.rolling_horizon_load_forecast_mode
            if args.rolling_horizon_load_forecast_mode is not None
            else benchmark.DEFAULT_ROLLING_HORIZON_LOAD_FORECAST_MODE
        ),
        "throughput_cost_eur_per_kwh": _or_default(args.throughput_cost_eur_per_kwh, 0.0),
        "load_forecaster": args.load_forecaster or benchmark.DEFAULT_LOAD_FORECASTER,
        "rolling_horizon_price_outlook": (
            args.rolling_horizon_price_outlook or benchmark.DEFAULT_ROLLING_HORIZON_PRICE_OUTLOOK
        ),
        "eval_end": args.eval_end,
        "data_dir": args.data_dir,
    }


def _benchmark_summary(run_dir: Path, benchmark: Any) -> str:
    """One honest line about a finished run, read back from what the run recorded.

    The artifacts are already written at this point, so a cosmetic failure here must
    not turn a successful benchmark into a non-zero exit: it degrades to a note.
    """
    try:
        payload = json.loads((run_dir / benchmark.RESULTS_JSON_NAME).read_bytes().decode("utf-8"))
        basis = payload.get("assumptions", {}).get("cost_objective", {}).get("cost_basis", "?")
        # The ORACLE's terminal accounting, which is what this word means in the header:
        # the day-ahead policy has its own, per-day, and is reported separately below.
        # Shadowing this with the day-ahead value is a bug that was actually made here,
        # caught only because the printed line turned into a dict.
        oracle_terminal = (
            payload.get("assumptions", {}).get("terminal_accounting", {}).get("used", "?")
        )
        ev = payload.get("evaluation", {})
        months = ",".join(ev.get("months_included", []) or []) or "?"
        parts = []
        for name, record in (payload.get("policies") or {}).items():
            cost = record.get("cost_eur")
            if cost is None:
                parts.append(f"{name}=no cost")
            else:
                flag = "certified" if record.get("is_certified") else "uncertified"
                parts.append(
                    f"{name}={cost:,.4f} EUR "
                    f"({record.get('solver_status')}, {flag}, bound={record.get('bound_kind')})"
                )
        for name in payload.get("solver", {}).get("errors") or {}:
            parts.append(f"{name}=no result row")
        day_ahead = payload.get("day_ahead") or {}
        if day_ahead.get("ran"):
            # The day-ahead rung is the one whose result is telemetry-shaped: days
            # solved, days degraded to hold-SOC, and the forecast error the decisions
            # rode on. A single "cost=" figure would hide all of it.
            forecast = (day_ahead.get("forecast") or {}).get("summary") or {}
            day_ahead_terminal = day_ahead.get("terminal_value") or {}
            mae = forecast.get("mae_kwh")
            closed = (payload.get("comparison") or {}).get("day_ahead_closes_gap_fraction")
            parts.append(
                f"day_ahead: "
                f"{day_ahead.get('days_solved')}/{day_ahead.get('day_count')} local days "
                f"solved, {day_ahead.get('days_degraded_to_hold', 0)} held SOC, "
                f"{day_ahead.get('days_at_time_limit', 0)} at time limit, "
                f"forecast MAE={'n/a' if mae is None else f'{mae:.4f}'} kWh/interval, "
                f"end-of-day SOC={day_ahead_terminal.get('eur_per_kwh')} EUR/kWh, "
                f"causal->oracle gap closed={'n/a' if closed is None else f'{closed:.1%}'}"
            )
        rolling = payload.get("rolling_horizon") or {}
        if rolling.get("ran"):
            # Same reasoning for the fifth rung, with the two numbers that define it: the
            # cadence it ran on and the solver cost of that cadence.  solve_count is the
            # unit of cost of a run (a 15-minute cadence is ~48x the legacy solve count, and
            # only ~37x its wall time, because a fine-cadence solve is cheaper), and the
            # multiplicity says why the clipping kWh below is not curtailment.
            rh_terminal = rolling.get("terminal_value") or {}
            noise_bits = (
                f"sigma={rolling.get('sigma')}, bias_sigma={rolling.get('bias_sigma')}, "
                f"seed={rolling.get('seed')}"
            )
            parts.append(
                f"rolling_horizon: "
                f"{rolling.get('events_solved', 0)}/{rolling.get('event_count')} events solved "
                f"({rolling.get('solve_count')} solves), "
                f"{rolling.get('events_skipped_no_usable', 0)} skipped, "
                f"{rolling.get('events_time_limit', 0)} at time limit, "
                f"cadence={rolling.get('replan_cadence')}, {noise_bits}, "
                f"end-of-plan SOC={rh_terminal.get('eur_per_kwh')} EUR/kWh, "
                f"planned multiplicity={rolling.get('planning_multiplicity_mean')}x "
                f"(max {rolling.get('planning_multiplicity_max')}, executed "
                f"{rolling.get('executed_multiplicity_mean')}x — clipping kWh is per-plan, "
                f"not curtailment)"
            )
        return (
            f"run={payload.get('run_id') or run_dir.name} basis={basis} "
            f"oracle_terminal={oracle_terminal} "
            f"months={months} intervals="
            f"{ev.get('usable_interval_count')}/{ev.get('interval_count')} "
            f"usable | " + "; ".join(parts)
        )
    except Exception as exc:  # noqa: BLE001 - summary only, never a run failure
        log.debug("benchmark: summary unavailable: %s", _redact_error(str(exc)))
        return f"summary unavailable — see {_redact_error(str(run_dir))}"


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Run policy benchmarks on a dataset."""
    from battery_optimizer import benchmark

    if not benchmark.core_available():
        print(
            "benchmark: the numeric core is not usable (battery / policies / oracle / cost are "
            "missing or incomplete), so no policy could be solved. Nothing was run and nothing "
            "was written",
            file=sys.stderr,
        )
        return 1

    run_dir = benchmark.benchmark_dataset(**_benchmark_kwargs(args, benchmark))
    print(f"benchmark: run directory: {run_dir}")
    print(f"benchmark: summary: {_benchmark_summary(Path(run_dir), benchmark)}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Generate monthly reconciliation report."""
    from battery_optimizer import report

    run_dir = Path(
        report.generate_report(
            data_dir=args.data_dir,
            config_path=args.config,
            dataset=args.dataset,
            benchmark_path=args.benchmark,
        )
    )
    print(f"report: run directory: {run_dir}")
    print(f"report: wrote {run_dir / report.REPORT_MD_NAME}")
    print(f"report: wrote {run_dir / report.REPORT_JSON_NAME}")
    return 0


def _format_cell(value: object) -> str:
    """Render one result value for the aligned text output."""
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _print_rows(rows: list[dict], *, as_json: bool) -> None:
    """Print query results as JSON, or as an aligned plain-text table."""
    if as_json:
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        print("(0 rows)")
        return

    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    grid = [[_format_cell(row.get(h)) for h in headers] for row in rows]
    widths = [max([len(h)] + [len(r[i]) for r in grid]) for i, h in enumerate(headers)]

    def _line(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    for line in (_line(headers), _line(["-" * w for w in widths]), *(_line(r) for r in grid)):
        print(line)
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


#: Views that exist only when ``--benchmark`` is supplied (``report._connect``
#: registers them from the run's Parquet).  Anything reading one of these cannot
#: run against the dataset alone.
_BENCHMARK_VIEWS = ("benchmark_results", "benchmark_schedules")


def _benchmark_views_in(sql: str) -> list[str]:
    """Which benchmark-only views *sql* names, in :data:`_BENCHMARK_VIEWS` order."""
    return [view for view in _BENCHMARK_VIEWS if re.search(rf"\b{re.escape(view)}\b", sql)]


def _require_benchmark_views(sql: str, *, label: str, benchmark: str | None) -> None:
    """Refuse up front when *sql* needs views that ``--benchmark`` would register.

    Without this, DuckDB answers such a query with a ``CatalogException`` — which is
    neither a :class:`ValueError` nor a :class:`RuntimeError`, so it escapes
    :func:`main`'s expected-failure handling as a raw traceback.  A missing benchmark
    view is not a bug and not corrupt data: it simply does not exist unless
    ``--benchmark`` was passed, so say that here, before the engine is involved at all.
    Validating the SQL text rather than catching the DB error also keeps the message
    independent of DuckDB's exception hierarchy and of how the view was referenced.
    """
    if benchmark is not None:
        return
    needed = _benchmark_views_in(sql)
    if not needed:
        return
    quoted = ", ".join(f"'{view}'" for view in needed)
    many = len(needed) > 1
    raise ValueError(
        f"{label} needs the benchmark view{'s' if many else ''} {quoted}, which "
        f"{'are' if many else 'is'} only registered when --benchmark PATH is given: "
        f"point it at a benchmark run directory (see the `benchmark` command), or use "
        f"a query that reads only the dataset (`--list-queries` shows those)"
    )


def cmd_inspect_sql(args: argparse.Namespace) -> int:
    """Inspect DuckDB SQL queries."""
    from battery_optimizer import report

    chosen = [
        name
        for name, selected in (
            ("--sql", args.sql is not None),
            ("--query", args.query is not None),
            ("--list-queries", bool(args.list_queries)),
        )
        if selected
    ]
    if len(chosen) != 1:
        raise ValueError(
            "specify exactly one action: --sql 'SELECT ...', --query NAME or --list-queries"
            + (f" (got {', '.join(chosen)})" if len(chosen) > 1 else " (got none)")
        )

    if args.list_queries:
        names = report.list_curated_queries()
        if args.json:
            print(json.dumps(names, indent=2))
        else:
            for name in names:
                print(name)
        return 0

    if not args.dataset:
        raise ValueError(
            f"{chosen[0]} requires --dataset PATH — the built analysis dataset directory "
            "(see the `build` command)"
        )

    if args.query is not None:
        # An unknown name is left to ``report.run_curated_query``, which owns the
        # "unknown curated query" message and its list of alternatives; all we do
        # here is look up the SQL so the benchmark-view requirement can be checked
        # before DuckDB is opened.
        curated = getattr(report, "CURATED_QUERIES", {}).get(args.query)
        if isinstance(curated, str):
            _require_benchmark_views(
                curated,
                label=f"--query {args.query!r}",
                benchmark=args.benchmark,
            )
        rows = report.run_curated_query(args.query, dataset=args.dataset, benchmark=args.benchmark)
    else:
        _require_benchmark_views(args.sql, label="--sql", benchmark=args.benchmark)
        rows = report.inspect_sql(dataset=args.dataset, sql=args.sql, benchmark=args.benchmark)
    _print_rows(rows, as_json=args.json)
    return 0


def cmd_ha_states(args: argparse.Namespace) -> int:
    """Read and query Home Assistant enum state history."""
    from battery_optimizer.ha_states import format_as_of_result, format_summary, load_store
    from battery_optimizer.paths import resolve_data_dir

    data_dir = resolve_data_dir(args.data_dir)
    if not data_dir.exists():
        raise ValueError(f"data directory does not exist: {data_dir}")

    store = load_store(data_dir)

    # --as-of query
    if args.as_of is not None:
        if not args.entity:
            raise ValueError("--as-of requires --entity")
        from battery_optimizer.extract_ha import parse_utc

        at = parse_utc(args.as_of, name="--as-of")
        result = store.as_of(args.entity, at)
        print(format_as_of_result(result, entity=args.entity, at=at))
        return 0

    # --list-states
    if args.list_states:
        if not args.entity:
            raise ValueError("--list-states requires --entity")
        states = store.states_for_entity(args.entity)
        if not states:
            print(f"no states found for {args.entity}")
            return 0
        for s in states:
            print(
                f"  {s.last_changed.isoformat()}  state={s.state}  "
                f"carry_in={s.carry_in}  part={s.part_name}"
            )
        print(f"({len(states)} state(s))")
        return 0

    # Default: summary
    print(f"ha-states: data_dir={data_dir}")
    print(f"  {format_summary(store)}")
    if store.entities:
        print(f"  entities: {', '.join(store.entities)}")
    if store.duplicate_rows:
        print(
            f"  dedup: {store.total_rows_before_dedup} → {store.total_rows_after_dedup} "
            f"({store.duplicate_rows} duplicate(s) collapsed)"
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the resident shadow service until SIGTERM/SIGINT."""
    from battery_optimizer.service.config import resolve_port
    from battery_optimizer.service.serve import build_service, run_server

    service, _config = build_service(
        service_config=args.service_config,
        tariff_config=args.config,
        data_dir=args.data_dir,
    )
    run_server(service, host=args.host, port=resolve_port(args.port))
    return 0


def cmd_shadow_report(args: argparse.Namespace) -> int:
    """Shadow-vs-native comparison from the recorded store."""
    from battery_optimizer.config import load_config, resolve_config_path
    from battery_optimizer.paths import resolve_data_dir
    from battery_optimizer.service.config import load_service_config, resolve_service_config_path
    from battery_optimizer.service.shadow_report import (
        build_shadow_report,
        parse_window,
        render_markdown,
    )

    config = load_service_config(resolve_service_config_path(args.service_config))
    tariff = load_config(resolve_config_path(args.config))
    tz = config.tz
    since, until = parse_window(args.since, args.until, tz, datetime.now(UTC))
    report = build_shadow_report(
        resolve_data_dir(args.data_dir),
        since=since,
        until=until,
        tariff=tariff,
        tz=tz,
        interval_seconds=config.planning.interval_seconds,
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
    return 0


def cmd_climate_shadow_report(args: argparse.Namespace) -> int:
    """Section-A gates of the climate shadow from its persisted scores and day records."""
    from battery_optimizer.climate.mpc_config import MpcConfigError, load_mpc_config
    from battery_optimizer.climate.shadow_gates import SEASON_END, evaluate, render_markdown
    from battery_optimizer.paths import resolve_data_dir
    from battery_optimizer.service.climate_shadow import RECORDS_SINCE, config_paths, read_records
    from battery_optimizer.service.config import load_service_config, resolve_service_config_path

    config = load_service_config(resolve_service_config_path(args.service_config))
    root = resolve_data_dir(args.data_dir)
    since = datetime.combine(RECORDS_SINCE, datetime.min.time(), tzinfo=UTC)
    season_end = datetime.combine(SEASON_END, datetime.min.time(), tzinfo=UTC)
    until = max(datetime.now(UTC), season_end) + timedelta(days=2)
    scores = read_records(root, "climate_scores", since, until)
    prior_id = mpc_sha = None
    if not args.all_lineages:
        prefix = args.prior or config.climate_shadow.prior
        if prefix:
            ids = sorted(
                {s["prior_id"] for s in scores if str(s.get("prior_id")).startswith(prefix)}
            )
            prior_id = ids[0] if len(ids) == 1 else prefix
        mpc_sha = args.mpc_config_sha256
        if mpc_sha is None:
            try:
                mpc_sha = load_mpc_config(config_paths(config)[1]).sha256
            except MpcConfigError as exc:
                print(f"climate-shadow-report: {exc}; pass --mpc-config-sha256", file=sys.stderr)
                return 1
    report = evaluate(
        scores,
        read_records(root, "climate_days", since, until),
        go_live=config.climate_shadow.go_live,
        prior_id=prior_id,
        mpc_config_sha256=mpc_sha,
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
    return 0


def cmd_mirror(args: argparse.Namespace) -> int:
    """Mirror dataset to SQL — deferred in Phase 1."""
    print(
        "mirror: deferred — not implemented in Phase 1 (ADR-0003 §optional).\n"
        "The optional TimescaleDB / SQL mirror is out of scope for this phase.\n"
        "See ADR-0003 'Scope boundary' table and 'Rejected or deferred alternatives'."
    )
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_HANDLERS = {
    "extract": cmd_extract,
    "validate": cmd_validate,
    "build": cmd_build,
    "build-features": cmd_build_features,
    "decompose-load": cmd_decompose_load,
    "climate-fit": cmd_climate_fit,
    "forecast-eval": cmd_forecast_eval,
    "price-eval": cmd_price_eval,
    "benchmark": cmd_benchmark,
    "report": cmd_report,
    "inspect-sql": cmd_inspect_sql,
    "ha-states": cmd_ha_states,
    "serve": cmd_serve,
    "shadow-report": cmd_shadow_report,
    "climate-shadow-report": cmd_climate_shadow_report,
    "mirror": cmd_mirror,
}


def _error_line(message: str) -> str:
    """One clean stderr line: redacted, with embedded newlines collapsed away."""
    return " ".join(_redact_error(message).split())


#: Phrase every no-clobber refusal in this codebase uses (``paths.write_part_atomic``
#: and ``manifest.write_manifest``).  It is the only signal that a caught
#: :class:`FileExistsError` is the immutable store doing its job rather than an
#: ordinary "file already exists" from some other call.
_NO_CLOBBER_MARKER = "refusing to overwrite"

#: What to tell a user who hit the immutable store.  The refusal is correct and must
#: not be weakened; only the explanation is added here.  Deliberately offers no
#: automatic cleanup: deleting canonical data is the user's decision, never the
#: tool's.
_NO_CLOBBER_GUIDANCE = (
    "This is the immutable canonical store refusing to clobber itself, so nothing was "
    "overwritten and nothing was deleted. An identical id with different bytes means "
    "something that is not hashed into the id changed since the original build — in "
    "practice the code in a dirty working tree, whose version string records only a "
    "'-dirty' marker and not its content, or a provenance field the id does not cover. "
    "To build this window again, either point --data-dir at a fresh data directory, or "
    "remove that one dataset directory yourself and re-run. Removing it is left to you."
)


def _oserror_message(exc: OSError) -> str:
    """Describe an OS-level failure, adding guidance for a no-clobber refusal."""
    message = str(exc)
    if isinstance(exc, FileExistsError) and _NO_CLOBBER_MARKER in message:
        return f"{message} — {_NO_CLOBBER_GUIDANCE}"
    return message


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns an integer exit code.

    ``ValueError`` / ``RuntimeError`` (and their domain subclasses raised by the
    pipeline modules: ``BenchmarkInputError``, ``ReportInputError``, ``SQLInputError``,
    ``BenchmarkError``, ``ReportError``) are expected user-facing failures: one
    redacted line on stderr, exit code 1, no traceback.  ``OSError`` is handled the
    same way — a read-only mount, an unreadable directory, or the immutable store's
    no-clobber ``FileExistsError`` are conditions a user can hit and act on, not
    bugs, and a bare ``FileExistsError`` is an ``OSError`` so it used to leak a full
    traceback.  A no-clobber refusal additionally gets an explanation of what
    happened and what to do about it (see :func:`_oserror_message`); nothing is ever
    deleted on the tool's own initiative.  Anything else is a bug and is allowed to
    propagate.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    quiet_http_loggers(verbose=getattr(args, "verbose", False))

    handler = _HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except NotImplementedError as exc:
        print(f"{args.command}: {_error_line(str(exc))}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print(f"{args.command}: {_error_line(str(exc))}", file=sys.stderr)
        return 1
    except OSError as exc:
        # Not a ValueError/RuntimeError subclass, so without this clause every
        # filesystem condition — including the immutable store's FileExistsError —
        # surfaced as a raw traceback.  Still only *expected* failure shapes: an
        # unexpected exception type propagates untouched.
        print(f"{args.command}: {_error_line(_oserror_message(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
