# Battery model (physics, limits, losses, wear)

## Purpose and owner

The feasible charge, discharge and state-of-charge transitions of the house battery, shared by the
benchmark, the live planner and the shadow replay. Owner: this repository (`battery.py`; assumptions
resolved by `benchmark.resolve_battery_assumptions` and `service/planner.battery_spec_from_tariff` from
the `battery:` block of `config/tariff.yaml`). Roadmap: [Battery Model](../planning/components/battery-model.md).

## Interfaces

- **In:** `config/tariff.yaml` battery block (and, after commissioning, measured SOC and counters).
- **Out:** `BatterySpec` and `Horizon`; `simulate`, `check_feasible`, `compute_efc`.
- **Consumers:** [battery-optimizer](battery-optimizer.md), [boost-mode](boost-mode.md) (interlock reads
  SOC), [system-replay-benchmark](system-replay-benchmark.md), every `dataset_id` (the tariff file is hashed).

## Calibrated on

Nothing fitted. The battery (Marstek Venus E V3, AC unit) was **commissioned 2026-11-03** ([regime
log](regime-log.md)); the commissioning test measured signs and counters only. One 800 W charge read about
0.90 DC/AC and one discharge about 0.94 AC/DC, at 0.01 kWh counter resolution: consistent with 0.894 per leg,
not an RTE measurement (unbalanced partial cycle). Omnibattery's system caps are 1000 W charge / 800 W
discharge on the temporary test socket, below the modelled 2.5 kW. Confirmed from specification or contract: capacity 5.12 kWh nameplate, 2.5 kW AC power,
5.0 kW contracted import limit. Provisional values are in the next section. Datasets built at
`config_hash` `7a901801...` (RTE 0.80); everything computed at the earlier 0.88 is superseded.

## Assumptions

| Parameter | Value | Basis |
|---|---|---|
| Round-trip efficiency | 0.80 (0.89443 per leg) | Owner choice 2026-10-24, bottom of the 0.80 to 0.84 range reported by others; a scalar, not a curve |
| SOC window | 0.20 to 0.95 (3.84 kWh usable), **except on a chosen full-charge day, when the ceiling is 1.00** (ceiling 0.95 before that day, 1.00 from its start to the horizon end) | Owner's life-extension choice; real controller limits not read. The lifted day is a solver capability of [the periodic full-charge plan](../planning/periodic-full-charge.md); it is not wired to the live service yet (code only) |
| Charge limit near full | 200 W once a cell latches at 3.48 V (Omnibattery's `switch.venus_full_charge_voltage_taper`, voltage-latched, not SOC-based); modelled by one convex half-space per slot of the full-charge day, a ramp from full power at `taper.start_soc_frac` down to `taper.power_kw` at 100%. **Provisional**: `start_soc_frac` 0.90 and `power_kw` 0.2 are not measured, and the ramp is a relaxation of the real step (it allows more than 200 W just above the start), so it only binds when `start_soc_frac` is lower; `start_soc_frac: null` drops the rows | Omnibattery source read 2026-11-04 (plan section 2); to be calibrated on the first supervised run |
| Initial SOC | 0.20 | Floor, so no free stored energy |
| Idle draw | 0, not modelled | `battery.py`; unmeasured |
| High-SOC dwell cost | 0 (off); study value 0.01 EUR/kWh/h above 0.90 (`planning.high_soc_cost_eur_per_kwh_h`, `high_soc_threshold_frac`) | LFP calendar ageing is worse at high SOC; a planning convention, not a measured rate, same status as the wear cost; [study](../planning/high-soc-dwell-cost.md). The SOC window itself is unchanged |
| Wear / throughput cost | 0.01 EUR/kWh per AC kWh and direction (`planning.throughput_cost_eur_per_kwh`, set 2026-10-31) | Knee of the rolling-horizon sweep, see [Wear cost decision](#wear-cost-decision-2026-10-31); a planning convention, not a measured aging rate |
| Import limit | 5.0 kW; the model uses a 15-minute average with no margin, which is **insufficient** | Contract; enforced by the smart meter, which cuts supply on overload (owner-confirmed 2026-10-30, has happened); the trip is **instantaneous** (owner-confirmed 2026-10-31, no distributor document yet) |
| Export | none, no credit | Invariant of the kernel; no PV |

Evidence: [`config/tariff.yaml`](../../config/tariff.yaml), [benchmark](../benchmark.md), [ADR-0001](../adr/0001-home-battery-economic-dispatch.md).

## Invalidated when

- The battery is commissioned and a measurement disagrees with any row above (RTE vs power, idle draw,
  usable window, latency, firmware limits).
- The Omnibattery or firmware configuration changes limits; the warranty terms change the wear value.
- The wear cost is changed, or measured cycle ageing or the warranty terms disagree with it (below).
- Any edit to the battery block of `config/tariff.yaml`: every `dataset_id` is re-minted and the old
  datasets and benchmark results are superseded (record it).
- The import-limit basis turns out to be an instantaneous trip: the averaged constraint is then not a
  sufficient guard.
- The first supervised full charge disagrees with the taper parameters (`start_soc_frac`, `power_kw`), or
  the BMS does not reach 100% in the measured time.

## Recalibrate by

1. Commissioning measurement ([roadmap BM-1, BM-2](../planning/components/battery-model.md)): synchronized
   SOC, signed AC power and energy counters; several power and SOC bands; standby draw; full and partial
   cycles. Do not infer RTE from an unbalanced partial cycle.
2. Update the battery block in `config/tariff.yaml`, rebuild datasets, supersede the old ones, re-run
   `uv run battery-optimizer benchmark ...` ([benchmark](../benchmark.md)).
3. Wear: re-run `hack/throughput_sweep.py` (command below) on new-regime data, pick the knee and set
   `planning.throughput_cost_eur_per_kwh` (a service value; no dataset re-mint).
4. Acceptance: predicted against measured Wh and SOC on held-out physical intervals, by power band;
   a model refinement is rejected if the measured gain is negligible or it breaks MILP feasibility.
5. Taper: on the first supervised full charge record SOC, AC power and (if exposed) the maximum cell
   voltage every 30 s from 85% to completion; set `start_soc_frac` to the SOC at which AC power first
   falls under 400 W and `power_kw` to its steady value (`service.planner.FullChargeSpec`).

## Wear cost decision (2026-10-31)

**Convention.** `tp` is EUR per AC kWh, charged and discharged each (objective `(price + tp) * charge -
(price - tp) * discharge`), so a cycle of E kWh in and 0.8 E out costs `1.8 tp E`. Planning only: it never
enters the reported cost, so the savings below are real meter-bill savings of a plan made with that `tp`.
EFC is DC discharge over the 3.84 kWh usable window; nameplate cycles are EFC x 0.75.

Rolling horizon (benchmark policy, causal load forecast), observed load, dataset `cdd448abb006`, each window
against its own `no_battery`. Saving lost is relative to `tp` 0; the saving at `tp` 0 is 13.7% (S2), 16.0%
(S3) and 15.8% (summer 2025) of no-battery cost.

| tp | S2 lost | S2 EFC/day | S3 lost | S3 EFC/day | Summer lost | Summer EFC/day |
|---|---|---|---|---|---|---|
| 0 | 0% | 1.16 | 0% | 1.19 | 0% | 1.06 |
| 0.005 | 0.3% | 1.04 | 0.2% | 1.09 | -0.2% | 0.97 |
| **0.01** | **2.3%** | **0.91** | **1.7%** | **0.99** | **0.8%** | **0.89** |
| 0.015 | 5.8% | 0.80 | 4.8% | 0.88 | 2.8% | 0.82 |
| 0.02 | 10.4% | 0.70 | 11.3% | 0.73 | 5.7% | 0.74 |
| 0.03 | 21.5% | 0.52 | 21.2% | 0.57 | 15.3% | 0.59 |
| 0.05 | 44.3% | 0.28 | 44.1% | 0.33 | 44.5% | 0.29 |

Windows: S2 and S3 are 2024-10-01 to 2025-04-30 and 2025-10-01 to 2026-04-30, summer 2025-05-01 to 2025-09-30.
The oracle gives the same picture (knee 0.01 in S2, 0.015 in S3 and summer); its solves at `tp` above 0 end as
time-limited incumbents at 600 s, so its savings there are lower bounds.

**Knee** (largest `tp` before the saving falls by more than 5%): 0.01 in S2, 0.015 in S3 and summer. The
setting is **0.01**, the binding (S2) value: it gives up 1 to 2% of the saving and removes 16 to 21% of the
cycling. 0.015 is past the knee in S2.

**Warranty budget.** Marstek states a 10-year warranty and a cycle life above 6000 cycles at 25 degC (80%
retention per retailers; a trade-press report gives 70% residual capacity at 10 years; not verified in the
warranty card, and no throughput cap was found). 6000 cycles in 10 years is 1.64 nameplate cycles per day.
The sweep needs at most 0.89 nameplate cycles per day at `tp` 0 and at most 0.74 at 0.01, so the cycle budget
does not bind at any value: `tp` is an economic proxy for wear, not a warranty guard. Replacement-cost
amortisation gives `tp = price / (2 x N x usable kWh)` per direction for a battery replaced after N usable
cycles with no residual value; it is an upper bound because the battery also ages with calendar time.

**Reproduce** (outputs under `<store>/analysis/v1/throughput-sweep/<run-id>/`, fresh run id each time):

```bash
uv run python hack/throughput_sweep.py --dataset <store>/analysis/v1/<dataset_id> \
  --data-dir <store> --run-id <fresh-name>
```

## Status

**stale** (2026-11-03): the battery is commissioned and, since go-live (2026-11-03 23:45, regime log), live
with Omnibattery's system caps at 2500 W charge / 2500 W discharge and Marstek Premium mode on (from the test
socket's 1000/800 W: an "Omnibattery configuration changes limits" trigger, now matching the modelled 2.5 kW).
RTE, SOC window and idle draw are still assumptions, and the wear cost (0.01) is a planning convention to be
re-checked on measured data. Recalibrate by step 1 on the live cycles; idle draw was not set before go-live.
The full-charge ceiling and taper rows (above) are provisional and unmeasured, which extends the same reason; no
benchmark claim changes (the benchmark ladder does not model the full-charge day).
