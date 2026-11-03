# ADR-0006: Decouple heat-pump comfort control from battery price optimisation

- **Status:** Accepted (2026-10-30; architecture and interface only; no authorisation to change physical controls); amended 2026-10-31 (decisions 3 and 4, see [Amendment 2026-10-31](#amendment-2026-10-31))
- **Date:** 2026-10-30
- **Decision owners:** Homelab maintainers (the owner agreed these decisions on 2026-10-30)
- **Supersedes:** decisions 1 and 2 of [ADR-0005](0005-shared-energy-planning-climate.md) (the shared planning runtime with a price-driven climate MPC). Decisions 3 to 7 of ADR-0005 stay in force.
- **Extends:** [ADR-0001](0001-home-battery-economic-dispatch.md) (section 7, the integration boundary) and [ADR-0004](0004-battery-optimizer-production-architecture.md) (Omnibattery as sole Modbus writer; gates B, C and D)
- **Evidence:** [Whole-system replay](../planning/system-replay.md)
- **Execution plans:** [Climate optimizer implementation plan](../planning/climate-optimizer-implementation.md) (new objective and ordered work list), [C7-F field trial](../planning/climate-flow-calibration-optimization.md), [Comfort controller design](../planning/comfort-controller-design.md) (2026-10-31)
- **Affected repositories:** `pando85/home-energy-optimizer` (battery optimiser, evaluation) and `pando85/homelab` (AppDaemon/HA configuration, heat-pump control, battery integration)

## Context

Two controllers could react to the same price signal. The legacy AppDaemon rule picks the cheapest hours for the heat pump (HP), and the battery optimiser in this repository buys the cheapest energy for the house battery (Marstek, 5.12 kWh, 2.5 kW). ADR-0005 answered this with a shared, price-driven climate MPC next to the battery MPC and a forecast exchange between them, with joint dispatch left as a later possibility.

The whole-system replay (offline, read-only, empirical; see [system-replay](../planning/system-replay.md)) compares the legacy heat-pump control (A) with an efficiency-first one (B), each with and without the simulated battery. Relative results:

- The battery's share of cost is nearly independent of the heat-pump regime. In S3 it removes 16.0% of cost with the legacy load and 16.1% with the efficiency-first load. S2 agrees within about one point (13.4 to 14.6%).
- Efficiency-first costs a small amount before the battery (+2.1% in S3) and the battery more than covers it. B with battery is about 14% below legacy without battery in S3, with roughly half the time below 22 °C (11.4% against 20.5%).
- The battery is rarely at its floor on expensive cold days (0 to 3.3% of such slots).

Caveats that stay attached to these findings. B is an empirical transfer of the 15 h days, not a simulation of the comfort schedule decided below, so the battery findings are plausible but unconfirmed for the new load shape. The comfort side is an association that the field trial must confirm. The battery assumptions (round-trip efficiency 0.80, idle draw, taxes) are provisional.

Two further inputs drive the comfort schedule. The hall/kitchen averaged about 23.4 °C in the daytime in S1 and S2 and 22.2 °C in S3. In S3 the bedroom was below 21 °C 15.5% of the time. A price-chosen schedule cannot guarantee that anyone is comfortable at a given hour, and that is what the owner wants guaranteed.

Price data adds a third input for the exceptional hours. Windows of at least 3 h at or below 0.04 €/kWh are rare in November to March (7 days in about 2.5 winters) and frequent in April to October (99 days, mostly weekends 10:00 to 18:00). The price paid has been negative for a few hours.

## Decision

1. **Decouple.**
   - The HP controller (homelab AppDaemon) meets a comfort schedule with the fewest kWh: low water temperature, the native heating curve and long gentle runs. In normal mode it ignores price.
   - The battery optimiser (this repository) does price arbitrage and treats household load, the HP included, as exogenous.
   - Joint dispatch is not pursued. Revisit it only on new measured evidence and with a new ADR.

2. **Comfort schedule.** The schedule gives minimums, not targets.
   - Hall and kitchen at 23.5 °C or more from 12:00 on weekdays and from 09:00 at weekends.
   - Every room at 21 °C or more at all times.
   - Optimum start uses learned warm-up rates (outdoor temperature and current room temperature to the time needed to reach the minimum).
   - All parameters are exposed as Home Assistant helpers, so the owner changes them without a deployment.

3. **Boost mode for very cheap hours.**
   - Trigger: an absolute price threshold, starting at 0.04 €/kWh or less for 3 h or more, tunable. State the price basis (the pre-tax PVPC sensor, the same basis the battery uses) in the homelab configuration.
   - Order inside the window:
     1. the battery charges first;
     2. the HP soaks heat (room ceiling about 25.5 °C, water about 30 to 32 °C);
     3. the domestic hot water (DHW) tank goes to 55 to 60 °C.
   - Priority: boost sits after normal mode and after battery arbitrage. It never relaxes the comfort minimums, the compressor limits, the legionella and hygiene cycles or the manufacturer protections.
   - DHW comes first in delivery: it is useful all year, whereas the space-heating soak matters mostly in April to October, when it has little heat to store.
   - Mechanism (smallest that works): AppDaemon reads the battery state of charge (read-only) and starts the soak when it reaches about 90 to 95%, or charge power falls below a threshold, or `window_end - soak_duration` is reached, whichever comes first. The battery planner needs no input. With no battery present the rule degrades to "no battery". The battery cannot be made to go first through Omnibattery's grid-target regulation, which yields to load, so the ordering has to come from the HP side waiting.

4. **Price reactors.** Only the battery optimiser and boost react to price. Retire:
   - the legacy AppDaemon cheapest-hours heating rule, when the comfort schedule goes live;
   - DHW price forcing (one DHW writer remains);
   - OmniBattery's own price features (time slots, predictive grid charging, dynamic pricing), so the optimiser is the only battery price reactor.

5. **Battery actuation path.**
   - The Marstek battery connects to Home Assistant through the OmniBattery integration and is programmed by this repository's optimiser. OmniBattery remains the sole Modbus writer and fast regulator (ADR-0004 decision 4).
   - The existing gates still apply: B (extraction), C (two weeks of shadow against native behaviour) and D (physical commissioning and failure tests). Gate C now needs two weeks of real battery telemetry in the new HP regime, not overlapping the switch or alternating trial blocks.
   - A separate **actuator ADR** comes before go-live. It decides the writer process (a separate entrypoint, not `serve`), the scoped Home Assistant token limited to the battery entities, the manual-override back-off and the amendment of the shadow-phase rule in `AGENTS.md`.
   - This ADR does **not** change the shadow rule, add credentials or authorise any write. The project stays read-only until the actuator ADR is accepted.

6. **Interface: forecasts, not dispatch.** As in ADR-0005 decision 5, the systems exchange forecasts. The HP never reads the battery plan for comfort decisions, and the battery never commands the HP. The minimal optional HP-plan entity below is specified now and built **only if measurement shows the HP forecast share is material**. The test is the price-weighted headroom of `load_decomposition.py` for the `heat_pump` component on 6 to 8 weeks of data from the new regime, compared with the gap between the trailing-mean forecast and a perfect one. Otherwise keep the trailing-mean forecast, or the day-type forecast if it now passes its gate.

   One read-only Home Assistant entity, published by the HP controller:

   ~~~text
   sensor.hp_planned_energy        state = kWh planned for the next 24 h (P50)
   attributes:
     schema_version: 1
     generated_at: <UTC ISO>       information cutoff
     valid_until: <UTC ISO>        the battery ignores the plan after this
     plan_id / rule_version: <str>
     meter_basis: "ch2_branch" | "heishamon"
     slots: [ {start: <UTC ISO, 15 min>, kwh: <float>, kind: "heat" | "dhw" | "boost"}, ... ]
     boost: {start, end, max_kw} | null
   ~~~

   Battery side: `gross_forecast = forecast(whole-house meter minus measured HP) + sum(slots.kwh)`, falling back to the plain forecast when the entity is stale, missing or past `valid_until`, with the basis recorded in the forecast metadata. This is the no-double-count rule of the climate plan section 5. The residual must be computed on the same meter basis the plan publishes (the branch meter and HeishaMon differ by a stable factor and the branch also feeds other appliances). No new endpoint, credential or process is needed.

## Consequences

### Superseded and obsolete

- **ADR-0005** keeps its text. Its status line records that decisions 1 and 2 are superseded by this ADR. Decisions 3 to 7 stay.
- Obsolete or parked, with pointers from the plans:
  - roadmap section 6, joint HVAC/DHW and battery optimisation: closed by this ADR;
  - TODO-8.1: superseded;
  - the price-based climate objective (`min sum price x kWh`): replaced by minimum kWh subject to the comfort schedule, with price entering only through boost;
  - C3 and C4 as a cost MPC, and the shared climate planner in `serve` (C1b, `GET /climate/plan`): parked; the offline `climate/` package stays for warm-up-rate learning;
  - joint MPC in C7: dropped;
  - the cheapest-hours preheat arm of the C7-F field trial: dropped, since boost is rare in winter.
- Follow-up wording for later PRs, not made here: the ADR-0003 baseline should read "measured load under the HP regime in force in the window", and the ADR-0004 charge mapping (positive import target from desired charge plus forecast load) is to be reconsidered by the actuator ADR as a cap below the import limit.

### Stays valid

The battery kernel, oracle, rolling horizon and dispatch contract; the live planner (free terminal mode); ADR-0001 section 7 (AppDaemon owns comfort, the battery observes); ADR-0004's actuation pattern; the benchmark as the battery acceptance test (relative saving against `no_battery` on the same load); price-outlook and weather archives. Decoupling makes the exogenous-load assumption of the battery code more true, not less.

### Benchmarks and data

- Record the HP switch date(s) and any trial block calendar in `docs/planning/`, never in `config/tariff.yaml` (its hash re-mints every `dataset_id`).
- The `no_battery` baseline for the new regime is the load measured in that regime. Comparing new-regime battery cost against a legacy-regime `no_battery` would mix the regime premium into the battery's figure.
- Exclude the first 28 days after the switch, and alternating-arm trial weeks, from gate-C and forecast evaluations.
- Once the battery runs, the C7-F electricity metric must be read from the HP sub-meter or HeishaMon, not the whole-house meter, or battery dispatch leaks into it.

### New risks

1. **Load forecast transient.** The trailing-mean forecast averages 28 days, so for four weeks after the switch it mixes the legacy shape (HP load in cheap, mostly night hours) with the new one (weekday optimum start in the morning, in higher-price hours). It under-forecasts exactly where discharge pays most, then heals as the window rolls. It is also weekday-blind, while the schedule differs by day type; boost smears into other days; and cold snaps lag. This is safe while the battery is shadow-only and tolerable afterwards if the import cap below holds (the replay found the battery at its floor in 0 to 3.3% of expensive cold slots). Mitigation is measurement: the HP decomposition headroom decides whether the plan entity is built.
2. **Gross-load bug in `build.py`.** The offline build registers no battery telemetry and, by default, assumes no battery, so once a battery runs it would store net grid as gross load (charging counted as house load, discharge hidden). The service's own history path already reconstructs gross load. Before go-live, register the battery counters, implement `raw - charge + discharge` in `build` (this item first said `raw + charge - discharge`, a sign error
carried into the code and corrected 2026-11-03 before go-live) and refuse the no-battery assumption for windows after commissioning. Without this, post-commissioning datasets are unusable.
3. **Import limit of 5.0 kW and its enforcement (unconfirmed).** The model treats it as a 15-minute average with no margin and constrains charging only through forecast load, so unforecast boost or DHW load is invisible to it. Whether the real enforcement is a 15-minute average or an instantaneous overload trip of the smart-meter power control has not been confirmed with the contract or distributor. DHW at 55 to 60 °C may also engage the tank's electric booster, whose power is unverified. Smallest robust fix, to be tested at commissioning and decided in the actuator ADR: let Omnibattery's real-time regulation enforce a grid target below the limit minus a margin, with a charge-rate bound. AppDaemon must not start a booster while another large load runs.
4. **Wear cost 0.0 in `config/service.yaml`.** The live planner takes any cycle whose spread clears the 0.80 round trip, including marginal ones (about 1.2 equivalent full cycles per day in winter in the replay). A non-zero throughput cost, chosen from the existing sensitivity sweep and the warranty terms, is needed before go-live. *Mitigated 2026-10-31: set to 0.01 EUR/kWh, see [battery-model](../components/battery-model.md#wear-cost-decision-2026-10-31).*
5. **Two writers and herding.** Boost and battery charging land in the same cheapest slots. Single writer per device: OmniBattery entities only from the battery actuator, HP, HeishaMon and DHW entities only from AppDaemon; cross-reads are fine. HA cannot enforce this, so use convention plus a homelab lint and an alert on battery configuration changes that do not come from the actuator.

### Not decided here

The actuator ADR; the staged package rename (not coupled to go-live or authority changes); the HP-plan entity implementation; any throughput cost value; the boost threshold beyond its starting value. The staged rename loses part of its motivation (a co-hosted climate planner in `serve`) but stays valid.

## Amendment 2026-10-31

The owner and the maintainers agreed the comfort-controller design in
[comfort-controller-design](../planning/comfort-controller-design.md). The text above is kept as decided on
2026-10-30. Where it differs from this section, this section governs.

- **Decision 3 (boost), DHW step:** the DHW boost target is **at most 50 degC**, not 55 to 60 degC. The
  heat pump alone reaches it, so the 3 kW electric heater never starts (service manual: above about 51 to
  55 degC needs the heater). The other boost decisions stand.
- **Decision 4 (price reactors), DHW:** DHW **stays price-optimised**; DHW price forcing is not retired.
  Shifting the tank is lossless, whereas the battery loses 10 to 20% round trip and has limited capacity.
  Rule: the cheapest 2 h window of the day, ranked by price divided by expected COP, avoiding the
  optimum-start warm-up, tank target at most 50 degC. There are now **three price reactors**: the battery
  optimiser, the DHW scheduler and boost. The legacy cheapest-hours space-heating rule is still retired.
  Two writers in the same cheap hours (new risk 5) and the import-limit risk (new risk 3) therefore grow:
  the battery grid-target cap below the 5.0 kW limit is mandatory before go-live, and AppDaemon never
  triggers the 3 kW heater.
- **Heat-pump water temperature:** AppDaemon sets it, in direct mode, through a **software heating curve**
  with editable points (not the unit's native curve), integers clamped to 20 to 35 degC, written only in
  direct mode and rate-limited. This replaces "the native heating curve" of decision 1 and of the C7-F
  arm B wording.
- **Rollback:** by **`git revert` in homelab**, with the legacy app archived there. There is no runtime
  strategy switch. If AppDaemon is down the existing watchdog leaves the pump heating at the last water
  temperature and the thermostats keep protecting comfort.
- **Evaluation:** without a runtime switch the [C7-F](../planning/climate-flow-calibration-optimization.md)
  trial cannot alternate arms. It becomes a **before/after comparison** normalised for outdoor temperature,
  preregistered before go-live, which is weaker evidence than a randomised trial and is reported as such.
- **Shadow climate MPC permitted:** a thermal model and an MPC (minimum kWh subject to the same comfort
  schedule, price-blind) may be built in this repository and run **in shadow** for comparison. This partly
  un-parks C1b, **as shadow only**: no writes, no new credentials and no Home Assistant service calls. The
  shadow rule of `AGENTS.md` is unchanged. Shadow validates predictions and plan differences, not the
  realised comfort of the MPC.
- **Rollout gate:** the controller is merged and tested only once the house battery is working.
