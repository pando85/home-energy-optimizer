# ClimatePlan contract v1

The interface between the climate optimizer (this repository) and whoever consumes its plan (the homelab
comfort controller in AppDaemon, dashboards). It is **advisory**: the optimizer publishes a plan and never
acts on it. The decision record is [ADR-0008](adr/0008-climate-plan-handoff-to-the-comfort-controller.md)
(**Proposed, not accepted**). The machine-readable contract is
[`schemas/climate-plan-v1.json`](schemas/climate-plan-v1.json) (JSON Schema 2020-12); a complete example
is [`schemas/climate-plan-v1.example.json`](schemas/climate-plan-v1.example.json). A test validates every plan
the code produces against the schema, so the schema and the code cannot drift.

**Safety statement.** The optimizer stays read-only toward Home Assistant for climate: the export is an
HTTP `GET` on the optimizer's own in-cluster service, it adds no Home Assistant service call, no Modbus or
REST write and no entity to the write allowlist (`service/ha_guard.py` is untouched), and the optimizer's
token stays read-only. **Turning the export on does not let anything act on the heat pump**: the real lock
lives in the consumer (ADR-0008, `optimizer_live_allowed: false`), and the optimizer cannot be effectively
`live` in this code version whatever the owner selects.

## Endpoints

Both are `GET`, read-only, ClusterIP only, `Content-Type: application/json`. The base URL is the in-cluster
service of the optimizer (`http://battery-optimizer.<namespace>.svc:8080` in the homelab; configure it, never
hard-code a cluster address in a repository).

| Endpoint | Answers | For |
|---|---|---|
| `GET /climate/plan` | `200` with the plan while one is current, else `404` with the reason | the consumer (control) |
| `GET /climate/plan/summary` | always `200`: a flat document with `state`, the modes and, while a plan is current, the current slot, the next heating run and the predicted room temperatures | dashboards |

`GET /status` also carries a small `climate_plan` object (state, modes, plan id, age, heating now) and
`/metrics` the `battery_optimizer_climate_plan_*` series ([service](service.md#climate-plan-export)).

### What each state serves

`state` is the same word in the summary, the `404` body of `/climate/plan`, the `/status` object and the
`battery_optimizer_climate_plan_state` metric. The first row that applies wins.

| `state` | Why | `/climate/plan` | `/climate/plan/summary` |
|---|---|---|---|
| `climate_shadow_disabled` | `climate_shadow.enabled` is false | `404` | `200`, `state` and the mode fields only |
| `mode_off` | the effective mode is `off` (HA select, else the config fallback; the default) | `404` | `200`, as above |
| `no_plan` | no decision since the service started and none stored to resume | `404` | `200`, as above |
| `expired` | `now >= valid_until` of the latest plan | `404` (body names `plan_id`, `valid_until`) | `200`, as above |
| `planned` | a current plan with slots | `200`, the plan | `200`, the plan's summary |
| `skipped` | current, but no plan was possible for a data reason (observer warming up, incomplete inputs) | `200`, no slots, `reason` set | `200` |
| `error` | current, but the solver or the decision failed | `200`, no slots, `reason` set | `200` |

A `404` body is `{"error", "state", "reason", "requested_mode", "effective_mode", "live_locked",
"live_lock_reason", "mode_source"}`: a reader always learns what the owner asked for and why nothing is
served. The shadow's own hourly run, records and scoring continue in every state (they are the
measurement); the export mode only gates what is served.

## The plan document

Top level of a `200` body of `/climate/plan` (see the schema for exact types and ranges).

| Field | Meaning |
|---|---|
| `version` | `1`. A consumer refuses any other value. |
| `advisory` | Always `true` in v1. |
| `plan_id` | 16 hex characters, a hash of the plan's content (everything below except the mode fields and `summary`). The same decision always yields the same id, also after an optimizer restart. Apply a plan once per `plan_id`. |
| `generated_at` | When the decision run finished (UTC, `+00:00`). |
| `valid_until` | `t0 + plan_ttl_minutes` (90 by default). **At or after it the plan is absent.** |
| `t0` | The decision boundary (the hour); the start of slot 0. The run happens at about `t0 + 10 min`, so slot 0 is already under way. |
| `code_version` | The optimizer build. |
| `regime` | `legacy`, `comfort`, `comfort_off` or `unknown`: the control regime the optimizer saw. |
| `status`, `reason` | `planned` (`reason` null) or `skipped` / `error` (`slots` null, `reason` says why; no plan is exported from a failed decision). |
| `model` | Lineage for monitoring: `prior_id`, `model_hash`, `mpc_config_sha256`, `solver_status`. |
| `inputs.comfort` | Per room (`bedroom`, `hall_kitchen`, `office_t`): `floor_c`, `day_min_c` the plan was computed with and whether each came from the controller's helper (`*_from_helper`) or from the optimizer's config. |
| `inputs.away` | Away mode at the decision (every room planned at its floor + 0.5 K), `null` if unknown. |
| `inputs.weather` | `forecast` or `persistence` (the last outdoor reading held). |
| `slots` | The per-slot plan (below), `null` unless `planned`. |
| `requested_mode`, `effective_mode`, `live_locked`, `live_lock_reason`, `mode_source` | The owner's mode and what the optimizer does with it ([Modes](#modes)). Served fresh on every request; not part of `plan_id`. |
| `summary` | Derived on every request for dashboards ([below](#summary)). A consumer never needs it. |

### Slots

Columnar (compact: the whole plan is about 5 KB). Slot `i` covers
`[slots.start + i * step_minutes, slots.start + (i + 1) * step_minutes)`; `step_minutes` is 15, `n` is 96
(24 h). Find the slot of "now" by time, never by position in time of arrival. All arrays have length `n`.

| Array | Values | Meaning |
|---|---|---|
| `heating` | `0` / `1` | Space heating **planned on**. It is the **permitted-heating lever**: the plan says when heating should be allowed; the heat pump still starts and stops under its thermostats and run logic. |
| `water_c` | integer 20..35 or `null` | The planned **absolute** water target (degC) while heating; `null` where `heating` is 0. Already clamped to the controller's fixed 20 to 35 degC range. |
| `dhw` | `0` / `1` | Slots the plan treated as hot-water production (the controller's DHW plan for today, else yesterday's measured slots). Heating is always 0 there. Informative: the optimizer plans no hot water. |
| `predicted_room_c` | per room, number or `null` | The model's air temperature at the **end** of each slot under this plan. **Monitoring only**: it is a prediction of a plan that may not be executed, never a setpoint. |

**What the MPC really plans, and the mapping to levers.** The price-blind comfort MPC
([climate-mpc](components/climate-mpc.md)) chooses, per 15-minute slot, one binary *heat at water level k*
or *off*. The levels are the software curve (outdoor `-5 degC` to `32 degC` water, `15 degC` to `25 degC`,
linear, clamped) at the forecast outdoor temperature plus an offset of 0 to +6 K, rounded and clamped to
20..35 degC. So the MPC plans exactly two levers, both exported as is:

1. *on/off* of space heating -> `heating`;
2. *the absolute water target* the on-slot uses (curve + offset) -> `water_c`.

It does **not** plan: thermostat setpoints (it assumes the controller's: floor / day minimum + margin, with
optimum start), hot water (an input), the backup heater (never), boost, or cooling. None is in the plan, and
a consumer must not infer one. The MPC's minimum on (30 min) and minimum off (effectively 30 min) are
respected **inside the plan**; the consumer enforces its own limits on top (below).

### Summary

`summary` (and the flat `/climate/plan/summary`) are computed at the time of the request from the slots, so a
dashboard needs no templating beyond picking fields:

| Field | Meaning |
|---|---|
| `as_of`, `age_s`, `expires_in_s` | Request instant, seconds since `generated_at`, seconds until `valid_until` (negative never occurs in a served plan). |
| `slot_index`, `slot_start` | The slot in progress (`null` when the instant is outside the slots). |
| `heating_now`, `water_c_now`, `dhw_now` | The plan for the slot in progress (`null` for `skipped` / `error`). |
| `next_heating_start` | Start of the next heating run after the current slot (`null`: none in the horizon). |
| `next_heating_stop` | End of the current heating run if heating now, else of the next one. A run that reaches the end of the horizon ends there. |
| `heating_slots` | Planned heating slots over the horizon. |
| `predicted_room_c` | `{hours_ahead: [1..6], rooms: {room: [degC...]}}`: the predicted temperature `h` hours after the start of the current slot. |

`/climate/plan/summary` adds, flat: `state`, `reason`, the five mode fields, `plan_id`, `status`, `regime`,
`generated_at`, `valid_until`, `code_version`, `comfort` (per room, as `inputs.comfort`) and `away`. When no
plan is current only `version`, `advisory`, `state`, `reason` and the mode fields are present.

## Modes

The owner's control is the Home Assistant select **`input_select.climate_optimizer_mode`** with the options
exactly **`off`**, **`shadow`**, **`live`**. The optimizer only **reads** it, through the state subscription
it already has (the same way as the battery kill switch); the helper is created and owned by the homelab.

| Requested | Effective | Plan served | `live_locked` |
|---|---|---|---|
| `off` | `off` | no (`404`, `state: mode_off`) | false |
| `shadow` | `shadow` | yes, advisory | false |
| `live` | `shadow` | yes, advisory, identical to `shadow` | **true**, `live_lock_reason` set |

- **The effective mode is never `live`** in this code version (a constant and a test enforce it). `live`
  is a request: the plan says `requested_mode: "live"`, `effective_mode: "shadow"`, `live_locked: true`,
  `live_lock_reason: "ADR-0008 proposed, not accepted; homelab hard lock optimizer_live_allowed=false"`, and
  `battery_optimizer_climate_plan_live_locked` is 1, so a dashboard can show "live requested but locked".
- **Fallback.** A missing, `unavailable`, `unknown` or otherwise unrecognised helper state falls back to
  `climate_shadow.optimizer_mode` of the service config (default `"off"`); `mode_source` says `helper` or
  `config`. A garbage value never raises the mode.
- Nothing in `off` stops the climate shadow's own run, records or scoring.

## Validity, idempotency and fallback (rules for the consumer)

These are normative for a consumer; ADR-0008 repeats the ones that matter for safety.

1. **Absent means absent.** Treat the plan as absent when the request fails, is a `404`, is not valid JSON,
   fails schema validation, has `version != 1`, `advisory != true`, `status != "planned"`, has
   `now >= valid_until` (check **again** at every use, not only when fetched) or has a slot that does not
   cover `now`. Absent means the **rules** (the comfort controller's own logic), never "off" and never "keep
   the last plan".
2. **Ignore unknown fields.** Within v1 fields may be added (the schema is updated in the same change);
   a removal, a rename or a change of meaning is a new `version` and a new schema file.
3. **Idempotency.** A plan is identified by `plan_id`. A consumer that already applied a plan must not
   re-apply it as new; a replan (new `plan_id`) supersedes the previous one entirely.
4. **Comfort wins.** `heating = 0` never overrides a comfort floor: a room below its floor (per room helper)
   forces heating whatever the plan says. `heating = 1` never overrides the consumer's own run logic or limits.
5. **Consumer limits stay in the consumer.** Water 20..35 degC integers, tank at most 50 degC, compressor
   minimum on and off, write only when the target changes by at least 1 K and at most once per 15 min, never the
   backup heater, no sterilisation, no boost. The plan's `water_c` is a request that fits those limits by
   construction; the consumer clamps again.
6. **Time.** All instants are UTC with `+00:00`. Slots are on the UTC quarter-hour grid; local time is the
   consumer's business (DST does not move a slot).
7. **Cadence.** The optimizer replans hourly, about `t0 + 10 min`. Polling every 1 to 5 minutes is plenty; a
   missed run leaves 20 minutes of margin before the plan expires.

## Reading it from Home Assistant

Follow the pattern of the battery plan (a `rest_command` with `response_variable` and template sensors reading the
response), against **`/climate/plan/summary`**: it answers `200` in every state, so the sensors can show
`mode_off`, `expired` or `live requested but locked` instead of going unavailable. Because `rest_command`
exposes the status and the body, `/climate/plan` can also be read when the consumer needs the slots.

```yaml
# sketch: the homelab owns the real file
rest_command:
  climate_optimizer_summary:
    url: http://battery-optimizer.<namespace>.svc:8080/climate/plan/summary
    method: get
    timeout: 10
```

Sizes on a realistic fixture (96 slots, three rooms): `/climate/plan` about 4.9 KB, `/climate/plan/summary`
about 1.3 KB. Home Assistant limits the attributes of one state object to about 16 KB; the full plan fits in
one sensor's attributes with room to spare, but prefer small sensors (state and a few attributes from the
summary) and keep the slots in the `rest_command` response, which has no such limit.

## Changing the contract

A change to the plan document updates, in one commit: `climate/plan_export.py`,
`docs/schemas/climate-plan-v1.json` (and the example), this page and the tests. A breaking change is a new
`version`, a new schema file and a new endpoint path or negotiation, announced in the ADR.
