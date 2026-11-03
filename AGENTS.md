# battery-optimizer

Python toolkit for household battery optimisation: Prometheus / Home Assistant extraction, dataset
build and battery MPC benchmarks. This file is the entry point for coding agents. Detailed workflows
live in `.opencode/skills/` (also exposed to Claude Code through the `.claude/skills` symlink).

The GitHub repository is `pando85/home-energy-optimizer` (created as `pando85/battery-optimizer`).
The distribution, `battery_optimizer` package, `battery-optimizer` CLI, `BATTERY_OPTIMIZER_*`
variables, metrics and the `ghcr.io/pando85/battery-optimizer` image keep the old name until the
staged rename in `docs/planning/climate-optimizer-implementation.md`.

## Essential commands

Recipes live in the `justfile` ([just](https://just.systems/)). Local uv may be stale: run
`UV="uvx uv@<version>" just ...` with the version pinned in `.github/actions/setup-uv/action.yml`.

```bash
just                   # list recipes
just git-hooks         # install pre-commit + commit-msg hooks (once)
just lint              # every pre-commit hook on all files (ruff, yamllint, shellcheck, uv.lock)
just fmt               # ruff check --fix + ruff format
just test              # pytest (~1900 tests, several minutes)
just image             # build the container image locally
just release           # cut a release (see the release skill)
```

## Repository map

- `src/battery_optimizer/`: the package and the `battery-optimizer` CLI (`cli.py`).
- `tests/`: pytest suite; `hack/`: one-off analysis scripts; `config/tariff.yaml`: tariff config.
- `docs/adr/`: architecture decision records; `docs/planning/`: plans and progress notes;
  `docs/components/`: per-component calibration records and the regime log.
- `.ci/`, `Dockerfile`, `.github/`: release script, image, workflows.

## Rules

- Python 3.14, ruff (line length 100; rules E, F, I, N, W, UP). Run `just lint` and `just test`
  before reporting completion and include the actual results.
- Dependencies are unconstrained in `pyproject.toml`; `uv.lock` is the source of truth and Renovate
  refreshes it weekly. Commit `uv.lock` with any dependency change.
- One place per version pin: the project version is `[project] version` in `pyproject.toml` (changed
  only by `just release`), the uv version is pinned in `.github/actions/setup-uv/action.yml` and the
  `Dockerfile`. Pin GitHub actions to a full commit SHA with a `# vX.Y.Z` comment.
- Commits: Conventional Commits (`type(scope): Subject`, types in `.commitlintrc.json`), GPG-signed
  and `Signed-off-by` (`git commit -s`). Never pass `--no-gpg-sign` or `--no-verify`.
- Never edit `CHANGELOG.md` by hand and never create tags: `just release` and CI do it.
  Read `.opencode/skills/release/SKILL.md` before any release work.
- Behavioral changes need tests. Keep the smallest correct diff.
- Component records: a PR that changes a component's behaviour or calibration, or reflects a plant or
  control change, updates its page in `docs/components/` and the regime log, then either runs the page's
  recalibration or marks the dependent components `stale` (see `docs/components/README.md`).

## Privacy and safety

- Never commit energy data, datasets, parquet files, tokens, Home Assistant or Prometheus URLs of a
  real installation. Use environment variables; `.envrc` is gitignored (`.envrc.example` is the template).
- The data directory (`BATTERY_OPTIMIZER_DATA_DIR`, default `~/data/energy-baseline`) must stay outside
  the repository; the code refuses a path inside a git tree. The default keeps the pre-rename name on
  purpose.
- Shadow by default: `serve` reads and benchmarks only unless the deployed `service.yaml` says `mode: live`
  (`mode: dry_run` computes and records writes, sends nothing). The only write path is the live actuator of
  [ADR-0007](docs/adr/0007-live-battery-control.md): in `mode: live` only, and only through
  `service/ha_guard.py`'s `ActuationAllowlist` — `switch.turn_on/off` on the two Omnibattery direction switches
  and the system power-limit switch, `number.set_value` on the PD grid target and the system discharge cap
  (bounded values), `input_datetime.set_datetime` on the heartbeat helper, one entity per call, gated by the
  owner's kill switch (never written). Do not widen it, do not add any other service call, Modbus, REST write
  or device, and never make `live` a default. The other exception is the non-mutating
  `weather.get_forecasts` query (with `return_response`) of the passive forecast archive, allowed only by the
  narrow check in `service/ha_guard.py`; do not widen it. Home Assistant tokens belong to non-admin users:
  the read-only `battery-optimizer` user's outside `mode: live`, the separate `battery-optimizer-live`
  user's only in `mode: live`. Never commit a token value.
- The container image holds no data and no git; `BATTERY_OPTIMIZER_GIT_SHA` is the build-time commit
  identity.

## Further reading

- Decisions: `docs/adr/` (start with ADR-0003 for the measurement baseline and ADR-0004 for the
  production architecture).
- Plans and progress: `docs/planning/`, provenance of the extraction in `docs/provenance.md`.
- CLI behavior: `docs/cli.md` (`docs/benchmark.md` for `benchmark`); environment variables and secrets
  policy: `docs/configuration.md`; module notes: `docs/architecture.md`; known issues:
  `docs/known-issues.md`; the image: `docs/container.md`. `README.md` is the short overview.
