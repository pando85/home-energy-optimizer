# Tariff and prices (PVPC 2.0TD, price outlook)

## Purpose and owner

The price of each slot and the tariff terms around it. Owner: this repository (`config/tariff.yaml`,
`config.py`, `cost.py`, `service/pvpc.py`, `price_outlook.py`, `omie_outlook.py`). The price source is the
Home Assistant PVPC sensor, fed by the homelab deployment. Roadmap: [Price Outlook](../planning/components/price-outlook.md).

## Interfaces

- **In:** the archived realised PVPC per hour (EUR/kWh, tolls and charges included, **taxes excluded**),
  published next-day prices from about 20:15 local, optionally OMIE day-ahead for an estimate at 14:00.
- **Out:** `price_eur_per_kwh` per slot with `price_quality`; tariff period labels; the pre-tax cost basis.
- **Consumers:** [battery-optimizer](battery-optimizer.md), [boost-mode](boost-mode.md) (its threshold must
  use the same pre-tax basis), and every `dataset_id` (the tariff file is hashed into it).

## Calibrated on

- Realised PVPC from 2024-01-01 to 2026-10-01: about 98.8% of intervals measured, the rest ambiguous,
  forward-filled or missing and masked as unusable (dataset `cdd448abb006`).
- Period rules (weekday: valle 00 to 08, then 08 to 10, 10 to 14, 14 to 18, 18 to 22, 22 to 24; Saturday
  and Sunday all valle) are **labels only**: no result takes its price from the YAML.
- Price outlooks evaluated on the legacy history: `exact`, `published_only`, `persistence`, and the OMIE
  `omie_period` estimate, which passed its gate GC ([plan](../planning/price-outlook-improvement-plan.md)).

## Assumptions

- **Taxes unconfirmed** (`iee_pct`, `iva_pct` are null): every reported figure is pre-tax and
  `effective_prices` refuses the taxed basis. A uniform tax factor would not change the argmin.
- **Holidays missing** (`holidays: []`): national and regional holidays are treated as weekdays, which
  overstates the peak share.
- Published equals realised price (owner-asserted, not verified); no export credit, no standing or power
  charges are modelled.
- The price can be negative (26 h at or below 0, minimum -0.0126 EUR/kWh pre-tax, 2024-01 to 2026-10) and
  every consumer must keep it as is; the optimiser does, see
  [battery-optimizer](battery-optimizer.md) for the code references.
- The service never forecasts unpublished prices; its horizon ends at the last published slot.
- The periodic full-charge decision depends on the next-day publication time (about 20:15 local, measured by the
  `pvpc_snapshots` archive): it is taken in the first solve after tomorrow's prices are published, about 3.5 h before
  the day starts, and never on an estimated price.
- **Hard constraint (owner-confirmed 2026-10-30):** the 5.0 kW contracted import limit is enforced by the
  smart meter, which cuts supply on overload, and this has happened. It is not a cost term and must not be
  traded against price. The trip is on **instantaneous** load, not the 15-minute average
  (owner-confirmed 2026-10-31; no distributor document yet), so the model's averaged constraint is
  insufficient and the real-time cap in the actuator is required.

## Invalidated when

- The supply contract or tariff changes (not 2.0TD, new period hours, different pass-through), or the
  contracted power changes (the meter cut-off moves with it).
- Taxes are confirmed (reported figures change basis) or a non-uniform term (standing or power charge) is
  added to the objective.
- Holidays are populated (period labels and `dataset_id` change); any other edit of `config/tariff.yaml`.
- The PVPC sensor source or definition changes; the REE publication time moves; OMIE access changes.

## Recalibrate by

1. Read taxes from a verified invoice, fill `taxes` (with `valid_from` and source) and `holidays`
   (national plus the regional subdivision, as the `holidays` package lists them) in `config/tariff.yaml`.
2. Rebuild: `uv run battery-optimizer build ...`, record the supersession of the previous `dataset_id`
   ([progress](../planning/energy-baseline-progress.md)), then re-run `benchmark` ([benchmark](../benchmark.md)).
3. Acceptance: taxed cost no longer raises `TaxesUnconfirmedError`; the policies' argmin is unchanged for a
   uniform factor; on a sampled holiday the slots take the Sunday period. For the outlook, re-run
   `price-eval` before trusting `omie_period` after any OMIE or publication change.


## KPIs

Observability only ([service](../service.md#day-ahead-and-final-plan)); metrics are prefixed
`battery_optimizer_`.

| KPI | Where |
|---|---|
| Next-day publication time | the `prices_published` event (`service/v1/events/`) is written when the next-day prices first appear; `prices_tomorrow_published` flips to 1 at that time (first sample at 1 per local day) |
| Horizon length | `price_horizon_seconds` (now to the end of the last published price: short just before the next-day publication, about a day and a half just after) and `plan_horizon_slots` |
| Day-ahead lead time | `slot_plan_lead_seconds{plan="day_ahead"}`; offline `day_ahead_lead_hours` (min, mean, max) from `uv run python hack/day_ahead_kpis.py --from <day> --to <day>` |

## Status

**provisional** (2026-10-30): taxes unconfirmed and holidays missing; both change reported figures, not
the dispatch ranking.
