# Known issues

## HA retention: declared 10 days, effective boundary unverified

`apps/home-assistant/config/configuration.yaml` sets only `recorder.db_url` —
there is no `purge_keep_days`, no `commit_interval`, and no `exclude`/`include`
override, so Home Assistant's documented default of 10 days applies. The
*effective* coverage boundary is unverified and may be shorter; this package
does not probe retention or write synthetic rows to test availability (doing
either would change live Home Assistant configuration, which ADR-0003 forbids
in this phase).

A 2026-10-23 run with `--lookback-days 9` declared 10 chunks, wrote 9 parts
(39 rows), and observed one empty chunk (2026-10-14 → 2026-10-15). With 10-day
retention reaching back to roughly 2026-10-13, that empty chunk is **not**
explained by retention — with only two appliances producing 39 rows in nine
days, a day with no state transition is the likelier reading. An earliest
*observed row* is not a retention boundary; the entities may simply not have
changed state.

The operational conclusion stands regardless of the exact number: enum history
must be extracted recurringly well inside the declared retention because **HA
recorder history is not re-extractable**, unlike the Prometheus archive. The
recurring production schedule is **still not implemented**; today the cadence is
manual, on windows that deliberately overlap. That is only safe because
re-extraction is deduplicated at row level — see
[Overlapping re-extraction is deduplicated at row
level](#overlapping-re-extraction-is-deduplicated-at-row-level).

## Per-chunk emptiness is now captured in `quality_flags`

`extract_ha.py` materialises every declared chunk into a `ChunkOutcome`
*before* fetching, so a zero-row chunk produces flags rather than vanishing
from the manifest. A declared/written mismatch raises
`chunk_accounting_mismatch` with the empty windows named; per-chunk ledger
entries live in `params.chunk_outcomes[]` and summary counts in
`params.chunk_accounting`. Two flag prefixes distinguish the cause:
`empty_chunk:` (payload carried nothing — causes indistinguishable) and
`empty_chunk_after_window_filter:` (payload carried rows, none inside the
half-open window). `coverage_notes` spells out the disagreement in words.

The mismatch is reconciled against `parts_written + fully_suppressed_chunks`,
so a chunk that found nothing new because everything was already stored is not
counted as a gap — only a chunk that yielded no new observation at all is.

## Overlapping re-extraction is deduplicated at row level

Manual extraction on a short recurring cycle overlaps windows by design (10-day
retention leaves no room for gap-free scheduling), and ADR-0003 asserts that
overlapping pulls are "cheap and idempotent to re-pull". That claim was false
until 2026-10-23: two real runs (9 parts / 39 rows, then 3 parts / 12 rows)
stored 51 rows for 39 distinct observations — 12 duplicates, 0 genuinely new
rows — while the second manifest recorded `quality_flags: []`, so it looked like
a clean, fully productive run even though the store had grown by ~31 %.

`extract_ha.py` now compares every candidate row against what is **already
stored** before writing:

- **Identity** is `(entity, last_changed, last_updated, state, attributes_json)`
  — the five `RAW_ENUM_STATE` columns that describe what Home Assistant recorded.
  `extraction_id`, `source_window_start` and `carry_in` are excluded: they describe
  the pull, not the observation. `extraction_id` is a fresh UUID per run, which is
  precisely why the content-addressed no-clobber store could never recognise two
  overlapping parts as duplicates — some of those part files were byte-identical
  except for that one column. Timestamps are compared as whole microseconds since
  the epoch, so a one-microsecond difference is never collapsed.
- **Prior coverage comes from the part files on disk**, not from manifests and not
  from window arithmetic: a row physically present is a row that must not be
  written twice, and a run that died after writing some parts left those parts
  behind. Manifests are scanned only to report provenance — which parts a successful
  run claimed, which are orphans.
- **The requested window is never narrowed.** Chunk boundaries stay a deterministic
  function of `[start, end)` so carry-in anchoring, part naming and part content all
  remain reproducible; dedup happens per row on top of that.
- **Nothing already stored is touched.** Dedup means not writing a duplicate —
  never rewriting, appending to, or deleting an existing part.

Accounting is recorded per chunk in `params.chunk_outcomes[]` and in totals in
`params.row_accounting`: `raw_rows` (what the response carried), `rows_seen`
(candidates surviving carry-in detection, the half-open filter and intra-chunk
dedup), `duplicate_rows_suppressed` (already present, not re-written), and
`new_rows` / `rows_written` (identical, because a new row is always written — a
failing write aborts the run before any manifest exists). `params.prior_coverage`
records what was scanned: parts, rows, distinct keys, pre-existing duplicates,
orphan parts and unreadable parts. `params.row_identity` records the key itself.

When any row is suppressed the manifest raises
`duplicate_rows_suppressed:<window>: seen=… already_present=… new=… rows_written=…`
for that chunk plus a `duplicate_rows_suppressed:total:` entry. **This is an
accounting disclosure, not an alarm:** on an intentional overlap suppression is the
expected outcome, each observation stays stored exactly once, and nothing is lost.
It is deliberately distinct in prefix and wording from the genuine-problem flags
(`empty_chunk:`, `empty_chunk_after_window_filter:`, `chunk_accounting_mismatch`,
`no_rows_for_entity:`, `no_rows_extracted:all`). A chunk whose rows were all already
present is marked `fully_suppressed`: no part file, but not a coverage gap, so it
does not raise `chunk_accounting_mismatch` either — and `no_rows_extracted:all` is
keyed on rows *seen*, so a productive overlap never claims that coverage is missing.

Three further disclosures describe real problems rather than intent:

- `carry_in_classification_disagreement:<window>` — a suppressed duplicate would be
  classified as carry-in differently from the copy already stored, which means the
  two runs' chunk boundaries did not line up. The stored row is kept as-is; check
  the chunk width and window start.
- `duplicate_rows_already_stored:<n>` — the prior parts themselves hold a logical key
  more than once, the residue of pre-dedup overlaps. This run reports the count and
  repairs nothing.
- `prior_part_unreadable:<part name>: <error type>` — a damaged part contributed no
  keys, so rows it holds may be written again. Duplication is the acceptable failure
  mode; losing not-re-extractable history is not. Only the part *name* is recorded,
  never its path.

**Idempotence holds:** a re-run of a window that is already fully stored writes zero
new parts and zero rows, discloses the suppression counts, and raises neither
`no_rows_extracted:all` nor `chunk_accounting_mismatch`. Covered by
`tests/test_extract_ha.py::TestCrossRunDedup`, including the mis-aligned-chunk case
where the tail chunk's window differs from the run that stored its rows and still
duplicates nothing. Idempotence is exact when the chunk width stays the same across
runs: carry-in rows are anchored to the chunk start (unchanged, deliberate behaviour),
so a run with a different `chunk_seconds` also synthesises carry-in rows at new anchors
— genuinely different rows, which are written, not deduplicated. Keep the chunk width
constant and overlapping pulls cost nothing.

**The 12 rows already duplicated in the private data dir** (manifest `1ec3085f…`,
overlapping `750bce0e…`) were not — and will not be — removed by this package. They
were inert because `build.py` has no HA reader at all, and they stay inert for a
consumer that does exist: the read-side `ha-states` reader (see
[§`ha-states`](cli.md#ha-states)) **collapses them at read time** on exactly the identity
described here, rather than rewriting immutable history. Two things follow. First,
the read-side rule was mandatory for any HA reader regardless of when the store
is cleaned, and it is now implemented: dedup on the same row identity, exactly as the Prometheus path collapses
`(series_key, ts, value)`. Second, if a physically clean store is ever wanted, either
revert the whole contributing run (its manifest together with the parts that *only*
that manifest claims, after verifying every one of their keys also exists in another
part, and after a snapshot) or rebuild into a fresh data directory. Never rewrite a
part in place, and never delete a part while its manifest still claims it — `validate`
checks manifest part hashes against the files on disk, so a half-revert breaks
validation instead of fixing it.

## `source_window_start` / `carry_in` flags

Both fields are preserved in the raw Parquet for every HA row. `carry_in=True`
marks rows whose `last_updated` was at or before the chunk start (not a real
transition). Consumers must filter on `carry_in=False` before treating
`last_changed` as a real event time.

Both are provenance about the *query*, not about the observation, which is why
neither is part of the dedup identity above: `source_window_start` is anchored to
the chunk start, and `carry_in` is derived from it. That anchoring is what makes
carry-in rows collapse correctly on re-extraction — the same chunk start produces
the same synthetic row, so an overlapping re-pull suppresses it like any other
duplicate rather than inventing an appliance event per run.

## Battery: findings of the commissioning test (2026-11-03)

A supervised test of the Marstek Venus E V3 behind Omnibattery v1.5.0 on the commissioning evening (backup
switchover, an 800 W charge, a discharge, HA restart and kill) left the issues below. The measured facts that
are now configuration (AC power sign, AC-side counters, commissioning instant) are in `config/service.yaml`
and the [regime log](components/regime-log.md); the live-control consequences are in
[ADR-0007](adr/0007-live-battery-control.md#commissioning-findings-2026-11-03).

### The PD target is bounded by the system caps while the power limits are on

With `switch.omnibattery_system_power_limits` on, Omnibattery bounds `number.omnibattery_pd_target_grid_power`
to `[-system max discharge, +system max charge]` (-800..+1000 W with the caps 800/1000 W on the test socket),
not to the batteries' ±2500 W. The actuator reads the entity's live `min`/`max` attributes and never turns the
power limits off; the discharge mapping turns them on. So every charge after the first discharge (and today,
with the owner's caps left on) gets `min(house load + P, import_cap_w, system max charge)`: the charge is at
most `system max charge - house load` (also capped at the system max charge itself), and **zero** once the
house load reaches the system max charge, since the discharge switch is off. Nothing fails: the target is in
range, the readback confirms it, the guard passes and the heartbeat runs; only `clamped: true` in the
`actuation/` record and the gap between desired and observed battery power show it.

This is the **pre-go-live state** (battery on the temporary test socket, caps 1000 W charge / 800 W
discharge). The **go-live configuration** (owner decision) sets both system caps to the battery's rating:
`number.omnibattery_system_max_charge_power` = `number.omnibattery_system_max_discharge_power` = **2500 W**,
so the PD target is bounded to ±2500 W, as ADR-0007 decision 4 assumes, and the charge is at most
2500 W − house load. Discharge above 800 W also needs **Premium mode** in the Marstek app (the firmware limits
discharge to 800 W without it). The planner (`config/tariff.yaml` `battery.power_kw: 2.5`) and the actuator
(`actuation.max_discharge_cap_w: 2500`) already assume 2500 W both ways, so the owner must enable Premium at
go-live; without it a discharge intent above 800 W is under-delivered by the firmware, which the actuator
cannot see (the cap it writes is in range). No code change is planned for this. Turning the power limits
off in charge mode or writing the system max charge from the service stays an owner decision (it removes the
owner's own cap and needs an ADR-0007 amendment). The cut is visible in `dry_run` and `live` as `battery_optimizer_actuator_clamped_watts`: the
planned battery power the last tick could not request (0 when nothing was cut; see
[service metrics](service.md#metrics) for the alert).

### Planning from the SOC entity checks availability and liveness, not age

Omnibattery reports the SOC only on change and HA does not refresh `last_reported` for an unchanged value
(verified 2026-11-03, ADR-0007 decision 6), so with the old 900 s age test an idle battery's SOC looked stale
after 15 minutes and the planner refused to plan (`soc_stale`); in `live` that would also have blocked the
first charge out of idle. When Modbus fails Omnibattery keeps the last SOC instead, so the age test protected
nothing. Resolved: `freshness_s.battery_soc` is `null` by default (availability only: present, not
`unavailable`/`unknown`, numeric, within 0..100 %), as the actuator's `actuation.freshness_s.battery_soc`, and
the planner also requires `actuation.entities.battery_liveness`
(`sensor.omnibattery_non_responsive_batteries`) to read exactly `None`, the actuator's
`battery_unresponsive` check; any other state refuses to plan with `soc_stale`. With
`planning.soc_source: entity` that sensor is tracked in every mode. A number in `freshness_s.battery_soc`
restores the age test. The committed config now uses `soc_source: entity`.

### Idle battery counters voided a live slot's gross load

With the battery mapped, a slot record's gross load needs both AC energy counters at both slot edges. The
observer took an edge value only if it was no older than `observation.counter_max_age_s` (300 s), but
Omnibattery reports the counters on change only (like the SOC; seen at go-live, 2026-11-03: an idle
battery's counters keep an old `last_reported`), so the idle direction's counter was always older than
that, and one unknown counter voids both: from go-live every battery-mapped slot had
`battery_charge_kwh`, `battery_discharge_kwh`, `gross_load_kwh` and the counterfactual cost `null` (not
wrong), also while discharging. The grid import counter had the same flaw whenever the battery held the
import at zero. The same behaviour made the counters look stale in `/status` and
`battery_optimizer_input_stale` under the 900 s they inherited from `freshness_s.grid_energy`.

Resolved in two steps. `freshness_s.battery_charge_energy` / `battery_discharge_energy` are `null`
(availability only) by default, as `battery_soc`, and a number restores the age test. At a slot edge the
value is the last reading at or before the edge, used when the entity is available and the HA session was
in sync at the edge (from the applied `get_states` to the disconnect, backed off by the heartbeat interval
plus timeout); an edge before the first sync after a start or inside a disconnection stays unknown. The
battery counters and the SOC add their `freshness_s` age test at the edge only when it is a number; the
grid counter has none. So the slot that contains a restart has no start SOC or counters.
`observation.counter_max_age_s` is gone (ignored if still present). Slot records written before the edge
fix was deployed keep the `null`s; the Prometheus history was never affected (it samples on every scrape),
and slots that end before `battery.commissioned_at` use the grid counter alone.

### The PD loop oscillates; the discharge margin was too small

Omnibattery's PD loop swings by about ±150 to 200 W on a ~2 s period. With the original 50 W
`discharge_margin_w` the grid went to -10..-94 W during the discharge test (Shelly `total_returned` stayed
0.0 Wh). The committed config uses 200 W (the code default stays 50 W); a smoother Omnibattery PD tuning
profile (owner setting) would allow a smaller margin.

### The battery holds its last command when Home Assistant is down

On a clean HA restart (40 s down) and a forced pod kill (23 s down) while discharging, the battery kept
discharging at about 700 W and the grid stayed steady: it does **not** go to standby, and Omnibattery does not
set it to standby on shutdown. On start Omnibattery re-took control with the switches persisted (a brief dip
to about 190 W). A longer outage (about 10.5 minutes, HA stopped while discharging) gave the same result: the
battery held about 620 W of discharge for the whole outage (read directly over Modbus), with no firmware
timeout and no standby; Omnibattery resumed control at once when HA came back. So there is **no device
timeout of at least 10 minutes**, and the HA watchdog and the service's safe idle cannot stop the battery while
HA is down: during an outage it neither follows the house load (a discharge can export when the load drops)
nor backs off a charge on a load step (the import cap is not regulated). Live mode needs a failsafe that does
not depend on HA. See ADR-0007.

### Omnibattery can stay in `backup_mode` after the grid returns

After a grid return the inverter goes back to Bypass, but Omnibattery v1.5.0 can stay in its `backup_mode`
cooldown: an upstream race (in `coordinator.py` the key-update timestamps are set mid-poll, the data is
published later) skips the Bypass zeroing, and a backup-port load above `number.venus_backup_offgrid_threshold`
(default 50 W; 57 to 85 W observed) keeps refreshing the 5-minute cooldown. While it lasts
`integration_status` is `backup_mode`, so the actuator holds safe idle (`integration_unhealthy`), possibly
indefinitely. Upstream: issue ffunes/Omnibattery#660, fixed by ffunes/Omnibattery#723 (stage
`_key_update_times` together with `data.update`; targets `release/v1.5.1`).

**Workaround in place:** `number.venus_backup_offgrid_threshold` = **500 W** (Omnibattery default 50 W). A
threshold of 200 W cleared the state within 5 minutes in the test; the owner set 500 W the same evening. It
must stay above the backup-port load: the servers moved there on 2026-11-03 and draw about 225 to 255 W
(`sensor.venus_ac_offgrid_power`), below the 500 W threshold.

Side effects while the high threshold is in place:

- During a real outage with a backup-port load **below** the threshold, Omnibattery does not treat it as
  genuine off-grid demand: it keeps the battery in its PD loop and goes on commanding the islanded battery
  against a grid meter that no longer sees it. `integration_status` then need not be `backup_mode`, so the
  actuator's guard may pass and the actuator may keep writing as well.
- When the grid returns there is no 5-minute settle: below the threshold the cooldown never starts, so
  Omnibattery (and the actuator) resume control at once instead of waiting for the inverter to settle in
  Bypass.

**Removal procedure** (once an Omnibattery release containing ffunes/Omnibattery#723 is installed):

1. Check the installed Omnibattery version against the release notes for #723.
2. Set `number.venus_backup_offgrid_threshold` back to **50 W**.
3. With a load on the backup port and the inverter in Bypass (grid present), verify that
   `sensor.omnibattery_integration_status` returns to `balanced` within 5 minutes and stays there (no
   `backup_mode` latch). A short supervised grid-loss test (as in the commissioning test) then confirms
   Backup Mode on loss and the return to `balanced` after the grid comes back.
4. If it latches again, put 500 W back and reopen the upstream issue. Otherwise record the change in the
   [regime log](components/regime-log.md) and delete this workaround here and in ADR-0007.
