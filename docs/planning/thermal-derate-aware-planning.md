# Derate-aware planning: Omnibattery's Temperature Charge Limit in the planner

- **Status:** study and design. **PR 2 is implemented** as a read-only shadow (`planning.thermal.mode: shadow`,
  default `off`; see [service.md](../service.md#thermal-shadow-derate-prediction)): inputs, thermal state with a
  guarded refit, per-plan prediction, metrics. Nothing else is: the planner, the solver and the actuator do not use
  it, and no allowlist is widened. The study is `hack/battery_thermal_derate.py` (with
  `tests/test_battery_thermal_derate.py`; its model and fit now live in `service/thermal.py`). Each step of the
  [PR breakdown](#7-pr-breakdown) is its own PR.
- **Date:** 2026-11-04
- **Related:** [battery longevity](battery-longevity.md) (step 6, the thermal policy), the [thermal charge soft
  cap](thermal-charge-soft-cap.md) (implemented, off by default; its thermal fit is superseded by section 2), the
  [high-SOC dwell cost](high-soc-dwell-cost.md), the [periodic full charge](periodic-full-charge.md),
  [ADR-0007](../adr/0007-live-battery-control.md), the component page [battery-thermal](../components/battery-thermal.md).
- **Code today:** `service/battery_health.py` already reads the internal temperature (read-only) and counts the seconds
  above 35 and 40 degC; `oracle.solve_oracle` has the SOC taper rows and the soft cap but **no per-slot charge power
  bound**; `service/planner.PlanningSnapshot` carries the soft cap and dwell parameters.
- **Privacy:** temperatures, powers, curve parameters and relative figures only; no energy totals, costs, addresses.

## 1. Why

The Marstek Venus E V3 exposes one temperature to Omnibattery, `sensor.venus_internal_temperature` (register 35000;
the V3 driver has no cell or MOSFET temperature). On 2026-11-04 it rose from 32.6 degC (idle) to 52.4 degC during a charge
of about 75 minutes at 1.8 to 2.5 kW (12:48 to 14:02 UTC), and still read about 50 degC at 1.5 kW afterwards. At 14:10:56
UTC the owner switched on Omnibattery's **Temperature Charge Limit** (`switch.omnibattery_temp_charge_limit`, 40 degC,
10 degC band, 40% floor, discharge limiting off). Twenty seconds later Omnibattery wrote 1000 W to the Venus (2500 W x 40%)
where it had been writing 1500 W: **the physical charge power the planner asks for is now a function of a temperature the
planner does not model**, and the owner can switch the limit on and off at will.

A derate-blind plan is not wrong, only optimistic: it assumes 2.5 kW is available in any slot, the battery delivers less
once hot, the SOC falls behind the plan and the next 15-minute replan corrects it. The cost is plan inconsistency (the
valley is used less fully than planned, the cheapest slots are not chosen early enough) and a wrong picture of how long a
full charge takes. How large that is depends on how often the planner asks for sustained high power, which the soft cap
already discourages (section 5). The replay of section 8 decides whether the change is worth shipping at all.

## 2. Evidence (2026-11-04, `hack/battery_thermal_derate.py`)

> **Correction (PR 2).** PR #91 printed the two gains the wrong way round. The study script negates
> `sensor.venus_ac_power` (negative while charging) so that its power is positive while charging, but its regressors
> took the *negative* part of that power as the charge: the coefficient it called `k_c` multiplies the **discharge**
> power and the one it called `k_d` the **charge** power. A synthetic check with known gains reproduces the swap
> (`tests/test_battery_thermal_derate.py`), and the fitted 11.0 degC/kW reproduces the 2.5 kW session
> (32.6 + 11.0 x 2.2 kW x (1 - exp(-75/44)) = 52.4 degC) which 6.9 cannot (46.7). The shared model in
> `service/thermal.py` is now explicit (positive while charging, `k_c` on the charge power) and this page, the
> [component page](../components/battery-thermal.md) and the service prior use the physical labels: **charge gain
> `k_c` = 11.0 degC/kW** (bootstrap 8.7 to 11.7, leave-one-episode-out 9.7 to 11.6) and **discharge gain `k_d` = 6.9
> degC/kW** (3.6 to 9.5; 3.1 to 8.2). The coefficients, `tau`, `T_amb` and the held-out scores do not change;
> everything *derived from the labelled `k_c`* did and is recomputed below with the corrected charge gain from the
> reported coefficients (the recorder is not re-read in this PR). Statements below about which gain "collapses" or
> is "identifiable" were read with the old labels and are to be re-read after the next run of the script.

**Data.** Home Assistant recorder, read only (events, not 5-minute windows), from the first AC energy (2026-11-03 19:14:30
UTC) to 14:24 UTC: 19.2 hours, five episodes (the timeline split at the midpoints of idle gaps over 60 min): commissioning
test at 0.8 kW, a 0.3 kW discharge, **one charge at 1.4 kW mean (peak 1.9 kW, 31.9 to 42.0 degC)**, a day of 0.2 to 0.8 kW
discharge, and **the 2.5 kW charge (32.6 to 52.4 degC)**. Only two charge sessions exist above 1 kW. Data before AC was
energised is excluded (a cold start: 25 degC rising at zero power).

**Model.** `dT/dt = (T_amb + k_c P_charge + k_d P_discharge - T) / tau`, fitted by least squares on the multi-step
prediction error (horizons 10 to 90 min, origins every 2 min, 2729 rows), linear in `(T_amb, k_c, k_d)` for a given `tau`,
so `tau` is profiled on a grid. Uncertainty: leave-one-episode-out (jackknife) ranges and a moving-block bootstrap
(2 h blocks, 400 draws) at the best `tau`. The fit moves with every refresh (five more minutes of data took `tau`
from 48 to 44 min and `k_d` from 7.4 to 6.9): the profile of `tau` is flat between 40 and 52 min.

| parameter | fit | bootstrap 5 to 95% | leave-one-episode-out range |
|---|---|---|---|
| `tau` | 44 min | 40 to 52 (rmse within 5% of best) | 44 to 52 |
| `T_amb` (idle level) | 32.6 degC | 32.2 to 33.0 | 32.3 to 32.9 |
| `k_c` (charge) | 11.0 degC/kW | 8.7 to 11.7 | 9.7 to 11.6 |
| `k_d` (discharge) | 6.9 degC/kW | 3.6 to 9.5 | **3.1 to 8.2** |

**Validation (honest).** Prediction `h` minutes ahead from the measured temperature at the origin and the measured power
in between, held-out episode never in the fit, against persistence (hold the origin temperature):

| horizon, min | 10 | 20 | 30 | 45 | 60 | 90 |
|---|---|---|---|---|---|---|
| leave-one-episode-out rmse, degC | 0.44 | 0.75 | 0.98 | 1.25 | 1.43 | 1.66 |
| persistence rmse, degC | 0.97 | 1.80 | 2.55 | 3.56 | 4.35 | 5.22 |

Pooled over horizons: 1.11 against 3.21 degC (walk-forward, fit on the past only, the deployment case: 1.02 against
3.30). Where the mean power over the horizon exceeds 1 kW: 2.53 against 8.45 degC with a **bias of -1.06 degC** (the model
runs cold). The decisive failure is the high-power episode: with the 2.5 kW charge held out (parameters from episodes that
never exceeded 1.9 kW) the free simulation of the session peaks at 46.6 degC against 52.4 observed, `k_c` collapses to
3.1, and the rmse is 2.10 (persistence 7.07). So **the fit is useful, but `k_c` at 2 to 2.5 kW rests on one session and is
in-sample**. Further findings:

- A single gain for both directions fits as well out of sample (leave-one-out 1.05, walk-forward 1.41): separate charge and
  discharge gains are not resolved; `k_d` (the discharge gain) rests on discharges of at most 0.82 kW and **must not be
  extrapolated to 2.5 kW** (it would predict 49.9 degC at steady state, which nothing supports).
- A quadratic charge term has a negative coefficient (heating sub-linear at the top, unphysical for resistive losses) and
  no out-of-sample gain: dropped.
- Ambient: the idle plateau sits between 31.7 and 33.0 degC over the 19 hours. A daily harmonic on the ambient (amplitude
  about 0.9 degC in sample) **does not survive validation** (leave-one-out 1.17, walk-forward 2.74, against 1.11 and 1.02
  without it): one day cannot identify a daily cycle. A per-episode ambient (an oracle, not a forecast) halves the error
  (1.11 to 0.63), so the level drift of the idle plateau is the largest remaining error; re-estimating `T_amb` from the last
  3 hours online recovers about a third of it (0.97).
- Structure: the cool-down has a fast part (about 45 min) and a slow one (hours; the earlier study saw 90 min and more), so
  the sensor is not one first-order node. After the 2.5 kW session the temperature kept rising for a few minutes while the
  power fell from 2.5 to 1.5 kW. A second, slow state would be the next model, and the data cannot yet support it.
- The earlier study's one-session fit (`tau` 38 min, `k_c` 7.8, `T_amb` 33.4) scores 1.00 on the same rows, within noise of
  the new fit (it saw the 1.4 kW session): the two fits agree on the order of magnitude and disagree on `tau` by 6 to 10 min.

**Derate curve check.** Predicted cap = `min(system cap, Venus cap) x factor(T)`, with factor 1 up to 40 degC, linear to
0.4 at 50 degC, flat below (limit 40, band 10, floor 40%, caps 2500 W, read from the recorder), against
`number.venus_set_charge_power` (the power Omnibattery writes) and the measured AC power, from 14:11:26 to 14:24 UTC (13.5
minutes, 25 grid points):

| quantity | result |
|---|---|
| temperature during the window | 48.3 to 50.2 degC |
| predicted factor and cap | 0.40 to 0.50, 1000 to 1255 W |
| written set power | 1000 to 1255 W: **within 15 W of the predicted cap at every point** (share within 30 W: 100%, above cap plus 30 W: 0%) |
| measured AC over written set | 0.996 (median) |
| reaction time | the first write (1000 W) 20.7 s after the switch turned on |
| the 10 minutes before the switch | 1385 to 1605 W written at 50.4 to 52.4 degC; the curve would have capped them at 1000 W |

The floor (T at or above 50 degC gives exactly 1000 W) and the linear part between 48.3 and 50.2 degC (the cap rose from 1000
to 1255 W as the battery cooled, matching `2500 x (1 - 0.6 (T - 40) / 10)` to 15 W) are confirmed. **Not checked:** the knee
at 40 degC and the 40 to 48 degC part of the ramp, hysteresis (the curve is documented as continuous with none), the driver
minimum, the discharge side (switch off), other limit values. The check covers 13.5 minutes of one regime; it will be
re-run as the battery cools through the band (the script prints it on every run).

**What follows physically** (model above with the corrected charge gain `k_c` 11.0, `tau` 44; in brackets the bootstrap ends
8.7 and 11.7; recomputed with `service/thermal.py`, replacing the figures PR #91 derived from the discharge gain): from an
idle start a charge at 2.5 kW reaches the 40 degC knee after 14 min (18 at 8.7, 13 at 11.7) and the steady state `T_amb +
k_c P` of 60 degC, far into the band; at 2.0 kW the knee comes after 18 min (steady 55), at 1.5 kW after 26 min (steady 49).
The derate is itself a feedback: the thermal steady state meets the curve at **about 1.4 kW and 48 degC** (1.6 kW and 46
degC at 8.7; 1.3 kW and 48 degC at 11.7). A charge held at the maximum request for three hours from idle delivers 62% of the
unlimited energy at 2.5 kW (70% at 8.7, 60% at 11.7), 76% at 2.0 kW (86%, 74%), and **96% at 1.5 kW** (100%, 93%): a
request of 1.5 kW is limited only after about 26 minutes, so **the soft cap at 1.5 kW and the derate overlap more than PR
#91 concluded** (it found them barely overlapping), and the derate is worth more than it assumed. One hour at 2.5 kW
delivers 1.92 kWh instead of 2.5 (1.87 at 11.7). The discharge gain 6.9 must not be extrapolated either: 2.5 kW of
discharge would settle at 49.9 degC, which nothing supports.

**What data is needed, and how long to wait.** The verdict today is *usable for `tau`, `T_amb` and the order of magnitude of
`k_c`; too thin for `k_c` at 2 to 2.5 kW and for `k_d`*. Needed: at least 10 episodes, with at least 4 charges above 1.8 kW
that start from an idle temperature (3 hours after the last charge) and at least 8 full cool-downs (3 hours or more), over at
least two weeks and ideally including a different weather. That is about **14 days of normal operation**. The catch is that
the soft cap and the derate both suppress exactly the high-power sessions the fit needs: if the soft cap is enabled before
that data exists, `k_c` above 1.5 kW never becomes identifiable. Either collect the data first (leave the soft cap off for
the 14 days, as it is today) or run **one owner-approved calibration charge** of 60 minutes at a fixed 2.5 kW from an idle
start (only the 25 minutes below 40 degC are unaffected by the derate), a handful of cents of price difference, outside this
repository's write path (the owner sets it in Omnibattery). Re-run the script weekly:

```bash
uv run python hack/battery_thermal_derate.py --kube-context <context> --cache-dir <dir outside the repo>
```

## 3. Design: inputs and the thermal state

**Read-only inputs, never written.** Entities (configured under a new `planning.thermal.entities`, all optional; a role
that is not configured disables the feature): the temperature sensor (the one `battery_health.entities.temperature` already
reads, one entity store entry), `switch.omnibattery_temp_charge_limit`, `number.omnibattery_temp_charge_limit_c`,
`..._band_c`, `..._floor_pct`, `switch.omnibattery_temp_charge_limit_discharge`, and the two cap numbers the actuator
already knows (`number.omnibattery_system_max_charge_power`, `number.venus_max_charge_power`). They are state subscriptions
of the existing HA session: **no service call is added, `ActuationAllowlist` is unchanged**, and no value is ever written to
these entities (the planner does not choose the owner's derate setting; a planner that does would be a new write path,
which [AGENTS.md](../../AGENTS.md) forbids).

**Semantics, per solve.** The snapshot freezes `ThermalInputs(enabled, limit_c, band_c, floor_pct, discharge_enabled,
limit_kw, min_kw, temperature_c, temperature_age_s)`.

| situation | model |
|---|---|
| switch off | **no derate in the model**; the plan is the blind plan, byte for byte |
| switch on, numbers valid (`band_c > 0`, `0 <= floor_pct <= 100`, `limit_c` within 25 to 60) | the curve below |
| switch or a number `unavailable` / `unknown` / out of range | **blind**, with a metric and a log line: assuming a derate that is not there only under-uses the battery, assuming none when there is one is what happens today and is corrected physically; the safe direction is the status quo |
| temperature unavailable or older than 10 min | the derate is predicted from the thermal model alone starting at `T_amb` (flagged `cold_start`); no cap above the knee is claimed |
| discharge switch on | the same curve caps the discharge power, **only if the fit has supported discharge data** (section 4); otherwise the discharge side stays blind |

The curve is Omnibattery's documented one: `cap_kw = max(limit_kw x f(T), min_kw)` capped at `limit_kw`, with `f = 1` at
`T <= limit_c`, `f = floor` at `T >= limit_c + band_c`, linear between, continuous, no hysteresis; `limit_kw` is the
smaller of the two cap numbers and `min_kw` the driver minimum (800 W; the 40% floor of 2500 W is 1000 W, above it). A change
of the switch, a number or the discharge switch **triggers a replan** (`trigger=thermal_config`) and joins `inputs_hash`
only when the feature is active, so a deployment without it keeps every hash.

**Thermal state.** One first-order node, exactly the model of section 2: parameters `(tau, T_amb, k_c, k_d)`. Priors are the
committed values of the component page, so the service starts without a fit. Per slot with constant AC power the exact
discretisation is `T_end = a T_start + (1 - a)(T_amb + k_c P_c + k_d P_d)`, `a = exp(-h / tau)`.

**Online refit.** A rolling buffer of the temperature and AC power (the observations `battery_health` already sees; 1-minute
grid, 21 days, kept in the history sink, never leaves the data directory). A **daily refit** at a quiet hour and at start-up,
with the algorithm of the script (multi-step least squares, `tau` profiled), in a pure `service/thermal.py`. Guards, because
the fit is data-starved: ridge towards the prior with a weight that fades with the number of effective rows; bounds
(`tau` 20 to 120 min, `k_c` 2 to 15, `T_amb` 20 to 40 degC); a gain is refitted only if its direction has at least 10 rows with
a mean power above 1 kW from at least 3 episodes, otherwise it keeps the prior (this is `k_c` above 1.8 kW and `k_d` today);
a refit is **accepted only if its walk-forward 60-minute rmse beats the current parameters on the last 7 days** and its
leave-one-episode-out skill against persistence is at least 0.5; otherwise the previous parameters stay and the model status
is `degraded`. Status values: `prior`, `fitted`, `degraded`, exported with the parameters and the fit date. An **online
ambient** estimate (`T_amb` from the last 3 hours with the gains fixed) is a second step, enabled only if the replay
confirms the gain seen offline (rmse 1.12 to 0.97).

## 4. Design: the plan

**Per-slot temperature prediction.** From the measured temperature at the decision time and the plan's own charge and
discharge power per slot, the exact recursion above gives `T_start[t]`, `T_end[t]` for every slot of the horizon (192 slots,
vectorised, microseconds). Inside a slot with constant power the temperature is monotone, so `max(T_start, T_end)` is the
slot's worst case.

**Solver change (PR 3).** A per-slot upper bound on the charge column and the discharge column, `charge_limit_kw[t]`
(and `discharge_limit_kw[t]`), `None` meaning the spec limit, byte for byte the historical model when absent (golden digests,
as for the soft cap and the dwell cost). It composes with the full-charge taper rows (both are upper bounds, the tighter
wins) and with the import-limit row. Nothing else in the MILP changes.

**Fixed-point solve (PR 4).**

```
caps = [limit_kw] * n                     # iteration 0 is the derate-blind plan (kept for the comparison)
plan_0 = solve(caps)
for it in 1 .. K (K = 3):
    T = simulate(T_now, plan_{it-1}.charge / h, plan_{it-1}.discharge / h)
    new[t] = min(limit_kw, max(limit_kw * f(max(T_start[t], T_end[t])), min_kw))
    if max_t |new[t] - caps[t]| <= 50 W: converged, stop
    caps = new if it == 1 else 0.5 * (caps + new)             # damping from the second step
    plan_it = solve(caps)
final = best of the iterates by (max_t violation[t], cost), violation = plan.charge_kw[t] - new[t]
```

If no slot of the blind plan has a predicted temperature above the knee, `new = caps = limit_kw` and the loop stops after
the first simulation: **the common case costs one solve and one cheap simulation**. The iteration count is at most 1 blind
plus 3 aware solves, each under the existing `solver_time_limit_s`; the aware plan is discarded for the blind plan if the
cycle's time budget is exceeded.

*Convergence and oscillation.* The map `P to T to cap` is monotone decreasing (more power, hotter, lower cap) but the solver's
response to a cap is not: lowering the cap of one slot moves charge into neighbouring slots, which heats them. Cycles are
therefore possible. Guards: damping, the iteration limit, keeping the iterate with the smallest violation, a flag
`oscillating` (the sign of the cap change flips in at least two slots between iterations), and the residual `violation_w` is
recorded in the plan. The remaining optimism is bounded and short-lived: the physical derate clips the realised power
anyway and the next replan starts from the measured temperature.

*Alternative worth a replay arm (PR 7, not the first choice).* Because `T_end[t]` is linear in the charge and discharge
variables of the earlier slots, the curve inside the band is a **linear row** `charge[t] <= h limit_kw (1 - (1 - floor)
(T_end[t] - limit_c) / band_c)` with a dense lower-triangular part (about 18 000 non-zeros for 192 slots). That solves the
coupled problem exactly in one MILP and gives the solver foresight (do not heat before a later high-power slot), with no
iteration. Against it: the row is stricter than the real curve above `limit_c + band_c` (the real cap stops at the floor,
the linear one keeps falling) which is acceptable because no plan should live there; the model must stay linear (it does:
one node); and the full-charge MILP is already the slow solve. The fixed point ships first because it needs only the
per-slot bound, degrades to the blind plan, and works for any future non-linear thermal model.

**Interaction with the existing terms.**

- *Charge soft cap* (1.5 kW at 0.005 EUR/kWh, off by default): **keep, unchanged, as the preference for staying cool**; the
  derate is a feasibility limit, the soft cap a price on heat exposure, and the two do not double count. They barely overlap
  (section 2): the derate cap at 1.5 kW needs 46 degC. The replay (section 8) compares four arms (blind, aware, blind with the
  cap, aware with the cap and a 2.0 kW knee) and decides whether the knee can rise once the plan knows the derate. It is
  **not dropped** on this evidence: the derate protects the battery from the owner's chosen 40 to 50 degC band, the soft cap
  keeps it below the band, and only the second reduces time at 35 to 45 degC.
- *High-SOC dwell cost:* independent. A slower charge near the top lengthens the time above 90%; the per-slot bounds enter the
  same MILP, so the dwell cost and the derate bounds trade off inside one solve with no extra code.
- *Full-charge day:* the checkpoint must stay reachable. The taper (200 W above a latch) already dominates the last points; a
  derate-limited climb from 20% to 100% can lengthen the lifted day. The shortfall slack keeps the MILP feasible and the
  scheduler's cost estimate (`forcing_cost`) must be computed with the same caps, so the decision does not underestimate a
  hot full charge. A test with a hot start is part of PR 4.
- *Import limit:* untouched; the derate only lowers the power the battery can draw.
- *Discharge:* the symmetric bound exists in the solver and the iteration, but stays inactive (blind) while the discharge
  switch is off or the discharge gain is `prior` (section 3).

## 5. Observability

In the plan record (`service/v1/plans`, no new store), a `thermal` block per plan and per slot:

- per slot: `temp_start_c`, `temp_end_c`, `derate_factor`, `charge_cap_kw`, `charge_kw` (achievable, after the caps);
- per plan: `mode`, the derate state (`off`, `on`, `unknown`), `limit_c`, `band_c`, `floor_pct`, the model (`status`, `tau_min`,
  `t_amb_c`, `k_c`, `k_d`, `fitted_at`, rows), `iterations`, `converged`, `oscillating`, `max_violation_w`, and in shadow mode
  the **blind-versus-aware comparison** (cost difference, kWh of planned charge above the achievable power, first
  differing intent).

Metrics (Prometheus, same exporter; names proposed): `battery_optimizer_thermal_state{state}`, `..._thermal_model_status`,
`..._thermal_parameter{name}`, `..._thermal_predicted_temperature_celsius{horizon_min}`, `..._thermal_derate_factor{when}`
(now and predicted for the next slot), `..._thermal_fixed_point_iterations`, `..._thermal_oscillating_total`,
`..._thermal_forecast_error_celsius{horizon_min}` and the observed `..._temp_limit_enabled`.

**KPIs** (also on the component page):

1. *Thermal-model skill*: with the **measured** power as input, the rmse of the prediction 15, 30, 60 and 120 minutes ahead
   against persistence, rolling 14 days, plus the bias where the mean power exceeds 1 kW. Today (leave-one-out, section 2):
   60 min 1.43 against 4.35 degC, bias -1.06 degC above 1 kW. Target: 60-min rmse at most 1.5 degC, MSE skill at least 0.6,
   `|bias|` at most 0.5 degC above 1 kW.
2. *Plan temperature error*: planned against realised temperature 60 minutes ahead, stratified by planned power (mixes the
   thermal error with the realised-power error; reported separately from 1).
3. *Derate curve conformance*: share of derate-active minutes where the written set power is within 30 W of the predicted cap
   (today 100% over 13.5 minutes); alerts if it falls, because an Omnibattery upgrade or a changed number moved the curve.
4. *Achievable-power gap*: planned charge minus realised charge in slots with a derate factor below 1 (blind versus aware).
5. *Exposure*: hours per week above 35, 40 and 45 degC (the existing `battery_health` counters, one more threshold).

## 6. Evaluation: replay and benchmark

The benchmark's `rolling_horizon` gains a thermal plant (PR 5): the first-order node with the fitted parameters and the
curve act on the *executed* charge of **both** arms, so a derate-blind plan is penalised the way the real battery penalises
it (the realised charge is `min(planned, cap(T))`, the SOC follows the realised charge, the next replan starts from the
simulated temperature). Arms on the dataset and the five windows of the soft-cap study (956 days, observed load, causal
trailing-mean load, two publications per day, hourly replan on one window): **B** blind; **A** aware (fixed point);
**B+C** blind with the 1.5 kW soft cap; **A+C** aware with the cap (knees 1.5 and 2.0 kW). Reported as relative figures:
cost against B in percent of B's saving, kWh not delivered against the plan, replans that changed the current intent, hours
above 40 and 45 degC, the solve-time ratio, the convergence rate.

Pass criteria for enabling A: a cost no worse than B in every window under a plant with **perturbed parameters** (`k_c` and
`tau` by plus and minus 30%, `T_amb` by plus and minus 2 degC, the bootstrap region), not only at the fitted ones (otherwise
the replay rewards a model that is exactly right by construction); the achievable-power gap at least 30% smaller in
derate-active slots; convergence in at most 3 iterations in at least 95% of solves; the solve time within 3x the blind
median. **Expected size:** small. The derate binds only after about 25 minutes at 2.5 kW, and the soft cap makes that rare;
if A gains less than 0.1% of B's saving, the outcome is stage 1 only (inputs, thermal state, metrics, KPIs) and the soft
cap stays the thermal lever. The honest limits: the replay plant uses the same model class as the planner; one constant
ambient (no seasonal data; one day today); the replay has no owner toggling.

## 7. Rollout and PR breakdown

Rollout is a config flag `planning.thermal.mode: off | shadow | active` (default `off`, which keeps every plan, hash and
metric as today). **Shadow comparison first**: in `shadow` the service reads the inputs, keeps the thermal state, publishes
the metrics and, each cycle, solves the aware plan beside the blind plan and **records both; the blind plan still drives
the intents**. `active` switches the intents to the aware plan. Gate from `shadow` to `active`: at least 14 days, the
thermal-model KPI met, the derate conformance KPI at least 95%, at least 10 derate-active slots seen (otherwise the aware plan
was never exercised and the comparison says nothing), no more than 5% non-converged or oscillating cycles, and the replay
criteria of section 6. Rollback is the flag; the switch and numbers are never touched.

| # | PR | Content | Depends on |
|---|---|---|---|
| 1 | `docs(planning): Derate-aware planning study` | **this PR**: study script and tests, this design, the [battery-thermal](../components/battery-thermal.md) page, regime-log entry | none |
| 2 | `feat(service): Read-only thermal inputs, thermal state and refit` | **implemented** (see below): `planning.thermal` config, entity subscriptions, `service/thermal.py` (curve, recursion, refit with guards), metrics, status API, per-slot `thermal` block for the **blind** plan; mode `off` or `shadow` records only | 1 |
| 3 | `feat(oracle): Per-slot charge and discharge power bounds` | `charge_limit_kw[t]`, `discharge_limit_kw[t]` in `build_model` / `solve_oracle`, byte-identical when absent, golden digests, composition with taper, soft cap, dwell | none |
| 4 | `feat(planner): Derate-aware fixed-point solve, shadow comparison` | the loop of section 4 with its guards, `mode: shadow` solves both and records the comparison, full-charge hot-start test | 2, 3 |
| 5 | `feat(benchmark): Thermal plant and derate in the replay, with its study` | thermal plant in `rolling_horizon`, `hack/thermal_derate_sweep.py`, the arms of section 6, the result and the soft-cap knee decision in this document | 3 |
| 6 | `feat(planner): Aware plan drives the intents` | `mode: active`; deploy-config change recorded in the regime log; component pages recalibrated | 4, 5, the shadow gate |
| 7 | `feat(oracle): Linear derate rows (experiment)` | the joint formulation of section 4 as a replay arm only | 3, 5 |

**PR 2 as implemented**, against sections 3 and 5: `planning.thermal` is `mode: off | shadow` (no `active` yet) with the
entities of section 3 (the two cap numbers and the discharge switch optional, a missing required role refused at
start-up with the mode on). The refit is the algorithm of section 3 with these specifics: a ridge towards the prior of
500 pseudo-rows fading as the rows grow; a gain refitted only with at least 10 minutes above 1 kW from at least three
episodes; acceptance by **leave-one-episode-out** 60 minute error (skill against persistence at least 0.5, and better than
the current parameters on the held-out rows of the last 7 days) in place of a walk-forward fit, because a walk-forward
fit needs a long history before it can judge anything and the held-out comparison is the same question on the data
that exists; data without thermal excitation (60 minute persistence error under 1 degC) is not judged. The shadow
prediction simulates the node through each plan with the derate as a feedback at one-minute substeps and records
the per-slot columns as a `thermal` key of the plan file (no sibling record; the key is absent when off). Scoring at
slot close covers the end temperature at three leads and the achievable charge power in derate slots. The online
ambient estimate, the plan temperature error stratified by power, the derate-conformance KPI (it needs the written
set power, not read yet) and the exposure threshold at 45 degC are not part of PR 2.

PRs 2 and 3 are independent and can proceed in parallel. PR 1 changes no service code and PRs 2 to 4 change no plan while
`mode` is `off`. The first deployment decision is the owner's, as for the soft cap and the dwell cost.

## 8. Limits and open questions

- One day of data; `k_c` at 2 to 2.5 kW from one session; `k_d` from at most 0.82 kW; the ambient from 19 hours. The refit
  guards exist because of this, and the 14-day wait of section 2 is the cheapest remedy.
- The sensor is Venus-internal temperature, not a cell temperature; nothing here claims an ageing rate or a lifespan effect.
- The first-order node is the model the planner needs, not the physics; a second slow state is the next candidate if the
  refit data show the fast and slow cool-down separately.
- The derate curve was checked over 48.3 to 50.2 degC only; a conformance KPI and a re-run of the script as the battery
  cools through the band cover the rest. Omnibattery upgrades can change the curve (invalidation trigger of the component
  page).
- The owner can toggle the switch and change the numbers at any time; every such change is a regime change recorded in the
  [regime log](../components/regime-log.md), and the planner handles it per solve rather than by assumption.
