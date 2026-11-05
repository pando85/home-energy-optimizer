# Climate preregistration, winter 2026-27

- **Committed:** 2026-10-31, **before any 2026-27 heating data has been seen**. The last heating data in the
  feature set ends 2026-05-01 and space heating has been off since (see the [regime log](../components/regime-log.md)).
- **Covers:** (A) the shadow thermal model and MPC (track B, step B3), (B) the comfort controller
  before/after comparison ([design section 12](comfort-controller-design.md#12-evaluation)).
- **Review:** the owner reviews these criteria **together with the homelab comfort-controller PR before
  go-live**. Thresholds change **only before go-live**, through a dated, visible amendment (section 4). After
  go-live nothing here is edited except to add a result.
- **Not in scope:** euro figures, savings and absolute energy totals. Every criterion is relative to a
  stated baseline or a physical unit.
- **Context:** [C2 recovery, section 7](climate-c2-recovery.md#7-status-2026-10-31-b0-results-r0-to-r2) and the
  [thermal model](../components/thermal-model.md) page. The numbers of B0 are exploratory and are not these
  criteria's result.

## 1. Shared definitions

- **Regimes.** *Legacy regime:* the shadow deployment until go-live of the comfort controller. *New regime:*
  go-live plus 7 days (settling) through **2027-03-31**. The days between are excluded from both.
- **Valid heating day.** A day with space heating running in at least part of the day, and with the
  thermostat-room temperatures, the heat-pump energy source and the outdoor series present for at least 90%
  of the 15-minute slots. The owner confirms this definition at review.
- **Room-mean.** The mean of the thermostat rooms listed in [design section 3](comfort-controller-design.md#3-comfort-schedule-and-setpoints).
  *Amended 2026-10-31 (section 4):* bedroom, hall_kitchen and office_t (`comfort.rooms`), office A excluded.
- **Persistence.** The last observed value held over the horizon. **Skill** is
  `1 - RMSE(model) / RMSE(persistence)` on the same forecast origins.
- **UNDETERMINED** means the data requirement was not met. It is never read as a pass.

## 2. A. Shadow thermal model and MPC (track B, B3)

The candidate is the 2-state, room-air-only model with a weekday/weekend daily gain and weekly re-estimation
(C2 recovery section 7.3), predicting from forecast origins in the live data. **Two separate windows, two
separate verdicts:** the legacy regime and the new regime. Each verdict needs **at least 6 weeks of valid
heating days**, otherwise **UNDETERMINED**.

| Criterion | Threshold |
|---|---|
| Room-mean RMSE at 6 h | at most 0.30 K |
| Room-mean RMSE at 12 h | at most 0.40 K |
| Skill versus persistence | at least 25% at **both** horizons |
| Mean bias at 6 h | absolute value at most 0.15 K |
| Daily heat-delivery error | median absolute percentage error at most 15%, on days whose heat is above 20% of the window's median daily heat |

**Verdict.** PASS on all criteria: the MPC becomes a **candidate** for a real alternating-arm trial in a later
season, which is a separate decision with its own preregistration. FAIL on any criterion: the MPC is not
promoted.

The MPC plan-versus-rule comparison (predicted kWh of the MPC plan against the realised kWh of the rule) is
**descriptive only** and grants nothing: the MPC never acts, so its realised comfort cannot be observed.

## 3. B. Comfort controller before/after (design section 12)

The **new regime** is compared with the **S3 legacy baseline** (winter 2025-26), **matched by outdoor-temperature
band** (the bands of the warm-up analysis: below 4, 4 to 8, 8 to 12 and 12 degC and above; a band with fewer than
10 days in either regime is pooled with its neighbour). Heat-pump electricity is taken on the **heat-pump
branch meter or HeishaMon basis, never the whole-house meter**. At least **4 weeks of valid heating days**
are required, otherwise **UNDETERMINED**.

| Group | Criterion | Threshold |
|---|---|---|
| Comfort | Hall/kitchen below 23.5 degC (per room since the amendment of 2026-11-05: each room with a day minimum above its floor, below its own day minimum) | at most 10% of the day-window time |
| Comfort | Each thermostat room below 21 degC (per room since the amendment of 2026-11-05: below its own floor) | at most 3% of all time |
| Efficiency | Heat-pump electricity per heating degree-day (base 15 degC, as `HDD_BASE_C` in `hack/system_sim.py:64`, so it stays on the replay's basis; daily mean outdoor) | at most +20% against the S3 band-matched baseline |
| Machine | Compressor starts per day | median at most 20 |
| Machine | Electric heater | no controller-caused activation (DHW heater hours unchanged) |
| DHW | Share of DHW heat-pump energy in the scheduled window | at least 80% |
| DHW | Tank temperature from the scheduler | never above 50 degC |

**Rationale for the efficiency margin.** The comfort target is about **1.3 K above** S3's realised day mean in
the hall/kitchen, so a higher use is expected: roughly **+8 to 13%**. The +20% limit allows that plus
estimation error and no more.

**Verdict.** **GO** (keep the controller) if every criterion passes. **NO-GO** if any fails: `git revert` in
homelab (design section 10). **UNDETERMINED** if the data is insufficient; the controller stays and the
period is extended or the comparison is repeated next winter.

**Strength of evidence.** A before/after comparison is **weaker than a randomised trial**. Weather, occupancy,
tariff, the setpoint and the water target all differ between periods, and the band matching and the
degree-day normalisation are imperfect. A GO means "no sign of harm against these limits", not a measured
saving. Every result is reported with this limit stated.

## 4. Amendments

Allowed only **before go-live**, one dated entry each, naming the threshold, the old and the new value and the
reason. The commit history of this file is the audit trail. Two amendments have been made, both **before
go-live** (the comfort controller is not live, so no new-regime data exists). The first, on
2026-10-31 (four rows): the shadow runner (B2) was not deployed; no threshold value changed, the rows fix
definitions the text left open or that disagreed with the evidence the thresholds came from. The second, on
2026-11-05 (one row), makes the comfort criteria per room; with the default helper values it reproduces the
old criteria exactly. Where a row and section 1, 2 or 3 differ, the row applies.

| Date | Criterion | Old | New | Reason |
|---|---|---|---|---|
| 2026-10-31 | Room-mean (sections 1 and 2) | The mean of the thermostat rooms of design section 3 (four rooms, office A included) | The mean of `comfort.rooms` of `config/climate.yaml`: bedroom, hall_kitchen and office_t, which is the model's `t_air` with zero offset; persistence is the same mean held. Office A is excluded as a server heat sink. The valid-day room coverage reads the same three rooms. The four-room mean is reported as a diagnostic only | The thresholds come from B0's 7.3 numbers (0.249 / 0.325 K), which are errors of `t_air` over these three rooms; a four-room mean would test something the thresholds were not derived from |
| 2026-10-31 | Prediction basis (section 2) | Not stated | The prediction is driven by the **measured heat and the measured outdoor temperature** (`t_out_model`, the last valid reading held where it is unknown), from the shadow's observer state at each forecast origin; the prior fit uses the same outdoor series. It tests the thermal model, not the operational weather forecast; the error with the forecast the plan used is a diagnostic | The B0 7.3 basis the thresholds came from |
| 2026-10-31 | Daily heat-delivery error, new regime (section 2) | Not stated (the legacy permission `u`) | Legacy regime: the heat law driven by the replayed permission `u`. New regime: driven by the **measured space-heating run**, because the rule and its `u` are retired. This is a weaker test of the heat law (it conditions on heat being delivered). The median APE and the 20%-of-median day filter are unchanged | `u` does not exist after go-live |
| 2026-10-31 | Go-live (section 1) | Not stated how it is fixed | The new window needs a configured go-live date (`climate_shadow.go_live`); without one there is no new-window verdict. A date taken from the records (the first `comfort` record) is reported as provisional only and never fixes a window | A window boundary must not move with the data |
| 2026-11-05 | Comfort criteria (section 3) | Hall/kitchen below 23.5 degC at most 10% of the day-window time; each thermostat room below 21 degC at most 3% of all time (one shared floor and one day minimum, hall/kitchen only) | **Per room, against the schedule in effect at each slot.** The controller holds `input_number.comfort_floor_<room>` and `input_number.comfort_day_minimum_<room>` for `hall_kitchen`, `bedroom`, `office_a` and `office_t`; the effective day minimum of a room is `max(day minimum, floor)`; the day window is shared and unchanged (weekdays from 12:00, weekends from 09:00, until 22:00). (1) Each thermostat room is below **its own floor** at most **3%** of all time. (2) Each room whose day minimum is above its floor is below **its own effective day minimum** at most **10%** of the day-window time; the share is taken over the slots of the window in which that room's day minimum in effect is above its floor in effect, and a room with no such slot in the evaluation window is not evaluated by (2). Values are the helper values in effect at each 15-minute slot (the series `hass_input_number_state{entity="input_number.comfort_floor_<room>"}` and `..._day_minimum_<room>`, last sample at or before the slot start). A slot whose series is missing, older than 2 hours, or outside the controller's ranges (floor 18 to 23 degC, day minimum 18 to 26 degC) uses the defaults (floor 21.0 in every room; day minimum 23.5 in `hall_kitchen`, 21.0 elsewhere) and is counted in the reported share of default-valued slots. **Defaults reproduce the old criteria exactly**: hall/kitchen is the only room with a day minimum above its floor, at 23.5 against 21.0, and every floor is 21.0. The thresholds (10%, 3%), the room-mean (bedroom, hall_kitchen, office_t) and the exclusion of office A from the room-mean are unchanged; office A stays one of the thermostat rooms of the comfort criteria as before. **If the owner changes a value during the evaluation**, each slot is judged against the value in effect at that slot (no retroactive re-scoring, no restart of the window); every change (time, room, helper, old and new value) is listed in the result, taken from the helper series and from the shadow's `inputs.comfort` record, and the result also states the shares against the default values as a diagnostic (reported, not a gate) | One shared floor and one hall/kitchen day minimum no longer describe the controller: the owner chose per-room helpers (2026-11-05) so that, for example, an office can be kept warmer than the rest. Judging each room against the minimum it was actually set to keeps the criteria meaningful and tied to the controller, while the defaults and the diagnostic keep them comparable with the S3 baseline and stop a lowered setting from passing by itself |

## 5. Items for the owner at review

- The validity definition (section 1) and the room list (design section 3; for section A fixed by the amendment of 2026-10-31). The degree-day base follows `hack/system_sim.py`.
- The per-room comfort amendment of 2026-11-05 (section 4): the per-room wording, the denominator of the day-minimum share, the fallback to the defaults for a missing or out-of-range helper value, and the diagnostic against the default values.
- The go-live date: it fixes the start of the new regime and the end of the legacy window. It is set in the service config (`climate_shadow.go_live`); a date read from the records is never used (amendment of 2026-10-31).
