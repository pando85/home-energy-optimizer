# Data-driven tuning of the discharge margin

- **Status:** Proposed. The owner approved the design on 2026-11-05; implementation has not started. Merging this plan
  authorises nothing on the installation: Step 1 is an observability PR reviewed on its own, Step 2 needs a separate,
  explicit owner OK.
- **Date:** 2026-11-05
- **Decisions:** [ADR-0007](../adr/0007-live-battery-control.md) (live control; Step 2 would need a note or amendment).
- **Related:** [known issue](../known-issues.md#the-pd-loop-oscillates-the-discharge-margin-was-too-small), [planned
  discharge not delivered](../known-issues.md#planned-discharge-is-not-delivered-open-waiting-for-data-from-2026-11-04),
  [actuator](../service.md#live-control-dry_run-and-live).
- **Code touched (Step 1):** `service/actuator.py` (or the runtime path that feeds it), `service/metrics.py`,
  `docs/service.md` (metrics table). Homelab (`pando85/homelab`): the rule file and the dashboard (no Alertmanager change).
- **Privacy:** aggregate figures only; no energy totals, costs or addresses.

## 1. Context

In `discharge` the actuator sets Omnibattery's PD grid target to `actuation.discharge_margin_w`, so the grid keeps
that much import and the battery never exports. The margin covers the **oscillation of the PD loop around its target**.
It does not cover sudden load drops: those export for the loop's few-second reaction time whatever the margin, and their
size scales with the discharge power.

Margin history:

| Date | Margin | Why |
|---|---|---|
| commissioning | 50 W | code default; brief exports of -10 to -94 W, PD swing of +-150 to 200 W |
| 2026-11-04 | 200 W | first committed value |
| 2026-11-05 | 100 W | owner decision, evidence below |

**Evidence for 100 W.** 2-second Home Assistant grid samples over about 17 h of live discharge on 2026-11-04/05 at the
200 W target: median 192 to 195 W, 1st percentile 131 to 151 W, below 0 W in 0 to 0.04% of the samples. Replaying 100 W
on the same data gives about 0.2% of the time below zero and about 1 Wh/day exported, and frees about 0.5 to 1.2 kWh/day
of discharge in low-load hours. Omnibattery's `pd_controller_deadband` is 40 W, so the practical floor is about 50 to
75 W. Each further 25 W step is worth only a few EUR per year: **lower it only on clean data.**

**Existing alerts** (homelab, `apps/battery-optimizer/rules-service/actuator.yaml`); these are the "raise it" signals:

- `BatteryGridExport` (warning): more than 5 Wh exported in 24 h, from the Shelly's device-integrated returned-energy
  counter.
- `BatteryGridExportSustained` (critical): more than 10 Wh in 15 min.

**Limitation.** The control loop and both alerts rely on the same Shelly meter. The independent cross-check is the
utility meter's hourly export data (Datadis or the distributor portal), checked by hand once a week after each margin
change.

## 2. Step 1 (to build): measure and suggest

Observability only: no change to the plan, the plan hash or the actuation, no new write, `ha_guard.py` untouched.

### 2.1 Service metrics

The service already receives every grid-power `state_changed` from Home Assistant (about every 2 s), whereas Prometheus
scrapes HA once a minute, so the distribution is measured in the service. Names follow the existing
`battery_optimizer_actuator_*` family (`dry_run`/`live` only, like every `actuator_*`):

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `battery_optimizer_actuator_discharge_margin_watts` | gauge | none | the configured `discharge_margin_w` |
| `battery_optimizer_actuator_discharge_grid_watts` | histogram | `power_band` | measured grid power at every grid-power update while the actuator is **live, in control, guard ok and in discharge mode** |
| `battery_optimizer_actuator_export_seconds_total` | counter | none | seconds with grid power below 0 W while discharging, integrated between consecutive updates |

- Buckets of the histogram (W): -500, -200, -100, -50, -25, 0, 25, 50, 75, 100, 125, 150, 200, 300, 500, +Inf.
- `power_band` is the measured battery discharge power: `lt500`, `500_1000`, `ge1000` (W). Low morning and high evening
  discharges behave differently (an export after a load drop is bigger at high power), so a single pooled histogram
  would hide it.
- The gauge is a scrape-time family like the other actuator gauges; the histogram and the counter are event instruments
  (like `actuator_writes_total`), updated from the grid-power state path. The integration of `export_seconds_total`
  adds the interval to the previous update only when both samples are fresh, so an HA outage does not count as export.

### 2.2 Suggestion rule `BatteryMarginCanBeLowered`

In the same homelab rule file, evaluated by prometheus-long-term every minute, `for: 1h`, all conditions together.
**Sketch**: validate with `promtool check rules` and against live data when implemented.

```promql
(changes(max(battery_optimizer_actuator_discharge_margin_watts)[7d:5m]) == 0)
and on()
(count_over_time(max(battery_optimizer_actuator_discharge_margin_watts)[7d:5m]) > 1900)
and on()
(max(battery_optimizer_actuator_discharge_margin_watts) > 75)
and on()
(
  increase((max by (entity) (hass_sensor_energy_kwh{entity="sensor.shellyem_main_channel_1_energy_returned"}))[7d:1m])
  <= 0.007
)
and on()
(
  histogram_quantile(
    0.001,
    sum by (le) (increase(battery_optimizer_actuator_discharge_grid_watts_bucket[7d]))
  ) >= 25
)
and on()
(sum(increase(battery_optimizer_actuator_discharge_grid_watts_count[7d])) > 50000)
```

| Condition | Meaning |
|---|---|
| `changes(max(margin)[7d:5m]) == 0` | seven full days at the current margin |
| `count_over_time(max(margin)[7d:5m]) > 1900` | the margin series covers the whole window (7 d / 5 m = 2016 points). **Added to the approved conditions** so the rule cannot fire right after the metric first appears, when `changes() == 0` is trivially true |
| `max(margin) > 75` | not at the floor |
| `increase(returned energy)[7d:1m] <= 0.007` | at most 7 Wh (kWh counter) exported in 7 days, about 1 Wh/day |
| `histogram_quantile(0.001, ...) >= 25` | the lowest 0.1% of grid samples are still at least 25 W import |
| `sum(increase(..._count[7d])) > 50000` | enough discharge samples (about 28 h at 2 s) |

PromQL notes:

- Every operand is an aggregate or a subquery of an aggregate, so each is a one-element instant vector; the energy term
  keeps the `entity` label, hence `and on()` (match on no labels) throughout. No `scalar()` is needed and none is used,
  so a missing series gives an empty result and the rule stays silent: the failure mode is "no suggestion".
- The quantile is computed on the buckets summed over `power_band`. A per-band variant
  (`sum by (le, power_band)`, one alert per band, suggest only when all bands with enough samples agree) is an open point.
- A subquery at 5 m resolution can miss a margin that was changed and reverted within 5 minutes; a service restart
  with a new value always shows as a change.

**Why the margin-unchanged condition.** It restarts the 7-day clock after every change, so the rule judges only data
collected at the current margin and stops firing once the margin is lowered.

Annotation: "Lower `actuation.discharge_margin_w` by 25 W (to <current minus 25> W)". Labels: `severity: warning`
(see [2.3](#23-delivery)).

The rule is the machine-readable form of the [shared definitions](#25-shared-definitions-step-1-and-step-2) below;
Step 2 reuses them unchanged.

### 2.3 Delivery

Alertmanager routes `severity: info` to the `null` receiver, so an info alert would never reach the owner. That is why
`BatteryMarginCanBeLowered` is `severity: warning`: it takes the **existing default route to the `telegram` receiver**
with the default `repeat_interval` of 12 h. **No new Alertmanager route and no change to
`system/monitoring/resources/alertmanager-secret.yaml`.** Result: one Telegram message when the conditions have held
for an hour, a repeat every 12 h while they still hold, and it resolves by itself when the margin changes (the 7-day
clock restarts).

A Grafana panel on the battery-optimizer dashboard shows the margin, the export in Wh/day, the 0.1% quantile per
power band and the rule state.

### 2.4 Acting on it

Stays manual: the owner (or Claude, on the owner's OK) lowers the margin by 25 W in homelab
`apps/battery-optimizer/files/service.yaml`. Record each change in the [regime log](../components/regime-log.md) and
check the utility meter's hourly export by hand a week later.

| Signal | Action |
|---|---|
| `BatteryMarginCanBeLowered` fires | lower by 25 W |
| Export is 1 to 5 Wh/day, or the quantile is near 0 W | stay |
| `BatteryGridExport` on 2 or more days (real export events, section 2.5) | raise by 25 to 50 W |
| `BatteryGridExportSustained` fires once | raise by 25 to 50 W |

### 2.5 Shared definitions (Step 1 and Step 2)

One definition each, used by the rule above, the decision table and the Step 2 controller. Step 2 must not redefine
them: the manual rule is the automatic controller's input.

- **Real export event:** export measured while discharging, on a day without a Home Assistant outage, beyond the
  thresholds of the existing alerts of section 1 (read from the Shelly returned-energy counter, cross-checked with
  `battery_optimizer_actuator_export_seconds_total`): more than 5 Wh in 24 h, or more than 10 Wh in 15 min. A day
  with an HA outage is excluded both from export events and from clean days.
- **Clean 7 days:** seven full days at the current margin (the margin series unchanged and present for the whole
  window), with at most 7 Wh exported (about 1 Wh/day), the lowest 0.1% of discharge-time grid samples at or above
  25 W import, and enough samples (more than 50000), i.e. exactly the conditions of `BatteryMarginCanBeLowered`.
  The 7-day clock restarts at every margin change.

## 3. Step 2 (later, needs the owner's explicit OK): automatic margin

**This is the documented future automation path.** The owner wants it recorded because it may be automated later. It is
Step 1's manual rule with the owner removed from the loop: the same [shared definitions](#25-shared-definitions-step-1-and-step-2)
of a *real export event* and of *clean 7 days* feed the controller, with no redefinition.

The actuator adapts the margin itself inside fixed bounds of **75 to 200 W**:

- **+50 W immediately** after a real export event (section 2.5);
- **-25 W after clean 7 days** (section 2.5), the criteria of the rule of section 2.2.

No new device permission: the margin is the value already written to the PD grid target, within the existing bounds of
the ADR-0007 allowlist. Nothing is widened (same entities, same services, same value bounds). It does change live
control behaviour, so it needs an ADR-0007 note or amendment and tests, and it is decided around **early December
2026**, after Step 1 has shown that the metrics are reliable. Until then the alert and the manual change of section 2.4
are the only path.

## 4. Open points

- Histogram bucket choice (resolution near 0 to 100 W against series count).
- Whether to exclude the first minutes after a mode change (charge to discharge) from the histogram and the counter.
- Cadence of the manual utility-meter cross-check (weekly after a change, then monthly?).
- Whether the quantile condition should be evaluated per `power_band`.
