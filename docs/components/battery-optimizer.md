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
  checkpoint** ([periodic full charge](../planning/periodic-full-charge.md), solver and planner only so far,
  not wired to the service). On that day the SOC ceiling is 1.00 (0.95 before it), the SOC must reach 100% at
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
- Omnibattery's own price features will be off, so this is the only battery price reactor.

## Invalidated when

- Any [battery-model](battery-model.md) parameter changes, or the wear cost is changed.
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
ceiling, soft checkpoint and taper rows described above. Nothing calls them yet (the service never sets
`full_charge` on a snapshot), so plans are byte-identical to before and no recalibration or regime-log row is due
from this change; it extends the reason above with the taper parameters, which are provisional.

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
