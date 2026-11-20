"""The spike guard of the live room temperatures (climate/room_guard.py)."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from battery_optimizer.climate import room_guard
from battery_optimizer.climate.room_guard import SpikeRule, despike
from battery_optimizer.features import RawSeries

MIN = 60 * 1_000_000
T0 = int(datetime(2026, 11, 19, 6, 0, tzinfo=UTC).timestamp() * 1e6)
RULE = SpikeRule()


def minutes(values: list[float], *, step: int = 1) -> tuple[np.ndarray, np.ndarray]:
    return T0 + np.arange(len(values), dtype=np.int64) * step * MIN, np.array(values, float)


def ramp(a: float, b: float, n: int) -> list[float]:
    return list(np.linspace(a, b, n + 1)[1:])


def test_the_cooking_spike_of_2026_11_19_is_replaced_by_the_level_before_it():
    # 24.3 for an hour, +5.2 K in 24 minutes, back to 24.7 within 70 minutes (the real shape)
    values = [24.3] * 60 + ramp(24.3, 29.5, 24) + ramp(29.5, 24.7, 70) + [24.8] * 30
    t, v = minutes(values)
    out, flagged = despike(t, v, RULE)
    assert flagged.any()
    peak = 60 + 24
    assert out[60:peak].max() <= 24.3 + RULE.release_k  # nothing above the base plus the release
    assert (out[flagged] == 24.3).all()
    # the part of the rise below the release and the decay tail below it are the raw readings
    assert np.array_equal(out[~flagged], v[~flagged])
    assert out.max() <= 24.3 + RULE.release_k
    assert (t == minutes(values)[0]).all()  # every timestamp kept: no gap for the slot builder


def test_normal_heating_rises_and_slow_drifts_are_untouched():
    # 0.9 K in 30 minutes (the largest rise of a comfort room in four weeks), then 1 K an hour
    values = [22.0] * 40 + ramp(22.0, 22.9, 30) + ramp(22.9, 25.0, 120) + [25.0] * 30
    t, v = minutes(values)
    out, flagged = despike(t, v, RULE)
    assert not flagged.any() and np.array_equal(out, v)


def test_the_recovery_after_a_window_was_open_is_not_a_spike():
    # bedroom 2026-10-26: 23 -> 19.5 in 20 minutes, hours of 20, then back to 22.9 in 85 minutes
    values = [23.0] * 30 + ramp(23.0, 19.5, 20) + [20.0] * 180 + ramp(20.0, 22.9, 60) + [23.0] * 30
    t, v = minutes(values)
    out, flagged = despike(t, v, RULE)
    assert not flagged.any() and np.array_equal(out, v)


def test_a_step_that_lasts_is_believed_after_the_hold():
    values = [22.0] * 40 + [26.0] * 300  # a thermostat moved into the sun: a lasting step
    t, v = minutes(values)
    out, flagged = despike(t, v, RULE)
    first = np.flatnonzero(flagged)[0]
    last = np.flatnonzero(flagged)[-1]
    assert t[last] - t[first] <= RULE.max_hold_minutes * MIN
    assert (out[last + 1 :] == 26.0).all()  # past the hold the raw reading is used again
    assert (out[first : last + 1] == 22.0).all()


def test_two_spikes_in_a_morning_are_both_replaced():
    one = ramp(24.0, 28.0, 15) + ramp(28.0, 24.0, 40)
    values = [24.0] * 30 + one + [24.0] * 120 + one + [24.0] * 30
    t, v = minutes(values)
    out, flagged = despike(t, v, RULE)
    assert out.max() <= 24.0 + RULE.release_k
    assert flagged.sum() > 20


def test_sparse_samples_and_a_disabled_guard():
    t, v = minutes([22.0, 22.0, 26.0, 26.5, 26.5, 22.1], step=10)  # one reading every 10 minutes
    out, flagged = despike(t, v, RULE)
    assert flagged.tolist() == [False, False, True, True, True, False]
    assert out.tolist() == [22.0, 22.0, 22.0, 22.0, 22.0, 22.1]
    off, none = despike(t, v, SpikeRule(enabled=False))
    assert np.array_equal(off, v) and not none.any()
    one, flag = despike(t[:1], v[:1], RULE)
    assert one.tolist() == [22.0] and not flag.any()


def test_a_cooker_beside_one_thermostat_does_not_change_the_other_series():
    t, v = minutes([24.3] * 60 + ramp(24.3, 29.5, 24) + [24.4] * 80)
    hall = RawSeries("climate.hall", "m", "", t, v)
    bed = RawSeries("climate.bed", "m", "", t, np.full(t.size, 23.0))
    other = RawSeries("sensor.x", "m", "", t, np.full(t.size, 7.0))
    series = {"hall": hall, "bed": bed, "x": other}
    sources = {"hall_kitchen": "hall", "bedroom": "bed", "office_t": "missing"}
    out, flags = room_guard.guard_series(series, sources, RULE)
    assert out["bed"] is bed and out["x"] is other  # untouched series are the same objects
    assert out["hall"].values.max() <= 24.3 + RULE.release_k
    assert np.array_equal(hall.values, v)  # the input is never modified
    assert set(flags) == {"hall_kitchen", "bedroom"}  # a room without a series is skipped
    at = datetime.fromtimestamp(t[-1] / 1e6, UTC)
    audit = room_guard.audit(series, sources, RULE, out, flags, at)
    assert audit["enabled"] and audit["max_rise_k"] == 2.0 and audit["window_minutes"] == 30
    h = audit["rooms"]["hall_kitchen"]
    assert h["replaced_24h"] > 0 and h["active"] is False
    assert h["raw_last"] == 24.4 and h["used_last"] == 24.4
    assert h["max_raw_minus_used_24h"] > 3.0
    assert audit["rooms"]["bedroom"] == {
        "replaced_24h": 0,
        "active": False,
        "raw_last": 23.0,
        "used_last": 23.0,
        "max_raw_minus_used_24h": 0.0,
    }


def test_a_spike_in_progress_at_the_decision_is_flagged_active():
    t, v = minutes([24.3] * 60 + ramp(24.3, 29.5, 20) + [29.5] * 3)
    series = {"hall": RawSeries("c", "m", "", t, v)}
    out, flags = room_guard.guard_series(series, {"hall_kitchen": "hall"}, RULE)
    at = datetime.fromtimestamp(t[-1] / 1e6, UTC)
    audit = room_guard.audit(series, {"hall_kitchen": "hall"}, RULE, out, flags, at)
    assert audit["rooms"]["hall_kitchen"]["active"] is True
    assert audit["rooms"]["hall_kitchen"]["used_last"] == 24.3
    assert audit["rooms"]["hall_kitchen"]["raw_last"] == 29.5
