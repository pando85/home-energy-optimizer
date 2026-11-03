# Regime log

Dated log of real plant and control changes. Each row says what changed, the evidence for the date and
which component pages it touches. **Add a row (or turn a `planned` row into a dated one) before anything
else when the house or a control rule changes**; see [how to use these pages](README.md#how-to-use-these-pages).

How the dates were derived (2026-10-30), so they can be re-checked:

- **data**: from the climate feature set `da6d678016ed` (window 2023-11-07 to 2026-05-01, 15-minute
  slots) in the pulled cluster store, read only. Water target = median `flow_target_temp` over slots with
  heat-mode compressor energy; setpoints = mean `*_setpoint` of the three comfort rooms (bedroom,
  hall-kitchen, office T); `min_hours` and the enable flag = value at the start of the slot.
- **code**: the rule-version tables in `structured_forecast.py` (`CLIMATE_RULE_VERSIONS`,
  `DHW_RULE_VERSIONS`), which mirror the homelab commits named there.
- **doc**: the [climate plan](../planning/climate-optimizer-implementation.md) and the homelab page
  `docs/user-guide/heat-pump-water-temperature.md` (audit of 2026-10-30).
- **git**: the homelab commit history (read by the coordinator, commit ids named in the row).
- **observation**: a read-only reading of the device or sensor on the date given (not a change).
- **unverified**: no evidence in this repository; the entry says which bounds are known.

Seasons are those of [`config/climate.yaml`](../../config/climate.yaml): S1 2023-11-08 to 2024-04-30,
S2 2024-10-01 to 2025-04-30, S3 2025-10-01 to 2026-04-30.

## Log

| Date (local) | Change | Evidence | Touches |
|---|---|---|---|
| 2023-11-08 | Start of the `hass_*` history at 1-minute cadence (S1 begins). The direct water-temperature mode is verified only for 2026-10-30 and its 90-day history; the mode in earlier seasons is **unverified** (a constant target per season is consistent with direct mode) | doc | data-pipeline |
| 2023-11-11 | Water target 35 degC (S1 regime; thermostat setpoints 24.0 hall-kitchen, 23.7 mean of the three comfort rooms) | data | cop-model, thermal-model |
| 2023-11-25 | `climate_control` first release: heat only in hours priced below two thirds of the trailing mean, else the cheapest `min_hours` hours (`climate_control@fda18eb2`) | code | heat-pump-controller, battery-load-forecast |
| 2023-11-26 to 2023-12-31 | `min_hours` tuned by hand (5, 7, 9, 6), then 11 from 2023-12-31 (S1 and S2 run 11 h) | data | heat-pump-controller |
| 2024-04-27 to 2024-11-08 | Water target 35 -> 28 degC. **Date unverified**: the last 35 degC heating slot is 2024-04-27, the first 28 degC one is 2024-11-09, and the pump only made hot water in between | data (bounds only) | cop-model, thermal-model |
| 2024-11-09 | S2 heating starts, `min_hours` 11 for all 147 heating days up to 2025-04-23; setpoints 24.8 hall-kitchen in the gate months, 24.4 mean of the three rooms | data | all climate components |
| 2025-02-20 | Water target 28 -> 26 degC (exact day; 28 on 2025-02-19). Two isolated days at 32 to 34 degC on 2025-03-22/23 are excursions, not a regime | data | cop-model, thermal-model |
| 2025-03-16 | `dhw_control` first release: one forced hot-water hour per day (`dhw_control@363047d3`) | code | heat-pump-controller |
| 2025-04-01 | Thermostat setpoints fall from 24.5 to 25.5 (hall-kitchen 25.5) to 23.0 in every room and stay at 23.0 (S3 regime). A temporary 24 to 25 in 2025-08-25 to 2025-09-24 is off-season | data | thermal-model, cop-model |
| 2025-09-22 to 2025-11-03 | Legacy rule disabled; S3 heating starts 2025-11-04 at `min_hours` 10 (`min_hours` 8 before, 6 for the first two days) | data | heat-pump-controller |
| 2025-12-29 | `dhw_control` interval 24 h -> 12 h (`dhw_control@153d7f88`) | code | heat-pump-controller |
| 2026-01-01 23:30 | `min_hours` 10 -> 15 (first slot at 15; the rule decision of 2026-01-02 uses it): 53 heating days at 10 h, then 84 heating days at 15 h up to 2026-04-03 | data | heat-pump-controller, battery-load-forecast |
| 2026-02-09 to 2026-02-12 | Thermostat outage (excluded by `filters.thermostat_outage`) | data | thermal-model |
| 2026-04-07/08 | `min_hours` 15 -> 10; rule disabled from 2026-04-08, enabled again 2026-04-22 (last day in the feature set: 2026-04-30) | data | heat-pump-controller |
| 2026-04-28 | `climate_control@e734f5e9`: history window 10 -> 7 days with retries | code | heat-pump-controller |
| 2026-09-05 | `climate_control@5af36d49` and `dhw_control@5af36d49`: negative prices accepted | code | heat-pump-controller |
| 2026-10-30 | C0 AppDaemon fixes (homelab), committed and pushed to homelab master between about 16:37 and 16:45 +01:00 (commits `9566b7ad0` to `23611caa7`); ArgoCD deploys from GitHub, so deployed the same day: the enable toggle cancels timers and restores the heat mode, a heartbeat watchdog restores it after 15 minutes, the price-retry loop re-checks the toggle, all nine heat modes map to `SetOperationMode`, numeric sensors for operating mode, defrost and 3-way valve. Legacy hour selection unchanged. Date verified from homelab git | git | heat-pump-controller, data-pipeline |
| 2026-10-30 | Template room-temperature sensors and `hass_climate_action` (real valve demand) start to exist; earlier demand must be inferred. Pump verified in direct mode, water target 26 degC, space heating off (hot water only) | doc | data-pipeline, thermal-model |
| 2026-10-30 | Observation, not a change: HeishaMon shows the legionella/sterilization function off (`Sterilization_State` 0; configured 65 degC / 10 min unused; tank never above 51 degC 2023-11 to 2026-10) and DHW heater lifetime hours 0 | observation | heat-pump-controller, boost-mode, battery-optimizer |
| 2026-10-30 | Observation, not a change: room/backup heater lifetime hours 29 (date range unknown); heater-on outdoor temperature 0 degC. Read-only HA sensors for the heater, sterilization and `Heating_Mode` are being added in homelab (entity ids to follow) | observation | heat-pump-controller, data-pipeline |
| 2026-10-30/31 | Heater, sterilization and Heating_Mode sensors added to HA (homelab commits `d9c3bc3e7` and `1f23f6b2b`), history starts here; entity ids in [heat-pump-controller](heat-pump-controller.md#heater-facts-import-limit-inputs) | git | heat-pump-controller, data-pipeline, battery-optimizer |
| 2026-10-30 | Decoupled design agreed ([ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md)): the heat pump does comfort with the fewest kWh and ignores price, the battery does price arbitrage | doc | all |
| 2026-10-31 | Climate shadow runner (track B2) added to the service, **disabled by default, not deployed**: B0's model and the comfort MPC on live data, read-only. Its deployment day starts the preregistered legacy window. Not a plant change | doc | climate-mpc, thermal-model |
| 2026-10-31 | Comfort-controller design agreed with the owner ([design](../planning/comfort-controller-design.md), [ADR-0006 amendment](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md#amendment-2026-10-31)): DHW stays price-optimised at most 50 degC, AppDaemon sets the water temperature from a software curve, rollback by `git revert`, evaluation becomes before/after. Design only, nothing deployed | doc | heat-pump-controller, boost-mode |
| 2026-11-03 20:14:32 | **Battery commissioned** (Marstek Venus E V3, 5.12 kWh, AC-coupled, behind Omnibattery v1.5.0): AC input first energised, `battery.commissioned_at` 2026-11-03T19:14:32.572Z. From here Shelly channel 1 is net grid and gross load = grid - charge + discharge (AC-side counters `sensor.venus_total_charging_energy` / `_discharging_energy`; the discharge counter excludes the backup port). Measured: grid sign (negative on export), `sensor.venus_ac_power` negative while charging. Left in place: backup function on, system power limits on at 1000 W charge / 800 W discharge (temporary test socket), backup off-grid threshold 200 W (500 W later the same evening), both allow switches off, PD target 0 (battery idle). The optimizer stays in shadow with a simulated SOC | observation (supervised commissioning test, owner present) | data-pipeline, battery-model, battery-load-forecast, battery-optimizer |

## Planned (not yet real: dates to be filled in)

| Planned change | Components that go stale when it happens | Notes |
|---|---|---|
| **Comfort schedule live**: hall-kitchen at least 23.5 degC from 12:00 on weekdays and from 09:00 at weekends, every room at least 21 degC always; optimum start | battery-load-forecast, heat-pump-controller, hp-consumption-forecast, system-replay-benchmark | Start the 28-day settling clock here |
| **Climate shadow (B2) deployed**: `climate_shadow.enabled`, its prior id and the date | none (read-only); it starts the legacy window of the [preregistration](../planning/climate-preregistration-2026-27.md) | Record the prior id; keep the prior through both windows |
| **Comfort controller live (after the battery works)**: AppDaemon run logic, software water-temperature curve in direct mode (20 to 35 degC), DHW at most 50 degC by price / COP; legacy `climate_control.py` archived in homelab, rollback by `git revert` | heat-pump-controller, cop-model, thermal-model, battery-load-forecast, hp-consumption-forecast, system-replay-benchmark | [Design](../planning/comfort-controller-design.md); merged and tested only once the house battery is working. Also the "after" start of the before/after evaluation and the 28-day settling clock; preregister the criteria before this date |
| **Legacy cheapest-hours space-heating rule retired** (DHW price scheduling stays, one hot-water writer) | hp-consumption-forecast (LF-1), battery-load-forecast | Three price reactors by design: battery optimiser, DHW scheduler, boost |
| **Boost mode live** for very cheap hours (rare in winter) | boost-mode, battery-load-forecast | Interlock reads battery SOC |
| **Shadow planner on the measured SOC deployed** (`planning.soc_source: entity`, availability plus Omnibattery's liveness sensor; config of the optimizer, not a plant change) | none (shadow records only: from this date `soc_source` is `entity` in plans and slots) | Fill in the deploy date; shadow-report windows should not span it |
| **Battery go-live** through OmniBattery driven by this optimizer (`mode: live`; installed and commissioned 2026-11-03, row above) | battery-model, battery-optimizer, battery-load-forecast | Measure RTE and idle draw; go-live configuration: system max charge and discharge 2500 W each (from the test socket's 1000/800 W) and Marstek Premium mode on (discharge above 800 W) ([known issues](../known-issues.md#the-pd-target-is-bounded-by-the-system-caps-while-the-power-limits-are-on)) |
| **Heating-curve change / water-temperature trial** (C7-F, now a before/after comparison; alternating arms only if an arm switch is ever added) | cop-model, thermal-model, battery-load-forecast | The software curve takes effect at controller go-live; alternating-arm weeks, if any, are excluded from forecast and gate windows |
