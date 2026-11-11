# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.18.2](https://github.com/pando85/home-energy-optimizer/tree/v0.18.2) - 2026-11-11

### Fixed

- actuator: Engage reserve protection only below the SOC floor ([6d1cd9c](https://github.com/pando85/home-energy-optimizer/commit/6d1cd9ce02feff024f241016ef783bd1f0e36648))

## [v0.18.1](https://github.com/pando85/home-energy-optimizer/tree/v0.18.1) - 2026-11-11

### Fixed

- actuator: Fall back to the plan-SOC rule when the storage value does not pay ([7f72488](https://github.com/pando85/home-energy-optimizer/commit/7f7248831ef4fb8b589b65c1a62a6fab34aa47ff))

## [v0.18.0](https://github.com/pando85/home-energy-optimizer/tree/v0.18.0) - 2026-11-11

### Added

- actuator: Measure the grid-power distribution while discharging ([64f1dc9](https://github.com/pando85/home-energy-optimizer/commit/64f1dc914471a4bd28a981578cc06414ddcaf7ac))
- service: Value-based load-following from the plan's storage value ([7b44530](https://github.com/pando85/home-energy-optimizer/commit/7b445308956895fa37ec516679dd70e433cbd50c))

## [v0.17.0](https://github.com/pando85/home-energy-optimizer/tree/v0.17.0) - 2026-11-11

### Added

- config: Layer a site overrides file over the shipped service config ([b95a020](https://github.com/pando85/home-energy-optimizer/commit/b95a020d8804155ba67fa786e2e5b41539a59575))

### Documentation

- readme: Map battery and climate optimizer components ([7c0819a](https://github.com/pando85/home-energy-optimizer/commit/7c0819a1829966d221e21e89ab2c744db2df0409))
- Reference idle drain modeling work in feature/idle-drain-model branch ([0a1ce51](https://github.com/pando85/home-energy-optimizer/commit/0a1ce516a8472cb324cc79c7c86525a7d5d0826d))

### Chore

- renovate: Monthly schedule for pre-commit hooks group ([1210b48](https://github.com/pando85/home-energy-optimizer/commit/1210b48c19a8f368c01f6b4afb295c4d31157c6d))

## [v0.16.0](https://github.com/pando85/home-energy-optimizer/tree/v0.16.0) - 2026-11-08

### Added

- actuator: Add reserve protection with bounded recovery charge below 20% SOC floor ([fe80f17](https://github.com/pando85/home-energy-optimizer/commit/fe80f17a47bfb1ae038ce5bcd21b5261f7b619b5))

### Documentation

- components: Record load-following discharge enabled with release 0.15.0 ([5e7327b](https://github.com/pando85/home-energy-optimizer/commit/5e7327b12e463025b638bb2931291fac2ffa47fa))
- components: Record the 2026-11-07 battery review findings ([659c6c0](https://github.com/pando85/home-energy-optimizer/commit/659c6c07b6deea5a2888b10da79ccc3f652258ee))

## [v0.15.0](https://github.com/pando85/home-energy-optimizer/tree/v0.15.0) - 2026-11-07

### Added

- actuator: Follow the real load in planned discharge slots ([60ff30e](https://github.com/pando85/home-energy-optimizer/commit/60ff30e674eb518fefdce87d328a63baa13b15ef))

### Documentation

- components: Log the margin change and the enabled discharge margin bound ([abab65a](https://github.com/pando85/home-energy-optimizer/commit/abab65a5ae47c70537a7e6960b112ed05c964896))
- components: Drop the cost figure from the heat pump controller page ([18b4d99](https://github.com/pando85/home-energy-optimizer/commit/18b4d998e7311771d724535eefac29079ab13413))
- State the discharge margin bound as enabled since 0.14.0 ([ac571aa](https://github.com/pando85/home-energy-optimizer/commit/ac571aa2a35a87faf60a370607da1fe4993356a5))
- Record battery findings, PD analysis and comfort controller gates ([60028de](https://github.com/pando85/home-energy-optimizer/commit/60028de31a422f23145aae0c08bd22daa9a3649e))

## [v0.14.0](https://github.com/pando85/home-energy-optimizer/tree/v0.14.0) - 2026-11-05

### Added

- planner: Bound planned discharge by the actuator's import margin ([f73221b](https://github.com/pando85/home-energy-optimizer/commit/f73221bfdfe80983d6cd61344f127a185e84f75e))

### Documentation

- planning: Plan data-driven tuning of the discharge margin ([605e105](https://github.com/pando85/home-energy-optimizer/commit/605e1053f04f96196dcd63536a48dbdf2505035d))

## [v0.13.0](https://github.com/pando85/home-energy-optimizer/tree/v0.13.0) - 2026-11-05

### Added

- climate: Publish an advisory climate plan behind an owner mode select ([227bf9a](https://github.com/pando85/home-energy-optimizer/commit/227bf9a3933fb8c69e980bd82dba83dd990c36cd))

### Documentation

- planning: Draft the per-room comfort amendment of 2026-11-05 ([5131d33](https://github.com/pando85/home-energy-optimizer/commit/5131d33492cc243d8e95408cc45924830b0a3288))

## [v0.12.0](https://github.com/pando85/home-energy-optimizer/tree/v0.12.0) - 2026-11-05

### Added

- climate: Make the comfort schedule per room ([e952ede](https://github.com/pando85/home-energy-optimizer/commit/e952ede42c86fe9db60cce6ea5eba1acea5f42d4))

### Documentation

- components: Record the backup heater decision and its outside-defrost KPI ([aad1bc0](https://github.com/pando85/home-energy-optimizer/commit/aad1bc07aac935c01c4a07207af89d76aa66ff2f))

## [v0.11.0](https://github.com/pando85/home-energy-optimizer/tree/v0.11.0) - 2026-11-04

### Fixed

- climate: Bridge short input outages in the live shadow observer ([9c5064c](https://github.com/pando85/home-energy-optimizer/commit/9c5064c01400adf8cca6b9da71410617a2c05ae0))

### Documentation

- components: Add KPI sections to the climate components ([7eb38e7](https://github.com/pando85/home-energy-optimizer/commit/7eb38e7844cc360e1ddd1cd665fe9815754c0ac5))
- Record the planned-discharge shortfall and its open fix ([6a4ebf6](https://github.com/pando85/home-energy-optimizer/commit/6a4ebf6a0d820cd92a6deb4f635bb0cdf94927d4))
- Park derate-aware planning until the battery fans are in ([d4ef700](https://github.com/pando85/home-energy-optimizer/commit/d4ef700ef716065dcc532329942cd9e6f8449f25))

## [v0.10.0](https://github.com/pando85/home-energy-optimizer/tree/v0.10.0) - 2026-11-04

### Added

- service: Read-only thermal inputs, thermal state and refit ([34ebdaa](https://github.com/pando85/home-energy-optimizer/commit/34ebdaaeb9062b76a9b11fb73307901c3be6dfd0))

### Documentation

- planning: Derate-aware planning study, thermal model fit and battery-thermal component ([2ed41dc](https://github.com/pando85/home-energy-optimizer/commit/2ed41dc2f973670d0b35d43c8628e93283817732))

## [v0.9.0](https://github.com/pando85/home-energy-optimizer/tree/v0.9.0) - 2026-11-04

### Added

- service: Day-ahead plan next to the final plan, with forecast KPIs ([ba32ce1](https://github.com/pando85/home-energy-optimizer/commit/ba32ce11ba182ca7eee53a3fc41d059348db2d63))

## [v0.8.0](https://github.com/pando85/home-energy-optimizer/tree/v0.8.0) - 2026-11-04

### Added

- planner: High-SOC dwell cost in the solver, planner and replay, with its study ([3a62aee](https://github.com/pando85/home-energy-optimizer/commit/3a62aee13d21815d897d79a13ae65bafbd7cff32))
- planner: Thermal soft cap on charge power in the solver, planner and replay, with its studies ([de0690e](https://github.com/pando85/home-energy-optimizer/commit/de0690e3528131c1fac16c08907748706ec2bfe4))
- service: Scheduler for the periodic full charge (full-charge PR 3) ([265e611](https://github.com/pando85/home-energy-optimizer/commit/265e611b683a73c0c859b89420b8f6216fd6a78a))

## [v0.7.0](https://github.com/pando85/home-energy-optimizer/tree/v0.7.0) - 2026-11-04

### Added

- actuator: Arm Omnibattery's weekly full charge for the periodic full charge ([76b04f1](https://github.com/pando85/home-energy-optimizer/commit/76b04f1610fe9a129a38f241998c0dd711dde141))
- planner: Soft full-charge target in the solver and planner (full-charge PR 1) ([a5eb6c8](https://github.com/pando85/home-energy-optimizer/commit/a5eb6c8715d4d5bcacd8908731e593438b9dce9b))
- service: Read-only battery health telemetry ([4753e55](https://github.com/pando85/home-energy-optimizer/commit/4753e55937e33602835a9e81ebc39c1a3d4efdd5))

### Documentation

- components: Record the 0.6.1 deploy time in the regime log ([156453b](https://github.com/pando85/home-energy-optimizer/commit/156453bcb955737718b7c35f009571a96b0097d3))
- planning: Plan the periodic full charge for cell balancing ([569e186](https://github.com/pando85/home-energy-optimizer/commit/569e186f7ef478cfee9e03b9ae622bbb14f6254c))
- planning: Amend the full-charge plan and add the battery longevity policy ([f7b12c3](https://github.com/pando85/home-energy-optimizer/commit/f7b12c354a73524958c22a35208e9040ac346cbb))

## [v0.6.1](https://github.com/pando85/home-energy-optimizer/tree/v0.6.1) - 2026-11-03

### Fixed

- extract: Stop querying Prometheus entities after their retirement ([67f2b5e](https://github.com/pando85/home-energy-optimizer/commit/67f2b5e2cb929f99b5197c04a6b01ce7c7286a78))
- package: Report the version pyproject.toml declares ([697f471](https://github.com/pando85/home-energy-optimizer/commit/697f4716ac1fe8893285505690a31f130720197e))
- service: Check the idle battery energy counters for availability only ([dbb6f1d](https://github.com/pando85/home-energy-optimizer/commit/dbb6f1d73d5622922d71f30241c905e371eb8be4))
- service: Adopt a plan late in a slot that opened without one ([a0494d6](https://github.com/pando85/home-energy-optimizer/commit/a0494d6783550e50804c975c63a89a0960fe23be))
- service: Keep unchanged counters and SOC at slot edges while HA is in sync ([ebc9ef5](https://github.com/pando85/home-energy-optimizer/commit/ebc9ef5b4e8889af60d16c896e16539dc57f9f3e))

## [v0.6.0](https://github.com/pando85/home-energy-optimizer/tree/v0.6.0) - 2026-11-03

### Added

- service: Log what the running service decides and does ([886b0a4](https://github.com/pando85/home-energy-optimizer/commit/886b0a4102ed1a823e9075017c90723553313d02))

## [v0.5.1](https://github.com/pando85/home-energy-optimizer/tree/v0.5.1) - 2026-11-03

### Fixed

- features: Read the servers from the battery backup port after the rewiring ([9822f84](https://github.com/pando85/home-energy-optimizer/commit/9822f84b98ec190045445b770bdb132397205944))

## [v0.5.0](https://github.com/pando85/home-energy-optimizer/tree/v0.5.0) - 2026-11-03

### Added

- config: Commission the Venus E V3 battery in the service config ([81236e0](https://github.com/pando85/home-energy-optimizer/commit/81236e0dbf8adb732d3951df4d72ea78b6c9038c))
- service: Add dry_run and live battery control through Omnibattery ([1836691](https://github.com/pando85/home-energy-optimizer/commit/1836691867d8355fd45e1db68c8667df84bd08e8))
- service: Plan from the battery SOC entity and expose clamped actuation ([181488c](https://github.com/pando85/home-energy-optimizer/commit/181488c5fb3e3b35c4aaa523195369e8477627e6))

## [v0.4.2](https://github.com/pando85/home-energy-optimizer/tree/v0.4.2) - 2026-11-03

### Fixed

- build: Subtract battery charge and add discharge when reconstructing gross load ([4db5f19](https://github.com/pando85/home-energy-optimizer/commit/4db5f19ff8a44f7d487e19d643683b10244f7d54))

## [v0.4.1](https://github.com/pando85/home-energy-optimizer/tree/v0.4.1) - 2026-10-31

### Fixed

- service: Wait for the Home Assistant sync before the climate shadow catch-up run ([3845f67](https://github.com/pando85/home-energy-optimizer/commit/3845f67d1ca2f970a84c701452158f2398263f4c))

## [v0.4.0](https://github.com/pando85/home-energy-optimizer/tree/v0.4.0) - 2026-10-31

### Added

- climate: Add the C2 data loader, config and audit ([953e3b3](https://github.com/pando85/home-energy-optimizer/commit/953e3b3dbb3111aa20626fb6e3eacc873d57e4d0))
- climate: Add the heat-pump electricity (COP) model ([2182e3e](https://github.com/pando85/home-energy-optimizer/commit/2182e3eecb12f6ccfdbb43fe2258e8ba7cd80447))
- climate: Add thermal models, heat response and climate-fit ([0ce7812](https://github.com/pando85/home-energy-optimizer/commit/0ce78129972b3359b07b1b75b38e322f3dba8417))
- climate: Add an AEMET reference station and a fused outdoor temperature ([c792a8b](https://github.com/pando85/home-energy-optimizer/commit/c792a8b483eaa2f345b04688c36e915c8af49c63))
- climate: Run the predeclared model iteration ([a9c26f8](https://github.com/pando85/home-energy-optimizer/commit/a9c26f8492a7eff234ab032a106681af21554b00))
- climate: Add a price-blind comfort MPC and its offline replay (track B1) ([c285046](https://github.com/pando85/home-energy-optimizer/commit/c28504610cb3bc70b8f1cadfcf958d52ebbd0eea))
- climate: Run the thermal model and comfort MPC in live shadow (track B2) ([45ce7f4](https://github.com/pando85/home-energy-optimizer/commit/45ce7f455fc0226a319e116b5d81bc8d19edc110))
- extract: Add the OMIE day-ahead price source ([8b14542](https://github.com/pando85/home-energy-optimizer/commit/8b14542b8ca68a48e491060573e875fe0221c602))
- forecast: Add cheap statistical load-forecaster candidates ([aa1334f](https://github.com/pando85/home-energy-optimizer/commit/aa1334f9738ce01f94572657aec279f0e855e239))
- forecast: Add a structured component load forecaster ([13c904f](https://github.com/pando85/home-energy-optimizer/commit/13c904f8e37123b8557ffd2f99a5f98769807ef2))
- forecast: P4 second look with the OMIE price estimate and a disabled-day fix ([ca50aab](https://github.com/pando85/home-energy-optimizer/commit/ca50aab13bb5354f90ba177a026252edc9cf2107))
- hack: Add a read-only heat-pump x battery system replay ([f5be13d](https://github.com/pando85/home-energy-optimizer/commit/f5be13da8fb7c3eeef958002de205e06f8a59374))
- just: Add a pull-store recipe for local analysis copies ([3259944](https://github.com/pando85/home-energy-optimizer/commit/3259944c5f7baa352bea3a09c5859e294b66f6ba))
- price-outlook: Add OMIE next-day price estimators (PO-C) ([b6484b7](https://github.com/pando85/home-energy-optimizer/commit/b6484b70169e97f8e271d0e402b1163311255138))
- rolling-horizon: Add price-outlook seam with exact, published_only and persistence ([e2f498b](https://github.com/pando85/home-energy-optimizer/commit/e2f498bdc36702de734a281d7ff5ef6a0dd12007))
- service: Price battery wear at 0.01 EUR/kWh in the live planner ([c88342c](https://github.com/pando85/home-energy-optimizer/commit/c88342ce4c9a317d6fd58b759f2c7773ba597b12))
- Add offline climate extraction and climate feature set (C1a) ([ac90586](https://github.com/pando85/home-energy-optimizer/commit/ac90586d9dba850b91ff571810e7d355535553f1))

### Fixed

- build: Reconstruct gross load from battery counters after commissioning ([40e6bb8](https://github.com/pando85/home-energy-optimizer/commit/40e6bb86c58856ca32d2825ffab30a6a6668dee8))

### Documentation

- climate: Plan flow-temperature calibration and guarded optimization ([ded8cbf](https://github.com/pando85/home-energy-optimizer/commit/ded8cbf4103e008050c861118ef4fe33b2a5e12b))
- planning: Reconcile C7-F with the C2 outcome and plan the joint field trial ([109432e](https://github.com/pando85/home-energy-optimizer/commit/109432e926f14455b63e270f0b8438c7470a32c1))
- Record the PO-A result and unblock load forecaster P4 ([d6125dc](https://github.com/pando85/home-energy-optimizer/commit/d6125dc77653d198453c2a9d053c852198c78882))
- Record the P2 result in the load forecaster plan ([12eb815](https://github.com/pando85/home-energy-optimizer/commit/12eb815bfa4681e1a870de3079e834a32b30306d))
- Park the load forecaster and price outlook plans ([936f01f](https://github.com/pando85/home-energy-optimizer/commit/936f01f8ea38a58647aa90538d7497c56359f456))
- Point the direnv example at the pulled store copy ([6954200](https://github.com/pando85/home-energy-optimizer/commit/6954200b98ec07b63bc7b21a70d50e7d9d1e0b67))
- Plan shared climate MPC on the existing battery optimizer runtime ([a2eb559](https://github.com/pando85/home-energy-optimizer/commit/a2eb559c397e7ad834db16099a485ebfe6ae0d71))
- Record climate topology, data audit, C2 economic gate and C0 scope ([4418ec9](https://github.com/pando85/home-energy-optimizer/commit/4418ec9c6c1720187785965640dcacf1d84cf900))
- Record the C2 design and predeclared climate gate ([69ed998](https://github.com/pando85/home-energy-optimizer/commit/69ed998ad4db168c870954d90b596664de5da7b9))
- Document the whole-system replay and how to reproduce it ([3008366](https://github.com/pando85/home-energy-optimizer/commit/3008366d4fd5c59e642a8a1b18845439ff506b34))
- Decouple heat-pump comfort control from battery price optimisation (ADR-0006) ([feb1884](https://github.com/pando85/home-energy-optimizer/commit/feb1884c35e2fe08756c083e6bfc20e472acfd13))
- Add component calibration records and the plant regime log ([0a1cdc0](https://github.com/pando85/home-energy-optimizer/commit/0a1cdc0a5971b5aae7a617019eb45479c9cff1ce))
- Record the Aquarea heater and import-limit facts in the component pages ([276df4a](https://github.com/pando85/home-energy-optimizer/commit/276df4a4011c7e3f42a831caf9459bb87063f839))
- Design the comfort controller and amend ADR-0006 ([e30d7c3](https://github.com/pando85/home-energy-optimizer/commit/e30d7c3f9d8258ddb2bde8b89e17f9f0f4f43e94))
- Record the owner's comfort-controller answers ([509b222](https://github.com/pando85/home-energy-optimizer/commit/509b222167f81f15ac92ad8e360ec27f0c1d9f3a))
- Record the B0 diagnosis and preregister the 2026-27 criteria ([0e8ca30](https://github.com/pando85/home-energy-optimizer/commit/0e8ca302f8cd4d7e550e3583982c2a54d8bb7c16))

### CI

- Refuse AI session links in commit messages ([9e0168a](https://github.com/pando85/home-energy-optimizer/commit/9e0168a0a5aaae5702ba181feea34b8f08438c75))

### Chore

- Follow the repository rename to home-energy-optimizer ([2bd8165](https://github.com/pando85/home-energy-optimizer/commit/2bd81650bc69c937fe416443b060b7025c894cf1))
- Make the comfort-history and cheap-price analyses reproducible ([4d1dadf](https://github.com/pando85/home-energy-optimizer/commit/4d1dadf0996f78229993df219dd48ff781587664))

## [v0.3.0](https://github.com/pando85/battery-optimizer/tree/v0.3.0) - 2026-10-25

### Added

- features: Add sub-load feature table and load decomposition report ([9ef0654](https://github.com/pando85/battery-optimizer/commit/9ef0654944f3591aa9ed4e528a154ffbc82ae754))
- forecast: Add a load-forecaster seam with an offline perfect oracle ([b6af2fd](https://github.com/pando85/battery-optimizer/commit/b6af2fd6f52af7d8f580e7d81a281342a7920077))
- forecast-eval: Add a pre-registered load-forecaster evaluation harness ([d6646ad](https://github.com/pando85/battery-optimizer/commit/d6646ad4d6c1dcc61ed4979b3d8d7cc87b6f7620))
- service: Archive price snapshots and weather forecasts ([7b57745](https://github.com/pando85/battery-optimizer/commit/7b5774577c4b5aca608cb1ac18f1d174d825989c))

### Documentation

- Reconcile production roadmap with v0.2.1 and homelab rollout ([d9617c9](https://github.com/pando85/battery-optimizer/commit/d9617c9a1c09fc6d377d3daabd3490ada62b930d))
- Split the README into focused guides ([66acfe4](https://github.com/pando85/battery-optimizer/commit/66acfe4191610857030aa92d5d223de0bef55ad2))
- Define planning components and individual roadmaps ([a8d6bc7](https://github.com/pando85/battery-optimizer/commit/a8d6bc791ce3a253aa1b04f43d78e7e8978342cc))
- Add load forecaster improvement plan ([8f8a867](https://github.com/pando85/battery-optimizer/commit/8f8a867f28f904220ee95042a4a9f120df1e016a))
- Record P0-P3 outcomes and plan P4 in the load forecaster plan ([3c28ec2](https://github.com/pando85/battery-optimizer/commit/3c28ec200145cd444ade21cd8941ef1963b6eddd))
- Add price outlook improvement plan ([cd786de](https://github.com/pando85/battery-optimizer/commit/cd786dea6517de154194563131bc273f8fafcf2b))

## [v0.2.1](https://github.com/pando85/battery-optimizer/tree/v0.2.1) - 2026-10-25

### Fixed

- extract: Exit non-zero when a run records coverage gaps ([44eb411](https://github.com/pando85/battery-optimizer/commit/44eb411ad341c9ceb9a355ec09ee492bc00cfe13))

## [v0.2.0](https://github.com/pando85/battery-optimizer/tree/v0.2.0) - 2026-10-25

### Added

- service: Add the resident shadow service ([a1d52fe](https://github.com/pando85/battery-optimizer/commit/a1d52fef8b571d6515b9b339fde4bbc3f499aeb5))

### CI

- Lint the PR title with commitlint ([173e9ee](https://github.com/pando85/battery-optimizer/commit/173e9ee53f53ed22cedca77dc38279830888775c))
- Attach only the wheel and sdist to GitHub releases ([f6d935e](https://github.com/pando85/battery-optimizer/commit/f6d935e414cd2c51656153afa951c7693f51aa45))

## [v0.1.0](https://github.com/pando85/battery-optimizer/tree/v0.1.0) - 2026-10-25

### Added

- manifest: Accept a build-time commit identity when git is unavailable ([db1993a](https://github.com/pando85/battery-optimizer/commit/db1993abbd3b23fbbfeb33175ebf3f05e9ad1954))

### Documentation

- battery: Decide production runtime and independent repo ownership (pando85/homelab#4653) ([97fdf17](https://github.com/pando85/battery-optimizer/commit/97fdf1734f9b74704ab07e9ea0154f66e530afca))
- battery: Document Omnibattery HA control contract and integration gates (pando85/homelab#4654) ([b184ed2](https://github.com/pando85/battery-optimizer/commit/b184ed2ca6227c5428aac648a8216aaa9b6862b2))
- Propose phased home battery economic optimizer ADR (pando85/homelab#4651) ([3e01138](https://github.com/pando85/battery-optimizer/commit/3e01138231f41d85a689502d0c2b53d77934c5b4))
- ADR-0002 appliance-aware load forecasting with HA telemetry and NILM (pando85/homelab#4652) ([5a2cb0d](https://github.com/pando85/battery-optimizer/commit/5a2cb0d5c2e8961a4f5530d75323de73d04e47c5))
- Define reduced-scope energy baseline ADR ([bccfb0b](https://github.com/pando85/battery-optimizer/commit/bccfb0b25fe0dc737a7f2b731ada93e3f6104a20))
- Record production execution path for energy baseline ([e470103](https://github.com/pando85/battery-optimizer/commit/e4701037635bc2efd6dda2fa7de71409a921f143))
- Record conditional acceptance of the four-policy baseline ([b6a01e8](https://github.com/pando85/battery-optimizer/commit/b6a01e85617011bb3705274eff46e318bdd6d3b2))
- Record owner-supplied battery and price facts ([43a696d](https://github.com/pando85/battery-optimizer/commit/43a696d6e2d3f590bab7a2e66726f4c757d25df3))
- Fill rebuild placeholders with the 0.80 ladder results ([a379e3f](https://github.com/pando85/battery-optimizer/commit/a379e3fa5440968950f9eba37554450465a68b5c))
- Record the bounded pre-migration milestone ([f9033ba](https://github.com/pando85/battery-optimizer/commit/f9033ba5f49971344f2ce369c652fb3605c31357))
- Resolve contradictory milestone claims and disclose provenance ([d765013](https://github.com/pando85/battery-optimizer/commit/d7650139555eb08367d34e605c00d5ec3ddcfa9a))
- Record pinned re-runs and the causal five-rung ladder ([7d64b5b](https://github.com/pando85/battery-optimizer/commit/7d64b5be9bacd5409db4732e37b4f5c3d5e4b942))
- Record the independent review of the pinned evidence ([0dd35be](https://github.com/pando85/battery-optimizer/commit/0dd35be8ff0506e61a9002d51d5377c7b46ecc06))
- Fix links and paths in the moved ADRs and planning notes ([e70d2f2](https://github.com/pando85/battery-optimizer/commit/e70d2f2272ea36a6bea785ae0ce871ccce6a110e))
- Record production sequencing as phases A to D ([6bab29c](https://github.com/pando85/battery-optimizer/commit/6bab29cd74e8ba3e87b90429116224ba0bd14279))
- Add provenance of the homelab extraction and a docs index ([2b407c1](https://github.com/pando85/battery-optimizer/commit/2b407c17ade984afcd02c65fdf158a4caf141516))
- Add badges, container usage and environment variables to the README ([fc77027](https://github.com/pando85/battery-optimizer/commit/fc77027e3596db39679f2055eab6881bc223f67d))
- Add AGENTS.md and CLAUDE.md ([01d3b50](https://github.com/pando85/battery-optimizer/commit/01d3b50a582fd6196427a1f8b4b5c0f671292aba))

### Build

- docker: Add multi-stage container image ([3c58df4](https://github.com/pando85/battery-optimizer/commit/3c58df4ae5664990beb6b3596fd1304c7b1373c8))
- Target Python 3.14 ([d9eebfd](https://github.com/pando85/battery-optimizer/commit/d9eebfd618bb5a99fb8fcfd8eba93b9f290054e7))
- Add package metadata and a tools dependency group ([b98a064](https://github.com/pando85/battery-optimizer/commit/b98a064dbf05b3d52f11e4da860df3f66ddbf0e4))
- Add Makefile ([68d55b0](https://github.com/pando85/battery-optimizer/commit/68d55b030fcb4ef607a47a5bfb2aba043c78a3b5))
- Add changelog generation and release script ([9597c04](https://github.com/pando85/battery-optimizer/commit/9597c04c741476d9fb2d71c9c7fc6f05749df260))
- Add pre-commit hooks and commit linting ([51d9b24](https://github.com/pando85/battery-optimizer/commit/51d9b24c59ea61a09660185be51d0ba21eb735ea))
- Create CHANGELOG.md on the first release ([89d072b](https://github.com/pando85/battery-optimizer/commit/89d072b36ad062c391da8464252004554a3417f1))
- Normalize trailing newlines of the generated changelog ([5a3dfa9](https://github.com/pando85/battery-optimizer/commit/5a3dfa94acfa90514ef3fc562acfbfea2302f08f))
- Replace the Makefile with a justfile ([639afcd](https://github.com/pando85/battery-optimizer/commit/639afcdb04679a4a59d810c8cee37ead6d071393))

### CI

- Add lint and test workflow ([97efa25](https://github.com/pando85/battery-optimizer/commit/97efa255e8a5914b9896d756933371398f8096de))
- Add pre-commit workflow ([37be66a](https://github.com/pando85/battery-optimizer/commit/37be66a50f1d9545d8babd04401f23a0328b920c))
- Add image build, publish and release workflow ([dc4c660](https://github.com/pando85/battery-optimizer/commit/dc4c6607b77b33d7e21b94844cbec015d4ce9b91))
- Add auto-tag workflow ([a5f424b](https://github.com/pando85/battery-optimizer/commit/a5f424b0006fe8317e95292f6aad45600cd71d88))
- Feed commit messages to commitlint through COMMIT_EDITMSG ([be309a8](https://github.com/pando85/battery-optimizer/commit/be309a8bd9e91b66119dd73c613d38c22aca2292))

### Chore

- Add Renovate config ([0fc61c7](https://github.com/pando85/battery-optimizer/commit/0fc61c758c7f39853a483ee546f307595b84bbea))
- Drop the redundant Renovate regex manager ([47c3066](https://github.com/pando85/battery-optimizer/commit/47c30667aa112ce5c52677892f59c76060a50bed))
- Add GitHub community files ([7ae38cf](https://github.com/pando85/battery-optimizer/commit/7ae38cfac94fc764ae27d15d0c1eeb3f45cd3865))
- Add direnv example and ignore .envrc ([d1e9bd3](https://github.com/pando85/battery-optimizer/commit/d1e9bd37709533f9eaf095e30f8e9ed4aa049b23))

### Refactor

- Rename package to battery_optimizer ([3b5a93a](https://github.com/pando85/battery-optimizer/commit/3b5a93ab09280eba18a75f6f7533750feb99fc57))

### Testing

- Resolve git-tree refusal fixture against the repository root ([67e99ef](https://github.com/pando85/battery-optimizer/commit/67e99ef2f9953dc7bec0c4692b9ead8f8e17143a))
- Compare golden cost totals with a 1e-12 relative tolerance ([2e3504c](https://github.com/pando85/battery-optimizer/commit/2e3504cf2188e46b6669710d65c46c63688d583c))

### Scripts

- Add energy-baseline package with Prometheus and HA extraction ([0b9e726](https://github.com/pando85/battery-optimizer/commit/0b9e72647e1c69692af42aa4113c19064f86458b))
- Complete energy-baseline benchmark and correct build facts ([fc6375d](https://github.com/pando85/battery-optimizer/commit/fc6375d1eef108e6a16cc060ef80a7d9b3a74d99))
- Deduplicate HA re-extraction and record Phase 1 decisions ([5b735a5](https://github.com/pando85/battery-optimizer/commit/5b735a5b03a18d7cb5b5a17a040e067500e738a2))
- Add day-ahead policy on a shared scoring basis ([0ce3447](https://github.com/pando85/battery-optimizer/commit/0ce3447e1d699a40545a76fa5c19c0660d4e6bbb))
- Add two-publication rolling-horizon experiment ([3eb41a9](https://github.com/pando85/battery-optimizer/commit/3eb41a9f7ab1c7d29c635b991180a641711ae233))
- Promote rolling horizon to a fifth policy at RTE 0.80 ([bda3d7d](https://github.com/pando85/battery-optimizer/commit/bda3d7d522be6a14da777a97faf4bc6d400c9e1e))
- Correct false forecast-basis comparability claim ([395a408](https://github.com/pando85/battery-optimizer/commit/395a408f968e568fb94e440221f783d8d8b4443a))
- Add artifact-integrity manifest module and report wiring ([0bfec3b](https://github.com/pando85/battery-optimizer/commit/0bfec3ba438fe711d8b73ce9e424fefa36cfa920))
- Align rolling-horizon load forecast with the day-ahead basis ([d7f8b06](https://github.com/pando85/battery-optimizer/commit/d7f8b062f5d85e7c4b280e191289e7c12d8fd3de))
- Add sensitivity sweeps, replay runtime, and validation runner ([87afbb6](https://github.com/pando85/battery-optimizer/commit/87afbb6356d0794ca10b77d81a474ebd4ce39a92))
- Preserve plan sequence across restart in the replay runtime ([173fb3f](https://github.com/pando85/battery-optimizer/commit/173fb3ffb15881432f6687e08cb7b04a5ebeaade))
- Correct docstring overpromises and derive slots per day ([428b019](https://github.com/pando85/battery-optimizer/commit/428b0190bec45878cdf076268abf9e0393d231fa))
- Pin code provenance in both experiment runners ([f57c27b](https://github.com/pando85/battery-optimizer/commit/f57c27ba4c4de83ffce3704e891541642b111b1e))
