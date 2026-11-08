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

**Measured on live data, not adopted (2026-11-07; about 80 h, 2026-11-03 22:45 to 2026-11-07 07:30 UTC).**

- Per-leg efficiency about 0.92 charging and 0.90 discharging, about 0.835 round trip, against 0.80 in the model.
- The discharge loss is roughly constant at 36 to 47 W, so efficiency depends on power: about 0.86 at 250 W and
  0.95 at about 1 kW. The charge loss is about 25 W plus 6.7% of the power.
- The apparent "missing" energy is conversion loss plus an idle drain, not the servers. The idle DC power reads
  about -30 W, and seven idle runs of 3 to 6 h show the SOC falling at about 34 W. The cause is unknown, possibly
  standby of the backup port.
- At this duty cycle the all-in round trip, drain included, is about 0.75.
- **Servers on the backup (EPS) port.** The load is flat, about 240 W (p5 to p95 220 to 276 W, maximum 370 W), about a
  third of the house's gross load. While idle or charging they pass through from the grid and are not in the AC
  charge counter (the integrated AC charge power matches the counter); while discharging they are in the
  discharge counter, and about 69% of the AC discharge went to them. The model has no backup-port load (it plans
  the house load, servers included, see [battery-load-forecast](battery-load-forecast.md)).
- **No outage reserve.** The planner keeps none for the servers: the 20% SOC floor covers about 3.5 h of them in an
  outage. Whether to add a reserve is the owner's decision, open. Protecting the **existing** 20% floor (the
  reserve protection of [ADR-0007](../adr/0007-live-battery-control.md#amendment-2026-11-08-reserve-protection))
  does not decide this: it holds the floor the owner already set, it does not raise it and keeps no extra energy for
  an outage.
- **Adopted 2026-11-08.** The idle drain of 26 W is now modelled. `config/tariff.yaml` battery block has
  `idle_drain_w: 26.0`, the benchmark resolver converts it to `idle_loss_kwh_per_hour` (0.026 kW), and the battery spec
  carries it. The SOC recurrence in `battery.py` (`soc_trajectory_kwh`) and the oracle MILP in `oracle.py` subtract the
  constant per-interval drain from every interval's SOC delta. The drain is applied unconditionally (not only when idle)
  for linearity in the MILP and consistency between the simulator and the solver; during charge and discharge the 26 W
  is negligible compared to the power levels (up to 2.5 kW) and the efficiency already captures the dominant conversion
  loss. Every policy path — the no-battery baseline (which overrides drain to zero, since a counterfactual without a
  battery incurs no standby loss), the causal price rule, the day-ahead solver, the clipping pass, the rolling-horizon
  execution carry, and all fallback branches (gap-excluded days, solver failures, frozen-by-seed) — applies the drain
  consistently so that every SOC trajectory agrees with `simulate()`. Every `dataset_id` is re-minted (the tariff file
  hash changed).

**Observed 2026-11-08 (read only, not adopted; 03:30-09:45 UTC).** The SOC fell from 18% to 15% while the battery was
idle every slot (actuation mode power 0, no charge or discharge energy in the 09:00-09:45 slots). About 3 points over
6 h at 5.12 kWh nameplate is about **26 W** of residual drain, in line with the 34 W measured on 2026-11-07; the
source of that parasitic drain is **not proven** (the backup port's about 240 to 245 W is AC *output*, which passes
through from the grid while idle or charging, so it is not evidence of a 245 W DC draw). Each plan started from a SOC
**clamped to the 0.20 floor while the battery measured 0.150** (`soc=0.200 soc_measured=0.150 soc_clamped=True`), so
the optimizer never saw the shortfall; the telemetry was not hidden, the planning state was clamped. Nothing in the
model changes here: the drain and the clamp are handled on the control side by the reserve protection of
[ADR-0007](../adr/0007-live-battery-control.md#amendment-2026-11-08-reserve-protection), which reads the measured SOC.
Hardware validation still open: the backup-port DC current and the battery's internal standby loss are not measured
(the installed HA entities for backup power and the native Omnibattery reserve were not available to read).

## Assumptions

| Parameter | Value | Basis |
|---|---|---|
| Round-trip efficiency | 0.80 (0.89443 per leg) | Owner choice 2026-10-24, bottom of the 0.80 to 0.84 range reported by others; a scalar, not a curve. Measured 2026-11-07, not adopted: about 0.835 round trip, power-dependent, about 0.75 all-in with the idle drain (see Calibrated on) |
| SOC window | 0.20 to 0.95 (3.84 kWh usable), **except on a chosen full-charge day, when the ceiling is 1.00** (ceiling 0.95 before that day, 1.00 from its start to the horizon end) | Owner's life-extension choice; real controller limits not read. The lifted day is chosen by the scheduler of [the periodic full-charge plan](../planning/periodic-full-charge.md) (off by default, `full_charge.enabled`); after completion the actuator holds the battery idle for `settle_timeout_s` (300 s) so Omnibattery's cell-spread measurement is not taken under load |
| Charge limit near full | 200 W once a cell latches at 3.48 V (Omnibattery's `switch.venus_full_charge_voltage_taper`, voltage-latched, not SOC-based); modelled by one convex half-space per slot of the full-charge day, a ramp from full power at `taper.start_soc_frac` down to `taper.power_kw` at 100%. **Provisional**: `start_soc_frac` 0.90 and `power_kw` 0.2 are not measured, and the ramp is a relaxation of the real step (it allows more than 200 W just above the start), so it only binds when `start_soc_frac` is lower; `start_soc_frac: null` drops the rows | Omnibattery source read 2026-11-04 (plan section 2); to be calibrated on the first supervised run |
| Initial SOC | 0.20 | Floor, so no free stored energy |
| Idle draw | 26 W (0.026 kW), adopted 2026-11-08 | `battery.py`, `oracle.py`; `config/tariff.yaml` `idle_drain_w: 26.0`. Measured 2026-11-07 at about 34 W and 2026-11-08 at about 26 W during idle slots; applied unconditionally to every interval's SOC recurrence (constant parasitic loss, negligible during charge/discharge vs efficiency). Source not proven (possibly backup port standby) |
| High-SOC dwell cost | 0 (off); study value 0.01 EUR/kWh/h above 0.90 (`planning.high_soc_cost_eur_per_kwh_h`, `high_soc_threshold_frac`) | LFP calendar ageing is worse at high SOC; a planning convention, not a measured rate, same status as the wear cost; [study](../planning/high-soc-dwell-cost.md). The SOC window itself is unchanged |
| Thermal charge-power soft cap | 0 (off); study value 1.5 kW at 0.005 EUR/kWh (`planning.charge_soft_cap_kw`, `charge_soft_cap_eur_per_kwh`) | The internal temperature rose about 10 degC in a 50 min charge at about 1.4 kW (2026-11-04) and LFP ageing is worse hot; a convex price on AC kWh charged above the knee, a planning convention like the wear cost, not a thermal model. The knee is a documented conservative choice because the thermal fit rests on 21 h and two charge sessions; [study](../planning/thermal-charge-soft-cap.md). The 2.5 kW limit itself is unchanged |
| Discharge import margin | 0 in the model; the live actuator keeps `actuation.discharge_margin_w` (100 W deployed since 2026-11-05, 200 W before) of grid import while discharging, so deliverable discharge is at most load minus that. Optional planner bound `planning.discharge_margin_bound` (code default off, enabled in the deployed config since 0.14.0, 2026-11-05) | Owner-chosen to stop brief exports (-10 to -94 W at 50 W, 2026-11-03); a control setting, not a battery parameter, so the model only notes it ([known issue](../known-issues.md#planned-discharge-is-not-delivered-planner-bound-enabled-since-0140-monitoring)) |
| Omnibattery Temperature Charge Limit | On since 2026-11-04 14:10:56 UTC (40 degC, 10 degC band, floor 40%, discharge off); **not modelled**: charge power is capped at `limit x f(T)` by the Venus internal temperature (1000 W at 50 degC or more) | Owner setting, may change at will; curve checked over 48.3 to 50.2 degC, [battery-thermal](battery-thermal.md), [design](../planning/thermal-derate-aware-planning.md) |
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


## KPIs

Observability only; they show whether the battery does what the model assumes. Metrics are prefixed
`battery_optimizer_`; the offline tool is `uv run python hack/day_ahead_kpis.py --from <day> --to <day>`.

| KPI | Where |
|---|---|
| Commanded against actual power | `actuator_desired_pd_target_watts` and `actuator_observed_battery_power_watts` (`dry_run`/`live`); the planned power is `slot_battery_power_watts{plan}` (+ charge), the actual from Home Assistant is negated (`sensor.venus_ac_power` is + while discharging) |
| Planned energy not delivered (clamped) | `actuator_clamped_watts` (`dry_run`/`live`); `shadow_clipped_energy_kwh_total` for the replay's clipping; offline `battery.<series>.mae_net_kwh_per_slot` |
| SOC tracking | offline `soc.<plan>.mae_frac` and `bias_frac` (actual SOC at the slot end minus the plan's target); `slot_soc_target_ratio{plan}` against the HA SOC sensor |
| Round-trip efficiency | not a service metric: the AC discharge energy over the AC charge energy of a full cycle plus the SOC change, from `shadow_actual_battery_energy_kwh_total` and the SOC sensor, or offline `battery.actual.discharge_kwh / charge_kwh` over a window that starts and ends at the same SOC |

## Status

**stale** (2026-11-04, reason added: the owner's Omnibattery Temperature Charge Limit now derates the charge power, see the assumptions table and the [regime log](regime-log.md)); first stale on 2026-11-03: the battery is commissioned and, since go-live (2026-11-03 23:45, regime log), live
with Omnibattery's system caps at 2500 W charge / 2500 W discharge and Marstek Premium mode on (from the test
socket's 1000/800 W: an "Omnibattery configuration changes limits" trigger, now matching the modelled 2.5 kW).
RTE is still an assumption (measured 2026-11-07, not adopted: the proposal is in Calibrated on), the idle draw is
now adopted at 26 W (2026-11-08, see assumptions table), and the wear cost (0.01) is a planning convention to be
re-checked on measured data. Recalibrate by step 1 on the live cycles.
The full-charge ceiling and taper rows (above) are provisional and unmeasured, which extends the same reason; no
benchmark claim changes (the benchmark ladder does not model the full-charge day).
