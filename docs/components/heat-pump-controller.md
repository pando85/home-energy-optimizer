# Heat-pump controller (legacy cheapest hours now, comfort controller next)

## Purpose and owner

Decides when the Panasonic Aquarea heats rooms and makes hot water. **Owner: the homelab repository**
(AppDaemon `climate_control.py` and `dhw_control.py`, HA helpers, HeishaMon automations); this repository
only observes it and replays it. Water-temperature control, its modes and the commands the unit accepts
are documented in the homelab repository at `docs/user-guide/heat-pump-water-temperature.md`. This
repository never writes to it (shadow phase, [AGENTS.md](../../AGENTS.md)).
The successor is specified in [comfort-controller-design](../planning/comfort-controller-design.md)
(agreed 2026-10-31, not built; merged only once the house battery works): AppDaemon decides heating on/off
from room temperatures, writes the water temperature from a software curve in direct mode and keeps DHW
price-optimised with the tank target at most 50 degC (never the 3 kW heater).

## Interfaces

- **In:** the PVPC price sensor, `input_number.appdaemon_climate_min_hours_per_day`, the enable toggles,
  manual thermostat setpoints, and the water-target helper (absolute value, direct mode).
- **Out:** operating mode `Heat+DHW` or `DHW`, `switch.aquarea_force_dhw_mode`; the unit's own water target.
- **Consumers:** the whole-house load that the [battery load forecast](battery-load-forecast.md) learns
  from; [cop-model](cop-model.md) and [thermal-model](thermal-model.md) data; LF-1
  ([hp-consumption-forecast](hp-consumption-forecast.md)) replays it.

## Heater facts (import-limit inputs)

From the Panasonic service manual (indoor WH-ADC0309J3E5 with outdoor WH-UD09JE5 documented; the owner's
WH-ADC0309JE5C and WH-UD09JE5-1 are assumed equivalent). Page details in the homelab
`docs/user-guide/heat-pump-water-temperature.md`.

- One integrated 3.00 kW electric heater (13.0 A) on its own supply (own 16 A breaker); backup for space
  heating and, with the tank heater set to Internal, the DHW booster (p.25-26, 138, 146).
- Heat pump alone at most 3.47 kW (about 2.0 kW rated at A7W35); **worst case with the heater is about
  6.5 kW**, above the 5.0 kW import limit, which the smart meter enforces by cutting supply (cuts have
  happened).
- Automatic starts: space heating below the outdoor "O/D temp for heater ON" (default 0 degC, live reading
  0 degC; p.144); defrost if the backup heater is enabled (p.145); hot water when the tank target exceeds the
  heat pump reach, about 51 to 55 degC (p.26, 107, 137-138); the **weekly sterilization**, default 65 degC for
  10 min, a scheduled 3 kW load (p.150); anti-freeze below 6 degC (p.152); a forced heater on a heat pump
  error only if "Force Heater" = Auto (default Manual; p.144, 147-148). SMART DHW (default 20:00 to 05:00)
  can suppress reheat (p.152).
- The current 49 degC tank target (2026-10-30) is within heat pump range. Any DHW target above about
  51 degC starts the heater.
- Power-limit inputs exist (demand control 0-10 V, external compressor switch, SG-ready, CN-THERMO; most need
  the optional CZ-NS4P board; p.92-93, 98, 103, 159-163). The manual does not say demand control limits the
  heater. Whether any is fitted is unknown.
- Observed (read-only, 2023-11-08 to 2026-10-25): no heater state or heater power signal is exported, and the
  tank temperature never exceeded 51 degC, so no 65 degC sterilization or heater-boosted hot water is visible;
  the outdoor temperature was below 0 degC on only 11 days (minimum -3 degC).
- HeishaMon observation of 2026-10-30 ([regime log](regime-log.md)): the legionella/sterilization function is
  **off** (`Sterilization_State` 0; the configured 65 degC / 10 min is unused; the tank never exceeded 51 degC
  from 2023-11 to 2026-10); DHW heater lifetime hours **0**; room/backup heater lifetime hours **29**; the
  heater-on outdoor temperature is 0 degC. So the booster has never run, and the backup heater has run for
  space heating or defrost for 29 h in total, over an unknown period and not attributable to a day.
- **Heater sensors (read-only, added in homelab on 2026-10-30/31, recorded in Prometheus):**
  `binary_sensor.aquarea_sterilization_state`, `sensor.aquarea_sterilization_temperature`,
  `sensor.aquarea_sterilization_max_time`, `binary_sensor.aquarea_force_heater_state`,
  `sensor.aquarea_room_heater_operating_hours`, `sensor.aquarea_dhw_heater_operating_hours`,
  `sensor.aquarea_heater_on_outdoor_temperature`, `sensor.aquarea_heating_mode` (0 curve, 1 direct),
  `binary_sensor.aquarea_tank_heater_enabled`, `binary_sensor.aquarea_room_heater_enabled`,
  `binary_sensor.aquarea_internal_heater_state`, `binary_sensor.aquarea_external_heater_state`. Before
  them there was no heater history; it starts on those dates.
- **Decision (owner, 2026-11-05): the backup heater stays under the unit's own control.** The controller
  only writes setpoints and operating modes, never the heater. Evidence: under the legacy control the
  heater ran only inside defrost (6 min on 2026-11-04, 8 min on 2026-11-05, all within
  `binary_sensor.aquarea_defrost_active`). The one warning sign is heater minutes **outside** defrost (KPI
  below); the lever against it is fewer cold starts, that is the unit's minimum on and off times.
  Size: about 0.4 kWh per defrost event, about EUR 0.05 to 0.10 a day. The 02:10 local grid peak of 4.9 kW (5 kW
  contract) was defrost plus heater; the battery's 4.5 kW import guard covers an overlap with charging. Lever: fewer cold
  starts, `MIN_ON` 30 / `MIN_OFF` 20 min in the comfort controller. Follow-up: only about 6 days of heater data exist;
  recheck after a cold week.

## Calibrated on

Not fitted: the rules and `min_hours` were chosen by the owner. What exists is the observed behaviour:

- Legacy rule: hours below two thirds of the trailing mean price, else the cheapest `min_hours`; hot water
  forced in the cheapest hour of each 12 h block. Versions `climate_control@5af36d49`,
  `dhw_control@5af36d49` ([regime log](regime-log.md)).
- Observed regimes: S2 11 h; S3 10 h then 15 h from 2026-01-02. Water target 28 then 26 degC; setpoints
  23.0 in S3. Feature set `da6d678016ed`.
- Comfort history, hall-kitchen (local time, whole winters Nov to Mar, slots with a reading; S1 coverage 92%,
  S2 100%, S3 97 to 98%). Temperatures in degC:

  | | S1 | S2 | S3 |
  |---|---|---|---|
  | Setpoint mean (weekday 12 to 22) | 24.0 | 24.8 | 23.0 |
  | Weekday 12 to 22: mean / p10 / p90 | 23.4 / 22.0 / 24.4 | 23.4 / 22.4 / 24.5 | 22.2 / 21.0 / 23.2 |
  | Weekday 12 to 22: share at or above 23.5 / 25 | 56% / 5.0% | 47% / 0.7% | 2.9% / 0.2% |
  | Weekend 09 to 22: mean, share at or above 23.5 | 23.7, 71% | 23.6, 59% | 22.2, 4.5% |
  | Night 00 to 07: mean | 23.1 | 22.9 | 21.7 |
  | Share of time below 21: bedroom / hall-kitchen | 6.4% / 5.2% | 2.5% / 1.5% | 15.5% / 12.3% |
  | Bedroom p10 | 21.5 | 21.8 | 20.4 |

  The 23.5 degC target of the comfort schedule was met about half the weekday afternoons under the S1 and
  S2 setpoints and almost never under S3 (setpoint 23.0), where the bedroom spent 15.5% of the winter below
  21 degC. Reproduce: `uv run python hack/comfort_history.py --feature-set da6d678016ed
  --data-dir <store>` (writes `<store>/analysis/v1/comfort-history/<run-id>/`).
- Comfort evidence is an **association**: 15 h days against 10 h days in S3 show about 12% against 39% of
  time below 22 degC for +16% heating electricity ([climate plan](../planning/climate-optimizer-implementation.md),
  [replay](../planning/system-replay.md)). Months apart, so confounded.

## Assumptions

- Mode command path `input_select.heishamon_heatmode` is reliable (years of operation, owner decision).
- The pump runs a fixed direct water target; the stored heating curve is inactive
  ([C7-F](../planning/climate-flow-calibration-optimization.md)).
- Setpoints are manual and high enough that comfort-room valves stay open.
- The heater stays off in normal operation, apart from a few minutes a day inside defrost (hot water at or below about 50 degC, outdoor above 0 degC); the
  weekly sterilization, the one scheduled exception, is observed off on 2026-10-30 (the facts above).
- The comfort schedule ([ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md): hall-kitchen at least 23.5 degC from 12:00 on weekdays and from 09:00
  at weekends, all rooms at least 21 degC) can be met by optimum start with the fewest kWh. Untested:
  the schedule shape was never replayed.

## Invalidated when

- Any change of rule version, `min_hours`, enable flag, water target, curve or setpoint (log it first).
  A changed hour selection also needs a matching `structured_forecast.py` replay update.
- The comfort controller goes live, the legacy rule is retired, the DHW rule changes (cheapest 2 h by
  price / COP, at most 50 degC) or boost starts: the load shape changes for every consumer listed above.
- The heater facts change (tank target above about 51 degC, sterilization or "Force Heater" enabled, a
  power-limit input fitted, a model difference from the manual).
- A fourth price reactor appears: there are three by design (battery optimiser, DHW scheduler, boost);
  the legacy rule or a battery with native price features would be extra.

## Recalibrate by

There is no fit; the check is measurement. After a change: wait 28 days, then
`uv run battery-optimizer build-features --signal-set climate` and `uv run python hack/climate_audit.py`
on the new window. Acceptance: all rooms stay at or above 21 degC, the hall-kitchen reaches 23.5 degC at
the scheduled time, and kWh per heating degree-day and Kh below 22 degC are no worse than the 15 h days
(replay: about 0.75 kWh/HDD, about 1.15 Kh/day).

## KPIs

Observability only; the controller is the legacy AppDaemon rule in the homelab repository, so the KPIs come from
Home Assistant's own `hass_*` metrics. The thresholds are the section-B criteria of the
[preregistration](../planning/climate-preregistration-2026-27.md#3-b-comfort-controller-beforeafter-design-section-12);
the comfort controller will be judged on the same series. `{R}` stands for
the matcher `entity=~"climate.thermostat_(bedroom|hall_kitchen|office_t)"`. Dashboard: `Climate: heat-pump controller`
(uid `climate-heat-pump-controller`).

| KPI | Where |
|---|---|
| Tracking: room temperature minus setpoint, K | `max by (entity) (hass_climate_current_temperature_celsius{R}) - max by (entity) (hass_climate_target_temperature_celsius{R})` |
| Each room below 21 degC (at most 3% of time) | `avg_over_time((max by (entity) (hass_climate_current_temperature_celsius{R}) < bool 21)[1d:5m])`; offline `uv run python hack/comfort_history.py` |
| Hall-kitchen below 23.5 degC in the weekday day window (at most 10%) | `avg_over_time((max(hass_climate_current_temperature_celsius{entity="climate.thermostat_hall_kitchen"}) < bool 23.5 and on() (hour() >= 11 and hour() < 21 and day_of_week() >= 1 and day_of_week() <= 5))[7d:5m])` (UTC 11 to 21 is local 12 to 22 in winter time, an hour off in summer time) |
| Heating electricity per heating degree-day (at most +20% against the S3 baseline) | `sum(increase(hass_sensor_energy_kwh{entity="sensor.aquarea_heat_energy_consumed"}[1d])) / ((15 - avg_over_time(max(hass_sensor_unit_celsius{entity="sensor.aquarea_outside_ambient_temperature"})[1d:5m])) > 1)` (unit sensor, which reads low while running); offline `hack/climate_audit.py` and the replay's about 0.75 kWh/HDD |
| Compressor starts per day (median at most 20) | `sum(increase(hass_sensor_state{entity="sensor.aquarea_compressor_start_stop_counter"}[1d]))` |
| Rule activity: heating and hot-water hours against `min_hours` | `count_over_time((max(hass_sensor_power_w{entity="sensor.aquarea_heat_power_consumed"}) > 0)[1d:5m]) / 12` (and `dhw`) against `hass_input_number_state{entity="input_number.appdaemon_climate_min_hours_per_day"}`; `hass_input_boolean_state{entity="input_boolean.appdaemon_climate_enable"}`; `sum(increase(hass_automation_triggered_count_total{entity="automation.appdaemon_climate_watchdog"}[1d]))` |
| Heater safety (booster stays off) | `hass_binary_sensor_state{entity=~"binary_sensor.aquarea_(internal_heater_state\|external_heater_state\|force_heater_state\|sterilization_state)"}`; `sum by (entity) (increase(hass_sensor_unit_h{entity=~"sensor.aquarea_(room\|dhw)_heater_operating_hours"}[7d]))` (hot-water heater hours unchanged) |
| Backup heater minutes outside defrost (target 0 a day; early November 2026: 0 outside defrost, 6 to 8 min a day inside it) | `sum_over_time(((max(hass_binary_sensor_state{entity=~"binary_sensor.aquarea_(internal\|external)_heater_state"}) > bool 0) * on() (1 - max(hass_binary_sensor_state{entity="binary_sensor.aquarea_defrost_active"})))[24h:1m])`; dashboard panel `Heater minutes outside defrost`; the homelab alert `HeatPumpHeaterOutsideDefrost` fires on heater on and defrost off for 5 minutes (decision in the heater facts above) |
| Tank never above 50 degC | `max_over_time(max(hass_sensor_unit_celsius{entity="sensor.aquarea_tank_temperature"})[1d:5m])` |

Not yet measured: the share of hot-water energy inside the scheduled window (the window is not a metric), the
band-matched comparison with the S3 baseline (offline only), and what the controller decided and why (planned
hours, the mode it commanded): the legacy rule exports only the helpers above and the successor is not built.

## Status

**valid (legacy rule), being replaced** (2026-10-30; successor designed 2026-10-31, **planned**, merge
gated on the battery working). Space heating is off for the season; the comfort controller is planned
([regime log](regime-log.md#planned-not-yet-real-dates-to-be-filled-in)).
