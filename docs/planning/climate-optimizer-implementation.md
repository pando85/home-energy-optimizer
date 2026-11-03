# Climate optimizer: shared-runtime architecture and phased implementation plan

- **Status:** C2 completed **UNDETERMINED / no-go** (2026-10-30); [diagnostic-first recovery (R0–R5)](climate-c2-recovery.md) proposed. The original C2 gate remains frozen. No new climate actuation is authorized.
- **Date:** 2026-10-30
- **Architecture decision:** [ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md) (decoupled comfort control and battery arbitrage; supersedes decisions 1 and 2 of [ADR-0005](../adr/0005-shared-energy-planning-climate.md); amended 2026-10-31, see [comfort-controller-design](comfort-controller-design.md)). **Read [the decoupled objective](#decoupled-objective-and-obsolete-items-adr-0006) first: the price-driven objective, shared planner and cost-MPC rows below are obsolete or parked.**
- **Repository ownership:** `pando85/battery-optimizer` for models, planning, history and evaluations; [`pando85/homelab`](https://github.com/pando85/homelab) for AppDaemon controls, HA entities, GitOps, manifests and deployment
- **Related:** [ADR-0001](../adr/0001-home-battery-economic-dispatch.md), [ADR-0004](../adr/0004-battery-optimizer-production-architecture.md), [battery runtime](../service.md), [component architecture](../architecture.md), [load forecaster](components/load-forecaster.md)

## 1. Goal, baseline and scope

Move from **"run for the cheapest N hours"** to **"find the lowest-cost physically feasible heat-pump schedule that maintains room comfort, hot-water availability and compressor health"**. Start with space heating; integrate domestic hot water (DHW) planning once the shared safety/ownership boundary is ready. Cooling follows only after separate model and condensation-safety work.

This is an **incremental extension** of the existing resident battery-optimizer service, **not** a third Deployment. Reuse data and runtime infrastructure, but keep battery and climate optimizers domain-specific. No new background database, MQTT broker, Kubernetes service or broad HA write credential is justified.

### Verified current state (repository review, 2026-10-30)

| Area | Exists today | Limitation / planning implication |
|---|---|---|
| AppDaemon runtime | Single-replica `home-assistant` chart Deployment; `acockburn/appdaemon:4.5.13`, `apps.yaml`; deployed with HA via ConfigMaps | Existing control authority and user toggles must remain intact |
| Space heating/cooling | `climate_control.py` chooses PVPC below 2/3 of a historical mean, otherwise the cheapest minimum number of hours | No room-temperature, COP, heat-loss or weather forecast in the planning objective |
| DHW | `dhw_control.py` forces hot water in the cheapest hour of each 12-hour block | No tank-need or water-demand prediction; separate app can compete with unified planning |
| Plant control | `input_select.heishamon_heatmode` maps via HA automation to HeishaMon `SetOperationMode`; `switch.aquarea_force_dhw_mode` is a separate actuator | Input-helper state is not equivalent to confirmed heat-pump mode; avoid unbounded state waits in the C5 executor (C0 keeps the proven command path) |
| Available telemetry | HeishaMon exposes heating/DHW thermal/electric power, COP-related measurements, tank/flow/outdoor temps, compressor/defrost state; HA LocalTuya has bedroom, hall-kitchen and office thermostats | Entity IDs, rooms and topology verified (see the audit below); still verify timestamps, units and calibration before modeling |
| Existing thermal observability | Grafana climate/room/energy dashboards, a Shelly EM branch reported as aerothermal load | Channel 2 reads above HeishaMon (see the audit below); reconcile in C1/C2 before treating either as ground truth |
| Shared data/runtime | `service/ha_client.py`, `entity_store.py`, `pvpc.py`, `timegrid.py`, `history.py`; weather/PVPC archives, 15-minute UTC time grid, Prometheus, shadow sink and metrics | Reuse; do not build another data-ingestion stack |
| Battery planning | `battery-optimizer serve` solves read-only battery MPC every 15 minutes, publishing shadow plans; hardware SOC may be simulated | Keep all existing battery endpoints/semantics and HA write guard unchanged |
| Schedule-aware battery forecasting | `structured_forecast.py` already replays versioned legacy AppDaemon climate/DHW rules **offline** | Preserve this as baseline; replace replay with versioned `ClimatePlan` forecasts only after validation, without double counting |

Relevant source:
[`homelab/apps.yaml`](https://github.com/pando85/homelab/blob/master/apps/home-assistant/appdaemon/apps/apps.yaml),
[`climate_control.py`](https://github.com/pando85/homelab/blob/master/apps/home-assistant/appdaemon/apps/climate_control.py),
[`dhw_control.py`](https://github.com/pando85/homelab/blob/master/apps/home-assistant/appdaemon/apps/dhw_control.py),
[`heishamon.yaml`](https://github.com/pando85/homelab/blob/master/apps/home-assistant/config/integrations/heishamon.yaml),
[`home-assistant/values.yaml`](https://github.com/pando85/homelab/blob/master/apps/home-assistant/values.yaml),
[`GoHeishaMon`](https://github.com/pando85/GoHeishaMon) (the owner's fork running on the CZ-TAW1; `src/taw2mqtt.go`, `HandleSetOperationMode` accepts 0-10).

### Verified plant topology and data audit (read-only Prometheus and homelab review, 2026-10-30)

| Area | Verified fact | Planning implication |
|---|---|---|
| Heat distribution | Underfloor heating. LocalTuya thermostats (bedroom, hall-kitchen, office A, office T) open and close each room's valve; setpoints are set in HA and changed manually, normally high enough that valves stay open, except in office A. Bathrooms and the corridor floor have no thermostat and are always open | The slab is the main thermal store; once heating is permitted, the pump curve and room valves decide the delivered heat |
| Comfort rooms | Bedroom, hall-kitchen, office T. Office A is warmed by servers and computers (large internal gain) | Office A is excluded from the comfort objective, but modelled as a heat sink with its valve |
| Control lever | Binary space-heating permission per 15-minute slot (`Heat+DHW` vs `DHW`, existing mode path) plus the DHW force | Flow temperature stays out of scope; see 3.3 |
| History | `hass_*` metrics at 1-minute cadence from 2023-11-08 (about 2.5 heating seasons); no gap over a day in the last 12 months except a ~3-day thermostat outage in Feb 2026 | Enough history for chronological held-out validation |
| Electricity | Over 131 heating days (Nov 2025-Mar 2026), Shelly EM channel 2 (aerothermal branch, which also feeds dishwasher, washer and extractor) reads a median 1.28x (typically 1.18-1.56x) the HeishaMon heat+DHW+cool consumed energy, with ~20 W idle baseline; with the compressor running it still reads 120-310 W above HeishaMon | HeishaMon likely under-reports. Use channel 2 net of appliances as the cost-side reference and HeishaMon per-mode values for the heat/DHW split; reconcile in C1/C2 |

Usable history:

- `hass_climate_current_temperature_celsius`, `_target_temperature_celsius`, `hass_climate_mode`, `hass_climate_action` for the four thermostats
- `sensor.aquarea_outside_ambient_temperature`, tank, inlet, outlet and outlet-target temperatures
- `sensor.aquarea_compressor_frequency` and pump flow
- heat, DHW and cool power and energy, produced and consumed
- `switch.aquarea_force_dhw_mode` and `sensor.shellyem_main_channel_2_*`
- the AppDaemon enable booleans and min-hours number

Not usable, or with caveats:

- The HA exporter drops string states: no history for `sensor.aquarea_mode`, `sensor.aquarea_defrost_state`, `sensor.aquarea_3_way_valve`, `input_select.heishamon_heatmode` or `weather.*`. Weather forecasts come from the passive forecast archive only.
- `sensor.aquarea_defrost_temperature` is constant 0, so there is no defrost history. `binary_sensor.aquarea_power_state` has no state metric.
- `sensor.aquarea_energy_consumed` was frozen for ~322 days: use the per-mode heat, DHW and cool energy. The COP sensors start in 2025-12 or later: derive COP from energy.
- The template room sensors (`sensor.*_temperature`) start 2026-10-30: use the thermostat climate metrics.
- `hass_climate_action` reflects real demand only from 2026-10-30. Earlier, valve/demand state must be inferred from current vs target temperature with hysteresis.
- The `instance` label changes on every HA restart: aggregate queries by `entity`.
- Heating vs DHW can be separated historically from the separate heat and DHW power metrics. AppDaemon's permission schedule can be reconstructed by replaying the legacy policy (as `structured_forecast.py` does).

**Part of C0/C1a homelab work (text-state history):** numeric template sensors for the operating-mode code, defrost and the 3-way valve, so Prometheus starts retaining their history.

**Out of scope for the initial rollout:** hardware flow-temperature changes, direct Modbus control, multi-pump dispatch, automatic battery actuation, replacing Omnibattery, installing PV generation, retraining large ML models, and removing AppDaemon. Any projected savings before controlled validation are **model estimates**, not measured bill reductions.

## 2. Target architecture and responsibility map

~~~mermaid
flowchart TD
    subgraph SOURCES[Existing sensors and history]
        HA[HA and HeishaMon events]
        ARCH[PVPC and weather archives]
        PROM[Prometheus and HA history]
    end
    subgraph ENGINE[Existing battery-optimizer Deployment]
        INPUT[Read adapters, freshness, UTC/DST and tariffs]
        TM[Thermal model and COP estimator]
        DHW[DHW tank and demand model]
        CMPC[Climate planner]
        BMPC[Existing battery forecast and dispatch planner]
        STORE[Existing shadow store and metrics]
        API[Read-only GET /climate/plan]
        INPUT --> TM
        INPUT --> DHW
        TM --> CMPC
        DHW --> CMPC
        INPUT --> BMPC
        CMPC --> STORE
        CMPC --> API
        CMPC -. forecast HVAC electrical kWh .-> BMPC
        BMPC --> STORE
    end
    HA --> INPUT
    ARCH --> INPUT
    PROM --> INPUT
    API -. gated, future .-> AD[Existing AppDaemon executor]
    AD --> HACTL[HA / HeishaMon commands]
    HACTL --> PUMP[Panasonic Aquarea]
~~~

The **engine is read-only** in this plan. The existing AppDaemon -> HA -> pump arrow is deployed; the plan-consuming dotted arrow is a **proposal**, not a current implementation. Battery dispatch has no actuation arrow.

### Components, boundaries and proposed module locations

| Component | Input / responsibility | Output | Reuse or new |
|---|---|---|---|
| **Acquisition and freshness** | HA state/event ordering, sensor health, observed plant mode, weather forecasts and historical telemetry | Timestamped validated `ClimateObservation` | Reuse `service/ha_client.py`, `entity_store.py`, `history.py`; extend role mapping |
| **Price outlook and time base** | Published PVPC and tariff assumptions at the decision timestamp; UTC quarter-hours, local labeling, DST | `PriceCurve`, known/estimated/provenance flags | Reuse `service/pvpc.py` and `service/timegrid.py`, no second parser |
| **Building thermal model** | Indoor/outdoor temperature, heat-pump output, permission and inferred valve state, passive gains | Predicted indoor temperature trajectory and confidence/fit diagnostics | New `climate/thermal.py` |
| **COP model** | Heat/electrical measurements, ambient/flow temperature, operation mode and defrost | Estimate marginal electricity per delivered thermal kWh | New `climate/cop.py`; bounded fallback |
| **DHW model** | Tank temperature, reheating events, tank loss, demand windows, hardware hygiene schedule | Tank trajectory and DHW availability constraints | New `climate/dhw.py` |
| **Climate planner** | Frozen snapshot, comfort bands, thermal models, price/forecast horizon, compressor constraints | `ClimatePlan` with rationale, no direct actions | New `climate/planner.py`, `climate/contracts.py` |
| **Climate scheduler/runtime** | Replans triggered by slots and meaningful input changes; solver budget/cancellation | Shadow decisions, `GET /climate/plan`/status/metrics | Extend existing `service/runtime.py` and `service/api.py` behind a feature flag |
| **AppDaemon actuator** | Current selected intent, plan TTL, HA control ownership and safety telemetry | Verified HA/HeishaMon command or conservative fallback | Refactor in `homelab`; use existing writable HA connection, not battery-optimizer credentials |
| **Forecast exchange** | Versioned predicted HVAC + DHW electric kWh by slot, quality/provenance | Battery load-forecast feature; never a device command | Extend battery load forecaster and `structured_forecast.py` replay/evaluation path |

Treat file paths as **candidate boundaries**, not a mandate to split every component into its own class or microservice. Refactor shared runtime only when a second implementation requires it. Preserve offline/live use of the same domain kernels and input semantics.

### Separation from the battery domain

The battery planner manages SOC, conversion efficiency, import constraints and dispatch intents; the climate planner manages room/tank temperatures, thermal energy, COP, comfort and compressor operation. Their optimizers must remain independent initially. Reusing SciPy/HiGHS does not imply reusing `battery.py` or `oracle.solve_oracle` as the thermal solver.

Avoid a feedback loop: the battery planner consumes a **frozen** climate electrical-demand forecast for a given generation. The climate plan does **not** assume a future battery discharge is guaranteed or arbitrarily free. Joint cross-domain optimization would require a new ADR and counterfactual evaluation.

## 3. Data and model design

### 3.1 Input inventory and validation

| Source role | Candidate HA data | Model / quality checks |
|---|---|---|
| Indoor comfort | LocalTuya thermostat climate metrics: bedroom, hall-kitchen, office T (comfort); office A (modelled, not in comfort) | Flag stale/unknown, sensor offset, open-window events and heterogeneous room temperatures; infer valve state before 2026-10-30 |
| Outdoor temperature | `sensor.aquarea_outside_ambient_temperature` (verify actual registry), `weather.forecast_home` hourly forecasts (archive only, no Prometheus history) | Sensor/forecast reconciliation and forecast issue time; do not substitute realized future weather in backtests |
| Heat and electricity | Aquarea heat/DHW/cool power and energy, compressor frequency; Shelly EM channel 2 net of appliances as the cost-side reference | Reconcile channel 2 with HeishaMon (median 1.28x); validate energy integrals, W/kW units, zero/negative readings, standby, compressor start count and DHW separation; defrost has no history |
| Tank heat | Aquarea tank temperature/target, force DHW and operating mode | Exclude draws, reheats and anti-legionella from standing-loss fitting; no assumed tank sensor identity |
| Electricity price | `sensor.esios_pvpc` | Reuse published/next-day prices, `_d` fallback-hour handling, pretax-vs-billed tariff reconciliation; permit negative prices |
| Occupancy/comfort policy | HA schedules/helpers and manual overrides | Versioned comfort band per room (optionally per time of day); initial heat mode band 22-28 °C; authorized setback explicit, not inferred from device names |
| Hardware bounds | Aquarea/HeishaMon capabilities and configured minimum run/rest times | Verify manufacturer/install-specific limits before enabling control; preserve pump-native frost/hygiene protection |

Store UTC-aware, half-open 15-minute slots with provenance, sample age, availability cutoff and quality flags. Preserve raw inputs; train only on measurements available **as of** the historical decision. Recorder may purge after 10 days, so use the existing archival pipeline rather than training solely against short HA history.

Model structure: a **whole-house model** driven by total delivered heat first, then **per-room temperature models** on inferred valve state as a refinement. Comfort rooms are bedroom, hall-kitchen and office T; office A enters as a heat sink with its valve. If rooms disagree materially, use a conservative comfort aggregation.

### 3.2 Learn physics progressively

Fit two candidate heating models from the start. The first-order model:

~~~text
T_in[t+1] = T_in[t] + dt/C *
            (Q_heat[t] - UA*(T_in[t]-T_out[t]) + Q_passive[t])
~~~

The two-state slab+air model (illustrative), since underfloor heating makes the slab the main store:

~~~text
T_air[t+1]  = T_air[t]  + dt/C_air  * (k*(T_slab[t]-T_air[t]) - UA*(T_air[t]-T_out[t]) + Q_passive[t])
T_slab[t+1] = T_slab[t] + dt/C_slab * (Q_heat[t] - k*(T_slab[t]-T_air[t]))
~~~

C2 compares both on held-out data (see [C2 design](#c2-design-and-predeclared-gate)); the two-state model is not deferred. Learn/estimate `UA` (heat-loss coefficient), `C` (effective capacity/time constant) and bounded passive gain from filtered heating/coasting intervals. Distinguish heat delivered by the machine from electricity consumed; COP is a function of ambient/flow temperature and operating conditions, not a universal constant. Compare a simple temperature-conditioned COP baseline with a fitted model, rejecting implausible values.

DHW uses a **different** tank-loss/draw model and demand windows; a household draw is not a building heat-loss observation. Pump-side safety and legionella cycles remain authoritative. Cooling is a later stage (C7) with its own model. Contracts and C1a extraction are mode-aware and already extract cooling power and energy. Underfloor cooling needs a dew-point/condensation constraint, so **room humidity sources are a prerequisite** for cooling.

Models must be versioned, bounded, replayable and persisted on the existing PVC **only inside the planning runtime**. Do not store learned parameters solely in AppDaemon's current `emptyDir`.

### 3.3 Optimization and solver behavior

> **Obsolete since ADR-0006 (2026-10-30).** The objective below is price-driven and is replaced by the [decoupled objective](#decoupled-objective-and-obsolete-items-adr-0006). The constraint and safety text still applies. Kept for the record.

The decision variable is the **space-heating permission schedule** `u[t]` per 15-minute slot (`Heat+DHW` vs `DHW`) plus the DHW force, with minimum run/rest times. Delivered heat is a **modelled response** to `u[t]` (pump curve, room valves, slab state), not a free `Q_heat[t]`. A thermostat-setpoint offset within the comfort band is a candidate later lever; it needs another writer, so C2 reports it as an offline counterfactual without gating on it; it feeds C7.

Optimize a 24–48 h lookahead on the existing 15-minute grid, bounded by actual **published** prices and reliable forecast coverage; unknown later prices must be labeled as estimates with an uncertainty policy, never silently treated as known. A terminal heat-storage value/penalty must prevent artificially cheap end-of-horizon cooling.

Indicative objective, with hard physical and safety constraints and softened comfort only for unavoidable infeasibility:

~~~text
min Σ_t [ actual_import_price[t] * predicted_electric_kWh(u)[t]
        + comfort_deviation_penalty[t]
        + compressor_start_penalty[t]
        + terminal_energy_penalty ]
~~~

Constraints include the versioned room comfort band (initial heat mode value: minimum 22 °C, maximum 28 °C in bedroom, hall-kitchen and office T; every evaluation and plan records the band used), admissible permission states, manufacturer-verified minimum on/off durations, DHW availability windows and no contradictory DHW+space-heating demand on a single compressor. Distinguish fixed hardware protection from tunable comfort preferences. A negative import price must never create an unbounded incentive to overheat the house or tank.

Start with a temperature-aware rule heuristic and a small bounded solver; adopt mixed-integer/advanced formulations only if the simpler method demonstrably fails. Preserve the original cheap-hours policy for comparative baselines and rollback.

## 4. Plan publication and executor contract

The first implementation is **shadow only**. Introduce `GET /climate/plan` under the existing internal FastAPI service, with optional read-only status and metrics. Existing `/plan`, `/status` and `/metrics` battery semantics remain compatible.

Define a versioned, immutable `ClimatePlan` contract containing:

| Field | Semantics |
|---|---|
| `schema_version`, `plan_id`, `generation` and `input_hash` | Detect incompatible, replaced or duplicated decisions |
| `mode` | `shadow` (non-actionable) or, after a separate explicit rollout, `active` |
| `created_at`, `valid_from`, `expires_at` | UTC-aware plan freshness; never execute an old intent |
| `price_provenance`, `weather_provenance`, `model_version` | Audit input availability and uncertainty |
| `slots[]` with UTC start/end, action, expected electric kWh, heat/DHW allocation, predicted temperatures and reason | Explain current decision and make load demand reusable by battery planning |
| `constraints` and `readiness` | Explain rejected or degraded plans, including the comfort band version used; unknown is not safe-by-default |

The read-only endpoint publishes **proposals**, not a write API or a claim that a plan was executed. Return a clearly invalid/unavailable state if inputs or solver results are stale. Keep internal access scoped; never expose home occupancy or temperatures publicly.

### Executor state machine

| Mode | Climate actuator behavior |
|---|---|
| **Disabled / manual override** | Do not issue optimization writes; hand control back to the user/pump according to an explicit ownership policy; cancel only owned timers |
| **Legacy** | Existing AppDaemon price schedule remains the authoritative writer |
| **Shadow** | Read and compare proposed plans, but **never** issue plan-derived writes |
| **Active (future gate)** | Execute only the current valid, acknowledged intent while supervising temperatures, hardware mode and minimum run times |
| **Degraded / fault** | Expire optimization authority; transition through a documented safe controller/hardware fallback rather than assuming the last HA helper state is safe |

Only one climate writer may be authoritative at a time. When replacing independent legacy climate/DHW timers, transition both to a single coordinated executor or retain DHW hardware ownership explicitly—never run overlapping force-DHW schedules from two controllers. From C5, AppDaemon may call HA services, but must confirm the *reported* HeishaMon operating mode; `input_select` reflecting a requested option is insufficient. Every C5 actuation needs deadline, bounded retries, idempotent intent handling and mismatch alerting. These requirements apply to the C5 executor, not to C0, which keeps the proven legacy command path.

Do **not** equate `disabled`, transport loss or optimizer failure with `off`. Document how the pump's native thermostats/frost protection continue to operate, how a stale heating request is revoked, and what manual takeover does during a restart. The proposed climate plan consumer is a separately reviewed homelab PR; no runtime control changes in this planning PR.

### Packaging/deployment specifics

- The homelab chart packages `appdaemon/apps/*` directly into a ConfigMap and copies it to an `emptyDir` at startup. If AppDaemon code later becomes a Python package, update chart globs/mount paths and import tests; do not assume nested files appear automatically.
- Existing AppDaemon is `replicas: 1`, but a single replica is **not** a reliable single-writer lock across restarts/reconciliations. Reconcile actual pump state and plan generation on startup.
- Keep the read-only HA credential and `ha_guard.py` exactly restricted in the battery-optimizer process. The future AppDaemon plan consumer uses its existing appropriately scoped write-capable integration, not the battery optimizer's token.
- Run climate solver work off the shared event loop; bound runtime and queueing. Do not let climate solves starve battery slot closing, price archiving, API liveness, or existing battery MPC work. Benchmark CPU/memory on the current Deployment before raising limits. If demand warrants it, separate worker pools inside the process, not new pods by default.
- Keep GitOps ownership of image digest, explicit enable flags, secrets, monitoring and rollbacks in `pando85/homelab`. No auto-promotion from shadow to active on image or ConfigMap update.

### Packaging, release and rename migration

**Near-term: retain the current release name and deploy the same image for its two existing workloads.** Do not introduce `climate-optimizer` or an additional image build. The package contains domain-specific `battery` and `climate` planning modules, with separate CLI commands but a common dependency set and OCI release.

| Kubernetes resource | Container image | Process | Operational separation |
|---|---|---|---|
| Existing `battery-optimizer` Deployment | Existing pinned `ghcr.io/pando85/battery-optimizer` OCI image | `battery-optimizer serve`, eventually scheduling both battery and climate planners in-process | Read-only HA token; independent planner snapshots, readiness, solve budgets and metrics |
| Existing `battery-optimizer-pipeline` CronJob | **Same image and pinned digest** | Existing `extract`, `validate`, `build`; future offline `climate-evaluate`/training mode if justified | Short-lived batch process; dataset/PVC and historical API access only |
| Existing AppDaemon Deployment | Its **existing separate AppDaemon image** | Existing `climate_control` / `dhw_control` code; later a thin `ClimatePlan` reader/executor | Sole current HA/HeishaMon climate writer, with manual override and rollback |

Planned CLI shape after the optional project-wide rename (illustrative, **not implemented**):

~~~console
home-energy-optimizer serve
home-energy-optimizer extract
home-energy-optimizer validate
home-energy-optimizer build
home-energy-optimizer benchmark
home-energy-optimizer climate-evaluate   # proposed
home-energy-optimizer climate-actuator   # future, separately authorized
~~~

**Optional replacement for AppDaemon:** package `climate-actuator` in the same OCI artifact but run it as a **separate Kubernetes Deployment/process**, with its own service account, restricted HA write credential, supervised reconciliation and exclusive command ownership. The read-only `serve` process must still reject all actuation calls. Do not run the actuator as a side effect of starting `serve`, embed its writable HA client in the planner, or deploy it until the C5/retirement-specific gates have been reviewed. When enabled, the intent is to replace AppDaemon's actuation Deployment, **not** to add a permanently competing writer. The separate battery/Omnibattery controller remains independently owned.

**Staged naming/operational migration** (a separate follow-up PR or PR series; do not combine with climate runtime development):

1. **Repository URL (done):** GitHub `pando85/battery-optimizer` was renamed to `pando85/home-energy-optimizer`. Consumers, GitHub Actions, release tooling, issue/PR links and configured source URLs were audited in #35 and the homelab docs; the image workflow names `ghcr.io/pando85/battery-optimizer` explicitly instead of deriving it from the repository, so the image keeps its name until step 3. Do not rely solely on GitHub's old-URL redirects.
2. **Python and CLI compatibility:** Decide an import/entrypoint compatibility period before switching `battery_optimizer` to `home_energy_optimizer`. Preserve running scripts, packaging metadata and existing command usage until migrated in tested increments.
3. **OCI identity and supply chain:** Publish and scan the new GHCR image name separately, ensure actual manifest availability and signed/provenance requirements, then update homelab's image **by digest**. GHCR names are not guaranteed to redirect after a repository rename. Do not remove old image tags before pin consumers move.
4. **GitOps rollout:** Update the optimizer Deployment and pipeline CronJob to the **same verified new digest**, keeping their commands, PVC/mounts, ServiceMonitor/PrometheusRule, secrets, service names and ArgoCD identities stable unless a later explicit migration demands a change.
5. **Persistent dataset/API compatibility:** Preserve content-addressed manifests, `dataset_id`, `code_version`, archived source hashes, existing `/plan` and `/status` contracts and historical report reproducibility. Record any migration alias or metadata format version rather than rewriting historical artifacts.
6. **Rollback:** Keep a tested reference to the prior image digest, command and GitOps configuration. Repository rename or branding must **not** toggle battery or climate actuation.

The target is **one image build and release pipeline**; existing battery planner Deployment and pipeline CronJob remain separate processes/workloads, and AppDaemon remains a separately built image until a distinct actuator-replacement decision. Creating the actuator entrypoint does not authorize running it, and this plan intentionally avoids introducing a third planner Deployment.

## 5. Climate ↔ battery integration

The existing `structured_forecast.py` (LF-1) **already** replays the AppDaemon policy and estimates HVAC electricity offline. Do not duplicate or delete it when adding climate MPC. Make load forecast provenance explicit:

1. Legacy periods: forecast/replay the versioned cheap-hours and DHW rules as currently implemented.
2. Shadow period: record the candidate `ClimatePlan` separately, alongside **actual legacy** execution. Do not label a shadow plan as realized electricity demand.
3. Active climate period (future): use the published **committed/executable** climate schedule where known, plus residual household demand; account for plant deviations separately.
4. Validate that `P_house = P_nonHVAC + P_climate + P_DHW` is a mutually exclusive decomposition: if the forecast baseline already contains heat-pump load, **subtract/re-fit** it before adding a new climate load schedule. Never add predicted HVAC load twice.
5. Keep battery optimization separately constrained by SOC, grid import, conversion losses and Omnibattery's eventual physical safety rules. An inactive/simulated battery cannot make real HVAC electricity free.

Forecast interfaces should carry UTC slot IDs, kWh, quality and model/plan version, with a clear information cutoff. No cyclic re-optimization of climate and battery without a later architecture decision and measured benefit.

## 6. Iteration sequence (separately reviewable PRs)

| Stage | Repository | Deliverable | Exit gate |
|---|---|---|---|
| **C0 — Correctness audit and stabilization** | homelab | The agreed scope below (disable restores the heat mode, HA watchdog, retry re-check, full heat-mode map, numeric sensors); preserve existing user-facing toggles | Tests cover disable, the watchdog (stale heartbeat while enabled and `DHW`), the retry loop re-checking the toggle and the nine-option mode map |
| **C1a — Offline extraction and climate feature set** | battery-optimizer + homelab configuration | Map verified room, tank, compressor, power and weather data (mode-aware, including cooling power/energy) into extraction and a **separate climate feature set**; quality report; homelab numeric template sensors for operating mode, defrost and 3-way valve (C0 homelab work); no control writes | Enough measured history and documented gaps to fit a model without inventing data; `dataset_id` and existing feature-set ids unchanged; battery baseline regression-free |
| **C2 — Thermal identification, COP and dual-objective gate** | battery-optimizer | Pure models (first-order and two-state slab+air), fitting, safe parameter envelopes, recorded versions and chronological validation; COP fitted on electricity; policy simulator and the go/no-go gate ([design](#c2-design-and-predeclared-gate)); DHW stays fixed | Model acceptance criteria met on the held-out season (otherwise UNDETERMINED, treated as no-go); bad sensors trigger model fallback; gate verdict recorded with its preregistration |
| **C2-R — Recovery after UNDETERMINED** | optimizer (offline) | [Independent diagnostic-first model recovery](climate-c2-recovery.md): sensor and heat attribution, observability, minimal/empirical alternatives, and prospective validation | New preregistered observation/action/policy gates; does **not** amend or override the failed C2 protocol; no actuation |
| **C1b — Live climate observer** (parked, ADR-0006) | battery-optimizer | Live climate observer in `serve` behind a feature flag, read-only; blocked by C2 no-go until a *new* accepted recovery route and separate promotion decision | Existing battery runtime unaffected; freshness and quality metrics |
| **C3 — Climate shadow planner** (obsolete as a cost MPC, ADR-0006) | battery-optimizer | **Blocked:** historical C2 no-go. A new accepted C2-R policy gate may separately authorize an experimental shadow planner; existing C3 must not use failed A/B as accepted defaults | Reproducible new acceptance, deterministic bounded plans and no interference with battery runtime |
| **C4 — Evaluation and plan handoff** (obsolete as a cost comparison, ADR-0006) | battery-optimizer, then homelab | Compare cheap-hours, comfort-rule and MPC using identical historical decision-time inputs; add AppDaemon **shadow** plan reader and telemetry | Comfort, model-cost and cycling comparisons recorded; shadow reads cannot actuate; tested expires/parse errors |
| **C5 — Gated heating control** | homelab (plus explicit optimizer flag/config) | Single writer for heat and DHW; controlled schedule activation with acknowledgement and safety fallback | Manual supervised pilot, recovery and hardware-state checks, acceptance of temperature and cycling behavior; easy switch back to legacy |
| **C6 — Demand forecast exchange** (reduced to the optional HP-plan entity, ADR-0006) | battery-optimizer | Expose committed climate electric load to the battery forecaster; validate residual and historical reconciliation | No double counting; battery shadow savings/forecast regret improve on matched windows; no battery writes |
| **C7 — Optional evolution** | both, new ADR as needed | Per-room refinements beyond C2, DHW optimization refinements, [flow-temperature calibration and optimization (C7-F)](climate-flow-calibration-optimization.md), setpoint-offset lever (needs a writer), cooling (needs humidity sources and a dew-point constraint), ~~joint MPC~~ (dropped by ADR-0006), AppDaemon retirement | Separate evidence and safety/comfort decision per addition; C7-F has its own gate and must not invalidate the predeclared C2 heat-v1 gate |

**Flow-temperature extension (C7-F):** [Dedicated flow-temperature field-trial plan](climate-flow-calibration-optimization.md) is now control-surface verification (F0), passive coverage (F1) and a model-free joint field trial (F2) of permitted heating hours and water temperature against current operation, with its own preregistered per-arm GO/NO-GO/UNDETERMINED verdicts. None depends on an accepted C2 model. Guarded flow actuation (F5) and maintenance (F6) are parked until the trial shows a static configuration leaves substantial value. C7-F is optional and must neither delay nor rewrite C2/C3/C5 acceptance or reuse the C2 heat-v1 gate.

**Ordering:** C0 can begin independently in `homelab`; C1a and C2 came first and ran while legacy heating stayed active. C2 ended UNDETERMINED after its one predeclared iteration, which counts as no-go (see the [outcome](#c2-model-iteration-the-one-predeclared-iteration-config-version-2)): C1b and C3–C5 do not start as specified, and the C2 gate and its preregistration stay as committed. If a permission policy is ever accepted later, C3–C4 must still precede any C5 actuation. C6 can start read-only before C5 but must distinguish hypothetical shadow schedules from committed ones. The battery Phase D commissioning/authorization is **independent** of C5. No timeline, energy savings percentage or thermal parameter is assumed.

**Next steps after the C2 outcome:**

1. **C2-R recovery:** [R0–R5 diagnostic-first work](climate-c2-recovery.md) starts with a reproducible failure ledger, checks the room/valve/heat/electricity observability and tests minimal physical and regularized empirical models *without assuming either A or B is valid*. Keep all C2 v2 failure results and model parameters unchanged.
2. **Model-free observational baseline (`min_hours`).** Qualitative result, observational and confounded, so an association rather than an intervention effect: 15 h against 10 h cut the time below 22 °C sharply, at a measurable electricity and cost premium. Both the cost and the comfort gain sat at 6–12 °C outdoor; at 12 °C and above the extra hours cost nothing.
3. **C7-F F0/F1:** the control-surface audit (starting from the Prometheus finding that the pump runs a fixed direct water target with an inactive curve) and the coverage report can start now.
4. **Joint field trial (C7-F F2): agreed in principle.** A model-free, preregistered trial of permitted heating hours and water temperature (fixed target vs native curve) against current operation, run this winter. Its arms and preregistration are still to be agreed with the owner, and the small homelab change (manual or calendar) is agreed separately, before any homelab change. **Proposed, not decided.** Its varied data also feeds later model identification.
5. **Separate gates, not an automatic retry:** no tuning of the original v2 thresholds or reuse of S3 as an untouched confirmation. A new predeclared evaluation must rely on genuinely held-out/prospective data and be accepted before C3; C4/C5 and physical authorization remain separate.
6. **Whole-system replay (offline, read-only):** the [system replay](system-replay.md) (`hack/system_sim.py`) transfers the 15 h days empirically, with no thermal model, to compare the legacy heat-pump control with an efficiency-first one, each with and without the simulated battery. The battery's value was nearly independent of the heat-pump regime, and the comfort side of the efficiency-first regime remains an association that the C7-F field trial must confirm.

### C0 scope (agreed after the owner's review)

**Agreed C0 scope (homelab):**

- Disabling `input_boolean.appdaemon_climate_enable` cancels the timers and restores the heat mode (`input_select.appdaemon_climate_heat_mode`) with a notification. AppDaemon shutdown or reload never changes the mode.
- An HA watchdog automation restores the heat mode when the AppDaemon heartbeat (`sensor.last_appdaemon_connection`) is stale for 15 minutes while climate control is enabled and the pump reports `DHW`.
- The toggle is the manual override.
- The price-retry loop re-checks the toggle before re-registering.
- The "Set heatmode" automation maps all nine options to `SetOperationMode` 0-8.
- Numeric template sensors for the operating-mode code, defrost and the 3-way valve, so Prometheus keeps their history.

**Reviewed and not changed, with reasons:**

| Item | Decision |
|---|---|
| Mode command path and acknowledgement | Proven reliable in years of operation; owner decision. Confirming the reported mode with a deadline is a C5 executor requirement, not C0 |
| DST timers | AppDaemon 4.5.13 resolves non-existent or repeated wall-clock times to a fixed instant. Worst case is a one-hour shift or one extra heating hour twice a year; heating is never lost |
| Stale PVPC at the 00:00:30/00:00:40 runs | The `pvpc_updated` integration rewrites the attributes within 0.5 s of midnight from cached next-day prices (verified on 75/75 midnights); missing data makes the sensor unavailable rather than stale |
| DHW app (re-force inside the chosen hour, overlap with heating windows, no notification without prices) | By design or harmless |
| Timer cancellation through AppDaemon internals | Left as is; owner decision |

Comfort awareness (a temperature guard) is deferred to C3.

Any change to which hours the legacy policy selects is a versioned policy change and needs a matching `structured_forecast.py` replay update. Every AppDaemon ConfigMap change restarts AppDaemon (reloader), so C0 merges are actuation events until restart handling is fixed.

### C2 design and predeclared gate

Agreed with the owner on 2026-10-30. Everything is expressed in %, Kh (degree-hours) and starts, so no private money threshold is committed.

**Owner decisions**

- **Dual objective:** the same money for more comfort, or the same comfort for about 10% less money. Either wins.
- **Comfort floor:** "no worse than legacy"; 22 °C is the target. A saving bought with a colder house does not count.
- **DHW fixed:** it keeps its historical schedule in every policy. C2 optimizes space heating only.
- **Setpoint-offset lever:** reported, not gated. It feeds C7.

**Data facts behind the design (qualitative on purpose)**

- Legacy cheap-hours already buys heating well below the day-mean price, and a crude same-day perfect-packing ceiling leaves little cost headroom. Comfort at equal cost is therefore a first-class outcome.
- Legacy already runs below 22 °C in cold weather, with thermostat setpoints held above the target.
- Heat is delivered in only part of the permitted hours: within permission the pump's own control sets the heat, so it is modelled as a response to `u`.
- `min_hours` took four values in season S3 (6, 8, 10, 15): a natural experiment on the lever, used for validation.
- The forecast archive starts on 2026-10-25 and there is no irradiance sensor: weather is persistence and solar is a clear-sky proxy.

#### Models

- **`T_air`:** mean of the comfort rooms with per-room offsets by local hour (training medians). Office A is excluded; per-room dynamics are C7.
- **Inputs per 15-minute slot:** `Q_heat` (power-integrated thermal output; the energy counter is the daily cross-check, flow x dT a diagnostic only), `T_out`, `S` (clear-sky proxy: sin+ of the solar elevation, site coordinates from private config in the data dir) and the internal gain `q0` (servers power becomes an input only if its variance is material).
- **Model A, first order:** `C dT/dt = Q + UA (T_out - T) + q0 + g_s S`.
- **Model B, slab + air:** `C_a dT_a = k (T_s - T_a) + UA (T_out - T_a) + q0 + g_s S`, `C_s dT_s = Q - k (T_s - T_a)`. The return temperature during steady circulation (flow above about 5 L/min, at least 10 min into a run, no DHW) is a second, noisy slab measurement, `y2 = T_s + b_ret`, which gives B its identifiability.
- **Discretization and fit:** exact ZOH (`scipy.linalg.expm`, dt = 900 s); bounded simulation-error least squares over 48 h windows from local midnight (B gets a per-window initial `T_s` as a nuisance parameter); a steady-state Kalman observer supplies the state at forecast origins.
- **Parameter envelope:** tau = C/UA in [10, 500] h, UA in [0.03, 0.5] kW/K, `C_s` in [2, 60] kWh/K, k in [0.1, 3] kW/K, `q0` in [0, 3] kW. A parameter on a bound means the fit failed.
- **Training filters:** drop slots with unknown-quality inputs, window-open events (a room falls more than 0.5 K in 15 minutes), slots where at least two comfort rooms are at or above setpoint (valves probably shut), the February 2026 thermostat outage and cooling slots.
- **Heat response to `u`** (`plant.py`): B: `Q = u (1 - dhw) clip(H_e (T_flow_target - T_s), 0, Q_max(T_out))`, with the measured flow target as the input: the PR 1 audit found no weather-compensated heating curve (the flow target is a constant setting per season, changed by hand between and within seasons). A: `Q = u (1 - dhw) (c0 + c1 (T_ref - T_out) + c2 first_hour_of_block)`. Both are capped by the valves: `Q x (phi + (1 - phi) open)`, `open = T_air < sp - 0.25`, where `phi` is the always-open share (bathrooms, corridor), default 0.3 until floor areas are supplied. Historical `u` comes from `structured_forecast.replay_mask_for_day`; days with the climate rule disabled are excluded.
- **Dependencies:** numpy, scipy and pyarrow only; no new dependency, no pandas.

#### Validation and model acceptance

- **Seasons:** S1 = 2023-11-08..2024-04-30 (train only); S2 = 2024-10..2025-04 (train, secondary test); S3 = 2025-10..2026-04 (held-out test and primary gate period); 2026-10 onward is untouched and reserved for C4. Fit on October-April; the gate and headline metrics use November-March.
- **Folds:** fit S1, test S2; fit S1+S2, test S3 (the model iteration below fits S2 only).
- **Forecasts:** origins at 00, 06, 12 and 18 local, observer state, 24 h open loop on measured `T_out`. RMSE and bias at 1, 6, 12 and 24 h in two modes: Q-driven (measured `Q`) and u-driven (replayed `u` through the response model), against persistence and diurnal persistence.
- **Acceptance** (committed in `config/climate.yaml` before any results):
  - Q-driven RMSE on S3 at most 0.30 K at 6 h, 0.40 K at 12 h, 0.50 K at 24 h; |bias| at 24 h at most 0.15 K; at least 15% better than the better baseline at 12 h and 24 h.
  - u-driven RMSE at 24 h at most 0.60 K; daily thermal-energy MAPE at most 15% with season bias at most 5%.
  - u-driven bias within each S3 `min_hours` regime at most 10% (the lever-response test).
  - Legacy replay fidelity on S3: simulated legacy electricity and cost within 5% of realized; Kh below 22 °C within 25% or 10 Kh.
  - Parameters inside the envelope and each within 30% between the folds.
- **Selection:** use B only if it beats A by at least 10% RMSE at 12 h and 24 h. If neither model is accepted the verdict is UNDETERMINED and counts as no-go; one predeclared model iteration is allowed.

#### COP model

- Fit electricity, not COP, on hourly heat-mode hours without DHW: `E_el = Q (a + b (T_flow - T_out) + c max(0, 5 - T_out)) + d starts + e hours_on`. WLS weighted by `Q`; exclude `Q` < 0.1 kWh and implied COP outside [1, 9].
- Baseline: a COP table binned by `T_out` in 2 K steps; if the regression loses to the table, use the table.
- Validation on S3 given measured `Q`: daily electricity MAPE at most 8%, season error at most 3%.

#### Policies and simulator

Every policy, legacy included, runs through the same model (thermal, response, valve cap, COP, starts). Episodes are maximal runs of eligible days from the same observed state; stored heat left at the end is valued at legacy's mean cost per thermal kWh, by the same rule for all policies. Cost is the PVPC price times modelled heating electricity, pre-tax, on the HeishaMon basis, with a sensitivity scaled by the channel-2 ratio. The battery is ignored.

1. **Legacy:** replay `climate_control` as of each date (rule version, enable flag, `min_hours`).
2. **Temperature-aware heuristic:** hourly decisions at :00, at least 1 h on and 1 h off, horizon to the end of published prices (at most 24 h), `T_out` by persistence, effective cost = price / COP(`T_out`). Start from `u` = 0; while the predicted minimum `T_air` before the end of the horizon is below 22 + `m`, switch on the cheapest remaining hour before the first violation; never heat when the prediction is above 28; execute the first hour and repeat. The single tunable `m` is fixed on S2 with the fold-1 model, giving two variants: `m_cost` (smallest `m` whose S2 comfort is no worse than legacy) and `m_comfort` (largest `m` whose S2 cost is at most legacy). Both are frozen in the preregistration before S3 is run.
3. **Perfect foresight (diagnostic only):** a daily MILP (`scipy.optimize.milp`) with realized prices and `T_out`, end state at least legacy's, `Q` continuous in [0, `u` `Q_max`], at most 4 blocks per day. If its saving is below the cost threshold and its comfort gain below the comfort threshold, no permission policy can pass: a clean stop.

Levers: **permission** is gated. **Permission plus setpoint offset** (setpoint min(28, sp + 2) in chosen hours, perfect foresight without the valve cap) is reported only.

#### Gate (heat-v1, permission lever, S3, primary model)

GO if all hard rows hold and at least one of conditions C or K holds.

- **Hard rows:** 0 Kh above 28 °C; modelled mean starts per day at most 1.10 x simulated legacy, no modelled day above 30 starts and at most 4 permission blocks per day; the planner/plant mismatch run keeps the sign of the passing condition; the model is accepted.
- **C, cost (`m_cost`):** heating electricity cost at least 10% below simulated legacy; bootstrap 95% lower bound of the saving above 0 and the saving larger than the legacy replay cost error; comfort no worse than legacy, i.e. Kh below 22 °C per comfort room at most legacy + 5 Kh per season and worst excursion at least min(legacy worst, 21.5 °C).
- **K, comfort at the same money (`m_comfort`):** Kh below 22 °C summed over the comfort rooms at least 25% below simulated legacy; bootstrap 95% lower bound of the reduction above 0 and the reduction larger than the legacy replay Kh error; cost no higher than legacy, i.e. point delta at most 0 and bootstrap 95% upper bound at most +3%.

Otherwise NO-GO, or UNDETERMINED if the model is not accepted. C1b, C3, C4 and C5 start only on GO.

**Model-error bounds:** paired simulated differences; a saving or comfort gain only counts above the legacy replay error; the `min_hours` regime test; the mismatch run (plan with one accepted model or fold, simulate with the other); an out-of-distribution share report with a warning above 10%.

**Statistics:** S3 November-March is primary and S2 is reported. The same day mask for every policy, paired daily differences and a 7-day block bootstrap (`forecast_eval.block_bootstrap`) with a preregistered seed. The saving is decomposed into a volume effect and timing plus COP.

#### C2 model iteration (the one predeclared iteration, config version 2)

The first `climate-fit` run was UNDETERMINED on S3. Diagnosis: S1 is a different plant regime (flow
water about 34 °C against 26–28 °C later, comfort-room valves shut about half the heating time while
heat went to the always-open rooms and the slab, apparent UA about three times S2's and S3's; nothing
physical changed in the house); heat measurement is fine; the fit did not score valve-shut and
rule-disabled slots although their heat drove the simulation, which inflated UA; the fold-stability
ratio diverges at a legitimate zero bound; long compressor runs left `T_out` unknown and the
simulable S3 days biased to mild weather; the `T_out`-binned COP table cannot follow flow changes.
Agreed with the owner, all written into `config/climate.yaml` and committed before the rerun:

- **Training excludes S1** (`seasons.training_excluded`): one fold, fit S2, test S3. The outdoor
  offset model keeps S1 (it is about the sensor, not the plant).
- **Causal weekly refit** (`model.adaptive`): from the first S3 gate-month day, every 7 days, the
  thermal parameters are refitted on the trailing 28 days before the origin, shrunk toward the S2
  fit (penalty `lambda * sum(((p - p_S2) / box width)^2)`); response, valve cap, offsets and starts
  stay the S2 fit. `lambda` per model is chosen on S2 only (prior on S2 up to 2024-12-31, weekly
  refits over the rest of the S2 gate months, lowest mean Q-driven RMSE at 12 and 24 h among values
  whose refits meet the stability criterion) and committed separately before any S3 run.
- **Fit scoring:** every slot with a valid air temperature is scored; only unknown quality, open
  window, thermostat outage and cooling mark bad data. The validation scoring is unchanged.
- **`T_out`:** `t_out_debiased` stays the source; slots where it is unknown take `t_out_fused`
  (`signals.t_out_fill_unknown_from_fused`), recorded per slot; the model id hashes the station
  manifest.
- **COP:** the model is a table binned by lift `T_flow - T_out` (2 K, same minimum count and
  nearest-bin rule), trained on S2 and validated on S3 against the unchanged 8 % / 3 % limits; the
  regression and the `T_out` table are reported. With it the legacy-replay electricity and cost
  criteria are computed (simulated heat priced through the lift table at the flow target).
- **Acceptance protocol changes (only these two):** fold stability becomes "at least 95 % of the
  weekly refits have every parameter inside the envelope and none on a bound, `g_s` at 0 allowed";
  the baseline gain at 12 and 24 h becomes "not worse" (at least 0 %). Every other threshold is
  unchanged; the COP limits now sit in the acceptance table. No further iteration follows: a
  failure is UNDETERMINED.

**Outcome (2026-10-30, one run at the predeclaration commit): UNDETERMINED**, so C2 counts as no-go
and the iteration budget is spent. Neither model passed every criterion. Both failed the 6 h
Q-driven RMSE, the u-driven daily-energy MAPE and season bias, the `min_hours` 10 regime bias and the
COP season error. A also failed the 12 h RMSE, the 24 h bias, the 15 h regime bias and the whole
legacy replay. B passed the legacy replay (electricity, cost and Kh) but failed the parameter
criteria: its S2 slab capacity sits on its lower bound and most weekly refits hit a bound. The
figures stay in the private model directory.

#### Code layout and PR order

- **Package:** new `src/battery_optimizer/climate/` with `data.py`, `cop.py`, `thermal.py`, `plant.py`, `policies.py` and `gate.py`; data audit in `hack/climate_audit.py`.
- **Config:** committed `config/climate.yaml` holds the heat-v1 comfort band (22-28 °C, rooms), filters, parameter envelope, acceptance criteria and gate thresholds. Site coordinates and `phi` live in a private YAML in the data dir, whose hash is recorded.
- **CLI:** `climate-fit` writes `<data_dir>/analysis/v1/climate-models/<model_id>/` (`model.json`, `validation.json`, `validation.md`; `model_id` = sha256 of feature-set id, dataset id, windows, code version and config hash). `climate-gate` writes `<dataset>/climate-gate/<name>-<ts>/` (`preregistration.json` before any policy runs, `daily.parquet`, `summary.json`, `summary.md`) with the verdict.
- **Lineage:** C2 reads only; `dataset_id` and existing feature-set ids do not change.

| PR | Deliverable |
|---|---|
| 0 (operational, not a PR) | After C1a merges, extract and build the climate set for 2023-11-07..2026-05-01 |
| 1 | `data.py`, `config/climate.yaml` and the audit: thermostat resolution, gaps, counter quantization, the return-temperature slab proxy, the heating curve, the legacy-mask hit rate, the channel-2 ratio and whether the outdoor sensor sees sun |
| 2 | `cop.py` |
| 3 | `thermal.py`, `plant.py` and `climate-fit`, with synthetic-house identifiability tests |
| 4 | `policies.py`: simulator, heuristic, perfect-foresight MILP, setpoint variant and mismatch run |
| 5 | `gate.py`, `climate-gate` and docs, reusing the `forecast_eval` preregistration and bootstrap |

### Recommended first implementation PRs

- **homelab — C0:** the agreed scope above (disable restores the heat mode, HA watchdog, retry re-check, nine-option mode map, numeric template sensors). Preserve current schedules by default.
- **battery-optimizer — C1a:** offline extraction and a separate climate feature set from the existing Prometheus archive; model input audits for room, tank and HVAC electric/thermal power.
- **battery-optimizer — C2:** offline first-order and two-state thermal models, COP fitted on electricity, test fixtures and the dual-objective gate, in the five PRs above; no runtime configuration or actuator.
- **optimizer — C2-R (now):** [diagnostics and independent model validation](climate-c2-recovery.md), not a repeat of the exhausted C2 iteration; preserve original A/B no-go and use a new prospective confirmation dataset.
- **battery-optimizer — C3 (only after a new accepted gate; not started, C2 was UNDETERMINED):** opt-in shadow planning and `GET /climate/plan`; prove battery shadow behavior unaffected.
- **homelab — C4/C5 (same condition):** explicit read-only plan consumption then a separately approved, reversible controlled heating rollout.
- **now, see the next steps in the ordering above:** the `min_hours` analysis, then C7-F F0/F1 and the joint field trial ([flow-temperature field-trial plan](climate-flow-calibration-optimization.md)), keeping its preregistered verdicts separate from the predeclared C2 permission-only decision. The trial is agreed in principle; its arms and preregistration are still open.

### Decoupled objective and obsolete items (ADR-0006)

[ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md) (2026-10-30) replaces the price-driven objective. The heat-pump controller **minimises electricity subject to a comfort schedule**; the battery optimiser does the price arbitrage and treats the heat pump as exogenous load.

~~~text
min  Σ_t predicted_electric_kWh(u)[t]
s.t. hall/kitchen >= 23.5 °C from 12:00 on weekdays and from 09:00 at weekends   (minimum, not target)
     every room   >= 21 °C at all times
     optimum start from learned warm-up rates; low water temperature, native heating curve, long gentle runs
     compressor, DHW, hygiene and manufacturer constraints unchanged
~~~

Price enters through **boost** (at most 0.04 €/kWh for 3 h or more, tunable): battery first, then a heat soak (room ceiling about 25.5 °C, water about 30 to 32 °C), then DHW. Boost sits after normal mode and battery arbitrage. All parameters are Home Assistant helpers.

**Amended 2026-10-31** ([ADR-0006 amendment](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md#amendment-2026-10-31), [design](comfort-controller-design.md)): DHW stays price-optimised (cheapest 2 h window ranked by price / COP) and DHW, boost included, stays at most 50 °C, so the 3 kW heater never starts; there are three price reactors (battery optimiser, DHW scheduler, boost).

**Obsolete or parked by ADR-0006:**

| Item | Status |
|---|---|
| Price-based climate objective (`min Σ price × kWh`, section 3.3) | Replaced by the objective above |
| C1b live observer, C3 shadow planner, C4 cost comparison, `GET /climate/plan` | Parked (C3 and C4 as a cost MPC are obsolete); the offline `climate/` package stays for warm-up-rate learning |
| C6 demand forecast exchange | Reduced to the optional `sensor.hp_planned_energy` entity (specified in the ADR, built only if the HP forecast share is material) |
| C7 joint MPC | Dropped |
| Cheapest-hours preheat arm of the C7-F trial | Dropped (boost is rare in winter) |
| Legacy cheapest-hours rule | Retired when the comfort schedule goes live (DHW price scheduling stays, amended 2026-10-31) |
| C2-R G-R3 "report cost and electricity" | Electricity at comfort becomes the primary metric, cost secondary |

**Ordered work list** (smallest change that works; "needs tests" marks behavioural code changes):

*Now (this winter; docs and homelab, no battery code)*

1. ADR-0006 and the plan updates (this change).
2. Record the HP switch date(s) and the trial block calendar in `docs/planning/`, not `config/tariff.yaml`. Exclude the first 28 days after the switch and alternating-arm weeks from gate-C and forecast evaluations.
3. Homelab: retire the legacy cheapest-hours space-heating rule when the comfort controller goes live. **DHW keeps price scheduling** (one DHW writer, cheapest 2 h window by price / COP). The heater question is answered: the booster is the 3 kW heater, so the tank target is capped at 50 °C and AppDaemon never starts the heater.
4. Import-limit basis: the owner confirmed an **instantaneous** trip on 2026-10-31 (no distributor document yet); a written confirmation from the contract or distributor is still welcome. The actuator ADR must require real-time regulation against measured load with a margin.
5. Homelab: comfort controller (comfort schedule, optimum start, software water-temperature curve, DHW rule, helpers) as specified in [comfort-controller-design](comfort-controller-design.md). **Merged and tested only once the house battery is working.** Evaluated as the before/after comparison of C7-F F2, with the criteria preregistered before go-live.

*Track B: shadow thermal-model MPC (this repository, read-only; [design section 13](comfort-controller-design.md#13-track-b-shadow-thermal-model-mpc))*

- **B0:** diagnostics, [C2 recovery](climate-c2-recovery.md) R0 to R2 and the warm-up rates (in progress; feeds the optimum-start table).
- **B1:** offline MPC replay (minimum kWh subject to the comfort schedule, price-blind).
- **B2:** shadow runner in the service (plan and prediction metrics only; no writes, no new credentials).
- **B3:** preregistered comparison with the controller after winter.

*Before battery go-live (gates C and D)*

6. Gross-load reconstruction in `build` (`raw - charge + discharge`; sign corrected 2026-11-03, register and extract the battery counters, refuse the no-battery assumption after commissioning); needs tests. **Done in code (2026-10-31)**: configuration in `config/service.yaml` (`battery.commissioned_at`, `entities.battery_*_energy`, set at commissioning 2026-11-03), [data-pipeline](../components/data-pipeline.md). Install day: set both, then extract the counters.
7. Disable OmniBattery's native economic features.
8. Charge adapter with a grid target capped below the import limit minus a margin, with a charge-rate bound; test the target and cap interaction at commissioning.
9. Boost interlock in AppDaemon (battery state of charge, then soak, then DHW), reading the battery state read-only.
10. Throughput cost: **done 2026-10-31** (0.01 EUR/kWh, the knee of the rolling-horizon sweep; decision, table and warranty terms in [battery-model](../components/battery-model.md#wear-cost-decision-2026-10-31); re-check on measured data after commissioning).
11. Actuator ADR (separate writer process, scoped token, shadow-rule amendment in `AGENTS.md`), then the failure tests of gate D.
12. Gate C on two weeks of real battery telemetry in the new regime.

*Later (evidence-gated)*

13. HP decomposition headroom on 6 to 8 weeks of new-regime data; build the HP-plan entity and the battery-side forecaster only if it is material, otherwise consider the day-type forecast.
14. Confirm the replay findings with the ordinary benchmark on measured new-regime load.
15. Measured round-trip efficiency curve, idle draw and wear recalibration (TODO-7.1, TODO-7.4).
16. Staged rename, only after go-live is stable and never in the same change as an authority change.
17. Retire the forward-looking resume path of the structured forecaster; keep its code for legacy-regime evaluation.

Not needed: a shared or joint MPC, a climate planner in `serve`, a new endpoint or broker for the exchange, a boost field in the dispatch contract.

## 7. Validation, metrics and non-negotiable safety

### Evaluation rules

- **Causality:** replay prices as published at the decision, use weather *forecast issue time* (not realized future weather), measured building/tank state as-of the slot and chronological train/test splits.
- **Baselines:** existing AppDaemon policy; same policy with temperature guard; first-order heuristic; MPC; perfect foresight only as an explicitly labeled diagnostic upper bound.
- **Costs:** PVPC marginal purchase prices with verified tariff/tax assumptions, electricity measured by mode, COP and transition/cycling cost assumptions; separate fixed charges. No generated savings figure is accepted as a bill measurement.
- **Comfort:** record the comfort band used; report degree-hours below minimum/above maximum, absolute worst excursion, per-room disagreement, recovery delay and expected-versus-realized temperature trajectory.
- **Machine:** starts/day, short cycles, compressor runtime, high-flow operation, defrost effects, forced DHW cycles and unsuccessful acknowledgements.
- **Model:** 15/30/60-minute and full-horizon temperature MAE/bias, DHW temperature trajectory, COP error, confidence, fit drift and invalid-sensor counts.
- **Operational:** valid-plan coverage, stale input time, solution feasibility/failures/latency, cancelled/superseded plans, applied-vs-observed mode, fallback frequency, service CPU/memory, impact on battery MPC and price/weather archival.
- **Promotion:** predeclare comfort bounds, minimum on/off constraints, accepted model errors, runtime budgets, rollback thresholds and economic improvement criteria based on observed baseline performance; do not invent thresholds now. Regressions in comfort or plant safety override nominal cost gains.

### Mandatory failure-scenario tests

| Scenario | Required expectation |
|---|---|
| Europe/Madrid spring-forward and fall-back, including duplicated `02h_d` | UTC-aligned 92/100 quarter-hour slots with correct tariff association |
| Missing, stale, negative and partly published PVPC prices | Correct availability/error attribution, no random coercion and explicit policy for estimated tail |
| Stale room/outdoor/tank sensors; open windows; contradictory zone temperatures | No false thermal learning; conservative limits or no optimizer actuation |
| Solver timeout, CPU saturation, failed persistence, restart or overlapping generations | No old/stale plan execution, no battery shadow starvation, bounded recovery |
| HA disconnect, wrong helper state, MQTT acknowledgement mismatch (C5 executor; not a C0 requirement) | No infinite wait or false command-success metric; hardware mode reconciled |
| Manual override, disable during heating, competing DHW event | Deterministic owner transition; no orphaned timer or conflicting writer |
| Defrost, compressor short cycle, tank draw, pump hygiene/legionella cycle | Machine/hygiene safeguards remain authoritative, do not train on misleading intervals |
| Cooling requests while heating-only planner active | Refuse to produce cooling control; no unsafe water-temperature or dew-point commands |
| Battery SoC absent or simulated, missing grid measurement | Existing battery shadow service unchanged; climate cannot turn hypothetical battery discharge into real savings |

### Rollback and operational handoff

For each control-stage PR, specify the HA helper/feature flag that restores **legacy** ownership, the expected actual HeishaMon mode after handoff, who controls DHW, the actuator-reconciliation sequence and how to verify the pump after stopping AppDaemon. A stopped process or expired plan is not evidence that the heat pump stopped or entered a safe thermostat mode. Keep native pump protections enabled. Alert on unexpected state and investigate rather than blindly retrying unlimited commands.

## 8. Acceptance criteria and unresolved decisions

The architecture is ready for implementation when this document and ADR are reviewed, but not when the physical controller is ready for actuation. Before C1a, confirm:

- [x] Rooms and thermostats: comfort in bedroom, hall-kitchen and office T; office A excluded (internal gain), modelled as a sink. Topology: underfloor heating, per-room valves opened by the thermostats.
- [x] Actual HeishaMon and thermostat sensor IDs (see the audit).
- [ ] Agreement between HeishaMon energy and the Shelly EM aerothermal branch, including idle/DHW. Partially answered: channel 2 reads a median 1.28x HeishaMon; reconcile in C1/C2.
- [x] Panasonic operating mode and room sensor configuration (permission `Heat+DHW` vs `DHW`; thermostat climate metrics).
- [ ] Manufacturer-safe cycling bounds and native frost/hygiene behavior. Non-blocking for C2: the gate uses starts relative to simulated legacy until the compressor start limit is known.
- [x] Initial heat mode comfort band: minimum 22 °C, maximum 28 °C, as versioned configuration (per room, optionally per time of day).
- [ ] DHW comfort bands and holiday semantics. Disable semantics are decided (C0 scope: the toggle is the manual override and restores the heat mode); manual/holiday behavior of the future executor stays open.
- [ ] Whether battery-optimizer's current deployment can accommodate thermal-data ingestion and solves without interrupting existing slots/archival.
- [ ] Source of reliable historical weather forecasts (existing `weather.forecast_home` archive and limits) and price snapshots for causal replay.
- [x] C2 design, comfort floor and dual-objective gate (see [C2 design](#c2-design-and-predeclared-gate)). The numeric thresholds are committed in `config/climate.yaml` before any result, and the heuristic margins are frozen in the preregistration before S3 is run.
- [ ] Floor areas of the always-open zones (`phi`); non-blocking, defaults to 0.3.
- [ ] Data risks for the C2 models (thermostat resolution, a sun-exposed outdoor sensor, a poor return-temperature slab proxy); non-blocking, the PR 1 audit decides them first.
- [ ] Measurable pilot/rollback criteria agreed before the first active heating plan.

**Completion definition:** the user can compare a causal, reproducible cheap-hours baseline with temperature-aware and MPC forecasts; the climate planner runs read-only on the existing service; AppDaemon executes only explicitly authorized, current and verified plans; DHW and heating share one command owner; battery planning consumes climate demand without double counting; and any upstream failure has a tested comfort-preserving fallback. Removing AppDaemon or enabling battery hardware dispatch is **not** required for this plan to be complete.

## 9. References

- [ODIN (documentation and user manual; solver source not published)](https://github.com/gekkekoe/heatpump-optimizer)
- [tvofi/heatpump_optimizer (MIT-licensed, inspectable MPC reference)](https://github.com/tvofi/heatpump_optimizer)
- [EMHASS (thermal/energy scheduling reference)](https://github.com/davidusb-geek/emhass)
- [Homelab AppDaemon controls](https://github.com/pando85/homelab/tree/master/apps/home-assistant/appdaemon/apps)
- [GoHeishaMon (owner's fork on the CZ-TAW1; `src/taw2mqtt.go`, `HandleSetOperationMode` accepts 0-10)](https://github.com/pando85/GoHeishaMon)
- [Homelab battery optimizer deployment](https://github.com/pando85/homelab/blob/master/docs/deployment/battery-optimizer.md)
