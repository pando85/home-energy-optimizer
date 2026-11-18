# Load Forecaster — improvement plan

> **Parked on 2026-10-25 (owner decision).** LF-0 (`trailing_mean`) stays the forecaster in the
> service. The code built here stays in the repository, and the passive archives keep collecting.
> Why, what is kept and when to resume: [§6](#6-decision-parked-2026-10-25).
>
> **Re-checked on 2026-11-18, after the battery went live:** the plan stays parked and LF-0 stays.
> A perfect load forecast is worth about 1 % of the battery saving under value-based load-following
> ([§7](#7-re-check-after-the-battery-went-live-2026-11-18)).

- **Status (2026-10-25):** P0, P2 and P3 are done; P2 failed G2, so LF-0 stays the reference. The P1 forecaster seam (`load_forecast.py`) and the
  P1 evaluation harness (`forecast-eval`, see
  [Benchmark reference](../benchmark.md#forecast-evaluation-forecast-eval)) are built. The P0 run
  is done and its results are private. P4 is built and evaluated: **G4 failed**, with the gain
  concentrated in the cooling season and on days whose prices are published, and a loss in the
  heating season (see §P4). A **second look** with the OMIE price estimate and a disabled-day fix
  **failed G4 again**: it repairs the 14:00 mask but not the heating loss, which sits in the hot-water
  part (see §P4, second look). The plan refines the
- **Question:** can a better household load forecast reduce the planned battery's net cost, and by
  how much? Answer it with the least machinery first. Stop as soon as the evidence says the remaining
  gain is not worth the maintenance.
- **Scope:** offline evaluation first. Then a **forecast-only** shadow in the existing service, which
  needs no battery. Read-only throughout: no actuation and no new write credentials. Raw and
  predicted series stay in the private data dir, and no monetary figure goes into this repo.

## Progress

| Phase | Status | Where | Gate |
|---|---|---|---|
| P0 headroom | **Done** | `perfect` oracle in #14; private run `forecast-eval/p0-*` | G0: owner threshold pending |
| P1 harness | Seam **done** (#14); harness in review (#16) | `load_forecast.py`, `forecast-eval` | G1: seam passed (bit-identical, leakage test) |
| P2 cheap candidates | **Done, G2 failed** | this document, §P2 | G2 |
| P3 sub-load audit | **Done** | #15: `build-features`, `load_decomposition.py`; private feature set and P3 report | **G3 passed** |
| P4 structured forecaster | **Done, G4 failed twice** | `structured_forecast.py`; private runs `forecast-eval/p4-structured-*` and `p4b-second-look-*` | G4 failed: cooling holds (2026 only in the second look), heating never |
| P5–P7 | **Parked** (P5 collection continues passively) | §6 | — |

All phases run on the refreshed private dataset `dd5a2178…`, which covers 2024-01-01 to 2026-10-01.
Its rows for 2024–2025 are identical to the canonical `2e01620b…`, so 2026-01…09 can serve as a
second evaluation period.

## 1. Where we start

**LF-0 (production and benchmark):** `forecast_load_trailing_slot_mean`. For each 15-minute slot
position it takes the mean of the same position over the previous 28 local days, cut at the
decision's local midnight. It uses no weekday, holiday, temperature or schedule information.

Evidence already on record:

| Finding | Source | Implication |
|---|---|---|
| Ranking windows by MAE gives the opposite order to ranking them by dispatch cost (7 d best MAE, 28 d cheapest; on the frozen holdout, 28 d best MAE, 365 d cheapest) | [progress §Forecast experiments](energy-baseline-progress.md#forecast-experiments), [experiments §11](energy-baseline-forecast-experiments.md) | Never select a forecaster on MAE. EUR is the primary metric. |
| About 24 % of planned discharge is clipped because measured load fell below the forecast | [progress](energy-baseline-progress.md) | Forecast error does reach execution, but its EUR value has never been isolated. |
| The ordering of `rolling_horizon` against `day_ahead` flipped with the load-forecast basis (frozen vs causal) | progress, *bounded pre-migration milestone* | The ladder is sensitive to the load basis, so the experiments need paired comparisons. |
| The 81-arm grid has no pinned evaluation code | progress, *provenance blast radius* | New runs must stamp code and config identity. |

**Signals verified in long-term Prometheus on 2026-10-25 (read-only):**

| Signal | Series | History from | Notes |
|---|---|---|---|
| Main meter | `sensor.shellyem_main_channel_1_*` | 2024-01 | Already the canonical gross load (battery absent). |
| Servers | `sensor.shellypmmini_servers_power` (`hass_sensor_power_w`) | 2024-01-29 | 30-day mean about 215–245 W. That is consistent with the owner's "~250 W" estimate, a little lower. |
| Heat pump, electrical input | `sensor.aquarea_{heat,dhw,cool}_power_consumed` | 2024-01-29 | Split into heating, DHW and cooling. The total `sensor.aquarea_power_consumed` only exists from 2025-12-10. |
| Outdoor temperature | `sensor.aquarea_outside_ambient_temperature` (`hass_sensor_unit_celsius`) | 2024-01-29 | Measured, not forecast. Using it as a feature for future slots leaks future information (see P4). |
| AppDaemon controls | `input_boolean.appdaemon_climate_enable`, `input_number.appdaemon_climate_min_hours_per_day`, `input_boolean.appdaemon_dhw_enable`, `switch.aquarea_force_dhw_mode` | 2024-01 (DHW enable from 2025-03) | At 00:00:40, `climate_control` takes every hour priced below ⅔ of the trailing 7-day mean, or the cheapest `min_hours_per_day` hours if that gives fewer. It runs in `Heat+DHW` **or `Cool+DHW`** mode (`input_select.appdaemon_climate_heat_mode`, which has no value series), and the off mode is `DHW`. `dhw_control` forces DHW at the cheapest hour of each 12-hour block. Both rules are price-driven and can be replayed (see P3 and P4). |
| Washer energy | `sensor.terrace_lavadora_energy_today` (from 2026-07), `sensor.lavadora_cycles` / `_energy_yesterday` (from 2025-11) | see notes | Daily washer energy labels for P5. `sensor.smart_plug_v2_terrace_power` is **not** the washer: it reported only between April and July 2024, with small loads. |
| Appliance enums | `sensor.lavadora_*`, `sensor.lavavajillas_*` (status, remaining, delayed start/end) | HA recorder only, extracted since 2026-10-15 | Prometheus has only `hass_entity_info` for the enum states, so history is short and has to be accumulated. |

## 2. Phases

```
P0 headroom ──► P1 harness ──► P2 cheap baselines ──► P4 structured ──► P6 ML/quantiles ──► P7 shadow
                   ▲                                    ▲   ▲               ▲
P3 data audit ─────┴────────────────────────────────────┘   │               │
Price Outlook PO-A/PO-B (GA failed: persistence) ───────────┘               │
P5 appliance data (passive accumulation starts now) ────────────────────────┘
```

P0 and P3 can run in parallel. P5 only collects data until it has enough history. Each phase ends
at a gate. Failing a gate stops the plan there, with the simpler forecaster kept and the negative
result recorded.

### P0 — Headroom: what is perfect load knowledge worth? (≈1 day)

The aim is to bound the prize before building anything.

- Add an offline-only `load_forecast_mode: perfect`. It feeds the measured load of the target day
  to `day_ahead` and `rolling_horizon` as the forecast. Prices, battery, SOC seed, terminal value
  and solver stay identical. The service must reject this mode.
- Run `causal` (LF-0) against `perfect` on the full 2025 year and on 2026-01…09.
- Report:
  - the EUR gap per year, per month and per season;
  - the change in clipped charge/discharge kWh;
  - the per-day distribution of the gap, to see whether it comes from a few days or from everywhere;
  - which slots carry it (price tercile × hour).
- **Gate G0:** the owner writes down a minimum worthwhile annual gain before seeing the result. If
  the whole gap is below it, stop after P2, because only near-zero-cost tweaks are justified. Every
  later phase reports its result as a **share of this gap captured**.

**Result (2026-10-25; amounts private).** Runs were made for 2025 and for 2026-01…09, under
`day_ahead` and `rolling_horizon`, with exact prices.
- Perfect load knowledge is worth **about a fifth of the battery's net saving** over the
  no-battery baseline, and the share is similar in both periods and under both policies.
- The gap is **broad**. It is positive on most days, the ten largest days carry only about
  13–17 % of it, and every month contributes.
- It sits in the **most expensive price tercile, mainly 18:00–21:00 local**. A perfect forecast
  discharges more into the evening peak, partly by charging in different cheap slots.
- Clipped charge and discharge disappear entirely under perfect load.
- LF-0's WAPE is about 47 % per slot, with negligible bias.
- G0 is pending the owner's threshold, which stays outside the repo.

### P1 — Evaluation harness (≈2–3 days)

This makes every candidate cheap to compare and impossible to leak future data.

- **Forecaster seam (built):** `load_forecast.run_forecaster(name, load, usable, days, cutoff_idx,
  target_days)` returns the forecast with provenance. A non-oracle forecaster sees only read-only
  history before the cutoff. LF-0 (`trailing_mean`) is the first implementation, and `perfect` is
  the P0 oracle. Select a forecaster with `--load-forecaster` in the benchmark CLI and with
  `planning.load_forecaster` in `service.yaml` (default `trailing_mean`; oracles are rejected).
  To add a candidate, write one `@register_forecaster` function. Planner and MPC internals stay
  unchanged.
- **Walk-forward folds:** use monthly folds from 2025-01 to 2026-09, which gives 21 folds covering
  every season twice. Each fold fits only on data before each decision's cutoff. Decision instants
  are the policy's real events: 00:00 for `day_ahead`, and 14:00 and 20:30 for `rolling_horizon`.
- **Forecast metrics:**
  - MAE, WAPE, bias and P95 absolute error;
  - each of those by horizon bucket (0–6 h, 6–24 h, 24–36 h), price tercile, weekday/weekend and season;
  - a **price-weighted error** `Σ price·|error|` as a cheap EUR proxy. Check that it agrees with real EUR before trusting it.
- **Economic metric:** paired EUR difference against LF-0 on identical days, with a **weekly block
  bootstrap** 95 % interval, plus the share of the P0 gap captured.
- **Provenance:** every run stamps the git commit, dirty flag, config hash, dataset id and
  forecaster name and parameters. This closes the provenance gap in the 81-arm grid.
- **Pre-registration:** a short file in the private run dir lists the candidates, folds and
  thresholds before the first candidate runs.
- **Gate G1:** LF-0 passed through the seam reproduces the canonical five-policy run bit-for-bit
  (golden digest), and a leakage test passes. The test perturbs every post-cutoff value and checks
  that the forecast does not change.

### P2 — Cheap statistical candidates, no new data (≈2 days)

All of these are variations of the existing function:

| Candidate | Idea |
|---|---|
| `window_N` | N ∈ {7, 14, 28, 56}, re-run on the folds rather than one year |
| `median` / `trimmed_mean` | Robust to occasional appliance spikes; the median is the MAE-optimal point estimate |
| `ewma_h` | Exponentially weighted over days, half-life h ∈ {3, 7, 14} |
| `daytype` | Separate profiles for weekday, Saturday and Sunday/holiday. Holidays come from the `holidays` package (Spain plus a configured subdivision). |
| `level_adjusted` | Profile × (last 24 h measured ÷ last 24 h profile), clipped. At 14:00 and 20:30 events it also uses today's observed slots. |
| `blend` | Weighted mix of recent and same-season-last-year profiles |

**Gate G2 (pre-registered in each harness run):** the primary policy is `rolling_horizon` and the
secondary is `day_ahead`. A candidate passes only if, under the primary policy and in **both**
periods (2025 and 2026-01…09):
- the 95 % weekly-block-bootstrap interval of its paired daily saving against `trailing_mean` lies
  entirely above zero, **and**
- its point estimate is at least 10 % of that period's P0 gap.

The best passing candidate becomes the new reference for P4. Otherwise LF-0 remains the reference.

**Result (2026-10-25, private run `forecast-eval/p2-candidates-*`, PR #19):**
- 15 parametrisations were evaluated (`trailing_mean` windows 7/14/56, `trailing_median` 14/28,
  `trailing_trimmed_mean`, `ewma` half-lives 3/7/14, `daytype` N 4/6/8, `level_adjusted`, `blend`
  0.5/0.75). **None passes G2.**
- Closest: `trailing_median` (28 days) passes both criteria in 2026-01…09 (about 14 % of the gap)
  but is slightly negative in 2025 with an interval including zero. With 15 comparisons and no
  multiplicity correction, the near-pass would not survive one anyway.
- Most candidates land within about ±10 % of the P0 gap with intervals including zero; `blend` and
  the 56-day window are worse than LF-0. The best candidate captures only about 5 % of the summed
  gap. The median family's gain comes from a lower, biased profile in the expensive tercile.
- MAE and price-weighted error rank candidates almost identically (correlation 0.97–0.99) and agree
  with EUR on the top three, so they are fit for shortlisting but not for deciding.
- **Conclusion:** cheap reshaping of the same profile is exhausted; the gap needs new information
  (P3/P4 sub-loads). **LF-0 stays the P4 reference**; `trailing_median` is reported beside it as a
  secondary, non-gating comparison.

### P3 — Sub-load data audit and extraction (≈2 days, parallel with P0–P2)

This phase finds out which part of the load is predictable and where the error comes from.

- Extend `extract_prom.ENTITY_SPECS` with:
  - servers power;
  - heat-pump heat, DHW and cool consumed power;
  - outdoor temperature;
  - the AppDaemon enable, min-hours and DHW switch series.

  Write them to a **separate feature table** (`analysis/v1/features/`) so the canonical `dataset_id`
  and the golden runs stay unchanged.
- Quality checks:
  - units and sample cadence;
  - gaps, using the same quality mask as the meter;
  - `servers + heat_pump ≤ main` per slot, with a tolerance and a count of violations;
  - that `*_consumed` is electrical input and not thermal output.
- **Decomposition report** (private): for each season, the share of kWh and the share of LF-0 error
  variance that come from servers, the heat pump and the residual. Error during expensive slots is
  reported separately.
- **Gate G3:** P4 goes ahead only if the heat pump (or another observable component) carries a
  material share of the error. If the residual dominates, skip to P5 or P6.

**Result (2026-10-25, #15; tables in the private P3 report).**
- **Delivered:** 15 entities registered in `ENTITY_SPECS`, none of them default extraction. They
  were extracted for 2024-01 to 2026-10. The `build-features` subcommand writes a separate
  slot-aligned feature table with its own manifest.
  - **Integration rule:** each sample's power is held until the next sample. A span longer than
    300 s counts as a gap. Coverage of 0.8 or more is `partial`; below that the slot is `unknown`
    (null, never zero).
  - `build` now ignores manifests that contain only feature entities, so extracting them never
    changes the canonical `dataset_id`.
- **Quality:**
  - Every series is sampled every 60 s.
  - The produced/consumed ratios (heat ≈ 4.5, DHW ≈ 2.9, cool ≈ 5.1) confirm that `*_consumed` is
    electrical input.
  - `servers + heat_pump ≤ main` fails in 0.16 % of slots. The causes are main-meter dropouts in
    March 2024 and one stuck heat-pump reading in December 2024, and those slots are excluded from
    the decomposition.
- **Decomposition of LF-0 error:**

  | Component | Share of error variance | In the expensive tercile |
  |---|---|---|
  | Heat pump | about 65 % | about 61 % |
  | ↳ DHW | about 32 % | |
  | ↳ heating | about 18 % | |
  | ↳ cooling | about 15 % | |
  | Residual | about 34 % | |
  | Servers | about 0 % | |

  A perfect heat-pump forecast would remove about half of the price-weighted error. Its share is
  55–70 % in every season.
- **G3 passed:** the heat pump is observable and carries the majority of the error.
- **Correction from P4 planning:** the P3 report treated cooling as unscheduled. In fact
  `climate_control` schedules cooling as well. See P4 for the replay of the full rule.

### P4 — Structured component forecaster (LF-1, planned 2026-10-25)

`forecast = servers + heat_pump + residual`. The residual model is trained on
`main − servers − heat_pump`, which prevents double counting.

**Evidence from P3 and the rule replay (private numbers in the P3 report):**
- The heat pump carries about two thirds of LF-0's error variance, both overall and in the most
  expensive tercile. Servers carry almost none.
- `climate_control` schedules **both heating and cooling**. `input_select.appdaemon_climate_heat_mode`
  switches between `Heat+DHW` and `Cool+DHW`, while the off mode is `DHW`. The real rule takes every
  hour priced below ⅔ of the trailing 7-day mean price, and falls back to the cheapest
  `min_hours_per_day` hours when there are fewer than that. Replaying it on enabled days places
  about 97 % of heating energy and about 98 % of cooling energy inside the selected hours, which
  cover about half the day. The simplified "cheapest N hours" rule captures less.
- So **when** the heat pump runs is almost fully known as-of. **How much** it uses per selected hour
  is the hard part.
- On disabled days cooling also runs under manual control, and DHW follows its 12-hour rule only
  partly.

**Steps:**

| Step | Component | As-of inputs | Model |
|---|---|---|---|
| P4a | Schedule mask | Published PVPC for the day, trailing 7-day mean price, `min_hours_per_day`, enable flag, mode (heat or cool) inferred from the last 48 h of heat vs cool consumption, because the mode select has no value series | Exact replay of `climate_control`, versioned against the AppDaemon source and validated against observed heat/cool consumption |
| P4b | Energy per selected hour | Mask, outdoor temperature (as-of), local hour, block length and position, number of selected hours, mode | Degree-hour or binned regression per mode, fitted walk-forward per monthly fold (owner choice: simple first; LightGBM stays in P6) |
| P4c | DHW | `dhw_control` 12-hour cheapest-slot replay plus trailing profile of the remainder | Rule-hour energy plus profile |
| P4d | Servers and residual | `main − heat_pump` | P2 reference (LF-0, since G2 failed); `trailing_median` as a secondary variant |
| P4e | Fallbacks | Rule disabled, mode unknown, prices or temperature stale | Heat-pump part falls back to LF-0, with the reason recorded in provenance |

**Owner decisions (2026-10-25):**
- **Temperature:** backtest with persistence, i.e. the same hour yesterday as measured. Start
  archiving HA `weather.forecast_home` hourly forecasts now, so a real forecast can be evaluated
  later. That needs the `ha_guard` allowlist to permit the read-only `weather.get_forecasts` call.
- **No extra climate signals for now.** Leave out thermostat setpoints, zone request temperatures and
  compressor frequency, and add them only if P4b underperforms.
- **Model:** simple regression first.
- **Dependency:** at the 14:00 decision tomorrow's PVPC is not published, so tomorrow's schedule is
  unknown. The owner wants this solved properly in the
  [Price Outlook](components/price-outlook.md) first. That means PO-1, decision-time publication
  snapshots, and PO-2, an early next-day PVPC estimate.
- **Resolved after PO-A (2026-10-25):** the dispatch value of tomorrow's prices at 14:00 is tiny and
  gate GA failed, so P4 does not wait for an OMIE estimator. P4a replays the rule on **published**
  prices where they exist (today, and tomorrow after publication) and on `persistence` prices for
  tomorrow before publication, flagged as estimated. If that 14:00 guess turns out to cost material
  money, Price Outlook PO-C is restarted.

Passive collection shipped in 0.3.0: the weather-forecast archive and the PVPC publication
snapshots.

- **Gate G4:** the paired EUR result beats the P2 reference with a bootstrap interval that excludes
  zero, in at least one heating and one cooling season. A mid-run change to the AppDaemon rule is
  handled by versioning, not by refitting silently.

**Status (2026-10-25): built and evaluated, G4 failed.** The forecaster is `structured` in
`structured_forecast.py` (design in the
[Benchmark reference](../benchmark.md#forecast-evaluation-forecast-eval) and the module docstring).
It is offline only: the service refuses it, because it needs the sub-metered feature table.

What was built, against the steps above:

| Step | What it does |
|---|---|
| P4a | Exact replay of `climate_control`, versioned by effective date against the homelab commits it mirrors (10-day price window until 2026-04-27, 7 days after; negative prices accepted from 2026-09-05). Published prices wherever they are public at the decision, the `persistence` outlook for tomorrow at 14:00 (recorded `estimated`). Mode from the last 48 h of measured heat against cool energy. Enable flag and `min_hours_per_day` as of the decision |
| P4b | One ridge regression per mode, refit per monthly fold: piecewise-linear in the persisted outdoor temperature (same clock hour of the latest of the last three days measured), block position, start-up term, hours selected. The in-mask prediction is scaled by the measured-over-predicted ratio of the last 7 enabled days (chosen on 2024 data, before the evaluation periods) |
| P4c | `dhw_control` replay (one forced hour per day from 2025-03-16, per 12-hour block from 2025-12-29), trailing mean by hours since the forced hour and quarter of the hour |
| P4d | Residual forecaster is a parameter (default `trailing_mean`), trained on `main - heat pump` |
| P4e | Fallback to the LF-0 trailing mean of the component, per target day, with the reason recorded and counted |

**Result (private run `forecast-eval/p4-structured-*`; relative figures only).** Reference LF-0, since
P2 passed no candidate. Primary policy `rolling_horizon`; seasons were pre-registered as heating =
Nov to Mar, cooling = Jun to Sep, one season instance per period.
- **G4 fails.** Cooling holds in both periods, with the interval entirely above zero: about 4 % of
  that period's P0 gap in 2025 and about 8 % in 2026. Heating holds in neither: about -4 % in 2025
  (interval includes zero) and about -7 % in 2026 (interval entirely below zero). Over a whole period
  the two cancel: -1 % and +0.5 % of the P0 gap, intervals include zero. Under `day_ahead` the signs
  are the same (cooling +6 % and +12 %, heating -2 % and -8 %).
- **Accuracy is better, money less so.** WAPE falls from about 47 % (LF-0) to 41-39 % under
  `day_ahead` and 43-42 % under `rolling_horizon`. The gain is large in cooling months (WAPE about
  48 % to 33-36 % in August) and absent in heating months. The heat-pump climate component alone
  falls from about 100 % WAPE (LF-0 on the same component) to about 70 %. The hot-water component
  is **worse** than LF-0 (about 131 % against 125 %).
- **The replay is right; the 14:00 price guess is not.** The mask holds 98-99.5 % of measured climate
  energy on days with published prices, but only 76-83 % on days planned on `persistence` prices,
  which is tomorrow at 14:00 in 96-100 % of the events. On those days the heat-pump forecast is no
  better than LF-0. This is the trigger the Price Outlook plan set for restarting PO-C.
- **Secondary comparisons** (not gated): against `trailing_median` the structured forecaster is level
  in 2025 and clearly worse in 2026 under `rolling_horizon`. With the residual forecast by
  `trailing_median` the cooling gain grows (about 5 % and 15 % of the P0 gap) and the heating loss
  stays.
- **Next:** restart PO-C and re-run P4 on an OMIE-based tomorrow estimate; or pre-register a
  season-conditional use (structured in cooling mode, LF-0 in heating mode); add climate signals
  only if heating stays weak. The owner chose the first and a fix of the disabled-rule days, with
  the hybrid reported as a candidate: see the second look below.

**Second look (owner decision 2026-10-25).** G4 failed for two identified causes: the 14:00 schedule
guess (tomorrow's PVPC is unpublished, so the replay ran on `persistence` prices and held only
76-83 % of the climate energy) and the days on which the rule is disabled (the fallback was the
LF-0 trailing mean of every day, which still carries the climate energy of the enabled days before
it; in 2024 it forecast about 15 times the energy measured on the 99 disabled days that were
checked). The owner chose to build PO-C (the OMIE estimator, see the
[Price Outlook plan](price-outlook-improvement-plan.md)), fix the disabled-day fallback, and re-run
P4 **once**. **2025 and 2026-01...09 were already seen in the first P4 run, so this is a second
look, not independent evidence; a pass is provisional until it is confirmed on forward data in the
forecast-only shadow (P7).** What was built (all off by default, so the first P4 results
reproduce):

| Change | Option of `structured` | Chosen on 2024 data only |
|---|---|---|
| The 14:00 schedule replay uses the PO-C estimate of tomorrow's prices | `price_outlook=omie_period` | the estimator's 28-day fit window |
| A day whose rule is disabled takes the climate part from recent *disabled-state* days | `disabled_fallback=disabled_history` | 28 samples within 60 days, zero when there is none |
| Seasonal hybrid: the climate part is the LF-0 trailing mean on the days of a season | `climate_lf0_season=heating` | none: the split is the G4 seasons |

- **Disabled-day design (2024).** On the 99 disabled days of 2024 whose data was complete (the
  rule is off for April-May and September-October), the slot-wise mean of the last K disabled days
  within L days had a per-day absolute error of 0.18-0.28 kWh, against 2.06 kWh for the LF-0
  trailing mean, whose forecast total was about 15 times the measured one. Longer histories
  scored slightly better, because disabled-day energy in 2024 is close to zero. K = 28 with L = 90
  was lowest and K = 28 with L = 60 within 3 % of it, so **K = 28, L = 60** was kept (the shorter
  look-back wins ties; the rule was stated after looking at the 2024 table). A plain "zero when
  disabled" had a lower 2024 error still (0.13 kWh per day), because 2024 disabled days drew almost
  nothing, but it was not adopted: the disabled-state history also follows manual cooling, which
  2024 hardly shows. No row of 2025 or 2026 was read to choose any of this.
- **Hybrid split, fixed before the run.** Heating = Nov-Mar: the climate part is LF-0 even when the
  rule is enabled. Cooling = Jun-Sep: structured. **Shoulder months (Apr, May, Oct) run the
  structured model** (they are mostly rule-disabled days, handled by the fallback). A disabled day in
  any season takes the disabled fallback first. Only the climate part changes: hot water and the
  residual are the same as in the primary candidate.
- **Pre-registered candidates** (`forecast-eval`, written to `preregistration.json` before any run,
  with the second-look label and the provisional-pass note): primary = structured + `omie_period` +
  `disabled_history`; secondary = the hybrid and the `trailing_median` residual variant (both on the
  primary); reference = LF-0. Gate G4 is unchanged: the 95 % weekly-block-bootstrap interval of the
  paired `rolling_horizon` saving against LF-0 must lie entirely above zero in at least one heating
  and at least one cooling season instance (seed 20261025, 10,000 resamples). Beside them, as
  non-gating diagnostics, the first P4 candidate (a regression check that it still reproduces),
  OMIE only, disabled fix only (attribution), and LF-0 `trailing_median`.

**Second-look result (private run `forecast-eval/p4b-second-look-*`; relative figures only; shares are of
that period's P0 gap).**
- **G4 fails again.** Primary (structured + OMIE + disabled fix), `rolling_horizon`, season instances:
  cooling about +2.5 % (interval includes zero) in 2025 and **+9.6 % (interval above zero)** in 2026;
  heating about -3.8 % (interval includes zero) in 2025 and -7.2 % (interval entirely below zero) in 2026.
  Whole periods -2.4 % and +2.6 %, intervals include zero. The first P4 run had cooling holding in both
  years; in 2025 it no longer does (the disabled-day fix costs about 2 % of the gap there and gains about 1 %
  in 2026).
- **The two diagnosed causes are fixed as accuracy.** The share of measured climate energy inside the
  replayed hours on estimated-price days rises from 76 % and 83 % to **92 % and 94 %** (published days stay at
  98-99 %); the climate WAPE on those days falls from about 100 % to 83 % and 71 %; every 14:00 event now has an
  estimate (13 fallbacks to `prices_unavailable` in 2025 are gone). Whole WAPE under `rolling_horizon`: 43.5 % to
  42.0 % (2025), 42.1 % to 39.0 % (2026), against 46.7 % and 46.0 % for LF-0. Against the first P4 candidate the
  primary is about 2.1 % of the gap better in 2026, with an interval above zero, and level in 2025
  (about -1.7 %, interval includes zero).
- **But the money does not follow, and heating is not climate.** OMIE alone moves the paired saving by less than
  0.3 % of the gap. The seasonal hybrid (LF-0 for the climate part in Nov-Mar) is **worse** than the primary in
  heating, in both years (about -2.4 % and -1.1 % of the gap), so the heating loss is not in the climate part. The
  only component the hybrid leaves structured in heating is hot water, which was already worse than LF-0 in P4 and
  still is (WAPE about 134 % against 125 % in 2025, 153 % against 148 % in 2026). That is an inference from
  the paired figures, not a separate run.
- **Secondary variants.** The hybrid fails G4 (heating -6.3 % and -8.3 % of the gap against LF-0). With the
  `trailing_median` residual, cooling in 2026 gains about 16 % of the gap (interval above zero) and heating loses
  about 8 %.
- **Candidate, not a result.** A pure seasonal hybrid, LF-0 for every component in heating and the structured
  forecaster in cooling, would by construction give about zero in heating and the cooling figures above, but it was
  not what was run and those figures were seen first. Per the owner's decision it needs forward confirmation in the
  P7 shadow before any adoption; so does replacing the hot-water part.

### P5 — Appliance-aware signals (LF-2, collect now, evaluate later)

- Data accumulates passively starting now. Keep `extract_ha` running for the washer and dishwasher
  enums, because the recorder only keeps 8 days of lookback. Confirm the terrace plug is the washer
  so it can serve as an energy label.
- **The highest-value feature is a *known future start*.** A dishwasher `delayed_start` or a washer
  `delayed_end` that has been programmed is a load at a known time and fully available as-of. The
  next most useful is the remaining energy of a cycle already running. Probabilities of unknown
  future starts come last.
- Evaluate once there are about 3 months of cycles. The split is session-disjoint, and results are
  reported separately for short horizons (0–6 h), where most of the benefit is expected.
- **Gate G5:** same EUR rule as G4. Classification accuracy or MAE alone do not count.

### P6 — ML challenger and quantiles (LF-3, only if gap remains)

Start only if P2–P5 leave a large share of the P0 gap uncaptured.

- LightGBM on:
  - slot of day, day type and holiday;
  - lags at 24 h and 168 h;
  - temperature (as-of forecast);
  - the replayed schedule mask;
  - appliance states.
- Train with quantile objectives for P10/P50/P90.
- Only P50 feeds the deterministic MPC. Quantiles are reported for calibration and kept for a future
  risk-aware planner. Wheels exist for amd64 and arm64, but adding the dependency to the image needs
  owner sign-off.
- **Gate G6:** improvement over P4 holds across folds and seasons, and P10/P90 coverage is within
  ±5 points. Otherwise keep the simpler model.

### P7 — Forecast-only shadow and promotion (LF-4, ≈2 days + 4 weeks)

**This phase does not need the battery.** Forecast accuracy can be shadowed live today.

- The service computes LF-0 and the candidate on each plan. Only the configured forecaster feeds the
  planner.
- Once each slot's measurement is in, record realized errors. Expose them as
  `battery_optimizer_load_forecast_abs_error_kwh{forecaster,horizon_bucket}` and add a Grafana panel.
- Fall back to LF-0 when any input is stale or missing (temperature, schedule replay, appliance
  state). The fallback reason goes on the existing provenance and is exported as a counter.
- After 4 weeks of shadow in which the live error ordering matches the offline one, promote by
  changing `planning.load_forecaster` in `config/service.yaml`, re-vendored into the homelab chart.
  Rollback is the reverse one-line change.
- **Revalidate after battery commissioning:** gross load becomes
  `import − export + discharge − charge`. Re-run the P1 harness on the post-commissioning data
  before trusting the forecaster on it.

## 3. Decision gates summary

| Gate | Pass condition | If it fails |
|---|---|---|
| G0 | P0 gap ≥ owner's predeclared minimum | Do only P2, then stop |
| G1 | LF-0 through the seam is bit-identical; leakage test passes | Fix the harness before any candidate runs |
| G2 | A P2 candidate's EUR bootstrap interval is entirely better than LF-0's | Keep LF-0 as reference |
| G3 | An observable component carries material error | Skip P4 |
| G4 | P4 beats the reference with an interval excluding zero, in ≥2 seasons | Keep the P2 reference |
| G5 | Appliance features add EUR on top of the reference | Keep collecting, or drop |
| G6 | ML is stable across folds and quantiles are calibrated | Keep the simpler model |
| G7 | 4-week live shadow is consistent with offline results | Do not promote |

## 4. Risks

- **Small prize:** the battery is 5.12 kWh, and a perfect forecast may be worth little. P0 exists to
  find this out first.
- **Behaviour drift:** occupancy, new servers and changes to AppDaemon rules. Mitigated by walk-forward
  folds, rule versioning and the live shadow error metric.
- **Temperature leakage:** only as-of forecasts are allowed, and the leakage test covers feature
  inputs as well as load.
- **Dataset churn:** sub-load features live in a separate table, so canonical runs stay reproducible.

## 5. Owner decisions needed

1. ~~G0 minimum worthwhile annual gain~~ — decided 2026-10-25: **no EUR floor**. Any gain whose
   95 % paired bootstrap interval lies above zero in both evaluation periods is worth keeping. The
   pre-registered G2 share criterion (at least 10 % of the P0 gap) still applies to P2 as recorded.
   **P2 pick rule:** if several candidates pass G2, the highest paired `rolling_horizon` saving
   averaged over both periods becomes the P4 reference, and a tie goes to the simpler candidate.
2. ~~The temperature source for P4~~ — decided: persistence for the backtest, plus archiving the HA
   weather forecast (see P4).
3. ~~Whether the terrace plug meters the washer~~ — P3: it does not. It reported only in
   April–July 2024 and its loads are small. Washer energy labels come from
   `sensor.terrace_lavadora_energy_today` and the Home Connect counters instead.
4. ~~The holiday subdivision for `daytype`~~ — decided: Spain national holidays only.
5. Whether LightGBM may be added as an image dependency (P6 only).

## 6. Decision: parked (2026-10-25)

**Decision.** After the P4 second look the owner parked the plan. LF-0 (`trailing_mean`, 28 days)
stays the configured forecaster (`planning.load_forecaster` in `config/service.yaml`). The code is
kept and documented, not reverted.

**Why.**
- **Small prize.** P0 says a *perfect* load forecast is worth only a modest amount per year with the
  planned 5.12 kWh battery.
- **No gate passed past P3.** No cheap reshaping of the profile passed G2. `structured` failed G4
  twice. The 14:00 schedule guess was repaired by the OMIE estimate (climate energy inside the
  replayed hours on estimated-price days rose from about 76–83 % to about 92–94 %), but the money
  did not follow.
- **What is left is seasonal and needs forward data.** The only gain is in the cooling season. It is
  about 10 % of the P0 gap in summer 2026 (interval above zero) and is not significant in summer 2025.
  The heating season loses, mostly in the hot-water part, whose forecast is worse than LF-0's.
  A seasonal hybrid (LF-0 in heating, `structured` in cooling) was formulated only after seeing the
  results, so it may not be adopted without confirmation on unseen data. The next unseen cooling
  season starts in June 2027. Confirming it live needs P7: running `structured` in the service
  with live sub-metered features and an OMIE fetch. That engineering is out of proportion to the
  expected gain today.

**What is kept.**

| Piece | Where | State |
|---|---|---|
| Forecaster seam, oracle, P2 candidates | `load_forecast.py` | Registered; `trailing_median` and the others are selectable offline |
| Structured forecaster (LF-1) with second-look options | `structured_forecast.py` | Offline only; the service refuses it |
| Evaluation harness with pre-registration, G4 seasons and secondary comparisons | `forecast_eval.py`, `battery-optimizer forecast-eval` | Reusable as is |
| Price-outlook seam, `persistence`, OMIE estimators | `price_outlook.py`, `omie_outlook.py`, `price_eval.py` | `omie_period` passed gate GC |
| OMIE day-ahead source with a backfill from 2024-01-01 | `extract --source omie`, `omie_prices.py` | Run by hand; not in the CronJob |
| Sub-load feature table and decomposition | `features.py`, `load_decomposition.py`, `build-features` | Reusable as is |
| PVPC snapshots and HA weather-forecast archive | service `archive:` (0.3.0) | **Live**, collecting into `service/v1/` |

**Resume when any of these holds.**
1. **Spring 2027 (about May).** Pre-register a pure seasonal hybrid that uses LF-0 for every component
   in heating months and `structured` with `omie_period` in cooling months. Then build P7 so
   that June–September 2027 is the unseen confirmation season. Use the archived weather forecasts
   instead of persistence temperature if they are better.
2. **The real battery appears in Home Assistant.** Its size and the post-commissioning gross load
   change the prize, so re-run P0 first, as the P7 "revalidate after commissioning" note says.
   *Evaluated 2026-11-18 ([§7](#7-re-check-after-the-battery-went-live-2026-11-18)): the prize is
   about 1 % of the battery saving, so this does not reopen the plan.*
3. **The household or the AppDaemon rules change materially**, for example a new heat pump schedule, a
   new large load, or a tariff change. Re-run P0 and P2.
4. **A cheap hot-water fix is wanted.** Test LF-0 for the hot-water component inside `structured`.
   Run it pre-registered, against a held-out period.

**How to resume.** Read §P4 (both runs), the private findings `p4-findings.md` and `p4b-findings.md`
in `forecast-eval/`, `po-c-findings.md` in `price-eval/`, and the Price Outlook plan. Refresh OMIE
with `extract --source omie` before any run that uses `omie_period`.

## 7. Re-check after the battery went live (2026-11-18)

Resume trigger 2 of §6 (the battery is commissioned) was evaluated with read-only experiments on the
live regime, 2026-11-04 to 2026-11-17. Raw series and run outputs stay in the private data dir.

**Value of a perfect load forecast.** A rolling-LP replay re-planned every 15 minutes, executed
against the minute-level real load with the value-based load-following of the service and the PVPC
prices. It approximates the service, it is not the service.

- A perfect load forecast is worth about **1 % of the battery saving** under value-based
  load-following, within the replay's own error. The same replay under the old plan-SOC following
  rule gives about 9 %, and the P0 study of 2026-10-25 gave about a fifth.
- Why: load-following absorbs timing errors in real time, and the usable battery energy (about
  3.8 kWh) is smaller than the 18:00 to 22:00 load, so the battery empties into the peak whatever the
  forecast says.
- The heat pump carries about 70 % of the error variance, but a perfect heat-pump forecast alone is
  worth a fraction of that 1 %. A bias-corrected LF-0 changes nothing.
- Replay fidelity: about 4 % above the realised saving in the second week and 28 % in the first;
  daily correlation 0.82. It ignores derate, idle drain, measured efficiency, PD-loop lag, dwell cost
  and the soft cap.

Reproduce: `uv run python hack/load_forecast_value_replay.py --from 2026-11-04 --to 2026-11-18
[--run-id <name>]` (`--to` is exclusive; read-only against Prometheus; about 1 to 2 minutes with the
variants, `--skip-variants` for the six forecast and perfect runs). It prints the gap per rule, by
hour bucket and week, the error split into heat pump and rest, the sensitivities and the
replay-vs-realised fidelity. The terminal value is the window's mean price to the cent
(`--terminal-value` overrides it) and the wear cost is the shipped `planning.throughput_cost_eur_per_kwh`;
the sensitivities are terminal value -20 % and +20 %, wear 0 and twice. The heat-pump hybrids use the
package's LF-0 kernel on the local day grid (the 25-hour clock-change day aligned by position, as in
the service), which moves them by a few tenths of a percent of the saving against the ad-hoc run.

**Cheap candidates (P2 again, 2026-08-01 to 2026-11-17).** Every 15-minute issue time, horizons 15
minutes to 24 hours, price-weighted MAE against LF-0.

| Candidate | Result |
|---|---|
| Nowcast anomaly correction (best: 15-minute window, tau 60 min) | -16 % at 0 to 1 h only, about 0 beyond; worse in 18 to 21 h and after the clock change |
| `trailing_median`, 28 days | -4 %, interval includes zero |
| UTC-aligned history | -2.6 % overall; -33 % in 18 to 21 h in the weeks after the autumn change (-16 % in the spring 2026 replicate), transient for about 4 weeks after each change |
| Ceiling with the heat pump known | all heat pump -50 %; hot water only -21 % overall and -44 % in 18 to 21 h; heating only -3 % (the heating season has barely started: re-check with winter data only if wanted) |

Reproduce: `uv run python hack/load_forecast_candidates.py --preset autumn-2026 [--run-id <name>]`, and
`--preset spring-2026` for the replicate (2026-03-17 to 2026-05-14). It prints the MAE and
price-weighted MAE per horizon bucket against LF-0 for the whole window and each sub-period, the
bootstrap intervals over local weeks, `--check-gross` the power-vs-counter check of the gross load
and `--diag` the hot-water profile before and after the clock change. Any other window:
`--from <local day> --to <UTC end of issue times> [--period NAME=START[,END]]`.

**NILM feasibility ([ADR-0002](../adr/0002-appliance-aware-load-forecasting.md)).**
- Data: the main meter is stored only at 60 s, the `sensor.aquarea_*_power_consumed` sensors update
  every 300 s, and `shellyem_main_channel_2` is a branch inside channel 1 that feeds the heat pump,
  washer and dishwasher. `circuit_1_power` (from 2026-10-31) is about gross minus channel 2.
- Residual episodes (2026-09-15 to 2026-11-02): cooking (hob and oven, median about 1.4 kW, about 17
  minutes, about 1.4 per day) is about 12 % of the residual energy overall and about 22 % in 18 to 21
  h, and explains about 90 % of the 18 to 21 h residual variance. Washer and dishwasher are identified
  with high confidence through channel 2 and Home Connect. Overlap is rare.
- Forecast probe: cluster identity or Home Connect flags give no gain over persistence at 15 to 120
  minutes. Since 2026-10-15 there were 5 dishwasher and 11 washer cycles, and delayed starts never
  fell in 18 to 21 h.
- Verdict: NILM is not worth building for forecasting. Observability is achievable without it
  (channel 2 minus the heat pump, Home Connect states, a cooking threshold on the rest). A hob and
  oven circuit meter would be the only ground truth for the dominant evening load.

Reproduce: `uv run python hack/load_nilm_probe.py --from 2026-09-15 --to 2026-11-03 --cycles-from
2026-10-15 --cycles-to 2026-11-18T10:20 --recent [--inventory] [--run-id <name>]` (`--cycles-to`
defaults to now, so a later run lists later cycles). It prints the episode inventory and energy
accounting, the washer and dishwasher cycles, the persistence-vs-identity probe and, with
`--inventory`, the sensor cadences. The "about 90 %" of the 18 to 21 h residual variance holds for
the hob and oven sessions together with the kettle-like bursts and the mid-power long episodes
(91 %); the hob and oven session elements alone give 81 %.

**A lever found elsewhere.** The unit-side hot-water run at about 18:15 to 18:35 local on 6 of 7
days (fixed in UTC, source unverified; [DHW price scheduling](dhw-price-scheduling.md) section 2) is
real peak consumption that the battery cannot cover. Moving it is a load-shift question, not a
forecast one.

**Conclusion.** LF-0 stays. Resume trigger 2 is evaluated and does not reopen the plan. Reopen it
only if load-following is switched off or the plant changes materially, for example a larger battery.
