# ADR-0009: Live climate control: the optimizer writes space heating

- **Status:** **Accepted 2026-11-18 by the owner for build and deployment in shadow.** The switch to live
  needs the owner's **separate explicit approval**, recorded in this ADR (see
  [Switch to live](#switch-to-live-not-yet-approved)) and in the [regime log](../components/regime-log.md) on that
  day. Until then nothing writes to the heat pump: the code ships with `climate_actuation.mode: shadow`, which has
  no write path, exactly like the battery's `shadow`.
- **Date:** 2026-11-18
- **Decision owners:** Homelab maintainers (the owner took the decisions below on 2026-11-18)
- **Supersedes in part:** [ADR-0008](0008-climate-plan-handoff-to-the-comfort-controller.md): decision 2 (AppDaemon
  as the only writer), decision 4 (the homelab hard lock `optimizer_live_allowed`, which belonged to a consumer
  that is not built), the consumer half of decision 3 and of decision 6, and precondition 1 (the preregistered
  model gate). Its decisions 1, 5, 7 and 8, the `input_select.climate_optimizer_mode` select, the ClimatePlan v1
  endpoint and its contract stay, with the mapping below.
- **Extends:** [ADR-0006](0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md) (decoupled comfort
  control; the HP is comfort plus efficiency, the battery is price) and
  [ADR-0007](0007-live-battery-control.md), whose structure it mirrors (three modes, allowlist, owner switch,
  heartbeat, HA watchdog, rollout).
- **Implementation (built 2026-11-18, mode `shadow`):** `src/battery_optimizer/service/ha_guard.py` (`ActuationAllowlist`),
  `service/climate_actuator.py` next to `service/climate_shadow.py`, `climate/plan_export.py`
  (`LIVE_ALLOWED`), `service/runtime.py`, `service/metrics.py`, and a new `climate_actuation:` section of
  `config/service.yaml`. The homelab half (helpers, watchdog, deployment) is a separate repository's change.

## Context

The comfort MPC ([climate-mpc](../components/climate-mpc.md)) runs as the live climate shadow: hourly at HH:10 it plans, per 15-minute slot, space heating on or off and the absolute water target,
and records and scores the plan. [ADR-0008](0008-climate-plan-handoff-to-the-comfort-controller.md) prepared an
advisory export for a homelab AppDaemon comfort controller (homelab draft PRs #4699 and #4746) to act on, behind
a model gate. The owner has decided on 2026-11-18 not to build that second writer: the optimizer itself becomes
the writer, and the gate is replaced by the owner's go.

### Owner decisions of 2026-11-18

1. **The optimizer is the only writer for space heating**: heating on/off (the operating mode) and the water
   target. AppDaemon's legacy `climate_control.py` is retired at the switch (disabled and archived in homelab);
   the comfort-controller drafts homelab #4699 and #4746 are **closed** and their logic is reused as reference.
   This **supersedes ADR-0008 decision 2 and its precondition 1**: the owner's go replaces the preregistered
   model gate. The gate and the section-A verdicts keep reporting; they no longer block.
2. **No `dry_run` stage is required before live.** The code still supports `shadow` / `dry_run` / `live`, as the
   battery actuator does.
3. **DHW stays with AppDaemon `dhw_control.py`** (force-DHW switch, 50 degC tank target) in this release. The
   optimizer never writes DHW, the tank target, the heater, sterilization or cooling
   ([dhw-price-scheduling](../planning/dhw-price-scheduling.md) section 8).
4. **Thermostat setpoints stay manual.** The optimizer does not write them. Optimum start by setpoint comes later,
   if ever, under an amendment of this ADR.

### What the homelab has today (read-only research, 2026-11-18, homelab `a2e2e2099`)

All paths are under `apps/home-assistant/` of the homelab repository; line numbers are those of that commit.

| Fact | Where |
|---|---|
| The legacy controller writes the operating mode by **`set_state` on `input_select.heishamon_heatmode`** (not a service call), with `Heat+DHW` (on) and `DHW` (off), then polls until the state matches | `appdaemon/apps/climate_control.py:102-119` (`set_state` at `:109`), `appdaemon/apps/apps.yaml:16-18` |
| `input_select.appdaemon_climate_heat_mode` (options `Heat+DHW`, `Cool+DHW`) only stores **which mode means "on"**; AppDaemon reads it, nothing writes it at runtime | `config/integrations/appdaemon_climate.yaml:13-18`, `appdaemon/apps/apps.yaml:8` |
| HA automation **"Set heatmode"** (state trigger on the select) publishes MQTT `panasonic_heat_pump/commands/SetOperationMode` (`retain: false`) with `Heat` 0, `Cool` 1, `Auto` 2, `DHW` 3, `Heat+DHW` 4, `Cool+DHW` 5, `Auto(Heat)+DHW` 6, `Auto(Cool)` 7, `Auto(Cool)+DHW` 8, then waits 5 s with the mirror automation off | `config/integrations/heishamon.yaml:175-218` |
| Options of `input_select.heishamon_heatmode`: `Heat`, `Cool`, `Auto`, `DHW`, `Heat+DHW`, `Cool+DHW`, `Auto(Heat)+DHW`, `Auto(Cool)`, `Auto(Cool)+DHW`. The automation "Set heatmode selector" mirrors the unit's `Operating_Mode_State` back into it; `sensor.aquarea_mode` (string) and `sensor.aquarea_mode_code` (4 = `Heat+DHW`, 3 = `DHW`) report the unit's mode | `heishamon.yaml:145-173`, `:448-459`, `:550`, `:576` |
| The **water target** is `input_number.heishamon_heatshift_z1` (min -5, max 40, step 1, one helper for both curve shift and direct absolute value). The automation "Set Z1 Heat Temperature" (state trigger) publishes `SetZ1HeatRequestTemperature` as `"%.0f"` of the helper (`retain: false`), mutes "Set Z1 Heat selector" for 5 s; that automation mirrors the unit's `Z1_Heat_Request_Temp` (`sensor.aquarea_z1_heat_request_temperature`) back into the helper | `heishamon.yaml:221-253`, helper `:404-408`, sensor `:708` |
| The **legacy never writes the water target**: it stays at the manually set 26 degC (Prometheus 2026-11-18: helper 26, request 26) | `climate_control.py` (no such call) |
| Writes need **direct mode**: `sensor.aquarea_heating_mode` (`Heating_Mode`, `1` direct, `0` curve). In curve mode the same command means a shift of -5..+5 K. The mode cannot be changed over MQTT/HA; it is an installer setting (read 1 on 2026-11-18) | `heishamon.yaml:966-972`; homelab `docs/user-guide/heat-pump-water-temperature.md` (Control modes, "What happens if the request is written in the other mode") |
| HeishaMon pitfalls (GoHeishaMon fork): no range check; an **empty or garbage payload becomes 0 degC** (byte 128); commands are FIFO with 1 s spacing and expire after 2 min; no acknowledgement frame (the state topic is the acknowledgement); **always `retain: false`**; `Heating_Mode` is only republished on change or the 300 s refresh | same doc, "Commands supported by the fork", "Safety notes", "Acknowledgement and latency" |
| The **climate watchdog** `automation.appdaemon_climate_watchdog` runs every minute; it acts only when `input_boolean.appdaemon_climate_enable` is on, `sensor.aquarea_mode` is `DHW`, HA has been up 15 min, **`sensor.last_appdaemon_connection`** (a heartbeat AppDaemon refreshes every minute with `set_state`) is missing or older than 15 min, and the configured mode is a valid option, at most once per 30 min. It calls `input_select.select_option` on `input_select.heishamon_heatmode` with the configured mode and notifies. It **does not touch the water target** | `config/integrations/appdaemon_climate.yaml:21-71`; `appdaemon/apps/last_connection_sensor.py:10-13` (the C0 fixes of 2026-10-30, homelab `57b71c4e6` and earlier) |
| `dhw_control.py` only turns on **`switch.aquarea_force_dhw_mode`** in the cheapest hour of each 12 h block (`interval_hours: 12`); the tank target `input_number.heishamon_tank_temp` is manual (50 on 2026-11-18) | `appdaemon/apps/dhw_control.py:96-104`, `apps.yaml:26-37` |
| `input_select.climate_optimizer_mode` **exists in homelab master** (options `shadow`, `off`, `live`, no `initial:`; commit `04d74373b`, shadow only) | `config/integrations/climate_optimizer_plan.yaml:20-27` |
| `input_number.comfort_floor_<room>` / `comfort_day_minimum_<room>` **do not exist in homelab master** (not in HA, none in Prometheus on 2026-11-18). They exist only in the draft PR #4699 (`appdaemon_comfort.yaml`; floor 18 to 23, day minimum 18 to 26, step 0.5; rooms `hall_kitchen`, `bedroom`, `office_a`, `office_t`), which is closed by this ADR | homelab PR #4699; the shadow already reads them absent-tolerant (`climate/shadow.py:151-152`) |
| The battery path to mirror: kill switch `input_boolean.battery_optimizer_live`, heartbeat `input_datetime.battery_optimizer_heartbeat` and automation `battery_optimizer_watchdog` (5 min) in `config/integrations/battery_optimizer.yaml:6-66`; the **same live token** (the `battery-optimizer-live` user's, from its own Vault KV path) is rendered by `apps/battery-optimizer/templates/external-secret.yaml:16-42` when `global.liveHomeAssistantToken` is true (it is) and is the only token the service holds; the mode is `files/service-overrides.yaml` (`mode: live`; `climate_shadow.optimizer_mode: shadow`); alerts `apps/battery-optimizer/rules-service/actuator.yaml`; runbook `docs/deployment/battery-optimizer.md` ("Live battery control") | homelab |

The climate writes therefore need **no new HA user or token**: the service already runs with the live user's
token, and the allowlist in `ha_guard` is the restriction (Home Assistant cannot restrict a token to entities).

## Decision

1. **Three modes, one section.** `climate_actuation.mode: shadow | dry_run | live` in `config/service.yaml`,
   independent of the battery's `mode`; the loader refuses any other value. `shadow` (the default) has **no
   climate actuator and no climate write path**. `dry_run` runs the full actuator (guard, mapping, ordering,
   limits, owner select, fallback) and records every write it would make under
   `<data_dir>/service/v1/climate_actuation/`, in the log and in the metrics, but sends nothing. `live` sends.
   No `dry_run` stage is required before `live` (owner decision 2). `live` requires the live token (the homelab
   chart fails the render otherwise, as for ADR-0007) and a configured entity map.

2. **A narrow, mode-gated widening of `ActuationAllowlist`** (`service/ha_guard.py`), **live mode only, one
   entity per call, no `return_response`, no other field**. The allowlist for the climate family is built only
   when `climate_actuation.mode: live`; a battery in `live` with the climate in `shadow` gets none of these
   entries, and the reverse. The existing entries of ADR-0007 are unchanged.

   | Service | Entity | `service_data` |
   |---|---|---|
   | `input_select.select_option` | `input_select.heishamon_heatmode` | `option` exactly `Heat+DHW` or `DHW` |
   | `input_number.set_value` | `input_number.heishamon_heatshift_z1` | `value` an integer in `[20, 35]` (degC) |
   | `input_datetime.set_datetime` | `input_datetime.climate_optimizer_heartbeat` | `timestamp` (finite, > 0) |

   This adds the services `input_select.select_option` and `input_number.set_value` to `ACTUATION_SERVICES`, and
   two allowlist fields (`input_selects`, `input_numbers`, domain-checked like the existing ones; integers only
   for the latter). `Heat`, `Cool*`, `Auto*` and every other option are refused. **Nothing else**: not DHW
   (`switch.aquarea_force_dhw_mode`, `input_number.heishamon_tank_temp`), the heater, the powerful mode
   (`input_select.heishamon_powermode`), quiet or holiday mode, main power, sterilization, cooling, thermostat
   setpoints, the owner's select, the legacy helpers or any MQTT, REST or Modbus write. The non-mutating weather
   query keeps its own check. The water target is the existing shared helper (see
   [Open items](#open-items)); its automation publishes with `retain: false` and the validated integer.

3. **Owner switch (never written): `input_select.climate_optimizer_mode`** of ADR-0008 (`off` / `shadow` /
   `live`). The actuator writes **only when the deployed `climate_actuation.mode` is `live` AND the select is
   `live`**; both are required, either alone is a permission to nothing. A missing, `unknown` or `unavailable`
   select means **no writes**. The select is read through the entity store and never written, and not
   allowlisted. `plan_export.LIVE_ALLOWED` is lifted for this path: the constant (a tripwire of ADR-0008) is
   replaced by a check that `climate_actuation.mode` is `live`, so the plan export reports `effective_mode: live`
   only then; otherwise a `live` request is still served as `shadow` and flagged `live_locked` with the reason
   "climate_actuation.mode is not live". The ClimatePlan v1 contract is unchanged (still `advisory: true`): the
   actuator consumes the same in-process decision, never a command.

4. **Mapping.** For the 15-minute slot containing now, from a plan **solved by this process** and not expired
   (`valid_until`, decision time + 90 min):

   | Plan slot | Heat mode written | Water target written |
   |---|---|---|
   | space heating on at T degC | `Heat+DHW` | T (integer, clamped 20 to 35) |
   | space heating off | `DHW` | untouched |

   Order, restrictive first as in ADR-0007: **turning heating off**: heat mode first; **turning heating on**:
   water target first, then heat mode. Each write is confirmed by readback of the entity store (`sensor.aquarea_mode`
   for the mode, `sensor.aquarea_z1_heat_request_temperature` for the water request, the unit's own
   acknowledgement) within a timeout; an unconfirmed write is retried up to a small bound and then counts as a
   failure; a guard refusal is never retried. At least 10 s between two writes to one entity (the "Set heatmode"
   and "Set Z1 Heat Temperature" automations mute their mirror for 5 s). The actuator runs as its own task on a
   tick of `climate_actuation.tick_interval_s` (default 60, at most 120); its failure never touches the battery
   planner, the battery actuator or readiness.

5. **Machine limits in code (not helpers, not plan-settable):**
   - compressor **minimum on 30 min and minimum off 20 min**, applied to the heat-mode command (after `Heat+DHW` no
     `DHW` for 30 min; after `DHW` no `Heat+DHW` for 20 min), the clocks taken from the observed `sensor.aquarea_mode`
     changes and surviving a restart by that observation. Two writes are exempt from the **minimum off** only: the
     safe-state write (decision 7) and a comfort-floor breach (decision 6); the minimum on still applies to every
     switch to `DHW`;
   - the water target is written **only in direct mode** (`sensor.aquarea_heating_mode` = 1, available; the mode is
     static, so an availability test and not an age test), as an **integer clamped to 20 to 35 degC**, only on a
     **change of at least 1 K** and **at most once per 15 min**; in curve mode (0) or with the sensor unavailable it
     writes no water target and raises an alert metric (`not_direct_mode`);
   - **validated payloads**: a value that is not a finite integer in range never leaves the process (an empty or
     garbage payload would become 0 degC in the unit);
   - **never** the heater, DHW, boost (powerful mode), sterilization or cooling; the heater stays under the unit's
     own control (owner decision 2026-11-05).

6. **Comfort floors win.** A comfort room (`hall_kitchen`, `bedroom`, `office_t`; the rooms of the plan) below its
   floor forces `Heat+DHW` (with the planned or, without one, the safe water target) **whatever the plan says**, at
   once: a room below its floor outranks short-cycle protection, so the breach is exempt from the minimum off (as the
   safe state is), while the minimum on still applies to switching heating off again. The floor is the
   per-room `input_number.comfort_floor_<room>` when present, fresh (at most 2 h old) and in 18 to 23 degC, else the
   configured value, **default 21 degC**; it releases at the floor plus `climate_actuation.floor_margin_c` (default
   0.5 K), which is the hysteresis against chatter. The plan can never lower a floor or shorten the comfort schedule. A room with a missing or `unavailable`
   reading cannot be proven below its floor and is skipped; no usable room reading at all is "observer without
   state" (decision 7).

7. **Fallback = the safe heating state.** On any of: a stale, missing or invalid plan (including a plan not solved
   by this process, so after a restart), a solver failure (`plan_failed`), an observer without state, no usable
   room reading, a guard failure, or the owner's select leaving `live`, the
   actuator **writes the safe state once** (`Heat+DHW`; water target `climate_actuation.safe_water_target_c`,
   default **26 degC**, the current legacy direct target, only in direct mode) and **then stops writing** (the
   heartbeat continues while the select is `live`) until a valid plan returns. The safe-state write is exempt from
   the 15-minute water rate limit and from the minimum off (turning heating **on** is the safe direction, as the
   legacy watchdog does); it is never exempt from validation. **The worst case is extra kWh, never no heating**, and the
   thermostats keep protecting the rooms through the floor valves. After a restart the first decision runs at once
   instead of waiting for HH:10 and the safe state holds until it has a plan.
   *One writer only:* while `input_boolean.appdaemon_climate_enable` reads `on` (the legacy controller is active)
   the actuator **writes nothing at all**: no plan-driven write, **no safe state and no heartbeat** (guard reason
   `legacy_writer_active`). It is deliberately not a fallback trigger, because a safe-state write would be a second
   writer next to the legacy controller. A missing helper counts as off, which is its state after the switch commit
   removes it. The HA watchdog is unaffected: it acts only while the select is `live`, and the switch procedure turns
   the legacy toggle off (and disables the app) before the select goes `live`.

8. **Heartbeat and HA watchdog (homelab).** While `climate_actuation.mode` is `live` and the select is `live`,
   every tick that ends reconciled (the desired state, or at least the safe state, confirmed) writes
   `input_datetime.climate_optimizer_heartbeat`; a tick that cannot confirm even the safe state sends none. Select
   leaves `live`: safe state once, then no writes, heartbeat included. A homelab automation
   `climate_optimizer_watchdog` (the shape of `battery_optimizer_watchdog`) fires every minute and on the select's
   `live` to anything edge, and, **while the select is `live` and the heartbeat is older than 10 minutes** (or on
   that edge), sets the safe state itself: `input_select.select_option` on `input_select.heishamon_heatmode` to
   `Heat+DHW`, and `input_number.set_value` on `input_number.heishamon_heatshift_z1` to 26 **only while
   `sensor.aquarea_heating_mode` is 1** (separate step, `continue_on_error`), then a notification with a fixed
   `notification_id`. It **replaces `automation.appdaemon_climate_watchdog` at the switch**. The heartbeat helper
   has no `initial:` and is restored across restarts. On SIGTERM in `live` the actuator task is cancelled and
   the safe state is attempted before the HA session closes, bounded by the shutdown timeout.

9. **Observability.** `battery_optimizer_climate_actuator_*` metrics, only in `dry_run` and `live`: mode, owner
   select state, in-control, guard state and reason, last confirmed write time per entity, writes by entity and
   result, write failures by reason, **fallbacks by reason**, heartbeats by result, the desired and observed heat
   mode and water target, floor overrides, `not_direct_mode`. `GET /status` carries a `climate_actuator` block.
   **Every write is logged** with time, entity, old value, new value and reason, and recorded as one JSONL row under
   `<data_dir>/service/v1/climate_actuation/`. Homelab alerts mirror `rules-service/actuator.yaml` (heartbeat
   stale, write failures, safe state, select `live` while the config is not).

## Safety layers

| Layer | Protects against | Does not cover |
|---|---|---|
| The unit's own protections and the thermostats' floor valves | Component limits, heater policy, rooms with a closed valve | Economics, comfort timing |
| Our guard (`ha_guard` allowlist plus the actuator guard and machine limits) | Any write outside three entities, an option or range outside the lists, short cycling, a curve-mode write, a plan that is stale or foreign | Its own process death (watchdog), HA loss |
| Comfort floors and the safe heating state | A bad plan, a dead solver, no data | Extra kWh (accepted) |
| HA watchdog automation (homelab) | Optimizer dead, hung or unable to confirm the safe state for 10 min | HA itself dead |
| Owner select | Hands control back at once; nothing is written while not `live` | Nothing by itself: it is a permission |

## Consequences and risks

- **The model has not passed a gate.** The C2 gate ended UNDETERMINED (2026-10-30) and the shadow's preregistered
  verdict is UNDETERMINED too: **2.4 of the 6 valid weeks** needed in the legacy window as of 2026-11-18. The
  owner's go replaces the gate. The plan is a prediction by a model that has not earned control; a bad plan is
  bounded by the floors, the fixed limits, the water range and the safe state, and costs kWh, not comfort-critical
  heating. The gate, the section-A verdicts and the comfort criteria of
  [climate-preregistration-2026-27](../planning/climate-preregistration-2026-27.md) keep reporting and are the
  evidence the owner reads after the switch.
- **Interaction with `dhw_control`.** It writes one entity, `switch.aquarea_force_dhw_mode`, which the optimizer
  never writes, and it holds the tank target at 50 degC (the 3 kW heater never starts). Both `Heat+DHW` and `DHW`
  include hot water, and the legacy pair already ran together in both modes through the winter of 2025-26 (the
  12 h forcing since 2025-12-29 against 10 to 15 heating hours a day; [regime log](../components/regime-log.md));
  inferred from that history, not isolated by an experiment, and the unit's DHW-over-heating priority is the unit's
  own. The load the optimizer plans is space heating only (DHW is transparent to its limits). The **climate
  comfort controller's DHW slots** (`input_text.comfort_dhw_plan`) do not exist without the closed drafts: the
  shadow keeps its fallback of yesterday's measured slots.
- **Two writers on one device, disjoint entities.** The optimizer writes the heat mode and the Z1 water request;
  AppDaemon writes only the force-DHW switch. The legacy `climate_control.py` is disabled in the same commit that
  goes live, and the `legacy_writer_active` guard keeps them from overlapping.
- **Import limit.** Contracted 5.0 kW, cut instantaneously; the heat pump alone draws up to 3.47 kW (about 2 kW at
  A7W35), a DHW cycle about 1.4 kW, the battery charge up to 2.5 kW including the house load, and the backup
  heater (not written, the unit's own policy) 3 kW. Nothing in the optimizer is joint: the climate actuator never
  reads the battery plan and the battery never commands the heat pump (ADR-0006 decision 6). The existing guards
  carry the overlap: the battery's 4500 W import cap and 4700 W hard rule, and the HP plan being price-blind (so it
  does not herd into the cheap hours the battery charges in). The load-step note of ADR-0007 (Omnibattery's PD
  change rate) applies to a heat-pump start while charging.
- **Components go stale** at the switch (28-day settling clock): battery-load-forecast, hp-consumption-forecast,
  heat-pump-controller, climate-mpc, thermal-model. The comfort controller's before/after evaluation of the design
  is replaced by the shadow's comfort criteria on the executed regime.
- **A wrong plan executes.** Unlike ADR-0008, nothing reviews the plan on the way (no consumer-side rules). The
  shadow-stage comparison of the plan with what the legacy controller did, and the supervised first day, are the
  mitigations the owner accepted in place of a `dry_run`.

## Rollback

Set `input_select.climate_optimizer_mode` to `shadow` or `off`: **immediate**, the actuator writes the safe state
once and stops, and the HA watchdog repeats it. Then, to remove the write path, set `climate_actuation.mode:
shadow` in the deployed config and roll out. To bring the legacy controller back, **revert the homelab switch
commit** (it re-enables `climate_control.py` and its watchdog and removes this ADR's helpers and watchdog). The
optimizer never restores an earlier configuration; the water target and the mode stay at the last written values
(the safe state is `Heat+DHW` at 26 degC).

## Switch to live (not yet approved)

1. **Release** the actuator (a code PR against this ADR; docs and a regime-log `planned` row only until then).
2. **Homelab PR deploying it with `climate_actuation.mode: shadow`** first (the select stays `shadow`): no write
   path, the shadow keeps running; verify the metrics exist and the helpers below do not yet matter.
3. **The switch PR (homelab), after the owner's explicit approval**: create `input_datetime.climate_optimizer_heartbeat`
   and the watchdog `climate_optimizer_watchdog`; create the per-room comfort floor helpers
   (`input_number.comfort_floor_<room>`, optionally `comfort_day_minimum_<room>`, ranges as in #4699, no
   `initial:`) when the owner wants per-room values; set `climate_actuation.mode: live` in
   `files/service-overrides.yaml`; remove `automation.appdaemon_climate_watchdog` and `input_boolean.appdaemon_climate_enable`;
   **disable and archive `climate_control.py`** (and its helpers) in homelab. The PR states the expected
   `Heating_Mode` (1), who controls DHW (`dhw_control`), and how to verify the pump after stopping the legacy app.
4. **Before the select goes `live`, the legacy toggle `input_boolean.appdaemon_climate_enable` is off (or its helper gone with the switch commit) and the app disabled.** **The owner then sets the select to `live` on a day they are home** and a heating day, watching the first runs:
   mode and water writes, heartbeat, a floor override, a fallback (stop the pod and watch the watchdog after 10 min).
5. **Regime-log row** and this ADR's record of the date and the owner's approval; the **28-day settling clock**
   starts; the five components above are marked `stale` in the same change, or recalibrated.

Approval record: *pending. The owner's go for the switch is not given by the acceptance of this ADR.*

## Open items

- **Water target entity.** `input_number.heishamon_heatshift_z1` is the only path (a helper of -5 to 40, shared by
  curve shift and direct value); the allowlist narrows it to 20 to 35 integers and the actuator requires direct mode.
  A dedicated narrower helper with its own automation is possible, but the existing automation is the proven path
  with echo handling; the owner may prefer it in the switch PR. A manual edit of the helper in the HA UI after the
  switch is overwritten by the next write.
- **Heat-mode entity.** `input_select.heishamon_heatmode`, not `input_select.appdaemon_climate_heat_mode`. The legacy
  app writes it by `set_state`; the optimizer uses the `input_select.select_option` service (as the legacy watchdog
  does), which triggers the same "Set heatmode" automation. Not tested live by this repository.
- **Dwell semantics.** Minimum on 30 min and off 20 min act on the heat-mode command, not on a compressor signal
  (the compressor also runs for DHW and defrost and is the unit's own). Whether to also read the compressor state is
  not decided.
- **Safe state and floor breach exempt from the minimum off** is a decision of this ADR, not measured: the unit has
  its own compressor protections, and a cold room outranks short-cycle protection.
- **Comfort floor and day-minimum helpers** do not exist; until they do the floor is the configured 21 degC. The
  owner's starting values are in [comfort-controller-design](../planning/comfort-controller-design.md) section 3.
  `input_boolean.comfort_away` and `input_boolean.comfort_control_enable` (read absent-tolerant by the shadow) do not
  exist either; there is no away mode in this release.
- **Heartbeat threshold (10 min) and tick (60 s)** are chosen here, not measured. The hourly plan tolerates one
  failed run (validity 90 min).
- **Force-DHW in each mode** is inferred from history (see above), not isolated.
- **Space heating is off for the season** (operating mode `DHW`, code 3, on 2026-11-18); the first live day needs a
  day the owner wants heating.

## Alternatives considered

| Alternative | Outcome |
|---|---|
| The AppDaemon comfort controller (#4699, #4746) as the single writer (ADR-0008) | Rejected by the owner (2026-11-18): a second code base to build and maintain for a plan the optimizer already computes; the drafts are closed and kept as reference |
| A `dry_run` stage before live | Not required by the owner; the code still has it |
| Writing through a new helper and an HA script that validates | Deferred: the allowlist plus the actuator guard validate before the wire; a narrower helper may come in the switch PR (open items) |
| A separate token for climate | Rejected: the live user's token is already mounted; the allowlist is the restriction (Home Assistant cannot scope tokens) |
| Heartbeat shared with the battery's | Rejected: a battery fault must not hide a climate fault and the reverse; separate helper, watchdog and thresholds |
| Writing thermostat setpoints (optimum start) | Not in this release (owner decision 4) |
