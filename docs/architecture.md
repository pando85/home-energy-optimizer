# Architecture

The component diagram is in the [README](../README.md#architecture).

The package has two halves. The offline pipeline (`extract`, `validate`, `build`, `benchmark`, `report`;
see the [CLI reference](cli.md)) ingests Prometheus and Home Assistant history into an immutable,
content-addressed store and scores dispatch policies on it. The resident [shadow service](service.md) reads
Home Assistant and Prometheus, runs the same MPC kernel live and records plans that are never sent anywhere.
Decisions are recorded in [`adr/`](adr/); the production architecture is
[ADR-0004](adr/0004-battery-optimizer-production-architecture.md).

## Proposed climate-planning extension

[ADR-0005](adr/0005-shared-energy-planning-climate.md) proposes reusing this **read-only** resident runtime for a domain-specific thermal/DHW planner rather than creating a second ingest/optimization deployment. It keeps AppDaemon's existing HeishaMon actuation separate and explicitly gated. No climate planner, ClimatePlan endpoint or AppDaemon plan consumer is implemented yet; see the [phased climate plan](planning/climate-optimizer-implementation.md). Battery dispatch remains shadow-only and unchanged.

## Planning components

The README [ASCII component diagram](../README.md#architecture) is a **logical architecture**:
the four named DECIDE components are responsibilities within one Python package, not four
microservices, new runtime processes or existing four plugin interfaces. The offline tools and
the resident shadow service reuse the same battery/optimization primitives, with intentionally
different information availability and orchestration rules. **No code refactor is required**
to make these boundaries explicit.

| Component | Owns / input | Current implementation and output |
|---|---|---|
| **[Load Forecaster](planning/components/load-forecaster.md)** | Predict future **gross household demand** (kWh per UTC slot) from causal load history | `load_forecast.py` registers the named forecasters behind one call contract (`run_forecaster`); `service/history.py` (`forecast_load`, `LoadForecast`) runs `planning.load_forecaster` through it; `rolling_horizon.py` supplies causal/frozen offline forecast modes; `structured_forecast.py` registers the offline structured forecaster `structured` (LF-1: replayed AppDaemon heat-pump schedule, fitted energy per hour, residual forecaster); `forecast_eval.py` is the offline evaluation harness (`forecast-eval`). Live baseline is the trailing 28-day same-slot mean with a local-midnight information cutoff. |
| **[Price Outlook](planning/components/price-outlook.md)** | Supply prices **known at decision time**, publication timestamps and the available priced horizon | `service/pvpc.py` (`parse_pvpc_state`, `PriceCurve`) reads published PVPC attributes and handles Europe/Madrid DST; `price_outlook.py` registers the offline price outlooks behind one call contract (`run_price_outlook`: `exact`, `published_only`, `persistence`) that `rolling_horizon.py` plans on, with `price_eval.py` as their evaluation harness (`price-eval`). **The live service does not forecast unpublished prices:** its horizon ends at the last published slot. |
| **[Battery Model](planning/components/battery-model.md)** | Supply physical state and constraints: capacity, SOC bounds, charge/discharge power, one-way efficiencies, grid import/export accounting | `battery.py` (`BatterySpec`, `Horizon`, numeric physics/validation), battery configuration resolved via `benchmark.resolve_battery_assumptions`; `service/virtual_battery.py` replays shadow decisions. The shipped **0.80 round-trip efficiency is an assumption**, not a device measurement. |
| **[Dispatch Optimizer](planning/components/dispatch-optimizer.md)** | Find feasible, low-cost charge/discharge decisions from **Load Forecast + Price Outlook + Battery Model**, current SOC and economic assumptions | `service/planner.py` freezes a `PlanningSnapshot`, invokes the shared `oracle.solve_oracle` (SciPy/HiGHS), and produces `DispatchPlan`/`DispatchIntent` via `dispatch.py`; `rolling_horizon.py` is the offline policy/replanning harness. The service re-solves every 15 minutes and on selected changes, but remains **shadow-only**. |

What each component was calibrated on, what it assumes and what invalidates it is recorded per
component in [components](components/README.md), with the [regime log](components/regime-log.md) of real
plant and control changes. Update them when a component or the plant changes.

### Boundaries and information flow

- The **Load Forecaster** emits time-aligned demand predictions. It does not choose battery
  actions or know future measured consumption. AppDaemon schedules and appliance-state
  history are **potential future features**, not currently incorporated in its baseline.
- The **Price Outlook** distinguishes published prices from possible estimates. There is
  currently no live price prediction; offline simulations with noisy future prices are
  not evidence of a production forecasting service. Persist **decision-time publication
  snapshots** before evaluating any new estimator against future outcomes.
- The **Battery Model** describes physics and allowable state transitions. It is an
  independent input to the optimizer, **not a third forecast** nor a downstream stage
  of either forecaster. When the real battery is commissioned, measured SOC replaces
  `soc_source: simulated`; efficiency/power characteristics must be measured before
  treating model upgrades as calibrated.
- The **Dispatch Optimizer** does not ingest raw HA events directly and does not own
  the physical regulation loop. The service assembles a frozen, hashable
  `PlanningSnapshot`, solves off the asyncio event loop and records intents.
  `DispatchPlan` is **not** a battery command or proof of achieved power.
- **SENSE** includes the HA/Prometheus read adapters, freshness and offline
  extraction/validation. **LEARN** contains immutable offline benchmarking, the
  shadow sink, virtual replay, reports and operational metrics. Neither controls
  hardware. **ACT** remains a future safety guard and HA/Omnibattery adapter;
  Omnibattery must retain sole Modbus ownership and fast Shelly-based feedback.

### Evolution: improve one component at a time

The table below is a **navigation summary**, not a second implementation checklist. Each
component has its own detailed roadmap (linked above) with milestones, measurable
acceptance tests, dependencies and stop rules. The existing
[implementation plan](planning/home-battery-optimizer-implementation.md) remains the
source of truth for cross-component production/hardware safety gates; the
[system roadmap](planning/energy-battery-roadmap.md) remains a global status overview.

| Component | Current baseline | Next candidate | Later / acceptance evidence |
|---|---|---|---|
| **Load Forecaster** | Causal trailing-slot-mean, 28 days | Condition on known server base load, AppDaemon schedules and HA appliance states | Compare LightGBM/CatBoost and P10/P50/P90 models using chronological holdouts; **promote only on out-of-sample net EUR savings/regret**, not MAE alone. Prevent target leakage and record feature availability at decision time. |
| **Price Outlook** | Published PVPC values only, with UTC/DST normalization | Archive decision-time publication snapshots and audit their completeness | Predict only the *unpublished* horizon where useful; compare estimates, probabilistic scenarios and **decision regret** to the published-only baseline. Never replace published prices with guesses. |
| **Battery Model** | Assumed constant RTE 0.80, nameplate power/capacity and SOC constraints | Measure AC efficiency vs power/SOC and standby draw; characterize BACKUP / AC bypass | Introduce calibrated rate-dependent or piecewise-linear physics **while retaining solver feasibility**; validate SOC, power and energy error on held-out cycles, including uncertainty. |
| **Dispatch Optimizer** | 15-minute shadow MPC using HiGHS, published price horizon and explicit terminal/throughput knobs | Calibrate throughput/degradation cost, residual energy value, import ceiling and replan triggers | Explore robust/scenario MPC only if it improves feasible net EUR savings, SOC/risk constraints and solver latency in like-for-like backtests and shadow comparison. |

**Iteration rule:** treat these as named seams first, not a mandate to create
`LoadForecaster`/`PriceOutlook`/`BatteryModel`/`DispatchOptimizer` classes or move
files now. Make an implementation-level interface change only when a second
implementation or measurable benefit warrants it. New algorithms must work
through the **same shared physical/solver kernel and causal input semantics**
used by benchmark and shadow operation; report forecast metrics *and* dispatch
economics with matching datasets, costs and terminal SOC assumptions.

**Safety and commissioning:** HA/Omnibattery battery telemetry is absent from
the current installation, so the shadow service runs with simulated SOC.
The ACT stage (device writes, safety/failover, HA-loss behavior) is neither
implemented nor authorized; [ADR-0004](adr/0004-battery-optimizer-production-architecture.md)
and the [production plan](planning/home-battery-optimizer-implementation.md)
keep its commissioning gate separate.

## Modules with no CLI surface

These have no CLI surface (or `hack/` only), deliberately:

- `dispatch.py` — the dispatch **intent contract** (see [Dispatch intent contract](#dispatch-intent-contract-dispatchpy)).
  Typed, validated, canonically serialized `DispatchIntent` / `DispatchPlan`. No writer, no I/O,
  no subcommand. This package still actuates nothing.
- `artifact_integrity.py` — output-manifest sidecars for the benchmark and report stages
  (see [Artifact integrity](#artifact-integrity-artifact_integritypy)).
- `economic_sensitivity.py` — pure rescoring / re-optimisation under user-supplied throughput-cost
  and terminal-value sweeps (see [Economic sensitivity](#economic-sensitivity-economic_sensitivitypy)). Experiment only, not part of the
  canonical benchmark pipeline.
- `omie_prices.py` — read side of the OMIE day-ahead store (`extract --source omie`, see
  [CLI](cli.md#extract---source-omie-omie-day-ahead-prices)): ES prices on the 15-minute UTC grid,
  per-day `first_observed_at` and the as-of rule. A library function for the price estimator; it
  never writes and is not a `build` input.
- `replay_runtime.py` — replay-only asyncio dispatch runtime with a recording sink. No writer,
  no actuation, no HA adapter, no wall clock. See [Replay runtime](#replay-runtime-replay_runtimepy).
- `climate/` — data layer, heat-pump electricity model, thermal models (C2), the comfort MPC (B1) and
  its live shadow (B2, run by `serve`). See [Climate subpackage](#climate-subpackage-climate).

## Dispatch intent contract (`dispatch.py`)

Typed, validated, canonically serialized `DispatchIntent` / `DispatchPlan`. Clock is a **required
caller argument with no default**. **No writer, no I/O, no CLI subcommand.** See module docstring.

## Artifact integrity (`artifact_integrity.py`)

**Status: complete — committed and wired into the canonical writers.** Output-manifest sidecars for benchmark
and report stages: `finalize_run_outputs` runs after the three benchmark artifact files, and the report
reader verifies the benchmark sidecar before consuming the payload and writes its own sidecar after.
**Detects integrity, not authenticity** (no signature claim). Legacy runs without a sidecar are
accepted but marked unverified. CLI: `hack/verify_artifacts.py <run-dir> [--report]`. See module
docstring.

## Economic sensitivity (`economic_sensitivity.py`)

**Status: complete — committed, experiment module.** Sweeps under **user-supplied hypothetical**
throughput costs and terminal values, re-optimizing at every parameter pair rather than rescoring a
fixed schedule. Break-even uses the **closed-form algebraic intersection** of the two schedules'
cost lines (accounting for unequal initial SOC), not a binary search. **Not a physical aging model,
no calibration.** Terminal optimization vs scoring separated. Runner: `hack/economic_sensitivity.py` (oracle only); `hack/throughput_sweep.py` sweeps the throughput
cost on the realistic `rolling_horizon` policy and the oracle over several windows and reports relative
saving, EFC and the knee (the wear-cost decision is in [battery-model](components/battery-model.md#wear-cost-decision-2026-10-31)).
See module docstring.

## Replay runtime (`replay_runtime.py`)

**Status: complete — committed. NOT deployed, NOT a resident service, NO HA adapter, recording
only.** Deterministic plan content; record ids are per-run random (`generation_id` is `uuid.uuid4()`). Caller-supplied time; recording-only sink; injectable planner.
Demo: `python -m battery_optimizer.hack.replay_runtime_demo`. See module docstring.

## Climate subpackage (`climate/`)

**Status: C2 PRs 1 to 3 and the model iteration — configuration, data loader, audit, heat-pump
electricity (COP) model, thermal models with causal weekly refits, heat response and `climate-fit`;
the B1 comfort MPC and its read-only live shadow (B2); no actuation.** The
offline half of the [climate optimizer plan](planning/climate-optimizer-implementation.md#c2-design-and-predeclared-gate).
It reads the climate feature set (`build-features --signal-set climate`) and the PVPC prices of a
canonical dataset, and never writes into either.

- `config.py` and [`config/climate.yaml`](../config/climate.yaml): the heat-v1 comfort band, training
  filters, parameter envelope, model-acceptance criteria and gate thresholds, committed before any
  model result exists. Loading is strict (unknown, missing or duplicated keys, wrong types and
  inconsistent ranges are errors) and the file's SHA-256 identifies it in every result.
- `data.py`: `load_climate_data` verifies the feature set through its manifest and returns
  `ClimateData`, numpy arrays on the 15-minute UTC grid with a local-day partition (92 / 96 / 100
  slots): `T_air` (comfort-room mean), `Q_heat` with its daily
  cross-check against the energy counter, the DHW and cooling masks, the inferred valve state, the
  clear-sky solar proxy, the legacy permission `u` replayed through
  `structured_forecast.replay_mask_for_day` (decided 40 s into each local day) and one boolean per
  training filter. `u` is `NaN` (and `u_known` false) on every day without a replayed schedule,
  including disabled-rule days, which are excluded; u-driven code must use `u_known` only.
  The Aquarea outdoor sensor reads low while the compressor runs, so the model's `t_out_debiased`
  keeps only slots with the compressor off for at least 30 minutes and interpolates linearly across
  running periods up to a configured cap (longer ones are unknown); raw `t_out` stays for
  diagnostics. Per-room offsets are the function `room_offsets(..., mask)`, not a field, so a fold
  fits them on its own training mask and nothing held out can leak.
- The site coordinates (and an optional always-open valve share `phi`) are read from
  `<data_dir>/climate/site.yaml` (`latitude`, `longitude`, optional `phi`). The file is private,
  never committed, and its hash is recorded with the data.
- `thermal.py`: model A, first order (`C dT/dt = Q + UA (T_out - T) + q0 + g_s S`, with
  `C = tau UA`), and model B, slab + air (`C_a`, `C_s`, coupling `k`), on the input
  `[Q, T_out (t_out_model), clear-sky proxy, 1]` per slot, discretised exactly under a zero-order
  hold (`scipy.linalg.expm`, 900 s). A slot's reading is compared with the state at the end of the
  slot. The fit is bounded simulation-error least squares over segments: runs of slots with known
  inputs inside 48 h windows from local midnight, started from the measured air temperature of the
  slot before; every slot with a valid air temperature is scored (`fit.valid_air`: not unknown
  quality, open window, thermostat outage or cooling; valve-shut and rule-disabled slots are plant
  states whose heat drives the simulation, so they are scored too). With a prior and a shrinkage
  `lambda` the cost adds `lambda * sum(((p - p_prior) / box width)^2)`. B's initial slab temperature per
  segment is projected out in closed form, and the return temperature in steady circulation
  (`data.steady_circulation`) is its second measurement `T_s + b_ret`, weighted by its own residual
  scale after a first pass. A parameter on a bound of its search box (the envelope where it has
  one), B's `tau = (C_a + C_s) / UA` outside the envelope, or a relative standard error above 0.5
  from the Jacobian at the optimum (not identifiable) fails the fit. A steady-state Kalman filter
  (`solve_discrete_are`, thermostat noise 0.05 K, process noise from the training one-step errors)
  gives the state at forecast origins; across a slot whose outdoor temperature is unknown but whose
  heat is known it carries on with the measured air temperature (B's slab equation needs only `Q`
  and `T_a`), otherwise it restarts.
- `plant.py`: the heat response to the permission `u` (A: `c0 + c1 (T_ref - T_out) + c2
  first_hour_of_block`; B: `clip(H_e (T_flow_target - T_s), 0, Q_max(T_out))` with the measured
  flow target, since the site has no heating curve), both times `u (1 - dhw)` and the valve cap
  `phi + (1 - phi) open` (`open`: share of comfort rooms below setpoint minus
  `model.response.plant_valve_cap_margin_k`, room temperature = `T_air` + the room's hour offset);
  the closed-loop u-driven simulation; the starts model
  `starts/day = alpha blocks + beta hours_on + gamma hours_on T_out`.
- `fit.py` and the `climate-fit` command ([CLI](cli.md#climate-fit)): per fold, room offsets,
  prior thermal fits, observers, responses (permitted non-DHW training slots, measured valve cap; B
  reads the observer's slab state) and the starts model use only the fold's training days (training
  seasons, fit months; `seasons.training_excluded` never trains), so nothing held out leaks. Weekly
  refits (`model.adaptive`): from the first test gate-month day, every `refit_every_days`, the
  thermal parameters are refitted on the fit-month days of the trailing `window_days` before the
  origin, starting from and shrunk toward the prior; the week's observer starts at the window, so
  everything at an origin uses data before it only. `select_shrinkage` picks `lambda` per model on
  S2 only (`hack/climate_shrinkage.py`). Validation on the test season's gate months:
  forecasts from 00/06/12/18 local, 24 h open loop, Q-driven and u-driven, RMSE and bias at 1, 6,
  12 and 24 h against persistence and diurnal persistence on common pairs, stopping at the first
  unknown input; u-driven daily thermal energy from each day's 00 origin (MAPE on days above
  1 kWh, season and `min_hours`-regime bias); the legacy replay over episodes of consecutive
  simulable days (parameters switch at the week boundaries): degree-hours below 22 °C, and the
  simulated heat's electricity through the COP model (lift from the flow target) and its PVPC cost
  against the realized; the starts model on measured hours. Acceptance on the fold that tests S3,
  each criterion with value and status (including the COP limits and the share of weekly refits
  with every parameter inside the envelope and none on a bound), selection (B only if it beats A by
  10% Q-driven RMSE at 12 h and 24 h; an accepted model only) and the verdict ACCEPTED or
  UNDETERMINED.
- `hack/climate_audit.py` (read-only) writes `report.json` and `report.md` into
  `<data_dir>/analysis/v1/climate-audit/<feature set>-<config>[-name]/`: thermostat resolution, gaps,
  counter quantization, the return temperature as a slab proxy, the heating curve, the legacy-mask
  hit rate, the channel-2 ratio, the outdoor-sensor sun test, filter survival per season and the
  `min_hours` regimes. The report holds private figures: it stays in the data dir.

```bash
uv run python hack/climate_audit.py --feature-set <climate feature set id> \
    --dataset <data_dir>/analysis/v1/<dataset_id>
```

**C2 PR 2: heat-pump electricity (`cop.py`).** Fits electricity, not COP, on hourly heat-mode hours
without hot water: `E_el = Q (a + b (T_flow - T_out) + c max(0, knee - T_out)) + d starts + e hours_on`
by WLS weighted by `Q`, with the measured flow temperature and `t_out_model` (hours without it are
dropped). `starts` is the compressor-starts counter delta; `hours_on` counts the 15-minute slots with a
compressor frequency above zero, so it is a quarter-hour upper bound of the true on-time. Hours below
`cop.min_q_kwh` or with an implied COP outside `cop.cop_range` are excluded, and the exclusion counts
are reported. Beside it sit two COP tables: by `T_out` (`cop.table_bin_k` bins) and by the lift
`T_flow - T_out` (`cop.lift_bin_k` bins), each bin needing 10 training hours (a sparser one reads the
nearest bin). Each fold of the config fits its training seasons and tests the next (fit months for
fitting, gate months for scoring), given the measured `Q`; a validation day needs 6 eligible hours.
Daily electricity MAPE and season error are checked against `cop.daily_mape` and `cop.season_error`.
The model is predeclared in `cop.model` (`lift_table` since the model iteration: the flow temperature
changed by hand, which a `T_out` table cannot follow) and the candidate with the lowest held-out MAPE
is reported beside it (`held_out_best`); passing the limits is reported separately. The report adds the
`d` and `e` terms with plausible physical ranges (a flag, not a gate), the implied COP curve, the
residuals by `T_out` bin and the DHW COP (information only).

For the simulator, `predict_electricity(model, q, t_flow, t_out, starts, hours_on)` evaluates the
selected model (the `T_out` table reads only `q` and `t_out`, the lift table `q` and `t_flow - t_out`) and `CopModel` is the versioned, hashable
parameter record (`model_id` is the SHA-256 of its canonical JSON, with the feature-set id and config
hash). The flow target is a per-season constant, not a heating curve, which is harmless because the
model reads the measured flow.

```bash
uv run python hack/climate_cop.py --feature-set <climate feature set id> \
    --dataset <data_dir>/analysis/v1/<dataset_id>
```

`hack/climate_cop.py` writes `cop.json` and `cop.md` into
`<data_dir>/analysis/v1/climate-cop/<feature set>-<config>[-name]/`; they hold private figures and
stay in the data dir.

`hack/system_sim.py` (read-only) replays a heat-pump regime x house battery grid per season and writes
`report.json`, `report.md` and one benchmark run per scenario into
`<data_dir>/analysis/v1/system-sim/<run-id>/`; method, results and the exact command are in the
[system replay](planning/system-replay.md).

**Price-blind comfort MPC (track B1, offline).** `mpc_config.py` loads
[`config/climate-mpc.yaml`](../config/climate-mpc.yaml) strictly (its own file: `config/climate.yaml`
stays untouched). `mpc_model.py` is the model interface the optimiser reads (`MpcModel`: linear state
equation, room outputs, heat table and valve demand along a reference, nonlinear simulation,
capacities) and its RC
implementation; `b2d.py` fits B0's room-air two-state model with a weekday/weekend diurnal gain (and
rebuilds C2's model B for comparison); `mpc.py` is the MILP (scipy `milp` / HiGHS) and the
solve, simulate and re-linearise loop; `mpc_replay.py` and `hack/climate_mpc_replay.py` run the
offline receding-horizon replay into `<data_dir>/analysis/v1/climate-mpc/<run-id>/`. Nothing in it
writes to Home Assistant or the plant. Formulation, causality and results:
[climate-mpc](components/climate-mpc.md).

**Live climate shadow (track B2).** `shadow.py` runs the same model and MILP on live data: raw
Prometheus samples go through the feature builder's slot rules and `build_climate_data`, so a live slot is
an offline feature-set slot; `ShadowPrior` is the offline part (B1's `fit_prior` and `fit_cop` on one
season, written by `hack/climate_shadow_prior.py`, content-addressed); `refit`, `decide` and `score`
reuse the replay's re-estimation, `decision_inputs`, `run_decision`, `open_loop_accuracy` and
`heat_driven`. `shadow_gates.py` evaluates the preregistered section-A criteria from the persisted records.
The resident service runs it (`service/climate_shadow.py`, off by default) and records into
`<data_dir>/service/v1/climate_*`; `climate-shadow-report` prints the gates. Ops, schema and
definitions: [service](service.md#climate-shadow-track-b2).

`hack/comfort_history.py` (read-only) writes `report.json` and `report.md` into
`<data_dir>/analysis/v1/comfort-history/<run-id>/`: per winter, the hall-kitchen temperature and
setpoint in the weekday, weekend and night windows, the share of time at or above 25 and 23.5 degC,
and each room's share below 21 degC. `hack/cheap_price_windows.py` (read-only) writes the same pair
into `<data_dir>/analysis/v1/cheap-price-windows/<run-id>/`: per month, the hours at or below price
thresholds and the days with a long cheap run, split weekday / weekend. Results and the exact
commands are in [heat-pump-controller](components/heat-pump-controller.md) and
[boost-mode](components/boost-mode.md).

### Fused outdoor temperature (`station.py`, `tout.py`)

The heat pump's outdoor sensor reads 1.5 to 2.5 K low while the compressor runs and warm in the sun,
so `t_out_debiased` keeps only slots with the compressor off and interpolates the rest, which leaves
a third of the recent seasons interpolated and a few percent unknown, in long cold runs where the
accuracy matters. An independent reference fixes that: the hourly SYNOP reports of a nearby AEMET
station, served keyless by Ogimet.

> Fuente: AEMET (via Ogimet). Ogimet's terms are not known, so the fetcher is gentle: monthly
> requests at least 5 s apart, retry with backoff on a 5xx or a timeout, and each month is fetched
> once and kept immutable.

- `station.py` decodes the air temperature from the SYNOP report by position (`iRiXhVV`, `Nddff`,
  `00fff` when the wind is 99 or more, then group 1 `1sTTT`, tenths of a degree, `s` = 1 negative;
  `NIL` and missing groups are skipped), fetches `https://www.ogimet.com/cgi-bin/getsynop?block=<WMO>&begin=YYYYMMDDHHmm&end=YYYYMMDDHHmm`
  and stores each month under `<data_dir>/climate/station/<WMO>/raw/` as `YYYY-MM.csv` plus
  `YYYY-MM.manifest.json` (source, station, request range, `fetched_at`, raw sha256, parser version).
  `load_station` verifies the raw months against their manifests and returns the series with a
  manifest hash that changes when a raw month or the parser changes.
  `reference_at_slots` interpolates it linearly to the 15-minute slot midpoints and bridges station
  gaps of up to `signals.t_out_fused.max_ref_gap_hours` (3 h: two missing hourly reports, or a
  3-hourly station); a longer gap leaves the reference unknown.
- `tout.py` fuses the reference `r(t)` with the house sensor through their difference. The offset
  `o(t) = house - r` is observed only on trusted dark slots (compressor off for at least
  `min_off_minutes` and a clear-sky proxy below `signals.t_out_fused.dark_solar_max`, so the sensor's
  sun bias stays out) and modelled as `o = mu + x`, `x` an AR(1) per slot with state variance `q` and
  observation variance `r`. `fit_offset_model` finds `mu, phi, q, r` by maximum likelihood on the
  Kalman filter's innovations (`scipy.optimize`) on the training seasons only
  (`signals.t_out_fused.fit_seasons`, S1 and S2; a held-out season is refused by the
  configuration), then `rts_smoother` runs the filter and a Rauch-Tung-Striebel pass over the whole
  period with the parameters fixed. `t_out_fused = r + mu + x_hat` with the smoother's standard
  deviation; where `r` is unknown it falls back to `t_out_debiased`. `kalman_filter` is the same
  model without the backward pass, the mode a live planner would use with a forecast as `r`; no live
  wiring exists.
- `data.py` builds the fusion when the private site file has an optional `reference_station` (a quoted
  5-digit WMO id) and the store holds the months the grid spans: `ClimateData.fusion` carries the
  reference, the fused series, its standard deviation and the station manifest hash, and
  `input_sha256` is one id over the feature set, dataset, config, site file, station manifest and
  `signals.t_out_source`, so a result keyed on it changes id when any input does. `t_out`,
  `t_out_debiased` and `t_out_fused` stay side by side; `t_out_model` is the one the models read,
  chosen by `signals.t_out_source` (`debiased` or `fused`, default `debiased`), and the
  `outdoor_unknown` filter follows it. With `signals.t_out_fill_unknown_from_fused` (owner decision
  of the model iteration) the debiased series takes the fused value exactly where it is unknown and
  the station reference is known; `ClimateData.t_out_filled` records those slots.
- **Switch rule**, decided before any thermal result uses fused data: fused replaces debiased when its
  RMSE is lower than `t_out_debiased`'s interpolation on held-out trusted readings in S3. The test
  (`tout.leave_block_out`) hides blocks of 1 to 6 h of trusted readings and predicts them from
  (a) the interpolation of the remaining trusted readings and (b) the fused series with the hidden
  readings also removed from the smoother's input; `hack/climate_station.py evaluate` repeats it on
  night blocks only (the sensor's sun bias is not in the truth) and on 8 to 24 h blocks against the
  uncapped interpolation (the gaps where `t_out_debiased` is unknown). The residual compressor-start step
  (`tout.compressor_start_step`: the change of a series across a compressor start, net of the
  station's own change) is reported next to it.
- `hack/climate_station.py` has `fetch` (the monthly downloads), `compare` (each candidate station
  against the house sensor on trusted slots: coverage, bias by local hour and season, RMSE,
  correlation of 15-minute and hourly changes, lag) and `evaluate` (fitted parameters, the
  leave-block-out test, the compressor-start step and coverage). Reports go to
  `<data_dir>/analysis/v1/climate-station/` and hold private figures.

```bash
uv run python hack/climate_station.py fetch --stations <WMO> [<WMO> ...]
uv run python hack/climate_station.py compare --stations <WMO> [<WMO> ...] \
    --feature-set <climate feature set id> --dataset <data_dir>/analysis/v1/<dataset_id>
uv run python hack/climate_station.py evaluate --feature-set <climate feature set id> \
    --dataset <data_dir>/analysis/v1/<dataset_id>
```

## Shadow service (`service/`)

**Status: Phase C (PROD-C1 to C3) built — shadow by default, no actuation path in `mode: shadow`.**
`mode: dry_run` / `mode: live` add `service/actuator.py` ([ADR-0007](adr/0007-live-battery-control.md),
accepted 2026-11-03; see [service.md](service.md#live-control-dry_run-and-live)). `battery-optimizer serve`
subscribes to Home Assistant (read-only allowlist, reconnect and full resync; passive PVPC-snapshot and
weather-forecast archives, see [service.md](service.md#passive-archives)), reads load history from
Prometheus, solves the offline MPC kernel every 15 minutes in a bounded worker, records time-bounded
shadow intents and exposes `/health/live`, `/health/ready`, `/status`, `/plan` and `/metrics`.
`battery-optimizer shadow-report` compares the recorded plans with the native battery behaviour.
The battery was commissioned on 2026-11-03 and its SOC and AC energy counters are mapped, but the
committed configuration still runs a virtual battery (`soc_source: simulated`) until the planner's SOC
freshness check suits Omnibattery's report-on-change entities. Architecture, configuration,
metrics, failure modes and the comparison method: [service.md](service.md).
