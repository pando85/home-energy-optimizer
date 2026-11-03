# Battery load forecast (LF-0 trailing slot mean, causal)

## Purpose and owner

Predicts whole-house load per 15-minute slot for the battery optimizer. LF-0 (`trailing_mean`) is the
same-slot mean of the previous 28 days, cut at local midnight. The service runs it live; the benchmark
runs it as the causal mode of `day_ahead` and `rolling_horizon`. Owner: this repository
(`policies.py` `trailing_slot_mean_for_day`, `rolling_horizon.py`, `load_forecast.py`,
`service/history.py` `forecast_load`). Roadmap: [Load Forecaster](../planning/components/load-forecaster.md).

## Interfaces

- **In:** the whole-house meter history (channel 1, [data-pipeline](data-pipeline.md)), 28 days.
  Config `planning.load_forecaster: trailing_mean`, `planning.forecast_window_days: 28`.
- **Out:** kWh per slot over the planning horizon, with `forecast_meta` provenance.
- **Consumers:** [battery-optimizer](battery-optimizer.md) (charge is capped at the import limit minus
  forecast load). Optional input: [hp-consumption-forecast](hp-consumption-forecast.md).

## Calibrated on

- Evaluated on the legacy heat-pump regime: 2025 and 2026-01 to 2026-09, dataset family `2e01620ba727`
  (frozen run `fe3cbfec46b6`) and the later cluster-store datasets. No fit: the window length was kept
  after 15 candidates failed gate G2.
- Results: per-slot WAPE about 47% with negligible bias; a perfect load forecast is worth about a fifth of
  the battery's net saving, mostly in the 18:00 to 21:00 expensive tercile ([plan](../planning/load-forecaster-improvement-plan.md)).
- Plant regime: no battery, legacy cheapest-hours heat pump (mostly night valle hours).

## Assumptions

- **Weekday-blind** and season-blind: it averages weekdays with weekends. `daytype` exists
  (`load_forecast.py`) but failed G2 on legacy data.
- The last 28 days resemble the next day. Heat-pump load is the largest slot-irregular component under the
  legacy rule, so it is what makes the forecast poor ([decompose-load](../cli.md#decompose-load)).
- Load is exogenous: the battery never changes it. Decoupling makes this more true.

## Invalidated when

- **The comfort schedule goes live or the legacy rule is retired.** Heat-pump load moves from cheap night
  hours into 08:00 to 12:00 (weekday optimum start) and 09:00 weekend preheat. For up to 28 days the window
  mixes both shapes and the forecast under-reads exactly where discharge pays most.
- Weekday and weekend schedules differ (12:00 against 09:00): the weekday-blind mean is structurally wrong.
- Boost runs: it smears rare large loads into the same clock slots of other days.
- C7-F alternating-arm weeks mix arms in one window; the battery is commissioned (gross-load rebuild
  needed); a large new load appears.

## Recalibrate by

After 28 days of new-regime data, plus the settling period (exclude the first 28 days after the switch and
alternating-arm weeks from every evaluation window):

1. `uv run battery-optimizer build` and `build-features` for the window.
2. `uv run battery-optimizer forecast-eval --dataset <dir> --name <run> --candidate daytype --candidate
   trailing_median:window_days=28 --p0-summary <new P0 summary>` ([CLI](../cli.md#forecast-eval)); the P0 gap
   is re-run first.
3. Acceptance: gate G2, pre-registered in the run: the 95% weekly-block bootstrap interval of the paired
   daily saving against `trailing_mean` lies above zero and the point estimate is at least 10% of the P0
   gap. Otherwise LF-0 stays. Resume triggers 2 and 3 of the plan both fire.

## Status

**stale** (2026-11-03): the battery is commissioned, so the forecast now learns from reconstructed gross load
(grid − charge + discharge after `battery.commissioned_at`, the grid counter alone before). The battery is live
since 2026-11-03 23:45 (regime log), so the reconstruction now carries real charge and discharge; the load
shape is unchanged. Recalibrate once the [data-pipeline](data-pipeline.md) reconstruction passes its
acceptance check. It also becomes stale when the comfort schedule goes live (recalibrate after 28 days of
new-regime data). It matters now that the battery is live.
