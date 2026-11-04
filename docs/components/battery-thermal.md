# Battery thermal model and Omnibattery derate

## Purpose and owner

Predicts the Marstek Venus E V3's internal temperature from its AC power and the charge power Omnibattery's Temperature
Charge Limit then allows, so that the planner can stop assuming 2.5 kW is always available
([design](../planning/thermal-derate-aware-planning.md)). Owner: this repository. The model, its fit and the derate
curve are `service/thermal.py`, shared by the offline study `hack/battery_thermal_derate.py` (its predecessor is
`hack/battery_thermal_study.py`) and by the service, which since PR 2 runs them **in shadow** (`planning.thermal.mode:
shadow`, off by default; [service](../service.md#thermal-shadow-derate-prediction)): it keeps the state, refits daily
with acceptance checks, and records a prediction next to every plan. **The planner does not use it yet.** The
Omnibattery switch and numbers are the owner's: this repository reads them and never writes them
([ADR-0007](../adr/0007-live-battery-control.md)); the `ha_guard` allowlist is unchanged.

## Interfaces

- **In:** `sensor.venus_internal_temperature` (register 35000, the only temperature the V3 driver exposes), the signed AC power
  (`sensor.venus_ac_power`), and Omnibattery's `switch.omnibattery_temp_charge_limit`, `number.omnibattery_temp_charge_limit_c`,
  `_band_c`, `_floor_pct`, `switch.omnibattery_temp_charge_limit_discharge` and the two cap numbers; all read only.
- **Out:** `(tau, T_amb, k_c, k_d)` (`/status` `thermal.model`, `battery_optimizer_thermal_parameter`), a per-slot temperature
  and achievable-power prediction of each plan (the `thermal` key of the plan file and of `/plan`, scored at slot close;
  shadow, observability only), the derate state and factor now. Not yet: the conformance of the curve.
- **Consumers:** [battery-optimizer](battery-optimizer.md) (derate-aware planning, planned), [battery-model](battery-model.md)
  (the charge power limit assumption), [system-replay-benchmark](system-replay-benchmark.md) (a thermal plant, planned).

## Calibrated on

Recorder events 2026-11-03 19:14:30 UTC (first AC energy) to 2026-11-04 14:24 UTC: 19.2 hours, five episodes, **two charges
above 1 kW** (1.4 kW mean, and 2.5 kW from idle to 52.4 degC), discharges of at most 0.82 kW. Plant regime: live control since
2026-11-03 23:45, Omnibattery caps 2500/2500 W, and the Temperature Charge Limit **off until 2026-11-04 14:10:56 UTC, on after**
([regime log](regime-log.md)). Commit: the one that adds the script. Fit (multi-step least squares, horizons 10 to 90 min,
2729 rows):

| parameter | fit | bootstrap 5 to 95% | leave-one-episode-out |
|---|---|---|---|
| `tau` | 44 min | 40 to 52 | 44 to 52 |
| `T_amb` | 32.6 degC | 32.2 to 33.0 | 32.3 to 32.9 |
| `k_c` (charge) | 11.0 degC/kW | 8.7 to 11.7 | 9.7 to 11.6 |
| `k_d` (discharge) | 6.9 degC/kW | 3.6 to 9.5 | **3.1 to 8.2** |

**Correction of the first record (PR 2).** The record of PR #91 had the two gains swapped (charge 6.9, discharge 11.0):
its regressors took the negative part of a power that was already positive while charging. The coefficients and every
held-out score are unchanged; only the labels, and the figures derived from the charge gain, are. The service prior
(`planning.thermal.prior`) is the corrected one. Details and a synthetic reproduction:
[study, section 2](../planning/thermal-derate-aware-planning.md#2-evidence-2026-11-04-hackbattery_thermal_derate-py).

Validation: leave-one-episode-out 60-minute rmse 1.43 against 4.35 degC for persistence (pooled over horizons 1.11 against
3.21; walk-forward 1.02 against 3.30); above 1 kW 2.53 against 8.45, bias -1.06 degC. With the 2.5 kW episode held out the
session peak is predicted at 46.6 against 52.4 degC. Derate curve: written set power within 15 W of
`min(caps) x factor(T)` at every one of 25 points (T 48.3 to 50.2 degC, caps 1000 to 1255 W), reaction 20.7 s. Equilibrium
(thermal steady state meets the curve), recomputed with the corrected charge gain: about 1.4 kW at 48 degC (1.3 to 1.6 kW
for `k_c` 11.7 to 8.7; PR #91's 1.8 kW at 45 degC used the discharge gain).

## Assumptions

- One first-order node with a constant ambient; the sensor is Venus-internal, not a cell temperature
  ([study](../planning/thermal-derate-aware-planning.md#2-evidence-2026-11-04-hackbattery_thermal_derate-py)).
- `k_c` (the charge gain) is linear in power up to 2.5 kW (two sessions above 1 kW; a quadratic term fits no better and
  is unphysical).
- `k_d` and a separate discharge gain are **not resolved** (a single gain validates as well); no discharge above 0.82 kW observed.
- The idle level (31.7 to 33.0 degC over 19 hours) is a constant; a daily harmonic fails validation, a per-episode level would
  halve the error (oracle), so level drift is the largest unmodelled term.
- Omnibattery's curve is the documented one (1 up to the limit, linear to the floor over the band, continuous, no hysteresis,
  never below the driver minimum, no temperature means no derate), checked only between 48.3 and 50.2 degC.

## Invalidated when

- The battery moves, the enclosure or ventilation changes, or the season shifts the idle level by more than 2 degC.
- The Omnibattery switch or any of its numbers changes (the owner may do it at will), or Omnibattery or the Venus firmware is
  upgraded (the curve, the temperature source or the driver minimum may change).
- The system charge caps change (the curve scales with them), or a second battery is added.
- The sensor jumps (a firmware change that alters register 35000) or the discharge switch is enabled (the discharge gain is a prior).

## Recalibrate by

`uv run python hack/battery_thermal_derate.py --kube-context <context> --cache-dir <dir outside the repo>` weekly until accepted.
Needed for acceptance: at least 10 episodes with at least 4 charges above 1.8 kW from an idle start and 8 full cool-downs (about
14 days of normal operation; the soft cap suppresses high-power sessions, so keep it off until then or run one owner-approved
calibration charge). Acceptance: leave-one-episode-out 60-minute rmse at most 1.5 degC and MSE skill at least 0.6 over
persistence, `|bias|` at most 0.5 degC above 1 kW, `k_c` bootstrap width at most 2 degC/kW, `tau` within plus or minus 15%,
derate conformance (set power within 30 W of the predicted cap) at least 95% over at least 60 derate-active minutes spanning
40 to 50 degC. KPIs once the service computes them: thermal-model skill, plan temperature error, derate conformance,
achievable-power gap, hours per week above 35, 40 and 45 degC.

## Status

**provisional** (2026-11-04, labels corrected in PR 2): fitted on one day, two charges above 1 kW; `tau`, `T_amb` and the order
of `k_c` are usable, `k_c` above 1.9 kW and `k_d` are not accepted (the acceptance check above fails), and the derate curve is
confirmed only over 13.5 minutes at 48.3 to 50.2 degC. Not used by the planner.

**Parked** (2026-11-04, owner): fans are to be added to the battery, so the thermal analysis is not prioritised and may be
deprecated; the shadow keeps running as the before/after measurement. See the
[parked note](../planning/thermal-derate-aware-planning.md#7-rollout-and-pr-breakdown).

**Implementation** (PR 2, shadow; `planning.thermal.mode`, default `off`): the service reads the Omnibattery switch and
numbers, the temperature and the AC power, keeps a one-minute buffer, refits daily within bounds with the acceptance checks of
[service.md](../service.md#thermal-shadow-derate-prediction) (the parameters only change on an accepted refit; the status is
`prior`, `fitted` or `degraded`), predicts every plan and scores the prediction at slot close. Until it has accepted a fit the
parameters above are the prior, and `thermal_model_status{status="prior"}` says so. The page stays `provisional` until the
acceptance criteria of "Recalibrate by" are met on the service's own data (the KPIs it exports are the end-temperature error
by lead and the achievable-charge error in derate slots; the model-skill and curve-conformance KPIs are not computed yet).
Switching the shadow on is not a plant or control change, so the regime log has no entry. The soft cap's thermal fit
([study](../planning/thermal-charge-soft-cap.md#2-thermal-evidence)) is superseded by this page; its 1.5 kW knee is
unchanged.
