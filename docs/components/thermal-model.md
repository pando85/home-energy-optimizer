# Thermal model (parked, C2 UNDETERMINED)

## Purpose and owner

Predicts room temperature from delivered heat and outdoor temperature (model A, first order; model B,
air plus slab) and the heat response to a heating permission, to plan heating against price. Owner: this
repository (`climate/thermal.py`, `plant.py`, `fit.py`, the `climate-fit` command). Its price-driven use
was dropped by the decoupling decision ([ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md)); what remains is diagnostic and, possibly, warm-up-rate
learning for optimum start.

## Interfaces

- **In:** climate feature set, [outdoor-temperature](outdoor-temperature.md), [cop-model](cop-model.md)
  for the electricity criteria, the replayed legacy permission `u`, the private site file.
- **Out:** `<data_dir>/analysis/v1/climate-models/<model_id>/` (`model.json`, `validation.json`).
- **Consumers:** not the whole-system replay; [climate-mpc](climate-mpc.md) refits B0's candidate offline (B1) and scores it read-only on live data (B2).

## Calibrated on

- Fold: fit **S2** (2024-10-01 to 2025-04-30), test **S3** (2025-10-01 to 2026-04-30), causal weekly refits
  shrunk toward the S2 fit. S1 is excluded as a different plant regime (35 degC water, valves shut about
  half the heating time, apparent UA about three times S2's and S3's).
- Runs: `c1794f600291` (config v2, hash `22888468...`, code `9055b4d`, dataset `cdd448abb006`, feature
  set `da6d678016ed`, UNDETERMINED) and the earlier `c521d2733188` (config v1, `e01282e5...`, S1 and S2, also UNDETERMINED).
- Plant regime: water 28 then 26 degC, setpoints 24 to 25 (S2) and 23.0 (S3), legacy 10 to 15 h.
- B0 (2026-10-31, run `b0-20261031`): C2 v2 reproduced exactly; challenger fits are **exploratory**.

## Assumptions

- Mean comfort-room temperature represents comfort; always-open share `phi` is 0.3 until floor areas
  exist; valve closure is binary.
- Return water as a slab proxy: **refuted by B0.** Its flow-dependent offset pulled B's slab to the bound;
  on room air only, the slab is identified at about 10.6 to 12.5 kWh/K in both seasons.
- C2 miss, root causes: no weekday/weekend daily gain (6 to 12 h), season shift (24 h, energy), extra starts (COP).
- Candidate (exploratory): 2-state, room air only, weekday/weekend daily gain, weekly re-estimation. Room-mean
  RMSE 0.249 K at 6 h and 0.325 K at 12 h (S2 to S3) against 0.334 and 0.426 for model A and 0.447 and 0.536
  for persistence; the reverse fold improves too. **Useful horizon 6 to 12 h** (1 h ties persistence).
- Evidence: [C2 outcome](../planning/climate-optimizer-implementation.md#c2-model-iteration-the-one-predeclared-iteration-config-version-2)
  and [C2 recovery](../planning/climate-c2-recovery.md#7-status-2026-10-31-b0-results-r0-to-r2).

## Invalidated when

Already unaccepted; triggers against any future fit:
- Water target, heating curve, thermostat setpoints or valve behaviour change (each moved the apparent UA).
- The permission lever disappears: under the comfort schedule there is no binary `u`, so the response
  model of `u` has nothing to learn from.
- The outdoor series or the heat measurement changes.
- Season-level UA shift beyond weekly re-estimation (S2 to S3: about 22%); thermostat reporting changes.

## Recalibrate by

Not by re-running `climate-fit`: the one predeclared iteration is spent and S3 is no longer an untouched
holdout. Test the candidate above prospectively on live heating data against the criteria committed in the
[2026-27 preregistration](../planning/climate-preregistration-2026-27.md) (section A), as the B2 shadow does (`climate-shadow-report`). G-R1 to G-R3 of the
[C2 recovery](../planning/climate-c2-recovery.md#7-status-2026-10-31-b0-results-r0-to-r2) plan are not reached.

## KPIs

Observability only. The parked model is scored read-only by its B0 candidate in the live shadow (B2), on the
comfort-room mean (bedroom, hall_kitchen, office_t) with the measured heat and outdoor temperature, so it tests
the model and not the weather forecast. Metrics are prefixed `battery_optimizer_climate_shadow_`; `window` is
`legacy` or `new`, `horizon` is `1h`, `6h`, `12h` or `24h`. Dashboard: `Climate: thermal model`
(uid `climate-thermal-model`).

| KPI | Where |
|---|---|
| Room-mean RMSE per lead, K (limits 0.30 at 6 h, 0.40 at 12 h) | `rmse_kelvin{window,horizon}`; offline `battery-optimizer climate-shadow-report`, and for the replay folds `validation.json` of `battery-optimizer climate-fit` (model, persistence and diurnal baselines) |
| Skill against persistence (at least 25% at 6 h and 12 h) | `skill_ratio{window,horizon}` = 1 - RMSE(model) / RMSE(persistence) |
| Bias per lead, K (6 h at most 0.15) | `abs_bias_kelvin{window,horizon}` (absolute mean error) |
| Daily heat-delivery error (median APE at most 15%) | `daily_heat_mdape_ratio{window}` |
| Data behind the verdict | `origins{window,horizon}`, `valid_weeks{window}` (6 needed, else UNDETERMINED), `gate{window,criterion,status}` for the six criteria and the verdict |
| Refit freshness | `last_refit_timestamp_seconds`, `sum by (result) (increase(refits_total[7d]))` (`refitted`, `prior_kept`) |
| The quantity predicted | room means of `hass_climate_current_temperature_celsius{entity=~"climate.thermostat_(bedroom\|hall_kitchen\|office_t)"}` |

Not yet measured: the error per room (only the room mean is scored; the four thermostat rooms are a private
diagnostic in `climate_scores`), the error under the forecast outdoor temperature the plan used
(`heat_plan_weather`, a private record) and the model's heat-response to a permission `u`, which has no
counterpart under the comfort schedule.

## Status

**parked** (2026-10-30, unchanged by B0 on 2026-10-31). Nothing acts on it; the battery side does not depend on it.
B0's candidate is under its predeclared evaluation in the B2 shadow (implemented, not deployed); un-park only on that verdict.
