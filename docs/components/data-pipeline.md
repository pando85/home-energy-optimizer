# Data extraction and dataset build

## Purpose and owner

Turns the Prometheus archive and the Home Assistant history into an immutable, content-addressed store:
15-minute UTC intervals of whole-house load and PVPC price (`build`), and a climate feature set of heat
pump, thermostat and sensor signals (`build-features`). Owner: this repository; the extraction CronJob
and the data volume are deployed from the homelab repository.

## Interfaces

- **In:** Prometheus `hass_*` history (1-minute, from 2023-11-08), HA history for enum states
  (not re-extractable), `config/tariff.yaml` (hashed into every `dataset_id`). Optional battery AC
  counters and `battery.commissioned_at`: `config/service.yaml`, never `tariff.yaml` (re-mints every id).
- **Out:** `<data_dir>/analysis/v1/<dataset_id>/` (`gross_kwh`, `raw_grid_kwh`, price, quality flags,
  `battery_absent`) and `<data_dir>/analysis/v1/features/<feature_set_id>/`.
- **Consumers:** every offline result ([benchmark](system-replay-benchmark.md), forecasters, climate models) and the shadow service history.

## Calibrated on

- Frozen baseline dataset `2e01620ba727` (2024-01-01 to 2025-12-31, `config_hash` `7a901801...`, RTE 0.80), run `fe3cbfec46b6`.
- Current cluster-store dataset `cdd448abb006` (2024-01-01 to 2026-10-01, built at code `0e5887f`,
  same `config_hash`); climate feature set `da6d678016ed` (2023-11-07 to 2026-05-01, code `ac90586`).
- Plant regime: no battery, legacy heat-pump control, S1 to S3 (see [regime log](regime-log.md)).

## Assumptions

- `gross_kwh` is the whole-house feed, Shelly main channel 1, user confirmed 2026-10-23; the heat pump
  and the servers are inside it, not added to it ([ADR-0003](../adr/0003-phase1-measurement-and-baseline.md)).
- Before `battery.commissioned_at`: no battery (`battery_absent=True`, a recorded assumption); a window
  ending before it is byte-identical to a build before the battery code (same id, manifest, part).
- From it: `gross = raw - charge + discharge` per interval, as `service/history.py`. An unknown counter or
  a negative result nulls `gross_kwh` (`battery_counters_missing`, `battery_gross_negative`); counters
  not configured or absent from the archive refuse the build. Any counter decrease nulls its interval
  (stricter than the `counter_rates` rollback rule).
- HA recorder history cannot be re-extracted; string states are not exported ([known issues](../known-issues.md)).
- Heat-pump electricity: the branch meter (channel 2) reads about 1.28 times the HeishaMon counters and also feeds appliances ([climate plan](../planning/climate-optimizer-implementation.md)).

## Invalidated when

- **A battery runs while `battery.commissioned_at` and the counter ids are unset** (channel 1 is net grid
  stored as gross; `build` refuses a battery-looking entity in the archive then), or the counters are
  not AC-side kWh or not metric `hass_sensor_energy_kwh`.
- The meter, its CT orientation or the channel mapping changes; an HA entity is renamed.
- `config/tariff.yaml` changes in any field: every `dataset_id` is re-minted, the old one superseded.
- A new heat-pump signal enters the feature set (new id), or the exporter changes what it retains.

## Recalibrate by

1. Install day, in one commit: set both `entities.battery_*_energy` ids and `battery.commissioned_at`
   (ISO with offset) in `config/service.yaml`; date the regime-log row.
2. `extract --entities <charge>,<discharge>` (alone), `validate`, `build` ([CLI](../cli.md#build)), then
   `build-features --signal-set climate`.
3. Acceptance: `build_manifest.json` has `battery_reconstruction` with few unusable intervals, and raw
   minus gross equals the metered charge minus discharge on a sampled week.

## Status

**valid** until battery go-live. Reconstruction and refusal are in the code (climate plan item 6); the
install-day config is unset, untested on real counters. 2026-11-03: the build's reconstruction had the
sign reversed (`raw + charge - discharge`, the service always used `raw - charge + discharge`); corrected
before go-live, so no dataset was built with it.
