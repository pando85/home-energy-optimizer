# COP model (heat-pump electricity from delivered heat)

## Purpose and owner

Predicts the heat pump's electricity for a given delivered heat, flow temperature and outdoor
temperature: `E_el = Q (a + b lift + c max(0, 5 - T_out)) + d starts + e hours_on`, with the COP table
binned by **lift** (`T_flow - T_out`, 2 K bins) as the declared model (`cop.model: lift_table`).
Owner: this repository (`climate/cop.py`, `hack/climate_cop.py`).

## Interfaces

- **In:** hourly heat-mode rows from the climate feature set without hot water: measured heat (HeishaMon),
  measured flow temperature, `t_out_model` ([outdoor-temperature](outdoor-temperature.md)), compressor
  starts and running slots.
- **Out:** `CopModel` (versioned, hashable; `predict_electricity`), `cop.json` / `cop.md` in the data directory.
- **Consumers:** the legacy-replay electricity and cost criteria of [thermal-model](thermal-model.md);
  the heat-pump regime transfer of the [whole-system replay](system-replay-benchmark.md) uses measured
  COP bands, not this model; the offline [climate-mpc](climate-mpc.md) refits its lift table. Nothing live.

## Calibrated on

- Trained on **S2** (2024-10-01 to 2025-04-30, fit months), validated on **S3** gate months (November to
  March). Config `config/climate.yaml` v2 (hash `22888468...`), feature set `da6d678016ed`, dataset
  `cdd448abb006`, thermal-model run `c1794f600291` (the run that carries the COP validation).
- Plant regime: flow target 28 then 26 degC (S2), 26 degC (S3), legacy 10 to 15 h control. **S1 (35 degC)
  is not in training**: it is a different regime. The lift table therefore covers lifts reached at about
  26 to 28 degC water only.

## Assumptions

- HeishaMon heat and electricity counters are the reference; the branch meter reads about 1.28 times
  higher and carries appliances ([climate plan](../planning/climate-optimizer-implementation.md)).
- Hot water and cooling slots are excluded (the DHW COP is reported for information only); defrost is
  not observable (the defrost temperature sensor is constant 0).
- `starts` and `hours_on` are quarter-hour upper bounds of true runtime; the flow target is a per-season
  constant, so the model must read the **measured** flow temperature.
- A bin with fewer than 10 training hours reads the nearest bin.

## Invalidated when

- The water target or heating curve changes outside about 26 to 28 degC (C7-F arms, a curve activation):
  lifts outside the table fall on nearest-bin extrapolation.
- The comfort schedule changes compressor behaviour (more starts, longer low-frequency runs): the `d`
  and `e` terms and the low-lift bins were fitted on 10 to 15 h legacy days.
- HeishaMon firmware, sensors or counters change; the outdoor series changes (`t_out_source`).
- Hot water begins to be boosted to 55 to 60 degC and is mixed into heat-mode rows.

## Recalibrate by

`uv run python hack/climate_cop.py --feature-set <id> --dataset <dir>` (or `battery-optimizer
climate-fit` for the validated record, [CLI](../cli.md#climate-fit)), training on the new regime's
season and testing on a later block. Acceptance, unchanged from the config: daily MAPE at most 8% and
season error at most 3% on the held-out block. Also report (flags, not gates) the share of lifts outside
the table (warn above 10%) and whether `d` and `e` sit in their plausible physical range.

## KPIs

Observability only. The model is not run live, so the live part is the **measured side**: the reference it is
validated against and the drivers of its terms. Metrics are Home Assistant's (`hass_*`, HeishaMon entities).
Dashboard: `Climate: COP model` (uid `climate-cop`).

| KPI | Where |
|---|---|
| Measured COP, the reference (heat, hot water) | `sum(increase(hass_sensor_energy_kwh{entity="sensor.aquarea_heat_energy_produced"}[1d])) / sum(increase(hass_sensor_energy_kwh{entity="sensor.aquarea_heat_energy_consumed"}[1d]))`, and `dhw` for hot water (the counters of the model's basis); the unit's own `hass_sensor_unit_x{entity="sensor.aquarea_heat_cop"}` while running |
| Model error: daily MAPE (at most 8%) and season error (at most 3%) on a held-out block | offline `uv run python hack/climate_cop.py --feature-set <id> --dataset <dir>`: `cop.json` / `cop.md`, `validation.<model>.daily_mape` and `season_error`; also `battery-optimizer climate-fit` |
| Lift, the table's bin variable (K) | `max(hass_sensor_unit_celsius{entity="sensor.aquarea_outlet_temperature"}) - max(hass_sensor_unit_celsius{entity="sensor.aquarea_outside_ambient_temperature"})` while `hass_sensor_power_w{entity="sensor.aquarea_heat_power_consumed"} > 0` |
| Starts and hours on (the `d` and `e` terms) | `sum(increase(hass_sensor_state{entity="sensor.aquarea_compressor_start_stop_counter"}[1d]))`; `sum(increase(hass_sensor_unit_hours{entity="sensor.aquarea_compressor_operating_hours"}[1d]))`; offline the fitted `d` and `e` against their plausible physical range in `cop.json` |
| Heat and electricity per day | `sum(increase(...heat_energy_produced[1d]))` and `...consumed` (the model's inputs and output) |

Not yet measured: the predicted COP or electricity live, hence the model error as a metric; the share of lifts
that fall outside the table (nearest-bin reads, the page's warn above 10%) and the residual by bin. Both need
the model run on live data, which nothing does; offline only.

## Status

**provisional** (2026-10-30): daily MAPE about 7.7% passes, season error about 5.0% fails the 3% limit.
Not accepted, not used live. It stays a diagnostic baseline until the water-temperature trial supplies
a wider lift range.
