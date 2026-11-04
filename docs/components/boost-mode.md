# Boost mode (planned)

## Purpose and owner

In very cheap hours, heat the slab and the hot-water tank beyond the comfort need (a thermal soak), as the
only price-reactive behaviour left on the heat-pump side. Rare in winter, frequent in April to October on
weekends around midday. Decided in [ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md); **owner: the homelab repository**
(AppDaemon), with the battery side read-only. Not built. Phase 2 of the
[comfort controller design](../planning/comfort-controller-design.md#8-boost-phase-2-after-normal-mode-works):
built only after normal mode works, and after the house battery is working.

## Interfaces

- **In (AppDaemon):** the pre-tax PVPC sensor (same basis as the battery, state it in the homelab config),
  the Omnibattery SOC and charge-power sensors read-only, the current comfort and hot-water state.
- **Out:** heat and hot-water commands through the existing AppDaemon path; the hot-water boost target stays
  at or below about 50 degC (heat pump only, see Assumptions); optionally `kind: boost` slots
  in [hp-consumption-forecast](hp-consumption-forecast.md) (not needed: with the interlock the HP waits
  for the battery, so the battery plan for the window is already right).
- **Consumers:** [heat-pump-controller](heat-pump-controller.md); [battery-load-forecast](battery-load-forecast.md)
  sees boost only as smeared load.

## Calibrated on

Nothing. The [whole-system replay](../planning/system-replay.md) did not simulate boost; the S3 load it
used came from 15 h price-chosen days. The ordering argument is arithmetic only: the usable 3.84 kWh
fills in about 1.7 h at 2.5 kW, so a window of 3 h or more holds both.

How often such windows exist is measured on the realised pre-tax PVPC price (dataset `cdd448abb006`, usable
intervals 2024-01 to 2026-10, local time; unit prices in EUR/kWh; a qualifying day has a run of at least 3 h
at or below the threshold):

| | Heating months (Nov to Mar) | Other months (Apr to Oct) |
|---|---|---|
| Hours at or below 0 / 0.02 / 0.04 / 0.06 | 0 / 0 / 51 / 607 | 26 / 161 / 713 / 1817 |
| Hours below 25% of the day's mean | 15 | 336 |
| Qualifying days at 0.04 (weekday / weekend) | 7 (2 / 5) | 99 (17 / 82) |
| Median start and end of the longest run | 11:00 to 16:00 | 10:00 to 18:00 |
| Longest run, median / maximum | 5 h / 8 h | 7 h / 10 h |
| Minimum price | 0.0206 | -0.0126 |

Windows are weekend middays from April to October; in winter they are rare. The 26 h at or below 0 are 8 h
at exactly 0 (2024-04) and 18 h below 0 (2026-09). Reproduce: `uv run python hack/cheap_price_windows.py
--dataset <store>/analysis/v1/cdd448abb006d401854b193f7a240c367c92908ecfa75a76d76a413828e80e9d
--data-dir <store>` (thresholds are `--thresholds`, the run length `--min-run-hours`; writes
`<store>/analysis/v1/cheap-price-windows/<run-id>/`).

## Assumptions

- Battery first, then space-heating soak, then hot water last; the order comes from the heat pump
  waiting (soak starts at SOC about 90 to 95%, or charge power below a threshold, or
  `window_end - soak_duration`). Do not expect the battery to yield through grid-target control: it would
  yield to load, so the heat pump would effectively go first.
- Boost and the battery charge land in the same cheapest slots; the sum with the house load must stay under
  the 5.0 kW import limit.
- **The DHW boost must stay at or below about 50 degC (heat pump only).** The Panasonic service manual
  (p.26, 107, 137-138, 146) gives a heat-pump-only ceiling of about 51 to 55 degC (control reference 51 degC;
  "above 55 degC only possible with backup heater"). A target of 55 to 60 degC, as in ADR-0006 decision 3,
  starts the integrated 3.00 kW electric heater (p.25-26, 138, 146): heat pump plus heater is about 6.5 kW
  (heat pump alone at most 3.47 kW), above the 5.0 kW limit on its own and far above it with the battery
  charging. The smart meter cuts supply on overload, which has happened.
- The sterilization cycle (default 65 degC for 10 min, weekly, p.150) also uses the heater, so it is a
  scheduled 3 kW load that boost must not overlap with the battery charge or the soak.
- Legionella and pump hygiene cycles stay authoritative; every other price reactor is off.

## Invalidated when

- The import-limit basis turns out to be an instantaneous trip rather than a 15-minute average.
- The Omnibattery SOC entity, its grid-target semantics or its native price features change.
- The legacy cheapest-hours rule still runs, or a reactor beyond the three by design (battery optimiser,
  DHW scheduler, boost) appears. The DHW scheduler stays price-optimised (amended 2026-10-31), so it often
  shares the cheapest hours with boost and the battery charge.
- The heater facts above change (a firmware or model difference from the assumed-equivalent manual, a
  power-limit input fitted, "Force Heater" or the tank heater setting changed), or the hot-water target or
  the soak duration changes; the boost price basis
  differs from the battery's.

## Recalibrate by

Before the first boost, predeclare thresholds and the acceptance check; then measure on the first boost
days (log the heat-pump sub-meter, the battery counters, SOC and the main meter at 15-minute and
instantaneous resolution; [data-pipeline](data-pipeline.md) gross reconstruction must be in place).
Acceptance: no interval above the import limit minus the margin while the battery charges; the soak
starts after the battery reached its SOC trigger or the fallback time; comfort is not degraded the day
after; then rerun [battery-load-forecast](battery-load-forecast.md) with boost days flagged.

## KPIs

Observability only. Boost is **planned** (no rule, threshold or interlock), so there are no activation or soak
KPIs yet. What is measurable today are the limits it must respect, so a first boost day can be read against
them. Dashboard: `Climate: planned components` (uid `climate-planned`, boost row).

| KPI | Where |
|---|---|
| Import margin against the 5.0 kW limit (no interval above the limit while the battery charges) | `max(battery_optimizer_actuator_observed_grid_power_watts)` and its 15 min mean `avg_over_time(max(battery_optimizer_actuator_observed_grid_power_watts)[15m:1m])` against 5000 W (needs the actuator in `dry_run` or `live`) |
| Battery first: SOC and charge power the soak waits for | `max(hass_sensor_battery_percent{entity="sensor.venus_battery_soc"})`; `max(clamp_min(battery_optimizer_actuator_observed_battery_power_watts, 0))` |
| Hot-water cap (at most about 50 degC, heat pump only) | `max_over_time(max(hass_sensor_unit_celsius{entity="sensor.aquarea_tank_temperature"})[1d:5m])` |
| The 3 kW heater and the sterilization stay off | `hass_binary_sensor_state{entity=~"binary_sensor.aquarea_(internal_heater_state\|external_heater_state\|sterilization_state)"}`; `sum(increase(hass_sensor_unit_h{entity="sensor.aquarea_dhw_heater_operating_hours"}[7d]))` |
| Cheap windows that qualify (hours at or below a threshold, runs of 3 h or more) | offline `uv run python hack/cheap_price_windows.py --dataset <dir>`; no live metric: the forward price curve is a sensor attribute |
| Comfort not degraded the day after | the [heat-pump-controller](heat-pump-controller.md) comfort KPIs for the following day |

Not yet measured: boost activations per day, soak energy and duration, the SOC at soak start, the gap between
the soak start and the battery trigger, saved cost, and the battery-charge and soak overlap per slot. They need
the rule to export its decisions (a state and a kWh counter) when it is built.

## Status

**planned** (2026-10-31): no rule, threshold, interlock or code. Blocked on normal mode working, the
import-limit basis and the retirement of the legacy rule.

**ADR-0006 decision 3 amended 2026-10-31** ([amendment](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md#amendment-2026-10-31)):
the DHW step is at most about 50 degC, heat pump only, so the 3.00 kW heater never starts. The 5.0 kW
import limit is hard (the meter cuts supply), so the battery grid-target cap below the limit is mandatory
before go-live.
