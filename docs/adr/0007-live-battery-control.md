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
   | `discharge` at P W | charge **off**, discharge **on** | `discharge_margin_w` (+50: a small import, never export) | limits **on**, discharge cap `max(P, min_discharge_cap_w)` |
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
   4500 W house guard.

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
   because a prior plan was persisted").

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

- **HA host loss** (power, crash, network). Omnibattery, the watchdog and the heartbeat all live in HA; whether
  the Venus keeps its last power or idles on its own without HA is **untested** (implementation plan
  **TODO-5.4**). Until that test is done, unattended live operation relies on the device's own behaviour.
- **Load steps while charging.** The import while charging is bounded by the PD target (≤ 2500 W today, see
  decision 4) through Omnibattery's PD loop on the meter feedback. A sudden step in house load (the 3 kW heat-pump
  heater, an oven) is absorbed by reducing the battery charge, but not instantly: Omnibattery limits the change of
  its output per cycle (`number.omnibattery_pd_controller_max_power_change`, currently 800 W per ~2 s cycle), so a
  3 kW step while charging at 2 kW gives a few seconds of import up to roughly 5 kW before the charge is backed
  off. The rollout therefore recommends that the **owner raises that setting to 2000 W**; it is an Omnibattery
  tuning value, set by the owner and **never written by the service** (it is not in the allowlist). Transients
  faster than the PD loop are not bounded by this code.
- The sign of `sensor.venus_ac_power` has not been measured; `dry_run` and `live` refuse to start until it is set.
  A wrong sign makes the house-load estimate wrong by twice the battery power; the import cap still holds.
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
   `planning.soc_source: entity`. Disable Omnibattery's competing economic strategies. Raise
   `number.omnibattery_pd_controller_max_power_change` from 800 to 2000 W (owner setting, never written by the
   service) so a load step while charging is backed off within one PD cycle (see [Not solved](#not-solved-by-this-adr)).
2. **dry_run for 1–2 days:** `mode: dry_run`, kill switch **on** (nothing is sent in `dry_run`; the switch only
   makes the rehearsal faithful). Review `service/v1/actuation/` and the `actuator_*` metrics: guard reasons, the
   mapping at every slot, setpoint churn (planned against the simulated written state, so a repeated write is
   real churn), how often a charge is `clamped` by the 2500 W target bound, the house-load estimate against the
   meter.
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
