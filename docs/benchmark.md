# Benchmark reference

Full reference for `battery-optimizer benchmark`: the flags, the five-rung policy ladder, the battery
assumptions and the benchmark methodology notes. The other commands are in the [CLI reference](cli.md); the
decision record is [ADR-0003](adr/0003-phase1-measurement-and-baseline.md).

## Usage and flags

```bash
uv run battery-optimizer benchmark \
  --dataset ~/data/energy-baseline/analysis/v1/<dataset_id> \
  --train-end 2026-09-01T00:00:00+00:00 \
  --policies all \
  --terminal-mode free \
  --time-limit-s 600.0
```

**`--time-limit-s 600.0` in the example is deliberate, not decoration.** ADR-0003 §*Benchmark
methodology clarifications* records what a full-year horizon actually costs: at the default 120 s the
oracle terminated as a time-limited incumbent — `solver_status="time_limit"`, `bound_kind="upper"`,
`is_certified=False` — so its saving is only a lower bound, while 600 s on the same horizon certified
within tolerance and could be reported as a proven optimum. `benchmark.py` still pins
`DEFAULT_TIME_LIMIT_S = 120.0`, so any run that wants a certified ceiling must pass this flag
explicitly. The day-ahead rung is a separate knob: `--day-ahead-time-limit-s` is per **local day**
(default 5.0 s), not per horizon, and the run records which one a number came from.

`--train-end` is **required with no default**: thresholds are fitted on the earlier split and
frozen before evaluation, so the cut must be declared explicitly and is never inherited. Optional
flags (`--train-days`, `--charge-pct`, `--discharge-pct`, `--initial-soc-frac`, `--import-limit-kw`,
`--min-usable-fraction`, `--quality-policy`, `--time-limit-s`, `--mip-rel-gap`, `--mip-abs-gap`,
`--allow-reconstructed-quality`, `--terminal-mode`, `--day-ahead-forecast-window-days`,
`--day-ahead-terminal-value`, `--day-ahead-time-limit-s`,
`--rolling-horizon-replan-interval-slots`, `--rolling-horizon-horizon-slots`, `--rolling-horizon-sigma`,
`--rolling-horizon-bias-sigma`, `--rolling-horizon-seed`, `--rolling-horizon-time-limit-s`,
`--rolling-horizon-load-forecast-mode`, `--rolling-horizon-price-outlook`, `--throughput-cost-eur-per-kwh`,
`--load-forecaster`, `--eval-end`) default to the
benchmark module's constants. The CLI calls `core_available()` first and refuses with a clear message if
the numeric core (battery / policies / oracle / cost) is not usable.

**A full-year training window is 366 days, not 365.** When the split is meant to cover all of a
leap year — e.g. `--train-end 2025-01-01T00:00:00+00:00 --train-days N` for calendar 2024 —
`N` must be **366**, not 365: a leap year has 366 days and 365 excludes 2024-01-01. This applies
only to the day's *date* boundary; the day-ahead partition respects local Madrid calendar days
regardless. A UTC-midnight `--train-end` is also not a Madrid-local-year boundary (Madrid is
UTC+1 or UTC+2, so the local year turns at 23:00Z the day before); if a "full calendar year" is
being claimed, the claim is UTC-scoped and the report must say so.

## Usability gate and config drift check

**Usability gate:** intervals with `allocation_quality="estimated_boundary"` are benchmarked by
default (it is a bounded, telescoping-preserving approximation, not a degraded fallback).  The
`--allow-reconstructed-quality` flag admits reconstructed *values*: forward-filled prices and
partially gapped energy totals.  `unknown_gap`/`gap_total_unknown` allocation and
`missing`/`ambiguous` prices are never benchmarked, with or without the flag.

**Config drift check:** the benchmark verifies that the tariff config being used matches the
one pinned in the dataset's build manifest.  A mismatch hard-fails with both hashes shown —
the dataset must be rebuilt with the same config, or the same config must be passed.  There is
no bypass: benchmarking a battery against another build's data is exactly the error this check
exists for.

## Battery assumptions

**Battery assumptions — what is confirmed, what is assumed, and what is pinned.** Only two figures in
the `battery:` block are confirmed hardware facts: `capacity_kwh: 5.12` (nameplate) and `power_kw: 2.5`
(max AC power). `import_limit_kw: 5.0` is user-confirmed from the contract, never inferred from sensor
data. Everything else in the block is a labelled assumption — most importantly `round_trip_efficiency`,
which since 2026-10-24 ships **0.80**, the bottom of the community-measured 0.80–0.84 range, adopted on
the reasoning that at 2.5 kW the unit operates mostly near its power rating where efficiency is
typically worst. It supersedes the previous provisional 0.88 and is **still not a measurement of this
household's unit**: the efficiency-versus-power curve is unmeasured. Idle drain is now modelled at
26 W (adopted 2026-11-08, see battery-model.md), applied unconditionally to every interval's SOC
recurrence. Because the block is mapped to one-way efficiencies by a symmetric square root, the kernel
now charges and discharges at `sqrt(0.80) = 0.89443` per direction, replacing `sqrt(0.88) = 0.93808`.
Two consequences follow, and both are why the whole block is pinned by
`tests/test_benchmark.py::TestResolveBatteryAssumptions::test_shipped_tariff_battery_assumptions_are_pinned`:

- **A silent edit to efficiency, capacity, the SOC window or the import ceiling moves every reported
  figure while the test suite stays green** — and re-mints `dataset_id`, because `config_hash` feeds
  `compute_dataset_id`. The 0.88 → 0.80 change did exactly that, and passing silently is how the gap was
  found. The test is verified non-vacuous: it fails at the old value. It also pins the *framing*, so
  changing a number cannot promote an assumption to a measurement — `idle_losses_measured` stays `false`.
- **Every dataset built at 0.88 now fails the config drift check above, by design**, and every benchmark
  figure computed at 0.88 is an optimistic superseded result — including the accepted authoritative run
  and the forecast-experiment grid. `config_hash` moved from
  `41fdc30b926c099be2cbef201db4141244d82faaa80b9907c7530354f64bf182` to
  `7a901801b4148233e232fb86d534c4b7f626ec32af8e7279ad2f9e77831687d1`. The rebuild at 0.80 **has run**:
  dataset `2e01620ba72777ee8c25e5d8967151c095d2d5371450b85aecdbf7a8da69afd9` (70,176 rows, analysis
  content byte-identical to its 0.88 predecessor), and the five-rung ladder has been re-run on it
  (run `f816306cb8ad46afbb49a77e73e5ff1a`, oracle certified, **frozen load-forecast basis** —
  superseded at the causal basis by run `fe3cbfec46b6e51514fdc115a65a2165`, see *Causal
  load-forecast alignment* below). Rebuild *after* committing:
  `code_version()` is the git HEAD of a clean tree and also feeds `dataset_id`, so every commit
  re-mints the identity and a pre-commit build is invalidated by the very commit it waits for.

## Import ceiling

**Import ceiling (`--import-limit-kw`):** the site's contracted power, `import_limit_kw: 5.0` in
`config/tariff.yaml`, **user-confirmed from the contract on 2026-10-23** — never inferred from
sensor data. It constrains the *charging decision* only:
`charge[t] <= max(0, import_limit_kw * h - load[t])`. Because `load[t]` is per-interval energy,
that comparison is an average over the interval — the same 15-minute-average basis on which
contracted power is actually enforced, so the constraint matches the contract rather than
approximating it. Net grid import above the ceiling is therefore an **observation**, not a
violation: it can only happen where the measured load already exceeds the ceiling, which no
dispatch decision caused and none can undo, and treating it as infeasible would invalidate the
historical baseline itself. Schedules report those intervals via
`Schedule.ceiling_exceeding_intervals` / `battery.ceiling_exceedance_indices`. No safety margin
is applied; the configured value is used exactly as given. Instantaneous peaks (2025 archive
maximum 8.68 kW) bear on the battery's power rating, not on contract feasibility — see
`battery.py` invariants 6 and 7 for the measured table.

## Dataset identity

**Dataset identity:** `dataset_id` is a SHA-256 over the source part hashes, the canonical
config hash, the code version (including the working-tree dirty marker), the schema version,
the requested window and the builder parameters.  A dirty working tree produces a different id
from a clean one, so the reproducibility guarantee the id exists to provide is preserved.

## Policy ladder

**Policy set — five rungs, one per information level.** The evaluation window is scored by
`no_battery`, `causal_price`, `day_ahead`, `rolling_horizon` and `oracle` (ADR-0003
§"Strategy comparison"; the day-ahead rung was previously "considered and deferred" in
`docs/planning/energy-baseline-appdaemon-baseline-context.md` and is now built, and `rolling_horizon` was
promoted from an offline experiment to a canonical rung on 2026-10-24 — the ladder was four rungs until
then, and `POLICY_NAMES` is now `('no_battery', 'causal_price', 'day_ahead', 'rolling_horizon', 'oracle')`,
which is also what `--policies all` runs):

| Policy | Prices known | Load known |
|---|---|---|
| `no_battery` | none — measured history is the counterfactual | none |
| `causal_price` | none (percentile thresholds frozen on the train split) | none |
| `day_ahead` | **the day's 24 h, at local midnight** | **a strictly causal forecast** |
| `rolling_horizon` | today's 24 h from 20:30 **the evening before**, and tomorrow's from 20:30 today — the same publication the day-ahead rung waits for at midnight, in hand earlier, over a horizon that runs past the local midnight `day_ahead` stops at | **a strictly causal forecast** (the same profile mechanism) |
| `oracle` | whole horizon, perfect | whole horizon, perfect |

`rolling_horizon` sits **between** `day_ahead` and `oracle` because it is strictly more informed than
`day_ahead` and still has to forecast its load. The publication rule is stated once, in one function, and
derived from the decision instant — prices for local day D+1 become exact at 20:30 on day D — so a 09:00
solve estimates D+1 and a 21:00 solve does not, at **any** cadence. That single statement is what keeps
the information set from drifting between the two cadences. The rung re-solves on a schedule, executes only
up to the next event, and carries the **actually reached** SOC forward, so the next solve starts from the
real state rather than the planned one. Its flags, canonical defaults, cadence costs and dominance rules
are under *The rolling-horizon rung* below.

The causal rule captured a known share of the oracle's optimum, but the oracle also knows future
*load*, so the unreachable remainder conflated two different things. `day_ahead` separates them:
whatever it recovers is value reachable from information that is genuinely published for free, and
what remains is value that needs load foresight nobody has. That split —
`comparison.day_ahead_closes_gap_fraction` — is the number a build/no-build decision should read,
not the causal→oracle gap on its own. The fifth rung adds the matching measurement,
`comparison.rolling_horizon_closes_gap_fraction`, alongside `rolling_horizon_minus_day_ahead_eur` and
`rolling_horizon_minus_causal_eur`. Those differences are reported **without an expected sign**:
`rolling_horizon_minus_day_ahead_eur` is `rolling_horizon − day_ahead` on the shared basis, so a negative
value means the better-informed rung came out cheaper and a positive one means it did not — either is the
result, not a defect. The run also carries
`comparison.rolling_horizon_vs_day_ahead_observation`, which records the 81-of-81 empirical dominance as
"strong empirical evidence, not a mathematical proof" and explains why no guarantee is available.

## Day-ahead rung

The day-ahead price assumption is **not** self-certifying; it rests on this household's own
deployed automation, verified from source: `apps/home-assistant/appdaemon/apps/climate_control.py:45`
schedules `run_daily(..., "00:00:40")` and lines 73-91 of the same file read
`pvpc["attributes"][f"price_{i:02d}h"]` for `i in range(24)` off `sensor.esios_pvpc`
(`apps/home-assistant/appdaemon/apps/apps.yaml:10`), re-labelling them onto the current local day
(line 85); `apps/home-assistant/appdaemon/apps/dhw_control.py:48` does the same at `00:00:30`. A full
day's curve is therefore in hand at local midnight in production today. What the pipeline still
cannot verify is the *archival* half of ADR-0003 §*Policy design*: the store keeps the price realised in each
hour, not a midnight snapshot of the attribute dict, so `day_ahead` assumes published-in-advance
equals realised-and-archived. The artifact's own disclosure calls that assumption "true by construction
for day-ahead PVPC, but not measured here" — the first half describes how the tariff is designed to
publish, not what this archive holds. The pipeline's inability to verify it is **unchanged**; what changed
on 2026-10-24 is the *status of the gap*. ADR-0003 §*Open validation item: day-ahead publication versus
realised archive* records the **owner assertion** (`user_assertion`, `asserted_on: "2026-10-24"`,
`verified_from_data: false`) that the 20:15–20:20 publication on D−1 describes day D and is the realised
curve, which resolves the assumption as an owner-stated fact and closes the day-boundary convention
(previously TODO-0.4), and makes the midnight attribute-dict snapshot an optional confirmation rather than
a blocking item. An owner assertion is not a measurement, so nothing here should be read as the archive
having been checked.

The reliance is still recorded in every run artifact under
`day_ahead.information_set.assumptions_disclosed` instead of being quietly relied on, and it claims no
**direction of error** — nor does this page. A wrong midnight curve would change which decisions the
policy makes, not the prices those decisions are costed on, because the plan is re-scored against the
same archived realised prices every other policy is scored on; so divergence can hurt or help, its
expected direction is adverse, and that is an expectation, not a bound. Establishing a direction needs a
midnight snapshot to compare against, which is the cheap fix ADR-0003 keeps open (snapshot
`sensor.esios_pvpc`'s attribute dict at `00:00:40` daily).

Mechanically the policy is a **rolling daily MILP**: the evaluation window is partitioned on
`Europe/Madrid` calendar days (never UTC days — the tariff is hourly in local time, so a
spring-forward day is 92 intervals and a fall-back day 100, each in exactly one solve), one
`scipy.optimize.milp`/HiGHS solve per day through the *same* `oracle.solve_oracle` assembly the
ceiling uses, and the actual end-of-day SOC rolled forward as the next day's initial condition. The
load forecast is a trailing mean of the same slot position over the previous
`--day-ahead-forecast-window-days` days (`default 28`), reading only intervals strictly before the
target day; a slot with no prior sample falls back to the pooled window mean, and the first local day
of a dataset forecasts zero — the ladder is recorded in `day_ahead.forecast.fallback_rule`, never
applied silently. The forecast constrains decisions and **never** enters cost: every euro is
re-computed against the measured load, and a plan the measured load cannot absorb is clipped to it
(discharge beyond measured load would export; charge beyond measured-load headroom would breach the
ceiling) with the clipped energy reported.

`day_ahead` is **myopic by construction** and that is disclosed rather than tuned away: a daily solve
cannot see tomorrow, so end-of-day SOC is credited at `--day-ahead-terminal-value` EUR/kWh — the
policy's only cross-day coupling, per ADR-0001's "preserve a terminal SOC value … instead of
artificially emptying the battery at midnight". Omitted, the value is the mean price of the frozen
training window and the artifact records provenance `default:training_window_mean_price`; passing a
number records it as `cli`. `0.0` is a legal, documented choice (pure cash flow, battery emptied by
design). Note what a low terminal value does *not* mean: because charging only pays off once the
credit covers the charge, valuing storage far below the cheap-hours price is not conservatism, it is
a disabled battery.

**Two terminal values, not one (a withdrawn claim recorded because it was the first draft).**
An earlier version of `day_ahead` used one value — the internal training-window mean V — for
*both* the daily planning and the reported `cost_eur`, while the oracle used 0.0 for its
reported cost. The two policies were therefore being scored on different terminal bases, which
made the headline comparison and `oracle_dominates_day_ahead` meaningless: the day-ahead policy
was receiving a credit for its ending SOC that the oracle wasn't, and the credit could be worth
more than the entire causal→day-ahead delta it was meant to measure. Fixing this required
separating the two numbers:

- **`terminal_value_eur_per_kwh`** (V, set by `--day-ahead-terminal-value` or defaulted to the
  training-window mean): used *inside* each daily objective so the myopic daily solve has a
  reason to keep useful energy overnight. Recorded in the artifact as
  `day_ahead.terminal_value.eur_per_kwh` and as `internal_planning_value_eur_per_kwh` in
  `assumptions.terminal_accounting.day_ahead`. The sum of the daily objectives under V is
  recorded as `day_ahead.internal_daily_objective_sum_eur` with an explicit note that it is
  **not** the common score.
- **`scoring_terminal_value_eur_per_kwh`** (default `0.0`, threaded from
  `_oracle_kwargs["terminal_value_eur_per_kwh"]`): what the returned Schedule's `cost_eur` and
  `inventory_adjustment_eur` use, and the same number the oracle is scored on. Recorded as
  `policies.day_ahead.terminal_value_eur_per_kwh` (which is what `BENCHMARK_RESULT` and the
  per-policy JSON record expose) and as `day_ahead.scoring_terminal_value_eur_per_kwh`.

With V ≠ 0 and a positive final SOC, the internal sum and the reported cost differ by exactly
`V × (terminal - initial)` — the credit the daily objectives gave stored energy. The reported
number is the metered cost on the shared scoring basis. This is what makes
`oracle ≤ day_ahead` a real dominance rather than a bookkeeping artefact, and it is pinned
by `tests/test_benchmark.py::TestDayAheadScoringValue` and the V=0.4 case in
`TestDayAheadPolicy.test_cost_is_meter_plus_telescoped_inventory`.

**Seed protection is horizon-level, not day-level.** The charge the evaluation window was *handed*
is not any policy's to spend; everything bought after the window opened is, including across a day
boundary. A first draft of `day_ahead` floored each daily solve at that day's own opening SOC, which
silently re-classified yesterday's purchased charge as unspendable seed: on a synthetic fixture the
battery filled on day one, froze at the ceiling for the rest of the window, and lost to the causal
rule while holding 4.6 kWh it was forbidden to use. The floor is now fixed at
`max(soc_min, initial SOC)` for the whole roll — the same floor `causal_price` and the free `oracle`
use, which is what keeps all five policies on one common feasible set and what makes
`oracle ≤ day_ahead` a provable relation rather than a hope. Supporting it needed one defaulted
parameter on `oracle.build_model`/`solve_oracle` (`floor_kwh`); with the default the certified model
is byte-identical, pinned by
`tests/test_oracle.py::TestDayAheadReusesOracleAssembly::test_default_floor_reproduces_the_certified_model_exactly`.
A spec whose opening seed already fills the SOC window has no headroom in either direction: those days
are reported as `frozen_by_seed_at_ceiling` with zero flows instead of being sent to the solver as an
invalid spec.

One day's MILP failing does **not** abort the run: that day holds SOC, charges nothing, is flagged in
`day_ahead.per_day[].degradation`, and the counts surface in `day_ahead.days_infeasible` /
`days_no_incumbent` / `days_skipped_no_usable_intervals` / `days_without_headroom`. The oracle's
answer is never substituted.

A forecast-derived plan can ask for more than the measured load allows — discharge beyond it would
export, charge beyond the measured-load headroom would breach the ceiling — so the plan is clipped to
the measured constraints in chronological order and the refused energy is reported as
`charge_clipped_kwh` / `discharge_clipped_kwh` with `days_with_clipping`. Clipping only ever reduces
a flow, never re-plans one: a wrong forecast costs dispatch quality and is disclosed, it does not
buy feasibility.

Ordering is asserted only where it is provable. Oracle ≤ baseline and oracle ≤ causal hold as before.
Oracle ≤ day-ahead is guaranteed when the oracle is a *certified* optimum **and**
`terminal_mode_used == "free"`: the free oracle minimises over exactly the feasible set that
contains the day-ahead plan (same SOC floor `max(soc_min, initial)`, same physics, same endpoint
freedom, and the same scoring terminal value, now 0.0 by default), so `oracle.cost_eur ≤
day_ahead.cost_eur` is arithmetic. On a time-limited incumbent the oracle reports its own best
feasible cost and the comparison is empirical — hence the separate
`comparison.oracle_dominates_day_ahead_guaranteed` flag. **When `terminal_mode_used` is `"fixed"`
the guarantee is `False` by construction:** the fixed oracle pins final SOC to the initial value,
which may exclude the day-ahead plan's actual final SOC, so the day-ahead plan is not in the
oracle's feasible set and no dominance is provable even if the empirical comparison happens to
hold. Do not assert fixed dominance; a `terminal_mode=fixed` run with a day-ahead plan ending
above its seed is the canonical counterexample the test suite pins
(`TestDayAheadScoringValue::test_fixed_terminal_mode_does_not_guarantee_day_ahead_dominance`).
**Day-ahead versus causal is guaranteed in NEITHER direction** and is never asserted: a myopic
optimiser with a poor forecast and a badly chosen terminal value can lose to a dumb threshold
rule. The difference is reported as `comparison.day_ahead_minus_causal_eur`, and if it is
positive, that is the result, not a defect.

## Rolling-horizon rung

**The rolling-horizon rung (`rolling_horizon`) — a fifth policy with a configurable replan cadence.**
The rung re-solves on a schedule, executes only up to the next event, and carries the **actually reached**
SOC forward, so the next solve starts from the real state rather than the planned one. Physics are reached
only through the same kernel the other rungs use, and its scoring terminal value is the same shared 0.0
threaded from the oracle's own keyword arguments — the fifth rung is scored on the common objective, not on
one of its own.

Canonical defaults, all verified:

| Knob | Default | Why that default |
|---|---|---|
| `--rolling-horizon-sigma` | `0.0` | The ladder compares *information*, not synthetic forecast error; every other rung sees exact prices, so a noisy fifth rung would not be comparable |
| `--rolling-horizon-bias-sigma` | `0.0` | Same reason |
| `--rolling-horizon-seed` | `0` | Only matters with noise; noise stays an experiment reachable through flags |
| `--rolling-horizon-replan-interval-slots` | unset (legacy two-publication 14:00 / 20:30 schedule) | The configuration the golden-value tests pin bit-for-bit and the one validated on real data |
| `--rolling-horizon-horizon-slots` | unset with the legacy cadence; `96` when a cadence is given | The legacy horizon is DST-dependent and derived from the local-day partition, so a single recorded number would be fabricated |
| `--rolling-horizon-time-limit-s` | the day-ahead per-solve limit | Per **solve**, like the day-ahead's per-day limit, and unlike the whole-horizon `--time-limit-s` |
| `--rolling-horizon-price-outlook` | `exact` | The canonical convention: the 14:00 event sees tomorrow's realized prices. It is an upper bound, kept so results stay comparable with earlier runs. See *Price-outlook seam* below |
| `--rolling-horizon-load-forecast-mode` | `"causal"` at the benchmark and CLI layer (`"frozen"` for a direct `run_rolling_horizon` call) | `"causal"` uses the same trailing-slot-mean function as `day_ahead`, so both rungs share a like-for-like load basis. `"frozen"` is the legacy mode the golden-value tests pin bit-for-bit and remains reachable, and stays the default when the function is called directly so the noise experiments keep one shared load basis. See *Causal load-forecast alignment* below. |

**Cadence, and what it costs.** `--rolling-horizon-replan-interval-slots 1` with
`--rolling-horizon-horizon-slots 96` is ADR-0001 §3's controller: re-solve every 15 minutes over a rolling
24-hour horizon (96–192 slots covers its 24–48 h band), execute only the current interval. **It costs
roughly 48× the solve count** — 8,832 solves against the 185 the legacy two-publication schedule needs
over a 92-day holdout — **and roughly 37× the wall time** (~175 s against ~4.7 s per arm, both measured on
synthetic fixtures). The two multipliers differ because a fine-cadence solve is *cheaper* than a legacy one
(~20 ms/solve against ~24 ms/solve): each legacy event plans the rest of its local day *and the next one*
(~34 h at 14:00), while cadence 1 plans a fixed 96 slots, and per-solve time tracks horizon length. So wall
time grows more slowly than solve count, and neither ratio is the horizon length. The two multipliers
themselves are still synthetic-fixture measurements. The fine-cadence arms have **now been run on real
data** on the 0.80 dataset across the four representative windows, where the ladder 96 → 4 → 1 improves
or ties monotonically; the legacy cadence remains the validated canonical configuration on the full-year
ladder. Legacy
behaviour is not merely unchanged but **bit-identical**, pinned by golden digests extracted from `HEAD` with
`git show` and pinned as literals — digests of the charge/discharge/SOC/grid arrays, exact cost, EFC, terminal
SOC, event and clipping counts, noise statistics, plus digests of the whole event table and the frozen
profile — over two fixtures (one with a partial opening local day, one covering the 2025-10-26 fall-back
weekend) at four noise arms, with non-vacuity asserted (the control and two seeds give three different
charge digests).

**Two honesty properties the artifact reports; do not drop or reinterpret them.**

- `executed_multiplicity_mean` is exactly **1.0** while `planning_multiplicity_mean` is much higher —
  about **94.4** at cadence 1 over a 96-slot horizon, about **2.5** at the legacy cadence. An interval is
  *planned* by every overlapping horizon and *executed* once. **Clipping totals are therefore not
  comparable across cadences and are not curtailment**: they count repeated planning of the same
  intervals, not extra refused energy. The artifact says so itself: `clipping_totals_comparable_across_cadences`
  is `false`, with a note explaining why.
- `solve_count` is recorded per run, so a run's compute cost is visible in its own artifact instead of
  being discovered afterwards.

**Dominance is asserted only where provable.** `comparison.oracle_dominates_rolling_horizon_guaranteed`
follows the existing rule exactly: `oracle ≤ rolling_horizon` is guaranteed only when the oracle is
**certified** *and* scored in `free` terminal mode, because only then does the oracle minimise over the
feasible set that contains this plan; under `fixed` it is `False` by construction. The relationship the
fifth rung is supposed to demonstrate is **not** asserted: `comparison.rolling_horizon_may_lose_day_ahead`
is recorded as **`true`**, together with `rolling_horizon_may_lose_causal` and `rolling_horizon_may_lose_baseline`.
That `rolling_horizon ≤ day_ahead` held in **81 of 81** offline configurations is strong empirical evidence
**within that configuration**, not a proof — it is recorded as an observation about that experiment,
whose figures are themselves superseded (see *Battery assumptions* above). All 81 configurations were a
Q4 holdout immediately following the training cut; that scope does not transfer to evaluation windows
that extrapolate far beyond the cut, and a measured counterexample existed: on the 2025 full-year
ladder the same rung cost MORE than `day_ahead`. The identified asymmetry sufficient to explain the
reversal is the load-forecast basis: this rung's profile is frozen at the training cut while
`day_ahead` re-learns before each dispatch day. Re-running the identical Q4-2025 window on the same
dataset reproduced the win, which rules out the efficiency correction as the cause. That reversal is
now resolved: on the aligned causal basis the rolling rung beats `day_ahead` on the full year and in
all four representative windows. Cost consistency (`cost_eur == meter_cost_eur`, inventory adjustment 0) is a
loop over whichever policies ran, so the fifth rung is covered by extension rather than duplication.

**Causal load-forecast alignment (`--rolling-horizon-load-forecast-mode`, complete).**
The full-year reversal exposed a load-forecast-basis asymmetry: `rolling_horizon`'s profile was frozen
at the training cut while `day_ahead` re-learns before each dispatch day, making the two rungs
incommensurable. The fix introduces a `"causal"` mode that uses the **same** trailing-slot-mean
function `day_ahead` already uses, so the two rungs share a like-for-like load basis. Decision:
**conservative per-event local-day cutoff** — each solve builds one forecast truncated at the **event's
own** local-day start, and that **single vintage is held across the entire horizon**; a later target day
in the same horizon therefore keeps using the cutoff of the day the solve was dispatched for, never its
own midnight. No interval anywhere in the horizon sees information that only exists after the event day
(the per-target-day-midnight variant was the leaky one and is not what is implemented);
**frozen mode preserved** — `"frozen"` remains reachable as the legacy mode the golden-value tests pin
bit-for-bit, so the comparison to the superseded experiment remains reproducible. **The canonical
benchmark and CLI default is now `"causal"`**; `run_rolling_horizon` called directly still defaults to
`"frozen"` for backward compatibility. The 14 legacy golden digests are untouched and one new causal
golden has been added. The frozen profile's original design intent — keeping every noise arm
on one identical load basis so arms are comparable to each other — is preserved: `"causal"` only
changes the *canonical rung's* default load basis, not the experiment module's behaviour.

**Load-forecaster seam (`--load-forecaster`).** `day_ahead` and the causal `rolling_horizon` rung get
their load forecast from a registered forecaster in `load_forecast.py`, through one call contract:
`run_forecaster(name, load_kwh, usable, days, cutoff_idx, target_days)`. A non-oracle forecaster is
handed only the history before `cutoff_idx`, as read-only views, so post-cutoff data cannot reach it.
`tests/test_load_forecast.py` checks that for every registered non-oracle forecaster. `day_ahead` cuts
each day at its own local midnight (`forecast_each_day_at_its_start`), and the causal rolling rung cuts
at the event's local-day start, the same rules as before the seam. Registered forecasters:

| Name | What it forecasts | Params |
|---|---|---|
| `trailing_mean` (default) | LF-0, the 28-day same-slot trailing mean. Bit-identical to the pre-seam path | `window_days`, fed by `--day-ahead-forecast-window-days` |
| `perfect` | **Offline oracle.** It returns the measured load of the target slots. Gap-excluded slots pass through the same value the whole-horizon oracle sees, and the shared usable mask gives them zero flow, so the value cannot change a plan. It is flagged `oracle=True`, refused by the service, and recorded as an oracle in the artifact | none |
| `trailing_median` | Same-slot median over the previous `window_days` local days. Robust to appliance spikes | `window_days` (28) |
| `trailing_trimmed_mean` | Same-slot mean after dropping `floor(trim·n)` values at each end | `window_days` (28), `trim` (0.1) |
| `ewma` | Same-slot exponentially weighted mean over days, weight `0.5^(age/half_life_days)` with age counted from the target day | `half_life_days` (7), `window_days` (56, the cap) |
| `daytype` | Mean of the last `n_days` local days of the target's day type: weekday, Saturday, or Sunday-or-holiday. Spanish national holidays (plus an optional subdivision) come from the [`holidays`](https://pypi.org/project/holidays/) package. Days without usable data are skipped | `n_days` (6), `window_days` (84, the search limit), `subdivision` (none = national only) |
| `level_adjusted` | The 28-day trailing-mean profile times `clip(observed last 24 h ÷ profile of those slots, lo, hi)`. **Declares `intraday_cutoff`**: at the 14:00 and 20:30 events the last 24 h include today's observed slots. One factor scales every target day; under 24 observed slots it stays 1.0 | `window_days` (28), `lo` (0.7), `hi` (1.4), `min_slots` (24) |
| `structured` | **LF-1.** Heat pump plus residual, with the heat-pump schedule replayed from the prices (see below). **Declares `intraday_cutoff` and a `fit` step, and needs side inputs: offline only, refused by the service** | `features` (feature set id, required), `window_days` (28), `residual_forecaster` (`trailing_mean`), `price_outlook` (`persistence`), `calibration_days` (7), `disabled_fallback` (`lf0`), `disabled_history_days` (28), `disabled_lookback_days` (60), `climate_lf0_season` (none) |
| `blend` | `w · trailing mean + (1 − w) ·` the same-season-last-year profile: the mean of the local days within `last_year_half_width_days` of the same date one year earlier. Trailing alone where fewer than `min_last_year_days` of those days have usable data | `w` (0.5), `window_days` (28), `last_year_half_width_days` (14), `min_last_year_days` (7) |

The six statistical candidates (`trailing_median` to `blend`) read the
same history as LF-0, aligned by slot position inside each local day, so a 92- or 100-interval DST
day is still one local day. An interval that is not usable is unknown: it never contributes and is
never counted as zero. A slot no history day knows takes the window's pooled mean, and a window with
no usable interval forecasts 0.0 kWh and is flagged `zero_no_history`, exactly as LF-0 does. They
are evaluated with [`forecast-eval`](#forecast-evaluation-forecast-eval). The shadow service plans at
the local midnight of the decision day, so there `level_adjusted` sees yesterday only.

The default adds nothing to the run identity, so a pre-seam identity is reproduced except for
`code_version`. Any other forecaster adds `load_forecaster` and `load_forecaster_params`. Every run
records the forecaster's name, params and oracle flag under `lineage.load_forecaster`, next to
`code_version` (with its dirty marker), `config_hash` and `dataset_id`. A non-default forecaster with
`--rolling-horizon-load-forecast-mode frozen` is refused, because the frozen profile does not use the
seam. To add a forecaster, write one function decorated with `@register_forecaster("name")`; its
keyword-only parameters, with their defaults, become its recorded params.

Three optional capabilities are declared at registration, and all are recorded in the forecaster's
provenance:

- `fit=fit_fn` for a trained model. `fit_fn(FitRequest, **params)` returns any state. The seam fits
  once per **monthly walk-forward fold**, the local calendar month of the decision's cutoff, on
  history strictly before that month's first local day. It hands the state to every decision in
  the fold as `request.fitted`. The benchmark runs and the evaluation harness therefore use the same
  folds, 2025-01 to 2026-09 for the P0 periods.
- `intraday_cutoff=True` lets the rolling rung hand the forecaster history up to the event instant
  (14:00 or 20:30) instead of the event's local midnight, so it can use the slots of the day
  observed so far. `day_ahead` decides at local midnight, where both cutoffs are the same. Without
  the flag the local-midnight cutoff is kept bit for bit. The leakage test in
  `tests/test_load_forecast.py` perturbs everything from each forecaster's **declared** cutoff on.

- `side_loader=loader` for a forecaster that reads more than the load. The caller builds one
  `SideInputs` for the whole index space (`build_side_inputs`; `benchmark` and `forecast-eval` do it
  from the dataset table) and passes it as `side=`. The seam hands the forecaster a `SideView`:
  every measured side series is cut at `cutoff_idx` like the load, and the prices at the first index
  that is not yet public at the cutoff instant (D+1 from 20:15 local). A forecaster without a
  `side_loader` is never handed side data, and one with it must still run, degraded and saying why,
  when none is passed. The fit cache is keyed by the side inputs' content fingerprint.

```python
@register_forecaster("level_adjusted", intraday_cutoff=True)
def _level_adjusted(request: ForecastRequest, *, window_days: int = 28) -> list[DayForecast]: ...


@register_forecaster("my_model", fit=_fit_my_model)
def _my_model(request: ForecastRequest, *, alpha: float = 0.5) -> list[DayForecast]: ...
```

**Structured forecaster (`structured`, LF-1).** `forecast = heat pump + residual`, built from the
[sub-load feature table](cli.md#build-features) (`features=<feature set id or unique prefix>`).
AppDaemon's `climate_control` and `dhw_control` are price rules, so *when* the heat pump runs is
replayed and only *how much* it draws is modelled. All inputs are as of the cutoff.

- **Schedule mask.** An exact replay of `climate_control` for each target day: the hours priced below
  two thirds of the trailing mean price (10 days of PVPC history until 2026-04-27, 7 days after),
  or the cheapest `min_hours_per_day` hours when fewer qualify. The rule is versioned by effective
  date against the homelab commits it mirrors (`CLIMATE_RULE_VERSIONS`, `DHW_RULE_VERSIONS`), so a
  change of the AppDaemon rule is a new version, not a silent refit. The heat or cool mode is the one
  with more measured energy in the last 48 h. The enable flag and `min_hours_per_day` are the last
  known values. Prices are the published ones wherever they are public at the decision; the other
  days use the `price_outlook` param (`persistence`, or an OMIE estimator such as `omie_period`,
  which reads the OMIE day-ahead series carried by the side inputs and cut at the first OMIE day not
  usable at the cutoff) and are recorded as `estimated`. So the 14:00 event plans tomorrow on
  estimated prices and the 20:30 event on published ones.
- **Energy per selected hour.** One ridge regression per mode, fitted once per monthly fold on the
  replayed history before the fold (the seam's `fit` step): piecewise-linear in the as-of outdoor
  temperature (persistence: the same local clock hour of the latest of the last three days it was
  measured), the position in the block of selected hours, a start-up term and the number of selected
  hours. Outside the mask the climate energy is a constant. The in-mask prediction is scaled by the
  ratio of measured to predicted energy over the last `calibration_days` enabled days (clipped to
  0.5-2), because the heat pump's behaviour drifts inside a month.
- **Hot water.** The `dhw_control` replay places the forced hours (one per day from 2025-03-16, one
  per 12-hour block from 2025-12-29); the energy is the trailing mean by hours since the last forced
  hour and quarter of the hour over `window_days` days.
- **Residual.** `main - heat pump` is forecast by the `residual_forecaster` param through the same
  seam at the local-midnight cutoff, so nothing is counted twice.
- **Fallbacks.** A disabled rule, an unknown mode or flag, unavailable or incomplete prices, missing
  temperature or no fitted model make the climate part fall back to the LF-0 trailing mean of that
  component; the hot-water part falls back the same way. The reason is recorded per target day and
  counted per event kind in the rolling-horizon artifact (`forecast_metadata.forecaster_labels`).
  Without side inputs the forecast is LF-0 of the main load.
- **Second-look options (all off by default, so the first P4 results reproduce).**
  `disabled_fallback=disabled_history` forecasts the climate part of a day whose rule is disabled
  as the slot-wise mean of the last `disabled_history_days` (28) complete days within
  `disabled_lookback_days` (60) whose rule was disabled, and as zero when there is none, instead of
  the trailing mean of every day (which still carries the enabled days' climate energy). The reason
  stays `rule_disabled`, so the fallback counts do not change. `climate_lf0_season=heating` (months
  Nov-Mar) or `cooling` (Jun-Sep) is the seasonal hybrid: on the days of that season the climate
  part is the LF-0 trailing mean even when the rule is enabled (reason `season_hybrid`); a disabled
  day takes the disabled fallback first, and the shoulder months Apr, May and Oct belong to neither
  season, so the model runs there. The month is the local month of the target day.

`tests/test_structured_forecast.py` replays the rules on hand-built days (the fallback branch, ties,
23- and 25-hour days), pins every fallback reason, and perturbs the load, every measured side series,
the unpublished prices and the fold's own month to show the forecast does not move.

**Price-outlook seam (`--rolling-horizon-price-outlook`).** What a rolling-horizon event knows about
the prices it plans over comes from a registered price outlook in `price_outlook.py`, through one
call contract: `run_price_outlook(name, prices, published_end_idx, decision_idx, plan_end_idx, ...)`.
PVPC for local day D+1 counts as published from 20:15 local on day D
(`PVPC_NEXT_DAY_PUBLISHED_FROM`), so the 20:30 event sees tomorrow and the 14:00 event does not.
A non-oracle outlook receives only the published prices, as a read-only view, and answers only for
the unpublished slots, so a published price is never overwritten. Each slot of the plan gets a
provenance: `published`, `estimated` or `unavailable`. An event plans only up to its first
`unavailable` slot and values the energy left there at the run's planning terminal value, which is
what the live service does at its last published price. `tests/test_price_outlook.py` checks for
every registered non-oracle outlook that prices after the publication boundary cannot reach it,
and that perturbing tomorrow's realized prices leaves the 14:00 plan unchanged.

| Name | What the 14:00 event plans on for tomorrow |
|---|---|
| `exact` (default) | **Offline oracle**, the upper bound: tomorrow's realized prices, degraded by `--rolling-horizon-sigma` / `--rolling-horizon-bias-sigma` when those are set. Bit-identical to the pre-seam rung |
| `published_only` | Nothing: the plan ends at midnight, the last published price, and the remaining energy is valued at the terminal value. This is the live service |
| `persistence` | Today's price at the same local wall-clock time. On a fall-back target day both repeated hours take today's value; a spring-forward source day lends its missing hour from the day before |
| `omie_period` | **PO-C.** Tomorrow's PVPC from tomorrow's OMIE day-ahead price: `PVPC_hour = beta * mean(OMIE ES over the hour) + intercept(2.0TD period)`, one slope and one intercept per period (P3 covers weekends and the nine national fixed-date holidays), fitted by least squares on the hourly prices of the last 28 fully published local days. OMIE for D+1 is usable from 13:30 local on D, so the 14:00 event uses it |
| `omie_period_month` | `omie_period` with a level offset per calendar month of a longer window (120 days), predicted at the latest month's level. It follows drift of the regulated and futures components instead of averaging it |

The default adds nothing to the run identity and leaves the per-event telemetry unchanged. Any other
outlook enters the identity as `rolling_horizon_price_outlook`, records `price_outlook` provenance and
counts in the telemetry, and adds `planned_through_idx`, the estimated and unavailable slot counts
and a `plan_prices_sha256` to each event. Every run records the outlook under
`rolling_horizon.price_outlook`. A non-default outlook needs the legacy cadence and zero sigma and
bias: the noise channels model an estimate of `exact` only, and a finer cadence would put decisions
between the 20:15 publication and the 20:30 boundary the noise model keeps. To add an outlook, write
one function decorated with `@register_price_outlook("name")`. An outlook whose inputs only exist from
a local time of day (OMIE day-ahead prices, usable from `OMIE_NEXT_DAY_USABLE_FROM` = 13:30) declares
`inputs_usable_from=`; before that time the seam does not call it and falls back to `published_only`.

**OMIE side input (`needs_omie=True` outlooks).** The OMIE estimators read a series the dataset does
not hold. `benchmark` loads it from the verified OMIE store (`omie_prices.load_omie_es_prices`, hash
checked, 15-minute UTC grid, `NaN` where a day is not stored) as an `OutlookSide` aligned to the
dataset grid, and the seam cuts it for every decision at the first OMIE day that is not yet usable
(`omie_end_idx`: D+1 from 13:30 local on D), exactly as it cuts the published PVPC. The estimator
therefore cannot see a later OMIE day or an unpublished PVPC. The fit window and the estimate use
only the published days before the decision. If the side input is missing, an OMIE hour of the target
day is missing in any quarter, or fewer than 7 published days are in the window, the affected slots
are `unavailable` (the `published_only` behaviour) and the reason is recorded per event
(`price_outlook_fallback`) and counted in the telemetry (`price_outlook.fallback_events`). The side
input's identity (date range, missing days, fingerprint of the values) enters the run identity and
the `price_outlook.side_input` record. `omie_quantile` of the plan is not built: it exists to feed
PO-D, which stays deferred. The fit windows (28 and 120 days) were chosen on 2024 data only.

`--eval-end` cuts the dataset at an instant before anything else runs. The run is then the run on a
dataset that ends there, with the same rows, partitions and terminal values, and the cut enters the
run identity. Without the flag nothing changes.

## Forecast evaluation (`forecast-eval`)

`battery-optimizer forecast-eval` ([CLI](cli.md#forecast-eval)) compares registered forecasters
against LF-0. It implements P1 of the
[load-forecaster plan](planning/load-forecaster-improvement-plan.md). All its output is private and
goes under `<dataset>/forecast-eval/`.

**Pre-registration.** `preregistration.json` is written before the first candidate runs. It records
the candidates and params, periods, policies, metrics, bootstrap seed and gate G2, together with the
git commit, dirty flag and config hashes.

**Forecast accuracy** is measured at the policies' real decision instants, on usable slots:

- `day_ahead`: one forecast per local day, made at 00:00 local.
- `rolling_horizon`: one forecast per 14:00 or 20:30 event, over that event's plan horizon. Warm-up
  events are left out.

The forecasts come from the same seam calls the planner uses. The harness reports MAE, WAPE, bias
(forecast minus measured), P95 absolute error and the price-weighted absolute error
`Σ price·|error|`. Each is given overall and broken down by:

- horizon bucket from the decision instant: 0–6 h, 6–24 h, 24–36 h;
- price tercile, using the terciles of the period's usable prices;
- day type (weekday or weekend; holidays are not separated);
- meteorological season;
- month.

**Economic evaluation.** Each candidate and period gets one `benchmark` run with the P0 parameters:
`no_battery`, `day_ahead` and `rolling_horizon`, exact prices, monthly usability gate at 0.8 in
`report` mode, and the previous calendar year as the training window. The harness pairs the
per-local-day cost with LF-0's on the same days. The saving is LF-0 minus the candidate, so positive
means the candidate is cheaper. Its sum over the period is the point estimate. The 95 % interval
comes from a non-overlapping **7-local-day block bootstrap** with a fixed seed, at least 5000
resamples, and one draw matrix per period that every comparison shares. The harness also reports the
share of the P0 gap (LF-0 minus `perfect`) that the candidate captures. The gap comes from
`--p0-summary` if given, otherwise from an in-run `perfect` candidate. With three or more
candidates it reports the Spearman correlation between price-weighted error and EUR cost, to show
whether the cheap proxy ranks forecasters the same way the money does.

**Gate G2** (pre-registered). The primary policy is `rolling_horizon` and the secondary is
`day_ahead`. A non-oracle candidate passes if, for the primary policy in every period, the bootstrap
interval of its paired saving is entirely above zero **and** the point saving is at least 10 % of
that period's P0 gap. The secondary policy is reported but not gated.

**Sanity checks** in every summary:

- LF-0 against itself is exactly zero, point and interval.
- When `--p0-summary` is given and `trailing_mean` and `perfect` are both candidates, the LF-0 cost,
  the `perfect` cost and their gap are compared for exact equality with the P0 run (gate G1).

**Gate G4** (structured forecaster). `--gate G4 --gate-candidate LABEL --gate-reference LABEL`
pre-registers the season definition and the rule before any run. Seasons are local calendar months
of each period's days: heating is November to March, cooling June to September, April, May and
October are transitional and appear only in the whole-period figures. A *season instance* is one
period's days in one season (blocks of 7 consecutive days for the bootstrap; fewer than 14 days is
not evaluated). The candidate passes if, for `rolling_horizon`, the 95 % interval of its paired saving
against the reference lies entirely above zero in at least one heating **and** at least one cooling
season instance. Whole-period figures, `day_ahead` and every `--compare CANDIDATE=REFERENCE` pair are
reported, not gated. Candidates take an alias, `structured@lf1:features=...`, so the labels are
short. A forecaster that declares a `diagnose` hook adds a component report to the summary: heat-pump
WAPE (climate, hot water, total) against the LF-0 trailing mean of the same component, the mask
hit-rate (measured climate kWh inside the replayed hours) by price provenance, and the counts of
price provenance and fallbacks per decision kind.

## Price-outlook evaluation (`price-eval`)

`battery-optimizer price-eval` ([CLI](cli.md#price-eval)) measures what knowing tomorrow's prices at
14:00 is worth. It implements PO-A of the
[price-outlook plan](planning/price-outlook-improvement-plan.md) and reuses the bootstrap, provenance
and pre-registration machinery of `forecast-eval`. All its output is private and goes under
`<dataset>/price-eval/`.

- **Pre-registration.** `preregistration.json` is written before the first benchmark runs: outlooks
  and their provenance, periods, benchmark parameters, publication rule, metric definitions,
  bootstrap seed and the GA gap definition.
- **Economic evaluation.** Each outlook and period gets one `benchmark` run with the P0 parameters on
  `no_battery` and `rolling_horizon` (LF-0 load forecast). The per-local-day `rolling_horizon` cost
  is paired against `exact` and against `published_only`, with the same 7-local-day block bootstrap
  as `forecast-eval`. The GA gap is `published_only` minus `exact`; each outlook's share of it is
  reported. Whether the gap is material is judged against a private owner threshold, so the run gives
  no verdict.
- **Action differences.** Over the slots executed by the 14:00 events (14:00 to 20:30 local), the
  number of slots and days whose charge or discharge differs between two outlooks, and how many of
  those days started from the same state of charge, so the difference comes from price information
  alone.
- **Price-estimate metrics.** On the slots an outlook estimated at the 14:00 events, from the same
  seam call the planner used: MAE and bias (estimate minus realized), and per fully estimated local
  day, on hourly prices, the Spearman rank correlation and the overlap of the 10 cheapest and the 4
  most expensive hours. It also reports the MAE on days with a negative price and on DST days, and
  how often an estimator fell back and why.
- **Climate-mask overlap (`--features`).** At each 14:00 event, AppDaemon's `climate_control` is
  replayed for the fully estimated day twice: on the outlook's prices and on the realized prices
  (rule version of the day, enable flag and `min_hours_per_day` as of the decision, trailing mean
  from published prices). The report gives the mean hour overlap (Jaccard) of the two masks and the
  share of the day's measured heat-pump heat and cool energy that falls inside each mask. This is
  what the P4 structured forecaster needs from a price outlook.
- **Gate GC.** Pre-registered when an OMIE outlook is evaluated. An estimator passes only if, in
  every period, it beats `persistence` on mean and median within-day Spearman, on the cheapest-10
  overlap, on the mask Jaccard and on the climate energy inside the estimated mask. The dispatch
  criterion of the plan is not applied because GA failed; the share of the GA gap captured is
  reported for information. Without `--features` the rule-hour criteria are not computed and the
  verdict is `UNDETERMINED`.
- **Reference reproduction.** With `--reference-summary`, the `exact` rolling-horizon schedule is
  compared column by column with the `trailing_mean` run of a `forecast-eval` summary.

## Throughput cost, input validation and artifacts

**Throughput cost (`--throughput-cost-eur-per-kwh`, complete).** Planning-only term on a shared
basis. User-supplied **hypothetical** economic convention, **not a physical aging model, not
calibrated**. `0.0` (default) leaves plans bit-identical; never enters reported cost. Recorded in
run identity. See [Economic sensitivity](architecture.md#economic-sensitivity-economic_sensitivitypy).

**Invalid input is refused, in one line, with exit 1 and no traceback.** All five paths were verified:
cadence < 1; horizon < 1; `--rolling-horizon-horizon-slots` given **without** a cadence — refused on purpose,
because the legacy schedule derives its horizon from the local-day partition, so accepting the flag would
emit an artifact claiming a horizon the run never used; sigma < 0 or non-finite; bias sigma < 0 or non-finite.

Per-day solve **wall-clock** times are deliberately absent from the artifacts: runs are
content-addressed and written no-clobber (`write_part_atomic` refuses the same run id with different
bytes), so a timing field would make an identical re-run fail and would destroy byte-level
reproducibility. Deterministic per-day telemetry (`solver_status`, `is_certified`, `mip_gap`, the
count of days that consumed their limit) is recorded instead; the rolling rung adds the same telemetry
**per event**, plus `prices_exact_end_idx` and `estimated_price_intervals`, so each solve's information
set is readable from the artifact rather than having to be re-derived.

Artifacts follow the existing precedent: `BENCHMARK_RESULT` and `Schedule.as_dict()` are unchanged,
and the day-ahead detail lives in `benchmark.json` (`day_ahead.*`) plus the wide
`schedules.parquet` (`policy="day_ahead"` rows).

The causal policy is deliberately **not** a replica of the household's AppDaemon rule; AppDaemon's
effect is already embedded in the measured load, which is why the *no-battery* policy is the
counterfactual. `day_ahead` shares the *information* AppDaemon uses (tomorrow's prices), not its
thermal actuators or its rule.

## Oracle certification and solver noise

**Oracle certification:** certification is a property of the time limit, not of the oracle. At the
default 120 s on a full-year horizon the oracle returns a **time-limited incumbent**
(`is_certified=False`, its cost an upper bound), so the implied saving is only a lower bound on
optimal saving; at 600 s or more on that same horizon it **certified within tolerance**
(`solver_status="optimal"`, `bound_kind="certified"`) and is reported as a proven optimum. ADR-0003
§*Benchmark methodology clarifications* carries the measured telemetry for both. The knob that decides
this is the whole-horizon `--time-limit-s` above — not `--day-ahead-time-limit-s`, which is a separate
per **local day** limit (default 5.0 s) on the day-ahead solves and certifies nothing about the oracle.

**Solver noise at the no-export bound:** HiGHS can return a discharge one ulp above the load that
its own no-export row is bounded by, so a plan `battery.check_feasible` accepts (its slack is
`_TOL_KWH = 1e-6` kWh) can carry a net grid import of `-1.4e-16` kWh. `cost.compute_interval_cost`
refuses *any* negative energy by design — export credits are not modelled — so the orchestrator
costs a negative inside the kernel's own slack as zero and reports how many intervals needed that per
policy in `cost_consistency.<policy>.grid_noise_snapped_intervals` with an explanatory
`grid_noise_note`. Beyond that slack a negative is never clamped and the cost model's error is the
correct, loud outcome. This was found by a run over the then-four-policy ladder on synthetic data (the final
interval of a 100-slot fall-back day), not by the day-ahead policy itself — its clipped plans come out with
exact zeros, which `TestGridNoiseSnapping` pins. The check is a loop over the policies that ran, so the
fifth rung is covered by the same code path.

## Cost basis

**Cost basis:** all cost figures are **pre-tax** (untaxed energy cost). Tax rates
(`iee_pct`, `iva_pct`) are null/unconfirmed in `config/tariff.yaml`, so no billed number
exists. Fill taxes from a verified invoice to get a taxed basis.
