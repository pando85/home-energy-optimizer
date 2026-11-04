# Battery optimizer and live planner

## Purpose and owner

Chooses charge and discharge per 15-minute slot to minimise the cost of importing the household load.
One MILP kernel (`oracle.solve_oracle`, HiGHS) serves the benchmark policies (`policies.py`,
`rolling_horizon.py`) and the live shadow planner (`service/planner.py` `run_planner`, free terminal mode,
re-solved every 15 minutes). Owner: this repository. Roadmap:
[Dispatch Optimizer](../planning/components/dispatch-optimizer.md). Go-live is through OmniBattery, driven
by this optimizer, under [ADR-0007](../adr/0007-live-battery-control.md) (accepted 2026-11-03): `mode: shadow` (default)
has no writer; `dry_run` records the Omnibattery writes it would make; `live` sends them
(`service/actuator.py`).

## Interfaces

- **In:** [battery-load-forecast](battery-load-forecast.md), published prices ([tariff-prices](tariff-prices.md)),
  [battery-model](battery-model.md), current SOC (the measured `sensor.venus_battery_soc` since this
  change, `soc_source: entity`, trusted on availability and Omnibattery's liveness sensor;
  [known issues](../known-issues.md#planning-from-the-soc-entity-checks-availability-and-liveness-not-age)).
- **Out:** `DispatchPlan` and `DispatchIntent` (`dispatch.py`, types only), shadow records and metrics.
  Not a battery command by itself.
- **Consumers:** [system-replay-benchmark](system-replay-benchmark.md); the battery actuator
  (`service/actuator.py`, `dry_run`/`live` only), which maps the current slot's intent to Omnibattery's
  direction switches, PD grid-import target (capped at `actuation.import_cap_w`, 4500 W) and discharge cap.

## Calibrated on

- Nothing is fitted; the evidence is the benchmark on the legacy regime: dataset `2e01620ba727`
  (2024-01-01 to 2025-12-31, RTE 0.80), run `fe3cbfec46b6`, oracle certified. Share of the causal-to-oracle
  gap closed: `day_ahead` 65.1%, `rolling_horizon` 69.8% (causal load basis).
- Replay on S2 and S3: `rolling_horizon` removes about 13 to 16% of no-battery cost under either heat-pump
  regime; the battery sits at its SOC floor while importing in 0 to 3.3% of expensive cold-day slots
  ([system replay](../planning/system-replay.md)).
- Plant regime: no battery, legacy heat pump, pre-tax PVPC.

## Assumptions

- **The objective is unchanged; its feasible set may contain one day with a lifted ceiling and a soft
  checkpoint** ([periodic full charge](../planning/periodic-full-charge.md), chosen by the service's
  scheduler only with `full_charge.enabled`, off by default). On that day the SOC ceiling is 1.00 (0.95 before it), the SOC must reach 100% at
  a slot the optimizer picks, or the plan pays `shortfall_penalty_eur_per_kwh` (10, two orders of magnitude
  above any price) for the missing energy, so the plan is always feasible; the terminal value, the free
  terminal mode and the regular plan are untouched, and the regular plan is never replaced by an evaluation
  (`run_planner` evaluate mode adds the lifted and target solves of the same snapshot). The extra cost of the
  day is `forcing_cost = target - lifted` (the lifted free plan also stores more at the horizon end, so
  `target - normal` is a diagnostic only). The solve is a MILP with one binary per eligible slot: the target
  solve takes about 13 times the regular solve at the median (taper on, random windows) and about 1.5 s at the
  worst, against a 20 s limit.
- Load is exogenous. The decoupled design keeps the objective unchanged; only price-driven heat-pump
  planning and a joint MPC are dropped ([ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md)).
- The horizon ends at the last published price slot; there is no unpublished-price forecast live.
- **Hard constraint (owner-confirmed 2026-10-30):** the 5.0 kW import limit is enforced by the smart
  meter, which cuts supply on overload; this has happened. A trip is a safety and availability failure,
  never an acceptable cost trade-off. The trip is **instantaneous**, not a 15-minute average (owner-confirmed
  2026-10-31; no distributor document yet).
- Charge is limited only through forecast load (`charge <= limit * h - forecast load`) on a 15-minute
  average with no margin; load that is not in the forecast (boost, hot-water booster) is invisible to that
  constraint. Because the trip is instantaneous, the cap cannot rely on 15-minute averages: OmniBattery must regulate
  against the measured load in real time with a margin below the limit. This is a requirement for the
  actuator ADR, and at go-live the cap is **required**, not optional. ADR-0007 implements it as the PD
  grid-import target `min(house load + P, 4500 W)` (Omnibattery reduces the charge when house load rises), a
  guard that refuses any written target above the cap, and a hard rule that switches charge off when the
  measured import exceeds 4700 W. This house limit is separate from the battery's own 2500 W charge/discharge
  limit (`number.venus_max_charge_power` / `number.venus_max_discharge_power`, minimum 800 W), which the
  optimizer does not write.
- **Heater inputs to the import limit** (Panasonic service manual; details in
  [heat-pump-controller](heat-pump-controller.md#heater-facts-import-limit-inputs)): the heat pump alone draws
  at most 3.47 kW, and its integrated 3.00 kW electric heater brings the worst case to about 6.5 kW, above the
  5.0 kW limit without any other load. The heater starts for hot water above about 51 degC, for the weekly
  sterilization (65 degC, 10 min, default) and below the outdoor heater-on temperature (default 0 degC). The
  sterilization is a **scheduled 3 kW load**, and none of it is in the load forecast, so the grid-target cap
  must hold the battery charge below the limit minus the heater, not only the forecast load.
  Read-only heater binaries now exist (`binary_sensor.aquarea_internal_heater_state`,
  `binary_sensor.aquarea_external_heater_state`, `binary_sensor.aquarea_force_heater_state`; history from
  2026-10-30/31): they are the signal for a future heater-aware load guard (follow-up, not built now).
- **Negative prices are handled; no price is clamped.** The price paid can be negative (26 h at or below 0 in
  2024-01 to 2026-10, 18 h of them below 0, minimum -0.0126 EUR/kWh pre-tax; [boost-mode](boost-mode.md)).
  The cost model keeps them (`cost.py:26-28`, `cost.py:634-637`, `cost.py:1064-1066`: a negative price gives a
  negative cost), the MILP forbids charging and discharging in the same slot with a binary mode, so a negative
  price is a charging opportunity and not a free loop (`oracle.py:28-40`; tests `tests/test_battery.py:273`
  and `:289`), and the PVPC reader drops only non-finite values (`service/pvpc.py:108-115`). Remaining limit:
  at a negative price the optimiser wants to import as much as it can, bounded only by the import-limit
  headroom on forecast load (the cap in the assumption above), and export is not modelled.
- **Throughput cost 0.01 EUR/kWh** per AC kWh and direction (`config/service.yaml`, set 2026-10-31), the knee
  of the rolling-horizon sweep: it keeps 98% (S2), 98% (S3) and 99% (summer) of the zero-cost saving and cuts
  cycling from about 1.2 to about 0.9 to 1.0 equivalent full cycles per day, well inside the warranty cycle
  budget ([sweep table and warranty](battery-model.md#wear-cost-decision-2026-10-31)). At 0.0 every marginal
  cycle that clears the 0.80 round trip was taken. The sweep used the benchmark's two-publication rolling
  horizon, not the live 15-minute re-solve; the live planner's behaviour at 0.01 is unmeasured.
- **High-SOC dwell cost (off, 2026-11-04).** `planning.high_soc_cost_eur_per_kwh_h` (0 = off) prices every
  kWh of end-of-slot SOC above `planning.high_soc_threshold_frac` (0.90) per hour held, in every solve (the
  full-charge ones too, so the 100% checkpoint finishes close to the discharge; the checkpoint's shortfall penalty
  must exceed the dwell cost over the longest horizon, checked at solve and at config load). Planning only: the
  reported cost excludes it and the dual bound is dropped, as for the throughput cost. Replay study
  ([high-soc dwell cost](../planning/high-soc-dwell-cost.md)): 0.01 EUR/kWh/h cuts the hours above 90% by 46% and
  those at or above 95% by 70% for 0.08% of cost (0.41% of the saving); a hard 20-90 window costs 3.3% of the
  saving. Measured on the benchmark's rolling horizon, not the live 15-minute re-solve (one season checked).
- **Thermal charge-power soft cap (off, 2026-11-04).** `planning.charge_soft_cap_eur_per_kwh` (0 = off) prices every
  AC kWh charged above `planning.charge_soft_cap_kw` (1.5) in a slot, in every solve (the full-charge ones too; the
  checkpoint's shortfall penalty must exceed the dwell cost plus the cap cost per stored kWh, checked at solve and at
  config load). Planning only, same reporting rules as the dwell cost. Replay study
  ([thermal soft cap](../planning/thermal-charge-soft-cap.md)): 1.5 kW at 0.005 EUR/kWh cuts the mean power while
  charging from 2.13 to 1.49 kW and the energy charged above 1.5 kW from 35% to 7% for 0.10% of cost (0.52% of the
  saving, worst window 0.91%). The knee is a conservative documented choice: the temperature fit has 21 h of data and
  no session above 1.5 kW. Omnibattery's Temperature Charge Limit stays the backstop.
- Omnibattery's own price features will be off, so this is the only battery price reactor.

## Invalidated when

- Any [battery-model](battery-model.md) parameter changes, or the wear cost, the high-SOC dwell cost or the thermal
  charge soft cap is changed.
- [tariff-prices](tariff-prices.md) changes (taxes confirmed, holidays added, new publication timing).
- The load forecast changes regime (comfort schedule, boost): re-check floor-hit share and clipping.
- At go-live: measured SOC replaces simulated SOC; the charge adapter caps the grid target below the
  import limit; a second price-reactive writer appears.
- Omnibattery is upgraded beyond 1.5.1b1 (the weekly full-charge contract the day relies on), the SOC window of
  `tariff.yaml` changes, or the charge-delay configuration differs from the one the plan assumes.
- The heater facts change (hot-water target above about 51 degC, sterilization schedule, a power-limit input
  fitted), or the contracted power or the meter's trip behaviour changes, or a meter cut-off happens with the battery
  charging (treat as a failed acceptance and review the margin).

## Recalibrate by

`uv run battery-optimizer benchmark --dataset <store>/analysis/v1/<dataset_id> --train-end <ISO>
--policies all --terminal-mode free --time-limit-s 600` ([benchmark](../benchmark.md)), on a window in the
new regime after the 28-day settling period, with that window's measured load as `no_battery`.
Acceptance: oracle certified; `rolling_horizon` gap closure and relative saving against `no_battery` on the
same load are reported next to the legacy figures above (same RTE basis) and any material drop is
explained before go-live; the floor-hit share on expensive cold-day slots stays near the replay's 0 to 3.3%.
For the full-charge day: the existing benchmark plus the `dry_run` decision study of the plan (the distribution
of `forcing_cost`); the benchmark ladder itself does not model the day, so no benchmark claim is made for it.


## KPIs

Observability only ([service](../service.md#day-ahead-and-final-plan)). The *day-ahead plan* of a slot is the
first plan that covered it, the *final plan* the plan of record at the slot open. Metrics are prefixed
`battery_optimizer_`.

| KPI | Where |
|---|---|
| Plan revision, kWh per day (how much the plan moved between day-ahead and final) | `increase(plan_revision_kwh_total[1d])`; offline `battery.plan_revision` |
| Daily saving against a house without the battery (pretax, before valuing the stored energy) | offline only: `battery.<series>.saving_eur` and `saving_eur_per_day` for `day_ahead`, `final` and `actual` (the live cost counters are `shadow_native_cost_eur` and `shadow_counterfactual_cost_eur`) |
| Average price charging against average price discharging | offline `avg_price_charging_eur_per_kwh` and `avg_price_discharging_eur_per_kwh`; a working arbitrage has the second above the first by more than the round-trip loss and wear cost |
| Planned against actual battery energy | `increase(shadow_day_ahead_energy_kwh_total{direction}[1d])`, `shadow_planned_energy_kwh_total` (final), `shadow_actual_battery_energy_kwh_total` (actual) |
| Power and SOC target, day-ahead against final | `slot_battery_power_watts{plan}`, `slot_soc_target_ratio{plan}` |

Offline: `uv run python hack/day_ahead_kpis.py --from <day> --to <day>`.

## Status

**stale** (2026-11-03): live since 2026-11-03 23:45 (first live slot 22:45 UTC; [regime log](regime-log.md)),
with system caps 2500/2500 W and Marstek Premium mode on. The go-live trigger above fired (measured SOC replaces
simulated SOC, the import-cap adapter writes the grid target); the objective and the model are unchanged.
Recalibrate by the benchmark above on measured load after the 28-day settling period; RTE, idle draw and the
wear cost are re-checked through [battery-model](battery-model.md). Before go-live, **valid, shadow only**
(2026-10-31): the actuator ([ADR-0007](../adr/0007-live-battery-control.md), accepted
2026-11-03) and its import-cap adapter are built but not deployed (`mode: shadow` in the committed config): a code
change, not a plant change, so no regime-log row and no recalibration. The battery was commissioned on
2026-11-03 (regime log); no trigger above fired (the battery block of `tariff.yaml` is unchanged, the battery is
idle). The AC power sign is measured (-1); the shadow planner reads the measured SOC (`soc_source: entity`, a
config change of the optimizer, not of the plant: the inputs and the model are unchanged, so no recalibration).
Not go-live ready: idle draw, the HA-independent failsafe ([ADR-0007](../adr/0007-live-battery-control.md#live-preconditions-2026-11-03)),
the PD-target bound while the system power limits are on (pre-go-live: charge cut to the test socket's 1000 W
system max charge minus the house load; go-live: system caps 2500/2500 W and Marstek Premium mode for discharge
above 800 W, matching `power_kw: 2.5`) and the [battery-model](battery-model.md) caps are open; the wear cost is set (0.01) and is
re-checked on measured data.

Full-charge solver (2026-11-04, code only): `oracle.FullChargeModel` and `planner.FullChargeSpec` add the lifted
ceiling, soft checkpoint and taper rows described above. Nothing calls them unless the scheduler is enabled, so
plans are byte-identical to before; it extends the reason above with the taper parameters, which are provisional.

Full-charge scheduler (2026-11-04, code, `full_charge.enabled: false`, [regime log](regime-log.md)): the service
prices tomorrow once per evening after the regular plan is published (`planner.evaluate_full_charge`), chooses the
day by the adaptive cadence of the balance readings, and from the decision on plans with the commit-mode target
plan until the run ends. With the feature off (the committed default) nothing changes and no recalibration is due;
the recalibration when it is switched on is the `dry_run` decision study above plus the first supervised run.

High-SOC dwell cost (2026-11-04, code only): `high_soc_cost` in the solver, the planner, the service config and the
benchmark, default 0, so plans, `inputs_hash` and `config_hash` are byte-identical until a deploy sets it (planned,
regime log). Merging changes nothing observable and nothing goes stale; enabling it changes the plans (less time
above 90%, same cost within 0.1%) and is recorded in the regime log when it happens, with the dependants checked
then (the replay benchmark and the dry-run decision study are recomputed on plans made with it).

Thermal charge soft cap (2026-11-04, code only): `charge_soft_cap` in the solver, the planner, the service config and the
benchmark, default 0, so plans, `inputs_hash` and `config_hash` are byte-identical until a deploy sets it (planned,
regime log). Merging changes nothing observable and nothing goes stale; enabling it changes the plans (charge spread over
more slots, same cost within 0.1%) and is recorded in the regime log when it happens, with the dependants checked then.

Slot decisions (2026-11-04, code only, regime log): a slot that opened without an intent adopts one later in
the slot (`late_intent`, see [service](../service.md#scheduling-and-the-slot-cycle)), and a solve inside such a
slot plans from the slot in progress. This changes when the actuator follows a plan, not what the plan is: the
kernel, its inputs and the calibration above are untouched, so no recalibration and no dependent goes stale.
Shadow-report windows that span the deploy mix the two slot rules (`late_intent` slots are not counted as
coverage, and their counterfactual covers only the adopted share of the slot).

Slot records (`service/v1/slots/`) carry the battery counters, the gross load and the counterfactual cost
for battery-mapped slots only from the deploy of the edge-value fix on (2026-11-04, code only, regime log):
earlier live slots have them `null` ([known issues](../known-issues.md#idle-battery-counters-voided-a-live-slots-gross-load)).
A recording fix, not a plant or model change: no recalibration.

Derate (2026-11-04, regime log, plant change): the owner switched on Omnibattery's Temperature Charge Limit (14:10:56 UTC), so the
realised charge power is capped by the battery temperature (1000 W at 50 degC or more) while the plan still assumes the spec's
2.5 kW in every slot. The plans stay valid and the replan corrects each 15-minute slot; the stale status above is extended with this
reason. [Derate-aware planning](../planning/thermal-derate-aware-planning.md) is designed, not built; the temperature model is
[battery-thermal](battery-thermal.md).
