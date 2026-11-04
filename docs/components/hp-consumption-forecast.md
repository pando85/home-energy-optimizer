# Heat-pump consumption forecast (C6, optional HP-plan interface, LF-1)

## Purpose and owner

Tells the battery forecaster how much electricity the heat pump will draw and when, so the battery does
not learn it by lag. Two pieces: **LF-1 `structured`** (built, offline: replays the legacy AppDaemon rule,
`structured_forecast.py`) and the **HP-plan interface** (C6, specified here, not built). Owner: this
repository for the consumer side; the plan would be published by the homelab controller
([heat-pump-controller](heat-pump-controller.md)).

## Interfaces

- **LF-1 in:** prices, as-of outdoor temperature, climate features; **out:** a load forecast through the
  forecaster seam. The service refuses it.
- **Interface (proposed, one read-only HA entity, no new endpoint):** `sensor.hp_planned_energy`, state =
  kWh planned for the next 24 h, attributes `schema_version`, `generated_at`, `valid_until`, `plan_id`,
  `meter_basis` (`ch2_branch` or `heishamon`, they differ by about 1.28), `slots[]` of
  `{start, kwh, kind: heat|dhw|boost}`, `boost`.
- **Battery side:** `gross_forecast = LF-0(ch1 - hp_measured) + sum(slots.kwh)`, falling back to plain
  LF-0 when the entity is stale, missing or past `valid_until`; the basis is recorded in `forecast_meta`.
  The residual subtraction must use the same meter basis the plan publishes (no double counting).
- **Consumer:** [battery-load-forecast](battery-load-forecast.md).

## Calibrated on

Nothing for the interface. LF-1 was evaluated on the legacy history only (periods of the
[load-forecaster plan](../planning/load-forecaster-improvement-plan.md)): it **failed gate G4 twice**; the heating season lost, mostly in the hot-water
part; only a cooling-season gain of about 10% of the P0 gap was seen. Rule versions
`climate_control@5af36d49` and `dhw_control@5af36d49`.

## Assumptions

- A perfect whole-house forecast is worth about a fifth of the battery's net saving
  ([load-forecaster plan](../planning/load-forecaster-improvement-plan.md)); the heat-pump share of that
  is smaller, and a clock-scheduled heat pump should be more slot-regular than the price-chosen one.
- Under a grid-target charge adapter forecast error becomes charge-energy error, not a safety problem,
  provided the target is capped below the import limit.

## Invalidated when

- **LF-1:** the legacy cheapest-hours rule is retired or its version changes. Its replay then describes
  a rule that no longer runs; keep the code for legacy-history evaluation only.
- **Interface:** the controller's plan format, meter basis or horizon changes; the heat-pump sub-meter
  changes; boost adds kinds the forecaster does not know.

## Recalibrate by

Decide on evidence, not by building: after 6 to 8 weeks of new-regime data run
`uv run battery-optimizer decompose-load --features <climate feature set dir> --dataset <dir>`
([CLI](../cli.md#decompose-load)) and read the heat-pump row of the price-weighted headroom. Predeclare
what share of the P0 gap counts as material before looking. Build the interface only above it; then
acceptance is a paired forecast-eval against LF-0 on a window after the build.

## KPIs

Observability only. The forecast is **not built** (the HP-plan entity is specified, not published), so there is
no forecast error to read. What is measurable today is the load it must predict and the bar a real forecast has
to beat: a naive "same 15 minute slot yesterday" baseline. Basis: HeishaMon power (`sensor.aquarea_power_consumed`),
which is not the branch meter (they differ by about 1.28). Dashboard: `Climate: planned components`
(uid `climate-planned`, row on the heat-pump consumption forecast).

| KPI | Where |
|---|---|
| Load to predict, by kind (heat, hot water, cooling) | `hass_sensor_power_w{entity=~"sensor.aquarea_(heat\|dhw\|cool)_power_consumed"}`; per day `sum(increase(hass_sensor_energy_kwh{entity="sensor.aquarea_heat_energy_consumed"}[1d]))` (and `dhw`, `cool`) |
| Baseline MAE and bias per 15 min slot, kWh (the bar to beat) | with `A = avg_over_time(max(hass_sensor_power_w{entity="sensor.aquarea_power_consumed"})[15m:1m]) / 4000`: `avg_over_time(abs(A - A offset 1d)[1d:15m])` and `avg_over_time((A - A offset 1d)[1d:15m])` (write `offset 1d` on the subquery) |
| Heat-pump share of the house load | `avg_over_time(max(hass_sensor_power_w{entity="sensor.aquarea_power_consumed"})[1d:5m]) / avg_over_time(max(battery_optimizer_actuator_house_load_watts)[1d:5m])` (different meter bases; needs the actuator in `dry_run` or `live`) |
| Consumer: whole-house load forecast error | `sum(increase(battery_optimizer_load_forecast_abs_error_kwh_total{plan="final"}[1d])) / sum(increase(battery_optimizer_load_forecast_scored_slots_total{plan="final"}[1d]))` (and `plan="day_ahead"`) ([battery-load-forecast](battery-load-forecast.md)) |
| LF-1 against LF-0 on legacy history (offline) | `uv run battery-optimizer forecast-eval ...` ([CLI](../cli.md#forecast-eval)); headroom of the heat-pump part `uv run battery-optimizer decompose-load --features <dir> --dataset <dir>` |

Not yet measured: the forecast's MAE and bias per slot, its daily energy error, its availability and age
(`valid_until`, fallback to LF-0), and the error by kind (heat, dhw, boost). They start with the interface; when
it is built, score them like the battery-load-forecast KPIs (per slot close, against the measured heat-pump
energy on the plan's `meter_basis`).

## Status

**planned** (2026-10-30): interface specified, not built. **LF-1 goes stale** the day the legacy rule is
retired; it is parked as a forward-looking forecaster either way.
