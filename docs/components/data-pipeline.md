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
- Plant regime: no battery, legacy heat-pump control, S1 to S3 (see [regime log](regime-log.md)). The battery
  was commissioned 2026-11-03 19:14:32 UTC; no dataset reaching that instant has been built yet.

## Assumptions

- `gross_kwh` is the whole-house feed, Shelly main channel 1, user confirmed 2026-10-23; the heat pump
  and the servers are inside it, not added to it ([ADR-0003](../adr/0003-phase1-measurement-and-baseline.md)).
- Before `battery.commissioned_at`: no battery (`battery_absent=True`, a recorded assumption); a window
  ending before it is byte-identical to a build before the battery code (same id, manifest, part).
- From it: `gross = raw - charge + discharge` per interval, as `service/history.py` (which also keeps the grid
  counter alone for slots ending at or before it). The counters `sensor.venus_total_charging_energy` /
  `_discharging_energy` are AC side (commissioning test 2026-11-03: charge 0.180 kWh against 0.182 kWh of
  integrated AC power and 0.165 kWh DC), 0.01 kWh resolution; the discharge counter excludes the backup port. An unknown counter or
  a negative result nulls `gross_kwh` (`battery_counters_missing`, `battery_gross_negative`); counters
  not configured or absent from the archive refuse the build. Any counter decrease nulls its interval
  (stricter than the `counter_rates` rollback rule).
- The `servers` feature is the Shelly PM mini before `features.SERVERS_ON_BACKUP_PORT_SINCE` (2026-11-03
  22:05 UTC) and the battery's backup-port output `sensor.venus_ac_offgrid_power` from it on; each entity is
  read only inside its window ([regime log](regime-log.md)). In Bypass the servers stay inside channel 1.
  **Unverified:** whether a discharge feeds the backup port from the battery; the discharge counter excludes
  the backup port, so that energy would be missing from `gross_kwh` and show as `servers + heat pump > main`.
- `washer_plug` ends at `features.WASHER_PLUG_REFLASHED_AT` (2026-11-03 22:46 UTC, plug reflashed for
  another load) and is unknown from then on; it has had no data since 2024-07-08 anyway.
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
   minus gross equals the metered charge minus discharge on a sampled week. `build-features --dataset`
   (with `sensor.venus_ac_offgrid_power` extracted) shows no new `servers + heat pump > main` violations
   in discharging slots after the servers' move.

## Status

**stale** (2026-11-03): the battery is commissioned and recalibration step 1 is done (counters and
`battery.commissioned_at` set); steps 2 and 3 need a week of post-commissioning data. Until the acceptance
check passes, the reconstruction is untested on real counters. Reconstruction and refusal are in the code
(climate plan item 6). 2026-11-03: the build's reconstruction had the
sign reversed (`raw + charge - discharge`, the service always used `raw - charge + discharge`); corrected
before go-live, so no dataset was built with it. 2026-11-03: the servers moved to the battery backup
port; the `servers` feature switches source at that instant, and `washer_plug` ends at the terrace
plug's reflash the same night (feature-set ids change).
