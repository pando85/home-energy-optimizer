# Documentation

Guides, design records and planning for the battery optimizer. The project began in
[`pando85/homelab`](https://github.com/pando85/homelab); see [provenance](provenance.md) for what moved,
how identities map, and the caveats when comparing old artifacts. The project overview and quick start are in
the [top-level README](../README.md).

## Guides

| Document | Purpose |
|---|---|
| [Configuration](configuration.md) | Environment variables, data directory, development and production locations, Home Assistant authentication, secrets policy |
| [CLI reference](cli.md) | Every command, the `extract` exit codes and coverage gaps, `ha-states`, `inspect-sql` |
| [Benchmark reference](benchmark.md) | `benchmark` flags, the five-rung policy ladder, battery assumptions, methodology notes |
| [Architecture](architecture.md) | Module notes: dispatch intent contract, artifact integrity, economic sensitivity, replay runtime, shadow service |
| [Container image](container.md) | Published tags, running the batch commands and `serve` from the image, provenance verification |
| [Known issues](known-issues.md) | HA retention, per-chunk emptiness, row-level re-extraction dedup, `carry_in` flags |

## Component calibration records

What each component was calibrated on, what it assumes and what invalidates it, so a plant or control
change leads to a recalibration instead of a silent drift.

| Document | Purpose |
|---|---|
| [Components overview](components/README.md) | How to use the pages, page template, dependency map and status table of every component |
| [Regime log](components/regime-log.md) | Dated log of real plant and control changes (water target, setpoints, `min_hours`, AppDaemon fixes) and the planned ones (comfort schedule, battery go-live) |

## Architecture decision records

| ADR | Title | Status |
|---|---|---|
| [0001](adr/0001-home-battery-economic-dispatch.md) | Forecast-informed economic dispatch | Proposed |
| [0002](adr/0002-appliance-aware-load-forecasting.md) | Appliance-aware load forecasting | Proposed |
| [0003](adr/0003-phase1-measurement-and-baseline.md) | Phase 1 measurement and baseline | Accepted (2026-10-23) |
| [0004](adr/0004-battery-optimizer-production-architecture.md) | Production runtime and independent source repository | Accepted (2026-10-25; software architecture, shadow only) |
| [0005](adr/0005-shared-energy-planning-climate.md) | Shared runtime and climate optimization | Proposed (2026-10-30; architecture only) |

## Delivery snapshot (2026-10-25)

- **Application and releases:** v0.2.1 published with wheel, sdist and multi-arch OCI image; associated CI, image and scan jobs succeeded. See [releases](https://github.com/pando85/home-energy-optimizer/releases).
- **Deployed via homelab GitOps:** a daily extract/validate/periodic-build CronJob and resident read-only shadow service, using a ZFS PVC, ServiceMonitor, Grafana dashboard and alert rules. See the [operator runbook](https://github.com/pando85/homelab/blob/master/docs/deployment/battery-optimizer.md).
- **Not accepted yet:** repeated unattended pipeline success, alert and backup-restore drills, two-week live native-battery shadow comparison. The service uses **simulated SOC** before Marstek/Omnibattery commissioning. **No device writer or live dispatch exists.**
- **Release policy follow-up:** image publication runs independently of the CI workflow and a tagged release does not require the scan job to pass, although v0.2.1 checks succeeded. See the [production gates](planning/home-battery-optimizer-implementation.md#16-production-packaging-and-runtime-rollout).

## Service

- [Resident shadow service](service.md): architecture, endpoints, metrics, configuration, failure modes and the
  shadow-vs-native comparison method. Environment variables shared with the batch commands are in
  [configuration](configuration.md#environment-variables).

## Planning

| Document | Purpose |
|---|---|
| [Implementation plan](planning/home-battery-optimizer-implementation.md) | Phased plan; [§16](planning/home-battery-optimizer-implementation.md#16-production-packaging-and-runtime-rollout) is the single production sequencing checklist (Phases A to D) |
| [Battery roadmap](planning/energy-battery-roadmap.md) | Cross-component system progress summary and sequencing |
| [Load Forecaster roadmap](planning/components/load-forecaster.md) | Baseline -> server/schedule/appliance features -> ML and predictive uncertainty, with economic promotion gates |
| [Price Outlook roadmap](planning/components/price-outlook.md) | Exact PVPC -> historical publication snapshots -> selective unpublished-price forecasting |
| [Battery Model roadmap](planning/components/battery-model.md) | Assumed RTE -> physical telemetry/calibration -> solver-compatible efficiency, standby and reserve behavior |
| [Dispatch Optimizer roadmap](planning/components/dispatch-optimizer.md) | Shadow MPC -> calibrated objective/constraints -> robust scenarios and gated device handoff |
| [Baseline progress](planning/energy-baseline-progress.md) | Phase 1 status, built datasets and benchmark runs |
| [Forecast experiments](planning/energy-baseline-forecast-experiments.md) | Rolling-horizon and forecast experiment records |
| [AppDaemon baseline context](planning/energy-baseline-appdaemon-baseline-context.md) | Context on the existing AppDaemon heating/DHW control the baseline is compared against |
| [Climate optimizer plan](planning/climate-optimizer-implementation.md) | Shared-runtime components, temperature/DHW modeling, shadow evaluation, AppDaemon migration and control gates |
| [C2 recovery and alternative identification](planning/climate-c2-recovery.md) | UNDETERMINED outcome, diagnostic-first model simplification, plant/energy identification and new prospective promotion gates |
| [Climate flow calibration and optimization (C7-F)](planning/climate-flow-calibration-optimization.md) | Optional model-free joint field trial of permitted heating hours and water temperature; guarded flow actuation parked |
| [Comfort controller design](planning/comfort-controller-design.md) | Homelab AppDaemon comfort controller replacing the cheapest-hours rule: run logic, schedule, software water-temperature curve, DHW rule, rollback, before/after evaluation and the shadow MPC (track B) |
| [Climate preregistration 2026-27](planning/climate-preregistration-2026-27.md) | Criteria committed before any 2026-27 heating data: shadow thermal model and MPC (track B) and the comfort controller before/after comparison; amendments only before go-live |
| [Periodic full charge for cell balancing](planning/periodic-full-charge.md) | Plan (not implemented, off by default): opportunistic 100% charge on an adaptive cadence (amended 2026-11-04: new-balance-reading success criterion, settle hold, 5 to 28 days by balance status) chosen from next-day prices, soft-target MILP, Omnibattery weekly-full-charge as self-limiting enabler, ADR-0007 allowlist amendment, state, failure modes and the 3-PR split |
| [Battery longevity policy](planning/battery-longevity.md) | Plan: documented Omnibattery behaviour versus proposed site settings, current and target state, the six steps (full-charge PRs, balance and thermal telemetry, scheduler, high-SOC dwell cost, thermal soft cap), risks and out-of-scope |
| [Whole-system replay](planning/system-replay.md) | Offline heat-pump regime x battery replay (`hack/system_sim.py`): results as relative figures and how to reproduce them |

## Provenance

- [Provenance of the homelab extraction](provenance.md): source commit, rename, full commit map, `code_version` /
  `dataset_id` rule, signatures, `source_hashes` caveat and environment variable renames.

## Owned elsewhere

The Kubernetes deployment, ArgoCD/Helm values, secrets references and the
[production execution plan](https://github.com/pando85/homelab/blob/master/planning/energy-baseline-production-execution.md)
(extraction schedule, retention) live in `pando85/homelab`.
