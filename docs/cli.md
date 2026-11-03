# CLI reference

Every command is run as `uv run battery-optimizer <command>` from a checkout, as `battery-optimizer <command>`
when installed, or as the container image's entrypoint (see [Container image](container.md)). Paths, environment
variables and secrets are described in [Configuration](configuration.md). There is no recurring scheduler yet:
every command here is run manually (see [No recurring scheduler yet](configuration.md#no-recurring-scheduler-yet)).
The modules that have no CLI surface are described in [Architecture](architecture.md#modules-with-no-cli-surface).

| Command        | Status       | Purpose                                        |
|----------------|--------------|------------------------------------------------|
| `extract`      | **functional** | Ingest from Prometheus / HA history / OMIE day-ahead prices to Parquet |
| `validate`     | **functional** | Validate raw Parquet against schemas + manifest hashes (prometheus, ha-history, omie) |
| `build`        | **functional** | Build 15-min analysis dataset                  |
| `build-features` | **functional** | Build the slot-aligned sub-load (or, with `--signal-set climate`, climate) feature table, apart from the canonical dataset |
| `decompose-load` | **functional** | Private report: share of load and of LF-0 error per sub-load |
| `climate-fit`  | **functional** | Private: fit and validate the climate thermal models and heat response per fold (C2) |
| `forecast-eval` | **functional** | Pre-registered load-forecaster evaluation: accuracy and paired EUR against LF-0 |
| `price-eval`   | **functional** | Pre-registered price-outlook evaluation: paired EUR against `exact` and `published_only` |
| `benchmark`    | **functional** | Run policy benchmarks on a dataset             |
| `report`       | **functional** | Generate monthly reconciliation report         |
| `inspect-sql`  | **functional** | Inspect DuckDB SQL queries                     |
| `ha-states`    | **functional** | Read Home Assistant enum state history (read-side, deduplicated, as-of) |
| `serve`        | **functional** | Resident service: shadow by default (read-only HA/Prometheus, live MPC, intents recorded never sent); `dry_run`/`live` actuator behind `mode:` ([docs](service.md)) |
| `shadow-report`| **functional** | Shadow plans vs native battery behaviour: coverage, energy, cost, SOC violations |
| `climate-shadow-report` | **functional** | Private: the preregistered section-A gates of the climate shadow (B2) from the store |
| `mirror`       | deferred     | Deferred — not implemented in Phase 1          |

## `extract`

```bash
# Prometheus (no auth, default 4 entities)
uv run battery-optimizer extract \
  --source prometheus \
  --config config/tariff.yaml \
  --lookback-days 9

# HA history (requires BATTERY_OPTIMIZER_HA_TOKEN env var)
uv run battery-optimizer extract \
  --source ha-history \
  --config config/tariff.yaml \
  --start 2026-10-14T00:00:00Z \
  --end 2026-10-23T10:59:56Z \
  --entities sensor.lavadora_current_status \
  --entities sensor.lavavajillas_current_status

# Both sources in one run
uv run battery-optimizer extract --source all --lookback-days 7
```

Time window: either `--start`/`--end` (ISO-8601 UTC, end defaults to now) or
`--lookback-days N` (start = N days ago at midnight UTC, end = now).

### Exit codes and coverage gaps

| Code | Meaning |
|------|---------|
| `0` | Success, with no coverage gap recorded (or `--allow-coverage-gaps` was passed) |
| `1` | The run failed (extractor error, bad arguments or window); no manifest for that source |
| `2` | Usage error (argparse) |
| `3` | The run **completed and committed its data and manifest**, but the manifest records a coverage gap |

Exit `3` exists so a scheduled job (the Kubernetes CronJob) does not look green while data has holes.
The data and manifest stay on disk and inspectable; stderr names every gap flag (chunk windows
included). With `--source all` both sources always run before the code is chosen, so a Prometheus
gap never skips the non-re-extractable HA pull. Pass `--allow-coverage-gaps` for interactive or
backfill runs (for example a window that starts before a series existed) to get exit `0`; the
summary is still printed.

Flags that are **gaps** (exit `3`): `empty_chunk:`, `empty_chunk_after_window_filter:`,
`chunk_accounting_mismatch`, `no_rows_for_entity:`, `no_rows_extracted:all` (HA) and
`no_samples_extracted:`, `no_series_chunks:`, `series_without_samples_chunks:` (Prometheus). A
known-dead entity (`sensor.esios_pvpc_2`) is expected to be empty and never counts. An entity that
no longer exists has a `retired_at` instant in `extract_prom.ENTITY_SPECS` (strictly after its last
archived sample): chunks starting at or after it are not queried and are disclosed as
`retired_entity_chunks_skipped:` (informational), samples at or after it are not kept, and earlier
chunks keep the gap flags above. Retired: `sensor.shellypmmini_servers_power` (2026-11-03 22:23 UTC,
renamed) and `sensor.smart_plug_v2_terrace_power` (22:46 UTC, reflashed).

Flags that stay **informational** (no exit change): `duplicate_rows_suppressed:`,
`duplicate_rows_already_stored:`, `carry_in_classification_disagreement:`,
`prior_part_unreadable:`, `duplicate_samples_dropped:`, `boundary_duplicates_dropped:`,
`non_finite_samples_dropped:`, `overlapping_target_samples:`, `dead_duplicate_entity:`,
`retired_entity_chunks_skipped:`. They say how
the run went, not that data is missing. An HA entity that simply did not change is *not* an empty
chunk: the history API returns its state at the chunk start (the `carry_in` row), so a zero-row
chunk means purged history or an absent entity.

Per-request `httpx`/`httpcore` INFO lines are hidden by default so the pipeline's own progress lines
stay readable in container logs; `-v`/`--verbose` shows them again (HA extraction still suppresses
`httpx` itself, because its URLs name the recorder host).

Re-running an overlapping or identical window is safe for the HA source: rows
already in the store are not written again, and the manifest discloses how many
were suppressed. See [Overlapping re-extraction is deduplicated at row
level](known-issues.md#overlapping-re-extraction-is-deduplicated-at-row-level).

### `extract --source omie` (OMIE day-ahead prices)

```bash
# Public files, no token. Dates are local (Europe/Madrid) delivery dates, both inclusive.
uv run battery-optimizer extract --source omie --config config/tariff.yaml \
  --start 2024-01-01 --end 2026-10-26 --allow-coverage-gaps
uv run battery-optimizer extract --source omie --config config/tariff.yaml --lookback-days 3
```

Downloads the OMIE `marginalpdbc_YYYYMMDD.1` file of every delivery day from
`https://www.omie.es/es/file-download?parents=marginalpdbc&filename=...` (a browser-like
`User-Agent`; the `parents%5B0%5D=` form answers with an HTML redirect). The file suffix is the
publication version: `.1` normally, but a few days exist only as `.2` or `.3` (and answer 404 for the
earlier suffixes), so versions 1 to 5 are tried in order and the first present one is stored
(`source_file_version`, `file_version` in the manifest). It is a **separate source**
for the price-outlook estimator ([plan PO-C](planning/price-outlook-improvement-plan.md)):

- `--start`/`--end` take `YYYY-MM-DD` (a timestamp is refused). `--end` defaults to today's local date;
  `--lookback-days N` is `[today - N, today]`. Ask for tomorrow explicitly: its file appears at about
  13:00 local on D.
- `--source all` stays prometheus + ha-history; `omie` is always named. `--entities` does not apply.
- **Store layout:** one immutable part per delivery day,
  `raw/omie/v1/parts/omie-day-<local midnight UTC>-<next local midnight UTC>-<sha8>.parquet`, and one
  manifest per run (`source: omie`) listing the new parts with `raw_sha256` (of the downloaded
  bytes), `observed_at`, `http_status`, period count and resolution, plus a per-day ledger in
  `params.days` and counts in `params.day_counts`. `params.query_template` records the URL template.
- **Schema `RAW_OMIE_DAY_AHEAD`:** `delivery_date`, `period` (1-based, local order from local
  midnight), `period_minutes` (60 or 15), `interval_start`/`interval_end` (UTC; DST-correct, computed
  from local midnight plus period steps in UTC), `price_es_eur_mwh`, `price_pt_eur_mwh` (EUR/MWh;
  the file lists PT first, ES second; ES is the one in use), `observed_at`, `source_sha256`,
  `source_file_version`, `extraction_id`. Periods are hourly (24, or 23/25 on DST days) up to
  2025-09-30 and quarter-hourly (96, or 92/100) from 2025-10-01; the resolution is inferred from the
  period count and must fit the length of the local day.
- **Idempotent:** a day whose part is already on disk is skipped without a request, so re-runs and
  overlapping windows add nothing (the manifest still records the run). A part left by a crashed run
  (no manifest) is adopted into the next run's manifest (`omie_orphan_part_adopted`, informational).
  `observed_at` of a backfilled day is the backfill time, not the publication time.
- **Polite:** at least 0.5 s between requests, retries with exponential backoff on 429/5xx/transport
  errors, a response size cap, and an abort (exit 1, no manifest, stored parts kept) after 5
  consecutive days that could not be downloaded.
- **Strict parsing:** the response is data. Header, line shape, `*` terminator, date, consecutive
  periods, a period count that fits the day and finite prices are all checked; one failure stores
  nothing for that day.

Flags: `omie_missing:<date>` (no file in any version after the day's expected publication, 14:00 local on D-1),
`omie_parse_error:<date>`, `omie_fetch_failed:<date>` and `omie_part_unreadable:<date>` are **gaps**
(exit `3`). `omie_not_published_yet:<date>` (no file before that time), `omie_orphan_part_adopted:` and
`omie_price_outside_expected_range:` (a price outside [-500, 4000] EUR/MWh, stored anyway) stay
informational.

**The canonical dataset is unaffected:** `build` reads only `source == "prometheus"` manifests and
`SCHEMA_VERSION` is unchanged, so no OMIE extraction can change a `dataset_id` (pinned by a test).

Reader for the estimator, `battery_optimizer.omie_prices.load_omie_es_prices(data_dir, start_date=,
end_date=, as_of=None)`: reads through the OMIE manifests (full SHA-256 and schema check per part) and
returns the ES price on the 15-minute UTC grid (hourly periods repeated over four slots, `NaN` where
no day is stored), the resolution each slot came from, the local delivery date per slot, the missing
dates and `first_observed_at` per day. `as_of` blanks the days not yet usable under the as-of rule of
the plan: an OMIE D+1 value may be used from **13:30 local on D** (`omie_usable_from`).

## `validate`

```bash
uv run battery-optimizer validate --source ha-history
uv run battery-optimizer validate --source prometheus
uv run battery-optimizer validate --source omie  # explicit: not part of `all`
uv run battery-optimizer validate --source all   # default: prometheus + ha-history
```

Checks per source:
- Every part file matches `RAW_SAMPLE` / `RAW_ENUM_STATE` schema
- Filename content hash matches actual SHA-256 of file bytes
- Every manifest's recorded part hashes match the actual files on disk
- No zero-row parts, no unreadable parts
- `omie` also checks each day: one delivery date matching the file name, periods `1..N`, a 60 or
  15 minute resolution that fits the local day (DST: 23/25 hourly, 92/100 quarter-hourly),
  contiguous UTC intervals from local midnight to the next, finite prices, and a single part per day

Exit code 0 if all checks pass; 1 otherwise.

## `build`

```bash
uv run battery-optimizer build \
  --start 2026-10-01T00:00:00+00:00 \
  --end 2026-10-23T00:00:00+00:00 \
  --assume-no-battery  # default; use --no-assume-no-battery to refuse
```

The `--assume-no-battery` flag is a **recorded assumption, not a detection**: missing battery
telemetry cannot prove a battery does not exist. The assumption is pinned in the build manifest.
When `--no-assume-no-battery` is set, the build requires registered battery telemetry to
reconstruct gross load; without it the build refuses.

**Battery commissioning:** `battery.commissioned_at` and the two AC energy counters
(`entities.battery_charge_energy`, `entities.battery_discharge_energy`) live in
`config/service.yaml` (override with `BATTERY_OPTIMIZER_SERVICE_CONFIG`; deliberately not in
`tariff.yaml`, whose hash is part of every dataset id). A window that ends at or before that instant
(or with none recorded) is built exactly as before. A window that reaches it reads the counters, and
every interval ending after that instant (one that merely touches it counts) gets
`gross_kwh = raw_grid_kwh - charge + discharge` with `battery_absent=false`; `--assume-no-battery`
never covers those intervals. An interval whose counter is unknown has `gross_kwh` null and
`energy_quality` `battery_counters_missing` (or `battery_gross_negative` when the meters disagree). If
the counters are not configured, or have fewer than two samples in the archive, the build exits 1
instead of publishing. With no `commissioned_at` recorded, the build also exits 1 when a raw manifest
declares an entity named like a battery (`battery`, `charging_energy`, `discharging_energy`).
Extract the counters with `extract --source prometheus --entities <charge>,<discharge>`, in a run of
their own.

**Immutable store:** the canonical data directory is write-once, content-addressed. Re-running
`build` for a window that already exists is idempotent when the bytes match. When they do not —
`refusing to overwrite .../build_manifest.json with different content (existing=…, new=…)` —
the build **refuses** with one line on stderr and exit code 1; it does not print a traceback, and
it never overwrites or deletes anything. The dataset id covers source part hashes, config hash,
code version, schema version, window and builder parameters, so an identical id with different
bytes means something outside those inputs changed since the original build — in practice code in
a dirty working tree, whose version string records only a `-dirty` marker, or a provenance field
the id does not cover. Fix it on your side of the boundary: point `--data-dir` at a fresh data
directory, or remove that one dataset directory yourself and re-run.

## `build-features`

```bash
uv run battery-optimizer extract --source prometheus --config config/tariff.yaml \
  --start 2024-01-01T00:00:00Z --end 2026-10-01T00:00:00Z --allow-coverage-gaps \
  --entities sensor.shellypmmini_servers_power,sensor.venus_ac_offgrid_power,sensor.aquarea_heat_power_consumed
uv run battery-optimizer build-features \
  --start 2024-01-01T00:00:00Z --end 2026-10-01T00:00:00Z \
  --dataset ~/data/energy-baseline/analysis/v1/<dataset_id>
```

Builds a slot-aligned 15-minute UTC table of the observable sub-loads from the raw samples of the
feature entities (the servers circuit, heat-pump consumed and produced power, outdoor temperature
and the AppDaemon control states; `extract_prom.FEATURE_ENTITY_IDS` also lists the climate group,
which belongs to the climate set below and must be extracted alone). Each power series becomes kWh per
slot by sample-and-hold integration; a gap longer than `--max-gap-seconds` (300) is not held, and
a slot with less than `--min-coverage` (0.8) covered is `unknown`, never zero. The rule is spelled
out in the `features` module docstring and pinned in the manifest. The servers circuit is
`sensor.shellypmmini_servers_power` before `features.SERVERS_ON_BACKUP_PORT_SINCE` (2026-11-03, when the
servers moved to the battery backup port) and `sensor.venus_ac_offgrid_power` from it on; each entity is
read only inside its window (`Signal.successors`, listed under `signals` in the manifest).

The table is written to `<data_dir>/analysis/v1/features/<feature_set_id>/` with a
`features_manifest.json` (hashes of every raw part read, code version, parameters, quality checks:
units, cadence, gaps per month, COP sanity, long constant runs). It never touches a canonical
dataset directory, and the feature entities are not default entities, so `extract` without
`--entities` and the canonical `dataset_id` are unchanged. A raw manifest that declares only
feature entities is also ignored by `build`'s lineage hash. With `--dataset`, the
`servers + heat pump <= main` check against that dataset's gross load is written to
`quality/main_meter-<dataset_id12>.json` next to the manifest.

### Climate signal set (`--signal-set climate`)

```bash
# The climate entities are their own extraction group (`extract_prom.CLIMATE_ENTITY_IDS`).
uv run battery-optimizer extract --source prometheus --config config/tariff.yaml \
  --start 2026-01-12T00:00:00Z --end 2026-01-15T00:00:00Z --allow-coverage-gaps \
  --entities 'climate.thermostat_bedroom,climate.thermostat_bedroom#setpoint,sensor.aquarea_tank_temperature'
uv run battery-optimizer build-features --signal-set climate \
  --start 2026-01-12T00:00:00Z --end 2026-01-15T00:00:00Z
```

`--signal-set` is `default` (the sub-load table above, unchanged and with the same feature-set id)
or `climate`: a **separate** feature set, with its own id and directory next to the default ones,
for the heat-pump climate work. Per 15-minute UTC slot it carries the mean room temperature and
setpoint of each thermostat (`room_<room>_temp_mean_c`, `room_<room>_setpoint_mean_c`; rooms in
`extract_prom.CLIMATE_ROOMS`, with `features.COMFORT_EXCLUDED_ROOMS` marking office A as a heat
sink, recorded under `climate.rooms` in the manifest), outdoor, tank, flow, return and flow-target
temperature, mean compressor frequency and pump flow, compressor starts, heating, hot-water and
cooling electric and thermal energy (kWh integrated from power, and `*_energy_*_delta` from the
monotonic energy totals), the aerothermal branch (Shelly channel 2), the servers circuit (an
internal gain of office A) and the force-DHW and AppDaemon helper states. The coverage and `measured`/`partial`/`unknown` rules are the same as above; nothing
is zero-filled. A counter (`*_delta`) is the increase of the counter per slot: a drop to under half
the previous value is a reset and counts the value read after it, a smaller drop is a rollback and
counts nothing (the energy totals slip back by tenths of a kWh and then climb again in normal
steps, so the climb is counted); both are counted in the manifest quality (`counter_resets`,
`counter_rollbacks`). `--dataset` works with this set too (it carries the servers and heat-pump
power signals the check needs).

The climate group (`extract_prom.CLIMATE_ENTITY_IDS`) must be extracted **alone**. `extract` refuses
a run that names a climate entity together with any other entity: a default or unregistered entity
would enter the canonical lineage and change the `dataset_id`, and an earlier feature entity would
make the default feature set read the manifest and change its `feature_set_id`. The entities both
sets use (outdoor temperature, servers circuit, heat/DHW/cool power, force-DHW and the AppDaemon
helpers) are extracted once, with the earlier feature entities, and the climate set reads them from
that extraction. Quote `--entities` values that contain `#`.

A thermostat has two series under one entity: `climate.thermostat_x` is its current temperature and
`climate.thermostat_x#setpoint` its target temperature (`extract --entities` takes both,
and the manifest's `entities_without_samples` names series the same way; the `#setpoint` suffix is a registry key, the Prometheus query and the stored `entity` are the plain
entity id). The numeric operating-mode, defrost and 3-way-valve sensors are not registered yet.

## `decompose-load`

```bash
uv run battery-optimizer decompose-load \
  --features ~/data/energy-baseline/analysis/v1/features/<feature_set_id> \
  --dataset ~/data/energy-baseline/analysis/v1/<dataset_id>
```

Writes `p3-report/summary.{json,md}` inside the feature-set directory (private: it holds energy and
price figures). For each season and month it reports the share of kWh from servers, heat pump and
the residual, runs LF-0 (28-day trailing mean) on the main load and on every component with one
common mask, and reports each component's error, its share of the error variance and its
price-weighted error, overall and for the top price tercile. It also replays the AppDaemon climate
and DHW rules on the day's prices to show how much heat-pump energy they explain.

## `climate-fit`

```bash
uv run battery-optimizer climate-fit \
  --feature-set <climate feature set id> \
  --dataset ~/data/energy-baseline/analysis/v1/<dataset_id>
```

Fits the C2 thermal models (first-order A and slab + air B), their observers and heat responses
and the compressor-starts model on the fold of [`config/climate.yaml`](../config/climate.yaml)
(`--climate-config` to use another file; fit S2, test S3), refits the thermal parameters causally
every week of the test season on the trailing window, shrunk toward the training fit, fits the COP
model, validates on the test season and records the acceptance verdict of the
[C2 design](planning/climate-optimizer-implementation.md#c2-model-iteration-the-one-predeclared-iteration-config-version-2).
It reads the climate feature set, the dataset's PVPC prices (for the replayed legacy permission `u`),
the private site file `<data_dir>/climate/site.yaml` and its reference station's stored months
(needed for the long-gap outdoor fill); it writes nothing else and no runtime or actuator is touched.
It refuses to run while `model.adaptive.shrinkage` is unset: that value is chosen on S2 only by
`hack/climate_shrinkage.py` (same arguments, writes
`<data_dir>/analysis/v1/climate-shrinkage/<inputs>-<config>/`) and committed before the run.

Output: `<data_dir>/analysis/v1/climate-models/<model_id>/` with `model.json` (parameters per fold
and model, physical reading, fit diagnostics, observer, response, room offsets and starts model),
`validation.json` (Q-driven and u-driven forecast errors, daily energy, the legacy replay's
degree-hours, every acceptance criterion with its value and status, the A vs B selection) and
`validation.md`. `model.json` also holds every weekly refit; `validation.json` the outdoor fill
share, the COP validation and the legacy replay's electricity and cost. `model_id` is the SHA-256 of
the feature-set id, the dataset id, the fold windows, the code version, the config hash, the
site-file hash and the input hash (which covers the station manifest and the outdoor source). The files are written
atomically and never overwritten: the same id with different bytes is refused. The data dir must
lie outside a git tree. The output holds private figures: it stays in the data dir.

The command prints the directory and the verdict: `ACCEPTED` or `UNDETERMINED` when no model
passes. The COP limits and the legacy-replay electricity and cost (simulated heat priced through the
COP lift table) are acceptance criteria; the model-iteration budget of the plan is never used by
the command itself. See
[Architecture](architecture.md#climate-subpackage-climate) for the method.

## `forecast-eval`

```bash
uv run battery-optimizer forecast-eval \
  --dataset ~/data/energy-baseline/analysis/v1/<dataset_id> \
  --name p2-cheap \
  --candidate trailing_mean:window_days=14 \
  --candidate <registered name>[:key=value,...] \
  --p0-summary ~/data/energy-baseline/analysis/v1/<dataset_id>/forecast-eval/<p0 run>/summary.json
```

The evaluation harness of the [load-forecaster plan](planning/load-forecaster-improvement-plan.md)
(P1). Each `--candidate` is a registered forecaster with optional param overrides; LF-0
(`trailing_mean`, default params) is always the reference and is added when missing. `--period`
(repeatable) picks the evaluation windows, by default both P0 periods: `2025`
(2025-01-01 to 2026-01-01 UTC) and `2026-01..09` (2026-01-01 to 2026-10-01 UTC). `--resamples`
(default 10000, at least 5000) and `--seed` (default 20261025) set the bootstrap.

It writes a private run to `<dataset>/forecast-eval/<name>-<UTC timestamp>/`:

- `preregistration.json`, written before any candidate runs: candidates and params, periods,
  policies, metric definitions, bootstrap seed, and gate G2 with the P0 gaps it reads.
- `summary.json` and `summary.md`: forecast accuracy, the paired economic result, the gate verdict,
  sanity checks and provenance (git commit and dirty flag, tariff and evaluation config hashes,
  dataset id, forecasters, every benchmark run id, runtime per candidate).

`--gate G4` pre-registers the structured forecaster's gate (`--gate-candidate`, `--gate-reference`,
default reference `trailing_mean`), and `--compare CANDIDATE=REFERENCE` (repeatable) adds paired
comparisons that are reported but never gated. A candidate alias (`structured@lf1:features=<feature
set id>,residual_forecaster=trailing_median`) names it in those options. The tariff and data
directory come from `--config` and `--data-dir`; the feature set is read from
`<data dir>/analysis/v1/features/`. `--label` and `--note` (repeatable) record free text in the
pre-registration, for the role of a run (for example a second look at periods that were already
seen) and for what its result may be used for. A structured candidate with an OMIE
`price_outlook` also reads the OMIE day-ahead store of `--data-dir`.

The method (decision instants, metrics, block bootstrap, gates, folds) is described in
[Benchmark reference § Forecast evaluation](benchmark.md#forecast-evaluation-forecast-eval).
`benchmark --eval-end` is the flag the harness uses to cut a period out of a longer dataset.

## `price-eval`

```bash
uv run battery-optimizer price-eval \
  --dataset ~/data/energy-baseline/analysis/v1/<dataset_id> \
  --name po-a \
  --reference-summary ~/data/energy-baseline/analysis/v1/<dataset_id>/forecast-eval/<g1 run>/summary.json
```

The evaluation harness of PO-A in the [price-outlook plan](planning/price-outlook-improvement-plan.md).
Each `--outlook` (repeatable) is a registered price outlook; the default is `exact`,
`published_only` and `persistence`, and the two references `exact` (upper bound) and
`published_only` (the live service) are always included. `--period`, `--resamples` and `--seed`
work as in `forecast-eval`. `--features <feature set id>` adds the replayed climate-mask overlap
of each outlook's estimate, and `--label` records free text in the pre-registration. Passing an OMIE
outlook (`omie_period`, `omie_period_month`) also evaluates gate GC against `persistence` and reads
the OMIE day-ahead store of the data directory. `--reference-summary` names a `forecast-eval`
summary whose
`trailing_mean` benchmark runs are compared slot by slot with this run's `exact` rolling-horizon
schedule, which shows that the default outlook reproduces the pre-seam result.

It writes a private run to `<dataset>/price-eval/<name>-<UTC timestamp>/`: `preregistration.json`
(written before any benchmark runs), and `summary.json` / `summary.md` with the paired economic
result, the action differences at the 14:00 events, the price-estimate metrics, the reference
reproduction and provenance. The method is described in
[Benchmark reference § Price-outlook evaluation](benchmark.md#price-outlook-evaluation-price-eval).

## `benchmark`

Runs the policy ladder (`no_battery`, `causal_price`, `day_ahead`, `rolling_horizon`, `oracle`) on a built
dataset and writes the benchmark artifacts into the dataset's `benchmarks/` directory. `--train-end` is
required and has no default. The flags, the policy semantics, the battery assumptions and the
methodology notes are long enough to have their own page: see the
[benchmark reference](benchmark.md).

## `report`

```bash
uv run battery-optimizer report --dataset ~/data/energy-baseline/analysis/v1/<dataset_id>
uv run battery-optimizer report --benchmark ~/data/energy-baseline/analysis/v1/<dataset_id>/benchmarks/<run_id>
```

`--dataset` must contain exactly one benchmark run; if multiple runs exist, the error lists the
available run ids rather than silently picking the newest. `--benchmark` overrides `--dataset`.
`--config` is optional but recommended: when passed, its canonical hash is checked against the one
pinned in the run and a mismatch refuses (`ReportError`) instead of reporting against another tariff.

**The report states its own horizon scope.** `report.md` carries a *Horizon scope* line under
*Evaluation window*, and `report.json` carries the same sentence as `horizon_scope_note`: the horizon
is a **UTC-scoped window, not a calendar year in the dataset's local timezone**, unless both bounds
genuinely fall on local midnights, in which case the line says it is a local calendar year too. The
sentence is derived from the run's own bound labels and the build manifest's pinned `params.timezone`,
so it hardcodes no year — the rule in the [benchmark reference](benchmark.md#usage-and-flags) ("the claim is UTC-scoped and the report
must say so") is now enforced by the report instead of left to `Z`-suffixed labels to imply it.

Reports are written into the run directory with the same no-clobber rule as every other artifact, so a
changed report does not silently rewrite a run that has already been reviewed: re-generating a report
whose bytes differ raises `FileExistsError`, which the CLI surfaces as one line and exit code 1 — it is
intended. An existing run's `report.md` and `report.json` are evidence, not a rendering to refresh; the
tool never deletes canonical data on your behalf, so if a report genuinely must be rebuilt, remove that
one file by hand and know that its bytes will differ from what was reviewed.

## `inspect-sql`

```bash
uv run battery-optimizer inspect-sql --list-queries
uv run battery-optimizer inspect-sql --dataset ~/data/energy-baseline/analysis/v1/<dataset_id> \
  --query price_distribution
uv run battery-optimizer inspect-sql --dataset ~/data/energy-baseline/analysis/v1/<dataset_id> \
  --sql "SELECT * FROM intervals WHERE price_eur_per_kwh > 0.20" \
  --benchmark ~/data/energy-baseline/analysis/v1/<dataset_id>/benchmarks/<run_id>
```

Exactly one action is required: `--sql`, `--query`, or `--list-queries`. `--sql` and `--query` are
mutually exclusive. Results print as aligned text by default, or JSON with `--json`. The SQL is
validated as a single read-only statement (SELECT / WITH ... SELECT / EXPLAIN SELECT); anything
else is refused at the grammar level.

`benchmark_results` and `benchmark_schedules` exist only when `--benchmark PATH` is given. Curated
queries that read them (`monthly_cost`, `excluded_intervals`, `benchmark_summary`) and any ad-hoc
SQL naming them are therefore refused up front, before DuckDB is opened, with one line naming the
missing view and telling you to pass `--benchmark` — instead of leaking a raw
`duckdb.CatalogException` traceback. Dataset-only queries (`price_distribution`,
`quality_summary`, `dataset_meta`) work without it.

## `ha-states`

```bash
# Summary of the store: parts, rows before and after dedup, entities present
uv run battery-optimizer ha-states

# Every deduplicated state for one entity, in order
uv run battery-optimizer ha-states --entity sensor.lavadora_current_status --list-states

# What that entity's state was at an instant (inclusive at the transition boundary)
uv run battery-optimizer ha-states --entity sensor.lavadora_current_status \
  --as-of 2026-10-20T12:00:00Z
```

Reads the Home Assistant appliance enum history that `extract --source ha-history` preserves, through
`ha_states.py`. It exists because the durable Prometheus archive cannot carry these values at all —
the exporter emits state-value series only for numerically convertible states — and because the
recorder keeps them for about ten days, so the Parquet store is the only surviving copy.

- **Deduplication happens at read time**, on the same identity the extractor uses:
  `(entity, last_changed, last_updated, state, attributes_json)`. Part files are immutable and 12 known
  pre-dedup duplicate rows were **deliberately left in place** rather than rewriting history, so collapsing
  them is the reader's job; see
  [Overlapping re-extraction is deduplicated at row level](known-issues.md#overlapping-re-extraction-is-deduplicated-at-row-level).
- **As-of queries are inclusive at the transition boundary**: at exactly a transition instant the new state
  is visible, because `last_changed` is when the state became effective. Only rows with
  `last_changed <= T` are returned, which makes it *structurally* impossible to leak a later transition into
  an earlier snapshot — ADR-0002's as-of requirement, not a convention to remember.
- `carry_in` and both timestamps (`last_changed` = event time, `last_updated` = observation time) are
  preserved verbatim; consumers must filter `carry_in=False` before treating `last_changed` as a real
  event. Unavailable/unknown states are surfaced, not silently dropped.
- `--as-of` and `--list-states` both require `--entity`; a refusal is a `ValueError`, which the CLI prints
  as one line with exit code 1.
- **Verified live against the real store, read-only:** `parts=12 rows_before_dedup=51 rows_after_dedup=39
  duplicates=12 entities=2` for `sensor.lavadora_current_status` and `sensor.lavavajillas_current_status`.

**This is a read-side module.** It does not feed `build`, does not enter `dataset_id`, and no policy or
benchmark reads it. No appliance modelling, NILM or forecasting was built on it: it closes the long-deferred
"there is no reader" gap and nothing more, and ADR-0002 Phase 3 remains the place where consumption would
start.

## `climate-shadow-report`

Prints the preregistered section-A gate report of the [climate shadow](service.md#climate-shadow-track-b2)
from the records the service persisted (`climate_scores`, `climate_days`); nothing is fetched.

```bash
battery-optimizer climate-shadow-report [--data-dir <store>] [--service-config <yaml>] \
    [--prior <id>] [--mpc-config-sha256 <hash>] [--all-lineages] [--format md|json]
```

`--service-config` supplies `climate_shadow.go_live` (without it there is no new-window verdict; the first
`comfort` record is shown as a provisional date only), the default prior and the default MPC config hash.
Only the records of that lineage are evaluated (`--all-lineages` mixes them, as a diagnostic). Per window
(legacy, new): valid heating days, each criterion with value, threshold and PASS /
FAIL / UNDETERMINED (fewer than 6 weeks of valid heating days is UNDETERMINED), RMSE, persistence RMSE,
skill and bias at 1, 6, 12 and 24 h, the daily heat MdAPE, solver statistics, the top-offset share and the
descriptive plan-over-rule electricity ratio. The figures are private: keep the output with the data.
Exit code 0; a malformed service config is an error.

## `mirror`

Deferred — not implemented in Phase 1 (ADR-0003 §optional). The optional TimescaleDB / SQL mirror
is out of scope for this phase.
