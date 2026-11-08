# ADR-0007: Live battery control through Omnibattery's public Home Assistant entities

- **Status:** Accepted (2026-11-03; decided by the owner). The code ships with `mode: shadow` as the
  default; nothing changes on the installation until the owner switches the deployed config to `dry_run`
  and later to `live` (see [Rollout](#rollout)).
- **Date:** 2026-11-03
- **Decision owners:** Homelab maintainers (the owner approved this ADR on 2026-11-03)
- **Extends:** [ADR-0004](0004-battery-optimizer-production-architecture.md) (sections "Omnibattery
  integration contract" and "Provisional mapping from MPC intent to public HA entities"), which it turns into a
  built adapter; [ADR-0001](0001-home-battery-economic-dispatch.md) (single writer, safe dispatch).
- **Implementation:** `src/battery_optimizer/service/actuator.py`, `service/ha_guard.py`
  (`ActuationAllowlist`), `service/runtime.py`, the `actuation:` section of `config/service.yaml`;
  operations in [Resident service](../service.md#live-control-dry_run-and-live).

## Context

The battery (Marstek Venus E V3, 5.12 kWh, 2.5 kW, AC-coupled, no PV) is now integrated in Home Assistant
through **Omnibattery v1.5.0**, which is the only Modbus writer and runs a fast PD loop on the Shelly grid
meter (2 s cadence). The shadow service already solves the economic MPC every 15 minutes and freezes one
`DispatchIntent` per slot; until now those intents were records only, and the HA adapters could not write
anything (one non-mutating `weather.get_forecasts` query aside). The owner has decided to move from shadow to
live control.

Site constraint: contracted import 5.0 kW and the smart meter cuts supply **instantaneously** on overload
([battery-optimizer](../components/battery-optimizer.md#assumptions)). A trip is a safety and availability
failure, never a cost trade-off.

Two separate limits apply and both stay in force:

- **Battery limit:** the Venus's own maximum charge and discharge power is 2500 W, set on the device through
  Omnibattery's per-battery `number.venus_max_charge_power` and `number.venus_max_discharge_power` (minimum
  800 W, currently 2500 W). The optimizer does **not** write them; `actuation.max_discharge_cap_w` (2500)
  matches the discharge one.
- **House limit:** grid import while charging stays at or below 4500 W (`actuation.import_cap_w`), 500 W under
  the 5 kW contract, whatever the battery limit allows; this is the guard of decisions 3, 4 and 6.

## Decision

1. **Three modes, one setting.** `mode: shadow | dry_run | live` in `config/service.yaml`; the loader refuses any
   other value. `shadow` (the default) is unchanged: no actuator, no write path, the same status, records and
   metrics. `dry_run` runs the full actuator (guard, mapping, ordering, kill switch, hysteresis) and records
   every write it would make under `service/v1/actuation/`, in the log and in the metrics, but sends nothing.
   `live` sends. `dry_run` and `live` require every `actuation.entities` entry and a measured
   `actuation.battery_ac_power_sign`; `live` also requires `planning.soc_source: entity` (a plan from a
   simulated SOC never drives the real battery).

2. **Our service never talks Modbus; it only sets Omnibattery's HA entities.** Omnibattery's automatic PD stays
   the fast regulator against the measured grid power; the optimizer only sets direction permissions, a grid
   import target and a discharge cap (ADR-0004 decision 4). Manual mode is never used.

3. **A narrow, mode-gated write path in `ha_guard`.** Only `live` builds the WebSocket client with an
   `ActuationAllowlist`; `shadow` and `dry_run` pass none, so every write raises `ForbiddenHACallError` exactly as
   before. With the allowlist, a `call_service` frame passes only when it is one of

   | Service | Entities (deployed config) | `service_data` |
   |---|---|---|
   | `switch.turn_on` / `switch.turn_off` | `switch.venus_battery_allow_charge`, `switch.venus_battery_allow_discharge`, `switch.omnibattery_system_power_limits` | none |
   | `number.set_value` | `number.omnibattery_pd_target_grid_power` | `value` in `[0, import_cap_w]` (default `[0, 4500]`) |
   | `number.set_value` | `number.omnibattery_system_max_discharge_power` | `value` in `[min_discharge_cap_w, max_discharge_cap_w]` (default `[100, 2500]`; never 0 = unlimited) |
   | `input_datetime.set_datetime` | `input_datetime.battery_optimizer_heartbeat` | `timestamp` (finite, > 0) |

   with exactly one entity (a string, in `target` or `service_data`), no `return_response` and no other field.
   The owner's kill switch is **not** writable. The weather query keeps its own check; REST stays GET-only in
   every mode. `number.omnibattery_system_max_charge_power` is not written (and not allowlisted): the charge is
   bounded by the import target, and ADR-0004 warns that a matching charge cap can reduce real charging.

4. **Mapping** of the intent frozen for the current slot (`actuator.Actuator.desired_for`):

   | Intent | Direction switches | PD grid target (W, + import) | System power limits |
   |---|---|---|---|
   | `idle` | charge **off**, discharge **off** | `neutral_pd_target_w` (0) | untouched |
   | `charge` at P W | discharge **off**, charge **on** | `min(house load + P, import_cap_w, entity max)`, floored, never negative | untouched |
   | `discharge` at P W | charge **off**, discharge **on** | `discharge_margin_w` (a small import, never export; 200 W from 2026-11-03, 100 W deployed since 2026-11-05, code default 50) | limits **on**, discharge cap `max(P, min_discharge_cap_w)` |
   | *safe idle* | charge **off**, discharge **off** | untouched | untouched |

   House load = grid power − battery AC power (charge positive; the sensor's sign is
   `battery_ac_power_sign`), both from fresh readings. Because Omnibattery holds the grid at the target, extra
   house load reduces the battery's charge rather than raising the import: the import never exceeds the target,
   and the target never exceeds `import_cap_w` (4500 W, 500 W below the contracted 5.0 kW).

   **Effective ceiling (verified on the installation 2026-11-03):** Omnibattery bounds
   `number.omnibattery_pd_target_grid_power` to the batteries' summed charge limit, here **min −2500 / max
   2500 W**. The target actually written is therefore `min(house load + P, import_cap_w, entity max)`, and the
   desired state records that value with `clamped` set when it was cut. So the charge is at most
   2500 W − house load: with a low night load (~500 W) about **2 kW**, not the battery's 2.5 kW (known limitation,
   see [Follow-ups](#follow-ups)). The grid import while charging is at most 2500 W today, well under the
   4500 W house guard. **Correction (commissioning, 2026-11-03):** ±2500 W is the bound with the system power
   limits **off**; with them on the bound is `[-system max discharge, +system max charge]`, which caps the charge
   at the system max charge − house load (see [Commissioning findings](#commissioning-findings-2026-11-03)).

   **Amendment 2026-11-08:** while the **measured** SOC sits at or below the tariff's SOC floor, a slot the plan maps
   to `idle` or `discharge` goes through the `charge` row instead at `actuation.soc_floor_recovery_w`, and a planned
   charge below it is raised to it. While the import trip's hold is running the tick holds the idle mapping instead
   (no charge, and the planned discharge is refused too). The rows and their bounds are unchanged, and the energy is
   off-plan, so its price can differ from the optimized plan's (see
   [reserve protection](#amendment-2026-11-08-reserve-protection)).

5. **Ordering, one write in flight, readback.** Writes are ordered restrictive first: switch off the direction(s)
   being removed, set the discharge cap, enable the power limits, set the PD target, and only then switch on the
   direction being enabled. Each write is confirmed by reading the entity back from the entity store (WebSocket
   events, with a REST GET every 2 s while waiting) within `readback_timeout_s` (10 s, the `CommandPolicy` lease);
   a refused or unconfirmed write is retried up to `max_attempts` (3, the first included); a guard refusal
   (`forbidden`) is never retried. When a write still fails the actuator falls back to safe idle and raises the
   alert metric. Only the charge-mode PD target's load tracking is rate limited: it is rewritten only when it
   moves by `min_setpoint_change_w` (100 W) and not within `min_command_interval_s` (30 s) of the previous write.
   Switch edges, mode changes, any reconcile that switches a direction on, the discharge margin (an unrestored
   margin could leave a 0 W target, i.e. export) and the discharge cap are never rate limited. `CommandPolicy`
   (`dispatch.py`) carries these values. Every write except a `turn_off` re-checks the kill switch from the
   entity store first (a reconcile with readbacks can take tens of seconds): when it is off the reconcile aborts,
   sets safe idle and hands control back. In `dry_run` the writes are planned against a simulated state of the
   entities it would have written, so a stable situation records no repeated writes and the churn is real.

6. **Safety guard before every tick and every write** (`Actuator.guard`). Any failing check selects safe idle
   and is recorded with its reason: no intent for the current slot (`no_plan`, `plan_expired`,
   `inputs_not_ready`), the intent not covering now or expired (`intent_expired`), the intent from a plan this
   process did not solve (`plan_not_from_this_process`), grid power older than 30 s, battery SOC or battery
   AC power missing, `unavailable`/`unknown` or non-numeric (SOC also outside 0..100 %; no age test by default,
   see below), `sensor.omnibattery_non_responsive_batteries` not exactly `None` (`battery_unresponsive`;
   `actuation.entities.battery_liveness`, required outside shadow), no PVPC price for the current slot,
   `sensor.omnibattery_integration_status` outside `healthy_states` (`balanced`, `charging`, `discharging`; any
   other value — `manual`, `grid_charging`, `backup_mode`, `initializing`, a competing Omnibattery strategy,
   `unavailable` — holds safe idle). **Hard rule:** measured import above `import_cap_w + import_guard_margin_w`
   (4700 W) while charge is on switches charge off at once, regardless of the plan, and refuses charge intents for
   `import_trip_hold_s` (300 s); a grid reading above that level while charging wakes the actuator without waiting
   for the tick (at most once per tick interval). A kill-switch change also wakes it.

   Config validation keeps the margins honest: `import_cap_w + import_guard_margin_w` < 5000 W (the contract),
   `discharge_margin_w` > 0, `tick_interval_s` ≤ 120 s (well inside the 5-minute watchdog),
   `0 < min_discharge_cap_w ≤ max_discharge_cap_w ≤ 2500` W (the battery's rating).

   *Battery freshness (verified on the installation 2026-11-03):* Omnibattery's coordinator entities report only
   on change, and HA does not refresh `last_reported` for an unchanged value on this install (`get_states` and
   REST included), so an idle battery's SOC and AC power keep a `last_reported` hours old while healthy. An age
   test would hold safe idle forever and the battery could never leave idle to charge. So
   `freshness_s.battery_soc` and `freshness_s.battery_ac_power` are `null` (availability only). Those entities
   do **not** turn `unavailable` when Modbus polling fails: on 2026-11-03 the battery stopped answering at 18:17
   and SOC/AC power kept their last values, while `sensor.omnibattery_non_responsive_batteries` changed from
   `None` to `venus` within a minute. The liveness guard is therefore the check that catches it. A
   number of seconds still enables an age test. The Shelly grid power reports every ~2 s and stays age-checked.

7. **Restart never resumes.** Plans are held only in memory and the actuator acts only on an intent whose plan
   was solved by this process (`generated_at` at or after the process start). After a restart it holds safe idle
   until the first fresh solve's slot opens (ADR-0004 decision 5: "A new pod must never resume actuation just
   because a prior plan was persisted"). *Amended 2026-11-04 (below): the slot in progress adopts the first
   fresh solve.*

8. **Owner kill switch, heartbeat, HA watchdog, graceful stop.**
   - The actuator writes only while `input_boolean.battery_optimizer_live` is `on` (missing, `unknown` or
     `unavailable` count as off). On its `on → off` edge it sets safe idle once and then stops writing,
     heartbeat included; while it was never on, nothing is written at all.
   - Every live tick that ends reconciled (the desired state, or at least safe idle, confirmed by readback)
     publishes a heartbeat: `input_datetime.set_datetime` on `input_datetime.battery_optimizer_heartbeat` with
     the current timestamp. A tick that cannot even confirm safe idle sends no heartbeat, so the HA watchdog
     takes over.
   - An HA-side watchdog automation (homelab, below) switches both direction switches off when the heartbeat is
     older than 5 minutes while the kill switch is on.
   - On SIGTERM (uvicorn lifespan shutdown) in `live`, the actuator task is cancelled and safe idle is set before
     the HA session closes, bounded by `shutdown_timeout_s` (15 s, inside uvicorn's 20 s grace).

9. **Observability.** Metrics (`battery_optimizer_actuator_*`, only in `dry_run`/`live`): mode, kill switch,
   in-control, guard state (`guard_ok` and `guard_reason{reason}`), alert, desired PD target / switches /
   discharge cap, observed PD target, battery AC power, grid power and house-load estimate, writes by role and
   result, write failures by reason, safe-idle entries by reason, heartbeats by result, the last confirmed write
   time. `GET /status` carries an `actuator` block (GET only). One JSONL row per tick that wrote (or would write)
   or changed state under `<data_dir>/service/v1/actuation/`.

## Safety layers

| Layer | Protects against | Does not cover |
|---|---|---|
| Battery BMS, inverter and installer protections | Cell limits, over-temperature, hardware faults | Economics, the meter trip |
| Omnibattery (inside HA) | Fast grid regulation every ~2 s to the PD target, SOC limits, its blockers; direction switches block a direction | HA or HA-host loss; competing Omnibattery strategies the owner leaves on |
| Our guard (`actuator.guard` + `ha_guard`) | Stale inputs, no or foreign plan, unhealthy integration, over-import while charging, any write outside the allowlist or its bounds | Its own process death (→ watchdog), HA loss |
| HA watchdog automation (homelab) | Optimizer dead, hung or unable to confirm safe idle for 5 min | HA itself dead |
| Owner kill switch | Hands control back at once; nothing is written while off | Nothing by itself: it is a permission |

## Not solved by this ADR

- **HA host loss** (power, crash, network). Omnibattery, the watchdog and the heartbeat all live in HA. Tested
  2026-11-03 for outages of 23 s, 40 s and about 10.5 minutes: the Venus **keeps its last power** (it kept
  discharging) and does not idle on its own; there is no device timeout of at least 10 minutes. HA host
  power-off is untested (implementation plan **TODO-5.4**). Unattended live operation needs a failsafe that
  does not depend on HA (see [Commissioning findings](#commissioning-findings-2026-11-03)).
- **Load steps while charging.** The import while charging is bounded by the PD target (≤ 2500 W today, see
  decision 4) through Omnibattery's PD loop on the meter feedback. A sudden step in house load (the 3 kW heat-pump
  heater, an oven) is absorbed by reducing the battery charge, but not instantly: Omnibattery limits the change of
  its output per cycle (`number.omnibattery_pd_controller_max_power_change`, currently 800 W per ~2 s cycle), so a
  3 kW step while charging at 2 kW gives a few seconds of import up to roughly 5 kW before the charge is backed
  off. The rollout therefore recommends that the **owner raises that setting to 2000 W**; it is an Omnibattery
  tuning value, set by the owner and **never written by the service** (it is not in the allowlist). Transients
  faster than the PD loop are not bounded by this code.
- The sign of `sensor.venus_ac_power` was measured on 2026-11-03 (negative while charging, so
  `battery_ac_power_sign: -1`). A wrong sign makes the house-load estimate wrong by twice the battery power; the
  import cap still holds.
- Competing Omnibattery features (predictive grid charging, time slots, price controls, hourly balance) must be
  disabled by the owner; the guard only notices them through `integration_status`.
- Partial writes after a crash: the last state persists in Omnibattery; the next process starts in safe idle once
  the kill switch is on and HA is synced.

## Follow-ups

- Slot and plan records keep `"shadow": true` in `dry_run` and `live` for now (unchanged record schema); the
  `actuation/` records are the authority for what was sent. Relabel them in a later change.
- The watchdog deliberately does **not** latch the kill switch off: after a recovery the service retakes control
  only with a plan it has solved itself (decision 7), which is the intended behaviour for unattended running.
- **Charge power ceiling (known limitation).** Through the PD target the charge is at most 2500 W − house load
  (about 2 kW at night), because Omnibattery bounds the target by the batteries' charge limit. Evaluate a
  force-charge path (for example Omnibattery's charge features or a per-battery setpoint) that reaches the full
  2.5 kW while keeping the 4500 W house guard; until then the planner's charge intents above that are clamped and
  recorded as `clamped`.

## Home Assistant helpers and watchdog (homelab, proposed; not in this repository)

```yaml
input_boolean:
  battery_optimizer_live:
    name: Battery optimizer live control
    icon: mdi:robot

input_datetime:
  battery_optimizer_heartbeat:
    name: Battery optimizer heartbeat
    has_date: true
    has_time: true

automation:
  - id: battery_optimizer_watchdog
    alias: Battery optimizer watchdog
    mode: single
    triggers:
      - trigger: time_pattern
        minutes: "/1"
      - trigger: state
        entity_id: input_boolean.battery_optimizer_live
        to: "off"
        id: kill_switch_off
    conditions:
      - condition: or
        conditions:
          - condition: trigger
            id: kill_switch_off
          - condition: and
            conditions:
              - condition: state
                entity_id: input_boolean.battery_optimizer_live
                state: "on"
              - condition: template
                value_template: >-
                  {{ as_timestamp(now())
                     - (state_attr('input_datetime.battery_optimizer_heartbeat', 'timestamp')
                        | float(0)) > 300 }}
    actions:
      - action: switch.turn_off
        target:
          entity_id:
            - switch.venus_battery_allow_charge
            - switch.venus_battery_allow_discharge
      - action: persistent_notification.create
        data:
          notification_id: battery_optimizer_watchdog
          title: Battery optimizer watchdog
          message: >-
            Direction switches turned off ({{ 'kill switch off' if trigger.id == 'kill_switch_off'
            else 'heartbeat older than 5 min' }}).
```

The `kill_switch_off` trigger repeats the service's own `on → off` safe idle inside HA, so the battery idles
even when the service is down when the owner flips the switch. The fixed `notification_id` replaces the previous
notification instead of stacking one per minute. The YAML uses the `triggers:` / `trigger:` / `actions:` /
`action:` syntax and needs **Home Assistant 2024.10 or later**.

Neither the watchdog nor the service's safe idle resets the PD grid target or the discharge cap: they may stay at
the last written values (for example a 2500 W import target). That is fine because both direction switches are
off, so Omnibattery neither charges nor discharges; the next charge or discharge always writes its target before
switching its direction on.

**Tokens.** `live` uses its own token, separate from the existing read-only `battery-optimizer` user's token
(which `shadow` and `dry_run` keep using): the dedicated non-admin HA user **`battery-optimizer-live`** (group
`system-users`, local only) and its long-lived token, stored in its own Vault KV path when deployed. Home
Assistant cannot restrict a token to entities or services, so for that user the allowlist in `ha_guard` is the
restriction. No token value is ever written in this repository or its config.

## Rollout

1. **Shadow** (now): unchanged, with the read-only `battery-optimizer` token. Create the two helpers and the
   watchdog with the kill switch **off**. Measure the
   sign of `sensor.venus_ac_power` with a supervised charge from the Omnibattery UI and set
   `battery_ac_power_sign`; map `entities.battery_soc` (and the battery energy counters) and set
   `planning.soc_source: entity` (all done: sign, SOC and counters 2026-11-03; `entity` committed once the
   planner's SOC check became availability plus liveness, see
   [Commissioning findings](#commissioning-findings-2026-11-03)). Disable Omnibattery's competing economic strategies. Raise
   `number.omnibattery_pd_controller_max_power_change` from 800 to 2000 W (owner setting, never written by the
   service) so a load step while charging is backed off within one PD cycle (see [Not solved](#not-solved-by-this-adr)).
2. **dry_run for 1–2 days:** `mode: dry_run`, kill switch **on** (nothing is sent in `dry_run`; the switch only
   makes the rehearsal faithful). Review `service/v1/actuation/` and the `actuator_*` metrics: guard reasons, the
   mapping at every slot, setpoint churn (planned against the simulated written state, so a repeated write is
   real churn), how often a charge is `clamped` by the target bound (`actuator_clamped_watts`), the house-load
   estimate against the meter.
3. **live, supervised:** kill switch **off**, `mode: live` with the `battery-optimizer-live` user's token (its
   own Vault KV path) mounted instead of the read-only one, roll out, then switch the kill switch **on** while
   watching the meter. Verify readback, heartbeat, a slot change in each direction, the watchdog (stop the pod
   and see the switches go off after 5 minutes) and SIGTERM safe idle. Only then leave it unattended, with the
   TODO-5.4 caveat above.

## Rollback

Turn `input_boolean.battery_optimizer_live` off (immediate: safe idle once, then no writes; the watchdog repeats
the safe idle in HA). Then set `mode: shadow` (or `dry_run`), mount the read-only `battery-optimizer` token again
and roll out; a shadow pod has no write path.
Restore Omnibattery's native configuration by hand (the optimizer never restores an earlier configuration). Safe
idle leaves the PD target and the discharge cap at their last values (see above): set them back before switching a
direction on for native control.

## Alternatives considered

| Alternative | Outcome |
|---|---|
| Direct Modbus from the service | Rejected (ADR-0004): conflicts with Omnibattery's single writer and PD loop |
| Omnibattery manual mode with a battery setpoint | Rejected: removes the battery from the PD loop; no fast meter feedback, export and over-import risk |
| An upstream atomic `economic_intent` API with lease | Deferred (ADR-0004 decision gate): public entities suffice for the first adapter; reconsider if the separate writes prove insufficient |
| Writing a charge cap equal to P | Rejected: the target bounds the import; ADR-0004 notes a matching charge cap can reduce real charging |
| Heartbeat by REST `POST /api/states` | Rejected: REST stays GET-only; a WebSocket service call through the same guard is narrower |

## Consequences

The service gains one narrow write path, compiled in but reachable only with `mode: live`; shadow behaviour and
the read-only default are unchanged. The ADR-0004 mapping hypothesis becomes testable on the hardware. Live
dispatch remains subject to the hardware safety gates of ADR-0004 decision 5 and TODO-5.4; acceptance of this ADR
is not by itself authorization to leave the battery under unattended live control.

### Commissioning findings (2026-11-03)

A supervised test on the commissioning evening (details in
[known issues](../known-issues.md#battery-findings-of-the-commissioning-test-2026-11-03)) changes these points
of the decision. The code changes they led to are the SOC check and the clamp metric below; the rest is
configuration or the [live preconditions](#live-preconditions-2026-11-03).

- **Charge ceiling with the power limits on.** Omnibattery bounds the PD target to `[-system max discharge,
  +system max charge]` while `switch.omnibattery_system_power_limits` is on (-800..+1000 W with the owner's
  test caps). The actuator reads that live bound and never turns the limits off (decision 4 turns them on for
  discharge), so a charge is cut to `system max charge − house load`, down to zero. Nothing fails; the cut
  is recorded as `clamped`/`clamped_w` and exported as `battery_optimizer_actuator_clamped_watts` (planned
  battery power not requested), on which homelab can alert ([service metrics](../service.md#metrics)). This is the pre-go-live
  state on the test socket; the go-live configuration sets both system caps to 2500 W (live precondition (c)).
  Changing the charge mapping instead (power limits off while charging, or a written charge cap, which needs
  the allowlist of decision 3) stays an owner decision.
- **Discharge margin.** The PD loop swings ±150 to 200 W on a ~2 s period; the 50 W margin gave brief exports
  (-10..-94 W). The config set `discharge_margin_w: 200`, and 100 since 2026-11-05 (evidence in
  [known issues](../known-issues.md#the-pd-loop-oscillates-the-discharge-margin-was-too-small)).
- **HA outage.** On an HA restart (40 s), a forced kill (23 s) and a 10.5-minute stop the battery held its last
  command and kept discharging (about 620 W through the long one); Omnibattery does not set standby on shutdown
  and the firmware has no timeout of at least 10 minutes. The watchdog (decision 8) and safe idle act only while
  HA runs, so during an outage nothing follows the house load or backs a charge off a load step. The watchdog
  design must not assume the battery stops when HA stops: unattended `live` needs an HA-independent failsafe.
- **`backup_mode` latch.** An upstream Omnibattery race can keep `integration_status` at `backup_mode` after
  the grid returns while the backup-port load exceeds `number.venus_backup_offgrid_threshold`; the guard then
  holds safe idle indefinitely. Workaround: a threshold above the backup-port load (200 W in the test, 500 W
  since, default 50 W); upstream fix ffunes/Omnibattery#723 (for #660, targets `release/v1.5.1`). While it is in
  place a backup load below the threshold during a real outage is not treated as off-grid demand (Omnibattery
  keeps commanding the islanded battery) and there is no 5-minute settle after the grid returns. Removal
  procedure (back to 50 W once a release with #723 is installed, then `integration_status` must return to
  `balanced` within 5 minutes with a backup-port load in Bypass) in
  [known issues](../known-issues.md#omnibattery-can-stay-in-backup_mode-after-the-grid-returns).
- **Measured SOC.** `planning.soc_source: entity`, required by `live` (decision 1), aged the SOC against
  `freshness_s.battery_soc` (900 s); an idle battery's SOC looked stale (decision 6), so planning would stop.
  Resolved: the planner now checks the SOC as the actuator does, availability only by default
  (`freshness_s.battery_soc: null`; a number restores an age test) plus `actuation.entities.battery_liveness`
  exactly `None` (tracked in every mode with `soc_source: entity`), and the committed config uses `entity`.
- **Premium and the 2500 W cap.** The decision to run Premium mode with the 2500 W discharge cap was backed by the
  perfect-foresight LP: an 800 W discharge cap would lose about 11 % of the savings.
- **One Modbus client.** The battery accepts a single Modbus TCP client, Omnibattery; others are refused. A
  "read directly over Modbus" probe therefore competes with Omnibattery.
- **PD settings are the owner's** (owner decision 2026-11-06). Omnibattery's PD settings (kp, kd, deadband,
  max_power_change, direction hysteresis, min cycle interval, tuning profile) are set by the owner; the optimizer only
  analyses them and recommends ([known issues](../known-issues.md#the-pd-loop-oscillates-the-discharge-margin-was-too-small)).
  A future optimizer write path would need an amendment of this ADR with bounded allowlist entries and tests.

### Live preconditions (2026-11-03)

Found in the commissioning test; each must hold before the kill switch is turned on in `live`:

- **(a) HA-independent failsafe.** The battery holds its last command for at least 10.5 minutes with HA down
  (no firmware timeout, no standby on Omnibattery shutdown), so neither safe idle nor the HA watchdog
  (decision 8) can stop it during an HA outage. Required before `live`: a failsafe that does not depend on
  HA. Planned (homelab ESPHome config, not this repository): the battery's ESPHome smart plug turns its relay
  off after N minutes without a connected HA API client, which removes the battery's AC input.
- **(b) Import margin 200 W (100 W since 2026-11-05).** Omnibattery's PD loop oscillates by about ±150 to 200 W on a ~2 s period; with a
  50 W grid-import target while discharging the grid briefly exported (-10..-94 W). `discharge_margin_w` was
  set to 200 W (the code default stays 50 W) and, from measured data, lowered to 100 W on 2026-11-05 (see
  [known issues](../known-issues.md#the-pd-loop-oscillates-the-discharge-margin-was-too-small)).
- **(c) System caps at the battery's rating.** *Pre-go-live state:* on the temporary test socket the system
  caps are 1000 W charge / 800 W discharge with the power limits on, so the PD target, and with it the charge,
  is bounded by `system max charge − house load` (the charge-ceiling finding above), and
  `battery_optimizer_actuator_clamped_watts` shows the planned power that is not requested. *Go-live
  configuration (owner):* off the test socket, `number.omnibattery_system_max_charge_power` and
  `number.omnibattery_system_max_discharge_power` both **2500 W**, and **Premium mode** enabled in the Marstek
  app, without which the firmware limits discharge to 800 W. The planner (`battery.power_kw: 2.5` in
  `config/tariff.yaml`) and `actuation.max_discharge_cap_w: 2500` already assume 2500 W both ways; with
  Premium off a discharge intent above 800 W would be under-delivered by the firmware without any clamp or
  guard reason, so enabling Premium is part of go-live, not a code change.

## Amendment 2026-11-04

Decision 7 is kept except its last clause. After a restart, or when a slot opens not ready, the slot in progress
no longer stays in safe idle until the next boundary: once the inputs are ready and a plan **solved by this
process** covers it (a plan made inside such a slot plans from that slot), the slot adopts that plan's intent for
the rest of the slot (`late_intent`, [service](../service.md#scheduling-and-the-slot-cycle)). The rest is
unchanged: a persisted plan, or one from an earlier process, is never acted on; a slot that opened with an intent
is never re-decided; and a late intent passes the same guard (kill switch, liveness, plan expiry, inputs, import
cap) as any other. Reason: every deploy cost up to 15 minutes of control, and the slot at local midnight usually
opened not ready because the day's load history is fetched just after it.

## Amendment 2026-11-04 (periodic full charge)

Decision 3's allowlist is widened by **exactly two entries, and only when `full_charge.enabled` is true and
`full_charge.mode` is `live` in `mode: live`** (`full_charge.mode: shadow`, the default, simulates the feature beside
live control and adds nothing: both entities stay refused, the actuator gets no directive and the weekly status is
never healthy); nothing else changes (kill switch, heartbeat, mode gating, readback, retry, `forbidden` never retried,
one entity per call, no `return_response`, REST GET-only):

| Service | Entity | `service_data` |
|---|---|---|
| `switch.turn_on` / `switch.turn_off` | `switch.omnibattery_weekly_full_charge_enabled` | none |
| `select.select_option` | `select.omnibattery_weekly_full_charge_day` | exactly `{"option": <one of monday, tuesday, wednesday, thursday, friday, saturday, sunday>}` |

Purpose: Omnibattery's own weekly full charge is used as a self-limiting enabler of a periodic charge to 100% for cell
balancing ([plan](../planning/periodic-full-charge.md)). Omnibattery lifts its maximum SOC to 100%, completes on its
own detection and restores the configured 95%; it clears the run at its day rollover, so if the optimizer dies the
worst case is a repeat on that weekday. Not allowlisted, and never to be: `switch.omnibattery_weekly_full_charge_delay`
and `switch.omnibattery_charge_delay` (read only), the charge-cutoff numbers, any button, and any other weekly-charge
entity. Decision 6 gains one healthy status: `weekly_full_charge`, accepted only while the actuator has armed a run for
the current local day. The service owns the two entities while the feature is enabled (it turns the switch off
whenever no run is armed). Decision 7 is unchanged: the persisted full-charge state is a calendar decision and a
timestamp, never a plan. A homelab watchdog follow-up turns the switch off on heartbeat loss.

The scheduler that arms it (amendment of the plan, 2026-11-04) adds **no write path**: after Omnibattery completes the
run the actuator holds the existing idle intent (charge off, discharge off, neutral PD target) for
`full_charge.settle_timeout_s` (default 300 s) or until a new balance reading arrives, keeping the weekly switch on
until then, and only afterwards turns it off. The hold is a field of the directive, subject to the kill switch and the
guard like any intent; a guard failure ends its effect (it is idle anyway). The service reads the balance sensors and
Home Assistant's `time_zone` (`GET /api/config`, already allowed) and writes nothing else.

## Amendment 2026-11-07 (load-following discharge)

Decision 4's discharge row is extended, **only with `actuation.load_following.enabled`** (off in the code default;
the owner enables it in the deployed config): in a `discharge` slot, while the measured SOC is at or above the
plan's end-of-slot SOC for that slot (the intent's `soc_target_frac`), the system discharge cap is raised from
`max(P, min_discharge_cap_w)` to `load_following.max_w` (null: `max_discharge_cap_w`). It engages at the plan SOC +
`soc_hysteresis_pct` (1 point) and releases below the plan SOC; a charge, idle or safe-idle tick resets it.

Why the cap may now exceed the planned power. The cap was the plan's power so that the battery delivered what the
plan priced, but the plan's power is an average of a forecast load. Live data of 2026-11-04 to 11-07 showed evening
peaks of 1.5 to 1.9 kW, mostly a 30-minute heat-pump hot-water cycle of about 1.4 kW whose timing the forecast gets an hour
wrong (summer time ended 2026-10-25), against a cap of about 1 kW: only 64% of the planned evening discharge was delivered (84% was deliverable at the measured load; of the gap, about 60% came from the cap and 40% from the margin and control tracking), the battery ended every evening at 40 to 49% SOC while the grid covered peak-price load, and the realised
saving was 74 to 81% of the perfect-foresight one. Above the plan SOC the battery holds more than the plan needs at
the end of the slot, so covering the real load down to that SOC spends no energy the plan keeps for later slots (only
the slot's own planned energy, sooner, plus anything left over from earlier under-delivery), and the 15-minute
replan from the measured SOC re-plans the rest. Below the plan SOC the planned cap is kept.

What does not change: the write path (same entity, `number.set_value`, the guard's bound `[min_discharge_cap_w,
max_discharge_cap_w]` is not widened; `load_following.max_w` must lie inside it, checked at config load), the PD
target (`discharge_margin_w`, so no export), the write ordering, the kill switch, the heartbeat, the guard and the
planner (`discharge_margin_bound` included). The ceiling is at most `max_discharge_cap_w` (≤ 2500 W, the Venus
rating); Omnibattery's per-battery caps and its temperature limit still bound the real power below the system cap.
Operations: [service](../service.md#load-following-discharge).

## Amendment 2026-11-08 (reserve protection)

**Measured, read only:** on 2026-11-08 03:30-09:45 UTC the battery sat in idle slots (actuation mode power 0, no
charge or discharge energy) and the SOC fell from 18% to 15%: about **26 W** of residual drain, consistent with the
about 34 W measured on 2026-11-07. **The source of that drain is not proven** — the backup port's about 240 to 245 W
is AC *output*, which passes through from the grid while idle or charging and is not in the AC charge counter, so it
is not evidence of a 245 W DC draw. Every plan of that night started from a SOC **clamped to the 0.20 floor while the
battery measured 0.150** (`soc=0.200 soc_measured=0.150 soc_clamped=True`): the planning state was clamped, the
telemetry was not hidden, and nothing in the plan could see or act on the shortfall.

Forcing idle does not help — the tick that was already idle is where the drain happened. So the actuator now
synthesizes a **bounded recovery charge** while the **measured** SOC sits at or below the tariff's floor
(`battery.soc_window_min`, 0.20 here): a tick the plan maps to idle or discharge becomes a charge of
`actuation.soc_floor_recovery_w` (200 W) with `reason: below_soc_floor`. It uses the existing charge row, so the PD
target is `house load + power` inside `min(import_cap_w, entity max)`, with the same write ordering, dwell and
readback, and only the already-allowlisted PD target and charge allow switch are written: **no entity, service or
bound is added or widened**, and nothing here makes a service live. A planned charge at or above the recovery power
is the stronger command and is mapped untouched; a weaker one is raised to it. When `min(import_cap_w, entity max)`
leaves less headroom above the house load than the recovery power, the setpoint is not pushed to that ceiling: an
idle or discharge tick holds the idle mapping, and a weaker planned charge keeps its own row (clamped by the ceiling
as it is today).

- **One floor, not two:** the floor comes from `config/tariff.yaml`, which the planner already uses, and is passed per
  tick as `ActuationContext.soc_floor_pct`. There is no second floor setting, and the planner's clamp stays (a plan
  must remain feasible); the protection reads the meter, not the plan's assumption.
- **Hysteresis:** engages at the floor, releases at floor + `actuation.soc_floor_hysteresis_pct` (default 1.0 SOC
  point, `0 < value <= 10`), latched between ticks, released only by a fresh SOC above the release point (a guard
  failure or a bad read does not drop it) and starting released after a restart, so a service that comes back at 15%
  recovers at once.
- **Everything else keeps priority:** `ha_not_synced`, the owner's kill switch and every guard reason (stale or
  non-finite SOC or load, an unresponsive or unhealthy battery, an expired or foreign plan, a stale price, import over
  cap) come first, then the settle hold, then the recovery, then the normal mapping. The import trip's
  `import_trip_hold_s` (300 s) is honoured in the recovery itself: the guard scopes that refusal to *charge* intents,
  so while the hold is running and the reserve is latched the recovery sends no charge **and refuses the planned
  discharge as well**, holding the idle mapping with both directions off rather than letting the floor drain further
  while no charge is allowed.
- **Cost:** the recovery kWh are off-plan, so their price can differ from the optimized plan's. They are recorded in
  the `actuation/` rows and in `GET /status` (`reserve`), and exported as `actuator_reserve_engaged`; the plan itself
  is not rewritten.
- **Enable semantics:** no separate flag. It is part of the actuator, so it takes effect automatically in `dry_run`
  (computed and recorded, nothing sent) and `live` for whichever deployment runs this release; `shadow` has no
  actuator and stays inert.
- What does not change: the allowlist, the write path and ordering, the guard (no new reason; `below_soc_floor` is a
  mapping reason after the guard passes), the planner and its SOC clamp, the heartbeat, the kill switch.

Operations: [service](../service.md#reserve-protection). Open hardware validation, not blocking this change: measure
the backup port's DC current and the battery's internal standby loss to attribute the residual drain; this amendment
holds the floor the owner already set, it does not decide a larger outage reserve and does not claim to prevent drain
physically.
