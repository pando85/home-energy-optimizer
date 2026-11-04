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
| `BATTERY_OPTIMIZER_PORT` | HTTP port of `serve` | `8080` |
| `BATTERY_OPTIMIZER_PROMETHEUS_URL` | Prometheus base URL | built-in default |
| `BATTERY_OPTIMIZER_GIT_SHA` | Build-time commit identity (40-hex, optional `-dirty`) used when git is unavailable | unset (set by the image build) |

`.envrc.example` shows a [direnv](https://direnv.net/) setup without any secret in it.

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

The `full_charge:` section (periodic full charge for cell balancing, **off by default**) is documented in
[service.md](service.md#periodic-full-charge-the-weekly-charge-enabler) and
[the plan](planning/periodic-full-charge.md). Only with `full_charge.enabled: true` in `mode: live` does the guard
admit the weekly-full-charge switch and its day select (seven weekday values); the section is validated in every
mode and refuses a setup that would leave the feature half configured.

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
