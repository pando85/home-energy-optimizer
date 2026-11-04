# Battery thermal model and Omnibattery derate

## Purpose and owner

Predicts the Marstek Venus E V3's internal temperature from its AC power and the charge power Omnibattery's Temperature
Charge Limit then allows, so that the planner can stop assuming 2.5 kW is always available
([design](../planning/thermal-derate-aware-planning.md)). Owner: this repository; today **offline only** (the study
`hack/battery_thermal_derate.py`, its predecessor `hack/battery_thermal_study.py`). The service does not use it yet. The
Omnibattery switch and numbers are the owner's: this repository reads them and never writes them
([ADR-0007](../adr/0007-live-battery-control.md)).

## Interfaces

- **In:** `sensor.venus_internal_temperature` (register 35000, the only temperature the V3 driver exposes), the signed AC power
  (`sensor.venus_ac_power`), and Omnibattery's `switch.omnibattery_temp_charge_limit`, `number.omnibattery_temp_charge_limit_c`,
  `_band_c`, `_floor_pct`, `switch.omnibattery_temp_charge_limit_discharge` and the two cap numbers; all read only.
- **Out:** `(tau, T_amb, k_c, k_d)`, a per-slot temperature and charge-cap prediction (planned), the conformance of the curve.
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
| `k_c` (charge) | 6.9 degC/kW | 3.6 to 9.5 | **3.1 to 8.2** |
| `k_d` (discharge) | 11.0 degC/kW | 8.7 to 11.7 | 9.7 to 11.6 |

Validation: leave-one-episode-out 60-minute rmse 1.43 against 4.35 degC for persistence (pooled over horizons 1.11 against
3.21; walk-forward 1.02 against 3.30); above 1 kW 2.53 against 8.45, bias -1.06 degC. With the 2.5 kW episode held out the
session peak is predicted at 46.6 against 52.4 degC. Derate curve: written set power within 15 W of
`min(caps) x factor(T)` at every one of 25 points (T 48.3 to 50.2 degC, caps 1000 to 1255 W), reaction 20.7 s. Equilibrium
(thermal steady state meets the curve): about 1.8 kW at 45 degC (bootstrap 1.5 to 2.4 kW).

## Assumptions

- One first-order node with a constant ambient; the sensor is Venus-internal, not a cell temperature
  ([study](../planning/thermal-derate-aware-planning.md#2-evidence-2026-11-04-hackbattery_thermal_derate-py)).
- `k_c` is linear in power up to 2.5 kW (one session; a quadratic term fits no better and is unphysical).
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

**provisional** (2026-11-04): fitted on one day, two charges above 1 kW; `tau`, `T_amb` and the order of `k_c` are usable, `k_c`
above 1.9 kW and `k_d` are not accepted (the acceptance check above fails), and the derate curve is confirmed only over 13.5
minutes at 48.3 to 50.2 degC. Not used by the planner. The soft cap's thermal fit
([study](../planning/thermal-charge-soft-cap.md#2-thermal-evidence)) is superseded by this page; its 1.5 kW knee is
unchanged.
