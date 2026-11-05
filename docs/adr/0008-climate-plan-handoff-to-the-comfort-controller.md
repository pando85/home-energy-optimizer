# ADR-0008: Climate plan handoff to the comfort controller

- **Status:** **Proposed** (2026-11-05; not accepted). Nothing in this ADR authorises any climate actuation.
  The infrastructure it describes ships **disabled** and the optimizer stays read-only toward Home Assistant
  for climate. Acceptance is gated by the [preconditions](#preconditions-to-accept-this-adr-and-flip-the-lock)
  below; the climate C2 gate is still UNDETERMINED / no-go.
- **Date:** 2026-11-05
- **Decision owners:** Homelab maintainers (the owner asked on 2026-11-05 for everything to be prepared and
  put in place behind a toggle that is disabled for now)
- **Extends:** [ADR-0006](0006-decouple-heat-pump-comfort-from-battery-price-optimisation.md) (decoupled comfort
  control; the plan consumer it left open), the plan-handoff design of the
  [climate implementation plan](../planning/climate-optimizer-implementation.md) (the optimizer only produces a
  plan, AppDaemon is the single writer, the optimizer keeps its read-only token).
- **Style reference:** [ADR-0007](0007-live-battery-control.md) (an actuation ADR). Unlike it, this ADR adds
  **no write path to this repository**.
- **Contract:** [ClimatePlan v1](../climate-plan-contract.md) and
  [`schemas/climate-plan-v1.json`](../schemas/climate-plan-v1.json). **Implementation (optimizer half):**
  `climate/plan_export.py`, `service/climate_shadow.py`, `service/runtime.py`, `service/api.py`, the
  `climate_shadow:` section of `config/service.yaml`. The homelab consumer is a separate repository's change.

## Context

The live climate shadow ([climate-mpc](../components/climate-mpc.md)) already plans space heating hourly for the
comfort schedule: per 15-minute slot, heating on or off and the water target, with predicted room temperatures.
It records and scores the plan and sends nothing. The owner wants the path prepared by which that plan could one
day drive the heat pump, without enabling it.

Three facts shape the design:

- **The optimizer must not become a climate writer.** Its Home Assistant token is read-only and its guard
  (`service/ha_guard.py`) admits exactly the battery allowlist (ADR-0007) and one forecast query. The comfort
  controller in AppDaemon already writes the heat pump through the proven command path and carries the machine
  limits, the comfort floors, the watchdog and the rollback.
- **The model has not earned control.** The C2 gate ended UNDETERMINED (no-go) and the 2026-27 shadow has not
  yet produced its preregistered verdicts. A plan is a prediction of a plan that was never executed.
- **Information must not be lost across the boundary.** A stale plan, a plan for another regime, a plan the
  controller cannot validate, or an owner's switch that is not understood must all lead to the same safe place:
  the controller's own rules.

## Decision

1. **The optimizer publishes an advisory plan; it never acts.** `GET /climate/plan` (and `/climate/plan/summary`
   for dashboards) on its own read-only HTTP surface returns the ClimatePlan v1 of the latest hourly decision:
   `advisory: true` always, a short validity (`valid_until` = decision time + 90 min), per 15-minute slot the
   planned space heating on/off and the absolute water target (already within 20 to 35 degC), predicted room
   temperatures for monitoring, the comfort levels used, the regime, the code version, the mode fields and a
   content hash (`plan_id`) for idempotency. No Home Assistant call, Modbus or REST write is added; `ha_guard.py`
   and its allowlist are unchanged; the read-only token is unchanged.

2. **AppDaemon's comfort controller is the only writer**, as for every other climate actuation. It consumes the
   plan; no other process may write the heat pump, and the optimizer is never given a token that could.

3. **Three modes, owned by the owner in Home Assistant.** `input_select.climate_optimizer_mode` with the options
   exactly `off`, `shadow`, `live`; the optimizer reads it (read-only, like the battery kill switch), and the
   consumer reads it too.

   | Mode | Optimizer | Comfort controller |
   |---|---|---|
   | **Rules** (`off`) | no plan served (`404`, `mode_off`); the shadow keeps measuring | its own rules, as today |
   | **Optimizer shadow** (`shadow`) | serves the advisory plan | reads and compares the plan, **writes nothing plan-derived** |
   | **Optimizer live** (`live`) | serves the same advisory plan, flagged `live_locked` | may act on a valid plan **only if the hard lock allows it** (decision 4) |

   A missing, unavailable or unrecognised select falls back to the optimizer's `climate_shadow.optimizer_mode`
   (default `"off"`); the consumer falls back to Rules.

4. **A hard lock in git blocks live regardless of the select.** The homelab consumer carries a constant
   `optimizer_live_allowed: false` in version control (a code or committed-config value, not an HA helper).
   While it is false, a `live` request behaves exactly like `shadow`: nothing is written from the plan, and the
   consumer reports "live requested but locked". The optimizer mirrors this: its **effective mode can never be
   `live`** in this code version (`live_locked: true`, reason "ADR-0008 proposed, not accepted; homelab hard lock
   optimizer_live_allowed=false", enforced in code and by test). Flipping the lock is a reviewed commit that
   follows the acceptance of this ADR; it is not a runtime setting.

5. **Comfort floors always win over the plan.** A room below its floor (the per-room
   `input_number.comfort_floor_<room>` helpers) forces heating whatever the plan says; the plan can never
   lower a floor or shorten the comfort schedule. While every room is at or above its floor the plan decides
   only the timing of heating and the water target; the consumer's run logic (thermostat hysteresis, optimum
   start) still applies on top of it.

6. **Fallback to the rules on anything doubtful.** The consumer treats the plan as absent, and runs its rules, when
   the request fails or is a `404`; the JSON does not parse or fails the schema; `version != 1`;
   `advisory != true`; `status != "planned"`; `now >= valid_until` (checked at every use); no slot covers
   `now`; or `plan_id` was already applied. Absent is never "off" and never "keep the last plan". A mode change
   to `off` or `shadow` revokes plan-derived control immediately.

7. **Fixed code limits stay in the consumer and cannot be set by a plan or a helper:** water target integers
   clamped to 20 to 35 degC; DHW tank at most 50 degC; compressor minimum on 30 min and minimum off 20 min; a
   water write only when the target changes by at least 1 K and at most once per 15 min; **no backup heater, no
   boost, no sterilisation**; direct-mode check before any water write. The plan exports only the two levers the
   MPC really plans (space heating on/off, water target); the contract states that setpoints, hot water, heater
   and cooling are not in it.

8. **Observability.** The optimizer exposes the plan's state, age, requested and effective mode, the live lock,
   and the planned heating and water target of the current slot as `/metrics` gauges and in `/status`; the
   consumer reports what it did with each `plan_id` (applied, ignored and why). A dashboard can show "live
   requested but locked".

## Preconditions to accept this ADR and flip the lock

All of them, in this order; none is satisfied today.

1. **A climate model that passes a new preregistered gate** on the C2-R recovery path
   ([climate-c2-recovery](../planning/climate-c2-recovery.md)): the original C2 gate stays frozen and is neither
   amended nor overridden; the shadow's section-A verdicts and the descriptive MPC-versus-rule comparison
   inform it but do not replace it.
2. **The owner's explicit go**, recorded in this ADR (status changed from Proposed to Accepted, with the date)
   and in the [regime log](../components/regime-log.md).
3. **A supervised first day**: the owner present, the rules one switch away, comfort and cycling watched, with the
   expected `Heating_Mode`, who controls DHW and how to verify the pump after stopping AppDaemon stated in the
   homelab change (the plan's rollback rules).
4. Only then the reviewed homelab commit that sets `optimizer_live_allowed: true`. Rollback is a revert of that
   commit or the select set to `shadow` or `off`.

## Consequences

- The export is harmless on its own (a read-only `GET`), so it can ship and be turned on, to `shadow`, for
  dashboards and for the consumer's shadow comparison, without any authorisation to act.
- The optimizer remains a single-purpose, read-only service for climate. A consumer bug can at worst ignore a
  good plan (extra rule-based kWh) or, once live is allowed, follow a bad one within the fixed limits and above
  the comfort floors.
- The plan is advisory by construction (`advisory: true` is a constant of v1) so a future version cannot become
  a command without a new contract version and an amendment of this ADR.
- The effective-mode constant (`plan_export.LIVE_ALLOWED`) is a second lock on this side. Accepting the ADR
  includes a reviewed change to it; until then the select can request `live` but the optimizer reports it locked.
- The plan exports predictions of the model as it runs. They carry no energy figure; the comfort levels and
  predicted room temperatures are household data on an in-cluster endpoint, never public.

## Alternatives considered

- **The optimizer writes the heat pump itself.** Rejected: a second writer, a second token with write scope, and
  no machine limits, floors or rollback on this side (ADR-0006, the climate plan's single-writer rule).
- **The optimizer writes a helper (an `input_text` plan) that AppDaemon reads.** Rejected: it widens the
  `ha_guard` allowlist and adds a write path to this repository for no gain over a `GET`.
- **A hard lock only in an HA helper.** Rejected: a helper can be set from a dashboard by accident; the lock lives
  in git and needs a reviewed commit.
- **Serve plans of a failed model with a `confidence` field and let the consumer judge.** Rejected for v1: the
  gate is the judge, before anything is live.
