# ADR-0010: Vacation mode: the optimizer plans the absence and the return

- **Status:** **Accepted 2026-11-23 for v1** (owner). v1 is built (this ADR's [Decision](#decision-v1)); the items
  under [After the first vacation](#after-the-first-vacation) are agreed directions, not built. Nothing here
  changes the write allowlist of [ADR-0009](0009-live-climate-control.md).
- **Date:** 2026-11-23
- **Decision owners:** Homelab maintainers (the owner took the decisions below on 2026-11-23)
- **Extends:** [ADR-0009](0009-live-climate-control.md) (live climate control: the optimizer writes space heating)
  and [ADR-0006](0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md).
- **Implementation (v1):** `climate/vacation.py` (the window, the schedule, the horizon, the contract object),
  `climate/shadow.py` (`decide`, `decision_hours`), `service/climate_shadow.py` (window records, refit pause,
  forced re-decision), `service/climate_actuator.py` (guard and vacation hold), `service/runtime.py` (helpers,
  re-decision on a change, summary), `climate/plan_export.py`, `service/metrics.py`, `service/config.py`
  (`climate_shadow.vacation`), `docs/schemas/climate-plan-v1.json`. The homelab half (the two helpers, a cancel
  script, the dashboard section, alert rules) is a separate repository's change.

## Context

The owner wants to mark an absence on the Climate optimizer dashboard ("out from 12-12 to 19-12") and have the
optimizer decide how to spend it: no comfort while nobody is home, and the configured comfort again on return, for
the least energy. Before v1 nothing did that:

| Fact | Consequence |
|---|---|
| The climate shadow read an `input_boolean.comfort_away` that was never created; when on, every room's minimum became its floor | Away would only drop the day minimums; the house would still be held at 18 to 23 degC |
| The actuator forces `Heat+DHW` for any comfort room below its floor (ADR-0009 decision 6); the floor helpers are bounded to 18 to 23 degC | Even a perfect plan could not let the house cool below 18 degC |
| The MPC is price-blind and minimises predicted heat-pump electricity plus a start penalty, comfort as soft minimums ([climate-mpc](../components/climate-mpc.md)) | With no minimum during the absence its optimum is to coast and heat as late as the return allows: a colder house loses less heat |
| The horizon is 24 h; the terminal term charges heat content below the initial one at its end | A 24 h plan cannot see a return days ahead, and the terminal term penalises the coasting the absence is for |
| Past absences with heating off (presence history, 2024 to 2026) took the comfort-room mean to about 17.5 degC in the coldest week and 19 to 21 degC in autumn and spring; the warm-ups after them took 1.5 to 2.5 days under the legacy controller | The warm-up can be longer than 24 h, so the horizon must reach the return |
| `q0` and the diurnal gain `D` of the thermal model stand for occupancy and internal gains, which an empty house does not have | The model predicts an empty house warmer than it is: it under-predicts the warm-up |
| The weekly causal re-estimation (`model.reestimate`) refits `ua` and `q0` on a trailing 28-day window | An absence in the window biases `q0` low for weeks after the return |
| A data outage longer than 1 h restarts the observer, which needs 24 h of data before it plans again; meanwhile the actuator falls back to `Heat+DHW` at 26 degC | An outage during an absence would heat the empty house for about a day |

### Owner decisions of 2026-11-23

1. **The optimizer owns vacations**, not a Home Assistant automation. The owner sets only when the house is empty
   (Leave, Return); the optimizer decides how cold it gets and when to warm up.
2. **No temperature knob for the owner.** How low the house goes should follow from the model and real data. v1
   has only a building **guard** of 12 degC, below anything the house has reached unheated, as a backstop; a
   data-driven damp limit needs room humidity sensors (after the first vacation).
3. **Ready 2 h before the return.**
4. **Thermostat schedules are not touched** (ADR-0009 decision 4 stands).
5. **Ship v1 before the first absence** (about 36 h after this decision) and improve it with that week's data.
6. **Hot water stays out of v1** (AppDaemon `dhw_control.py` runs unchanged); see after the first vacation.

## Decision (v1)

### 1. Interface: two dates, read only

- Two Home Assistant helpers, created by the homelab: `input_datetime.climate_vacation_start` (**Leave**) and
  `input_datetime.climate_vacation_end` (**Return**), date and time, local time.
- A window is **valid** when both parse, `end > start`, `end > now` and `end - start <= max_days` (60). There is
  no on/off boolean: the dates are the only source of truth.
- **No vacation** without an error: both equal (the owner's cancel, and fresh helpers) or `end` in the past.
- **Rejected** (`invalid_reason`): `unparseable`, `end_before_start`, `too_long`; `helpers_unavailable` when a
  helper is missing or unavailable. The contract echoes a rejected window's helper values so the owner sees what
  was rejected. Every rejection means the normal schedule: the failure mode is heating an empty house, never a
  cold return. Exception: while the helpers are **unavailable** (a restarting Home Assistant) the decision and the
  actuator keep the last valid window until its end, so a restart does not end a vacation for an hour.
- Read through the Home Assistant session (entity store): availability, **no age test** (ADR-0009 amendment of
  2026-11-18). The optimizer **never writes** them; the allowlist is unchanged.
- A change of either helper (not the first state of a sync) decides the current boundary again 60 s later
  (flagged `redecision`, never scored twice) and wakes the actuator, which reads the helpers on every tick.

### 2. Comfort schedule of a vacation

With `ready_at = end - ready_margin_minutes` (120), by the slot's **end** time (the MILP compares the minimum with
the end-of-slot state):

| Slot end | Comfort minimum per room |
|---|---|
| before `start` | the normal schedule |
| `[start, ready_at)` | only the guard `guard_c` (12 degC) |
| `[ready_at, end)` | the normal schedule's minimum **at `end`** (floor, and the day minimum if `end` is in the window) |
| from `end` | the normal schedule |

The valve model keeps the normal schedule's setpoints (the thermostats are not touched), not the old away
override. A window that starts inside the horizon is the mirror of optimum start: nothing is required after Leave.

### 3. The horizon covers the return

- Once `ready_at` is within `lookahead_hours` (48), the decision's horizon is `end + 6 h` (whole hours, at least
  the configured 24 h, at most 72 h). The decision record keeps every slot of it (`plan_on`, `plan_temps`,
  `horizon_hours`), so the warm-up can be scored after the trip; the gate scoring still reads the first 24 h.
- **Terminal term off** when the horizon ends inside `[start, ready_at)`; on (normal) when it covers the return.
- The DHW mask beyond the first day repeats yesterday's measured slots day after day. Beyond the forecast the
  outdoor temperature holds its last value (as today).
- **Solve time** (synthetic house and prior of the tests, the committed solver settings: 7 water levels, three
  re-linearisations, 20 s limit; three decision times each): one MILP solve at most 5.4 s at 24 h, 3.0 s at 50 h,
  2.9 s at 56 h (the default configuration's longest), 3.6 s at 72 h; the whole decision at most 8.2 s. All
  `optimal`. No hourly blocks were needed. The live solve-duration histogram is the check on real data.
- **Warm-up start** (`recovery_start`, v1): the first planned heating slot inside `[start, ready_at)`. Once it has
  begun it is kept for the window, so a later plan that starts with a short pause (the minimum off) does not flip
  the phase back to `away`.

### 4. Actuator

- During `[start, ready_at)` every room's floor is the guard: a room below it forces `Heat+DHW` (a **guard
  override**, its own gauge; the floor-override counter is not touched). From `ready_at` the normal floors apply,
  so a late warm-up is caught by the floor override.
- **Vacation hold:** during `[start, ready_at - lookahead_hours)` a fallback caused by the plan (none, stale, not
  from this process, invalid, failed, no observer state) holds heating **off** (`DHW`) with the guard still
  forced, instead of the safe state. The heartbeat continues (the optimizer is alive and holding on purpose). It
  is counted in `climate_actuator_vacation_fallbacks_total`, never in `climate_actuator_fallbacks_total`.
- A missing room reading or mode sensor keeps the normal fallback (the guard cannot be proven), and so does any
  fallback from `ready_at - lookahead_hours` on: with the return near, the worst case is extra kWh again.
- Everything else of ADR-0009 is unchanged: machine limits, restart grace, legacy toggle, the owner's select.

### 5. Model and measurement hygiene

- The observer keeps running through the absence; the hourly replan from the measured rooms corrects the
  occupancy bias of the coasting prediction.
- The model is unchanged in v1 (`q0` and `D` kept). Its bias makes the warm-up late, not early; the 2 h margin,
  the hourly replan and the floor override from `ready_at` absorb it.
- Every window is recorded (`climate_vacations`, closed at a cancel or a change with its effective end), so a
  restart remembers it. The weekly refit of `ua`/`q0` **pauses** while a vacation overlaps its trailing window or
  is under way; the last parameters stay.
- Decision records carry `inputs.vacation`, score records `vacation_phase`. Leaving vacation days out of the
  gate is a **declared deviation** from [the 2026-27 preregistration](../planning/climate-preregistration-2026-27.md),
  still to be written there (after the first vacation).

### 6. Contract (v1, additive)

`inputs.vacation` (and the flat summary's `vacation`, **always** present because it is read from the helpers at the
request) is `{phase, start, end, ready_at, recovery_start, guard_c, invalid_reason}` with `phase` one of `none`,
`scheduled`, `away`, `recovery`, `ready` ([contract](../climate-plan-contract.md)). `slots.n` is 96 normally and up
to 288 near a return. `inputs.away` stays for now.

### 7. Metrics

`climate_vacation_phase{phase}` and `climate_vacation_invalid{reason}` (one-hot, pre-labelled; an empty or finished
window is not invalid), `climate_actuator_vacation_fallbacks_total{reason}`,
`climate_actuator_vacation_fallback_active`, `climate_actuator_guard_override_rooms`
([service](../service.md#vacation-mode-adr-0010)).

### 8. Configuration

`climate_shadow.vacation`: `enabled` (true), `start_entity`, `end_entity`, `guard_c` (12.0), `ready_margin_minutes`
(120), `lookahead_hours` (48), `max_days` (60) ([configuration](../configuration.md)).

## Safety

- No new write path, entity or service call; the allowlist of ADR-0009 is unchanged.
- Invalid dates, a missing helper or a disabled feature mean the normal schedule. A plan failure during the absence
  holds heating off with the guard enforced by the actuator independently of the plan; near the return, and
  whenever the rooms or the unit cannot be read, the normal safe state applies.
- The HA watchdog is untouched: the heartbeat continues during a hold, and stops only if the optimizer stops.

## Consequences and risks

- **Saving:** a week away spends most of the absence with space heating off.
- **Late warm-up** if the empty house is colder than modelled: mitigated by the margin, the hourly replan and the
  floor override at `ready_at`. Measured on the first vacation.
- **Solve time** at 48 to 72 h: measured on synthetic data only (section 3); watch the solve-duration histogram.
- **Battery planning:** the load forecaster does not know the house is empty (after the first vacation).
- **Dashboards:** a 72 h plan has three times the slots; consumers that assume 96 slots must not.

## Data to collect during the first vacation

What we already know, from five past whole-house absences with space heating off (2 to 8.5 days, autumn
to late winter; offline analysis `hack/absence_analysis.py`, private outputs in the data dir):

- **The empty house settles high.** About 1 to 1.5 K the first day, then slowly towards
  `T_eq ~ 13.8 + 0.49 T_out` (12 settled days, RMSE 0.4 K): about 17 to 18 degC in a cold week, 7 to 11 K above
  outdoor. The 12 degC guard is not expected to bind. The bedroom runs coldest when the house is empty.
- **The model fails safe, but for the wrong reason.** Open loop with `Q = 0` it is too warm for the first one to
  two days (+0.4 to +1.0 K) and too cold after about four (down to -1.7 K at 120 h): it settles at
  `T_out + q0/ua`, about 4 K above outdoor against the measured 7 to 11 K. Dropping `D` changes nothing and a
  refit of `q0` asks for *more* gain, not less: the error is structural (a slow envelope node the air + slab
  model lacks), not an occupancy gain.
- **Warm-ups are much faster than modelled.** Measured 3 to 6 h from about 19 degC to a 21 degC room mean, with
  6.4 to 6.8 kW of heat delivered at 27 to 29 degC water; the prior's heat law caps at 4.0 kW and the model
  predicts the same warm-ups 1.2 to 10 times slower. The model's worst plausible case (hall 24 degC from
  17.3 degC at 0 degC outdoor, 4 kW) is about 40 h, inside the 48 h look-ahead. So v1 starts warm-ups early:
  extra kWh, not a cold return.
- **An observer restart happens:** one absence had a 4 h thermostat data gap that restarted the observer; live,
  that is a `no_state` decision, which the vacation hold now turns into "heating held off".

What to collect on this vacation (most of it is already recorded; the gaps are marked):

1. **The decisions and their extended plans** (`climate_decisions`, `predicted_room_c` up to 72 h) against the
   measured rooms, every hour: the coasting and warm-up error of the model the plan actually used.
2. **One clean warm-up at the plan's water target** (up to 35 degC): heat delivered per slot, water target, flow
   and return, compressor, each thermostat's `hvac_action` and when each room reaches its floor and the hall its
   day minimum. All history is at 26 to 29 degC water, so this calibrates the heat cap (4.0 kW modelled, 6.5+
   measured) and the fast air response.
3. **Exact labels:** the window as the optimizer read it (`climate_vacations` records), any visit, window
   opening or shutter change (gap: logged by hand by the owner on return), interior doors left open or closed.
4. **Base internal gains while empty:** the servers circuit and the other base load (Shelly circuits), and the
   hot-water tank's losses (AppDaemon `dhw_control` off during the trip, so the tank runs on the unit's own
   control): a measured gain instead of `q0`.
5. **Solar:** a radiation or PV series (gap: the solar gain sits on its lower bound in the prior).
6. **Gap, hardware:** room humidity (for the data-driven guard) and a floor-surface temperature (to separate the
   heated slab from the slow envelope node). Not available for this trip.

## After the first vacation

Agreed directions, not built:

- **Recovery check:** at every decision during the absence, simulate the fastest warm-up from the current state
  and extend the horizon (or start now) when it would not fit before `ready_at`; the look-ahead alone covers a
  one-week trip in late autumn.
- **Observer fast restart:** after hours with heating off the slab sits near the air temperature, so the observer
  can start from the measured rooms instead of waiting 24 h.
- **Empty-house model:** the past absences show the error is structural, not an occupancy gain (section above). Try a
  third, slow envelope state (or a neighbour boundary) and a heat cap calibrated on the measured warm-ups,
  validated on held-out absences including this trip; correct it online from the first hours of each absence.
- **Data-driven guard:** room humidity sensors, then a damp/mould limit computed from indoor humidity instead of
  the fixed 12 degC.
- **Gate exclusion:** write the declared deviation into the preregistration and leave vacation days out of the
  comfort and energy scores.
- **Retire `comfort_away`:** remove `climate_shadow.entities.away` and `inputs.away`.
- **Hot water:** recommended: move DHW into the optimizer (one writer of the heat pump, ADR-0009; DHW and space
  heating share the compressor; reheat by COP, not price, under ADR-0006, the battery covers price). The
  alternative, retiring AppDaemon `dhw_control.py` and letting the optimizer switch DHW off, needs a new allowlisted
  write anyway (`switch.aquarea_holiday_mode`, main power or a `Heat`-only mode). During an absence: no boost, and a
  sterilization cycle plus a reheat by `ready_at`. DHW costs about 1.5 kWh/day of electricity in an empty week.
- **Load forecast:** an empty-house load profile for the battery planner during a vacation.

## Alternatives considered

- **Home Assistant automation** that turns `comfort_away` on at Leave and off a fixed number of hours before
  Return, or that puts the optimizer in `shadow` and the unit in `DHW`: no optimizer change, but the floors keep the
  house at 18 degC or more (or nothing guards it at all) and the fixed lead is either wasteful or late. Rejected by
  the owner (decision 1).
- **An owner-set vacation floor** (for example 16 degC): simple, but the owner wants the model to decide (decision 2).
- **A permanently longer horizon** (48 to 72 h for every decision): covers the return without special cases, but
  plans further than the model is accurate (beyond about 12 h predictions are weak) every hour, for a feature used a
  few weeks a year.
