# Periodic full charge for cell balancing

- **Status:** Plan, owner decisions recorded 2026-11-04, **amended 2026-11-04** (see the
  [amendment](#amendment-2026-11-04-success-criterion-settle-hold-and-adaptive-cadence), which supersedes the fixed 5/15
  day cadence, the SOC-99 success criterion and the bootstrap of the original text; superseded passages stay in place,
  marked). PRs 1 and 2 are merged (#81 solver and planner, #80 actuator, guard and config); PR 3 (scheduler,
  [implementation notes](#pr-3-implementation-notes)) is implemented. The feature ships off by default. Merging this plan authorises nothing on the installation: each
  implementation PR below is reviewed on its own, and the first live run is supervised.
- **Date:** 2026-11-04
- **Decisions:** [ADR-0007](../adr/0007-live-battery-control.md) (live control; this plan amends it, text in
  [section 8](#8-adr-0007-amendment-text-and-agentsmd-edit)) and [ADR-0004](../adr/0004-battery-optimizer-production-architecture.md).
- **Code touched:** `src/battery_optimizer/oracle.py`, `service/planner.py`, `service/actuator.py`,
  `service/ha_guard.py`, `service/config.py`, `service/runtime.py`, `service/sink.py`, `service/metrics.py`, a new
  `service/full_charge.py`; `config/service.yaml`.
- **Related:** [battery longevity policy](battery-longevity.md) (the operating policy these amendments come from:
  balance telemetry, high-SOC dwell cost, thermal soft cap).
- **Owner of the code:** this repository. The HA-side watchdog lives in `pando85/homelab` (follow-up, section 12).

## Amendment 2026-11-04: success criterion, settle hold and adaptive cadence

Approved by the owner on 2026-11-04 after an external investigation of the Omnibattery cell-balance monitor (summarised in
[battery-longevity.md](battery-longevity.md)). It changes **what counts as a full charge**, **how the actuator ends a
run** and **how often a run happens**. Everything else of this plan (decision rule shape, MILP, enabler, guard, state
file) stands; where the original text disagrees, this section wins and the original passage carries a "Superseded" note.
Implemented in PR 3 (scope in [A7](#a7-pr-3-scope)), which may edit `service/config.py` for the new keys; PRs 1 and 2 are
merged and are not reopened.

### A1. Success criterion: a new valid balance measurement

A run counts as a full charge for the cadence **only when Omnibattery records a new valid balance measurement**:
`sensor.venus_last_balance_read` advances past the value seen when the run was armed. The service records, as evidence
next to it, `sensor.venus_cell_delta` (mV), `sensor.venus_balance_status`, `sensor.venus_delta_trend` and
`sensor.venus_delta_avg_4w` (the average of the last four **readings**, not four weeks).

| Outcome of a run | Counts for the cadence | State |
|---|---|---|
| `last_balance_read` advanced and `balance_status` is not `unknown` | **Yes**: `L` is the local date of the reading; the result classifies the next interval ([A3](#a3-adaptive-cadence)) | `done` |
| SOC reached 99 or 100 (or Omnibattery completed) but **no new reading** within the settle window | **No**: "reached, unmeasured". A failed attempt for the cadence: `L` is not moved, `consecutive_failures + 1`, retried at the next eligible day. Never reported as balanced | `failed(reached_unmeasured)` |
| SOC never reached 99, unacknowledged, never armed | No, as in [section 7](#7-state-persistence) | `failed(...)` |

A stale, missing or `unknown` reading is **unknown, never zero imbalance**: it neither resets the clock nor selects a
green cadence. SOC at 99% observed outside a run (a manual charge by the owner) no longer stamps `L` on its own; only a
new valid reading does, whoever caused the charge.

Superseded: the "SOC observed at 99% or more, or Omnibattery completes" definition of a full charge in
[section 4](#4-decision-rule) and the `soc_99` / `omnibattery_complete` values of `last_full_source` in
[section 7](#7-state-persistence). Those two events still drive the state machine (`reached`, `done`), but they stamp
`L` only together with the new reading; the source is `balance_reading`.

### A2. Settle hold in the actuator

Omnibattery stops charging at about 3.60 V on the highest cell (or at a confirmed BMS cutoff) and measures the cell
spread 60 s later; a reading taken under load is not comparable. After completion is observed (`integration_status`
leaving `weekly_full_charge`, or SOC 100, or `top_hold_timeout_min`), the actuator **holds idle**, with no discharge and
no charge (the existing idle intent), until **`last_balance_read` advances or `settle_timeout_s` (default 300, about five
times Omnibattery's 60 s) elapses**, whichever is first. Then the normal intent resumes and the weekly switch is turned
off (disarm moves behind the hold, so the run state Omnibattery holds is not cleared before it records the reading;
to be verified on the supervised run). A timeout without a reading is outcome `reached_unmeasured` (A1). The hold is a
field of the directive (`FullChargeDirective` gains a settle deadline, edited in PR 3 together with the actuator that
reads it) and is subject to the same kill switch and guard as every other intent: the guard still wins, and a
guard-failure safe idle simply ends the hold early.

### A3. Adaptive cadence

Replaces the fixed `min_interval_days` 5 / `max_interval_days` 15 of decisions 1 and 2 of
[section 1](#1-goal-and-owner-decisions). The interval bounds come from the **latest valid** `balance_status` and the
recent history, read from Omnibattery's sensors (never from a stale reading):

| Condition (latest valid reading) | `min` days | `max` days | Extra |
|---|---:|---:|---|
| `unknown`, or fewer than 4 comparable readings so far (includes the bootstrap, [A4](#a4-first-run)) | 10 | 14 | |
| 4 or more comparable readings, **all green**, and `delta_trend` not rising | 14 | 28 | |
| `yellow` | 7 | 14 | |
| `orange` or `red` on **two consecutive** readings | 5 | 7 | **Owner alert**; no automatic recovery or active-balance routine (the Omnibattery blueprint stays out of scope) |
| a single `orange` or `red` after a better reading | 7 | 14 (as `yellow`) | **Repeat the measurement at the next eligible day**; one reading alone is never an alert |

"Comparable" means a reading recorded after a settled completion of a run armed by this feature (or one the sensor itself
marks valid at the top-of-charge condition); readings whose status is `unknown` are not comparable. The decision rule of
[section 4](#4-decision-rule) is unchanged in shape, with `min`/`max` taken from the active row: `d < min` nothing,
`min <= d < max` threshold test, `d >= max` forced. The threshold function becomes
`threshold(d) = threshold_min_eur + threshold_scale_eur * (d - min) / (max - d)` (equal to the minimum at `d = min`,
divergent at `d = max`, monotone), so the day-5 / day-15 numbers of section 4 no longer apply as printed. The warning in
that section (the rule would normally fire at the minimum) still holds; the larger minimum of the common rows (10, 14)
is what makes the cadence long in practice, and `fixed_cost_eur` remains the knob to discriminate days.

**Configuration** (replaces `min_interval_days` and `max_interval_days` in the whole-schema block of
[section 12](#12-configuration-tests-rollout-and-pr-split); PR 3 edits `service/config.py` and `config/service.yaml`):

```yaml
full_charge:
  cadence:                    # days, keyed by the cadence class; the keys are fixed, the numbers owner-set
    unknown: {min_days: 10, max_days: 14}   # also: fewer than min_comparable_readings
    green:   {min_days: 14, max_days: 28}   # >= min_comparable_readings all green, trend not rising
    yellow:  {min_days: 7,  max_days: 14}   # also a single orange or red after a better reading
    alert:   {min_days: 5,  max_days: 7}    # orange or red on alert_consecutive readings in a row
  min_comparable_readings: 4
  alert_consecutive: 2
  settle_timeout_s: 300
```

Validation: per row `0 < min_days < max_days <= 30`; `green.min_days >= unknown.min_days >= yellow.min_days >=
alert.min_days` (a healthier battery is never measured more often than a worse one); `min_comparable_readings >= 1`;
`alert_consecutive >= 2`; `0 < settle_timeout_s <= 900`.

### A4. First run

There is no baseline reading since commissioning, so the first run is eligible **as soon as the feature is enabled**:
the bootstrap is "eligible now" (`L` is none, `d` is treated as `min` of the `unknown` row), the day is still chosen
**opportunistically by price** (the threshold rule at `d = min`), and it is **forced at `max` days from enablement**
(`enabled_at` is persisted in the state file; with `unknown` that is 14 days). The first live run is **supervised by the
owner** (kill switch in hand, as in the acceptance section). Superseded: bootstrap precedence (a) to (c) and the
2026-11-08 / 2026-11-18 dates of [section 7](#7-state-persistence); `bootstrap_last_full` remains as an explicit owner
override, and a valid `last_balance_read` dated after `battery.commissioned_at` is honoured as `L` when present.

### A5. Dwell on the day

The checkpoint of [section 4](#forced-day-and-the-checkpoint) should land **close to the expected discharge** and avoid
hours at 100%. The mechanism is the **high-SOC dwell cost** of the longevity plan (step 5 of
[battery-longevity.md](battery-longevity.md#4-steps)): a linear cost per kWh and hour of SOC above a soft threshold. It
applies **on the full-charge day as well**: the shortfall penalty still makes the checkpoint mandatory, and the dwell
cost then pushes the optimizer to reach 100% as late as the prices allow, and to leave it early. Until that change is
merged the checkpoint is placed at the end of the cheapest block as in the original text; the dwell is then a known cost
of the run, recorded in the decision row.

### A6. Answers to the open questions

Recorded in [section 13](#13-open-questions-for-the-owner): cadence (adaptive, A3) and bootstrap (A4) are answered; the
threshold scale, taper parameters and service-owned entities are unchanged and still open, and the homelab watchdog
stands as a prerequisite.

### A7. PR 3 scope

PR 3 ([section 12](#pr-split-3-prs-each-independently-reviewable-feature-off-no-behaviour-change-when-merged)) is
extended; the rest of its content is unchanged:

- the **adaptive cadence** (A3): `cadence` table, comparable-reading count and alert logic in `service/full_charge.py`,
  the owner alert, the `threshold(d)` form above, the `unknown` bootstrap and `enabled_at` (A4);
- the **success criterion** (A1): `last_balance_read` advance detection against the value at arming, outcome
  `reached_unmeasured` as a failed attempt, `last_full_source: balance_reading`, no `L` from SOC 99 alone;
- the **settle hold** (A2) in the actuator and the directive, `settle_timeout_s`, disarm after the hold;
- **balance telemetry consumed** from the read-only battery-health telemetry PR (step 3 of the longevity plan):
  `balance_status`, `cell_delta`, `delta_trend`, `delta_avg_4w`, `last_balance_read` come from the entity store that PR
  fills; PR 3 does not add a second reader;
- `config.py`, `config/service.yaml`, `docs/service.md` and `docs/configuration.md` for the keys above; tests for every
  row of the A1 and A3 tables, the settle hold (reading arrives, timeout, guard failure during the hold, restart during
  the hold), and a 40-day synthetic replay that checks the cadence stays within the active row.

Then, as before: at least 7 days of `dry_run`, a supervised first live run, and the homelab watchdog follow-up
([section 12](#follow-up-for-the-homelab-repository-not-changed-here)).

### PR 3 implementation notes

Where the code differs from the text above (the code is what the tests cover):

- **States.** The six states are `idle`, `scheduled`, `armed`, `charging`, `reached`, `settling`. `done`, `failed`
  and `cancelled` are outcomes (`runs_total{outcome}`: `done`, `done_simulated`, `failed_never_armed`,
  `failed_unacknowledged`, `failed_never_reached`, `failed_reached_unmeasured`, `cancelled`), recorded when the
  machine returns to `idle` in the same step; `reached_timeout` is gone (a timeout starts the settle hold, and the
  reading decides). One attempt per local day.
- **Arming** waits for local midnight of the run's day (not one slot before it), because the merged actuator refuses
  an arm whose weekday is not the local weekday of the tick (`not_today`). The acknowledgement clock starts when the
  actuator reports the arm driven, so a guard-failure safe idle does not time it out.
- **Class precedence** is worst first (alert, yellow, green, unknown), so two orange readings alert even with fewer
  than four comparable readings, and a yellow reading selects 7/14 even before four readings exist. The green class
  also needs a known, not rising `delta_trend` (Omnibattery: `rising`, `stable`, `falling`) and a latest reading no
  older than the green row's `max_days`.
- **Bootstrap `d`**: with no `L`, `d` is the `unknown` row's `min` until that many days have passed since
  `enabled_at`, then the days since enablement (forced at `max`).
- **Balance sensors** are `full_charge.entities` (three more than the seven of PR 2: `balance_status`, `delta_trend`,
  `delta_avg_4w`), read from the entity store; `battery_health.entities` may name the same entities (one store entry
  each) and the two sections must agree. No second reader and no dependency on `battery_health`.
- **Pause** resets the failure count when it starts; a new valid reading clears the count and the pause.
- **Writer separation** works in both directions: a state of the other writer is discarded.
- **Evaluation** is its own worker job after the regular plan is published; a target that misses by more than
  0.05 kWh is declined for the day; the decision is sticky once an evaluation was obtained.
- **Feature mode (coordinator request after review).** `full_charge.mode: shadow | live` (default `shadow`;
  `live` needs the service `mode: live`) is the feature's own switch, so the installation can stay `live` for normal
  battery control while the feature only simulates. Service `live` with the feature `shadow`: the simulated writer and
  a simulated state file, but the regular plan stays the plan of record (never commit mode), the actuator is built
  without the feature and receives no directive, and the guard does not admit the two entities. Going from feature
  `shadow` to `live` discards the simulated state (the first live run is eligible at once). In service `shadow` and
  `dry_run` the feature behaves as before. This replaces the earlier reading of "`dry_run`: the simulated writer"
  as the only way to study the decisions on the installation. `/status` has `full_charge.mode` and the metric
  `full_charge_mode{mode}`.
- **Startup checks**: Home Assistant's `time_zone` (REST `get_config`) and the weekly-entity contract suspend the
  feature (no directive: hands off) and alert; the entity check self-heals, the time-zone check retries every 60 s.

## 1. Goal and owner decisions

The Marstek Venus E V3 (5.12 kWh, LFP) needs a charge to 100% from time to time so that its BMS balances the cells at
the top of the charge. The planner works in a 20 to 95% window (`config/tariff.yaml`), so left alone the battery never
gets there. Decisions of the owner (final, 2026-11-04):

1. **No fixed weekday or hour.** The full charge happens **at least every 15 days and not more often than every 5
   days**, on a day the optimizer picks from the prices. *Superseded 2026-11-04: the bounds are adaptive,
   [A3](#a3-adaptive-cadence).*
2. **Rule (proposed by the owner, refined in [section 4](#4-decision-rule)):** count the days since the last 100%.
   Below 5: never. From 5 to 14: when the next day's prices are published, solve that day twice (normal, and "reach
   100% that day"); if the extra cost is under a threshold that grows from small at day 5 to unbounded at day 15, that
   day is the full-charge day. At 15 or more: force it on the next priced day, still in the cheapest slots.
3. **On the chosen day** the planner's SOC ceiling is lifted to 100% with a **soft** target to reach it: the plan never
   becomes infeasible.
4. **Mechanism ("option A"):** Omnibattery's own weekly-full-charge feature is the enabler and is self-limiting. In
   `mode: live` only, the service turns on `switch.omnibattery_weekly_full_charge_enabled` and selects the current
   weekday in `select.omnibattery_weekly_full_charge_day` before the first charge slot of the full-charge day, and turns
   the switch off after completion. Omnibattery lifts its maximum SOC to 100% (software-enforced on the V3), runs its
   top-voltage taper and completion detection (where the balancing happens), restores the configured 95% on
   completion and clears the run at its day rollover. If the optimizer dies, the worst case is a repeat on that
   weekday. The owner approved widening the `ha_guard` allowlist for this (and only this).
5. **Default off.** A service-config option; in `shadow` and `dry_run` the new writes are recorded, never sent, exactly
   like the existing writes.

Out of scope: any other Omnibattery setting, Modbus, a price or weather forecast for unpublished days, active
balancing hardware, and changes to the homelab repository.

## 2. Omnibattery behaviour (read from the installed source, v1.5.1b1, 2026-11-04)

Read-only study of `control/weekly_full_charge.py`, `__init__.py`, `switch.py`, `select.py`, `sensor.py` and
`control/max_soc_charge.py` of the integration as installed. Nothing was written to Home Assistant.

| Question | Finding | Consequence for the design |
|---|---|---|
| **Does toggling the switch or changing the select reload the integration?** | **No.** `WeeklyFullChargeSwitch._set_enabled` and `WeeklyFullChargeDaySelect.async_select_option` call `hass.config_entries.async_update_entry(...)`. The entry's registered update listener (`_async_update_listener`) is a hot reload of parameters: it calls `controller.update_pd_parameters()` (re-reads the weekly fields among others), re-checks the predictive balance fingerprint (not affected by these two keys), and saves a config backup file. There is no `async_reload`/`async_schedule_reload` on this path. The PD loop, the coordinators and the Modbus connections are not touched | Arming and disarming does not interrupt control. Each write costs one config-entry update and one backup write in HA, so the service writes only on edges, never per tick |
| Timezone of the day logic | `is_active()` uses naive `datetime.now()`; the HA container runs `TZ=Europe/Madrid` (`date` printed CET). The service's `timezone` is also `Europe/Madrid`, so the weekday Omnibattery sees is the service's local weekday, DST included | The service writes `weekday(local date of the run)`. Startup check: HA's `get_config` `time_zone` must equal the service timezone, else the feature stays disabled with an alert (the container `TZ` itself is not observable) |
| Day logic | Active only when enabled **and** today is the selected weekday **and** the run is not complete. At the first call after leaving the target day it clears `complete`, `registers_written` and the status. Changing the select or turning the switch off also clears them (`update_pd_parameters`: `day_changed or feature_disabled`) | Select first, then switch on, never the reverse (a select change while a run is active would be a mid-run abort). Turning the switch off after completion is safe and also resets the completion flag, so the next run, on any weekday, starts clean. |
| What 100% means on the V3 | `capabilities.hardware_soc_cutoff` is false for the V3 (`charging_cutoff_capacity: None` in `const/registers_common.py`): "software enforcement". `handle_registers()` writes no register; the ceiling is `_effective_charge_max_soc()` returning 100 with source `weekly_full_charge` while `_weekly_full_charge_unlocked()`. `number.venus_charging_cutoff_capacity` (95) is untouched and nothing needs restoring | No register state can be left behind by a crash on this hardware |
| Completion | `handle_registers()` calls `_complete_weekly_charge("all_batteries_full")` when `is_battery_full()` holds for every battery: SOC reads 100 (immediately, for a V3: the "keep charging until BMS cutoff" extension is for Venus A/D only), **or** the debounced BMS cutoff (5 cycles of about 2 s: SOC at least 99 or a cell in the taper zone, charge commanded above 10 W, power at most 10 W, inverter in Standby). Completion restores the configured limits, re-enables the charge hysteresis and records a post-cutoff cell-delta measurement. The 60 s delta-V measurement is diagnostic and no longer gates completion | Completion needs **a charge being commanded** until Omnibattery says so: the actuator must not stop charging at a modelled 95% or at SOC 99 (see [section 6](#6-planner-and-actuator-behaviour-near-the-top)) |
| Taper | With `switch.venus_full_charge_voltage_taper` on (it is), once any cell reaches 3.48 V the charge limit is capped at `NORMAL_BALANCE_CHARGE_POWER_W` = **200 W** until the cell falls below 3.44 V. It is voltage-latched, not SOC-based | The last stretch is slow and its start is unknown in SOC terms: the planner needs a taper model with a parameter to measure ([section 5](#5-lp-formulation))|
| **Completion-status entity** | The weekly status sensor (`sensor.omnibattery_weekly_full_charge_status`: `idle`/`disabled`/`charging`/`complete`, attributes `completion_reason` and per-battery `soc_at_completion`) is created **only if the weekly feature is enabled when the integration loads** (`sensor.py` platform setup). It is **not in this installation's entity registry**, and toggling the switch does not reload, so the sensor will not appear | **Do not depend on it.** Read-only completion signals below |
| Signals that do exist | `sensor.omnibattery_integration_status` is `weekly_full_charge` exactly while the run's state is "Charging to 100%" (priority 3, above `charging`/`discharging`), and returns to the normal values at completion. `sensor.venus_last_balance_read` (timestamp) advances when the post-completion top-balance reading is recorded; `binary_sensor.venus_balancing_mode` (BMS register 34009) is on while the BMS balances; `sensor.venus_cell_delta` (mV) and `sensor.venus_balance_status` give the result. SOC: `sensor.venus_battery_soc` | Run start is acknowledged by `integration_status == weekly_full_charge`; completion is that status **leaving** after SOC peaked at 97% or more (or SOC reads 100). The balance sensors are recorded as evidence, never as gates |
| **Guard conflict** | ADR-0007 holds safe idle unless `integration_status` is in `healthy_states` (`balanced`, `charging`, `discharging`). During a run it is `weekly_full_charge`, so **the unmodified guard would hold safe idle for the whole run and the battery would never charge** | The guard accepts `weekly_full_charge` only while the service itself has armed a run for today ([section 6](#6-planner-and-actuator-behaviour-near-the-top)) |
| `switch.omnibattery_weekly_full_charge_delay` | Semantics are inverted from the name: **on** = the weekly run respects the solar charge delay (waits for solar), **off** = it bypasses the delay (`_weekly_full_charge_skip_delay`). It matters only while the main charge delay (`switch.omnibattery_charge_delay`) is on. There is no PV on this site, so a held run could wait for the rest of the day | Currently off and must stay off. Read-only check; arming is refused and an alert raised if it is on |
| Persistence in HA | The two switch/select values live in the config entry (survive an HA restart). Run state (`complete`, `registers_written`) is stored per calendar date and restored only if saved the same day | An HA restart mid-run resumes the run; one after midnight clears it |
| "Force full charge" button | Mentioned in comments, **does not exist** as an entity in 1.5.1b1 (`_force_full_charge` is never set from an entity) | Not used |

The contract above is verified against **1.5.1b1**, a beta. At startup the service checks that the switch and the select
exist and that the select's `options` attribute contains the seven weekday values; otherwise the feature disables itself
with an alert. Re-verify after every Omnibattery upgrade (the regime-log row of [section 11](#11-component-records-and-regime-log)
asks for it).

## 3. Prices, forecasts and when the decision can be made

| Input | Availability in this service | Use |
|---|---|---|
| PVPC today | Always, `service/pvpc.py` | Plans |
| **PVPC tomorrow** | `price_next_day_HHh` appears from about 20:00 to 20:15 local (the passive archive `pvpc_snapshots` records the true instants). The live horizon ends at the last published slot (`max_horizon_slots` 192 caps it): about 96 slots until then, about 110 to 112 slots at 20:30 | **The decision input.** Decide when `PriceCurve.tomorrow_published` first holds, about 3.5 h before the day starts |
| OMIE day-ahead for tomorrow | Public from about 13:00; the offline estimators `omie_outlook.py` (PO-C, gate GC passed) are **not wired into the live service** (its horizon is published-only) | Not used. It would only move the decision from about 20:15 to about 14:00, which buys nothing because arming happens on the day itself, and an estimated price must never decide a physical action |
| Prices two or more days ahead | Do not exist | A "cheapest day among the next three" rule is impossible; that is why the rule is a sequential threshold |
| Load forecast | `trailing_mean`, 28 days, used by every plan | Same for both solves |
| Weather forecast | Passive archive only; no PV, and the load forecaster does not use it | Not used |

Recommendation: no price or weather forecast in version 1. Use the published next-day prices and the trailing-mean
load, i.e. the same snapshot every other plan uses. The `dry_run` records ([section 9](#9-observability))
let a later offline study check whether waiting one more day would often have been much cheaper.

## 4. Decision rule

### Terms

- `L`: the local date of the last full charge (state, [section 7](#7-state-persistence)). **A full charge is recorded
  when the battery SOC is observed at 99% or more** (any time, including a manual one by the owner) **or** Omnibattery
  completes a run, whichever is first. `reached_soc_pct` is 99. *Superseded 2026-11-04: only a new valid balance
  measurement counts, [A1](#a1-success-criterion-a-new-valid-balance-measurement); 99% alone is "reached, unmeasured".*
- `D`: the candidate day, normally tomorrow. `d = (D - L)` in calendar days.

### Evaluation (once per candidate day)

*The 5 and 15 below are the original fixed bounds; since 2026-11-04 they are the `min` and `max` of the active cadence
row, [A3](#a3-adaptive-cadence).*

Run in the first solve in which `D` is fully published and no decision exists for `D`; retried at later solves of the
evening if the inputs were not ready; sticky afterwards (a decision for `D` is never revisited, except cancellation, see
[section 10](#10-failure-modes)).

| `d` | Action |
|---|---|
| below `min_interval_days` (5) | Nothing. No solve is made |
| 5 to 14 | Three solves on the same snapshot: **normal** (ceiling 95%), **lifted** (ceiling 100% on `D`, no target) and **target** (ceiling 100% on `D`, soft checkpoint). `forcing_cost = obj(target) - obj(lifted)`. Accept `D` when `forcing_cost + fixed_cost_eur <= threshold(d)` |
| 15 or more (`max_interval_days`) | **Force**: `D` is the full-charge day. Same target solve, no threshold. The charge still lands in the cheapest slots because the checkpoint is chosen by the optimizer |
| No decision reached for `D` by local midnight and `d` is already 15 or more (service down, prices missing) | **Overdue fallback**: decide for *today* with today's remaining published slots, forced. The cadence bound is honoured whenever the service is up and prices exist |

`threshold(d) = threshold_min_eur + threshold_scale_eur * (d - 5) / (15 - d)` for `5 <= d <= 14`: it equals
`threshold_min_eur` at day 5, rises slowly, and grows without bound as `d` approaches 15 (day 15 is the forced case).
With the proposed defaults (0.01 EUR each, EUR per run, pre-tax, the objective's own unit): 0.010, 0.011, 0.013, 0.014,
0.017, 0.020, 0.025, 0.033, 0.050, 0.100 for `d` = 5 to 14. Defaults are provisional and are set from the `dry_run`
distribution of `forcing_cost`.

### Why `forcing_cost` is measured against the *lifted* plan, not the normal plan (refinement)

Lifting the ceiling also lets the free plan store 0.26 kWh more at the end of the horizon, and the objective credits the
end SOC at the terminal value (`planning.terminal_value`, the 28-day mean price). Measured on synthetic 2.0TD-like
prices and loads (a throwaway experiment, not committed; RTE 0.80): over 80 random evening decisions with a terminal
value of 0.11 EUR/kWh, `obj(target) - obj(normal)` was **negative in every case** (-0.024 to -0.009 EUR), because of
that credit, while against the lifted free plan the forcing cost was 0.000 EUR (-0.0001 to 0.0006): a plan that values
stored energy above the cheapest charge price already fills the battery. A second sweep with the 0.01 EUR/kWh throughput
cost (60 cases per value) gave the same at 0.08 and 0.11 EUR/kWh, and a positive forcing cost at a terminal value of
0.05 EUR/kWh (median 0.000, p90 0.068, max 0.088 EUR). So `forcing_cost` isolates the price of the constraint, while
`target - normal` is reported next to it as a diagnostic only. The honest consequence is a **warning for the owner**:
**with the shipped objective the extra cost is usually about zero, so the rule will normally fire at day 5 (a cadence of
5 to 6 days)**. If that is more often than wanted, raise `min_interval_days` (owner-set), or set `fixed_cost_eur` (a
per-run wear or time-at-100% cost, default 0) so that the threshold actually discriminates between days. See
[open questions](#13-open-questions-for-the-owner).

### Forced day and the checkpoint

On any chosen day the **checkpoint** (the slot at whose end SOC must be 100%) is chosen by the optimizer among the
slots of `D` that leave at least `completion_margin_slots` (2, i.e. 30 min) before local midnight, so Omnibattery has
time to detect completion before its day rolls over. Because the target solve minimises cost, the checkpoint falls at the
end of the cheapest charging block, and the battery sits at the top as short a time as the prices allow.

## 5. LP formulation

The existing kernel is already a MILP (`oracle.build_model`: continuous `charge`, `discharge`, `soc` plus one binary
`mode` per slot, HiGHS through `scipy.optimize.milp`). "SOC reaches 100% at *some* slot of day `D`" is `max_t soc[t] >=
C` over a window: a reverse-convex (disjunctive) constraint, not expressible in a pure LP. The continuous relaxation
`soc[t] >= C*w[t]`, `sum w = 1` is vacuous (`w` splits across slots).

### Options evaluated

| Option | Verdict |
|---|---|
| **A. Checkpoint enumeration**: fix `t*`, add `soc[t*] + s >= C`, solve once per candidate slot, keep the best | Exact, trivially correct, convex per solve, and the cost-versus-`t*` curve is a nice diagnostic. Measured 1.4 to 2.8 s for the 96 candidates of a day (14 to 29 ms each). Needs a loop in the worker thread and 96 plan objects to discard |
| **B. One MILP with checkpoint binaries** `z[t]`, `sum z = 1`, `soc[t] >= C - M(1 - z[t]) - s` | Same optimum as A (all spot checks equal to 1e-6), one call, one plan, uses the solver and the code path the service already has. Measured median 38 ms, p95 0.55 s, max 1.49 s over 60 random cases of 4 to 192 slots (the normal solve: median 11 ms, p95 62 ms, max 114 ms), against a 20 s `solver_time_limit_s` |
| C. Deadline slot: require `soc[t_d] >= C` at a fixed `t_d` (end of the day, or end of the cheap block by a rule) | Convex and fastest, but a fixed deadline is not a cost-optimal choice: at the end of day it cost **+0.09 to +0.20 EUR** more than the free checkpoint in the same cases, and any rule for `t_d` re-implements the optimizer badly |
| D. Relax to LP (drop `mode` binaries) for the decision | Rejected: the binaries are what stop charge and discharge in the same slot at negative prices; the decision must price the real problem |

### Recommendation: **B, one MILP with checkpoint binaries and a soft shortfall**

Added to the existing model (`oracle.build_model` gains optional arguments; with none given the model is byte-identical
to today's, a test):

- **Per-slot SOC ceiling.** `ub[soc[t]] = 0.95*C` for slots before the window, `1.0*C` from the window start to the end
  of the horizon (a ceiling that dropped back at midnight would make a plan that is at 100% at 24:00 infeasible, since
  discharge is bounded by the load). The kernel recheck in `planner.run_planner` and `dispatch.intents_from_schedule`
  uses a spec with `soc_max_frac = 1.0`, which is looser than the LP and therefore only validates.
- **Checkpoint.** Binaries `z[t]` for the eligible slots `t` of `D` (at most 96), `sum z = 1`, and a continuous
  shortfall `s >= 0` (kWh): `soc[t] + s >= C_target - M*(1 - z[t])` with `M = C_target - floor`.
- **Objective.** `+ lambda * s`, `lambda = shortfall_penalty_eur_per_kwh` (10 EUR/kWh, two orders of magnitude above any
  price). `s = 0` whenever the target is reachable; otherwise the plan gets as high as it can and `s` is reported. The
  model is **always feasible**, which is what "soft" must mean here (measured: 1.5 h left from 20% gives 0.74 kWh of
  shortfall and a 85.5% peak, not an error).
- **Taper model (convex).** The V3 charges at most about 200 W once a cell latches at 3.48 V. A step in SOC would be
  non-convex, so it is modelled by one linear half-space per slot, `charge[t] <= h*(p_tap + (P - p_tap)*(C - soc[t-1]) /
  (C*(1 - x0)))`, a ramp from the full power `P` at `x0 = taper.start_soc_frac` down to `p_tap = taper.power_kw` at 100%.
  It is conservative against the real step, keeps the model a MILP, and (measured) still reaches 100% from 20% within 3 h.
  `x0` and `p_tap` are **provisional** (0.90 and 0.2 kW) and are calibrated on the first supervised run
  ([section 11](#11-component-records-and-regime-log)); `taper.start_soc_frac: null` disables the rows.
- **Evaluation mode** (`FullChargeSpec.mode = "evaluate"`) runs the three solves of [section 4](#4-decision-rule) in the
  worker thread, after the regular plan is solved and published exactly as today; any failure (infeasible, no incumbent,
  timeout, exception) is recorded and skips the decision for this solve. The regular plan is **never** delayed or
  replaced by an evaluation. **Commit mode** (the day is chosen) makes the target solve the plan of record from the
  decision on, with the checkpoint re-optimised at every replan from the measured SOC; the target stays in force until
  completion is observed or the day ends, never lowered mid-run (`lambda` is not reduced).

### Two corrections to the plan the existing code makes

- **`clamp_soc`** clamps the measured SOC into `[soc_window_min, soc_window_max]`. It keeps that behaviour outside a run
  (a battery at 100% after balancing is planned as 95%: conservative by 0.26 kWh DC, corrected by every replan from the
  measured SOC). While a run is pending it clamps to the **lifted** maximum (1.0), otherwise a measured 98% would be
  planned as 95% and the plan would believe 5% was missing. After completion or at the day's end the clamp returns to
  0.95.
- **Terminal value** is unchanged. The lifted ceiling is the only change to the objective's feasible set.

## 6. Planner and actuator behaviour near the top

- The actuator has **no SOC-based stop** (ADR-0007 mapping: the intent of the slot decides). So the charge stops at a
  modelled 95% only if the plan says so; during a run the plan's ceiling is 100% and its target persists until
  **completion**, not until SOC 99. Measured SOC 99 with Omnibattery not complete leaves the LP a gap of about 0.05 kWh
  to the target, which it closes with a trickle in the cheapest remaining slot: that trickle is the commanded charge
  Omnibattery's BMS-cutoff detector needs. At SOC 100 a V3 completes at once.
- A slot's frozen intent can end up to one slot early relative to the taper; the replan at the next boundary continues.
- After completion the actuator holds idle until the balance reading arrives or `settle_timeout_s`
  ([A2](#a2-settle-hold-in-the-actuator)).
- `top_hold_timeout_min` (120): if SOC has been 99% or more for this long and Omnibattery has not completed (a BMS that
  stalls at 99), the service records the run as reached, disarms, and stops commanding the extra charge.
- **Guard.** `integration_status == "weekly_full_charge"` counts as healthy **only when** the actuator's directive says a
  run is armed for the current local day. Every other status keeps holding safe idle, exactly as today.
- **Writes** (new roles `full_charge_day`, `full_charge_enable`, in `WRITE_ROLES`, same readback, retry,
  `forbidden`-never-retried and dry_run simulation as every other role):
  - Arm, in this order: `select.select_option` to the weekday, then `switch.turn_on`, both before the `allow_charge`
    on-write of the same reconcile. Each is skipped when the readback already matches.
  - Disarm: `switch.turn_off`, in the restrictive block at the start of a reconcile.
  - Never rate limited; both write only on edges.
- **When to arm** (derived each tick from the committed plan until armed, then sticky): one slot (`arm_lead_slots`)
  before the first slot of the full-charge day in which the plan charges, **or** one slot before the plan's SOC would
  first exceed `arm_before_soc_frac` (0.93), whichever is earlier, never before one slot ahead of local midnight of the
  day. Omnibattery's 95% ceiling would otherwise stop the charge at 95% before the target.
- **When to disarm:** completion; reached plus `top_hold_timeout_min`; local midnight at the end of the day; arming not
  acknowledged within `ack_timeout_s` (90, three ticks: `integration_status` did not become `weekly_full_charge`); the
  kill switch's on-to-off edge and SIGTERM (best effort, inside `shutdown_timeout_s`, after the existing safe idle).
- **Refusals** (state `cancelled`, alert, no arming): `switch.omnibattery_weekly_full_charge_delay` is on or
  unreadable; the select's options are not the seven weekdays; HA's time zone differs from the service's; the kill
  switch is off.
- A guard-failure safe idle in the middle of a run does **not** disarm (it would only toggle HA config entries for
  nothing); the existing heartbeat watchdog and the homelab follow-up cover a dead service.

## 7. State persistence

The service already persists one mutable file, `service/v1/state/virtual_battery.json` (atomic replace, `0600`), plus
append-only JSONL under `service/v1/` partitioned by UTC day. This feature adds, in `service/sink.py`:

- **`service/v1/state/full_charge.json`** (mutable, same atomic write as the virtual battery), schema version 1:
  `last_full_at` (UTC), `last_full_source` (`soc_99`, `omnibattery_complete`, `bootstrap`), `last_full_local_date`,
  `state` (below), `run` (`day`, `weekday`, `decided_at`, `days_since`, `forced`, `forcing_cost_eur`, `threshold_eur`,
  `plan_id`, `armed_at`, `status_seen`, `soc_peak_pct`, `reached_at`, `completed_at`), `consecutive_failures`,
  `paused_until`, `writer_mode` (`live` or `simulated`).
- **`service/v1/full_charge/date=.../full_charge.jsonl`** (append-only): one row per decision, state transition and
  outcome, with the three solve objectives, the checkpoint slot, the shortfall, and the evidence sensors. `iter_records`
  gains the kind.

**Restart semantics** (compatible with ADR-0007 decision 7, *a restart never resumes a persisted plan*): the file holds
**knowledge and a calendar decision, not a plan and not a command**. After a restart the service learns `L` and that day
`D` was chosen, and re-derives the arming directive each tick from that state, the guard and a plan solved by *this*
process; the writes are idempotent (readback already matches, nothing is sent). A persisted `armed` state alone causes
no write. Corrupt, missing or unreadable file: bootstrap below.

**Bootstrap** (no state file; *superseded 2026-11-04 by [A4](#a4-first-run): eligible as soon as the feature is
enabled*): `L` is the first of (a) the latest time the SOC was at 99% or more in the Prometheus
history the service already reads (`prometheus.history_days`, 28), (b) `battery.commissioned_at` (2026-11-03), (c)
service start. With (b) the first eligible day is 2026-11-08 and the first forced one 2026-11-18. A configuration option
`bootstrap_last_full` (ISO date) overrides it.

**Non-live modes** (`shadow`, `dry_run`): the same file and records with `writer_mode: simulated`; a simulated run ends
as `done_simulated` at the end of its day and moves a simulated `L`, so the dry run reproduces the cadence and the cost
distribution without touching the real `last_full_at`. The first `live` start discards the simulated fields and
bootstraps the real `L`.

**State machine**

| State | Entered when | Leaves when |
|---|---|---|
| `idle` | start, or an earlier run ended | the rule picks a day: `scheduled` |
| `scheduled` | a decision for day `D` exists | arm condition met: `armed`; `D` ends: `failed(never_armed)`; refusal: `cancelled` |
| `armed` | select and switch confirmed by readback (live) or simulated (dry_run) | status `weekly_full_charge` seen: `charging`; not within `ack_timeout_s`: disarm, `failed(unacknowledged)` |
| `charging` | status seen | SOC at least 99%: `reached` (stamps `L`); status left with peak SOC 97 or more: `done`; day over: `failed(never_reached)` |
| `reached` | SOC at 99% or more | status left, or SOC 100: `done`; `top_hold_timeout_min`: `done(timeout)` |
| `done` | completion | disarm confirmed: `idle`. `L` is already stamped |
| `failed` / `cancelled` | as above | `consecutive_failures` incremented (failed only); at 3, `paused_until = +5 days` (owner alert); next evening re-evaluated |

## 8. ADR-0007 amendment text and AGENTS.md edit

Made in the implementation PR (PR 2). Append to `docs/adr/0007-live-battery-control.md`:

> ## Amendment 2026-11-04 (periodic full charge)
>
> Decision 3's allowlist is widened by **exactly two entries, and only when `full_charge.enabled` is true in
> `mode: live`**; nothing else changes (kill switch, heartbeat, mode gating, readback, retry, `forbidden` never retried,
> one entity per call, no `return_response`, REST GET-only):
>
> | Service | Entity | `service_data` |
> |---|---|---|
> | `switch.turn_on` / `switch.turn_off` | `switch.omnibattery_weekly_full_charge_enabled` | none |
> | `select.select_option` | `select.omnibattery_weekly_full_charge_day` | exactly `{"option": <one of monday, tuesday, wednesday, thursday, friday, saturday, sunday>}` |
>
> Purpose: Omnibattery's own weekly full charge is used as a self-limiting enabler of a periodic charge to 100% for cell
> balancing ([plan](../planning/periodic-full-charge.md)). Omnibattery lifts its maximum SOC to 100%, completes on its
> own detection and restores the configured 95%; it clears the run at its day rollover, so if the optimizer dies the
> worst case is a repeat on that weekday. Not allowlisted, and never to be: `switch.omnibattery_weekly_full_charge_delay`
> and `switch.omnibattery_charge_delay` (read only), the charge-cutoff numbers, any button, and any other weekly-charge
> entity. Decision 6 gains one healthy status: `weekly_full_charge`, accepted only while the actuator has armed a run for
> the current local day. The service owns the two entities while the feature is enabled (it turns the switch off
> whenever no run is armed). Decision 7 is unchanged: the persisted full-charge state is a calendar decision and a
> timestamp, never a plan. A homelab watchdog follow-up turns the switch off on heartbeat loss.

`AGENTS.md`, "Privacy and safety": after the list of allowed service calls add "`switch.turn_on/off` on the Omnibattery
weekly-full-charge switch and `select.select_option` on its day select (seven weekday values), only with
`full_charge.enabled`". `ha_guard.py`'s docstring and `docs/service.md`'s live-control section say the same.

### `ha_guard` changes (exact)

- `ACTUATION_SERVICES` gains `("select", "select_option")`.
- `ActuationAllowlist` gains `selects: Mapping[str, frozenset[str]]` (entity to its permitted options), validated in
  `__post_init__` like the others (entity ID shape, `select.` prefix, options non-empty strings). The weekday tuple
  `WEEKDAY_OPTIONS = ("monday", ..., "sunday")` is a constant of `ha_guard`, copied verbatim from the integration's
  select; it is not configurable.
- `check_actuation_call`: for `select`, one entity in `allowlist.selects`, `service_data` exactly `{"option": <str in
  that entity's set>}`; `Sunday`, `sun`, `""`, a list, an extra key, or `return_response` raise.
- The switch entity is added to `allowlist.switches` **only** when the feature is enabled; the select is added to
  `selects` only then. `actuation_allowlist(config)` stays `None` unless `mode: live`.
- Tests assert, with the feature off, that both entities raise `ForbiddenHACallError` in every mode, and, with it on,
  that no other entity of the same domains passes.

## 9. Observability

**Logs** (INFO): `full-charge decision day=<date> d=<n> forced=<bool> forcing_cost=<eur> vs_normal=<eur> threshold=<eur>
checkpoint=<slot> shortfall=<kwh>`; each state transition with its reason; arm, disarm and acknowledgement; `reached`
with the SOC and elapsed time; refusals at WARNING.

**`GET /status`**: a `full_charge` block (GET only): `enabled`, `state`, `last_full_at`, `days_since`, `next_eligible_day`,
`forced_after_day`, `run` (day, armed, status seen, SOC peak, checkpoint, shortfall), `last_decision`, `consecutive_failures`,
`paused_until`, `alerts`, and the read-only evidence (delay switch, `last_balance_read`, `balancing_mode`, `cell_delta`).

**Metrics** (`battery_optimizer_full_charge_*`, only with the feature enabled, old prefix kept per AGENTS.md):
`state{state}` (one-hot), `days_since_last` and `last_timestamp_seconds`, `scheduled_day_timestamp_seconds`,
`forcing_cost_eur` and `threshold_eur` (last decision), `runs_total{outcome}` (`done`, `reached_timeout`,
`failed_never_armed`, `failed_unacknowledged`, `failed_never_reached`, `cancelled`), `soc_peak_percent`, `alert{reason}`
(`delay_switch_on`, `timezone_mismatch`, `entities_missing`, `never_reached`, `unacknowledged`, `paused`,
`state_unwritable`), and the actuator's existing `writes_total{role,result}` for the two new roles. Suggested homelab
alerts: `days_since_last > 14.5`, `alert == 1`.

**Records**: [section 7](#7-state-persistence). The `actuation/` rows already record would-be and sent writes.

## 10. Failure modes

| Situation | Behaviour |
|---|---|
| **Optimizer down mid-run** | Omnibattery keeps its run (max SOC 100%, completion detection) but charges only while the direction switches allow it; the HA watchdog turns them off after 5 minutes without a heartbeat. The weekly switch stays on until the next start: the service then sees the switch on with `state` and the run, re-derives the directive and either continues (same day) or turns it off (day over). Without the follow-up watchdog change, the worst case is a repeat on that weekday |
| **HA restart mid-run** | The service goes through `ha_not_synced` safe idle (existing). Omnibattery restores the same-day run state and re-asserts the ceiling. After the resync the directive is re-derived, writes are idempotent. If completion happened during the outage, the evidence is `integration_status` not `weekly_full_charge` with the persisted `soc_peak_pct` at 97 or more: `done` |
| **Day rollover mid-charge** | Omnibattery exits the target day and clears the run (V3: nothing to restore). The service disarms at its first tick of the new day and records `failed(never_reached)` unless SOC reached 99% earlier (then `reached`/`done`). The checkpoint is never later than 30 min before midnight to make this rare |
| **Price data missing** | Evening decision: no solve, retried at later solves. If still undecided at midnight with `d` at 15 or more: overdue fallback for today. With `d` below 15: nothing, and the next evening decides. A run already committed continues on the prices in hand; losing today's prices mid-run is the existing `price_stale` safe idle |
| **Delay switch on** (or unreadable) | Refuse to arm, state `cancelled`, alert `delay_switch_on`. Never written by the service. A forced day that is cancelled is retried the next evening |
| **SOC never reaches 100** | Two cases: the BMS cut at 98 to 99% (Omnibattery's debounced cutoff completes the run: `done`), or it never got there (`failed(never_reached)`, `L` unchanged, `consecutive_failures + 1`). Three in a row pause automatic selection for five days and raise `alert{paused}`; the owner investigates. A manual charge to 99% resets everything (`L` is stamped from the observed SOC) |
| Arming not acknowledged | `integration_status` did not become `weekly_full_charge` within `ack_timeout_s` (select not matching the local weekday, delay on, manual mode, backup mode): disarm, `failed(unacknowledged)`, alert |
| Kill switch off during a run | The existing on-to-off safe idle, then one best-effort `turn_off` of the weekly switch; nothing else is written. State `cancelled` |
| State file unwritable / corrupt | Counters and last full charge keep in memory; alert `state_unwritable`; a corrupt file is renamed aside and the bootstrap runs. The worst outcome is one extra full charge |
| Two services or the owner flips the switch by hand | The service reconciles the switch to its directive on every tick in `live` (it owns the entity while enabled) |
| Omnibattery upgrade changes entity names or options | The startup check disables the feature and alerts; no write is attempted |
| Plan infeasible | Cannot happen by construction (soft target); the existing `OracleInfeasibleError` handling is unchanged |

## 11. Component records and regime log

Required by AGENTS.md in the implementation PRs (this plan PR changes none of them):

- **Regime log** (`docs/components/regime-log.md`): a `planned` row now (periodic full charge enabled, date to fill in),
  turned into dated rows at each event: code merged with the feature off (code, no plant change); config switched to
  `dry_run` with the feature on; Omnibattery weekly feature owned by the service (first supervised live run); each
  Omnibattery upgrade re-verification. Evidence column: `deploy` or `observation`.
- **`battery-model.md`**: assumptions gain "SOC window 20 to 95% except on a chosen full-charge day, when the
  ceiling is 100%", the taper parameters (`start_soc_frac`, `power_kw`) as **provisional, measured on the first run**, and
  the charge limit of 200 W once a cell latches at 3.48 V. Recalibrate by the first supervised run: record SOC, AC power
  and (if exposed) the maximum cell voltage every 30 s from 85% to completion; set `x0` to the SOC at which AC power
  first falls under 400 W and `p_tap` to its steady value. Status stays `stale` (already so since 2026-11-03), with the
  reason extended.
- **`battery-optimizer.md`**: assumption "the objective is unchanged; its feasible set has a day with a lifted ceiling
  and a soft checkpoint"; "invalidated when" gains an Omnibattery upgrade (weekly-feature contract), a change of
  `tariff.yaml`'s window, and a different charge-delay configuration; recalibration is the existing benchmark plus the
  `dry_run` decision study. Status reason extended; no new benchmark claim is made, since the benchmark ladder does not
  model the full-charge day.
- **`tariff-prices.md`**: one assumption: the decision depends on the next-day publication time (about 20:15), measured
  by the `pvpc_snapshots` archive.
- **`docs/service.md`, `docs/configuration.md`, `config/service.yaml`, `README.md`/`docs/README.md` index, `known-issues.md`**
  (the missing weekly-status entity and the guard conflict): in the PR that makes each change.

Privacy: relative figures and parameters only; no costs, savings or absolute energy in the repository.

## 12. Configuration, tests, rollout and PR split

### Config schema (`config/service.yaml`, parsed in `service/config.py`; **off by default**)

```yaml
full_charge:
  enabled: false                      # master switch; false: no decision, no state, no new entity read or write
  # min_interval_days / max_interval_days (5 / 15, as merged in PR 2) are replaced by the `cadence` table
  # of the 2026-11-04 amendment (A3), edited in PR 3
  min_interval_days: 5                # never before
  max_interval_days: 15               # forced on the next priced day
  threshold_min_eur: 0.01             # day-5 threshold, EUR per run (pre-tax, objective unit)
  threshold_scale_eur: 0.01           # hyperbolic growth towards day 15
  fixed_cost_eur: 0.0                 # added to the forcing cost (wear / time at 100%); tune spacing
  target_soc_frac: 1.0
  reached_soc_pct: 99                 # an observed SOC at or above counts as a full charge
  shortfall_penalty_eur_per_kwh: 10.0
  completion_margin_slots: 2          # checkpoint at least this many slots before local midnight
  arm_lead_slots: 1
  arm_before_soc_frac: 0.93
  ack_timeout_s: 90
  top_hold_timeout_min: 120
  taper: {start_soc_frac: 0.90, power_kw: 0.2}   # provisional; start_soc_frac: null disables the rows
  max_consecutive_failures: 3
  failure_pause_days: 5
  bootstrap_last_full: null           # ISO date; null: Prometheus history, then battery.commissioned_at
  entities:                           # roles; required when enabled
    enable_switch: switch.omnibattery_weekly_full_charge_enabled   # written (live only)
    day_select: select.omnibattery_weekly_full_charge_day          # written (live only)
    delay_switch: switch.omnibattery_weekly_full_charge_delay      # read only, must be off
    charge_delay_switch: switch.omnibattery_charge_delay           # read only, logged
    last_balance_read: sensor.venus_last_balance_read              # read only, evidence
    balancing_mode: binary_sensor.venus_balancing_mode             # read only, evidence
    cell_delta: sensor.venus_cell_delta                            # read only, evidence
```

Validation (`ConfigError`, like the existing sections): `0 < min_interval_days < max_interval_days <= 30`; thresholds,
fixed cost and penalty finite and non-negative (penalty positive); `0 < arm_before_soc_frac < target_soc_frac <= 1`;
`taper.start_soc_frac` in `(0.5, 1)` and `power_kw` in `(0, actuation.max_discharge_cap_w/1000]` or null; with
`enabled: true`: all `entities` set, `planning.soc_source: entity`, `actuation.entities.integration_status` and
`battery_soc` set. `enabled` works in every mode (shadow: decisions and plans recorded, no actuator; dry_run: plus the
would-be writes; live: sends). The two writable roles join the allowlist only when `enabled` and `live`.

### Test plan

- **Solver (PR 1):** byte-identical model and plans when no `FullChargeSpec` is given (golden); lifted ceiling is per
  slot; checkpoint MILP equals brute-force enumeration over the checkpoint on random cases (property test, sizes 4 to 192);
  soft target is always feasible (window too short, SOC too low, price spikes), reports the shortfall, and never raises;
  taper rows bind and still reach 100%; `terminal_mode` untouched; kernel recheck passes with `soc_max_frac = 1.0`;
  solve-time regression bound (the 60-case median stays under a generous multiple of the normal solve); `clamp_soc`
  lifted only while pending; `inputs_hash` unchanged when absent.
- **Guard and actuator (PR 2):** allowlist matrix above (all seven options pass, `sun`, `Sunday`, `""`, list, extra key,
  `return_response`, a second entity, the delay switch, the charge-delay switch, the cutoff numbers raise; nothing passes
  with the feature off or in shadow/dry_run); write ordering (select, enable, then `allow_charge`; disarm first);
  readback and retry like every role, `forbidden` never retried; `dry_run` simulates the two entities and records
  without sending; `weekly_full_charge` healthy only when armed (and every other status still holds safe idle);
  disarm on kill switch edge and SIGTERM; delay switch on refuses; config validation matrix; `service.yaml` committed
  default has the feature off.
- **Scheduler (PR 3):** threshold function (monotone, `threshold(5)` equal to the minimum, diverges at 15); the rule table
  for `d` = 0 to 20 including the overdue fallback; state-machine table (every transition and its reason); restart in
  every state (no write from persisted state alone, directive re-derived, idempotent); corrupt and missing state;
  bootstrap precedence; SOC at 99% outside a run stamps `L`; completion by status leaving versus peak below 97 (aborted);
  day rollover; simulated writer separation and the discard at the first live start; price-missing retry; a replay of 40
  synthetic days that checks cadence is within [5, 15] and no overlap of runs; `/status` and metric label sets.
- **Acceptance outside CI:** `dry_run` for at least 7 days on the real installation (decisions, `forcing_cost`
  distribution, planned arming writes, no sends); then one **supervised live run** with the kill switch and the owner
  present, a Home Assistant log check that no `omnibattery` setup line appears when the switch and select change (the
  no-reload claim), the taper timeline, the completion sensors, and the weekly switch off after completion.

### PR split (3 PRs, each independently reviewable, feature off, no behaviour change when merged)

| PR | Content | Depends on |
|---|---|---|
| **1. Solver and planner: soft full-charge target** | `oracle.build_model`/`solve_oracle` optional per-slot ceiling, checkpoint binaries, shortfall slack, taper rows; `planner.FullChargeSpec`, `FullChargeEvaluation`, `run_planner` evaluation and commit modes, `clamp_soc` ceiling argument; tests; `battery-model.md` and `battery-optimizer.md` | none |
| **2. Guard and actuator: the Omnibattery weekly-charge enabler** | `ha_guard` allowlist (`select_option`, selects, the switch), `config.FullChargeSettings` (the **whole** schema above, so PR 1 and PR 3 never edit config parsing), `actuator` roles, ordering, guard status, directive handling, dry_run simulation; ADR-0007 amendment, `AGENTS.md`, `docs/service.md`, `docs/configuration.md`, `config/service.yaml` (feature off); tests | none (uses the interface below) |
| **3. Scheduler: rule, state, observability** (scope amended 2026-11-04, [A7](#a7-pr-3-scope): adaptive cadence, success criterion, settle hold, balance telemetry from the telemetry PR) | `service/full_charge.py` (pure: rule, state machine, thresholds), `sink` state and records, `runtime` wiring (evaluation after the regular solve, committed plan, directive, observation, SOC at 99% detection), metrics, `/status`, regime-log rows, known issue, README/docs index; tests | PR 1 and PR 2 |

**PR 1 and PR 2 can be written and reviewed in parallel.** PR 3 starts when both interfaces are fixed (they are fixed
here) and merges last. Merging 1 and 2 changes nothing observable; merging 3 with `enabled: false` changes nothing; the
owner switches `enabled` and the mode in the deployed config.

**Shared interfaces**

```python
# PR 1, service/planner.py
@dataclass(frozen=True)
class FullChargeSpec:
    mode: Literal["evaluate", "commit"]
    window_start: int  # first slot of the full-charge day in the snapshot (0 if inside it)
    checkpoint_end: int  # exclusive end of the eligible checkpoint slots
    ceiling_before_frac: float  # 0.95: ceiling for slots before window_start
    target_soc_frac: float  # 1.0
    shortfall_penalty_eur_per_kwh: float
    taper_start_soc_frac: float | None
    taper_power_kw: float


@dataclass(frozen=True)
class FullChargeEvaluation:
    normal_cost_eur: float | None  # evaluate mode only
    lifted_cost_eur: float | None  # evaluate mode only
    target_cost_eur: float
    forcing_cost_eur: float | None
    vs_normal_eur: float | None
    checkpoint_slot: datetime | None
    shortfall_kwh: float
    solver: dict[str, Any]  # status, gap, time per solve


# PlanningSnapshot.full_charge: FullChargeSpec | None = None  (input_payload and inputs_hash unchanged when None)
# PlanResult.full_charge: FullChargeEvaluation | None = None  (to_record adds the key only when present)
# planner.clamp_soc(soc_frac, tariff, *, ceiling_frac: float | None = None)


# PR 2, service/actuator.py
@dataclass(frozen=True)
class FullChargeDirective:
    armed: bool  # the actuator drives the switch to this
    weekday: str | None  # "monday".."sunday": weekday of the run's local date; required when armed


# ActuationContext.full_charge: FullChargeDirective | None = None   (None: feature off, the two roles are never touched)
# Actuator.view()/snapshot() gain the weekly readings (enable, day, delay, integration_status) for PR 3 to read.
```

PR 3 builds the directive from its state each tick and reads the observations (SOC, `integration_status`, delay switch,
evidence sensors) from the entity store; PR 2 never imports `full_charge.py`.

### Follow-up for the homelab repository (not changed here)

Extend the ADR-0007 watchdog automation (heartbeat older than 5 minutes while the kill switch is on, and the kill-switch
`off` trigger) with `switch.turn_off` on `switch.omnibattery_weekly_full_charge_enabled`, so a dead optimizer cannot leave
Omnibattery armed (it already turns both direction switches off). Keep `switch.omnibattery_weekly_full_charge_delay` off
and leave `switch.omnibattery_weekly_full_charge_enabled` off by default in HA. Add the two alert rules of section 9.
Re-verify the integration contract when Omnibattery is upgraded beyond 1.5.1b1.

## 13. Open questions for the owner

Answers recorded 2026-11-04; the original questions follow the table.

| # | Question | Status |
|---|---|---|
| 1 | Cadence | **Answered: adaptive** (10/14, 14/28, 7/14, 5/7 days by balance status), [A3](#a3-adaptive-cadence) |
| 2 | Threshold scale | Open: set from the `dry_run` distribution, now over the active row's `[min, max]` |
| 3 | Taper parameters | Open: provisional until the first supervised run (the settle hold and the reading are recorded then too) |
| 4 | Bootstrap | **Answered: eligible as soon as the feature is enabled**, forced at `max` days from enablement, first live run supervised, [A4](#a4-first-run) |
| 5 | Service-owned entities | Open: not addressed by the amendment (the service still owns the weekly switch while enabled) |
| 6 | Homelab watchdog | Standing prerequisite for unattended `live` use; listed as a step of [battery-longevity.md](battery-longevity.md#4-steps) |

1. **Cadence.** With the shipped objective the forcing cost is usually about zero, so the rule fires at day 5 (every 5 to
   6 days). Keep that, raise `min_interval_days` (7?), or give `fixed_cost_eur` a value so days are discriminated?
2. **Threshold scale.** The defaults (0.01 and 0.01 EUR) are placeholders; set from the `dry_run` distribution?
3. **Taper parameters** (0.90 SOC, 200 W) are provisional until the first supervised run; acceptable?
4. **Bootstrap.** If the SOC history shows no 99% since commissioning, the first run is forced no later than 2026-11-18
   (15 days from the commissioning date); is that the intended start, or should `bootstrap_last_full` be set?
5. **Service-owned entities.** While the feature is enabled the service turns the weekly switch off whenever no run is
   armed, overriding a manual use of Omnibattery's own weekly charge. Acceptable?
6. **Homelab watchdog** change (section 12) is a prerequisite for unattended `live` use of this feature; schedule it with
   the owner of the homelab repository.
