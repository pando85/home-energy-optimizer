# High-SOC dwell cost

- **Status:** implemented in the solver, the planner, the service config and the benchmark; **off by default**
  (`planning.high_soc_cost_eur_per_kwh_h: 0`). The study below recommends a value; enabling it is a deploy-config
  change recorded as `planned` in the [regime log](../components/regime-log.md).
- **Date:** 2026-11-04
- **Related:** [periodic full charge](periodic-full-charge.md) (the 100% checkpoint day this cost must also shape);
  the battery-longevity plan (`battery-longevity.md`, a parallel change) covers the wider longevity question.
- **Code:** `oracle.build_model` / `solve_oracle` (`high_soc_cost`), `service/planner.py`, `service/config.py`,
  `rolling_horizon.py`, `policies.py`, `benchmark.py`; study `hack/high_soc_dwell_sweep.py`.

## 1. Why

LFP calendar ageing is worse at high state of charge (and at high temperature). A review recommends a 90% daily
maximum, with 95% only when the extra energy is discharged soon after. Today the planner has a hard 95% ceiling and
no price on the time spent high, so it parks at 95% as soon as that is free (tie-breaking in the MILP is arbitrary
among equal-price slots), and on a full-charge day it can sit at 100% for hours.

## 2. Model

`high_soc_cost = (threshold_frac, eur_per_kwh_h)`. One continuous column `e[t] >= 0` per slot with the row
`e[t] >= soc[t] - threshold * capacity` and `+ cost * h * e[t]` in the objective, `h` the slot length in hours: the
solver pays `cost` for every kWh of end-of-slot SOC above the threshold for every hour it is held. Properties:

- `None` or a zero cost gives the historical model byte for byte (golden digests in the tests); the columns come after
  the full-charge columns and the rows after every other row, so no index moves.
- It is a **planning convention** like the throughput cost, not an ageing model. The reported `cost_eur` is the
  plan's real cost (dwell excluded); the dwell cost is recoverable as `cost * high_soc_dwell_kwh_h(...)`. The dual
  bound belongs to the penalised objective, so with an active cost it is dropped and the result is reported as
  `feasible` / `upper`, exactly as with a non-zero throughput cost.
- **Full-charge day.** The cost applies in every solve of the snapshot, the lifted and target ones included, which is
  what makes the 100% checkpoint finish close to the discharge. The shortfall penalty of the checkpoint must
  dominate it: the solver refuses `shortfall_penalty <= cost * horizon_hours` (the dwell cost of holding one kWh
  above the threshold for the whole horizon) and the service config refuses it for the longest horizon, so the
  checkpoint is reached whatever the dwell price; the dwell cost only decides when the plan climbs.
- The cost is zero at or below the threshold, so a plan parked exactly at the threshold is free. The metric "hours
  above 90%" below is therefore strict, and "at or above 90%" is reported next to it.

## 3. Study

**Method.** `hack/high_soc_dwell_sweep.py`, the method of `hack/throughput_sweep.py`: the unchanged benchmark with
`policies=no_battery,rolling_horizon` (causal trailing-mean load, observed load, two publications per day), one run
per window and variant, throughput cost 0.01 in every variant, dataset `cdd448abb006` (RTE 0.80, window 0.20 to
0.95). Windows: 2024-02-01 to 2024-10-01 (W24), S2, summer 2025, S3, summer 2026 (to 2026-10-01): 956 days, all the
days of the dataset after a 28-day warm-up. Variants: **A** hard 20 to 90%; **B** hard 20 to 95% (today, the
reference); **C** 20 to 95% plus the dwell cost above 90% at 0.002 to 0.05 EUR/kWh/h. The A ceiling is applied after
the tariff is read (the dataset pins the tariff hash). Relative figures only.

Metrics: cost against B, in percent of B's cost and in percent of **B's saving over no battery** (the criterion);
mean hours per day strictly above 90%, at or above 90%, at or above 95%; throughput against B; the share of the
energy in the 90 to 95% band that is discharged within 2 h and 6 h of reaching it (the band is cut into 50 SOC levels;
for each level and each uninterrupted run of slots above it, the run length is the time that slice of energy sat
there; the share counts slices whose run is at most N hours).

**Result, pooled over 956 days (rolling horizon, two publications per day):**

| variant | cost vs B | penalty, % of B's saving | h/day above 90% | h/day at or above 90% | h/day at or above 95% | throughput vs B | 90-95% energy out within 2 h | within 6 h |
|---|---|---|---|---|---|---|---|---|
| A: hard 20-90 | +0.61% | +3.32% | 0.00 | 4.37 | 0.00 | -3.5% | n/a | n/a |
| B: hard 20-95 (today) | 0 | 0 | 5.18 | 5.18 | 4.18 | 0 | 13% | 82% |
| C 0.002 | +0.01% | +0.04% | 3.84 | 4.99 | 2.39 | +0.1% | 28% | 91% |
| C 0.005 | +0.03% | +0.14% | 3.30 | 4.90 | 1.86 | 0.0% | 38% | 92% |
| **C 0.01** | **+0.08%** | **+0.41%** | **2.82** | 4.77 | **1.26** | 0.0% | **49%** | 95% |
| C 0.02 | +0.15% | +0.84% | 2.37 | 4.66 | 0.84 | -0.3% | 62% | 95% |
| C 0.05 | +0.33% | +1.77% | 1.94 | 4.56 | 0.51 | -1.0% | 66% | 93% |

Per window the penalty in percent of B's saving is at most 0.82% at 0.01 (W24; S2 0.59, S3 0.44, summer 2025 0.40,
summer 2026 about 0), and 1.04%, 1.10% at 0.02 in S2 and S3 (so 0.02 fails a per-window 1% test although its pooled
figure is 0.84%). The hard 20-90 window costs 2.65 to 4.01% of B's saving per window and cuts throughput by 3 to
5%; the dwell cost keeps throughput within 1% of B up to 0.02.

**Criterion and recommendation.** The largest swept value whose cost penalty against B stays under 1% of B's saving
in **every** window while hours above 90% fall substantially: **0.01 EUR/kWh/h**. It removes 46% of the hours above
90% (5.18 to 2.82 per day), 70% of the hours at or above 95% (4.18 to 1.26), and moves the share of 90 to 95% energy
discharged within 2 h from 13% to 49%, for 0.08% of the cost and 0.41% of B's saving. It is roughly a quarter of the
cost of the hard 20-90 window (3.32% of B's saving), which removes the same hours but also the 90 to 95% energy that
does pay. Going to 0.02 buys 0.45 more hours per day for twice the penalty.

Residual: the plan still sits at exactly 90% for about 4.8 h/day, because the cost is zero at the threshold. If the
owner wants less time at 90%, lower `high_soc_threshold_frac`; that is a different trade-off and not measured here.

## 4. Replan cadence check and full-charge day

**Closer to the live cadence (S3, 211 days).** The same sweep with the rolling horizon re-solved every hour (4
slots) on the causal load forecast with a 192-slot horizon, as the live planner does (it re-solves every 15 minutes;
not run at that cadence for cost of compute):

| variant | cost vs B | penalty, % of B's saving | h/day above 90% | h/day at or above 95% | throughput vs B | 90-95% energy out within 2 h |
|---|---|---|---|---|---|---|
| A: hard 20-90 | +0.52% | +2.62% | 0.00 | 0.00 | -2.4% | n/a |
| B: hard 20-95 (today) | 0 | 0 | 5.43 | 4.29 | 0 | 12% |
| C 0.005 | +0.03% | +0.14% | 2.64 | 1.74 | +0.1% | 42% |
| **C 0.01** | **+0.13%** | **+0.65%** | **1.63** | **0.85** | -0.1% | **62%** |
| C 0.02 | +0.28% | +1.44% | 0.68 | 0.23 | -0.8% | 92% |

With more frequent replanning the same cost buys more: 0.01 removes 70% of the hours above 90% (46% at the
two-publication cadence) for a penalty of 0.65% of B's saving; 0.02 is past the 1% criterion here. 0.01 is the value
that satisfies the criterion at both cadences.

**Full-charge day (60 sampled days, 2025-10-01 to 2026-04-30, every third day).** Each day is the planner's commit
model solved on realised prices and load (a perfect forecast, so only the dwell price differs): ceiling 0.95 until
local midnight and 1.00 after, a checkpoint at 100% in any slot of the day, shortfall penalty 10, taper from 95%, plan
from 50% at 20:30 the evening before, 192 slots. The target is reached on every day at every value.

| variant | hours at 100% per day | hours above 90% | hours from first 100% to back at 90% | extra cost, % of B's saving on these days |
|---|---|---|---|---|
| B (no dwell cost) | 8.6 | 11.0 | 5.9 | 0 |
| C 0.002 | 5.0 | 6.9 | 3.7 | +0.24% |
| C 0.005 | 4.1 | 5.9 | 3.4 | +0.51% |
| **C 0.01** | **2.0** | **3.8** | **2.7** | **+1.55%** |
| C 0.02 | 0.6 | 1.9 | 1.5 | +3.08% |
| C 0.05 | 0.3 | 1.0 | 1.0 | +4.32% |

The dwell cost does what it is meant to on the 100% day: at 0.01 the plan sits at 100% for 2.0 h instead of 8.6 h,
and the checkpoint is reached late, just before the discharge. The extra cost applies only on the full-charge day
(one day in 5 to 15), on top of that day's forcing cost, so it is about 0.1 to 0.3% of B's saving averaged over a
season. A separate, lower value for the full-charge day is possible but not needed.

## 5. Limits

- The replay is the benchmark's rolling horizon on the observed load and the dataset's SOC rules, not the live
  planner on live data; the live battery is only days old. An hourly re-solve is checked on one season in section 4.
- The cost is a convention. No ageing rate was measured, and temperature (the other half of the review's concern) is
  not modelled; the value should be revisited once measured capacity fade or the manufacturer's calendar-ageing curve
  is available.
- Windows are replayed separately (each starts at the SOC floor), so cross-window SOC continuity is not modelled.

## 6. Reproduce

```bash
uv run python hack/high_soc_dwell_sweep.py --dataset <store>/analysis/v1/<dataset_id> \
  --data-dir <store> --run-id <fresh-name> --workers 16
uv run python hack/high_soc_dwell_sweep.py --dataset ... --run-id <fresh> --full-charge-only
```

Outputs are under `<store>/analysis/v1/high-soc-sweep/<run-id>/` (private euro figures stay in the benchmark runs;
`table.md` holds relative figures only).
