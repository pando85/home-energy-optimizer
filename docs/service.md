# Resident shadow service

`battery-optimizer serve` is the Phase C runtime of
[ADR-0004](adr/0004-battery-optimizer-production-architecture.md) (plan
[§16](planning/home-battery-optimizer-implementation.md#16-production-packaging-and-runtime-rollout),
PROD-C1 to C3). It is a single resident process that reads Home Assistant and Prometheus, runs the
same MPC kernel as the offline benchmarks every 15 minutes, records the resulting **shadow** plans,
and compares them with what the native battery controller actually did.

**In `mode: shadow` (the default) there is no actuation path.** The Home Assistant adapters cannot write a
state or call an actuating service (see [Safety](#safety)), there is no Modbus code, and plans are stored as
intents that are never sent anywhere. The one service call they can make is the non-mutating
`weather.get_forecasts` query used by the [weather-forecast archive](#passive-archives).

`mode: dry_run` and `mode: live` add the actuator of
[ADR-0007](adr/0007-live-battery-control.md) (Accepted 2026-11-03): it maps the current slot's intent to
Omnibattery's Home Assistant entities, `dry_run` only records the writes, `live` sends them through a narrow,
mode-gated allowlist. See [Live control](#live-control-dry_run-and-live).

## Architecture

```
                 Home Assistant                       Prometheus (long-term)
   WebSocket (subscribe + get_states)  REST GET       raw range-vector samples
                │                         │                     │
        ha_client.HAWebSocketClient  HARestClient       history.PrometheusHistory
                └──────────┬──────────────┘                     │  (worker thread, once a day)
                     entity_store.EntityStore                   │
            (source-time ordering, freshness)                   │
              │              │            │                     │
        observer.SlotObserver  pvpc.parse_pvpc_state   history.forecast_load
              │                        │                        │
              │        runtime.ShadowService.build_snapshot ────┘
              │                        │  PlanningSnapshot (frozen, hashed)
              │            ThreadPoolExecutor(max_workers=1) + timeout
              │                        │
              │        planner.run_planner = oracle.solve_oracle(free terminal)
              │                          + dispatch.intents_from_schedule
              │                        │
              └──────► slot open/close ─┴─► sink.ShadowSink (append-only JSONL / plan files)
                                │                     │
                     virtual_battery (counterfactual)  shadow_report (offline comparison)
                                │
                         api (FastAPI): /health/live /health/ready /status /plan /metrics
```

| Module (`battery_optimizer.service`) | Role |
|---|---|
| `config` | `config/service.yaml` + environment; `mode: shadow` (default), `dry_run` or `live` |
| `ha_guard` | Read-only allowlist for every WebSocket frame and REST request (plus the one `weather.get_forecasts` exception, and in `live` only the Omnibattery write allowlist, including the weekly-full-charge switch and day select when `full_charge.enabled`) |
| `actuator` | `dry_run`/`live` only: guard, intent → Omnibattery mapping, ordered writes with readback, kill switch, heartbeat ([ADR-0007](adr/0007-live-battery-control.md)) |
| `ha_client` | WebSocket session (auth, subscribe, resync, heartbeat, reconnect) and GET-only REST |
| `entity_store` | Latest state per entity, ordered by HA source time, freshness from `last_reported` |
| `pvpc` | Publication-aware price curve from `sensor.esios_pvpc` attributes |
| `history` | Prometheus load history (offline primitives) and the causal load forecast |
| `planner` | Snapshot → offline MPC kernel → time-bounded shadow intents |
| `observer` | Per-slot measurements (counter energy, power averages, SOC edges) |
| `virtual_battery` | Open-loop replay of decisions against measured load |
| `battery_health` | Pure, read-only battery health telemetry: temperature exposure, charge sessions, cell voltages, Omnibattery balance monitor ([Battery health telemetry](#battery-health-telemetry)) |
| `full_charge` | Pure decision rule, adaptive cadence and run state machine of the periodic full charge ([scheduler](#periodic-full-charge-the-scheduler)); the runtime does its I/O |
| `runtime` | Scheduling, readiness, bounded executor, slot cycle, metrics snapshot |
| `archive` | Record builders and the fetch schedule of the passive [archives](#passive-archives) |
| `sink` | Append-only store under the data directory |
| `shadow_report` | Shadow-vs-native comparison (`shadow-report`) |
| `plan_tracker`, `plan_kpis` | The [day-ahead plan](#day-ahead-and-final-plan) of every slot (rebuilt from the plan files on start) and its offline KPIs (`hack/day_ahead_kpis.py`) |
| `api`, `serve`, `metrics` | HTTP surface, uvicorn wiring, Prometheus metrics |

### What is reused from the offline code

* **Optimizer:** `oracle.solve_oracle(terminal_mode="free", terminal_value, floor_kwh, throughput_cost, high_soc_cost, charge_soft_cap, discharge_margin_kw)`
  — the call `rolling_horizon.run_rolling_horizon` makes per event — then
  `dispatch.intents_from_schedule`, which re-checks the plan against the battery kernel.
* **Battery parameters:** `benchmark.resolve_battery_assumptions` on `tariff.yaml` (`sqrt(RTE)` per
  leg, SOC window, import ceiling).
* **Forecast:** the registered load forecaster `planning.load_forecaster` (default `trailing_mean`,
  the 28-day same-slot mean `policies.forecast_load_trailing_slot_mean`), run through
  `load_forecast.run_forecaster` with the information cut at the local midnight of the decision day,
  which is the `CausalForecastProvider` rule. Config validation rejects oracle forecasters such as
  `perfect`.
* **History:** `extract_prom.query_range_vector` (instant queries over range vectors, never
  `query_range`), `build.stitch_series` / `counter_deltas` / `allocate_energy`.
* **Execution clipping:** `policies._clip_plan_to_model_bounds`, used by the rolling-horizon benchmark
  to execute a forecast-made plan against measured load.
* **Store:** `paths.write_part_atomic` for plan files.

### Deliberate deviations (and why)

| Topic | Benchmark | Service | Reason |
|---|---|---|---|
| SOC floor | Evaluation window's opening SOC (seed protection) | Physical window minimum (`soc_window_min`) | Seed protection is a fairness device between policies; live energy in the battery is real and is valued at the terminal value instead |
| Price horizon | May plan past publication with exact or noise-perturbed true prices | Ends at the last published slot (capped by `max_horizon_slots`) | Unpublished prices do not exist live; the trailing-mean terminal value keeps the plan from emptying the battery at the edge |
| Orchestration | `replay_runtime.ReplayRuntime` | `runtime.ShadowService` | The replay runtime requires a temperature reading, keeps an unbounded in-memory record list and uses a fixed-terminal oracle adapter; the service borrows its rules (one worker, timeouts never free the worker, obsolete plans discarded) |
| SOC | — | `soc_source: entity` (committed); `simulated` without a battery | The measured SOC entity, checked for availability and Omnibattery's liveness sensor; `simulated` is a virtual battery that evolves by applying the service's own decisions against measured load (see below) |

## Scheduling and the slot cycle

All slots are half-open UTC intervals on a 15-minute grid; Europe/Madrid only labels them, so the
2026-03-29 and 2026-10-25 local days have 92 and 100 slots with no ambiguous or missing slot. For
every boundary `B`:

1. `B - solve_lead_s` (120 s): scheduled solve; the plan's first slot is `B`.
2. `B`: the slot opens and its decision is **frozen**: the actionable intent valid at `B` from the
   newest unexpired plan made before `B`, or no decision with a reason (`no_plan`, `plan_expired`,
   `not_ready`). If any input is stale at `B`, the decision is `not_ready` even if a plan exists. A slot
   without an intent can still adopt one later in the slot (late adoption, below).
3. `B + slot_close_grace_s` (20 s): the previous slot closes — measurements, the frozen decision,
   the virtual-battery step and the costs are appended to the store.

**Late adoption.** A slot that opened **without** an intent (`no_plan`, `plan_expired`, `not_ready`) is
re-evaluated every 15 s and after every new plan until it ends. Once every input is ready and an unexpired plan
solved by this process has an actionable intent covering the slot, the slot adopts it for the rest of the slot:
the decision becomes `late_intent`, and from then on the slot is frozen like any other. A slot that opened
**with** an intent is never re-decided, whatever plans arrive mid-slot. Typical cases: a pod restart (deploy,
config change), whose first slot used to stay in safe idle for up to 15 minutes, and a transient not-ready at
`B` (the day's load history is fetched just after local midnight, so the slot at local midnight usually opened
`history_unavailable`).

```
                 open at B
  ┌─────────────────┴──────────────────┐
  intent ── frozen until B + 15 min     no intent (no_plan / plan_expired / not_ready)
                                          │ every 15 s and on every new plan:
                                          │ inputs ready and an unexpired own plan covers the slot?
                                          ├── no: stays without intent (safe idle in live)
                                          └── yes: late_intent, frozen until B + 15 min
```

For a slot without intent to have a plan to adopt, a solve that starts inside such a slot, before
`B + 15 min - solve_lead_s`, plans from the **slot in progress** instead of the next boundary (a *catch-up*
plan: its first slot began before its decision time, and it expires `plan_ttl_slots` slots after that first
slot as usual). The event solve after the first HA sync or the history refresh is the usual one at startup;
and whenever the inputs are ready inside such a slot but no plan covers it, the scheduler requests one itself
(`trigger: catch_up`), at most once per slot. The planner models the catch-up plan's first slot whole; the intent is a
power target held only for the rest of the slot, so the battery moves less energy than planned in that slot,
never more, and the next scheduled solve starts from the measured SOC again. In the last `solve_lead_s` of a
slot a solve plans from the next boundary, as the scheduled solve always does.

Extra solves are requested after a completed resync, newly published next-day prices, a refreshed
history or an SOC change of at least `replan.soc_change_frac`; they are debounced and rate-limited.
Solves run one at a time in a single-thread executor with `solve_timeout_s`. A request while the
worker is busy is skipped and counted (`busy`); a timed-out solve keeps the worker until its thread
really ends. A plan whose first slot has started by the time it lands is discarded as obsolete; a catch-up
plan, whose first slot is the slot it was made in, is discarded only when that slot has ended.

### Intents

Each plan yields one intent per slot with `plan_id`, `plan_sequence`, `valid_from`, `valid_until`
(`min(interval end, plan expiry)`), mode, requested power, SOC target, expected price,
`inputs_hash` (SHA-256 of the frozen snapshot) and `code_version`; every record carries
`shadow: true`. A plan expires `plan_ttl_slots` (2) slots after its first slot, so intents beyond the
expiry are recorded as forecast only (`actionable: false`) and an old plan can never be "current".
A restart never resumes a persisted plan.

### Day-ahead and final plan

The service replans every 15 minutes, so the plan of record for a slot (what `current_intent` and the
Home Assistant "planned" sensors show) is made at most a quarter of an hour before the slot and hides
the quality of the forecast. Three names are used everywhere (API fields, the `plan` label of the
metrics, the slot rows, this page):

* **day-ahead plan** of a slot: the **first** plan, by `(generated_at, plan_sequence)`, whose intents
  covered the slot and that was generated at or before the slot's start. Usually it is the plan made the
  previous afternoon when the next day's PVPC is published, so the lead time varies (hours to a day and a
  half ahead, less after a late publication). A plan made inside the slot (a
  catch-up plan) is not its day-ahead plan: the slot has none, which shows as a missing sample or `null`,
  never as a zero. Intents beyond the plan's expiry (forecast only, `actionable: false`) count: the
  day-ahead plan is a forecast, not a command;
* **final plan**: the plan of record when the slot opened, i.e. the intent frozen at the open (what
  `current_intent` is, `late_intent` included);
* **actual**: what the slot close measured (battery energy, grid, SOC, gross load).

Per slot and plan the service keeps the signed battery power (`+` charge, `-` discharge, like
`current_intent`), the SOC target at the slot's end, the house-load forecast of the slot (the plan's
`load_kwh` input; `inputs.load_kwh` in the plan file) and the expected buy price. Tracking is
`service/plan_tracker.py`: one entry per slot from two hours back to the end of the longest horizon
(bounded, past slots pruned). On start the tracker is rebuilt from the immutable plan files of the last two
UTC days (`rebuild_day_ahead`, off the event loop, logged as `day-ahead rebuilt plan_files=... slots=...`);
because the choice is by `(generated_at, plan_sequence)` and not by arrival, the rebuild gives what an
uninterrupted process would hold. A failed or partial rebuild only means missing day-ahead values. Nothing
here feeds the planner or the actuator: it is observability.

`/plan` gains two keys (everything else is unchanged; HA templates reading `current_intent.signed_power_w`,
`soc_target_frac` or `plan.intents[*]` keep working):

```json
{
  "current_intent": {"plan_id": "...", "signed_power_w": -800.0, "soc_target_frac": 0.31, "...": "..."},
  "current_slot": {
    "slot_start": "2026-11-05T17:15:00+00:00",
    "slot_end": "2026-11-05T17:30:00+00:00",
    "decision": "intent",
    "day_ahead": {
      "plan_id": "<plan_id>", "plan_sequence": 1790000001,
      "generated_at": "2026-11-04T15:00:12+00:00", "lead_seconds": 94488.0,
      "mode": "discharge", "signed_power_w": -600.0, "soc_target_frac": 0.35,
      "load_forecast_kwh": 0.21, "load_forecast_w": 840.0,
      "expected_buy_price_eur_per_kwh": 0.231,
      "charge_kwh": 0.0, "discharge_kwh": 0.15
    },
    "final": {"plan_id": "...", "lead_seconds": 105.0, "signed_power_w": -800.0, "...": "same keys"}
  },
  "day_ahead": {
    "slots": [
      {"slot_start": "2026-11-05T17:15:00+00:00", "...": "the day_ahead keys above",
       "latest": {"plan_id": "...", "generated_at": "...", "signed_power_w": -800.0,
                  "soc_target_frac": 0.31, "load_forecast_w": 800.0}}
    ]
  }
}
```

`current_slot.day_ahead` / `final` are `null` when the slot has none (before the first slot has opened,
after a first deploy, a slot without a decision). `day_ahead.slots` lists the slots that have not ended, the
one in progress first, each with the newest plan's values as `latest` (`null` when the newest plan does not
reach the slot), so Home Assistant can chart the day-ahead plan against the latest one. `charge_kwh` /
`discharge_kwh` are the AC energy the plan asks for over the whole slot.

## Readiness

`/health/ready` is 200 only when every input is fresh **and** an unexpired plan was generated in
the last `interval + solve_lead + 60 s`; otherwise 503 with the reasons (bounded set, also exported as
`battery_optimizer_not_ready_reason{reason}`):

| Reason | Meaning |
|---|---|
| `ha_unconfigured` | `BATTERY_OPTIMIZER_HA_URL` / token not set |
| `ha_disconnected`, `ha_not_synced` | WebSocket down, or connected but the post-connect `get_states` has not completed |
| `grid_power_stale` | Grid power missing, unavailable or older than `freshness_s.grid_power` |
| `soc_unconfigured`, `soc_stale` | SOC entity not mapped (entity mode) / missing, unavailable, out of range, older than `freshness_s.battery_soc` when set, or the battery liveness sensor not exactly `None` |
| `prices_missing`, `prices_insufficient` | No PVPC state, or fewer than `min_horizon_slots` published slots ahead |
| `history_unavailable`, `forecast_unavailable` | Load history not fetched for today / no usable history for a target slot |
| `tariff_invalid` | `tariff.yaml` missing or unconfirmed battery fields |
| `no_recent_plan` | No plan recent enough (readiness only, never a reason to skip a solve) |

Freshness is measured from HA's own `last_reported`, not from when the service received the
value. HA fires no `state_changed` event while a sensor keeps reporting the same value, so the
planning entities are also refreshed by REST GET every `poll_interval_s` (60 s).

## Configuration

`config/service.yaml` (override with `--service-config` or `BATTERY_OPTIMIZER_SERVICE_CONFIG`; site values
belong in a sparse [overrides file](configuration.md#service-config-overrides) merged over it) maps
roles to entity IDs and holds freshness, planning and observation settings; the file documents every
key. Entity IDs verified against the live registry on 2026-10-25 with the read-only token; the battery
roles mapped at commissioning on 2026-11-03:

| Role | Entity | State |
|---|---|---|
| `grid_power` | `sensor.shellyem_main_channel_1_power` | present (W; + import, negative on export, verified 2026-11-03) |
| `grid_energy` | `sensor.shellyem_main_channel_1_energy` | present (kWh import counter) |
| `price` | `sensor.esios_pvpc` | present (`pvpc_updated`; `price_HHh`, `price_next_day_HHh` after ~20:00, `_d` on the repeated hour) |
| `battery_soc` | `sensor.venus_battery_soc` | present (%; reported on change only) |
| `battery_charge_energy` / `battery_discharge_energy` | `sensor.venus_total_charging_energy` / `sensor.venus_total_discharging_energy` | present (kWh, AC side, 0.01 kWh resolution; reported on change only; the discharge counter includes the battery's supply to the backup port) |
| `battery_power` | — | not mapped: `sensor.venus_ac_power` is + while discharging, this role is + charge |
| observed | `sensor.shellyem_main_channel_2_power` | present (aerotherm branch, context only) |

The committed config uses `soc_source: entity`. The SOC is reported only on change, so it is checked as
the actuator checks it: availability only (`freshness_s.battery_soc: null`, the default; a number restores
an age test), and `actuation.entities.battery_liveness` (`sensor.omnibattery_non_responsive_batteries`, read
in every mode with `soc_source: entity`) must be exactly `None`; otherwise planning is refused with
`soc_stale` ([known issues](known-issues.md#planning-from-the-soc-entity-checks-availability-and-liveness-not-age)). The
two battery energy counters are reported on change only too, so `freshness_s.battery_charge_energy` and
`freshness_s.battery_discharge_energy` are also `null` by default (availability only; previously they
inherited `freshness_s.grid_energy`, 900 s): an idle battery no longer shows them stale in `/status` (`max_age_s`
null) and `input_stale`. They gate neither planning nor actuation, so no liveness test is added; a number
restores the age test. With the
counters mapped, the load history is `grid - charge + discharge` per slot, except slots that end at or before
`battery.commissioned_at`, which are the grid counter alone (the counters need not reach back that far).

The `actuation:` section is read only in `dry_run` and `live` (it is validated in every mode), except
`entities.battery_liveness`, which the planner reads with `soc_source: entity`; see
[Live control](#live-control-dry_run-and-live).

The optional `archive:` section switches the two [passive archives](#passive-archives) on; without it
both are off, so a deployed config must carry it. The `climate_shadow:` section (off by default) runs the
[climate shadow](#climate-shadow-track-b2); its Home Assistant helpers are those of the homelab
comfort controller (and the owner's `entities.mode` select, see [Climate plan export](#climate-plan-export)),
and each may be absent until it is deployed.

The optional `battery_health:` section names the read-only battery health sensors (all optional, null in
code; the committed file names the `sensor.venus_*` ones of the Omnibattery balance monitor and the cell data);
see [Battery health telemetry](#battery-health-telemetry).

The optional `planning.thermal:` section (`mode: "off"` by default; quote it, a bare YAML `off` is a boolean)
runs the read-only [thermal shadow](#thermal-shadow-derate-prediction).

The variables the service reads (the complete table for every command is in
[Configuration](configuration.md#environment-variables)):

| Variable | Purpose | Default |
|---|---|---|
| `BATTERY_OPTIMIZER_HA_URL` | Home Assistant base URL (the in-cluster Service URL, set by the homelab manifest) | unset → not ready |
| `BATTERY_OPTIMIZER_HA_TOKEN` / `BATTERY_OPTIMIZER_HA_TOKEN_FILE` | Non-admin long-lived token (value or mounted file): the read-only `battery-optimizer` user's in `shadow`/`dry_run`, the separate `battery-optimizer-live` user's in `live` | unset |
| `BATTERY_OPTIMIZER_PROMETHEUS_URL` | The long-term Prometheus holding the `hass_*` series (in-cluster Service URL) | unset → not ready |
| `BATTERY_OPTIMIZER_DATA_DIR` | Store root (`/data` on a PVC); must be outside a git tree | `~/data/energy-baseline` |
| `BATTERY_OPTIMIZER_CONFIG` | `tariff.yaml` | `config/tariff.yaml` |
| `BATTERY_OPTIMIZER_SERVICE_CONFIG` | `service.yaml` | `config/service.yaml` |
| `BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES` | Sparse site overrides merged over `service.yaml` ([semantics](configuration.md#service-config-overrides)) | unset |
| `BATTERY_OPTIMIZER_PORT` | HTTP port | `8080` |
| `BATTERY_OPTIMIZER_GIT_SHA` | Build-time code identity (set by the image) | — |

The image ships the base configuration in `/app/config/` (the working directory), so both config variables
can stay unset; a site mounts only a sparse overrides file (a ConfigMap) and points
`BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES` at it. The applied overrides are listed in `/status` as
`config_overrides` and logged at start. The process writes only under the data directory, so it runs with a
read-only root filesystem and needs no `/tmp`.

## HTTP endpoints

All `GET`, all read-only; there is no OpenAPI/docs route and no writable route (other methods
return 405). Bind address `0.0.0.0`, container port **8080**.

| Path | Body |
|---|---|
| `/health/live` | `{"status": "alive"}` — the process and its event loop are up |
| `/health/ready` | `{ready, inputs_ready, reasons, details}`; 200 or 503 |
| `/status` | Mode, adapter state (connects, reconnects, resyncs, failures, last error), per-entity freshness, price curve summary, history summary, last solve (timing, outcome), virtual battery, the climate shadow (prior, model hash, last run, queue, error, go-live, verdicts), `climate_plan` (the plan export: state, requested and effective mode, live lock, plan id, age, heating and water target now; `null` while the climate shadow is disabled), `battery_health` (only when a `battery_health.entities` role is configured), `thermal` (only with `planning.thermal.mode: shadow`), `full_charge` (only with `full_charge.enabled`), code version, config hash; in `dry_run`/`live` also `actuator` (kill switch, in control, guard reasons, desired and observed state, alert, last error, import-trip hold, last tick / confirmed write / heartbeat) |
| `/plan` | Latest plan with its snapshot summary, solver result and intents, plus the current intent (all flagged `shadow`), and the [day-ahead and final plan](#day-ahead-and-final-plan) of the current slot (`current_slot`) and of every upcoming slot (`day_ahead.slots`); with `planning.thermal.mode: shadow` the plan also carries its `thermal` block and `current_slot.thermal` the newest prediction of the slot |
| `/climate/plan` | The advisory [ClimatePlan v1](climate-plan-contract.md) of the latest climate decision, `200` while one is current, else `404` with `state` and `reason` (`climate_shadow_disabled`, `mode_off`, `no_plan`, `expired`); see [Climate plan export](#climate-plan-export) |
| `/climate/plan/summary` | The same for a dashboard: flat, always `200`, with `state`, the requested and effective mode, the live lock and, while a plan is current, the slot in progress, the next heating run and the predicted room temperatures |
| `/metrics` | Prometheus exposition |

Kubernetes probes: use `/health/live` for liveness **and readiness**. `/health/ready` answers 503
whenever an input is stale, which would remove the pod from the Service endpoints and stop the
ServiceMonitor scrape exactly when the metrics matter; alert on `battery_optimizer_ready` instead.

## Metrics

Prefix `battery_optimizer_`; labels are bounded (roles, configured entity IDs, fixed enums). No plan
ID, price, timestamp or household reading is a label.

| Metric | Type | Labels |
|---|---|---|
| `build_info` | gauge (1) | `code_version`, `version`, `soc_source`, `mode` |
| `config_override_info` | gauge (1) | `path` (one series per key of the site overrides file) |
| `ready`, `inputs_ready` | gauge 0/1 | — |
| `not_ready_reason` | gauge 0/1 | `reason` |
| `ha_connected`, `ha_synced` | gauge 0/1 | — |
| `ha_reconnects_total`, `ha_resyncs_total`, `ha_state_events_total` | counter | — |
| `ha_failures_total` | counter | `stage` (`connect`, `auth`, `subscribe`, `get_states`, `heartbeat`, `disconnect`, `protocol`) |
| `ha_state_updates_total` | counter | `result` (`accepted`, `refreshed`, `duplicate`, `out_of_order`) |
| `ha_rest_polls_total` | counter | `result` |
| `input_age_seconds`, `input_stale` | gauge | `role`, `entity` |
| `price_horizon_seconds`, `prices_tomorrow_published` | gauge | — |
| `solve_duration_seconds` | histogram | — |
| `solves_total` | counter | `result` (`ok`, `error`, `timeout`, `discarded`) |
| `skipped_solves_total` | counter | `reason` (`busy` + not-ready reasons) |
| `solve_in_progress` | gauge | — |
| `plan_age_seconds`, `plan_horizon_slots` | gauge | — |
| `current_intent_power_watts`, `current_intent_active` | gauge | — |
| `slot_battery_power_watts` (+ charge), `slot_soc_target_ratio`, `slot_load_forecast_watts` (mean over the slot), `slot_plan_lead_seconds` | gauge: the slot in progress, frozen at its open; no sample for a plan the slot does not have | `plan` (`day_ahead`, `final`) |
| `shadow_day_ahead_energy_kwh_total` | counter: AC energy the day-ahead plans requested, over the slots that had one (the final plan's is `shadow_planned_energy_kwh_total`, the actual `shadow_actual_battery_energy_kwh_total`) | `direction` |
| `plan_revision_kwh_total` | counter: sum over slots with both plans of `|day-ahead - final|` requested net battery energy | — |
| `load_forecast_scored_slots_total`, `load_forecast_kwh_total`, `load_actual_kwh_total`, `load_forecast_abs_error_kwh_total` | counter, at slot close, over the slots with a forecast of that plan and a measured gross load | `plan` |
| `history_refresh_total` | counter | `result` |
| `history_usable_slots`, `history_age_seconds`, `terminal_value_eur_per_kwh` | gauge | — |
| `shadow_slots_total` | counter | `decision` (`intent`, `late_intent`, `no_plan`, `plan_expired`, `not_ready`, `not_observed`) |
| `shadow_planned_energy_kwh_total`, `shadow_actual_battery_energy_kwh_total` | counter | `direction` |
| `shadow_native_cost_eur`, `shadow_counterfactual_cost_eur` | gauge (since start; PVPC can be negative) | — |
| `shadow_comparable_slots_total`, `shadow_soc_violations_total`, `shadow_clipped_energy_kwh_total` | counter | — |
| `virtual_battery_soc_ratio`, `observer_late_samples` | gauge | — |
| `sink_write_errors_total` | counter | — |
| `pvpc_snapshots_total` | counter | `result` (`written`, `failed`) |
| `weather_forecast_fetches_total` | counter | `result` (`ok` = fetched and written, `failed`) |
| `archive_last_success_timestamp_seconds` | gauge (absent until the first success) | `archive` (`pvpc_snapshots`, `weather_forecast`) |
| `climate_shadow_runs_total` | counter | `result` (`planned`, `plan_failed`, `no_state`, `no_inputs`, `error`) |
| `climate_shadow_solves_total` | counter | `status` (`optimal`, `gap`, `time_limit`, `infeasible`, `error`) |
| `climate_shadow_solve_duration_seconds` | histogram (one MILP solve) | — |
| `climate_shadow_scores_total` | counter | `result` (`scored`, `expired`, `failed`) |
| `climate_shadow_refits_total` | counter | `result` (`refitted`, `prior_kept`) |
| `climate_shadow_up`, `climate_shadow_scoring_queue` | gauge | — |
| `climate_shadow_last_run_timestamp_seconds`, `climate_shadow_last_refit_timestamp_seconds` | gauge | — |
| `climate_shadow_regime` | gauge 0/1 (the regime at the last run) | `regime` (`legacy`, `comfort`, `comfort_off`, `unknown`) |
| `climate_shadow_last_plan_top_offset_share_ratio` | gauge | — |
| `climate_plan_state` | gauge 0/1 (state set; what `/climate/plan` serves) | `state` (`planned`, `skipped`, `error`, `mode_off`, `no_plan`, `expired`) |
| `climate_plan_mode_requested`, `climate_plan_mode_effective` | gauge 0/1 (state set; effective is never `live`) | `mode` (`off`, `shadow`, `live`) |
| `climate_plan_live_locked` | gauge 0/1: live requested and refused (ADR-0008 not accepted) | — |
| `climate_plan_age_seconds` | gauge (since the latest plan was generated; absent before the first) | — |
| `climate_plan_valid` | gauge 0/1 (1 while the latest plan is inside `valid_until`) | — |
| `climate_plan_heating_planned_now`, `climate_plan_water_target_celsius` | gauge: the current slot of a `planned` plan; **absent** when mode is off, no plan is current or the plan has no slots (absent is not 0); the water target only while heating is planned | — |
| `climate_plan_served_total` | counter | `endpoint` (`plan`, `summary`), `result` (`ok`, `unavailable`) |
| `climate_shadow_rmse_kelvin`, `climate_shadow_skill_ratio`, `climate_shadow_abs_bias_kelvin`, `climate_shadow_origins` | gauge | `window` (`legacy`, `new`), `horizon` (`1h`, `6h`, `12h`, `24h`) |
| `climate_shadow_daily_heat_mdape_ratio`, `climate_shadow_valid_weeks`, `climate_shadow_top_offset_share_ratio` | gauge | `window` |
| `climate_shadow_gate` | gauge 0/1 (state set) | `window`, `criterion` (the six of the preregistration and `verdict`), `status` (`PASS`, `FAIL`, `UNDETERMINED`) |
| `thermal_*` | see [Thermal shadow](#thermal-shadow-derate-prediction): only with `planning.thermal.mode: shadow`, absent otherwise | — |
| `battery_temperature_celsius` | gauge (absent while unknown) | — |
| `battery_hot_seconds_total` | counter (seconds strictly above the threshold since start) | `threshold_celsius` (`battery_health.hot_thresholds_c`, 35 and 40) |
| `battery_hot_uncounted_seconds_total` | counter (seconds a stalled clock advance did not credit) | — |
| `battery_charge_session_active` | gauge 0/1 | — |
| `battery_charge_session_peak_temperature_celsius`, `battery_charge_session_duration_seconds` | gauge (the running session, else the last; absent before the first) | — |
| `battery_charge_sessions_total` | counter (completed sessions since start) | — |
| `battery_cell_voltage_max_volts`, `battery_cell_voltage_min_volts`, `battery_cell_voltage_spread_millivolts` | gauge (absent while unknown; the spread needs both) | — |
| `battery_cell_delta_millivolts`, `battery_cell_delta_avg_4w_millivolts` | gauge (balance monitor; absent while `unknown`) | — |
| `battery_balance_status` | gauge 0/1 (one-hot; absent until the entity was seen, `unknown` while HA says so) | `status` (`green`, `yellow`, `orange`, `red`, `unknown`) |
| `battery_balancing_mode` | gauge 0/1 | — |
| `battery_last_balance_read_timestamp_seconds`, `battery_last_balance_read_age_seconds` | gauge (absent until a valid read) | — |
| `actuator_mode` | gauge 0/1 (`dry_run`/`live` only, like every `actuator_*`) | `mode` |
| `actuator_kill_switch_on`, `actuator_in_control`, `actuator_guard_ok`, `actuator_alert` | gauge 0/1 | — |
| `actuator_guard_reason` | gauge 0/1 | `reason` (the guard reasons of [Live control](#live-control-dry_run-and-live)) |
| `actuator_desired_pd_target_watts`, `actuator_desired_allow_charge`, `actuator_desired_allow_discharge`, `actuator_desired_max_discharge_watts` | gauge (requested) | — |
| `actuator_observed_pd_target_watts`, `actuator_observed_battery_power_watts` (+ charge), `actuator_observed_grid_power_watts`, `actuator_house_load_watts` | gauge (observed; absent when stale) | — |
| `actuator_clamped_watts` | gauge (W of planned battery power the last tick could not request; 0 = none cut) | — |
| `actuator_load_following_engaged`, `actuator_load_following_raise_watts` | gauge (only with `actuation.load_following.enabled`: 1 while [load-following discharge](#load-following-discharge) is engaged, and how far it raised the discharge cap above the planned cap; 0 otherwise) | — |
| `actuator_reserve_engaged` | gauge (1 while [reserve protection](#reserve-protection) holds: the measured SOC is at or below the tariff's SOC floor) | — |
| `actuator_writes_total` | counter | `role` (`allow_charge`, `allow_discharge`, `max_discharge_power`, `power_limits`, `pd_target`, `heartbeat`, `full_charge_day`, `full_charge_enable`), `result` (`ok`, `failed`, `dry_run`) |
| `full_charge_mode` | gauge 0/1 (one-hot; only with `full_charge.enabled`, like every `full_charge_*`) | `mode` (`shadow`, `live`) |
| `full_charge_state` | gauge 0/1 (one-hot) | `state` (`idle`, `scheduled`, `armed`, `charging`, `reached`, `settling`) |
| `full_charge_days_since_last`, `full_charge_last_timestamp_seconds`, `full_charge_scheduled_day_timestamp_seconds` | gauge (absent while unknown / none scheduled) | — |
| `full_charge_forcing_cost_eur`, `full_charge_threshold_eur`, `full_charge_soc_peak_percent` | gauge (last priced decision; current or last run) | — |
| `full_charge_cadence_class` | gauge 0/1 (one-hot) | `class` (`unknown`, `green`, `yellow`, `alert`) |
| `full_charge_cadence_min_days`, `full_charge_cadence_max_days` | gauge (the active row) | — |
| `full_charge_alert` | gauge 0/1 | `reason` (`delay_switch_on`, `timezone_mismatch`, `entities_missing`, `never_reached`, `unacknowledged`, `reached_unmeasured`, `balance_alert`, `paused`, `state_unwritable`) |
| `full_charge_runs_total` | counter | `outcome` (`done`, `done_simulated`, `failed_never_armed`, `failed_unacknowledged`, `failed_never_reached`, `failed_reached_unmeasured`, `cancelled`) |
| `actuator_write_failures_total` | counter (each attempt) | `reason` (`ha_error`, `readback_timeout`, `forbidden`) |
| `actuator_safe_idles_total` | counter | `reason` (guard reasons, `write_failed`, `shutdown`) |
| `actuator_heartbeats_total` | counter | `result` (`ok`, `failed`, `dry_run`) |
| `actuator_last_successful_write_timestamp_seconds` | gauge (0 until the first confirmed write) | — |

Day-ahead and final KPIs (per-day views: `increase(...[1d])`; the signed load error is the difference of
two counters, so every series stays monotonic and survives restarts):

* load forecast MAE (kWh per slot): `increase(battery_optimizer_load_forecast_abs_error_kwh_total{plan="day_ahead"}[1d]) / increase(battery_optimizer_load_forecast_scored_slots_total{plan="day_ahead"}[1d])`;
* load forecast bias, actual minus forecast (kWh per day, positive = under-forecast): `increase(battery_optimizer_load_actual_kwh_total{plan="day_ahead"}[1d]) - increase(battery_optimizer_load_forecast_kwh_total{plan="day_ahead"}[1d])`;
* daily energy error in percent (forecast against actual): `100 * (increase(battery_optimizer_load_forecast_kwh_total{plan="day_ahead"}[1d]) / increase(battery_optimizer_load_actual_kwh_total{plan="day_ahead"}[1d]) - 1)`;
* plan revision (kWh per day): `increase(battery_optimizer_plan_revision_kwh_total[1d])`;
* battery energy by series: `increase(battery_optimizer_shadow_day_ahead_energy_kwh_total{direction="charge"}[1d])` (day-ahead),
  `...shadow_planned_energy_kwh_total{direction="charge"}` (final), `...shadow_actual_battery_energy_kwh_total{direction="charge"}` (actual).
  The final and actual counters also cover slots without a day-ahead plan (after a first deploy), so compare the three
  only over a period in which every slot had one, or use the [offline tool](#offline-day-ahead-kpis), which keeps to
  the slots that have all three.

The `battery_*` rows exist only when a `battery_health.entities` role is configured. Suggested alerts
(homelab): `ready == 0` for 30 m; `ha_connected == 0` for 10 m;
`input_stale{role="grid_power"} == 1` for 15 m; `plan_age_seconds > 1800`;
`increase(solves_total{result=~"error|timeout"}[1h]) > 0`; `increase(sink_write_errors_total[1h]) > 0`;
`history_age_seconds > 93600`; archives:
`time() - archive_last_success_timestamp_seconds{archive="weather_forecast"} > 10800` or
`increase(weather_forecast_fetches_total{result="failed"}[3h]) > 2`. A PVPC snapshot only exists when the sensor
changes, so alert on `increase(pvpc_snapshots_total{result="failed"}[1h]) > 0` rather than on staleness. Climate
shadow (when enabled): `climate_shadow_up == 0` for 2 h, `time() - climate_shadow_last_run_timestamp_seconds
> 7200`, or `increase(climate_shadow_runs_total{result="error"}[6h]) > 2`. Actuator (`live`): `actuator_alert == 1`
for 2 m; `actuator_in_control == 1 and actuator_guard_ok == 0` for 30 m;
`increase(actuator_heartbeats_total{result="failed"}[10m]) > 0`;
`actuator_guard_reason{reason="import_over_cap"} == 1`; charge under-delivery (`dry_run` and `live`):
`min_over_time(actuator_clamped_watts[30m]) > 100` (the plan is cut for a whole half hour, typically the
system max charge while the power limits are on).

## Logs

The log (stderr, `LEVEL logger: message`) carries one `key=value` line per event, on change only, never
per actuator tick; `-v`/`--verbose` adds DEBUG. Tokens never appear in it. At INFO:

- startup: `serving mode=live soc_source=entity version=... code_version=<sha> on 0.0.0.0:8080 (store ...)`;
- planner: `plan generated plan_id=... trigger=scheduled status=optimal solve_s=0.41 slots=52 first_slot=...
  first_mode=charge first_power_w=1500 soc=0.432 soc_measured=0.432 soc_clamped=False soc_source=entity`;
  `solve skipped trigger=... reason=... detail=...` the first time a reason occurs (repeats are DEBUG);
  timeouts and discarded plans at WARNING, solver errors at ERROR; `prices published`;
- slots: `slot opened start=... decision=intent mode=charge power_w=1500 plan_id=... not_ready=-` (or
  `decision=not_ready ... not_ready=ha_disconnected,...`), `slot adopted start=... mode=charge power_w=1500
  plan_id=... after_s=60 decision_at_open=not_ready` (a [late adoption](#scheduling-and-the-slot-cycle)) and
  `slot closed start=... decision=... grid_kwh=... battery_charge_kwh=... battery_discharge_kwh=... soc_start=...
  soc_end=...`;
- actuator (`dry_run`/`live`): `actuator kill_switch=on previous=off`, `actuator in_control=True`,
  `actuator guard reasons=no_plan` (when the set changes; `ok` when empty), `actuator desired mode=charge
  reason=intent pd_target_w=1900 max_discharge_w=- clamped_w=0` (when mode or reason changes), every write
  `actuator write role=pd_target service=number.set_value value=1900 result=ok` (`actuator dry_run write ...`
  in `dry_run`; the heartbeat is not logged). WARNING: write failures and readback timeouts,
  `actuator import trip ...` and `actuator import trip ended`, `actuator alert=write_failed`; ERROR:
  `actuator alert=safe_idle_failed`, a failed or timed-out safe idle at shutdown;
- periodic full charge (when enabled): `full-charge decision day=... d=... class=... forced=... accepted=...
  forcing_cost=... vs_normal=... threshold=... shortfall=... checkpoint=...`, `full-charge state idle -> scheduled
  reason=threshold day=...` (every transition with its reason), `full-charge outcome=done reason=balance_reading
  day=... soc_peak=...` (WARNING for a failed outcome), `full-charge paused until ...`, `full-charge suspended
  reason=...` / `resumed`, `full-charge time zone verified`, ERROR `full-charge disabled: Home Assistant time_zone=...`;
- Home Assistant: `home assistant connected`, `home assistant synced states=... tracked_seen=... resyncs=...`;
  WARNING `home assistant disconnected: ...; reconnect in ...` and `connect failed` (once per distinct
  error while retrying); ERROR `home assistant auth failed`;
- history: `history refreshed cutoff_day=... usable_slots=...`; WARNING `history refresh failed ...`;
- archives and climate shadow: `weather forecast archived ...` (WARNING when the fetch fails) and
  `climate shadow run t0=... status=... regime=...` per run.

## Store

Under `<data_dir>/service/v1/`, partitioned by UTC day (a DST day is never split or doubled),
files `0600`, directories `0700`:

| Path | Content |
|---|---|
| `plans/date=YYYY-MM-DD/<plan_id>.json` | One immutable file per plan (atomic, no-clobber): snapshot summary and inputs, solver result, intents |
| `slots/date=YYYY-MM-DD/slots.jsonl` | One row per closed slot: decision, measurements, price, virtual-battery step, costs (see below) |
| `events/date=YYYY-MM-DD/events.jsonl` | Start/stop, HA sync/disconnect, solve skipped/error/timeout/discarded, history refresh, price publication |
| `state/virtual_battery.json` | Virtual SOC (restored after a restart if younger than 6 h) |
| `state/thermal.json`, `state/thermal_samples.json` | Thermal shadow only (mutable, atomic): the model parameters, status and last refit; the one-minute temperature and power buffer ([thermal shadow](#thermal-shadow-derate-prediction)) |
| `state/full_charge.json` | Periodic full charge only (mutable, atomic): the scheduler's knowledge and calendar decision, never a plan ([scheduler](#periodic-full-charge-the-scheduler)) |

A slot row also carries `day_ahead` and `final` (the [per-slot values](#day-ahead-and-final-plan): plan ID,
generation time, `lead_seconds`, mode, `signed_power_w`, `soc_target_frac`, `load_forecast_kwh`,
`load_forecast_w`, expected price, and the `charge_kwh` / `discharge_kwh` the plan asks for over the whole slot,
not pro-rated for a late adoption; `null` when the slot has none) and `plan_revision_kwh` (`|day-ahead - final|`
requested net battery energy, `null` unless both exist), next to the measured values; every older key is
unchanged.

A slot row's `decision` is the outcome that held when the slot closed: `intent`, `late_intent`, `no_plan`,
`plan_expired`, `not_ready` or `not_observed`. `decision_at_open` is the decision frozen at `B` (equal to
`decision` except for `late_intent`; `null` when the slot was not observed); `inputs_ready_at_open` and
`not_ready_reasons` also describe `B`. For a `late_intent` slot, `adopted_at` is when the intent was adopted,
`adopted_after_s` the seconds from the slot start to then (both `null` otherwise), and `intent` is the adopted
intent as the plan holds it (its `plan_id` names the plan). Everything derived from the intent covers only the
time it was in force, `1 - adopted_after_s / interval_seconds` of the slot: `planned_charge_kwh` and
`planned_discharge_kwh`, the SOC-limit check, and the virtual-battery step that gives the counterfactual
(the intent's power scaled by that share, the energy-equivalent of no intent before `adopted_at`).
| `pvpc_snapshots/date=YYYY-MM-DD/pvpc_snapshots.jsonl` | One row per PVPC sensor state change (see [Passive archives](#passive-archives)) |
| `weather_forecasts/date=YYYY-MM-DD/weather_forecasts.jsonl` | One row per fetched HA weather forecast (see [Passive archives](#passive-archives)) |
| `climate_{decisions,scores,days,models}/date=YYYY-MM-DD/*.jsonl` | The [climate shadow](#climate-shadow-track-b2)'s records (only when it is enabled) |
| `full_charge/date=YYYY-MM-DD/full_charge.jsonl` | Periodic full charge only: one row per decision, reading, state transition, outcome and pause |
| `actuation/date=YYYY-MM-DD/actuation.jsonl` | `dry_run`/`live` only: one row per actuator tick that wrote (or would write) or changed state: kill switch, guard reasons, intent, desired and observed state, each write with its result, heartbeat, alert ([Live control](#live-control-dry_run-and-live)) |

JSONL rows are appended with a single `O_APPEND` write plus `fsync`; readers skip a torn last line.
The store is private household data: it never leaves the PVC and is never committed.

## Passive archives

Two append-only private archives collect data for later offline work (Price Outlook PO-B and Load
Forecaster P4). They are written through the same sink as slots and events (one `O_APPEND` write of one
complete JSON line, `fsync`, files `0600`, directories `0700`, UTC-day partitions, `schema_version: 1`),
are **never read by the planner**, and can never change readiness: a failed collection or write is logged,
counted and dropped. **There is no retention yet:** the files grow until pruned by hand (roughly a few MB
per day: one PVPC snapshot of a few kB per sensor change and one forecast of tens of kB per hour).

```yaml
archive:                      # section absent => both archives off (the code default)
  pvpc_snapshots:
    enabled: true             # committed config/service.yaml: true
  weather_forecast:
    enabled: true             # committed config/service.yaml: true
    entity_id: weather.forecast_home   # a weather.* entity
    type: hourly              # hourly | daily | twice_daily
    interval_minutes: 60      # must divide 1440
    offset_minutes: 5         # fetch at HH:05 UTC; 0 <= offset < interval
```

Unknown keys under `archive` are refused at startup. The archive settings are not part of the planning
`config_hash`.

**PVPC publication snapshots** (`pvpc_snapshots/.../pvpc_snapshots.jsonl`). One row for every accepted state
change of the `price` entity (`sensor.esios_pvpc`), and one for the first state the process sees after a
start (`trigger: "initial"`, later ones `"change"`). The 60 s REST refresh of an unchanged state is not a
change and writes nothing. Rows are written even when the state is `unavailable` or cannot be parsed into a
curve.

| Field | Content |
|---|---|
| `kind`, `schema_version`, `code_version`, `entity_id`, `trigger` | `pvpc_snapshot`, `1`, build identity, the price entity, `initial` / `change` |
| `observed_at` | Service clock when the change was applied (UTC ISO) |
| `last_changed`, `last_updated`, `last_reported` | Home Assistant's own timestamps (UTC ISO) |
| `state` | The state string |
| `attributes` | The raw attribute dict, verbatim (`price_HHh`, `price_next_day_HHh`, ...) |
| `next_day_present` | `true` when any `price_next_day_*` attribute exists |
| `reference_day` | Local date (`timezone`) of `last_updated`: the day the `price_HHh` keys refer to |

`observed_at - last_updated` is the delivery delay; the first row with `next_day_present: true` per
`reference_day` is the observed publication time.

**Weather-forecast archive** (`weather_forecasts/.../weather_forecasts.jsonl`). Every `interval_minutes`
(on a UTC grid at `offset_minutes` past; not at startup) the service calls `weather.get_forecasts` for
`entity_id` with `return_response` over the existing WebSocket session and appends one row. A fetch needs a
synced HA session; without one it fails and is retried.

| Field | Content |
|---|---|
| `kind`, `schema_version`, `code_version` | `weather_forecast`, `1`, build identity |
| `requested_at`, `received_at` | Service clock when the call was sent and answered (UTC ISO); the row is partitioned by `requested_at` |
| `entity_id`, `type` | As configured |
| `forecast` | HA's raw list for the entity (`response[entity_id].forecast`), entries untouched (`datetime`, `condition`, `temperature`, ... as the integration provides them) |

An empty or missing forecast list is a failure and is not written.

**Permission.** The only service call the service can make is `weather.get_forecasts`, and only in the form
the guard accepts (see [Safety](#safety)). The Home Assistant user behind the token must be allowed to call
that service (a normal non-admin user is); the token stays read-only in every other respect.

## Climate shadow (track B2)

With `climate_shadow.enabled` the service also runs B0's thermal model and the price-blind comfort MPC
([climate-mpc](components/climate-mpc.md)) on live data, in **shadow**: it predicts, plans, records and
scores, and sends nothing anywhere. It is the data source of the preregistered section-A verdicts
([preregistration](planning/climate-preregistration-2026-27.md)) and of the descriptive MPC-versus-rule
comparison after the winter (B3). Code: `climate/shadow.py` (live data, decision, scoring),
`climate/shadow_gates.py` (the gates), `service/climate_shadow.py` (the job). Each decision is also shaped into an
advisory plan that `GET /climate/plan` can serve, off by default ([Climate plan export](#climate-plan-export)).

**Data flow.** Every `replan_every_minutes` of `config/climate-mpc.yaml` (hourly), `offset_minutes`
after the boundary (default 10, after the weather fetch at :05), on its own worker thread:

1. **Read.** Raw Prometheus samples of the climate signals the job needs (rooms and setpoints, unit
   outdoor sensor, water target, compressor frequency, heat-pump power by mode, force-DHW, the legacy
   rule's enable and hours, PVPC, and the controller's per-room `input_number.comfort_floor_<room>` /
   `comfort_day_minimum_<room>` helpers, each absent-tolerant) for the trailing `model.reestimate.window_days + 2` days, cached in
   memory and fetched incrementally after the first run. They go through the feature builder's own slot
   rules and the climate loader, cut at the decision `t0`, so a live slot is computed like an offline
   feature-set slot (debiased outdoor sensor, DHW and cooling masks, comfort-room mean, replayed legacy
   permission). From Home Assistant, through the entity store only: `entities.regime`,
   `entities.dhw_plan` and `entities.away`, each optional. The forecast is the newest record of the
   [weather archive](#passive-archives) requested at or before `t0` and within
   `forecast_max_age_minutes` (so the fetch at :05 serves the next hour's decision).
2. **Refit** (`every_days`, 7): `ua` and `q0` on the `window_days` (28) complete heating-month days
   before today, the rest at the prior, exactly as the B1 replay; logged with its model hash.
3. **Decide** at `t0`: the observer state and the MPC plan over 24 h. The observer runs over two
   warm-up days and its state is recorded as the prediction origin; it reads `t_out_model`, the same
   outdoor series as the prior fit and the gate, with the last valid reading held where it is unknown.
   Horizon inputs:
   - the forecast outdoor temperature, else the last reading held;
   - DHW for today's slots from the comfort controller's plan (see the
     [contract](planning/comfort-controller-design.md#b2-interface-to-the-controller-read-only)), and
     yesterday's measured DHW slots for the rest of the horizon or when the plan says nothing about
     today;
   - the design setpoints with optimum start (per room: its floor, and its day minimum where that is above
     the floor; helper values in effect at `t0` over the config's, held over the horizon, recorded in
     `inputs.comfort`), or every room at its floor plus 0.5 K (21.5 degC by default) while `away` is on;
   - the run in progress from the measured slots.
4. **Score** each origin once the local day of its last horizon slot is complete (so the replayed permission
   of that day is known): open-loop room predictions at 1, 6, 12 and 24 h, and, for midnight origins, the
   day's heat delivery. Each completed day gets its validity record.
5. **Gates**: re-evaluated from the score and day records for `/metrics` and `/status`.

**Enable it.** Fit the prior offline, on the workstation that holds the climate feature set:

```bash
uv run python hack/climate_shadow_prior.py --feature-set <id> --dataset <dataset dir> --season S3
```

It prints the prior id and writes `<data_dir>/analysis/v1/climate-shadow/priors/<id>/prior.json`
(model parameters only, content-addressed). Copy that directory and the private `climate/site.yaml`
into the service's data directory, set `climate_shadow.enabled: true` and `climate_shadow.prior: <id>`,
and mount `climate.yaml` and `climate-mpc.yaml` next to `service.yaml` (the two paths are relative to the
service config's directory). The weather archive should be on (`archive.weather_forecast`, `hourly`);
without it the horizon holds the last reading. Set `climate_shadow.go_live` on the comfort controller's
go-live day: without it there is no new-window verdict. Nothing else is needed: no new credential, the
same read-only token and Prometheus URL. On start the job catches up the current hour once its offset has
passed, after the first Home Assistant sync (at most 60 s, then it runs anyway and records regime `unknown`). A missing prior, site file or config is reported (`climate_shadow_up 0`, which is also 0 until
the job has loaded; `/status`; a `climate_shadow_error` event) and retried every hour; it never affects
readiness or the battery planner.

**Where the data lands.** `<data_dir>/service/v1/`, append-only, UTC-day partitions:

| Kind | One row per | Content |
|---|---|---|
| `climate_decisions` | run | `t0`, local day and hour, `midnight`, `regime` and the raw helper state, `prior_id`, `model_hash` and parameters, MPC config hash; `inputs` (weather source, forecast time and its offset from the last reading, horizon outdoor temperature, DHW source and slots, away, run in progress, setpoints per room and slot); `origin` (`x0`, observer age); `plan` (status, every solve's status, time and gap, re-linearisation iterates, per slot on/off, water offset and target, open valve share, predicted room temperatures and electricity, the comfort deficit); `status` (`planned`, `plan_failed`, `no_state`, `no_inputs`, `error`) |
| `climate_scores` | scored origin | `room_mean` per horizon (the gate): `heat`, `heat_plan_weather`, `persistence` errors in K of the comfort-room mean `t_air`; `design_rooms` (diagnostic: the four thermostat rooms with their prior-season offsets); `accuracy` (the B1 replay's open-loop block: `air`, `hall`, `air_q`, persistence, and `day` for midnight origins); the action used, regime, `prior_id`, `mpc_config_sha256` and a plan summary (status, solve time, heating and top-offset slots, midnight planned electricity) |
| `climate_days` | completed local day with a decision | coverage of the gate rooms, heat-pump energy and outdoor series, space-heating slots, `valid_heating_day`, the regimes recorded that day |
| `climate_models` | weekly refit | valid-from day, prior id, parameters, whether the window refitted, window days, model hash, config hashes |

Every row carries `schema_version`, `code_version` and `written_at`. Energy figures are internal values of
the private store; no metric exposes one.

**Definitions** (the B1 replay's, with the
[preregistration amendment of 2026-10-31](planning/climate-preregistration-2026-27.md#4-amendments)):

- **Regime** per record: `comfort` when `input_boolean.comfort_control_enable` is on, `comfort_off` when it
  is off, `legacy` when it does not exist, `unknown` when HA is not synced. The preregistered windows:
  legacy = records in `legacy` before go-live; new = records in `comfort` from go-live plus 7 days through
  2027-03-31. Go-live is `climate_shadow.go_live` only; **without it there is no new-window verdict**, and
  the first `comfort` record is reported as a provisional date that never fixes a window.
- **Room-mean**: B0's three `comfort.rooms` (bedroom, hall_kitchen, office_t), the model's `t_air` with
  zero offset; persistence is the same mean held. Office A is excluded (server heat sink). The job refuses
  a `climate.yaml` with other comfort rooms. The four-room mean of design section 3 is a diagnostic.
- **Gate prediction**: from the persisted origin, driven by the **measured heat and the measured outdoor
  temperature** (B0's 7.3 basis, B1's `air_q`). It tests the thermal model, not the operational weather
  forecast. `heat_plan_weather` (the outdoor series the plan used) is a diagnostic.
- **Daily heat**: midnight origins, the heat law under the actions against the measured heat, median APE
  on days above 20% of the window's median daily heat. The action is the replayed legacy permission in the
  legacy regime and the **measured space-heating run** in the comfort regime, which is a weaker test
  (it conditions on heat being delivered).
- **Rows**: origins on valid heating days of the window; RMSE, bias and skill on the origins where both
  the model and persistence have an error.
- **Lineage**: one prior and one MPC config. The job resumes, scores and evaluates only the records of
  its own `prior_id` and `mpc_config_sha256`; the report counts the others.

**Read the gate report** (from the store alone, no network):

```bash
battery-optimizer climate-shadow-report [--data-dir <store>] [--service-config <yaml>] \
    [--prior <id>] [--mpc-config-sha256 <hash>] [--all-lineages] [--format md|json]
```

The lineage defaults to the configured prior and the hash of the configured `climate-mpc.yaml`.

Per window: valid heating days and weeks (6 weeks needed, else every criterion is UNDETERMINED), each
preregistered criterion with its value, threshold and PASS / FAIL / UNDETERMINED, RMSE, persistence RMSE,
skill and bias at 1, 6, 12 and 24 h, the daily heat MdAPE, solver statuses and times, the top-offset share
and the planned-over-rule electricity ratio (descriptive only). The report holds private figures: keep it
with the data. Locally: `just pull-store`, then point `--data-dir` at the copy.

**Recalibration triggers.** The weekly refit follows the season; the prior stays fixed through both
preregistered windows, because each verdict is about the model as it ran. A new prior (a new
`hack/climate_shadow_prior.py` run, set as `climate_shadow.prior`, noted in the
[regime log](components/regime-log.md)) is fitted for the next season, or earlier only when a trigger of
[climate-mpc](components/climate-mpc.md#invalidated-when) or
[thermal-model](components/thermal-model.md#invalidated-when) fires that changes the model's inputs
themselves: the outdoor source or the heat measurement, the comfort rooms or their thermostats. A change of
the schedule, the water curve or the machine limits is an edit of the MPC config (a new config hash in every
decision), not a refit; both start a new lineage, which the gates evaluate apart. Changing the room set,
a threshold or a window is a preregistration amendment, allowed only before go-live.

### Climate plan export

The climate shadow's hourly decision is also published as an **advisory ClimatePlan**, for the homelab comfort
controller's shadow comparison and for dashboards. Contract, fields and consumer rules:
[climate-plan-contract](climate-plan-contract.md); decision: [ADR-0008](adr/0008-climate-plan-handoff-to-the-comfort-controller.md)
(Proposed). **The export is a read-only `GET` on this service: it adds no Home Assistant service call or write,
`service/ha_guard.py` is unchanged, and turning it on does not let anything act on the heat pump.** The only
thing that can ever act on it is the homelab consumer, behind its own hard lock
(`optimizer_live_allowed: false` in git); in this code version the optimizer's effective mode is never `live`.

**Toggle: off by default.** The owner's control is the Home Assistant select
`input_select.climate_optimizer_mode` (options exactly `off`, `shadow`, `live`; `climate_shadow.entities.mode`),
which the service reads through its existing read-only state subscription, like the battery kill switch. The
`climate_shadow.optimizer_mode` key of `config/service.yaml` (`"off"`, committed default; quote it, a bare YAML
`off` is a boolean) is the **fallback** while the helper is missing, unavailable or holds anything else.
`climate_shadow.plan_ttl_minutes` (90) is the plan's validity after its decision time.

| Mode (requested) | Effective | `/climate/plan` |
|---|---|---|
| `off` | `off` | `404`, `state: mode_off` |
| `shadow` | `shadow` | the plan, advisory |
| `live` | `shadow`, `live_locked: true` | the same plan, `requested_mode: live` and the lock reason |

The mode only gates what is served: the shadow's hourly run, records and scoring continue in `off` (they are the
measurement). The plan is rebuilt from the newest stored decision when the service starts, so a restart inside
its validity keeps serving it; a plan past `valid_until` is `404` (`expired`), and consumers treat it as absent.
Nothing is stored beyond the decision records that already exist. Operations: scrape
`battery_optimizer_climate_plan_*` (see [Metrics](#metrics)); alert on `climate_plan_valid == 0` for 2 h while
the effective mode is `shadow` if a consumer depends on it.

## Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| HA unreachable / socket drops | `ha_connected=0`, `ha_failures_total{stage}` | Reconnect with capped backoff (1 s → 60 s, ±20 % jitter); full `get_states` resync on every connect; decisions `not_ready` meanwhile |
| Token rejected | `ha_failures_total{stage="auth"}` | Retried only at the maximum backoff |
| Subscribe or `get_states` refused or not acknowledged | `stage="subscribe"` / `"get_states"` | Session ends, reconnect |
| Missed pong | `stage="heartbeat"` | Session ends, reconnect |
| Stale grid power or SOC | `input_stale`, `not_ready_reason` | No solve (`skipped_solves_total{reason}`), no decision; a slot that opened not ready adopts an intent once the inputs recover (`late_intent`) |
| Prices missing, or next day not yet published late in the day | `prices_missing` / `prices_insufficient` | No solve until `min_horizon_slots` are published |
| Prometheus down at the daily refresh | `history_refresh_total{result="error"}` | Retried every 10 min; yesterday's history is not reused for today |
| Solver error or timeout | `solves_total{result}` | Reported; a timed-out thread keeps the worker (`busy`) |
| Store write fails | `sink_write_errors_total` | Logged; service continues |
| Archive write fails, or a forecast fetch fails (no session, call refused, timeout, empty or malformed response) | `pvpc_snapshots_total{result="failed"}`, `weather_forecast_fetches_total{result="failed"}` | Logged and counted; the record is dropped; a failed fetch is retried after 30 s, 60 s, ... capped at 10 min (never past the next grid instant); readiness, solves and slot closes are untouched |
| Climate shadow cannot load (prior, site file or config missing) or a run fails | `climate_shadow_up`, `climate_shadow_runs_total{result="error"}`, `/status` | Recorded (an `error` decision row or a `climate_shadow_error` event), retried at the next hour; readiness and the battery planner are untouched |
| Restart | `build_info`, events | No plan is resumed; decisions start after the first fresh solve, a catch-up plan adopted by the slot in progress (`late_intent`) |

## Shadow-vs-native comparison

```bash
battery-optimizer shadow-report --since 2026-11-01 --until 2026-11-15            # Markdown
battery-optimizer shadow-report --since 2026-11-01 --until 2026-11-15 --format json
```

`--since`/`--until` accept local dates (that day's local midnight) or ISO instants with an offset;
the default is the 14 complete local days before today. The report is recomputed from the store
alone, per local day and overall:

* **Coverage:** slots with a valid intent at their start divided by the real quarter-hours of the
  window (92/96/100 per local day), with decision outcomes, skipped-solve reasons, solve failures,
  plans and HA disconnects. A `late_intent` slot is not covered (it is listed under the outcomes).
* **Planned vs actual:** requested charge/discharge energy against the energy the native controller
  moved (battery counters, else integrated battery power, labelled), and the mean absolute and mean
  signed difference of slot-average power.
* **Cost:** native = `price × measured grid import`. Counterfactual = the frozen decisions replayed
  (a `late_intent` for the share of the slot it was in force, as in the slot row)
  open-loop on a virtual battery starting from the first measured SOC, against the measured gross
  load `grid − battery charge + battery discharge`, clipped by power, SOC window, import ceiling and
  no-export — `price × (load + charge − discharge)`. Same slots, prices and loads for both.
  `savings_eur` is native minus counterfactual; `savings_inventory_adjusted_eur` also values the
  difference in stored energy at the end of the window at the plans' terminal value.
* **SOC-limit violations:** decisions that, applied to the measured SOC at slot start, would have
  left the configured SOC window; plus the energy the replay had to clip.
* **Assumptions** are listed in every report: pretax energy only (taxes unconfirmed), open-loop
  replay (plans saw the measured SOC, the replay carries its own), provisional battery parameters,
  no idle self-discharge, and the slots excluded for missing load or price.

`comparison_basis` is `native_battery` only when battery telemetry is mapped and `soc_source` is
`entity`. Before commissioning it is `no_battery_pre_commissioning`: the "native" side is the house
with no battery and the counterfactual is the virtual battery, which estimates the battery's value
but does **not** satisfy exit gate C. `exit_gate.met` requires at least 14 local days with ≥ 90 %
coverage on the `native_battery` basis.

### Offline day-ahead KPIs

```bash
uv run python hack/day_ahead_kpis.py --from 2026-11-04 --to 2026-11-11 [--run-id week45] [--json]
```

Reads the plan files and the slot rows of a window of UTC days (`BATTERY_OPTIMIZER_DATA_DIR` or
`--data-dir`) and prints the KPI summary: the load forecast MAE, bias (actual minus forecast) and daily energy
error for the day-ahead and the final plan, the battery energy each plan asked for against what the battery
did, the plan revision, the average price of the energy charged against the energy discharged, the saving
against a house without the battery (`sum(price x (discharge - charge))`, pretax, before valuing the energy
left in the battery), and the SOC target error. Battery figures use the slots that have a day-ahead plan, a
final intent and a measured battery energy; load figures every slot where that plan has a forecast and the load
was measured. The final plan is read from the slot row's `intent`, so rows from before the day-ahead fields
existed work; the day-ahead plan is recomputed from the plan files (up to three UTC days before the window).
With `--run-id` it also writes `report.json` and a per-slot `slots.csv` under
`<data_dir>/analysis/v1/day-ahead-kpis/<run-id>/`, never in the repository. The logic is
`service/plan_kpis.py`.

## Live control (dry_run and live)

Decision and rationale: [ADR-0007](adr/0007-live-battery-control.md) (Accepted 2026-11-03). Module:
`service/actuator.py`.

Two limits stay separate: the battery's own maximum charge/discharge power (2500 W, Omnibattery's per-battery
`number.venus_max_charge_power` / `number.venus_max_discharge_power`, minimum 800 W; never written by the
service) and the house limit, grid import while charging at or below `import_cap_w` (4500 W; the 5 kW contract
trips instantly).

Every `actuation.tick_interval_s` (30 s), at every slot open, after every new plan and after every HA resync,
the actuator takes the intent frozen for the current slot and maps it to Omnibattery's entities:

| Intent | Direction switches | PD grid target (W, + import) | System power limits |
|---|---|---|---|
| `idle` | charge off, discharge off | `neutral_pd_target_w` (0) | untouched |
| `charge` at P W | discharge off, charge on | `min(house load + P, import_cap_w, entity max)` (never negative) | untouched |
| `discharge` at P W | charge off, discharge on | `discharge_margin_w` (+100 deployed since 2026-11-05) | on, discharge cap `max(P, min_discharge_cap_w)` (never 0 = unlimited; raised by [load-following](#load-following-discharge) when enabled) |
| safe idle | charge off, discharge off | untouched | untouched |

House load = grid power − battery AC power, charge positive (`battery_ac_power_sign` turns the sensor's sign
into that convention). Writes go restrictive first (switches off, discharge cap, power limits on, PD target,
switch on), one at a time, each confirmed by readback within `readback_timeout_s` and retried up to
`command_policy.max_attempts`; a write that still fails drops to safe idle and sets `actuator_alert`. Only the
charge-mode PD target's load tracking is rate limited (a change of `min_setpoint_change_w`, not within
`min_command_interval_s` of the last write); switch edges, mode changes, a reconcile that switches a direction
on, the discharge margin and the discharge cap are always written. Every write except a `turn_off` re-checks the
kill switch first; if it went off mid-reconcile the actuator aborts to safe idle and hands control back.

Omnibattery bounds the PD target by its entity `min`/`max`, which the actuator reads live: the batteries'
summed limits (±2500 W) with the system power limits off, `[-system max discharge, +system max charge]` with
them on (-800..+1000 W on 2026-11-03). The written charge target is `min(house load + P, import_cap_w, entity
max)`; the desired state records the value actually written, `clamped` when it was cut and `clamped_w`, the
planned battery power the cut removed (`min(requested − bound, P)`, also for the discharge cap), exported as
`actuator_clamped_watts`. The charge is
therefore at most 2500 W − house load (about 2 kW at night) with the limits off, and only `system max charge −
house load` (zero once the load reaches it) with them on. The actuator turns the limits on for a discharge and
never off, so the second case is the normal one: before go-live (test socket, 1000 W system max charge) the
charge is cut hard; the go-live configuration sets both system caps to 2500 W and Marstek Premium mode on
(discharge above 800 W), so the bound is then ±2500 W, as the planner's `power_kw: 2.5` assumes: see
[known issues](known-issues.md#the-pd-target-is-bounded-by-the-system-caps-while-the-power-limits-are-on).

`discharge_margin_w` is 100 W in the deployed config since 2026-11-05 (200 W before; code default 50 W): Omnibattery's
PD loop swings by ±150 to 200 W on a ~2 s period, and 50 W gave brief exports in the commissioning test. 2-second HA grid samples over about 17 h of live discharge on 2026-11-04/05 at the 200 W target had a median of 192 to 195 W, a 1st percentile of 131 to 151 W and were below 0 W in 0 to 0.04% of samples; replaying 100 W on the same data gives about 0.2% of time below zero and about 1 Wh/day of export ([known issues](known-issues.md#the-pd-loop-oscillates-the-discharge-margin-was-too-small)); export is alerted from the Shelly's returned-energy counter (homelab `BatteryGridExport`, `BatteryGridExportSustained`). A discharge is therefore at
most the house load minus this margin; `planning.discharge_margin_bound: true` (default `false`) makes the planner
bound each slot's discharge the same way (`max(0, forecast load - discharge_margin_w)`), so the plan does not promise
energy the actuator cannot deliver. It reads this one margin, joins `inputs_hash` and `config_hash` only when on,
and applies to the day-ahead and the final plan alike (one solve); see
[known issues](known-issues.md#planned-discharge-is-not-delivered-planner-bound-enabled-since-0140-monitoring).

**Guard** (any failure → safe idle, recorded with the reason): `import_over_cap` (measured import above
`import_cap_w + import_guard_margin_w` while charge is on: charge off at once, and charge refused for
`import_trip_hold_s`), `ha_not_synced`, `kill_switch_off`, `no_plan`, `plan_expired`, `inputs_not_ready`,
`intent_expired`, `plan_not_from_this_process`, `grid_power_stale`, `battery_soc_stale`,
`battery_power_stale`, `battery_unresponsive` (`entities.battery_liveness` not exactly `None`),
`price_stale`, `integration_unhealthy` (status not in `healthy_states`). Battery SOC and AC power are
checked for availability only by default (present, not `unavailable`/`unknown`, numeric; SOC within 0..100):
Omnibattery reports them only on change and HA does not refresh `last_reported` for an unchanged value, so an
idle battery's readings look hours old while healthy. With
`ha_not_synced` nothing can be sent; with `kill_switch_off` nothing is written except one safe idle on the
`on → off` edge.

**Kill switch and heartbeat.** Writes happen only while `actuation.entities.kill_switch`
(`input_boolean.battery_optimizer_live`) is `on`; the service never writes it. Every reconciled `live` tick
sets `input_datetime.battery_optimizer_heartbeat` to now; an HA watchdog (YAML in
[ADR-0007](adr/0007-live-battery-control.md#home-assistant-helpers-and-watchdog-homelab-proposed-not-in-this-repository))
turns both direction switches off when it is older than 5 minutes while the kill switch is on. A tick that
cannot even confirm safe idle sends no heartbeat.

**Restart and shutdown.** Plans live only in memory; the actuator acts only on intents of plans solved by
this process, so after a restart it holds safe idle until the first fresh plan: a catch-up plan solved after
the first HA sync is adopted by the slot in progress ([late adoption](#scheduling-and-the-slot-cycle)), so
control resumes within that slot rather than at the next boundary. A late-adopted intent passes the same
guard as any other. A graceful stop (SIGTERM)
cancels the actuator loop and sets safe idle before the HA session closes (`shutdown_timeout_s`). None of
this reaches the battery while Home Assistant itself is down: in the commissioning test (HA outages of 23 s
to 10.5 minutes) the battery kept its last command, see
[known issues](known-issues.md#the-battery-holds-its-last-command-when-home-assistant-is-down).

**`dry_run`** runs all of the above (kill switch included) and records every write with `result: dry_run`
under `actuation/` and in `actuator_writes_total{result="dry_run"}`; nothing is sent, because the HA client
has no write allowlist outside `live`. It plans each write against a simulated state of the entities it would
already have written (the real ones never change), so a stable situation records no repeated writes.

The actuator also wakes on a kill-switch change, and on a grid reading above
`import_cap_w + import_guard_margin_w` while charging, at most once per `tick_interval_s`.

Configuration keys (`actuation:` in `config/service.yaml`; defaults in parentheses): `entities.*` (eleven
entity IDs, all required outside shadow, including `battery_liveness`, deployed as
`sensor.omnibattery_non_responsive_batteries`: Omnibattery keeps the last SOC and AC power values when
Modbus fails, so this is the check that catches a silent battery), `battery_ac_power_sign` (none: required
outside shadow; -1 committed, measured 2026-11-03: `sensor.venus_ac_power` is negative while charging),
`tick_interval_s` (30), `import_cap_w` (4500), `import_guard_margin_w` (200), `import_trip_hold_s` (300),
`neutral_pd_target_w` (0), `discharge_margin_w` (50; 100 deployed), `min_discharge_cap_w` (100), `max_discharge_cap_w`
(2500), `healthy_states` (`balanced`, `charging`, `discharging`), `command_policy.max_attempts` (3),
`command_policy.min_setpoint_change_w` (100), `command_policy.min_command_interval_s` (30),
`readback_timeout_s` (10), `shutdown_timeout_s` (15), `freshness_s.grid_power` (30),
`freshness_s.battery_soc` (null = availability only), `freshness_s.battery_ac_power` (null; a number of
seconds enables an age test), `load_following.enabled` (false), `load_following.max_w` (null =
`max_discharge_cap_w`), `load_following.soc_hysteresis_pct` (1), `soc_floor_recovery_w` (200),
`soc_floor_hysteresis_pct` (1). Validation: `import_cap_w + import_guard_margin_w` < 5000 (the contract),
`discharge_margin_w` > 0, `tick_interval_s` ≤ 120, `0 < min_discharge_cap_w ≤ max_discharge_cap_w ≤ 2500`,
`load_following.max_w` within `[min_discharge_cap_w, max_discharge_cap_w]`,
`0 < load_following.soc_hysteresis_pct ≤ 10`, `soc_floor_recovery_w` > 0 and ≤ `import_cap_w`,
`0 < soc_floor_hysteresis_pct ≤ 10`. `mode: live` also requires
`planning.soc_source: entity`. The section is excluded from the plan's config hash.

### Load-following discharge

`actuation.load_following` (**off** by default; [ADR-0007 amendment 2026-11-07](adr/0007-live-battery-control.md#amendment-2026-11-07-load-following-discharge))
lets a planned discharge cover the real house load instead of the forecast one. In a slot whose intent is
`discharge`, while the measured SOC is at or above the plan's end-of-slot SOC (the intent's `soc_target_frac`),
the system discharge cap is raised from `max(P, min_discharge_cap_w)` to the load-following ceiling,
`load_following.max_w` (`null`: `max_discharge_cap_w`), bounded like every cap write by
`[min_discharge_cap_w, max_discharge_cap_w]` (the guard's bound, not widened) and the entity's own max, and never
below the planned cap. The PD target stays `discharge_margin_w`, so the battery follows the load up to the
ceiling and still never exports; Omnibattery's own limits (Venus caps, a temperature limit if switched on) still
apply below the system cap.

- **Hysteresis:** it engages when the measured SOC reaches the plan SOC + `soc_hysteresis_pct` (1 point) and
  releases when it falls below the plan SOC, so a SOC hovering at the plan SOC does not flap the cap; each edge is
  one cap write (never rate limited, as before).
- **Slot edges:** the first tick of a slot (the actuator wakes at every slot open) judges the new intent against
  its own plan SOC. The latch carries over between consecutive discharge slots (released at once if the SOC is
  below the new plan SOC) and resets on a charge or idle intent, a settle hold, a safe idle or the kill switch, so
  the next discharge needs the hysteresis again. A raised cap left on the entity after a discharge is harmless
  (discharge is off) and is rewritten before the next discharge switches on, as today.
- **Floor:** the plan SOC is never below the plan's minimum SOC, so neither is the release threshold; the overshoot
  is at most one SOC reading step plus one tick (30 s) at the ceiling. The MPC replans every slot from the measured
  SOC, so energy delivered early is planned out of later slots.
- **Records:** with the feature on, the desired state in the `actuation/` rows, `GET /status` and the log carries
  `load_following` (engaged), `planned_max_discharge_w` (the cap the plan alone gives) and
  `load_following_floor_pct` (the plan SOC); a row is written at every engage or release, with the log line
  `actuator load_following=on|off`. Metrics: `actuator_load_following_engaged`,
  `actuator_load_following_raise_watts`. With the feature off the records, metrics and writes are as before.

### Reserve protection

[ADR-0007 amendment 2026-11-08](adr/0007-live-battery-control.md#amendment-2026-11-08-reserve-protection): while the
**measured** SOC sits at or below the tariff's floor, a tick the plan maps to idle or discharge asks for a bounded
recovery charge instead (`reason: below_soc_floor`). Forcing idle was not enough: the measured residual drain
(about 26 W on 2026-11-08) continues in idle, so only opposing it holds the floor.

- **The floor is not configured twice:** it is `battery.soc_window_min` of `config/tariff.yaml` (0.20 here), the same
  value the planner clamps to, passed per tick as `ActuationContext.soc_floor_pct`. A tariff without a usable floor
  (missing, not finite, outside 0..1) means no protection. The planner's clamp is left as it is (a plan must stay
  feasible); the protection reads the meter, not the plan's assumption.
- **`actuation.soc_floor_recovery_w` (default 200 W, must be > 0 and <= `import_cap_w`)** is the AC charge power the
  recovery wants. It goes through the existing charge row: the PD target becomes `house load + power`, bounded by
  `min(import_cap_w, entity max)` like any planned charge, with the same write ordering, dwell and readback. Only the
  already-allowlisted PD target and charge allow switch are written; no entity, service or bound is added or widened.
- **It never weakens the plan:** a planned charge at or above the recovery power is mapped untouched
  (`reason: intent`); a weaker one is raised to it. When `min(import_cap_w, entity max)` leaves less headroom above the
  house load than the recovery power, nothing is raised to the ceiling the plan never asked for: an idle or discharge
  tick holds the idle mapping (`reason: below_soc_floor`, charge off) and a weaker planned charge keeps its own row
  (`reason: intent`, clamped by that ceiling as usual). Either way the tick never discharges below the floor.
- **Precedence:** `ha_not_synced`, the owner's kill switch, every guard reason (stale or non-finite SOC or load, an
  unresponsive or unhealthy battery, an expired or foreign plan, a stale price, import over cap) all come first, then
  the settle hold, then the recovery, then the normal mapping. During the import trip's `import_trip_hold_s` the
  recovery sends no charge (a recovery charge is a charge, so it waits for the hold), and because the guard scopes that
  refusal to *charge* intents, the recovery also refuses a planned discharge: the tick holds the idle mapping with both
  directions off instead of draining the floor while no charge is allowed.
- **Hysteresis:** engages at the floor, releases at floor + `actuation.soc_floor_hysteresis_pct` (default 1.0 SOC
  point, `0 < value <= 10`, validated at load), latched between ticks and starting released after a restart, so a
  service that comes back at 15% recovers immediately and a SOC hovering at the floor does not flap the setpoint.
- **Enable semantics:** no separate flag. It is part of the actuator, so it applies automatically in `dry_run`
  (computed and recorded, nothing sent) and in `live` for whichever deployment runs this release; `shadow` has no
  actuator and stays inert, and nothing here makes a service live. Recovery grid energy is off-plan: its cost can
  differ from the optimized plan, and the kWh stay recorded in the `actuation/` rows.
- **Records and metrics:** `GET /status` carries `reserve` (`floor_pct`, `engaged`, `recovery_w`,
  `hysteresis_pct`); the tick records carry `below_soc_floor` as the desired reason (a deliberate recovery, not a
  guard fallback). Metric: `battery_optimizer_actuator_reserve_engaged`.

### Periodic full charge: the weekly-charge enabler

Plan: [periodic-full-charge](planning/periodic-full-charge.md); the allowlist change is the
[ADR-0007 amendment 2026-11-04](adr/0007-live-battery-control.md#amendment-2026-11-04-periodic-full-charge).
The LFP cells need a charge to 100% from time to time so that the BMS balances them. Omnibattery's own weekly
full charge does the work and limits itself (it lifts its maximum SOC to 100%, completes on its own detection,
restores 95% and clears the run at its day rollover); the service only arms it. **Off by default**
(`full_charge.enabled: false` in `config/service.yaml`): with it off the actuator never reads or writes the
entities below and the guard refuses them in every mode (so does `full_charge.mode: shadow`, the default).

This part (the guard and the actuator) is what a `FullChargeDirective` in `ActuationContext.full_charge` drives:
`armed`, the weekday of the run's local date and the optional `settle_until` of the settle hold. The
[scheduler](#periodic-full-charge-the-scheduler) builds the directive.

* **Writes** (`live` with `full_charge.enabled` and `full_charge.mode: live` only; roles `full_charge_day` and `full_charge_enable`, with the
  same readback, retry, `forbidden`-never-retried and `dry_run` simulation as every other role; each only on an
  edge, never rate limited): arm = `select.select_option` to the weekday, then `switch.turn_on`, both before
  the direction on-writes of the same reconcile and each skipped when it already reads back; disarm =
  `switch.turn_off`, the first write of the restrictive block. While the feature is enabled and a directive is
  given, the service drives the switch to it on every tick (it owns the entity). A failed write of the pair
  drops to safe idle like any other, and the same directive is then refused as `write_failed` (not retried
  every tick) until the scheduler changes it.
* **Refusals** (arming is never written, the switch is driven off, `full_charge_refusal` in the actuator `view()`
  says why): `delay_switch_on` and `delay_switch_unreadable` (`switch.omnibattery_weekly_full_charge_delay`
  must read `off`: on means the weekly run waits for solar charge, and there is no PV here),
  `day_options_invalid` (the select's `options` must contain the seven weekdays; an Omnibattery upgrade can
  change them), `entities_missing` (switch or select absent or unavailable), `not_today` (the directive's
  weekday is not the local weekday of the tick), `kill_switch_off`, `write_failed`.
* **Guard.** `integration_status == weekly_full_charge` is healthy **only while the directive is effectively armed
  for today** (it is not in `healthy_states`, and the config refuses it there); every other status still holds
  `integration_unhealthy` safe idle. A guard-failure safe idle neither arms nor disarms (it would only toggle
  Home Assistant config entries); one that comes with a directive of `armed: false` still disarms.
* **Kill switch and shutdown.** The kill switch's `on → off` edge and SIGTERM turn the switch off once, best
  effort, after the existing safe idle (once a directive has been seen); nothing else is written.
* **`view()["full_charge"]` / `snapshot()["full_charge"]`** (feature enabled only): `enable_switch`, `day`,
  `delay_switch`, `integration_status`, `refusal` (and `driven_armed` in the view): the weekly readings the
  scheduler reads. In `dry_run` the switch and the day show the simulated state.
* **Time zone.** Omnibattery decides the weekday with the container's local time; the service writes the weekday
  of its own `timezone`. Checking that Home Assistant's `time_zone` equals the service's is the runtime's startup
  check ([scheduler](#periodic-full-charge-the-scheduler)), not part of the actuator.
* **Settle hold.** A directive with `settle_until` in the future makes the actuator map the slot to the idle
  intent (`desired.reason: settle_hold`, `view()["full_charge"]["settle_hold"]`) whatever the plan says, with the
  weekly switch still on; the guard still wins.

`full_charge:` keys (see the comments in `config/service.yaml`): `enabled`, `mode` (`shadow` default, or `live`:
see the [feature mode](#periodic-full-charge-the-scheduler)), `cadence` (the table of the
[scheduler](#periodic-full-charge-the-scheduler): `unknown` 10/14, `green` 14/28, `yellow` 7/14, `alert` 5/7 days),
`min_comparable_readings` (4), `alert_consecutive` (2), `settle_timeout_s` (300), `threshold_min_eur` and
`threshold_scale_eur` (0.01), `fixed_cost_eur` (0), `target_soc_frac` (1.0), `reached_soc_pct` (99),
`shortfall_penalty_eur_per_kwh` (10), `completion_margin_slots` (2), `arm_lead_slots` (1), `arm_before_soc_frac`
(0.93), `ack_timeout_s` (90), `top_hold_timeout_min` (120), `taper.start_soc_frac` (0.90, `null` disables it) and
`taper.power_kw` (0.2), `max_consecutive_failures` (3), `failure_pause_days` (5), `bootstrap_last_full` (ISO date or
`null`) and `entities.*` (ten entity IDs, all required when enabled: the two written, the delay switch and the
charge-delay switch, and six balance sensors, read only). Validation: per cadence row `0 < min_days < max_days <=
30` and `green.min_days >= unknown.min_days >= yellow.min_days >= alert.min_days` (a healthier battery is never
measured more often); `min_comparable_readings >= 1`; `alert_consecutive >= 2`; `0 < settle_timeout_s <= 900`;
thresholds, fixed cost and penalty finite and non-negative (penalty positive); `0 < arm_before_soc_frac <
target_soc_frac <= 1`; `taper.start_soc_frac` in (0.5, 1) and `taper.power_kw` in `(0, max_discharge_cap_w / 1000]`;
with `enabled: true` every entity set, distinct from each other and from `entities` and `actuation.entities`,
`planning.soc_source: entity`, and `actuation.entities.integration_status` and `battery_soc` set. The balance
sensors (`cell_delta`, `balance_status`, `last_balance_read`, `delta_trend`, `delta_avg_4w`, `balancing_mode`) may name
the same entities as `battery_health.entities` (one entity store entry each), and where both sections name a role
they must agree. `enabled` works in every mode; the two writable entities join the allowlist
only when it is true and the mode is `live`. The section is excluded from the plan's config hash (a committed
plan carries its spec in its inputs).

### Periodic full charge: the scheduler

Pure rule and state machine in `service/full_charge.py`, I/O in `runtime.py`; plan and
[amendment](planning/periodic-full-charge.md#amendment-2026-11-04-success-criterion-settle-hold-and-adaptive-cadence)
A1 to A7 are the specification, this section is how it behaves. Off with `full_charge.enabled: false`: no
decision, no state file, no new entity read, no metric, no `/status` block.

**Feature mode.** `full_charge.mode` is the feature's own switch, independent of the service `mode`: `shadow`
(default) or `live` (`live` needs the service `mode: live`; the config refuses it otherwise). With the service
`live` and `full_charge.mode: shadow` the feature is **isolated**: decisions are evaluated and recorded, a simulated
run advances a simulated `L` and the state file is `writer_mode: simulated`, but the regular plan stays the plan
of record (no commit-mode target plan, no lifted SOC clamp), the actuator is built without the feature and gets
no directive (`ActuationContext.full_charge` is `None`: the weekly switch and select are never touched, the weekly
status is never accepted as healthy, no readings of them), and the guard allowlist does not include the two
entities (both raise `ForbiddenHACallError`). Nothing about the real control path changes. In service `shadow` and
`dry_run` the feature behaves as before (simulated writer; `dry_run` also records the would-be arming writes).
Going from feature `shadow` to `live` discards the simulated state, like `dry_run` to `live`: the first live run
is eligible at once. `/status` has `full_charge.mode` and the metric `full_charge_mode{mode}`.

**Success criterion (A1).** A run counts as a full charge **only when Omnibattery records a new valid balance
measurement**: `last_balance_read` advances past the value seen when the run was armed and `balance_status`
is not `unknown`. SOC 99 or 100 (or Omnibattery completing) without a new reading inside the settle window is
`failed_reached_unmeasured`: a failed attempt, `L` not moved, never reported as balanced. A stale, missing or
`unknown` reading is unknown, never zero imbalance. Outside a run, a new valid reading stamps `L` too (an owner's
manual charge included); SOC at 99 alone never does. The sensors are read from the entity store, one entry
per entity (the balance sensors are shared with `battery_health`); a reading whose status follows its timestamp
later is accepted when the status arrives inside the window.

**Cadence (A3).** `min`/`max` days since `L` come from the class of the latest valid readings, worst first:

| Class | When | `min` / `max` days |
|---|---|---|
| `alert` | orange or red on `alert_consecutive` (2) valid readings in a row; owner alert `balance_alert`, no automatic recovery | 5 / 7 |
| `yellow` | the latest valid reading is yellow, or a single orange or red after a better one (repeat the measurement; never an alert) | 7 / 14 |
| `green` | `min_comparable_readings` (4) valid readings, all green, the latest not older than the green row's `max`, `delta_trend` known and not `rising` | 14 / 28 |
| `unknown` | otherwise: no valid reading, fewer than 4, a rising or unknown trend | 10 / 14 |

`d < min`: nothing (no solve). `min <= d < max`: once tomorrow's prices are published and a regular plan exists,
one evaluation solve (`planner.evaluate_full_charge`, after the regular plan is published, in the same worker)
prices the day; it is chosen when `forcing_cost + fixed_cost_eur <= threshold(d)` with
`threshold(d) = threshold_min_eur + threshold_scale_eur * (d - min) / (max - d)`, and a target that misses by
more than 0.05 kWh is declined. `d >= max`: forced, no solve. The decision is sticky for the day. Bootstrap (A4):
no `L` yet, so `d` is the `unknown` row's `min` until that many days after enablement, so the first run is eligible
as soon as the feature is enabled and forced `max` days after it (`enabled_at` is in the state file);
`bootstrap_last_full` is an explicit override, and a valid reading dated after `battery.commissioned_at` is honoured
as `L`. Overdue fallback: at the first tick of a day with no decision and `d >= max`, today is chosen (forced).
One attempt per local day; three failures in a row pause the rule for `failure_pause_days` (alert `paused`); a new
valid reading clears the count and the pause. `cancelled` runs (a refusal) are not failures.

**States**: `idle`, `scheduled` (a day was chosen; the plan of record is the commit-mode target plan from the
decision on), `armed` (the directive says arm; the plan allows it from local midnight of the day, one slot before
the first charge slot or before the SOC would exceed `arm_before_soc_frac`), `charging` (`integration_status` is
`weekly_full_charge`), `reached` (SOC at or above `reached_soc_pct`), `settling` (completion seen: status left, SOC
100 or `top_hold_timeout_min`). Every run ends in one outcome and the machine returns to `idle` in that step:
`done`, `done_simulated`, `failed_never_armed`, `failed_unacknowledged` (no `weekly_full_charge` within
`ack_timeout_s` of the arm being driven; a guard-failure safe idle does not start that clock),
`failed_never_reached` (day over, or the status left below 97% peak: aborted), `failed_reached_unmeasured`,
`cancelled`.

**Settle hold (A2).** After completion the directive carries `settle_until = completion + settle_timeout_s`
(300 s). Until then the actuator holds the existing idle intent (no charge, no discharge, neutral PD target,
`reason: settle_hold`) and the weekly switch stays on; the scheduler ends the hold by a new reading (`done`) or at
the deadline (`failed_reached_unmeasured`) and the same reconcile turns the switch off and resumes the plan.
The hold is a field of the directive; it needs no entity and no service call, is subject to the kill switch and the
guard (a guard-failure safe idle is idle anyway) and survives a restart (the deadline is in the state file). Whether
Omnibattery records its reading before the weekly switch goes off is verified on the supervised run.

**Gating.** At startup the runtime reads HA's `time_zone` (`GET /api/config`); a mismatch with the service's
`timezone` suspends the feature (hands off, alert `timezone_mismatch`), an unanswered check keeps it suspended and
is retried every 60 s. Once synced, the weekly switch, day select (seven weekdays in `options`) and delay switch must
exist (`entities_missing` otherwise, self-healing). While suspended or before the first step after a sync the
runtime passes **no** directive to the actuator (it never disarms a running run by accident after a restart).

**State and restart.** `state/full_charge.json` (atomic replace, `0600`, `writer_mode`) holds knowledge and a
calendar decision, never a plan: `L` and its source (`balance_reading`, `simulated`, `bootstrap`), the recent readings,
the trend, the run (day, decision, armed/completed instants, SOC peak, settle deadline), failure counters, the pause.
After a restart the directive is re-derived from the state, a plan solved by this process and the guard; the writes
are idempotent and a persisted `armed` alone writes nothing. A corrupt or invalid file is renamed
`full_charge.json.corrupt-<UTC time>` and the bootstrap runs; an unwritable file raises `state_unwritable` and keeps
the state in memory. A state of the other writer (below) is discarded: the first `live` start bootstraps the real
`L` (a reading after commissioning is honoured again), and so does a return to `dry_run`.

**`shadow` and `dry_run`** use the simulated writer: decisions, plans and (dry_run) the would-be arm writes are
recorded, nothing is sent and nothing runs on the battery; a simulated run ends as `done_simulated` at the end of its
day and moves a simulated `L` (the actuator's refusals still cancel it, except `kill_switch_off`, which only a live
run needs). Real readings still stamp `L`.

**Records** (`full_charge/date=.../full_charge.jsonl`, append-only, one row per event): `bootstrap`, `reading`,
`last_full`, `decision` (day, `d`, class, bounds, forced, overdue, accepted, `forcing_cost_eur`, `vs_normal_eur`,
the normal, lifted and target objectives, threshold, shortfall, checkpoint slot, solver summaries), `transition`
(with the evidence sensors: SOC, cell delta, 4-reading average, balancing mode, weekly switch and day, delay
switches, `integration_status`), `outcome` (with the run), `paused`.

**`/status` `full_charge`** (only when enabled): `mode`, `state`, `suspended`, `writer`, `last_full_at`,
`last_full_local_date`, `last_full_source`, `days_since`, `cadence` (class, `min_days`, `max_days`, trend, valid
readings), `next_eligible_day`, `forced_after_day`, `readings`, `run`, `last_decision`, `last_outcome`,
`consecutive_failures`, `paused_until`, `alerts`, `state_problem`, `time_zone`, `entities_problem`, `directive`,
`evidence` and the actuator's `full_charge` view.

**Metrics** (`battery_optimizer_full_charge_*`, only when enabled): `state{state}` (one-hot), `days_since_last`,
`last_timestamp_seconds`, `scheduled_day_timestamp_seconds`, `forcing_cost_eur`, `threshold_eur`,
`soc_peak_percent`, `cadence_class{class}`, `cadence_min_days`, `cadence_max_days`, `alert{reason}` (`delay_switch_on`,
`timezone_mismatch`, `entities_missing`, `never_reached`, `unacknowledged`, `reached_unmeasured`, `balance_alert`,
`paused`, `state_unwritable`), `runs_total{outcome}`; the actuator's `writes_total` covers the two new roles.
Suggested homelab alerts: `full_charge_alert == 1` for 5 m; `full_charge_days_since_last >
full_charge_cadence_max_days + 0.5`.

**Acceptance** (plan, A7): at least 7 days of `dry_run` on the installation (decisions, `forcing_cost`
distribution, the planned arming writes, no sends), a **supervised first live run** with the owner and the kill
switch (check that no `omnibattery` setup line appears in the HA log when the switch and select change, record the
taper timeline and the time from completion to the reading), and the homelab watchdog follow-up before unattended
use. Configuration to enable it: [Enabling](#enabling-the-periodic-full-charge).

#### Enabling the periodic full charge

Only the keys that differ from the committed defaults go into the deployed overrides file (homelab); the
committed `config/service.yaml` names the installation's entities and `planning.soc_source: entity` is already the
deployed one.

The service stays in `mode: live` for normal battery control; `full_charge.mode` decides whether the feature
only simulates or acts.

(a) Live control, feature in shadow (decisions and simulated runs recorded under `full_charge/` and in
`state/full_charge.json`; the plan of record, the actuator and the guard are unchanged; run it for at least 7 days):

```yaml
mode: live
full_charge:
  enabled: true
  mode: shadow
```

(b) Live control, feature live (after (a), with the owner present for the first run and the homelab watchdog
extended to the weekly switch; the guard then admits exactly the weekly switch and its day select besides the
existing allowlist):

```yaml
mode: live
full_charge:
  enabled: true
  mode: live
```

`full_charge.mode` defaults to `shadow`, so (a) is `enabled: true` alone. Going from (a) to (b) discards the
simulated state and bootstraps the real `L` (the first live run is eligible at once and forced 14 days after
enablement; set `full_charge.bootstrap_last_full` to an ISO date to override). Setting `enabled: false` again hands
the weekly entities back: the service stops reading and writing them. A `dry_run` service also simulates the
feature (and stops live battery control), but is no longer needed for the decision study.

### Battery health telemetry

Read-only. `battery_health:` in `config/service.yaml` names optional sensors; with none named the section is off
(no metric, no `/status` block). The entities join the entity store's tracked set, so they arrive through the
existing `state_changed` stream and the `get_states` resync, nothing else: there is no write, no new service
call and no change to `ha_guard`. The tracker (`service/battery_health.py`) is a pure state machine; the
runtime feeds it each accepted or refreshed observation and a 30 s tick, and reads it for `/status` and the
metrics. It informs only; the periodic full charge scheduler
([plan](planning/periodic-full-charge.md)) reads the same entity store entries, not this tracker.

| Key | Meaning |
|---|---|
| `entities.temperature` | `sensor.*`, degC (`sensor.venus_internal_temperature`) |
| `entities.cell_voltage_max` / `cell_voltage_min` | `sensor.*`, V (`sensor.venus_max_cell_voltage` / `sensor.venus_min_cell_voltage`) |
| `entities.cell_delta` | `sensor.*`, mV: last comparable settled top-of-charge delta (`sensor.venus_cell_delta`) |
| `entities.balance_status` | `sensor.*`: `green`, `yellow`, `orange`, `red` or `unknown` (`sensor.venus_balance_status`) |
| `entities.last_balance_read` | `sensor.*`, timestamp of the last valid measurement (`sensor.venus_last_balance_read`) |
| `entities.delta_trend`, `entities.delta_avg_4w` | `sensor.*`: trend text, mV average of the last four readings |
| `entities.balancing_mode` | `binary_sensor.*` (`binary_sensor.venus_balancing_mode`) |
| `hot_thresholds_c` | Strictly increasing temperatures whose exposure is counted (default `[35, 40]`) |
| `charge_threshold_w` | Sign-corrected AC power above which the battery is charging (100) |
| `session_end_hold_s` | The power must stay at or below the threshold this long to end a session (300) |
| `max_gap_s` | Most one clock advance credits to the held temperature (300) |

Entities are distinct from each other but may repeat `full_charge.entities` evidence sensors (the same store
entry serves both). The section is excluded from the plan's config hash.

* **Hot seconds** are seconds *strictly above* each threshold, by sample-and-hold: the last temperature is held
  until the next observation, the 30 s tick, a scrape or a `/status` call. HA reports a sensor only on change, so
  a steady temperature is silent for hours and the tick is what counts it. One advance credits at most
  `max_gap_s`; the remainder (a stalled event loop) is not counted and is reported as `uncounted_s`
  (`hot_uncounted_seconds_total`). While the temperature is unknown or unavailable nothing is counted. The
  counters restart at zero with the process, as Prometheus counters do (`increase()` handles it). They do not
  stop while Home Assistant is disconnected: the last temperature is held, which is a limit of the data.
* **Charge sessions** use `actuation.entities.battery_ac_power` and `actuation.battery_ac_power_sign` (the
  actuator's entity and sign: positive after the sign = charging; `-1` on this installation, where
  `sensor.venus_ac_power` is negative while charging), tracked in every mode. With no sign configured sessions are
  not tracked (`charge_sessions_tracked: false`). A session starts above `charge_threshold_w` and ends
  `session_end_hold_s` after the power last fell to the threshold or below (dated to the first sample below, so
  the taper at the top is one session); an unavailable power ends it at once. Peak temperature is the highest
  observed during the session, including the one held when it started. `/status` shows the running session, the
  last completed one and the count; the last 20 are kept in memory.
* **Unknown is never zero.** An unavailable, unknown or non-numeric state is `null` in `/status` and an absent
  sample in the metrics. The balance monitor reads `unknown` until the first full charge: `balance.status` is
  then `unknown`, every other balance field `null`. `last_read_age_s` is the staleness of the last valid
  measurement; `spread_mv` is `null` unless both cell voltages are known and the maximum is not below the minimum.
* **Status block.** `battery_health`: `temperature_c`, `hot_seconds` (keyed by threshold), `uncounted_s`,
  `charge_session` (`active`, `current`, `last`, `completed`), `cell_voltage` (`max_v`, `min_v`, `spread_mv`),
  `balance` (`cell_delta_mv`, `status`, `delta_trend`, `delta_avg_4w_mv`, `balancing_mode`, `last_read_at`,
  `last_read_age_s`), `actuation: "none (read-only telemetry)"` and `charge_sessions_tracked`.

The temperature and the two cell voltages are also extractable for offline analysis, as their own extraction
group; see [`extract`](cli.md#extract-the-battery-health-group).

### Thermal shadow (derate prediction)

Read-only and **observability only**. `planning.thermal.mode` is `"off"` (default: nothing is read, recorded or
exposed; the service, its plans, hashes, metrics and `/status` are byte for byte what they were) or `shadow`.
Design: [derate-aware planning](planning/thermal-derate-aware-planning.md) (this is its PR 2); component page:
[battery-thermal](components/battery-thermal.md). No plan, intent or actuation changes in either mode, and the
`ha_guard` allowlist is unchanged: the inputs join the entity store's tracked set, so they arrive through the
existing `state_changed` stream and `get_states` resync, and no value is ever written to them (a test asserts the
live allowlist admits none of them).

| Key | Meaning |
|---|---|
| `mode` | `"off"` or `shadow` (needs the five required entities, `actuation.entities.battery_ac_power` and `actuation.battery_ac_power_sign`) |
| `entities.temperature` | `sensor.venus_internal_temperature`; shared with `battery_health.entities.temperature` (they must agree; either may be omitted) |
| `entities.limit_switch`, `limit_c`, `band_c`, `floor_pct` | Omnibattery's `switch.omnibattery_temp_charge_limit` and `number.omnibattery_temp_charge_limit_c` / `_band_c` / `_floor_pct` (required) |
| `entities.discharge_switch` | `switch.omnibattery_temp_charge_limit_discharge` (optional: without it the discharge side is never derated) |
| `entities.system_max_charge_power`, `venus_max_charge_power` | the two cap numbers (optional): the curve scales with their minimum; without them `charge_limit_kw` |
| `charge_limit_kw`, `driver_min_kw` | 2.5 and 0.8: the limit when the caps are unknown, and the driver minimum the cap never falls below |
| `prior` | `{tau_min, t_amb_c, k_c, k_d}` before any accepted refit: 44 min, 32.6 degC, 11.0 and 6.9 degC/kW (charge and discharge gain; see the component page), each within its bounds |
| `refit_hour_local` | the daily refit runs at or after this local hour (4) and at start-up when one is due |

Entities must be distinct from each other and from `entities` roles, `actuation.entities` and
`full_charge.entities` (they may repeat `entities.observed`). The section never joins the plan's config hash.

* **Derate state.** `off`: the switch is off, no derate in the model. `on`: the switch is on and the numbers are
  valid (`band_c > 0`, `0 <= floor_pct <= 100`, `limit_c` within 25 to 60). `unknown`: the switch or a number is
  missing, `unavailable`, `unknown` or out of range, and the model stays blind (today's behaviour, the safe
  direction). The curve is Omnibattery's documented one: factor 1 up to the limit, linear to the floor over the
  band, cap `max(limit x factor, driver minimum)`, at most the limit; the discharge side uses it only while its own
  switch is on. A missing role, an unavailable state or a stale temperature (older than 10 minutes) is `null`/
  `unknown`, never a 0 and never an error: planning and actuation never wait for it.
* **Observation buffer.** A tick once a minute and every observation feed a one-minute grid of the temperature and the
  signed battery AC power (positive while charging, after `actuation.battery_ac_power_sign`) over 21 days, by
  sample-and-hold. A minute is unknown (NaN, never idle) when the temperature is older than 10 minutes, a reading
  is unavailable, or the Home Assistant session was not synced; the downtime of a restart is unknown too. The buffer
  and the model state are persisted (`state/thermal_samples.json` every 30 minutes and at a refit and at stop,
  `state/thermal.json` at every refit) and restored at start; an unreadable or invalid file is ignored (reported as
  `restore_problem`) and the prior stays.
* **Model and refit.** One first-order node, `T' = (T_amb + k_c P_charge + k_d P_discharge - T) / tau` (the code is
  `service/thermal.py`, shared with `hack/battery_thermal_derate.py`), starting from `prior`. Daily, in its own
  thread, the buffer is fitted (multi-step least squares over 10 to 90 minute horizons, `tau` profiled within 20 to
  120 minutes, a ridge towards the prior that fades with the rows, parameters clipped to `T_amb` 20 to 40, `k_c` 2
  to 15, `k_d` 2 to 20) and judged: a gain is refitted only if its direction has at least 10 minutes above 1 kW
  from at least three episodes, otherwise it keeps its value; the candidate is **accepted** only if its
  leave-one-episode-out 60 minute error beats persistence by a skill of at least 0.5 and beats the current
  parameters on the held-out rows of the last 7 days. Otherwise the parameters stay and the status is
  `degraded`; too little data (under two episodes or 12 hours) or no excitation (60 minute persistence error under
  1 degC) is `insufficient_data` and changes nothing. Status: `prior` (never fitted), `fitted`, `degraded`.
* **Prediction per plan.** Each published plan is simulated from the measured temperature (`cold_start` from
  `T_amb` when it is unknown) through its per-slot charge and discharge powers; the battery draws
  `min(requested, cap(T))` at every simulated minute, so the derate is a feedback inside a slot and the
  temperature follows the power actually achieved. The plan file (`plans/.../<plan_id>.json`) gains an additive
  `thermal` key (absent when off): `derate` (state, settings, limit and driver minimum), `temperature_start_c`,
  `cold_start`, `model`, and `slots`, columns aligned with `inputs.slot_starts`: `temp_start_c`, `temp_end_c`,
  `temp_max_c`, `derate_factor` and `charge_cap_kw` (the lowest inside the slot), `charge_planned_kw` and
  `charge_kw` (planned and achievable), `discharge_planned_kw`, `discharge_kw`; and a `summary`. The minutes
  between the decision and the first slot are not simulated. A failing prediction is logged and counted and never
  blocks planning.
* **Scoring at slot close.** The slot row gains a `thermal` block: the measured temperature at the slot's end, for
  each lead (`final` the plan of record, `1h` and `3h` the newest plan made at least one and three hours before the
  slot ended) the predicted end temperature and the error, and, for the plan of record in a slot where a derate was
  predicted or measured, the predicted achievable against the observed mean charge power. Beside a native
  controller that does not follow the plan the power comparison mixes the model error with plan-following error.
* **`/status` `thermal`.** `mode`, `temperature_c` and its age, `power_kw` (+ charging), `derate` (state, limit
  settings, `limit_kw`, `factor_now`, `derating_now`), `model` (status, parameters, `fitted_at`, `prior`, the
  quality of the last accepted fit, the last refit with its reason and candidate, `restore_problem`), `buffer`,
  `scoring` (per lead: n, mean absolute error, rmse, bias; the derate power error) and `current_slot` (the newest
  prediction for the slot in progress).

| Metric (`battery_optimizer_thermal_`) | Type | Labels |
|---|---|---|
| `temperature_celsius` | gauge (absent while unknown or stale) | — |
| `derate_state` | gauge 0/1 | `state` (`off`, `on`, `unknown`) |
| `derate_factor`, `derating` | gauge: factor of the curve at the measured temperature; 1 while it is in force and below 1 (absent while unknown) | — |
| `limit_setting` | gauge: the switch's numbers as read | `name` (`limit_c`, `band_c`, `floor_pct`, `limit_kw`) |
| `predicted_temperature_celsius`, `predicted_derate_factor`, `predicted_achievable_charge_kw` | gauge: the newest prediction for the slot in progress | — |
| `model_status` | gauge 0/1 | `status` (`prior`, `fitted`, `degraded`) |
| `parameter` | gauge | `name` (`tau_min`, `t_amb_c`, `k_c`, `k_d`) |
| `last_fit_timestamp_seconds`, `buffer_hours` | gauge (the first absent while `prior`) | — |
| `refits_total` | counter | `result` (`accepted`, `rejected`, `insufficient_data`, `error`) |
| `predictions_total` | counter | `result` (`ok`, `error`) |
| `scored_slots_total`, `temperature_abs_error_celsius_total`, `temperature_squared_error_celsius_squared_total` | counter: slots whose predicted end temperature was scored, and the sums of the absolute and squared error | `lead` (`final`, `1h`, `3h`) |
| `temperature_error_sum_celsius` | gauge: the signed sum (predicted minus measured; falls when the model runs cold) | `lead` |
| `derate_scored_slots_total`, `achievable_charge_abs_error_kw_total` | counter: slots with a derate whose achievable charge was scored, and the sum of the absolute error | — |
| `achievable_charge_error_sum_kw` | gauge: the signed sum | — |

## Safety

* The climate plan export ([Climate plan export](#climate-plan-export)) is an HTTP `GET` on this service and a read of
  the owner's mode select through the existing state subscription: no new service call, no write, the allowlist
  below is untouched, and the effective export mode is never `live`.
* WebSocket: every outgoing frame is checked against the allowlist `auth`, `ping`,
  `subscribe_events` (only `state_changed`), `subscribe_entities`, `subscribe_trigger` (only state
  triggers), `unsubscribe_events`, `get_states`, `get_config`. Anything else raises
  `ForbiddenHACallError` before reaching the socket.
* REST: `GET` only, on `/api/`, `/api/config`, `/api/states`, `/api/states/<entity_id>`, enforced by
  an `httpx` request hook.
* The single exception to "no service call" is **`weather.get_forecasts` with `return_response: true`**,
  accepted only as the exact frame `{"id": <int>, "type": "call_service", "domain": "weather", "service":
  "get_forecasts", "return_response": true, "service_data": {"type": "hourly" | "daily" | "twice_daily"},
  "target": {"entity_id": "weather.<name>" | [...]}}`. Another domain or service, a missing or non-`true`
  `return_response`, any additional field (in the frame, `service_data` or `target`, so no area, device or
  label targets), a non-`weather.` entity or a non-integer `id` raises `ForbiddenHACallError` before the frame
  reaches the socket. It is non-mutating: the service only returns the forecast the weather entity already
  holds, it changes no state and operates no device. The only caller is the passive forecast archive, which
  builds the frame with `ha_guard.weather_forecast_request`. REST `POST` stays forbidden.
* **Live write path (`mode: live` only).** The runtime hands the WebSocket client an `ActuationAllowlist`
  only in `live`; `shadow` and `dry_run` pass none, so every write is refused as above. With it, a
  `call_service` frame passes only as `{"id", "type": "call_service", "domain", "service", "target":
  {"entity_id": "<one entity>"}, "service_data": {...}}` for `switch.turn_on`/`turn_off` on the two direction
  switches and the power-limit switch, `number.set_value` on the PD grid target (`value` in `[0,
  import_cap_w]`) and the system discharge cap (`value` in `[min_discharge_cap_w, max_discharge_cap_w]`, so
  never the `0` that means unlimited), and `input_datetime.set_datetime` (`timestamp`) on the heartbeat. With
  `full_charge.enabled` and `full_charge.mode: live` also `switch.turn_on`/`turn_off` on the Omnibattery
  weekly-full-charge switch and
  `select.select_option` on its day select, `service_data` exactly `{"option": <one of the seven lower-case
  weekdays>}` ([ADR-0007 amendment](adr/0007-live-battery-control.md#amendment-2026-11-04-periodic-full-charge));
  its delay switch, the charge-delay switch, the cutoff numbers and any button stay refused. A list
  of entities, another entity or service, an out-of-bounds or non-numeric value, `return_response` or any other
  field raises `ForbiddenHACallError`. The kill switch is never writable. The only builder is
  `ha_guard.actuation_request`.
* The HTTP API has no writable route. Tests assert all of the above and statically keep Modbus/MQTT/
  HA libraries and HTTP write calls out of the package.
* Use a **non-admin** HA user's token. `shadow`/`dry_run` use the read-only `battery-optimizer` user's token.
  `live` uses a separate token of the dedicated non-admin user `battery-optimizer-live` (group
  `system-users`, local only), kept in its own Vault KV path when deployed. Home Assistant cannot restrict a
  token to particular entities or services, so for that user the `ha_guard` allowlist is the restriction.

## Local run

```bash
export BATTERY_OPTIMIZER_HA_URL=https://homeassistant.example.org
export BATTERY_OPTIMIZER_HA_TOKEN_FILE=/path/outside/repo/ha_token   # 0600
export BATTERY_OPTIMIZER_PROMETHEUS_URL=https://prometheus.example.org
export BATTERY_OPTIMIZER_DATA_DIR=/path/outside/repo/data
battery-optimizer serve --port 8080
curl -s localhost:8080/health/ready
```
