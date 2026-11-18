# Price Outlook — improvement plan

- **Status (2026-10-25):** PO-A done, **gate GA failed**: knowing tomorrow's prices at 14:00 is worth
  very little to dispatch. PO-B shipped in 0.3.0 and is collecting. **PO-C was restarted the same
  day** because Load Forecaster P4 met its restart condition (the 14:00 schedule guess on
  `persistence` prices misses 17-24 % of the measured climate energy, see the
  [Load Forecaster plan](load-forecaster-improvement-plan.md)); it is built (`omie_outlook.py`) and
  evaluated, **gate GC passed** (see §PO-C). PO-D and PO-E stay **deferred**, and the whole plan is **parked** with the Load Forecaster plan (see its [§6](load-forecaster-improvement-plan.md#6-decision-parked-2026-10-25)). The MPC keeps planning on published prices only. **Re-checked 2026-11-18 on the live regime (§4): GA still fails; the MPC keeps published prices only.** The remaining gap is a terminal-value effect, parked with three candidate strategies until more data (§5). Refines the
  [Price Outlook roadmap](components/price-outlook.md) stages PO-1…PO-4 into executable phases.
- **Why now:** P4 of the [Load Forecaster plan](load-forecaster-improvement-plan.md) replays
  AppDaemon's price-driven heat-pump schedule. At the 14:00 decision tomorrow's PVPC is not
  published, so neither tomorrow's prices nor tomorrow's schedule are known. The owner decided this
  must be solved here first.
- **Question:** how much is knowing tomorrow's prices at 14:00 worth, and how close can a
  decision-time estimate get to it?
- **Scope:** read-only. Use public market data only. The MPC sees no estimated price until the
  shadow gate passes, and no monetary figure goes into this repo.

## 1. Where we start

| Fact | Evidence | Implication |
|---|---|---|
| PVPC is **hourly**, with no variation within an hour through 2026-09. REE publishes D+1 at about 20:35 (measured, see §4), and the HA integration fetches from 20:00 into `price_next_day_HHh`. Measured since 2026-10-25 (24 days), the `prices_tomorrow_published` flag flips at 20:19 earliest, 20:35 median, 20:46 90th percentile, 20:50 latest; the `pvpc_snapshots` archive agrees (20:18 to 20:49, 17 days). Still hourly on 2026-11-18: in the per-minute Prometheus series 2026-04-25 to 2026-11-18 the value changes only at minute :01 of the hour, in all of 4941 changes. | `service/pvpc.py`; dataset `dd5a2178…`; §4 | Between about 13:00 and about 20:35 (measured, see §4), tomorrow is unknown to the service. |
| The service plans **only up to the last published price** and then uses a terminal value. | `service/runtime.py` | The live 14:00 plan does not see tomorrow at all. |
| The canonical `rolling_horizon` rung gives the 14:00 event **exact** prices for tomorrow (σ = 0). | `rolling_horizon.py`, [benchmark](../benchmark.md) | The benchmark overstates what is achievable today. That rung is an upper bound, not the service's behaviour. |
| No archive records when a price was published. Prometheus keeps only the current-hour value, and the HA recorder keeps 8 days. | [roadmap PO-1](components/price-outlook.md) | The *published = realized* assumption is still unverified. Snapshots have to start being collected. |
| OMIE day-ahead marginal prices are **public files without auth** (`marginalpdbc_YYYYMMDD.1`). They are published at about 13:00 local for D+1, are **15-minute since 2025-10-01** (96, or 92/100 on DST days), and history goes back years. | Fetched 2026-10-25: D+1 was not yet present at 12:24 local | A realistic estimate source exists before the 14:00 decision. |
| **Feasibility probe:** PVPC ≈ β·OMIE + an intercept per tariff period (2.0TD P1/P2/P3), fitted on 2026-08 and tested on 2026-09. | Scratch analysis, private | Compared with persistence (yesterday's PVPC): MAE about halved, within-day rank correlation about 0.97 against 0.87, overlap of the cheapest 10 hours about 93 % against 81 %. Ordering, which is what dispatch and the AppDaemon rule depend on, is well preserved. |
| Earlier synthetic-noise grid: price-estimate noise cost about a tenth of the policy margin | [experiments §11](energy-baseline-forecast-experiments.md) | A 0.88-era result on synthetic noise. PO-A re-measures it on real data. |

## 2. Phases

```
PO-A headroom ──► PO-C OMIE estimator (offline) ──► PO-E shadow ──► (feeds Load Forecaster P4a)
PO-B snapshots (passive, start now) ──┘ (as-of times)    ▲
PO-D scenarios (optional) ───────────────────────────────┘
```

### PO-A — Headroom: what is tomorrow at 14:00 worth? (≈1–2 days)

- Add `rolling_horizon` price-information modes. The canonical default stays unchanged.
  - **`published_only`:** the 14:00 event plans only through the last published price and uses
    the terminal value. This is what the service does today.
  - **`exact`:** today's canonical behaviour, the upper bound.
  - **`persistence`:** tomorrow is estimated as today's prices, a naive estimate.
- Run 2025 and 2026-01…09 with the P1 harness machinery: paired daily EUR and a weekly block
  bootstrap.
- Report how often the extra hours change the 14:00–20:30 actions.
- **Gate GA:** the gap `published_only − exact` must be material, against an owner threshold kept
  private. Independent of the dispatch value, P4 still needs a tomorrow schedule at 14:00, so PO-C
  runs anyway, but GA decides whether the MPC should ever use the estimate.

**Result (2026-10-25, private run `price-eval/po-a-*`, `price_outlook.py`, `battery-optimizer price-eval`):**
- `exact` reproduces the canonical rung bit for bit; leakage tests show `published_only` and
  `persistence` never see an unpublished price.
- The gap `published_only − exact` is about **3 %** of the P0 load-forecast gap in 2025 (bootstrap
  interval above zero) and about **1 %** in 2026-01…09 (interval includes zero). The extra hours
  change the 14:00–20:30 actions on more than half of the days, but by very little money: the 20:30
  replan already sees the published prices.
- `persistence` captures about half of that gap in 2025 and almost none in 2026. As a price
  estimate it has negligible bias, a within-day rank correlation of about 0.85 (median) and about
  75 % overlap with the true cheapest 10 hours.
- **GA failed** (owner, 2026-10-25). An estimated tomorrow is not worth building for dispatch.

### PO-B — Publication evidence (start now, passive)

- The service appends a private snapshot of every `sensor.esios_pvpc` state change: raw
  attributes, `last_updated`, `observed_at`, and whether `price_next_day_*` is present. It goes
  into `data_dir/service/v1/` through the existing sink.
- From the snapshots, measure the actual publication time distribution and verify *published =
  realized* against the Prometheus realized series.
- This ships in the same release as the HA weather-forecast archive agreed in Load Forecaster P4.
  Both are read-only, and the weather archive needs the `ha_guard` allowlist to permit
  `weather.get_forecasts`.

### PO-C — OMIE-based next-day estimator (offline) — restarted and built 2026-10-25

> Deferred after GA on 2026-10-25 and restarted the same day: Load Forecaster P4 showed that its
> 14:00 guess of tomorrow's heat-pump schedule (from `persistence` prices) is what keeps the
> structured forecaster from winning in the heating season. The OMIE extract source and its
> 2024-01-01 onward backfill already existed (`extract --source omie`, `omie_prices.py`, as-of
> 13:30 local), so the work was the estimators and their evaluation.

**Built** (`omie_outlook.py`, registered in the price-outlook seam; evaluated by `price-eval`):

| Name | Built | Notes |
|---|---|---|
| `omie_period` | yes | `PVPC_hour = beta * mean(OMIE ES over the hour) + intercept(2.0TD period)`: one slope and one intercept per period (P3 is also weekends and the nine national fixed-date holidays), least squares on the hourly prices of the last 28 fully published local days |
| `omie_period_month` | yes | The same with a level offset per calendar month of a 120-day window, predicted at the latest month's level |
| `omie_quantile` | **no** | It exists to feed PO-D, which stays deferred after GA |

- **Seam.** An outlook registered with `needs_omie=True` receives the OMIE series as a side input
  (`OutlookSide`), cut by the seam at the first OMIE day not yet usable at the decision
  (`omie_end_idx`, D+1 from 13:30 local on D), as it cuts the published PVPC. Before 13:30 the event
  falls back to `published_only`. A missing side input, a missing OMIE quarter in a target hour, or
  fewer than 7 published days in the fit window leaves the slot `unavailable` (the
  `published_only` behaviour), with the reason recorded per event and counted in the telemetry.
- **Leakage tests** (`tests/test_omie_outlook.py`): an OMIE day that is not usable at the decision,
  and any realized PVPC after the publication boundary, cannot change the estimate; D+1's OMIE
  changes it from 13:30 and not before; the seam's cut and `omie_prices.omie_usable_from` are one
  rule; the dispatcher plans on the estimate and records the side input.
- **Fit windows chosen on 2024 data only, before the 2025 and 2026 evaluation was run.** On the
  274 days of 2024-04-01 to 2024-12-31 (14:00 events, price metrics of `price-eval`; no 2025 or 2026
  row was read for this): `omie_period` windows 14, 28, 56 and 91 days gave a price MAE within
  8 % of each other, the lowest at 28 days (14 days a close second); within-day Spearman and the
  cheapest-10 overlap were equal to two decimals, so **28 days** was kept. `omie_period_month`
  windows 56, 91 and 120 days gave MAE decreasing with the window, so **120 days** was kept (the
  longest the 2024 history allows before the first evaluated day, so the grid edge, not an
  interior optimum). On the same 2024 days `persistence` had an MAE about four times larger.
  The choice rule (lowest MAE; ties on rank metrics go to the shorter window) was stated after
  looking at the 2024 table, not before; it only used 2024 rows.

**Result (2026-10-25, private run `price-eval/po-c-*`, pre-registered with gate GC before any
benchmark ran; relative figures only).** 2025 and 2026-01...09, 14:00 events, OMIE complete over both
periods (no fallback event).

- **Gate GC passed** for both estimators: in both periods each beats `persistence` on within-day
  rank, on the cheapest-10 overlap, on the replayed climate-mask overlap and on the climate energy
  inside the replayed mask. The dispatch criterion of the gate is not applied because GA failed.
- **Price estimate.** The mean absolute error of `omie_period` is about half of `persistence`'s
  (51 % in 2025, 46 % in 2026), with a small bias (a slight over-estimate in 2026, under a quarter
  of the error). Within-day Spearman, mean (median): 0.95 (0.98) in 2025 and 0.93 (0.97) in 2026,
  against 0.70 (0.84) and 0.75 (0.87) for `persistence`. Overlap of the 10 cheapest hours: 93 % and
  92 % against 74 % and 76 %; of the 4 most expensive: 90 % and 90 % against 73 % and 77 %. On the
  four 2026 days with a negative price the error is about a third of `persistence`'s. On the three
  DST days of the periods the estimators are **not** better than `persistence` (a sample of three).
- **Overlap with the AppDaemon rule's hours (what P4 needs).** Replaying `climate_control` on the
  estimate against on the realized prices, over the 223-227 days per period with a schedule: mean
  hour overlap (Jaccard) 0.89 and 0.93 against 0.67 and 0.76 for `persistence`. The share of measured
  climate energy that falls inside the replayed hours is **92 % and 94 %** against **76 % and 83 %**
  for `persistence`; with the realized prices it would be 97 % and 98 %. So `omie_period` closes
  about three quarters of the gap between persistence and perfect price knowledge, in both periods.
- **Variants.** `omie_period_month` is indistinguishable on every metric (mask overlap 93 % and
  94 % of the climate energy, price error within 5 % of `omie_period`), so the simpler `omie_period`
  stays the one P4 uses.
- **Dispatch (information only).** In 2025 `omie_period` captures about 70 % of the GA gap, with an
  interval that touches zero; in 2026 it captures none, but that period's gap is itself
  indistinguishable from zero. GA stays failed: the estimate is for the load forecaster, not the MPC.

**Original spec (kept for reference).**

- **Data** (built before, PR #24):
  - A new `extract` source `omie` writes the public day-ahead files into the raw store with
    manifests and hashes, covering 2024-01 to now.
  - Parse the hourly files before 2025-10 and the quarter-hour files after it, map them to UTC
    slots, and handle DST days.
  - Record the as-of rule: an OMIE D+1 value may be used only from **13:30 local**. That is
    conservative until PO-B and the shadow measure the real fetch time.
- **Price-estimator seam:** mirror the load-forecaster seam. Registered estimators return slot
  prices with `published`, `estimated` or `unavailable` provenance per slot, and a published value
  is never overwritten.
- **Candidates:**

  | Name | Inputs | Idea |
  |---|---|---|
  | `persistence` | Today's PVPC | Baseline |
  | `omie_period` | OMIE D+1, 2.0TD period (weekday, hour, national holidays) | β·OMIE + intercept per period, fitted on a trailing window |
  | `omie_period_month` | Same plus month or trailing-level terms | Tracks drift in regulated components and in the futures-basket share |
  | `omie_quantile` | Same | P10/P50/P90 residual quantiles (feeds PO-D) |

- **Metrics:**
  - MAE, bias, and within-day Spearman rank correlation;
  - overlap of the cheapest N hours and of the most expensive 4 hours;
  - **overlap of the AppDaemon rule's selected hours**, which is what P4a needs;
  - errors on negative-price and DST days;
  - paired EUR in `rolling_horizon` with the estimate at 14:00, against `published_only` and
    `exact`, reported as the share of the GA gap captured.
- **Walk-forward:** monthly folds from 2025-01 to 2026-09, with each fit using only data before the
  decision. Pre-register before running.
- **Gate GC:** the estimate beats persistence on rank and rule-hour overlap in both periods, and if
  GA was material, it captures a meaningful share of the gap with a bootstrap interval above zero.

### PO-D — Scenario prices (optional)

This phase starts only if PO-C's error is concentrated on specific days, so that a point estimate
misleads the MPC. It produces coherent day-level scenarios that preserve within-day correlation.
They feed the MPC only once the Dispatch Optimizer has an approved scenario interface.

### PO-E — Live shadow (≈2 days + 4 weeks)

- The service fetches OMIE D+1 once a day, after publication, with retry and a staleness flag.
  Egress is unrestricted once the NetworkPolicy is removed (see §3).
- It computes the estimate and records it with provenance. At the next-day publication (about 20:35) it scores the
  estimate and exports `battery_optimizer_price_estimate_abs_error{metric}` along with rule-hour
  overlap metrics.
- The MPC keeps using published prices only. A config flag extends the horizon with estimated slots
  **only after** 4 weeks of shadow consistent with offline results. The P4a schedule replay may
  consume the estimate as soon as GC passes.
- Fallbacks: if OMIE is missing, stale or fails to parse, use the published-only horizon and P4's
  profile fallback, and record the reason.

## 3. Owner decisions

Decided 2026-10-25:
- **Source:** OMIE public files. No ESIOS token.
- **PO-B ships now**, together with the HA weather-forecast archive from Load Forecaster P4, as a
  service release and homelab deploy.
- **Egress:** the service may reach `www.omie.es`. The owner also questioned why the
  battery-optimizer namespace has a restrictive egress NetworkPolicy at all. First-party,
  controlled software in this cluster normally runs without one; network policies are kept for
  uncontrolled workloads such as LLM harnesses. The policy is removed in the homelab deploy that
  ships PO-B. The read-only guarantee stays in code: `ha_guard` and no write credentials.

- **Benchmark convention:** the canonical `rolling_horizon` rung keeps `exact` prices for tomorrow
  at 14:00, so it stays bit-identical and comparable with earlier results as the upper bound. The
  realistic modes, `published_only` and the PO-C estimate, are reported beside it. Once PO-C has
  measured how precise the estimate is, the owner decides which realistic mode becomes the headline
  and reworks what is needed.

- **GA (after PO-A):** failed. PO-C and everything after it are deferred; P4 uses published prices
  where they exist and `persistence` for tomorrow at 14:00.

## 4. Re-check on the live regime (2026-11-18)

Read-only experiments, private outputs in the data dir / scratch; relative figures only.

**Value of tomorrow's prices at 14:00 (PO-A again).** Rolling-LP replay (the one of the
[load forecaster plan §7](load-forecaster-improvement-plan.md#7-re-check-after-the-battery-went-live-2026-11-18):
re-plan every 15 minutes, value-based load-following, executed against real load, LF-0 forecast
load), modes `published_only` (horizon to the last published slot, then the terminal value), `exact`
from 14:00, `omie_period` from 13:30, `persistence` from 14:00.

- Live window 2026-11-04 to 11-16 (13 days, minute execution): `exact` is worth about 5 % of the
  `published_only` saving; `omie_period` and `persistence` each capture about 97 % of that gap. 3
  days better, 2 worse, 8 equal: too few weeks for an interval.
- Longer window 2026-08-01 to 11-16 (106 days, slot execution, gross load before the battery):
  about 1.3 % with a constant terminal value (weekly-block bootstrap interval above zero);
  `omie_period` captures about 82 % and `persistence` about 86 %. With the service's terminal value
  (trailing mean price) the gap is about 0.6 % and its interval **includes zero**; `exact` is
  insensitive to the terminal value, `published_only` is not.
- Knowing tomorrow before 14:00 adds nothing over 14:00 (`exact` vs always-known differ in a handful
  of slots).
- Mechanism: emptying the battery more in 18 to 22 h and refilling in cheap night hours; holding
  charge for an expensive tomorrow morning is not the driver. The top 10 % of days give about 87 %
  of the gap.
- Reading: `persistence` captures as much as `omie_period`, so the gain is the planner's midnight
  valuation of stored energy (the terminal value), not price accuracy. A terminal value that
  reflects the overnight refill price would target the same small gap without a new data source.

**Accuracy of the 14:00 estimate (PO-C again, walk-forward, reimplemented `omie_period`, not the
repo module).** 2026-10-01 to 11-17 (46 days) against 2026-06-01 to 09-30 (121 days):

| Metric | `omie_period` Oct–Nov | `omie_period` Jun–Sep | `persistence` Oct–Nov |
|---|---|---|---|
| MAE, % of mean price | 14 % | 9 % | 22 % |
| Hours within ±10 % | 43 % | 58 % | 35 % |
| Hours within ±20 % | 66 % | 84 % | 56 % |
| Hours within ±30 % | 81 % | 90 % | 68 % |
| Within-day Spearman, mean | 0.98 | 0.97 | 0.79 |
| Overlap, cheapest 4 hours / dearest 4 hours | 91 % / 93 % | 88 % / 90 % | 58 % / 78 % |
| Daily spread error, % of mean spread | 12 % (under-estimated) | 9 % | 18 % |

- Ordering is excellent; the level is weaker since October. 78 % of the Oct–Nov squared error is a
  daily level offset (39 % in Jun–Sep), worst on days with a very low OMIE mean, Sundays and
  near-zero or negative hours (34 negative hours in Oct–Nov; the P1 intercept stays positive when
  OMIE is about zero). The within-day slope of PVPC on OMIE is about 1.16 but the between-day slope
  about 0.51 in Oct–Nov (1.02 and 0.80 in Jun–Sep); one slope over-reacts to the day level.
- Diagnostic only, chosen after seeing these windows, not validated out of sample: separate slopes
  for the day mean and the within-day deviation give about 5 % MAE and about 91 % of hours within
  ±20 % in both windows.
- A 15-minute variant gains nothing (PVPC is hourly). The 2026-10-25 DST day misses on level, not
  shape.
- OMIE D+1 was absent at 13:27 and present at 13:31 local on 2026-11-18 (one sample); the 13:30
  as-of rule holds with little margin.

**Data gaps found.** The local OMIE raw store ends 2026-10-25 and the repo reader found no OMIE
manifest (the experiments fetched the public files separately); the PVPC sensor read 0 for the whole
local day 2026-10-08 (outage, masked).

**Decision.** GA still fails on the live regime: no estimated price goes into the MPC. Optional and
small: a terminal value that reflects the overnight refill price. The OMIE estimate stays useful
where ordering matters for a decision taken before the publication (the heat-pump and DHW
schedules), see [DHW price scheduling](dhw-price-scheduling.md) section 6.

## 5. Terminal-value gap and strategies (parked 2026-11-18)

**What the gap is.** The live planner's horizon ends at the last published price
(`service/planner.py`); the energy left at the end is credited at the terminal value,
`planning.terminal_value: trailing_mean` (mean PVPC of the last 28 days, `service/runtime.py`
`_terminal_value`, exported as `battery_optimizer_terminal_value_eur_per_kwh`). From midnight until
the next-day publication (about 20:35) every plan ends at local midnight, so the 18:00 to 20:35 part
of the evening peak, and the value-based load-following floor (the plan's storage value), compare
discharging now against that terminal value. The trailing mean averages peaks with cheap night
hours, so it sits well above the cost of refilling the same kWh in the next night's valle hours.
Result in the §4 replay: the published-only planner holds energy back in the peak and enters the
night fuller (storage value at 20:00 about 8 % higher, battery at midnight about 0.2 kWh fuller on
average, 106 days), while a planner that sees tomorrow empties more in 18 to 22 h and refills
overnight. After 20:35 the plan sees tomorrow, which is why the gap is small.

**Size** (ceiling = exact prices for tomorrow from 14:00, against the live trailing-mean terminal
value).

- 2026-08-01 to 11-16, 106 days: about 0.6 % of the battery saving, interval includes zero.
- 2026-11-04 to 11-16, 13 days: about 5 %, no interval (too few weeks). A full-year figure between
  those is plausible; winter is unmeasured, and the gap concentrates on few days (top 10 % of days
  about 87 % of it) and on Mondays.
- For context: all forecast knowledge (load and prices, oracle against the live-like replay) is about
  7 % on the November days, prices most of it; the load part is in the
  [load forecaster plan §7](load-forecaster-improvement-plan.md#7-re-check-after-the-battery-went-live-2026-11-18).

**Candidate strategies** (share of the ceiling captured, 106 days, live terminal value; none has an
interval above zero).

| Strategy | Captured | Effort and risk |
|---|---|---|
| A. A lower constant terminal value (config only, `planning.terminal_value: <number>`) | A constant about 30 % below the November trailing mean: about half | No code. Goes stale with the season; too low and the plan empties the battery before midnight when tomorrow morning or a non-cheap night would pay more |
| B. Terminal value from the refill cost (for example the cheapest published valle price over the charge efficiency plus wear, capped by the trailing mean) | not measured | Small code change, tests and a shadow period; same edge cases as A but tracks the season; plans stay on published prices |
| C. Extend the horizon with estimated prices for tomorrow (`persistence` or `omie_period`) | about 64 % (`persistence`), about 58 % (`omie_period`); about 97 % on the 13 November days | Estimated prices would drive the 14:00 to 20:35 physical actions, against this plan's scope and the [periodic full-charge](periodic-full-charge.md) rule that an estimated price never decides a physical action; needs PO-E shadow, estimated-slot provenance in the live curve, and still a terminal value one day later |

- C works for the same reason as B: it puts a cheap night inside the horizon. `persistence` does as
  well as `omie_period`, so price accuracy is not what matters here.
- Side effect of any change: the terminal value also marks the inventory in the shadow report
  (`service/shadow_report.py`), so reported savings before and after are not directly comparable. The
  interaction with the full-charge day is untested.

**Re-analysis (pre-registered).** Re-run the §4 replay with A (a few constants), B and C against the
live trailing mean when either: (1) a winter of live battery data exists (after 2027-02-28), or
earlier on request with a simulated battery over winter 2025-26 (published prices and gross load
exist); (2) the battery, tariff or load-following changes. Rule: adopt a strategy only if the
weekly-block bootstrap interval of its paired daily gain lies above zero and no weekday gets worse;
prefer B over C unless C beats B with an interval above zero; C additionally needs the owner to
revisit the estimated-price rule.

**Larger levers, for priority.** The unit-side hot-water run at about 18:15 local
([DHW price scheduling](dhw-price-scheduling.md) section 2 and 6) is real peak consumption the
battery cannot cover, roughly estimated to be worth more than this whole gap; and usable capacity
(about 3.8 kWh, below the 18 to 22 h load) bounds every dispatch strategy.
