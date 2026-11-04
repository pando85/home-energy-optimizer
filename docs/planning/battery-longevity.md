# Battery longevity: SOC window, cell balance and thermal policy

- **Status:** Plan and operating policy, owner decisions recorded 2026-11-04; **nothing here is implemented yet** except
  the two merged full-charge PRs (#80, #81). Each step below is its own PR, reviewed on its own. Merging this document
  authorises nothing on the installation.
- **Date:** 2026-11-04
- **Battery:** Marstek Venus E V3 (5.12 kWh nominal, LFP), controlled through Omnibattery as the single Modbus writer
  ([ADR-0007](../adr/0007-live-battery-control.md)).
- **Related:** [periodic full charge](periodic-full-charge.md) (amended the same day with the decisions of this
  document), [battery model](components/battery-model.md), [dispatch optimizer](components/dispatch-optimizer.md).
- **Source:** an external investigation reviewed on 2026-11-04 (operational guidance, not a manufacturer
  specification). This page keeps its conclusions, separates documented behaviour from proposed settings, and adds the
  owner's decisions and one new observation. The Marstek BMS and hardware protections always take precedence; none of the
  numbers here is a safety cutoff.
- **Privacy:** relative figures, parameters, SOC and temperatures only. No costs, savings, absolute energy or
  installation addresses belong in this page.

## 1. Purpose

A daily grid-arbitrage workload cycles the battery every day. This plan keeps three things in balance: usable energy
(the economics), ageing (time at high SOC, temperature) and cell balance (an LFP pack needs an occasional charge to the
top so that its cells are measured and balanced). None of the SOC limits or intervals below is proven optimal for this
particular battery: they are engineering defaults to validate with measurements and, where possible, a replay.

## 2. What is documented and what is proposed

The distinction matters because the first column can change with an Omnibattery upgrade and the second is ours to tune.

### Documented Omnibattery behaviour (references 1 to 3 and 7; version-dependent)

| Topic | Behaviour |
|---|---|
| Top-of-charge sequence (Venus E, with 100% Charge Voltage Taper enabled) | Charge normally until the highest cell reaches about 3.48 V; taper the charge request to 200 W; stop at about 3.60 V on the highest cell, or earlier if a BMS cutoff is confirmed; wait 60 s after charging stops; record the comparable cell-voltage spread. These are Omnibattery implementation thresholds, not Marstek safety limits and not adjustable by us |
| Balance measurement | `cell_delta_mV = (max cell V - min cell V) * 1000`, taken at that reproducible top-of-charge condition. A live voltage spread at mid SOC is not a reliable imbalance estimate on LFP (flat curve). Measurements at other states or under load are not comparable |
| Balance entities (find by friendly name on the battery device; IDs are per installation) | Cell delta at 100% (mV), balance status (`green`, `yellow`, `orange`, `red`, `unknown`), last reading (timestamp), delta trend, delta average of the **last four readings** (not four weeks), maximum and minimum cell voltage, internal temperature |
| Status bands of the settled delta | green below 200 mV, yellow 200 to 229, orange 230 to 249, red 250 or more, unknown without a valid reading. These are the integration's diagnostic categories, not universal LFP health thresholds; its optional active-balance blueprint targets 30 mV, a different thing |
| Weekly full charge | Raises the target SOC to 100% on the selected weekday, waits for its completion criteria, restores the configured target. It does not implement a balancing algorithm of its own |
| Temperature Charge Limit | Documented default: charge power starts to be reduced above 40 degC, ramps down over the next 10 degC, floor at 40%; discharge limiting is a separate optional setting |

### Proposed site settings (ours, to validate)

| Parameter | Proposal | Qualification |
|---|---|---|
| Daily minimum SOC | 20% | Reserve; not a Marstek cutoff |
| Daily soft ceiling | 90% | Time at high SOC is the cost; see step 5 |
| Hard ceiling | 95% (kept) | The existing planner window; the soft band above 90% is conditional on prompt discharge |
| Periodic full charge | 100%, adaptive cadence | [Periodic full charge, amendment A3](periodic-full-charge.md#a3-adaptive-cadence) |
| Thermal soft cap | about 1.5 kW | Step 6; parameters from telemetry |
| Omnibattery Temperature Charge Limit | **default stays** (owner setting) | A backstop only; any more conservative threshold would be a site experiment |

Energy trade-off, as fractions of nominal capacity: a 20 to 90% window cycles 70%, a 20 to 95% window 75%, i.e. the last
5 points of SOC add a twentieth of the capacity before conversion losses and any BMS reserve. Whether those points are
worth their ageing cost is **uncalibrated**; the replay of step 5 reports it as a relative comparison and does not claim
a financial proof.

## 3. Current state and target state

| Aspect | Where we are | Where we want to be |
|---|---|---|
| SOC window | Hard 20 to 95% in the planner; the planner is indifferent to time spent between 90 and 95% | Hard 95% kept; a linear cost per kWh and hour of SOC above 90%, so the planner goes above 90% only when the energy is discharged soon (step 5) |
| Full charge | Solver and planner support a soft 100% checkpoint (#81); actuator, guard and config for Omnibattery's weekly full-charge enabler are merged (#80); no scheduler, feature off | Adaptive cadence driven by the measured balance, success only on a new valid reading, idle settle hold after completion ([amendment](periodic-full-charge.md#amendment-2026-11-04-success-criterion-settle-hold-and-adaptive-cadence)) |
| Balance visibility | The Omnibattery balance sensors exist but nothing in the service reads or records them; there is no baseline reading since commissioning | Read-only telemetry in `/status`, metrics and the dataset; a first valid reading as the baseline |
| Thermal behaviour | Internal temperature observed by hand; charge power is limited only by the configured caps | Hot-time and per-session peak temperature recorded; a convex penalty on charge power above about 1.5 kW chosen from the data (step 6) |
| Unattended safety | Heartbeat watchdog turns the direction switches off; nothing turns the weekly switch off | Homelab watchdog also turns the weekly switch off (follow-up in the other repository) |

### The observation behind the thermal step

The external document assumed that the internal temperature peaks at the end of a charge (a brief 40 degC peak near the
end of charging was reported). On **2026-11-04** the reported internal temperature rose by **about 10 degC (from about
32 to 42 degC) during a charge of about 50 minutes at about 1.4 kW at mid SOC (about 39 to 59%)**, not at the end of
charging, and fell back within an hour. So heat shows up under moderate power at mid SOC, which is why the thermal step
is about charge power over the session and not only about the top of the charge. **The location of the temperature
sensor (cells or electronics) is unverified**: an electronics reading would overstate the cell temperature and its fast
recovery would be the expected behaviour; the telemetry of step 3 (peak per session, seconds above thresholds, together
with power and SOC) is what lets the data decide. One observation proves nothing about lifespan, and a single short
peak is not by itself harmful; sustained or repeated high readings, high SOC and time matter more than a maximum.

## 4. Steps

Each step is a separate PR (or an action outside the repository) and each stays behind its own off switch where it
changes behaviour.

1. **Merged: PRs #80 and #81** of the [full-charge plan](periodic-full-charge.md): the soft full-charge target in the
   solver and planner, and the actuator, guard and config for the weekly-full-charge enabler. Feature off, no scheduler.
2. **This documentation PR**: amends the full-charge plan (success criterion, settle hold, adaptive cadence, first run,
   dwell, PR 3 scope) and records the longevity policy.
3. **Read-only battery-health telemetry** in the service (new entities read, nothing written, no change to the
   allowlist). Records in `/status`, metrics and the dataset extract:
   - the balance sensors: cell delta, balance status, last balance reading, delta trend, delta average of four readings;
   - internal temperature, maximum and minimum cell voltage;
   - **seconds above 35 degC and above 40 degC** (counters, not only a daily maximum), and the **peak temperature of each
     charge session** with its mean power and SOC range;
   - the same entities added to the dataset extract so that the replays below can use them.
4. **PR 3, the scheduler**, with the amendments of the full-charge plan (adaptive cadence, new-reading success criterion,
   settle hold in the actuator, balance telemetry consumed from step 3). Acceptance: at least **7 days of `dry_run`**, a
   **supervised first live run** with the owner and the kill switch, and the **homelab watchdog follow-up** (the watchdog
   also turns the weekly switch off on heartbeat loss) before any unattended use.
5. **High-SOC dwell cost.** Keep the hard 95% ceiling and add to the objective a **linear cost per kWh and hour of SOC
   above a soft threshold (90%)**. The planner then goes above 90% only when the extra energy is discharged soon, instead
   of whenever the purchase price is low; the cost also applies on the full-charge day, which is what pulls the checkpoint
   towards the expected discharge. The value is chosen from a **replay study** comparing three configurations: 20 to 90%
   hard, 20 to 95% hard, and the soft band; **relative figures only** are reported, and the value is the smallest one
   that makes the soft band behave as intended, not a claim of optimality. The model change is a MILP-compatible
   (linear, continuous) term, with a golden test that the model is unchanged when the cost is zero.
6. **Thermal soft cap.** A **convex penalty on charge power above about 1.5 kW** in the objective. The 2.0TD valley is
   long, so spreading the same charge over more slots costs little, while the penalty keeps the pack out of the hot
   region. The parameters (knee and slope) come from the temperature telemetry of step 3 and a **1.5 versus 2.5 kW
   comparison** of otherwise similar charge sessions (hot-time and peak per session, relative figures). Omnibattery's
   **Temperature Charge Limit stays at its default** (owner setting) as a backstop, not as the mechanism.
   **Status:** implemented off by default; evidence, replay study and recommended values (1.5 kW at 0.005 EUR/kWh) in
   [thermal soft cap](thermal-charge-soft-cap.md). The thermal data was too thin for a fitted knee (21 h, two charge
   sessions, none above 1.5 kW), so 1.5 kW is the documented conservative choice and the 1.5 versus 2.5 kW comparison
   is still open.

Steps 5 and 6 change the objective: each updates [battery-model](../components/battery-model.md) and
[battery-optimizer](../components/battery-optimizer.md) and the regime log in its own PR, per AGENTS.md.

## 5. Risks and what stays out of scope

| Risk | Mitigation |
|---|---|
| A reading is stale, missing or taken under load and is treated as a result | Success only on a new valid reading, `unknown` is never zero imbalance, settle hold keeps the battery idle during the 60 s measurement ([amendment](periodic-full-charge.md#a2-settle-hold-in-the-actuator)) |
| The cadence is too long for a drifting pack, or too short for a healthy one | Adaptive rows keyed by status; orange or red twice in a row raises an owner alert; the cadence numbers are config |
| One red reading triggers a reaction | One reading alone repeats the measurement at the next eligible day, never an alert or a recovery |
| The dwell cost or the thermal cap makes the plan worse in ways not seen in the replay | Each is a separate PR with a zero-value setting that leaves the plan unchanged, and a replay gate before use |
| The temperature sensor measures electronics, not cells | Treated as unverified; conclusions about cell temperature wait for the telemetry; the cap is a soft convex penalty, never a hard limit |
| Omnibattery changes thresholds or entity names in an upgrade | The startup checks of the full-charge plan disable the feature; the references below are rechecked at each upgrade; its voltage values are never copied into this repository's logic |
| Interplay between the economic actuation and a maintenance run | One orchestrator (the service through Omnibattery's weekly feature); the guard accepts the maintenance status only while a run is armed; timeout, HA restart and unexpected BMS stop behaviour is the failure-mode table of the full-charge plan |

**Out of scope**, unchanged by this plan:

- **Active-balance recovery**: no automatic recovery or active-balance routine; the Omnibattery recovery blueprint is not
  used and not automated, even on repeated orange or red (the owner is alerted and decides). Revisit only after repeated
  measurements and a review of the blueprint.
- **BMS thresholds and any voltage value** as our own cutoff: none is set, copied or overridden.
- **Direct Modbus**, REST writes or any new device: Omnibattery stays the only Modbus writer (ADR-0007).
- **Omnibattery's Manual Battery Control**, and any widening of the actuation allowlist beyond the two weekly-charge
  entities already amended into ADR-0007.
- Changing Omnibattery's Temperature Charge Limit (owner setting, default kept).
- Claims about lifespan or money from the SOC window, which stay uncalibrated.

## References

1. OmniBattery, Marstek configuration (Venus E v3 support, 2.5 kW rating, top-of-charge profile):
   https://ffunes.github.io/Omnibattery/configuration/batteries/marstek/
2. OmniBattery, Cell balance monitor (entities, voltage taper, settled measurement, status bands):
   https://ffunes.github.io/Omnibattery/features/cell-balance-monitor/
3. OmniBattery, Weekly full charge (native scheduling and completion semantics):
   https://ffunes.github.io/Omnibattery/features/weekly-full-charge/
4. Victron Energy, Lithium Battery Smart manual, Operation and Cell balancing (cross-vendor rationale for periodic full
   charging, not a Marstek specification):
   https://www.victronenergy.com/media/pg/Lithium_Battery_Smart/en/operation.html
5. Shen et al. (2025), Coupled influence of state-of-charge and storage temperature on calendar aging and subsequent cycle
   degradation in LiFePO4/graphite pouch cells, Journal of Materials Chemistry A 13, 26297-26309:
   https://doi.org/10.1039/D5TA03813H
6. Saeed, Alshehri and Munteshari (2024), Calendar aging of commercial lithium ion batteries under realistic conditions,
   Thermal Science and Engineering Progress, article 102436 (general Li-ion evidence, not specific to this battery):
   https://doi.org/10.1016/j.tsep.2024.102436
7. OmniBattery, Temperature charge limit (charge-derating algorithm and defaults):
   https://ffunes.github.io/Omnibattery/features/temperature-charge-limit/
8. ADR-0007, live battery control (single Modbus writer, guard, allowlist): [ADR-0007](../adr/0007-live-battery-control.md)

Omnibattery behaviour is version-dependent: recheck references 1 to 3 and 7 whenever the integration is upgraded.
