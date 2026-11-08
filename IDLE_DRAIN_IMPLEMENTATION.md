# Idle Drain Implementation Summary

## Overview
Implemented idle drain (parasitic loss) in the battery physical model based on measurements showing ~26W DC drain when the battery is idle.

## Changes Made

### 1. Configuration (`config/tariff.yaml`)
- Added `idle_drain_w: 26.0` parameter to the battery block
- This represents the measured parasitic drain of 26W when the battery is idle
- The config hash changed from `7a901801...` to `41280bae...`, which will mint new dataset IDs

### 2. Battery Physics (`src/battery_optimizer/battery.py`)

#### BatterySpec dataclass
- Added `idle_loss_kwh_per_hour: float = 0.0` field
- Default value of 0.0 maintains backward compatibility
- Updated docstring to document the field

#### SOC Trajectory Calculation (`soc_trajectory_kwh`)
- Added `interval_seconds: float = 900.0` parameter
- When `idle_loss_kwh_per_hour > 0`, subtracts constant drain from every interval:
  ```python
  drain_kwh = idle_loss_kwh_per_hour * interval_seconds / 3600.0
  delta = delta - drain_kwh
  ```
- Updated docstring to document the drain behavior

#### Validation (`validate_spec`)
- Added validation: `idle_loss_kwh_per_hour` must be non-negative

#### Documentation
- Updated module docstring to reflect that idle drain is now modeled
- Updated SOC recurrence formula to include the drain term

### 3. Oracle MILP (`src/battery_optimizer/oracle.py`)

#### SOC Recurrence Constraint
- Modified the SOC recurrence constraint to include idle drain:
  ```python
  drain_kwh = spec.idle_loss_kwh_per_hour * horizon.interval_seconds / 3600.0
  rhs = 0.0 if drain_kwh == 0.0 else -drain_kwh
  ```
- Special handling for `drain_kwh == 0.0` to maintain byte-identical golden model digests
- The drain is applied unconditionally (not just when idle) for MILP linearity

### 4. Config Loading (`src/battery_optimizer/config.py`)

#### BatteryConfig dataclass
- Added `idle_drain_w: float | None = None` field
- Updated docstring to document the field

#### Config Parser
- Added parsing of `idle_drain_w` from YAML:
  ```python
  idle_drain_w = bat_raw.get("idle_drain_w")
  ```

### 5. Benchmark Resolver (`src/battery_optimizer/benchmark.py`)

#### `resolve_battery_assumptions`
- Added logic to read `idle_drain_w` from config and convert to kW:
  ```python
  idle_drain_w = getattr(battery, "idle_drain_w", None)
  if idle_drain_w is not None:
      idle_loss_kwh_per_hour = idle_drain_w / 1000.0
      idle_losses_measured = True
  ```
- Maintains backward compatibility with old `idle_loss_kwh_per_hour` field

### 6. Dispatch Validation (`src/battery_optimizer/dispatch.py`)

#### `intents_from_schedule`
- Updated opening SOC calculation to account for idle drain:
  ```python
  drain_kwh = getattr(battery_spec, "idle_loss_kwh_per_hour", 0.0) * horizon.interval_seconds / 3600.0
  opening_kwh = float(soc_kwh[0] - (charge[0] * eta_c - discharge[0] / eta_d) + drain_kwh)
  ```
- This ensures the dispatch validation correctly reverses the SOC recurrence

### 7. Tests

#### New Tests in `tests/test_battery.py`
- `TestIdleDrain` class with 6 tests:
  1. `test_zero_drain_reproduces_historical_behaviour` - verifies backward compatibility
  2. `test_idle_drain_reduces_soc_every_interval` - verifies drain is applied
  3. `test_drain_applies_during_charge_and_discharge_too` - documents unconditional application
  4. `test_validate_spec_rejects_negative_drain` - validates non-negative constraint
  5. `test_simulate_includes_drain_in_soc` - verifies simulate() includes drain
  6. `test_drain_scales_with_interval_duration` - verifies correct scaling

#### New Tests in `tests/test_oracle.py`
- `TestOracleIdleDrain` class with 3 tests:
  1. `test_oracle_with_drain_produces_feasible_plan` - verifies oracle solves with drain
  2. `test_oracle_drain_reduces_terminal_soc_vs_no_drain` - verifies drain effect
  3. `test_build_model_includes_drain_in_soc_recurrence` - verifies MILP includes drain

#### Updated Tests in `tests/test_benchmark.py`
- Updated `test_shipped_tariff_battery_assumptions_are_pinned`:
  - Changed assertion from `idle_losses_measured is False` to `is True`
  - Changed assertion from `idle_loss_kwh_per_hour == 0.0` to `== 0.026`

### 8. Documentation

#### `docs/components/battery-model.md`
- Updated "Calibrated on" section to document the 26W measurement
- Updated assumptions table: "Idle draw" row now shows 26W (adopted 2026-11-08)
- Updated status section to reflect idle drain is now modeled

#### `docs/components/regime-log.md`
- Added entry for 2026-11-08 documenting the idle drain adoption

## Design Decisions

### 1. Unconditional Application
The drain is applied unconditionally to every interval (not just when idle) for two reasons:
- **MILP Linearity**: Conditional application would require additional binary variables, making the MILP more complex
- **Physical Justification**: The 26W parasitic loss is always present (BMS, standby circuits), even during charge/discharge. During active operation, it's negligible compared to the power levels (26W vs 2500W max).

### 2. Backward Compatibility
- Default value of `idle_loss_kwh_per_hour = 0.0` maintains backward compatibility
- When drain is 0.0, the model is byte-identical to the previous version (verified by golden model digests)
- Old `idle_loss_kwh_per_hour` config field is still supported for backward compatibility

### 3. Config Hash Change
Adding `idle_drain_w` to the config changes the config hash, which:
- Mints new dataset IDs (as required)
- Invalidates old datasets (they were computed without the drain model)
- Requires re-running benchmarks with the new config

## Test Results

### Core Tests
- `tests/test_battery.py`: 90 tests passed (including 6 new idle drain tests)
- `tests/test_oracle.py`: 108 tests passed (including 3 new idle drain tests)
- `tests/test_benchmark.py`: 109 tests passed (including updated shipped config test)

### Integration Tests
- `tests/test_service_runtime.py`: 40 tests passed
- `tests/test_dispatch.py`: 159 tests passed

### Lint
- All ruff checks passed

## Validation

The implementation satisfies all requirements:
1. ✅ Added `idle_drain_w` parameter to config (default 26.0W)
2. ✅ Applied drain to SOC dynamics in oracle.py
3. ✅ Updated battery.py equations (soc_trajectory_kwh)
4. ✅ Updated documentation (battery-model.md, regime-log.md)
5. ✅ Added comprehensive tests
6. ✅ Config hash changed (dataset_id will change)
7. ✅ All tests pass
8. ✅ Lint passes
9. ✅ Backward compatible (default 0.0 maintains old behavior)

## Physical Interpretation

The 26W idle drain represents parasitic losses from:
- Battery Management System (BMS) standby power
- Internal control electronics
- Possible backup port standby (not proven)

This loss is always present, whether the battery is idle, charging, or discharging. During active operation, it's negligible (26W vs up to 2500W), but during idle periods it's the dominant loss mechanism.

## Future Work

The current implementation uses a constant drain value. Future enhancements could include:
- Power-dependent drain model (if measurements show variation)
- Temperature-dependent drain (if correlations are found)
- Separate charge/discharge efficiency curves (currently symmetric sqrt)

These would require additional measurements and model calibration.
