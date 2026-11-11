# Configuration

Environment variables, data directory, Home Assistant authentication and the secrets policy. The per-command
flags are in the [CLI reference](cli.md); the service-specific settings (`config/service.yaml`) are in the
[shadow service](service.md#configuration) page.

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `BATTERY_OPTIMIZER_DATA_DIR` | Private dataset store; must be **outside** any git worktree | `~/data/energy-baseline` |
| `BATTERY_OPTIMIZER_CONFIG` | Tariff configuration file | `config/tariff.yaml` relative to the working directory |
| `BATTERY_OPTIMIZER_HA_URL` | Home Assistant base URL | built-in default |
| `BATTERY_OPTIMIZER_HA_TOKEN` | Home Assistant bearer token of a non-admin user; required for HA extraction, `ha-states` and `serve`. Only `serve` in `mode: live` ever writes, and only the allowlisted Omnibattery entities ([ADR-0007](adr/0007-live-battery-control.md)) | unset |
| `BATTERY_OPTIMIZER_HA_TOKEN_FILE` | File holding the token (e.g. a mounted secret); used by `serve` when the variable above is unset | unset |
| `BATTERY_OPTIMIZER_SERVICE_CONFIG` | Shadow service configuration (`serve`, `shadow-report`); `build`, `extract` and the `serve` load history also read its battery counters and `battery.commissioned_at` (a missing file named here is an error) | `config/service.yaml` relative to the working directory |
| `BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES` | Sparse site overrides layered over the service config (see [Service config overrides](#service-config-overrides)); read by `serve`, `shadow-report`, `climate-shadow-report`, `service-config` and, for the battery counters and `battery.commissioned_at`, `build` and `extract` (a missing file named here is an error) | unset |
| `BATTERY_OPTIMIZER_PORT` | HTTP port of `serve` | `8080` |
| `BATTERY_OPTIMIZER_PROMETHEUS_URL` | Prometheus base URL | built-in default |
| `BATTERY_OPTIMIZER_GIT_SHA` | Build-time commit identity (40-hex, optional `-dirty`) used when git is unavailable | unset (set by the image build) |

`.envrc.example` shows a [direnv](https://direnv.net/) setup without any secret in it.

## Service config overrides

The image ships the base configuration (`config/` of the repository: `service.yaml`, `tariff.yaml`,
`climate.yaml`, `climate-mpc.yaml`) at `/app/config/`, the working directory, so the defaults need no
variable. Site-specific values go in a **sparse overrides file** named by
`BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES` (or `--service-config-overrides` of `serve`, `shadow-report`,
`climate-shadow-report` and `service-config`). The base is resolved as before (`--service-config` >
`BATTERY_OPTIMIZER_SERVICE_CONFIG` > `config/service.yaml`), then the overrides are deep-merged over it:

- Mappings merge recursively; every other value (scalars, lists, `null`, and `false`, `0`, `""`)
  **replaces** the base value. A list is replaced as a whole, never merged.
- **Every key path must exist in the base file**, otherwise loading fails naming the path (a typo, or a key
  that a new version renamed or removed, must not silently drop a production value). A mapping can only be
  overridden key by key: a mapping against a non-mapping value (or the reverse, for example `null` over a
  mapping) is an error, as is an overrides file that is not a mapping or is empty.
- The merge happens before parsing, so validation applies to the effective values, and `config_hash` and the
  plan `inputs_hash` are computed from them: the same effective config hashes identically whether a value came
  from the base or the overrides. Without an overrides file nothing changes.
- Relative `climate_shadow.climate_config` / `mpc_config` stay relative to the directory of the **base** file.
- `serve` logs one INFO line per override (`config override <path>: <base> -> <value>`), `/status` carries
  `config_overrides: {path: value}` and the metric `battery_optimizer_config_override_info{path}` is 1 for each
  (paths containing `token`, `secret`, `password` or `key` show `<redacted>`). `build` and `extract` read the
  same overrides for `battery.commissioned_at` and the battery counters.
- `battery-optimizer service-config [--service-config PATH] --overrides PATH` loads and validates both,
  prints the applied overrides and the effective `config_hash`, and exits 1 on any error. Run it with the new
  image before a rollout:
  `docker run --rm -v ./overrides.yaml:/o.yaml:ro ghcr.io/pando85/battery-optimizer:<tag> service-config --overrides /o.yaml`.

### Deploying a new version

The homelab keeps **only** a sparse overrides file with the site-specific values (`mode`, `climate_shadow.*`,
`planning.discharge_margin_bound`, `actuation.load_following`, `planning.thermal.mode`, `full_charge.enabled`,
the dwell and soft-cap costs, ...) and mounts it with `BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES`. A version
bump changes **only the image tag**; never vendor a copy of `service.yaml`. Check the overrides against the new
image with `service-config` first: it fails on a key the new version removed or renamed.

## Development and production locations

### Data directory

All output goes to a private data directory **outside the git worktree**.
Default: `~/data/energy-baseline`. Override with `--data-dir` or `BATTERY_OPTIMIZER_DATA_DIR`. For new work set it to the pulled copy of the deployed store (see below).

The default directory keeps its pre-rename `energy-baseline` name on purpose (it is an existing
immutable store and stored benchmark JSONs embed its path). The legacy `ENERGY_BASELINE_*`
environment variables are still honoured as a deprecated fallback (`DeprecationWarning`); the
`BATTERY_OPTIMIZER_*` name wins when both are set.

### Local copy of the deployed store

Since 2026-10-25 the store of record is the homelab PVC (CronJob extraction plus the shadow service).
`~/data/energy-baseline` is **frozen**: it is the store behind the P0–P4 and PO-A/PO-C evaluation
runs, and adding manifests to it would change its dataset ids. For new offline analysis pull the
deployed store into a separate directory:

```bash
KUBE_CONTEXT=<context> just pull-store            # into ~/data/battery-optimizer-cluster
export BATTERY_OPTIMIZER_DATA_DIR=~/data/battery-optimizer-cluster
```

With [direnv](https://direnv.net/), copy `.envrc.example` to `.envrc`: it already points
`BATTERY_OPTIMIZER_DATA_DIR` at the pulled copy and has a `KUBE_CONTEXT` line to fill in, so
`just pull-store` and every CLI command use the same directory. Switch to the commented
`~/data/energy-baseline` line only to reproduce a recorded evaluation run.

The recipe streams `raw/`, `manifests/`, `analysis/` and `service/` out of the service pod with `tar`
(read-only on the cluster), runs `validate` on the copy and only then syncs it into the target with
mode 0700. Re-pulling is safe: parts and manifests are immutable, the service's append-only logs are
refreshed, and files that exist only locally (your own analysis runs) are kept. The cluster store has
no OMIE prices; run `extract --source omie` against the copy before anything that needs them.

**Climate site file.** The climate loader reads the site coordinates from
`<data dir>/climate/site.yaml` (`latitude`, `longitude`, optional `phi`, the always-open valve share).
It is private, stays in the data dir and is never committed; `pull-store` does not touch it.

The file may also hold `reference_station`, the quoted 5-digit WMO id of the AEMET station used as the
independent outdoor-temperature reference (`reference_station: "<5-digit WMO id>"`). The choice
reveals the site's area, so it lives only in this private file, never in the repository. The station's
hourly SYNOP reports are fetched with `hack/climate_station.py fetch` into
`<data dir>/climate/station/<WMO>/raw/` (one immutable file and manifest per month). Without the key
the climate loader behaves as before and `signals.t_out_source: fused` is refused. See
[Fused outdoor temperature](architecture.md#fused-outdoor-temperature-stationpy-toutpy).

*Attribution:* the station data is "Fuente: AEMET (via Ogimet)": AEMET's synoptic observations,
served by Ogimet (<https://www.ogimet.com/cgi-bin/getsynop>). Ogimet's terms are not known, so fetches
are monthly, at least 5 s apart and never repeated for a stored month.

**Code identity without git (container images).** Lineage records the commit via
`git rev-parse HEAD`. Where git is unavailable, set `BATTERY_OPTIMIZER_GIT_SHA` at build time to a
40-character lowercase commit SHA (optionally suffixed `-dirty`); it is used only when git cannot
answer, a malformed value raises `ValueError`, and with neither the identity is the
`nogit@battery-optimizer==<version>` sentinel.

**Development (this repo, local runs):** data lives under `/tmp/opencode/energy-baseline`
(chmod 0700, ephemeral, wiped on reboot). Used for first-extraction milestones and
offline tests. All files written with mode 0600, dirs with 0700.

**Production (planned):** data moves to a cluster PVC (e.g. `openebs-zfspv` on prusik)
with the same directory layout and permission model. Migration is a `rsync` + a
`--data-dir` flag flip; the package code is location-agnostic.

### Passive archives of the resident service

With the `archive:` section of `config/service.yaml` enabled, `serve` appends two private, append-only
archives under `<data dir>/service/v1/`: `pvpc_snapshots/date=YYYY-MM-DD/pvpc_snapshots.jsonl` (every
state change of the PVPC price sensor, raw attributes included) and
`weather_forecasts/date=YYYY-MM-DD/weather_forecasts.jsonl` (the hourly Home Assistant weather forecast,
fetched with `weather.get_forecasts`). Files are `0600`, directories `0700`, partitioned by UTC day.
There is **no retention yet**: nothing prunes them. Keys, record schemas, metrics and failure behaviour:
[shadow service](service.md#passive-archives). The weather fetch is the only Home Assistant service call the
service can make; the token otherwise stays read-only.

### Climate shadow of the resident service

With `climate_shadow.enabled` in `config/service.yaml`, `serve` runs the track-B2 climate shadow hourly and
appends its records under `<data dir>/service/v1/climate_{decisions,scores,days,models}/`. It needs, in the
data directory, the prior written by `hack/climate_shadow_prior.py`
(`analysis/v1/climate-shadow/priors/<id>/prior.json`, named by `climate_shadow.prior`) and the private
`climate/site.yaml`, and, next to the service config, `climate.yaml` and `climate-mpc.yaml` (paths relative to
the service config's directory). No new variable or credential: it uses `BATTERY_OPTIMIZER_PROMETHEUS_URL`
and the read-only token, and reads three optional HA helpers of the homelab comfort controller. Set
`climate_shadow.go_live` on its go-live day. Keys and ops: [shadow service](service.md#climate-shadow-track-b2).

### No recurring scheduler yet

There is no recurring scheduler, cron job, or Kubernetes CronJob wired up for this toolkit yet.
Every command in the [CLI reference](cli.md) is run manually (or by a human-driven runbook). A scheduled pipeline
(extract → validate → build → benchmark → report) is deferred to a later phase: see
[ADR-0003](adr/0003-phase1-measurement-and-baseline.md) for the scope boundary.

### Service mode (shadow, dry_run, live)

`mode:` in `config/service.yaml` selects what `serve` may do: `shadow` (the default and the committed value)
reads only; `dry_run` also computes and records the Omnibattery writes it would make and sends nothing; `live`
sends them through the `ha_guard` allowlist, gated by the owner's kill switch. The `actuation:` section (entity
IDs, import cap, margins, command policy, freshness) is documented in
[service.md](service.md#live-control-dry_run-and-live); `dry_run` and `live` refuse to start without the
measured `actuation.battery_ac_power_sign`, and `live` also without `planning.soc_source: entity`. Changing the
mode is a homelab config change of the deployed `service.yaml`, never an environment variable.

`actuation.load_following` (`enabled`, `max_w`, `soc_hysteresis_pct`, `floor`, `storage_value_margin_eur_per_kwh`; **off by default**) lets a planned discharge
follow the real house load: in a discharge slot, while the measured SOC is at or above the plan's end-of-slot SOC,
the system discharge cap is raised from the planned power to `max_w` (null = `max_discharge_cap_w`, and never
outside `[min_discharge_cap_w, max_discharge_cap_w]`, the guard's existing bound). It adds no entity, service call or
credential and is excluded from the plan's config hash; see
[service.md](service.md#load-following-discharge). `floor` (`plan_soc`, the default and the rule above, or
`storage_value`) and `storage_value_margin_eur_per_kwh` (default **0.005**, `0` to `0.1`) choose what holds the raised
cap back: with `storage_value` the cap is raised while the slot's price less `planning.throughput_cost_eur_per_kwh` is
at least the plan's marginal value of stored energy plus the margin
([service.md](service.md#storage-value-floor), [ADR-0007 amendment 2026-11-11](adr/0007-live-battery-control.md#amendment-2026-11-11-value-based-load-following)).
Enable it with a one-key overrides entry (`enabled` and `max_w` are the existing ones):

```yaml
actuation:
  load_following:
    floor: storage_value
```

`actuation.soc_floor_recovery_w` (default **200 W**) and `actuation.soc_floor_hysteresis_pct` (default **1.0** SOC
point, `0 < value <= 10`) are the reserve protection of
[ADR-0007](adr/0007-live-battery-control.md#amendment-2026-11-08-reserve-protection): while the **measured** SOC sits
at or below the tariff's floor, a tick the plan maps to idle or discharge asks for that bounded recovery charge
instead, through the existing charge row (`min(import_cap_w, entity max)` ceiling) and the existing allowlist
entries. The floor itself is not configured here: it is `battery.soc_window_min` of
[`config/tariff.yaml`](../config/tariff.yaml), the value the planner clamps to, so it is never set twice.
`soc_floor_recovery_w` must not exceed `import_cap_w`. There is no enable flag: the layer is part of the actuator, so
it is inert in `shadow`, recorded and not sent in `dry_run`, and sent in `live` under the kill switch. See
[service.md](service.md#reserve-protection).

The `full_charge:` section (periodic full charge for cell balancing, **off by default**) is documented in
[service.md](service.md#periodic-full-charge-the-weekly-charge-enabler) (keys, the
[scheduler](service.md#periodic-full-charge-the-scheduler) and
[how to enable it](service.md#enabling-the-periodic-full-charge)) and
[the plan](planning/periodic-full-charge.md). Only with `full_charge.enabled: true` and `full_charge.mode: live` in a service `mode: live` does the guard
admit the weekly-full-charge switch and its day select (seven weekday values); the section is validated in every
mode and refuses a setup that would leave the feature half configured. The cadence is the `cadence` table (days
by the class of the latest balance readings; `unknown` 10/14, `green` 14/28, `yellow` 7/14, `alert` 5/7), with
`min_comparable_readings`, `alert_consecutive` and `settle_timeout_s`; the balance sensors are in
`full_charge.entities` and may repeat `battery_health.entities` (the two must agree). `full_charge.mode` is the feature's
own switch, `shadow` (default) or `live`, and `live` needs the service `mode: live`: with the service live and the
feature `shadow` the feature only simulates (decisions and a simulated state are recorded; the regular plan stays the
plan of record, the actuator gets no directive and the guard does not admit the two entities), so the installation
stays `live` for normal battery control while the decisions are studied.

The `battery_health:` section (read-only battery health telemetry: temperature exposure, charge sessions, cell
voltages, Omnibattery's balance monitor) is documented in
[service.md](service.md#battery-health-telemetry). All its entities are optional and null in code; it adds no
write path, no environment variable and no credential, and is excluded from the plan's config hash.

The `planning.thermal:` section (read-only thermal state and derate prediction, **off by default**: `mode: "off"`,
quoted) is documented in [service.md](service.md#thermal-shadow-derate-prediction). With `mode: shadow` it reads
Omnibattery's Temperature Charge Limit switch and numbers (never written, never in the `ha_guard` allowlist), the
battery temperature (shared with `battery_health.entities.temperature`, they must agree) and the AC power of
`actuation.entities.battery_ac_power` with its sign; it keeps a thermal model with a daily guarded refit under
`<data_dir>/service/v1/state/` and records a prediction beside every plan. It adds no environment variable and no
credential, changes no plan, and is excluded from the plan's config hash.

## HA authentication

HA history extraction requires a bearer token. The token is **never written to
disk** by the package — it is passed via the `BATTERY_OPTIMIZER_HA_TOKEN`
environment variable or the `token=` keyword argument to `extract_ha_history()`.
The HA base URL is also **never recorded**: the manifest keeps only
`endpoint: "home-assistant-history-api"` plus `endpoint_instance_sha256_16`,
a one-way digest that allows cross-run identity correlation (telling two
instances apart) without recording the endpoint itself.

httpx INFO-level request logging (which would leak the full URL including
entity filters) is suppressed at the start of `extract_ha_history()` via
`_suppress_httpx_info_logging()`, and `_sanitize_error()` strips the base URL
and host from transport-error text before it reaches stderr or logs.

For a first-run login flow (username + password from `pass`), see
[`docs/troubleshooting/home-assistant-api-hacs-deploy.md` in pando85/homelab](https://github.com/pando85/homelab/blob/master/docs/troubleshooting/home-assistant-api-hacs-deploy.md).
The login flow is ephemeral: the token lives in memory for the duration of the
extract call and is never persisted.

## Secrets policy

- No secrets are written to files by this package
- The HA token belongs to a non-admin user. HA cannot restrict a token to entities or services, so outside
  `mode: live` the code (`service/ha_guard.py`) is what keeps the `battery-optimizer` user's token read-only.
  `mode: live` uses a separate token of the dedicated non-admin user `battery-optimizer-live` (group
  `system-users`, local only; its own Vault KV path when deployed), and the same guard admits only the
  allowlisted Omnibattery writes and the heartbeat. No token value is ever committed
- `BATTERY_OPTIMIZER_HA_TOKEN` is read from env or passed explicitly; never logged
- HA extraction writes no endpoint identity: the manifest records
  `endpoint: "home-assistant-history-api"` plus a 16-char SHA-256 digest of the
  base URL (`endpoint_instance_sha256_16`) for cross-run correlation only.
  Prometheus extraction still records `params.base_url` (an unauthenticated
  internal archive URL) — the asymmetry is a known gap
- `_redact_error()` strips bearer tokens, credential-bearing parameters
  (`password`, `passwd`, `pwd`, `token`, `access_token`, `refresh_token`,
  `id_token`, `secret`, `api_key`, `apikey`, `authorization`, `client_secret`
  in both `key=value` and `key: value` form), and common API-key prefixes
  (`sk-`, `pat-`, `ghp_`, `gho_`, `xoxb/xoxa/xoxp/xoxr/xoxs-`) from any
  error message before it reaches stderr or logs
- HA extraction additionally applies `_sanitize_error()` to strip the base URL
  and its host from transport-error text, and suppresses httpx INFO-level
  request logging (which would otherwise leak the full URL with entity filters)
- The CLI catches `ValueError`, `RuntimeError` (and their domain subclasses:
  `BenchmarkInputError`, `ReportInputError`, `SQLInputError`, `BenchmarkError`,
  `ReportError`), `NotImplementedError` (for deferred commands like `mirror`),
  and `OSError` (including `FileExistsError` from the immutable store's
  no-clobber writes, `PermissionError`, `IsADirectoryError`, `FileNotFoundError`)
  and prints one redacted line to stderr with exit code 1; no raw tracebacks
  leak. A no-clobber refusal additionally explains that nothing was overwritten
  or deleted and what to do instead — the tool never cleans up canonical data
  on its own
- Curated `inspect-sql` queries that need a benchmark-only view are checked up
  front, so a missing `--benchmark` is reported as a usage error rather than
  surfacing a DuckDB `CatalogException`
- Unexpected exceptions (e.g. `KeyError`, `TypeError`, or any other exception
  type) are allowed to propagate so bugs are visible; there is no blanket
  `except Exception`
