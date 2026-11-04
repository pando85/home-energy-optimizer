# Thermal soft cap on charge power

- **Status:** implemented in the solver, the planner, the service config and the benchmark; **off by default**
  (`planning.charge_soft_cap_eur_per_kwh: 0`). The studies below recommend **1.5 kW at 0.005 EUR/kWh**; enabling it is a
  deploy-config change recorded as `planned` in the [regime log](../components/regime-log.md).
- **Date:** 2026-11-04
- **Related:** step 6 of the [battery longevity plan](battery-longevity.md#4-steps); the [high-SOC dwell
  cost](high-soc-dwell-cost.md) (the template for this change, and enabled alongside it); the [periodic full
  charge](periodic-full-charge.md) (the checkpoint this cap must not outweigh).
- **Code:** `oracle.build_model` / `solve_oracle` (`charge_soft_cap`), `service/planner.py`, `service/config.py`,
  `rolling_horizon.py`, `policies.py`, `benchmark.py`; studies `hack/battery_thermal_study.py` and
  `hack/charge_soft_cap_sweep.py`.
- **Privacy:** relative figures, temperatures, SOC points and powers only; no energy totals, costs, addresses.

## 1. Why

On 2026-11-04 the battery's reported internal temperature rose by about 10 degC (about 32 to 42 degC) during a charge of
about 50 minutes at about 1.4 kW at mid SOC, and fell back within an hour. LFP ageing is worse hot, the Omnibattery
Temperature Charge Limit (documented: starts reducing power above 40 degC) is only a backstop, and the 2.0TD valley is
long, so charging the same energy more slowly usually costs little. Today the planner is indifferent to charge power
below the 2.5 kW limit: among equal-price slots the MILP picks arbitrary ones, and in the replay it charges at full power
for a third of the charged energy above 1.5 kW (section 3).

## 2. Thermal evidence

**Method.** `hack/battery_thermal_study.py` reads the internal temperature, the AC power and the SOC of the battery
read-only from the long-term Prometheus (`kubectl --context=<ctx> get --raw`, GET only, explicit context) from the
commissioning day (2026-11-03) to the time of the run, aligns them on a 30 s grid and fits a first-order model
`dT/dt = a_c*P_charge + a_d*P_discharge - b*T + c` (equivalently `a|P| - b*(T - T_amb)` with `T_amb = c/b`, an ambient
proxy that also absorbs idle self-heating) by least squares on non-overlapping 5-minute windows. Quality is judged by
R2, a leave-one-6-hour-block-out prediction check, a block bootstrap of the projection, and free simulations of whole
sessions against a persistence baseline (hold the starting temperature).

**Data.** 21.6 hours: 252 usable windows, 14 charging and 39 discharging windows. **Two charge sessions**: 14 min at
about 0.8 kW (47 to 50% SOC), and the 48 min session at about 1.4 kW (39 to 59%, 32.2 to 41.0 degC); seven small
discharge sessions at 0.2 to 0.7 kW. The highest 5-minute mean charge power in the data is 1.5 kW. There is no session at
2.0 or 2.5 kW, so the "1.5 versus 2.5 kW comparison of otherwise similar sessions" the longevity plan asks for **cannot
be made yet**.

**Result, honestly.**

| fit | R2 (in sample) | leave-block-out R2 | what it says |
|---|---|---|---|
| one first-order model on all windows | 0.73 | 0.05 (pooled), about 0 (split charge / discharge) | tau about 110 min, T_amb proxy 30.8 degC, `a` about 0.17 degC/min/kW. **No predictive skill**: the fit is driven by the slow idle drift and the long cool-down tail, and it projects a steady 60 degC at 1.5 kW, which nothing supports |
| the same model on the 1.4 kW session and its cool-down only (35 windows) | 0.97 | not applicable (one event) | `a` = 0.208 (se 0.006) degC/min/kW, `b` = 0.0266 (se 0.0017) per min, so **tau about 38 min**; T_amb proxy 33.4 degC |

The sensor does not behave as one first-order node: the cool-down after the session has a fast part (tau about 40 min)
and a slow one (about 90 min and more, reaching the 32 to 33 degC idle level only after hours), and the commissioning
afternoon shows an unexplained rise of about 3 degC at zero power. The event fit is the better description of the
heating phase; it is **a one-session, in-sample fit**, validated only weakly out of sample: on the 0.8 kW charge session
its free simulation misses by 1.4 degC against 3.4 degC for persistence, on the discharge sessions it beats persistence
in 2 of 6 (where persistence is already within 0.1 to 0.3 degC). The script's verdict is **too thin**: the pooled
model has no cross-validated skill (its only failed test; it has 14 charging windows, above its minimum of 8, from just
two sessions), and the event fit has one session and no data above 1.5 kW.

**Indicative projection (event fit, from an idle start at the T_amb proxy; extrapolation above 1.5 kW).** Steady state is
`T_amb + a*P/b`; the peak after a charge of S SOC points lasts `S*capacity/(P*0.9)`.

| charge power | steady | peak after 20 points | after 40 points | after 70 points |
|---|---|---|---|---|
| 1.0 kW | 41 degC | 39.9 (68 min) | 41.0 (137 min) | 41.2 (239 min) |
| 1.5 kW | 45 degC | 41.6 (46 min) | 44.0 (91 min) | 44.9 (159 min) |
| 2.0 kW | 49 degC | 42.7 (34 min) | 46.4 (68 min) | 48.3 (119 min) |
| 2.5 kW | 53 degC | 43.5 (27 min) | 48.3 (55 min) | 51.4 (96 min) |

What can be said, and no more: (1) the temperature rise in a session is roughly proportional to charge power and the
time constant is of the order of 40 minutes, so a charge of an hour or more at 2.5 kW reaches the steady regime while a
short one does not; (2) halving the time spent at high power matters more for the peak than the energy moved; (3) the
indicated steady values are above the 40 degC where the Temperature Charge Limit starts, even at 1.0 kW, which would be
the backstop's job and is **not** a claim that 1.0 kW is unsafe (the sensor location, cells or electronics, is
unverified, and the ambient proxy is itself an assumption). The table is an indication of shape and order of magnitude,
not a prediction to the degree.

**Fallback taken, as the data is too thin.** The knee is the plan's documented conservative choice, **1.5 kW**: it is
within the envelope that has been observed (the one 1.4 kW session already reached 41 degC), it is 60% of the 2.5 kW
rating, and the replay shows what it costs. The slope (the price) is chosen from the replay (section 3), not from the
thermal fit, which cannot support a number. Re-run the script when more sessions exist, in particular a charge at 2.0 kW
or more, and revisit the knee only with that evidence:

```bash
uv run python hack/battery_thermal_study.py --kube-context <context> --cache-dir <dir outside the repo>
```

## 3. Model

`charge_soft_cap = (threshold_kw, eur_per_kwh)`. One continuous column `x[t] >= 0` per slot with the row
`x[t] >= charge[t] - threshold_kw*h` (`charge[t]` is the AC energy of the slot in kWh, `h` the slot length in hours) and
`+ cost * x[t]` in the objective: a convex, piecewise-linear price on every AC kWh charged above the threshold power in a
slot, zero below it. Properties:

- `None` or a zero cost gives the historical model byte for byte (golden digests in the tests, also with a full-charge
  model and with the dwell cost); the columns come after the dwell columns and the rows after every other row, so no
  index moves.
- It is a **planning convention** like the throughput and dwell costs, not a thermal model. The reported `cost_eur` is
  the plan's real cost (cap excluded); the cap cost is recoverable as `cost * charge_soft_cap_excess_kwh(...)`. The
  dual bound belongs to the penalised objective, so with an active cap it is dropped and the result is `feasible` /
  `upper`, as with the throughput and dwell costs.
- **Full-charge day.** The cap applies in every solve of the snapshot (the lifted and target ones too). One more stored
  kWh costs at most `1/eta_c` AC kWh above the cap, so the shortfall penalty of the checkpoint must exceed
  `dwell_cost * horizon_hours + cap_cost / eta_c`: the solver refuses the model otherwise and the service config
  refuses it for the longest horizon (with the efficiency floored at 0.5, which no real battery is below). The
  checkpoint is therefore reached whatever the cap price; the cap only spreads the climb over more slots.
- The cap composes with the dwell cost: both are additive auxiliary terms, and the dominance test adds them.
- A threshold at or above the power limit is inert (the column is bounded by zero).

## 4. Replay study

**Method.** `hack/charge_soft_cap_sweep.py`, the method of the [dwell study](high-soc-dwell-cost.md#3-study): the
unchanged benchmark with `policies=no_battery,rolling_horizon` (causal trailing-mean load, observed load, two
publications per day), one run per window and variant, throughput cost 0.01 and **the dwell cost 0.01 EUR/kWh/h above 90%
in every variant**, dataset `cdd448abb006` (RTE 0.80, window 0.20 to 0.95), the same five windows (956 days). **B** is the
reference without the cap; **C** adds the cap at threshold 1.5 or 2.0 kW and cost 0.005, 0.01, 0.02 or 0.05 EUR/kWh.
Criterion: the lowest threshold with a cost whose penalty against B stays under **1% of B's saving over no battery in
every window**, and then the largest such cost.

**Result, pooled over 956 days:**

| variant | cost vs B | penalty, % of B's saving | worst window, % of B's saving | mean kW while charging | energy above 1.5 kW | energy above 2.0 kW | throughput vs B |
|---|---|---|---|---|---|---|---|
| B (no cap) | 0 | 0 | 0 | 2.13 | 35.4% | 17.1% | 0 |
| 1.5 kW at 0.005 | +0.10% | +0.52% | 0.91% (W24) | 1.49 | 6.7% | 3.2% | -1.0% |
| 1.5 kW at 0.01 | +0.18% | +0.96% | 1.43% (S2) | 1.44 | 3.5% | 1.6% | -1.5% |
| 1.5 kW at 0.02 | +0.27% | +1.48% | 2.18% (S2) | 1.40 | 1.3% | 0.6% | -2.0% |
| 1.5 kW at 0.05 | +0.37% | +2.03% | 2.89% (S2) | 1.38 | 0.0% | 0.0% | -2.2% |
| 2.0 kW at 0.005 | +0.03% | +0.19% | 0.33% (S2) | 1.80 | 24.2% | 2.2% | -0.2% |
| 2.0 kW at 0.01 | +0.06% | +0.35% | 0.58% (S2) | 1.78 | 23.2% | 0.9% | -0.4% |
| 2.0 kW at 0.02 | +0.09% | +0.51% | 0.82% (S2) | 1.77 | 22.7% | 0.2% | -0.6% |
| 2.0 kW at 0.05 | +0.11% | +0.61% | 0.92% (S2) | 1.76 | 22.5% | 0.0% | -0.6% |

"Energy above X kW" is the share of the charged energy that sits above X kW in its slot, read on the same scale for every
variant. Per window the penalty of 1.5 kW at 0.005 is 0.91% (W24), 0.79% (S2), 0.43% (S3), 0.38% (SU25) and 0.22%
(SU26). At 0.01 the 1.5 kW cap fails three windows (1.32%, 1.43%, 1.05%) although its pooled figure is 0.96%. The slot
peak still reaches 2.5 kW now and then (a rare slot wins its price spread even at 0.05 in some windows), so the cap is a
soft one by design.

**Criterion and recommendation.** The 1.5 kW threshold passes at **0.005 EUR/kWh** (largest passing cost, worst window
0.91%), so the recommendation is **`charge_soft_cap_kw: 1.5`, `charge_soft_cap_eur_per_kwh: 0.005`**. It cuts the mean
charge power while charging from 2.13 to 1.49 kW, the energy above 1.5 kW from 35% to 7% and above 2.0 kW from 17% to 3%,
for 0.10% of cost (0.52% of B's saving) and 1% less throughput. If the owner prefers a gentler cap, **2.0 kW at 0.05**
also passes the rule (worst window 0.92%) and removes the energy above 2.0 kW entirely, but leaves 22% of the charge
between 1.5 and 2.0 kW, which is the region of the observed 41 degC session; it is not recommended on the thermal
evidence. A value between 0.005 and 0.01 for 1.5 kW was not swept.

**Hourly replan (S3, 211 days).** The same sweep with the rolling horizon re-solved every hour (4 slots) on the causal
load forecast with a 192-slot horizon, as in the dwell study (the live planner re-solves every 15 minutes; not run at that
cadence for cost of compute):

| variant | cost vs B | penalty, % of B's saving | mean kW while charging | energy above 1.5 kW | energy above 2.0 kW | throughput vs B |
|---|---|---|---|---|---|---|
| B (no cap) | 0 | 0 | 2.12 | 35.3% | 16.8% | 0 |
| 1.5 kW at 0.005 | +0.11% | +0.57% | 1.51 | 7.4% | 3.4% | -0.8% |
| 1.5 kW at 0.01 | +0.22% | +1.11% | 1.45 | 3.6% | 1.7% | -1.4% |
| 1.5 kW at 0.02 | +0.34% | +1.75% | 1.42 | 1.3% | 0.6% | -2.1% |

The picture is the same at the hourly cadence: 0.005 passes the 1% criterion (0.57%), 0.01 does not (1.11%).

**Full-charge day (22 sampled days, 2025-10-01 to 2026-04-30, every seventh day).** The setup of the dwell study (the
planner's commit model on realised prices and load, ceiling 0.95 until local midnight and 1.00 after, a checkpoint at 100%
in any slot of the day, shortfall penalty 10, taper from 95%, plan from 50% at 20:30 the evening before, the dwell cost on
in every variant). The target is reached on every day at every variant (the shortfall penalty dominates the cap).

| variant | mean kW while charging | mean slot peak, kW | extra cost, % of B's saving on these days |
|---|---|---|---|
| B (no cap) | 2.27 | 2.50 | 0 |
| 1.5 kW at 0.005 | 1.59 | 2.12 | +0.73% |
| 1.5 kW at 0.01 | 1.49 | 1.92 | +1.45% |
| 1.5 kW at 0.02 | 1.46 | 1.74 | +1.75% |
| 2.0 kW at 0.005 | 1.89 | 2.26 | +0.25% |

On the full-charge day (one day in 5 to 15) the cap costs about 0.7% of that day's saving at the recommended value, on top
of the dwell cost, i.e. about 0.05 to 0.15% of B's saving averaged over a season.

## 5. Limits

- The thermal fit rests on 21 hours and two charge sessions; the cap's knee (1.5 kW) is a documented conservative choice,
  not a fitted optimum, and the price is a replay choice. No ageing rate or lifespan effect was measured or claimed.
- The replay is the benchmark's rolling horizon on the observed load and the dataset's SOC rules, not the live planner on
  live data (an hourly re-solve is checked on one season); windows are replayed separately.
- The sensor may measure electronics, not cells; the cap is a soft convex price, never a hard limit, and Omnibattery's
  Temperature Charge Limit is the backstop (owner setting): switched on at its default values on 2026-11-04 14:10:56 UTC, after
  this study; the soft cap and the derate barely overlap and the thermal fit of section 2 is superseded, see
  [derate-aware planning](thermal-derate-aware-planning.md) and [battery-thermal](../components/battery-thermal.md).
- A very long, flat valley makes the cap nearly free; a short valley with a steep spike makes it cost more. The study
  averages over 956 days of the real tariff.

## 6. Reproduce

```bash
uv run python hack/charge_soft_cap_sweep.py --dataset <store>/analysis/v1/<dataset_id> \
  --data-dir <store> --run-id <fresh-name> --workers 16 --full-charge
uv run python hack/charge_soft_cap_sweep.py --dataset ... --run-id <fresh> --windows S3:2025-10-01:2026-05-01 \
  --thresholds 1.5 --costs 0.005,0.01,0.02 --replan-slots 4
```

Outputs are under `<store>/analysis/v1/charge-soft-cap-sweep/<run-id>/` (private euro figures stay in the benchmark
runs; `table.md` holds relative figures only).
