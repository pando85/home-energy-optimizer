# Container image

Multi-arch (`linux/amd64`, `linux/arm64`) images are published to
`ghcr.io/pando85/battery-optimizer` with the tags `latest` (main), `sha-<short-sha>` (main),
`X.Y.Z` and `X.Y` (releases). The image runs as uid/gid `65532`, works with a read-only root
filesystem, and contains no data and no git.

The image holds the repository's `config/` directory (`service.yaml`, `tariff.yaml`, `climate.yaml`,
`climate-mpc.yaml`: entity ids and settings, no secrets, no data) in `/app/config/`, the working directory, so
the default lookups work without any variable. Site-specific values are a sparse overrides file mounted
anywhere and named by `BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES` (see
[Service config overrides](configuration.md#service-config-overrides)); a deployment mounts nothing else and a
version bump changes only the image tag. Check an overrides file against an image before rolling out:

```bash
docker run --rm -v "$PWD/overrides.yaml:/o.yaml:ro" \
  ghcr.io/pando85/battery-optimizer:<tag> service-config --overrides /o.yaml
```

A one-shot run of the batch commands, with the data directory and tariff config mounted and the token passed
by name, is in the [README quick start](../README.md#quick-start).

## Shadow service

The shadow service runs from the same image (`serve`, port 8080, read-only root filesystem, writes
only under the data directory); see [service.md](service.md):

```bash
docker run --rm --read-only --cap-drop ALL -p 8080:8080 \
  -v "$HOME/data/energy-baseline:/data" -v "$PWD/overrides.yaml:/overrides.yaml:ro" \
  -e BATTERY_OPTIMIZER_DATA_DIR=/data \
  -e BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES=/overrides.yaml \
  -e BATTERY_OPTIMIZER_HA_URL -e BATTERY_OPTIMIZER_HA_TOKEN -e BATTERY_OPTIMIZER_PROMETHEUS_URL \
  ghcr.io/pando85/battery-optimizer:latest serve
```

The [climate shadow](service.md#climate-shadow-track-b2) (off by default) needs no other variable:
`climate.yaml` and `climate-mpc.yaml` are in the image next to `service.yaml` (their paths are relative to the
base file's directory); put the prior
directory (`analysis/v1/climate-shadow/priors/<id>/`) and `climate/site.yaml` into the data directory.

## Data directory, provenance and code identity

The data directory must be writable by uid `65532`. Verify an image's provenance with
`gh attestation verify oci://ghcr.io/pando85/battery-optimizer:<tag> --owner pando85`.
The image records the commit it was built from in `BATTERY_OPTIMIZER_GIT_SHA` (see [Configuration](configuration.md#data-directory)).
