"""Discharge-time grid metrics (docs/planning/discharge-margin-tuning.md, step 1).

The runtime wiring (grid-power ``state_changed`` -> actuator) is in ``test_service_actuator.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from service_fakes import NOW, entity_state
from test_service_actuator import (
    ENTS,
    GRID,
    SETTINGS,
    ctx,
    intent,
    make,
    tick,
    world,
)

from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.entity_store import UpdateResult
from battery_optimizer.service.metrics import (
    POWER_BANDS,
    ServiceMetrics,
    _actuator_families,
    power_band,
)

HIST = "battery_optimizer_actuator_discharge_grid_watts"
EXPORT = "battery_optimizer_actuator_export_seconds_total"


def discharging(*, ac=-300.0, grid=120.0, mode="live", settings=SETTINGS):
    """An actuator whose last tick is in control, guard ok and in discharge mode."""
    clock = FakeClock(NOW)
    store = world(clock, grid=grid, ac=ac, discharge="on", limits="on", pd=100, cap=600)
    actuator, _, metrics = make(clock, store, mode=mode, settings=settings)
    tick(actuator, ctx(clock, intent("discharge", 600)))
    assert actuator.desired.mode == "discharge" and actuator.reasons == ()
    return actuator, store, clock, metrics


def push(actuator, store, clock, value, *, after=2.0, ac=None):
    """One grid-power update *after* seconds later, delivered as the runtime delivers it."""
    clock.advance(after)
    if ac is not None:
        store.apply(entity_state(ENTS.battery_ac_power, ac, clock.now()))
    state = entity_state(GRID, value, clock.now())
    result = store.apply(state)
    actuator.observe_grid_power(state, result)
    return result


def count(metrics, band=None):
    if band is not None:
        return metrics.registry.get_sample_value(f"{HIST}_count", {"power_band": band})
    return sum(count(metrics, b) for b in POWER_BANDS)


def export_s(metrics):
    return metrics.registry.get_sample_value(EXPORT)


def bucket(metrics, band, le):
    return metrics.registry.get_sample_value(f"{HIST}_bucket", {"power_band": band, "le": le})


def test_series_exist_at_zero_before_any_update():
    metrics = ServiceMetrics(actuation=True)
    assert export_s(metrics) == 0.0
    assert [count(metrics, b) for b in POWER_BANDS] == [0.0, 0.0, 0.0]
    text = metrics.render().decode()
    for le in ("-500.0", "-200.0", "-100.0", "-50.0", "-25.0", "0.0", "25.0", "50.0", "75.0"):
        assert f'{HIST}_bucket{{le="{le}",power_band="lt500"}}' in text
    for le in ("100.0", "125.0", "150.0", "200.0", "300.0", "500.0", "+Inf"):
        assert f'{HIST}_bucket{{le="{le}",power_band="ge1000"}}' in text


@pytest.mark.parametrize(
    ("watts", "band"),
    [
        (0.0, "lt500"),
        (499.9, "lt500"),
        (500.0, "500_1000"),
        (999.9, "500_1000"),
        (1000.0, "ge1000"),
        (2500.0, "ge1000"),
    ],
)
def test_power_band_edges(watts, band):
    assert power_band(watts) == band


def test_the_band_comes_from_the_measured_battery_discharge_power():
    actuator, store, clock, metrics = discharging(ac=-300)
    push(actuator, store, clock, 110)  # 300 W discharge
    push(actuator, store, clock, 90, ac=-700)
    push(actuator, store, clock, 80, ac=-1500)
    push(actuator, store, clock, 70, ac=-1500)
    assert [count(metrics, b) for b in POWER_BANDS] == [1.0, 1.0, 2.0]
    # 110 W is in the le=125 bucket, not in le=100; the buckets are cumulative.
    assert bucket(metrics, "lt500", "100.0") == 0.0
    assert bucket(metrics, "lt500", "125.0") == 1.0
    assert bucket(metrics, "ge1000", "75.0") == 1.0 and bucket(metrics, "ge1000", "+Inf") == 2.0


def test_the_band_follows_the_configured_battery_power_sign():
    settings = replace(SETTINGS, battery_ac_power_sign=-1)
    actuator, store, clock, metrics = discharging(ac=300, settings=settings)  # +300 raw = discharge
    push(actuator, store, clock, 110, ac=800)
    assert [count(metrics, b) for b in POWER_BANDS] == [0.0, 1.0, 0.0]


def test_a_missing_battery_power_gives_no_sample():
    actuator, store, clock, metrics = discharging()
    clock.advance(2)
    state = entity_state(GRID, 110, clock.now())
    result = store.apply(state)
    store._states.pop(ENTS.battery_ac_power)  # the reading is gone: no band can be assigned
    actuator.observe_grid_power(state, result)
    assert count(metrics) == 0.0


def test_a_re_confirmed_value_is_not_a_new_histogram_sample():
    actuator, store, clock, metrics = discharging()
    push(actuator, store, clock, 110)
    clock.advance(10)
    again = entity_state(GRID, 110, clock.now() - timedelta(seconds=10), reported=clock.now())
    assert store.apply(again) is UpdateResult.REFRESHED
    actuator.observe_grid_power(again, UpdateResult.REFRESHED)
    assert count(metrics) == 1.0


@pytest.mark.parametrize("mode", ["idle", "charge"])
def test_nothing_counts_outside_discharge(mode):
    clock = FakeClock(NOW)
    store = world(clock, grid=-50, ac=-300)
    actuator, _, metrics = make(clock, store)
    tick(actuator, ctx(clock, intent(mode, 600)))
    assert actuator.desired.mode == mode
    for value in (-50, -60, -70):
        push(actuator, store, clock, value)
    assert count(metrics) == 0.0 and export_s(metrics) == 0.0


def test_nothing_counts_before_the_first_tick():
    clock = FakeClock(NOW)
    store = world(clock, grid=-50, ac=-300)
    actuator, _, metrics = make(clock, store)
    for value in (-50, -60):
        push(actuator, store, clock, value)
    assert count(metrics) == 0.0 and export_s(metrics) == 0.0


def test_nothing_counts_while_the_guard_is_not_ok():
    clock = FakeClock(NOW)
    store = world(clock, grid=-50, ac=-300, discharge="on", limits="on", pd=100, cap=600)
    actuator, _, metrics = make(clock, store)
    tick(actuator, ctx(clock, intent("discharge", 600), price_fresh=False))
    assert "price_stale" in actuator.reasons and actuator.desired.mode == "safe_idle"
    for value in (-50, -60, -70):
        push(actuator, store, clock, value)
    assert count(metrics) == 0.0 and export_s(metrics) == 0.0


def test_nothing_counts_without_control_or_while_ha_is_not_synced():
    clock = FakeClock(NOW)
    store = world(
        clock, grid=-50, ac=-300, discharge="on", limits="on", pd=100, cap=600, kill="off"
    )
    actuator, _, metrics = make(clock, store)
    tick(actuator, ctx(clock, intent("discharge", 600)))
    assert not actuator.in_control
    push(actuator, store, clock, -50)
    push(actuator, store, clock, -60)
    assert count(metrics) == 0.0 and export_s(metrics) == 0.0
    # Control taken, then HA drops: the next tick is ha_not_synced, a guard reason.
    store.apply(entity_state(ENTS.kill_switch, "on", clock.now()))
    tick(actuator, ctx(clock, intent("discharge", 600)))
    assert actuator.desired.mode == "discharge"
    tick(actuator, ctx(clock, intent("discharge", 600), ha_synced=False))
    assert actuator.reasons == ("ha_not_synced",)
    push(actuator, store, clock, -50)
    push(actuator, store, clock, -60)
    assert count(metrics) == 0.0 and export_s(metrics) == 0.0


def test_dry_run_counts_like_live():
    actuator, store, clock, metrics = discharging(mode="dry_run")
    push(actuator, store, clock, -20)
    push(actuator, store, clock, -20)
    assert count(metrics) == 2.0 and export_s(metrics) == pytest.approx(2.0)


def test_a_stalled_tick_loop_stops_the_counting():
    actuator, store, clock, metrics = discharging()
    push(actuator, store, clock, 110, after=60)  # 60 s after the tick: still within 3 intervals
    assert count(metrics) == 1.0
    push(actuator, store, clock, 110, after=60)  # 120 s: the loop no longer vouches for the mode
    assert count(metrics) == 1.0


def test_a_stale_reading_is_not_counted():
    actuator, store, clock, metrics = discharging()
    clock.advance(2)
    old = entity_state(GRID, 110, clock.now() - timedelta(seconds=45))  # older than 30 s
    result = store.apply(old)
    actuator.observe_grid_power(old, result)
    assert count(metrics) == 0.0


def test_export_seconds_integrate_between_fresh_consecutive_updates():
    actuator, store, clock, metrics = discharging()
    push(actuator, store, clock, -20)  # the first sample opens the chain
    assert export_s(metrics) == 0.0
    push(actuator, store, clock, -40)  # previous -20 held for 2 s
    push(actuator, store, clock, 60, after=3)  # previous -40 held for 3 s
    push(actuator, store, clock, 70)  # previous 60: no export
    assert export_s(metrics) == pytest.approx(5.0)
    assert count(metrics) == 4.0
    # exactly 0 W is not an export
    push(actuator, store, clock, 0)
    push(actuator, store, clock, 50)
    assert export_s(metrics) == pytest.approx(5.0)


def test_a_re_confirmed_export_value_keeps_the_integration_going():
    actuator, store, clock, metrics = discharging()
    push(actuator, store, clock, -30)
    clock.advance(10)
    again = entity_state(GRID, -30, clock.now() - timedelta(seconds=10), reported=clock.now())
    assert store.apply(again) is UpdateResult.REFRESHED
    actuator.observe_grid_power(again, UpdateResult.REFRESHED)
    assert export_s(metrics) == pytest.approx(10.0)
    assert count(metrics) == 1.0


def test_export_is_not_counted_across_a_stale_gap():
    actuator, store, clock, metrics = discharging()
    push(actuator, store, clock, -30)
    # A tick keeps the loop vouching for the mode; then the stream goes quiet for 40 s (more than
    # grid_power_max_age_s) and resumes: the silent 40 s are not export.
    clock.advance(20)
    store.apply(entity_state(GRID, -30, clock.now()))  # keeps the guard's grid reading fresh
    tick(actuator, ctx(clock, intent("discharge", 600)))
    assert actuator.desired.mode == "discharge"
    push(actuator, store, clock, -30, after=20)
    assert export_s(metrics) == 0.0
    # The chain restarts on that update: the next pair counts again.
    push(actuator, store, clock, -30)
    assert export_s(metrics) == pytest.approx(2.0)
    # A gap just under grid_power_max_age_s still counts.
    push(actuator, store, clock, -30, after=29)
    assert export_s(metrics) == pytest.approx(31.0)


def test_a_stale_update_breaks_the_export_chain():
    actuator, store, clock, metrics = discharging()
    push(actuator, store, clock, -30)
    clock.advance(2)
    stale = entity_state(GRID, -30, clock.now() - timedelta(seconds=45))  # a late snapshot
    actuator.observe_grid_power(stale, UpdateResult.ACCEPTED)
    push(actuator, store, clock, -30, after=1)
    assert export_s(metrics) == 0.0  # no fresh previous sample to integrate from
    assert count(metrics) == 2.0


def test_a_chain_does_not_span_a_pause_in_discharge():
    actuator, store, clock, metrics = discharging()
    push(actuator, store, clock, -30)
    tick(actuator, ctx(clock, intent("idle", 0)))  # the next slot is idle
    push(actuator, store, clock, -30, after=1)
    tick(actuator, ctx(clock, intent("discharge", 600)))
    push(actuator, store, clock, -30, after=1)
    assert export_s(metrics) == 0.0


def test_the_margin_gauge_reports_the_configured_value():
    settings = replace(SETTINGS, discharge_margin_w=100.0)
    actuator, *_ = discharging(settings=settings)
    family = next(
        f
        for f in _actuator_families(actuator.snapshot())
        if f.name == "battery_optimizer_actuator_discharge_margin_watts"
    )
    assert [s.value for s in family.samples] == [100.0]
