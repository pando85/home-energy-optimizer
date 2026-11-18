# DHW price scheduling: analysis and owner decisions

- **Status:** Notes and owner decisions, 2026-11-18. No control change; nothing is authorised by this page.
- **Date:** 2026-11-18
- **Related:** [comfort-controller-design section 7.2](comfort-controller-design.md#72-dhw-stays-price-optimised-amends-adr-0006-decision-4)
  (the DHW rule), [heat-pump-controller](../components/heat-pump-controller.md) (the legacy owner and the heater
  facts), [regime log](../components/regime-log.md) (rows 2025-12-29 and 2026-11-07),
  [ADR-0006 amendment 2026-10-31](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md#amendment-2026-10-31),
  [ClimatePlan contract](../climate-plan-contract.md) (the `dhw` field).
- **Privacy:** rounded aggregates, times and temperatures only; no datasets, costs, addresses or tokens.

All figures below were read-only on 2026-11-18 (Prometheus and HeishaMon history). Nothing was written to the unit.

## 1. Current rule

The homelab AppDaemon `dhw_control.py` forces hot water (`switch.aquarea_force_dhw_mode`) in the cheapest hour of each
12 h block (interval 12 h since 2025-12-29, see the [regime log](../components/regime-log.md)). The unit's own
thermostat still reheats the tank whenever it falls below the target minus its reheat hysteresis. The tank read at most
51 to 52 degC on 2026-11-08 to 11-18, and the DHW heater operating hours are unchanged (0).

## 2. Observation, 2026-11-11 to 11-18

Forced-hour runs fell in the night and early morning (about 03:00 to 05:40) and at midday (about 11:00 to 15:50),
local time. There was also a run at about 18:15 to 18:35 local on 6 of 7 days, about a third of the DHW runs, at the
start of the evening price peak. The cycle stays fixed in UTC (regime log row 2026-11-07), so it looks like a
unit-side schedule rather than demand. **The source is unverified.** Some forced runs are very short when the tank is
already near target (for example 5 minutes on 2026-11-14 at 13:04).

## 3. Load context

At outdoor 12 to 16 degC (2026-11-08 to 11-18), DHW took about 1.5 to 2.7 kWh/day and space heating about 0.4 to
2.5 kWh/day (HeishaMon per-mode energy).

## 4. Who plans DHW

The climate optimizer (comfort MPC, live shadow) does not plan DHW. It takes the DHW slots as an input
(`input_text.comfort_dhw_plan`, else yesterday's measured slots; see
[design section 13, B2](comfort-controller-design.md#b2-interface-to-the-controller-read-only)), and the ClimatePlan v1
`dhw` field is informative only ([contract](../climate-plan-contract.md)). Consequence: retiring AppDaemon without a
new DHW owner ends DHW price shifting. The tank would only reheat when the unit decides, price-blind.

## 5. Owner requirement and decisions

- **Requirement (2026-11-18): hot water must always be hot.** The unit keeps reheating on demand for comfort. Price
  acts only by forcing extra cycles in cheap hours.
- **Decision: the tank target stays at 50 degC all day.** It is never above 50 degC, so the 3 kW heater never starts,
  and legionella stays off (owner decision of 2026-10-31, [design 7.2](comfort-controller-design.md#72-dhw-stays-price-optimised-amends-adr-0006-decision-4)).

### Considered and rejected: a setback

A setback would lower the tank target outside the cheap window (for example about 45 degC) and use 50 degC inside it.
It was attractive for two reasons:

- Forcing only adds cycles; it never prevents an on-demand reheat at peak price. A lower target outside the window
  moves the reheat threshold (target minus hysteresis) down, so normal draws wait for the cheap window.
- A target stops when the tank is full, whereas a fixed 1 h force can stop early or run on a full tank.

The owner rejected it on 2026-11-18 because it trades comfort for price: the floor is the target minus the unit's reheat
hysteresis, which could leave the tank lukewarm. Also noted: each target change is a settings write that the unit
probably persists.

## 6. Improvements that keep comfort (proposed, not implemented)

1. **Identify the source of the 18:15 run** (read-only: HeishaMon and the unit's timer settings). If it is a unit
   timer, disable it: it runs at peak and is not need-driven. If it is a demand reheat after afternoon use, keep it
   and rely on item 2.
2. **Time the forced run in the cheapest hour before the evening use**, instead of the cheapest hour of each 12 h
   block, so the tank enters the evening full and needs fewer peak reheats. The rule of
   [design 7.2](comfort-controller-design.md#72-dhw-stays-price-optimised-amends-adr-0006-decision-4) (cheapest 2 h
   window ranked by price divided by expected COP, avoiding the optimum-start warm-up) stays the target rule for
   whoever owns DHW.
3. **Limit:** little energy can be stored because of the 50 degC cap, so timing matters more than forcing harder or
   more often.

## 7. Coupling with the battery

Whoever schedules DHW should publish the DHW slots to the battery load forecast, following the no-double-count rule of
[ADR-0006](../adr/0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md). The battery's 4.5 kW import
guard already covers an overlap of DHW and charging ([heat-pump-controller](../components/heat-pump-controller.md#heater-facts-import-limit-inputs)).

## 8. Open

**Decided 2026-11-18 (owner, [ADR-0009](../adr/0009-live-climate-control.md)): DHW stays with AppDaemon
`dhw_control.py`** (force-DHW switch `switch.aquarea_force_dhw_mode`, tank target 50 degC) in the release in which the
optimizer becomes the space-heating writer. The optimizer never writes DHW, the tank target, the heater, sterilization
or cooling, and the allowlist of [ADR-0007](../adr/0007-live-battery-control.md) widened by ADR-0009 has no DHW entity.
`dhw_control.py` is therefore not retired at the switch, and the question of this section (which component owns DHW
once AppDaemon is gone) is deferred: a future DHW owner needs its own ADR. Items 1 and 2 of section 6 stay proposals
for whoever owns DHW.

Confirmed at the switch (2026-11-18, owner set the select `live` about 13:28 UTC): DHW stayed with AppDaemon `dhw_control.py`;
only space heating moved to the optimizer ([regime log](../components/regime-log.md)).
