"""``hack/price_info_replay.py`` and ``hack/price_estimate_accuracy.py``: the pure parts, on
synthetic data (no Prometheus, no store)."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

HACK = Path(__file__).resolve().parents[1] / "hack"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HACK / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


replay = _load("price_info_replay")
accuracy = _load("price_estimate_accuracy")


def test_parse_instant_date_is_local_midnight_and_offsets_are_kept():
    assert replay.parse_instant("2026-11-04") == datetime(2026, 11, 3, 23, tzinfo=UTC)
    assert replay.parse_instant("2026-08-01") == datetime(2026, 7, 31, 22, tzinfo=UTC)
    assert replay.parse_instant("2026-11-04T00:00:00+00:00") == datetime(2026, 11, 4, tzinfo=UTC)
    with pytest.raises(argparse.ArgumentTypeError):
        replay.parse_instant("2026-11-04T00:00:00")


def test_fill_fills_the_start_of_every_gap_up_to_the_limit_and_never_the_ends():
    nan = np.nan
    values = np.array([nan, 1.0, nan, nan, 4.0, nan, nan, nan, nan, 9.0, nan])
    forward = replay._fill(values, 2, linear=False)
    assert np.isnan(forward[0]) and np.isnan(forward[-1])
    assert forward[2] == 1.0 and forward[3] == 1.0
    assert forward[5] == 4.0 and forward[6] == 4.0 and np.isnan(forward[7]) and np.isnan(forward[8])
    linear = replay._fill(values, 2, linear=True)
    assert linear[2] == pytest.approx(2.0) and linear[3] == pytest.approx(3.0)


def test_lf0_is_the_trailing_28_day_mean_of_the_same_slot_and_needs_14_days():
    days = 45
    ldate = np.repeat(np.arange(days) + 739000, replay.SLOTS_PER_DAY)
    lslot = np.tile(np.arange(replay.SLOTS_PER_DAY), days)
    load = np.repeat(np.arange(days, dtype=float), replay.SLOTS_PER_DAY) + lslot * 0.01
    forecast = replay.lf0_forecast(load, ldate, lslot)
    assert np.isnan(forecast[: 14 * replay.SLOTS_PER_DAY]).all()
    assert np.isfinite(forecast[14 * replay.SLOTS_PER_DAY :]).all()
    day = 40
    expected = np.arange(day - 28, day).mean() + 5 * 0.01
    assert forecast[day * replay.SLOTS_PER_DAY + 5] == pytest.approx(expected)


def _settings(**kw):
    base = dict(
        cap=5.0,
        pmax=2.5,
        eta=0.9,
        smin=1.0,
        smax=4.75,
        limit=5.0,
        s0=2.0,
        wear=0.01,
        lf_ceil_w=2500.0,
        lf_tol=0.005,
        lf_floor_hyst=0.1,
        max_horizon=192,
        margin_base_w=100.0,
    )
    base.update(kw)
    return replay.Settings(**base)


def test_lp_charges_before_a_price_peak_and_not_before_a_flat_price():
    cfg = _settings()
    load = np.full(4, 1.0)
    margin = np.zeros(4)
    charge, discharge, _ = replay.solve(
        cfg, np.array([0.05, 0.05, 0.40, 0.40]), load, margin, 1.0, 0.0
    )
    assert charge > 0.1 and discharge == 0.0
    charge, discharge, _ = replay.solve(cfg, np.full(4, 0.20), load, margin, 1.0, 0.0)
    assert charge == pytest.approx(0.0, abs=1e-9)


def _frame(days: int = 60):
    first = date(2026, 1, 5)  # a Monday
    start = replay.local_midnight(first)
    starts = [start + timedelta(seconds=900 * i) for i in range(days * replay.SLOTS_PER_DAY)]
    local = [s.astimezone(replay.TZ) for s in starts]
    price = np.full(len(starts), 0.20)
    for i, t in enumerate(local):
        if t.weekday() == 5:
            price[i] = 0.01 if 12 <= t.hour < 14 else 0.10
    load = np.full(len(starts), 0.2)
    margin = np.full(len(starts), 50.0)
    return replay.Data(starts, price, load, np.full(len(starts), np.nan), None, margin)


def test_refill_terminal_value_reads_the_night_not_the_weekend_midday_trough():
    cfg = _settings()
    d = _frame()
    saturday = next(
        i
        for i, t in enumerate(d.ldates)
        if t.weekday() == 5 and t > date(2026, 2, 14) and d.lmin[i] == 10 * 60
    )
    night = replay.tv_array(cfg, d, "refill")[saturday]
    p3 = replay.tv_array(cfg, d, "refill_p3")[saturday]
    mean = replay.tv_array(cfg, d, "trailing_mean")[saturday]
    assert night == pytest.approx((0.10 + 0.01) / 0.9)
    assert p3 == pytest.approx((0.01 + 0.01) / 0.9)
    assert night <= mean
    assert replay.tv_array(cfg, d, "const:0.15")[saturday] == 0.15
    with pytest.raises(SystemExit):
        replay.parse_rule("refill_night")


def test_bootstrap_needs_enough_weekly_blocks_and_brackets_a_constant_gain():
    stats = replay.Stats.__new__(replay.Stats)
    first = date(2026, 1, 5).toordinal()
    stats.ud = np.arange(first, first + 14)
    assert stats.bootstrap(np.ones(14))[0] is None
    stats.ud = np.arange(first, first + 70)
    interval, blocks = stats.bootstrap(np.full(70, 0.01))
    assert blocks == 10
    assert interval == pytest.approx((0.70, 0.70))


def test_window_arguments_use_an_exclusive_end():
    window = accuracy.parse_window("Oct-Nov:2026-10-01:2026-11-18")
    assert window.first == date(2026, 10, 1) and window.last == date(2026, 11, 17)
    with pytest.raises(argparse.ArgumentTypeError):
        accuracy.parse_window("x:2026-10-01:2026-10-01")


def test_day_scores_of_a_perfect_estimate_and_of_a_level_offset():
    real = np.linspace(0.05, 0.40, 24)
    scores = accuracy.day_stats(real, real.copy())
    assert scores["sp"] == pytest.approx(1.0)
    assert scores["c4"] == scores["e4"] == 1.0 and scores["c4_reg"] == 0.0
    assert scores["spr_err"] == pytest.approx(0.0)
    rows = {
        "real": np.concatenate([real, real]),
        "pred": np.concatenate([real + 0.03, real - 0.01]),
        "target": np.repeat([1, 2], 24),
    }
    assert accuracy.level_offset_share(rows) == pytest.approx(1.0)
    metrics, days = accuracy.summarize(
        {**rows, "hour": np.tile(np.arange(24), 2), "per": np.ones(48)}
    )
    assert metrics["MAE"] == pytest.approx(0.02) and len(days) == 2
    assert days[0]["sp"] == pytest.approx(1.0)
