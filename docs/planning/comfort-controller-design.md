# Comfort controller design (homelab AppDaemon)

- **Status:** Design agreed with the owner on 2026-10-31; owner answers to the open items recorded 2026-10-31 (section 14); **not yet implemented**. No physical control is authorised by this document.
- **Date:** 2026-10-31
- **Decisions:** [ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md) and its "Amendment 2026-10-31" (this design amends decisions 3 and 4 of the ADR).
- **Parent plans:** [Climate optimizer implementation](climate-optimizer-implementation.md) (section 6 work list), [C7-F field trial](climate-flow-calibration-optimization.md)
- **Facts about the unit:** homelab `docs/user-guide/heat-pump-water-temperature.md` (modes, commands, safety notes); heater facts in [heat-pump-controller](../components/heat-pump-controller.md#heater-facts-import-limit-inputs).
- **Owner of the code:** `pando85/homelab` (AppDaemon and HA configuration). This repository holds the design, the evaluation and the shadow MPC (track B), and writes nothing to the plant.

## 1. Purpose, scope and rollout gate

The comfort controller is a new homelab AppDaemon app that **replaces the legacy cheapest-hours
`climate_control.py`**. It meets a comfort schedule with the fewest kWh and ignores price for space
heating. Hot water stays price-optimised (section 7).

**Rollout gate.** The implementation goes on a homelab branch. It is **merged and tested only once the
house battery is working** (installation expected the week of 2026-11-02). Until then the legacy app keeps
running unchanged. The reason is evaluation and safety: the import-limit cap (section 9) and the
heat-pump metering basis only matter once the battery runs, and the regime change should not be mixed
with the battery commissioning.

Out of scope: any battery actuation, cooling, and any change to the unit's internal protections.

**Parked (owner, 2026-11-05).** The controller (homelab draft PRs #4699 and #4746) is parked until the battery runs
optimally, that is, until the discharge shortfall is solved
([known issues](../known-issues.md#planned-discharge-is-not-delivered-planner-bound-enabled-since-0140-monitoring)).

**Go-live gates:**

- an owner-chosen date no earlier than 2026-11-10 and after the first live full charge;
- the owner confirms a "valid heating day";
- the legacy automation off and the 9 scheduler schedules off;
- the Comfort view (`apps/home-assistant/lovelace/dashboard-comfort.json` in homelab) created in HA on go-live day
  only, with a Lovelace backup first.

## 2. Run logic

AppDaemon decides whether heat-pump heating is on or off. It has to, because the pump's zone sensor is the
water temperature (verified 2026-10-30), so the room thermostats cannot call for heat; they only open and
close the floor valves.

| Transition | Condition |
|---|---|
| **On** | any thermostat room is below its setpoint minus a hysteresis |
| **Off** | every room is at its setpoint |

There is no separate optimum-start condition: optimum start works by moving the setpoint earlier (section 6), because a thermostat whose room is above its setpoint keeps its floor valve closed, so switching the heat pump on early would deliver no heat to that room.

Machine limits (in code, not helpers): compressor **minimum on 30 min, minimum off 20 min**; a
**starts-per-day alarm** (a notification, not a control action). AppDaemon uses 20 min as written;
the track-B MPC plans on 15-minute slots and rounds the minimum off up to 2 slots (30 min).

The on/off action is the existing, proven command path (operating mode `Heat+DHW` / `DHW` through
`input_select.heishamon_heatmode`), as in the C0 fixes of 2026-10-30.

## 3. Comfort schedule and setpoints

Minimums, as in ADR-0006 decision 2:

- hall/kitchen at least 23.5 degC from **12:00 on weekdays and from 09:00 at weekends**;
- every room at least 21 degC at all times.

Setpoints carry a **+0.5 K margin** over the minimum:

| Room (thermostat entity) | Setpoint in the day window | Setpoint otherwise |
|---|---|---|
| Hall/kitchen (`climate.thermostat_hall_kitchen`) | 24.0 | 21.5 |
| Bedroom (`climate.thermostat_bedroom`) | 21.5 | 21.5 |
| Office A (`climate.thermostat_office_a`) | 21.5 | 21.5 |
| Office T (`climate.thermostat_office_t`) | 21.5 | 21.5 |

The **day window** starts at the weekday or weekend start above and ends at **22:00** (owner-confirmed
2026-10-31; no longer an assumption). The end time stays editable as a helper.

**Per room (2026-11-05).** The floor and the day minimum are helpers per room, `comfort_floor_<room>`
(default 21.0 everywhere) and `comfort_day_minimum_<room>` (default 23.5 hall/kitchen, 21.0 elsewhere); the
effective day minimum of a room is `max(day minimum, floor)` and the window stays shared. With the defaults the
schedule above is unchanged. The evaluation criteria follow per room, against the values in effect at each slot
([preregistration amendment of 2026-11-05](climate-preregistration-2026-27.md#4-amendments)).

**Owner's starting values (go-live).** Set from the Comfort view at go-live, not code defaults (the code defaults
stay 21 / 23.5). Based on 14-day measured temperatures, about 0.5 to 1 K below the current ones.

| Room | floor degC | day minimum degC |
|---|---|---|
| hall_kitchen | 22 | 23.5 |
| bedroom | 22 | 22 |
| office_t | 22 | 23 |
| office_a | 21 | 21 |

## 4. Thermostat semantics

- **The setpoint is the room's current target.** AppDaemon writes setpoints only **at schedule
  boundaries** (the early-start write of section 6, window end) and for away mode.
- **A manual change becomes the target until the next boundary.** A manual change made before the early-start write holds until that early-start boundary, which overwrites it.
  The same applies to every other boundary. If a manual setpoint is below the 21 degC
  floor, that is the owner's explicit choice and the controller does not fight it.
- **Every manual override is logged** (room, time, old value, new value). Repeated patterns produce
  **suggested** schedule changes (a notification or a report), **never silent changes**.
- **The thermostats must not run their own device programs**, or the setpoint would change behind the
  controller's back. The programs are **disabled** (owner-confirmed 2026-10-31).

## 5. Away mode

One HA switch sets every room to 21.5 degC. When away is switched off, AppDaemon writes the schedule
setpoints that apply at that moment, including the day-window setpoint if the early-start time of the next
window has already passed. The compressor limits stay in force.

## 6. Optimum start

**Optimum start is the schedule boundary moved earlier.** AppDaemon writes the day-window setpoint of the
hall/kitchen at **(window start minus lead time)** instead of at the window start. The thermostat then opens
its valve because the room is below the new setpoint, the run logic of section 2 turns the heat pump on, and
the room reaches the minimum at the window start. The run logic stays purely demand-driven. The lead time
is a table by outdoor-temperature band, **editable as HA helpers**.

The defaults come from the warm-up analysis (track B, step B0, run `b0-20261031`, 2026-10-31); **no owner
decision is needed** (owner, 2026-10-31). They are the **p75 of the measured lead time** by day type,
editable as HA helpers:

| Outdoor band (degC) | Weekday, target 12:00 (minutes) | Weekend, target 09:00 (minutes) |
|---|---|---|
| below 8 | 675 | 530 |
| 8 to 12 | 545 | 430 |
| 12 and above | 275 | 215 |

**How they were measured.** The scenario is a hall/kitchen at 23.5 degC at 22:00 that coasts at the
observed night cool-down rate, then restarts; the lead is the compressor-to-trough lag plus the time to
re-warm. Rates are the October to April episodes of the S3 winter (2025-26), at **26 degC water**, which
is **conservative under the warmer software curve** of section 7.1. The outdoor band is the mean
debiased outdoor temperature of the night. A Monte Carlo over the observed episodes (rates drawn
independently, which widens the tails) gives the percentiles; the table uses the p75.

| Context (S3, hours before the target) | p50 weekday | p90 weekday | p50 weekend | p90 weekend |
|---|---|---|---|---|
| below 8 degC | 8.3 to 8.4 | 12.8 | 6.7 to 6.8 | 10.2 |
| 8 to 12 degC | 6.2 | 11.4 | 5.1 | 9.0 |
| 12 degC and above | 2.7 | 7.4 | 2.2 | 5.9 |

Measured rates (October to April, per thermostat room): cooling **0.04 to 0.12 K/h** (night median),
warming **0.08 to 0.19 K/h** at 26 degC water (median of the heating episodes, by band), lag between
compressor start and the first rise **0.5 to 0.75 h** (resolution about 0.5 h, because the thermostat
reports a change only every 30 min). In cold weather about 20% of the hall/kitchen heating episodes at
26 degC water do not raise the room at all.

**Consequence.** In cold weather the p75 early start falls around **01:00** (weekday, 00:45) or just
after midnight (weekend, 00:10), so a night setback pays only in **mild** weather. At the S3 setpoint and 26 degC water the morning deficit from a
07:00 start could not be recovered (median 12 to 24 h in bands below 12 degC), so a lower water
temperature makes optimum start much less effective.

**Night floor (21 degC).** Under the 23.5 degC schedule the hall/kitchen rarely goes below 21 degC
before the early start (at most 0.7% of nights in every band). The bedroom does so more often; if it
keeps its observed evening offset from the hall (0.1 to 0.6 K lower), the share is 0.5 to 3.1% (S3) and
1.3 to 7.7% (S2). The bedroom's **21.5 degC setpoint keeps demand-driven heating protecting it**: the
run logic of section 2 starts the heat pump when the room falls below the setpoint minus the hysteresis.
The floor needs no extra rule; the share of time below the minimum in section 12 watches it.

The first deployment uses these p75 defaults and records them in the [regime log](../components/regime-log.md)
when deployed. The 180/120 min placeholders are withdrawn. Band edges are the measured ones (B0 pooled
the fewer-than-4 degC nights with 4 to 8 degC because of too few nights).

## 7. Water temperature and hot water

### 7.1 Space-heating water temperature

AppDaemon controls it **directly**. The unit stays in **direct mode** (`Heating_Mode` 1). AppDaemon writes
`SetZ1HeatRequestTemperature` as an absolute degC value.

Requirements:

- a **software curve** with editable points; initial points: outdoor **-5 degC to 32 degC** water and
  **15 degC to 25 degC** water, interpolated linearly and clamped outside the two points;
- an optional **raise during optimum start** (a small, editable number of kelvin), applied **from the early-start write until the hall/kitchen reaches its setpoint**;
- **integers only, clamped to 20 to 35 degC**;
- **write only when `sensor.aquarea_heating_mode` = 1** (direct); in curve mode the same command means a
  shift in K, so never write;
- **MQTT retain false and a validated payload**: the fork turns an empty or garbage payload into 0 degC;
- **write only when the target changes by at least 1 K and at most once per 15 min**, because the unit
  probably persists settings (avoid memory wear);
- **log every write** (time, old target, new target, outdoor input, reason).

The curve is software, not the unit's native curve, so the unit's four curve points are untouched.

**Outdoor-temperature source for the curve (resolved, owner 2026-10-31):** the **offset-corrected unit
sensor**. The unit's own sensor reads about 1.6 to 2.5 K low while the compressor runs
([outdoor-temperature](../components/outdoor-temperature.md)); the correction offset is an **editable HA
helper**. An HA weather entity is not used.

### 7.2 DHW stays price-optimised (amends ADR-0006 decision 4)

ADR-0006 decision 4 retired DHW price forcing. This design **keeps DHW price-optimised**. Shifting heat into
the tank is lossless, whereas the battery loses 10 to 20% round trip and has limited capacity, so hot water
is the better place to use a cheap hour.

- **Rule:** the cheapest **2 h window of the day**, ranked by **price divided by expected COP** (warmer
  hours rank better), avoiding the optimum-start warm-up.
- **Tank target at most 50 degC**, so the heat pump alone reaches it and the 3 kW heater never starts
  (service manual: above about 51 to 55 degC needs the heater).
- **Legionella / sterilization** is currently off (`binary_sensor.aquarea_sterilization_state` 0) and
  **stays off for now** (owner, 2026-10-31). The final MPC (track B, section 13) should schedule it
  eventually; this is a future extension of the B-track scope.

### 7.3 Price reactors become three

The price-reactive writers are now: the **battery optimiser**, the **DHW scheduler** and **boost**
(section 8). DHW and battery charging will often share the cheapest hours.

## 8. Boost (phase 2, after normal mode works)

Boost is built only after normal mode has run in production. Parameters as in ADR-0006 decision 3, as
amended:

- **Space-heating soak:** room ceiling about 25.5 degC, water about 30 to 32 degC.
- **Start condition:** battery state of charge about 90 to 95%, **or** charge power drops, **or** window end
  minus soak time is reached, whichever comes first.
- **DHW in boost also stays at most 50 degC** (corrects the "55 to 60 degC" of the original decision 3).
- Boost never relaxes the comfort minimums, the compressor limits or the manufacturer protections.

## 9. Import limit and the heater

The **5.0 kW smart-meter limit is hard**: the meter cuts supply, and this has happened. DHW and battery
charging will often share the cheapest hours, so:

- the **battery grid-target cap below the import limit (work-list item 8 in the
  [climate plan](climate-optimizer-implementation.md)) is mandatory before go-live**;
- **AppDaemon must never trigger the 3 kW heater**: DHW target at most 50 degC, no sterilization from
  AppDaemon, no forced heater.

**Import-limit basis (owner-confirmed 2026-10-31; no distributor document yet): an instantaneous trip.**
The battery cap therefore cannot rely on 15-minute averages. OmniBattery must **regulate against the
measured load in real time with a margin below the limit**. This is a **requirement for the actuator
ADR**.

**Backup heater: decided 2026-11-05 (owner).** Leave the heater to the unit: the controller writes setpoints and
operating modes only, and the warning sign is heater on outside defrost. The unit still starts the heater by itself
below its heater-on outdoor temperature of 0 degC; decision, evidence and lever in
[heat-pump-controller](../components/heat-pump-controller.md#heater-facts-import-limit-inputs).

## 10. Rollback and failure mode

- **No runtime strategy switch.** Rollback is **`git revert` in homelab**; the legacy app is **archived in
  the homelab repository** so the revert is easy.
- **If AppDaemon is down,** the existing watchdog leaves the heat pump in heating mode at the last water
  temperature, and the thermostats keep protecting comfort through the floor valves. The worst case is
  extra kWh, never no heating.

Per the [rollback rules](climate-optimizer-implementation.md#rollback-and-operational-handoff), the
implementation PR states the expected `Heating_Mode`, who controls DHW, and how to verify the pump after
stopping AppDaemon.

## 11. HA helpers (editable)

Editable without a deployment: per-room day minimums and floors (section 3) and the window times (weekday and weekend), margin, lead
times, curve points, the optional optimum-start raise, away. **Machine limits stay in code** (minimum on/off,
water clamp 20 to 35 degC, write rate, tank cap 50 degC).

## 12. Evaluation

Without a runtime switch, the [C7-F](climate-flow-calibration-optimization.md) field trial cannot alternate
arms. It becomes a **before/after comparison**: past winters plus this winter until go-live, against the new
controller, **normalised for outdoor temperature** (heating degree-days or temperature-band matching). The
shadow MPC (track B) is the second reference.

Metrics:

- Kh below each minimum, per room, and the share of time below;
- heat-pump kWh per HDD, from the heat-pump meter or HeishaMon, **not the whole-house meter** once the
  battery runs;
- compressor starts per day;
- DHW cost share.

**Preregister the criteria before go-live** (see [climate preregistration 2026-27](climate-preregistration-2026-27.md); the same discipline as the F2a preregistration: criteria and
thresholds committed before the first controller day). A before/after comparison is **weaker evidence than a
randomised trial**: weather, occupancy and tariff differ between periods and the normalisation is
imperfect. The result is read with that limit stated.

## 13. Track B: shadow thermal-model MPC

A thermal model plus an MPC (objective: **minimum kWh subject to the same comfort schedule, price-blind**),
prepared in this repository and run in **shadow** (read-only, plan and prediction metrics only) in parallel
this winter. It is compared with the controller after winter. The shadow rule of `AGENTS.md` is unchanged:
no writes, no new credentials, no Home Assistant service calls.

Shadow can validate predictions and plan differences. It **cannot** show the realised comfort of the MPC,
because the MPC never acts.

**Future extension (owner, 2026-10-31):** the final MPC should also **schedule the Legionella /
sterilization cycle** (section 7.2) outside the cheap hours and within the import-limit headroom. It is not
part of B0 to B3.

| Step | Content | Status |
|---|---|---|
| **B0** | Diagnostics: [C2 recovery](climate-c2-recovery.md) R0 to R2 and the warm-up rates (feeds the optimum-start table) | done (2026-10-31), see [climate-c2-recovery section 7](climate-c2-recovery.md#7-status-2026-10-31-b0-results-r0-to-r2) |
| **B1** | Offline MPC replay on the committed comfort schedule | implemented, offline ([climate-mpc](../components/climate-mpc.md)); result below |
| **B2** | Shadow runner in the service (read-only plan and prediction metrics) | implemented, not deployed (2026-10-31; [ops](../service.md#climate-shadow-track-b2)) |
| **B3** | Preregistered comparison after winter | planned |

### B2 interface to the controller (read-only)

The shadow reads three helpers of the homelab app (`comfort_control.py`, helpers in
`config/integrations/appdaemon_comfort.yaml`), writes none of them, and tolerates their absence:

- `input_boolean.comfort_control_enable`: on while the comfort controller is in charge. It labels every
  shadow record `comfort` (off: `comfort_off`; absent: `legacy`). The preregistered windows depend on it.
- `input_text.comfort_dhw_plan`: JSON the app writes for **today only**, for example
  `{"day":"2026-11-16","start":"2026-11-16T02:00:00+01:00","end":"2026-11-16T03:00:00+01:00","forced":true,"target_written":true,"done":"2026-11-15"}`.
  After the window `start` and `end` are null; the app may also write `{"done":"2026-11-16"}` alone.
  The shadow reads it as follows:
  - `day` equal to the decision's local date: DHW in the slots that start in `[start, end)`; null
    `start` and `end` mean no DHW for the rest of that day;
  - otherwise, `done` equal to that date: today's window is finished, so no DHW today;
  - a plan of another day (stale), an unreadable value or a helper that does not exist says nothing
    about today.
  Today's slots follow the plan. Tomorrow's slots of the 24 h horizon, and every slot when the plan says
  nothing, repeat yesterday's measured DHW slots.
- `input_boolean.comfort_away`: on means away (section 5). Every setpoint is 21.5 degC and the minimum is
  the floor.

### B1 result (offline replay)

**The first replay (runs `b1-20261031-*`, code `dd344f3`) is withdrawn.** Its figures (+3 K binding,
the day minimum missed on 71 to 90% of days, plan over legacy 1.18 and 2.42) were an **artefact of the
setpoint assumption plus the placeholder `phi`**:

- the hall/kitchen valve opened only at the window start;
- 97% (S2) and 83% (S3) of the pre-window heating slots of its midnight plans had every modelled
  valve shut. This is measured in `days.jsonl` of `b1-20261031-b2d-perfect`: planned heating slots
  before the window start in which every room, at the slot's start state, was at or above its
  setpoint minus the 0.25 K valve margin;
- those slots heated only the always-open share, set to the unmeasured placeholder `phi` = 0.3. The
  run logic of section 2 would never do that.

The replay below makes five fixes:

- the optimum-start setpoint of section 6 (p75 lead by band);
- no heating where every modelled valve is shut;
- starts and minimum on/off counted on space heating only;
- the comfort window aligned to the slot end;
- re-linearisation keeping the best simulated iterate.

Runs `b1r2-20261031-b2d-perfect`, `-b2d-persistence`, `-c2-perfect` and `-b2d-perfect-offset3`:

- code `84b05f0`, MPC config `f1c25db4`, dataset `cdd448abb006`, feature set `da6d678016ed`;
- S2 and S3 from November to March, a 24 h plan every hour (2932 and 3082 decisions).

Formulation and causality rules: [climate-mpc](../components/climate-mpc.md).

**Exploratory.** No thermal model is accepted, and S3 was inspected in C2 and B0. **Planned kWh and
planned comfort are model predictions of plans that were never executed, not realised outcomes.** Each
day starts from the house as the legacy controller left it. Leakage:

- `perfect` weather is perfect foresight of the outdoor temperature, and the observer reads the same
  series, which uses the fused station series in long gaps;
- S2 is scored with an S3-fitted (later-season) prior;
- the `c2` S2 run is in-sample.

(a) Open-loop accuracy of the model inside the MPC (B0's model, legacy actions through it, perfect
weather), MAE / bias in K. These figures do not depend on the plan, so the fixes leave them unchanged.
Beyond about 12 h the plan relies on low-confidence predictions.

| Horizon | Room mean S2 | Room mean S3 | Persistence S2 / S3 | Hall/kitchen S2 | Hall/kitchen S3 |
|---|---|---|---|---|---|
| 1 h | 0.13 / -0.01 | 0.12 / 0.00 | 0.15 / 0.13 | 0.28 / -0.16 | 0.25 / +0.06 |
| 6 h | 0.26 / -0.03 | 0.22 / -0.01 | 0.44 / 0.31 | 0.38 / -0.18 | 0.32 / +0.05 |
| 12 h | 0.31 / -0.04 | 0.25 / -0.02 | 0.53 / 0.36 | 0.42 / -0.20 | 0.33 / +0.05 |
| 24 h | 0.30 / -0.08 | 0.24 / -0.04 | 0.38 / 0.27 | 0.41 / -0.25 | 0.32 / +0.02 |

- **Persistence weather:** room mean 0.39 / 0.28 K at 12 h and 0.37 / 0.31 K at 24 h. At 24 h S3 is
  worse than room persistence.
- **C2's model B:** 0.32 / 0.26 K at 6 h and 0.37 / 0.30 K at 12 h.
- **Heat delivered per day**, given the legacy permission (B0's weakest link): MAPE 14% (S2) and 15%
  (S3), season bias +3% and +8%. C2's model B: 15% and 24%, +8% and +19%.
- **Electricity per day:** MAPE 17% and 16%, bias +8% and +2%.

(b) The midnight plan against the legacy day, same days (97 in S2, 111 in S3), B0's model, perfect
weather, water offsets 0 to +6 K:

| | S2 | S3 |
|---|---|---|
| Planned / measured legacy electricity | 1.52 | 3.22 |
| Planned / the model's own prediction of the legacy day (days with both) | 1.42 | 2.53 |
| Same as the first row, by HDD tercile (mild, middle, cold) | 1.14, 1.59, 1.60 | 2.71, 3.48, 3.26 |
| Kh per day below 23.5 degC in the hall/kitchen window: plan, legacy | 1.8, 4.8 | 2.1, 15.1 |
| Days with any Kh below the day minimum: plan, legacy | 51%, 70% | 71%, 100% |
| Kh per day below the 21 degC floor (comfort rooms): plan, legacy | 0.35, 1.75 | 4.1, 17.4 |

Electricity ratios for the other runs:

| Run | S2 | S3 |
|---|---|---|
| Persistence weather | 1.62 | 3.28 |
| C2's model B | 1.46 | 3.32 |
| Offsets 0 to +3 K | 1.33 | 2.67 |

**Reading:**

- The plan buys comfort the legacy controller did not deliver: the hall/kitchen deficit is cut by 62%
  (S2) and 86% (S3). The electricity cost is correspondingly large.
- The plans heat 14 to 16 h a day with about one start.
- No kWh saving can be claimed. A comparison at matched comfort needs new-regime data.

**Planned heating slots** (all decisions, B0's model, perfect weather):

| | S2 | S3 |
|---|---|---|
| Rooms with an open valve: one of three | 97% | 91% |
| Rooms with an open valve: all three | 1% | 6% |
| Mean valve factor (`phi + (1 - phi) open share`, placeholder `phi` 0.3) | 0.54 | 0.57 |
| Simulated with every valve shut (run logic: no heat) | 1.8% | 0.3% |

During optimum start only the hall/kitchen valve is open, so the plan heats at about half of the full
heat output.

**Water offset choice, and why the range is +6 K.**

| Offset (K) | 0 | +1 | +2 | +3 | +4 | +5 | +6 |
|---|---|---|---|---|---|---|---|
| S2, share of planned heating slots | 9% | 8% | 10% | 7% | 8% | 19% | 40% |
| S3, share of planned heating slots | 5% | 4% | 4% | 5% | 4% | 7% | 71% |

| | 0 to +3 K, S2 | 0 to +3 K, S3 | 0 to +6 K, S2 | 0 to +6 K, S3 |
|---|---|---|---|---|
| Heating slots at the top offset | 79% | 95% | 40% | 71% |
| Kh per day below the day minimum | 2.8 | 5.1 | 1.8 | 2.1 |
| Planned / legacy electricity | 1.33 | 2.67 | 1.52 | 3.22 |
| Planned lifts outside the COP lift table | 5% | 7% | 13% | 22% |

- **Why widen.** With the setpoint fixes, +3 K still bound (79% and 95% of slots), so the range was
  widened. +6 K cuts the remaining day-minimum deficit by about a third in S2 (34%) and by more
  than half in S3 (60%).
- **+6 K still binds in S3.** It is chosen in 71% of heating slots.
- **The COP penalty of high water is understated.** At +6 K, 13 to 22% of the planned lifts lie
  outside the COP lift table, which reads the nearest bin, so the electricity of the high-water slots
  is under-estimated.
- **The +6 K result needs the water-temperature trial.** It should not be read as a recommended water
  curve until that trial ([C7-F](climate-flow-calibration-optimization.md)) supplies COP data at
  higher water.

(c) Solver:

- 0 infeasible of 24056 solves (four per decision);
- median 0.08 s, p95 at most 3.5 s;
- 297 solves (1.2%) hit the 20 s limit and used their incumbent, with a gap of at most 1.3%.

Re-linearisation:

- The free-running first reference is rarely the best iterate (16% and 2% of decisions).
- The last iterate changed no slot in 65% (S2) and 62% (S3) of decisions; otherwise the best
  simulated iterate is kept, so the loop does not always converge.

(d) The plan differs materially from the legacy day on 93% (S2) and 90% (S3) of days:

- electricity by more than 20% on 78% and 87%;
- heating-slot overlap below 0.5 on 70% and 57% (median overlap 0.38 and 0.45).

Legionella scheduling stays the future extension recorded above; it is not part of B1.

## 14. Owner decisions

Answered by the owner on 2026-10-31.

| Item | Section | Status | Date | Answer |
|---|---|---|---|---|
| Outdoor-temperature source for the curve | 7.1 | Resolved | 2026-10-31 | Offset-corrected unit sensor; the offset is an editable HA helper |
| Day window end time | 3 | Resolved | 2026-10-31 | 22:00, confirmed (no longer an assumption) |
| Lead-time values | 6 | Resolved (no owner decision needed) | 2026-10-31 | Resolved with B0 (2026-10-31): measured p75 defaults by day type in the table of section 6; the 180/120 min placeholders are withdrawn |
| Legionella policy | 7.2, 13 | Resolved | 2026-10-31 | Off for now; the final MPC (track B) should schedule it eventually (future extension) |
| Import-limit basis | 9 | Resolved (owner-confirmed; no distributor document yet) | 2026-10-31 | Instantaneous trip; battery cap must regulate in real time against measured load with a margin (actuator ADR requirement) |
| Thermostat device programs | 4 | Resolved | 2026-10-31 | Disabled, owner-confirmed |
| Backup heater enabled for space heating | 9 | Resolved | 2026-11-05 | Leave the heater to the unit; the controller writes setpoints and modes only; the warning sign is heater on outside defrost ([decision](../components/heat-pump-controller.md#heater-facts-import-limit-inputs)) |
