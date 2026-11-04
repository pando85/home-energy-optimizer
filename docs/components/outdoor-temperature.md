# Outdoor temperature (debias, reference station, fused fill)

## Purpose and owner

The outdoor temperature the climate models read. The heat pump's own sensor reads 1.5 to 2.5 K low
while the compressor runs and warm in the sun, so the series is rebuilt from the sensor's trusted slots
(`t_out_debiased`), optionally fused with a nearby AEMET SYNOP reference through a Kalman-smoothed offset
(`t_out_fused`). Owner: this repository (`climate/data.py`, `climate/tout.py`, `climate/station.py`).

## Interfaces

- **In:** the Aquarea outdoor sensor, compressor frequency, a clear-sky proxy from the private site file,
  and the monthly reference-station files in the data directory (manifest-hashed).
- **Out:** `t_out`, `t_out_debiased`, `t_out_fused` and the filled-slot mask in `ClimateData`;
  `t_out_model` is the one the models read.
- **Consumers:** [cop-model](cop-model.md), [thermal-model](thermal-model.md). Nothing battery-side.

## Calibrated on

- Offset model fitted on **S1 and S2 only** (`signals.t_out_fused.fit_seasons`, a held-out season is
  refused); evaluated on S3 (October to April). Config `config/climate.yaml` v2, hash `22888468...`.
- Feature set `da6d678016ed`, station manifest hash `0183591c...` (parser version in the manifest).
- Plant regime: legacy heat-pump control, compressor on for the 10 to 15 permitted hours.

## Assumptions

- A slot is a trusted reading once the compressor has been off 30 minutes; a longer running period is
  interpolated only up to 6 h, else unknown (`signals.t_out_debias`).
- The sensor-minus-station offset is observed only in the dark (clear-sky proxy below 0.05) and is a
  constant plus a slowly varying AR(1) term.
- Station gaps up to 3 h can be bridged; the station represents the site.
- Result on S3: on hidden trusted blocks the fused series has a **higher** RMSE than the interpolated
  debiased series, so `t_out_source: debiased` stays and fused only fills unknown slots
  (`t_out_fill_unknown_from_fused: true`). Evidence: the station `evaluate` report in the data directory,
  [architecture](../architecture.md#fused-outdoor-temperature-stationpy-toutpy).

## Invalidated when

- The heat pump's outdoor sensor is moved, shielded or replaced (the bias and the offset change).
- The compressor duty rises. Longer, gentler runs (efficiency-first or comfort-schedule control) leave
  fewer trusted off-slots: in S2/S3 about 60% of slots were trusted and about a third interpolated, so
  the unknown share grows and the fill matters more.
- The reference station changes, relocates or stops reporting; the parser or the raw months change
  (the manifest hash changes, so `input_sha256` and every dependent `model_id` change).
- The fit seasons change, or a new season is added to the offset fit.

## Recalibrate by

1. `uv run python hack/climate_station.py fetch --stations <WMO>` for the new months (5 s between
   requests, each month kept immutable).
2. `uv run python hack/climate_station.py evaluate --feature-set <id> --dataset <dir>`
   ([architecture](../architecture.md#fused-outdoor-temperature-stationpy-toutpy)).
3. Acceptance: the predeclared switch rule. Fused replaces debiased only if its RMSE is lower on held-out
   trusted readings of a season the offset fit has not seen; otherwise keep debiased and report the
   unknown share and the filled share of the new regime.

## KPIs

Observability only. The series the models read (`t_out_model`) is built offline, so the live part is what the
rule depends on: how often the unit sensor is trustworthy and the availability of the forecast archive the MPC
horizon reads. The shares below are approximations from the compressor frequency, not the model's 15 minute
slots. Dashboard: `Climate: outdoor temperature` (uid `climate-outdoor-temperature`).

| KPI | Where |
|---|---|
| Trusted share (compressor idle for 30 min; about 60% of slots in S2/S3) | `avg_over_time(max(min_over_time(hass_sensor_unit_hz{entity="sensor.aquarea_compressor_frequency"}[30m]) == bool 0)[1d:5m])`; offline the exact shares from `uv run python hack/climate_station.py evaluate --feature-set <id> --dataset <dir>` |
| Unknown share (a run longer than 6 h, filled from the station) | `avg_over_time(max(min_over_time(hass_sensor_unit_hz{entity="sensor.aquarea_compressor_frequency"}[6h]) > bool 0)[1d:5m])` |
| Compressor duty (what shrinks the trusted share) | `avg_over_time((max(hass_sensor_unit_hz{entity="sensor.aquarea_compressor_frequency"}) > bool 0)[1d:5m])` |
| Fused against debiased RMSE on held-out trusted blocks (the switch rule) | offline `hack/climate_station.py evaluate` (leave-block-out test, coverage); per candidate station bias and RMSE: `hack/climate_station.py compare` |
| Forecast archive availability | `sum(increase(battery_optimizer_weather_forecast_fetches_total{result="ok"}[1d]))` against `result="failed"`; `time() - max(battery_optimizer_archive_last_success_timestamp_seconds{archive="weather_forecast"})` (hourly at HH:05 UTC) |
| Unit sensor | `hass_sensor_unit_celsius{entity="sensor.aquarea_outside_ambient_temperature"}` against the compressor frequency (the self-heating dip) |

Not yet measured: the outdoor temperature **forecast error** (MAE and bias per lead against the measured
series): the forecast is archived and the plan records the horizon it used, but nothing joins it with the
debiased series as a metric (the private `climate_scores` carry the `heat_plan_weather` diagnostic only); the
debias offset against the reference station; the trusted, interpolated and unknown shares of the model's own
slots as a metric.

## Status

**valid** (2026-10-30) for the legacy compressor duty. Re-run `evaluate` once the comfort schedule has
produced 28 days of data and record the trusted, interpolated and unknown shares.
