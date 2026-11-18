# Whole-system replay and benchmark (the acceptance tests)

## Purpose and owner

Two tests that decide whether the other components are good enough. The **benchmark** (`benchmark`,
five-rung ladder) answers the battery question: saving against `no_battery` on the same measured load.
The **replay** (`hack/system_sim.py`) answers the system question: heat-pump regime (legacy cheapest hours
A against efficiency-first B) crossed with the simulated battery. Owner: this repository.

## Interfaces

- **In:** a built dataset and climate feature set ([data-pipeline](data-pipeline.md)), `config/tariff.yaml`,
  `config/climate.yaml`, a fresh run id.
- **Out:** private `report.json` / `report.md` and one benchmark run per scenario under
  `<store>/analysis/v1/system-sim/<run-id>/`; benchmark runs under the dataset. Relative figures only
  enter git ([system replay](../planning/system-replay.md), [benchmark](../benchmark.md)).
- **Consumers:** the decisions in ADR-0003, ADR-0004 and [ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md); [battery-optimizer](battery-optimizer.md).

## Calibrated on

- Replay reference run `v1-20261030-repro` at code `f5be13d` (clean): climate config v2
  (`228884683bc5...`), feature set `da6d678016ed`, dataset `cdd448abb006`, seasons S2 and S3, RTE 0.80,
  free terminal, pre-tax. Reproduces the first run `v1-20261030` field for field.
- Benchmark pinned run `fe3cbfec46b6` on dataset `2e01620ba727` (UTC 2025 evaluation, oracle certified).
- Plant regime: legacy rule as observed (S2 11 h; S3 10 h then 15 h); B is an empirical transfer of the
  S3 days that ran 15 h.

## Assumptions

- B's daily kWh follows the 15 h days' kWh-versus-HDD line (R2 about 0.75); its hourly shape is the
  kWh-weighted mean of those days, **inherited from the price-chosen hours**, so B's cost is somewhat
  flattered and the battery share of B1 probably understated.
- B's comfort is an association from months-apart 10 h and 15 h days; the C7-F field trial confirms or
  refutes it ([C7-F](../planning/climate-flow-calibration-optimization.md)).
- Heat-pump kWh scaled to the meter with k = 1.27 (S2) and 1.28 (S3). No thermal model, no hot-water change.
- Findings that hold under these assumptions: battery share of cost is 16.0% (A) against 16.1% (B) in S3;
  floor hits are 0 to 3.3% of expensive cold slots; about 1.2 equivalent full cycles per winter day, wear
  not modelled.

## Invalidated when

- The comfort schedule goes live: the real load shape is neither A nor B. Findings 1 and 2 (battery value
  independent of regime) are then unconfirmed for the actual schedule until measured.
- Battery assumptions change ([battery-model](battery-model.md)) or the tariff file changes (dataset
  re-mint); taxes confirmed.
- New data replaces the 15 h transfer source (water temperature or setpoints differ), or the C7-F trial
  changes the plant state.
- The runner or the benchmark code changes behaviour (the code version is part of every run id).

## Recalibrate by

- Replay: `uvx uv@0.13.5 run python hack/system_sim.py --feature-set <id> --dataset <dir> --config
  config/climate.yaml --tariff config/tariff.yaml --run-id <NEW id>` (never reuse an id; about ten
  minutes; full command and the identity table in [system replay §6](../planning/system-replay.md#6-reproduce)).
- After about four weeks of real new-regime data the ordinary `benchmark` on measured load **is** the B1
  measurement; no further transfer is needed.
- Acceptance: a faithful reproduction matches every identity and numeric field of `report.json`; for a
  new regime, `no_battery` is that regime's measured load, and the battery's relative saving is compared
  with findings 1 and 2.

## Status

**valid as recorded** (2026-10-30) for legacy and the transferred efficiency-first regime; **stale** (2026-11-18) as a
description of the house: the optimizer's comfort plan went live that day, so the real load shape is neither A nor B
([regime log](regime-log.md)). Replays made after it are not comparable with earlier ones.
