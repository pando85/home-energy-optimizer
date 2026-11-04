"""``hack/battery_thermal_derate.py``: the derate curve and the multi-step thermal fit on
synthetic data with known parameters."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "hack" / "battery_thermal_derate.py"
spec = importlib.util.spec_from_file_location("battery_thermal_derate", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HS = tuple(round(h * 60 / mod.STEP_S) for h in mod.HORIZONS_MIN)


def test_derate_factor_follows_the_documented_curve():
    f = mod.derate_factor
    assert f(35.0, 40.0, 10.0, 40.0) == 1.0
    assert f(40.0, 40.0, 10.0, 40.0) == 1.0
    assert f(45.0, 40.0, 10.0, 40.0) == pytest.approx(0.7)
    assert f(50.0, 40.0, 10.0, 40.0) == pytest.approx(0.4)
    assert f(60.0, 40.0, 10.0, 40.0) == pytest.approx(0.4)
    assert f(49.3, 40.0, 10.0, 40.0) == pytest.approx(1.0 - 0.6 * 0.93)
    assert f(math.nan, 40.0, 10.0, 40.0) == 1.0  # no temperature: no derate
    assert f(41.0, 40.0, 0.0, 40.0) == pytest.approx(0.4)  # a zero band is a step


def test_derated_cap_never_below_the_driver_minimum():
    cap = mod.derated_cap_w
    assert cap(30.0, 2500.0, 40.0, 10.0, 40.0) == 2500.0
    assert cap(50.0, 2500.0, 40.0, 10.0, 40.0) == pytest.approx(1000.0)
    assert cap(50.0, 2500.0, 40.0, 10.0, 40.0, min_power_w=1200.0) == pytest.approx(1200.0)
    assert cap(50.0, 500.0, 40.0, 10.0, 40.0, min_power_w=800.0) == pytest.approx(500.0)


def test_equilibrium_is_a_fixed_point_of_heating_and_derate():
    p, t = mod.equilibrium_charge_kw(32.5, 7.4, 2.5, 40.0, 10.0, 40.0)
    assert t == pytest.approx(32.5 + 7.4 * p)
    assert p == pytest.approx(2.5 * mod.derate_factor(t, 40.0, 10.0, 40.0), rel=1e-6)
    # a cool battery is never derated: the equilibrium is the limit itself
    p0, _ = mod.equilibrium_charge_kw(20.0, 2.0, 2.5, 40.0, 10.0, 40.0)
    assert p0 == pytest.approx(2.5, rel=1e-6)


def test_despike_drops_glitches_and_keeps_steps():
    v = np.array([30.0, 30.1, 30.2, 34.9, 30.3, 30.4, 30.5, 31.0, 31.6, 32.1, 32.7])
    out = mod.despike(v)
    assert np.isnan(out[3])
    assert np.isnan(out).sum() == 1
    ramp = np.linspace(30.0, 50.0, 40)  # a real 0.5 degC / sample ramp is not a spike
    assert not np.isnan(mod.despike(ramp)).any()


def test_zoh_holds_and_expires():
    t = np.array([0.0, 100.0, 400.0])
    v = np.array([1.0, 2.0, math.nan])
    grid = np.array([-10.0, 0.0, 99.0, 100.0, 399.0, 400.0, 500.0])
    assert np.allclose(
        mod.zoh(t, v, grid, None), [math.nan, 1, 1, 2, 2, math.nan, math.nan], equal_nan=True
    )
    aged = mod.zoh(t, v, grid, 150.0)
    assert np.isnan(aged[4])  # the last event is 299 s old


def test_episodes_split_at_the_midpoint_of_long_idle_gaps():
    p = np.zeros(1000)
    p[100:150] = 1.0
    p[600:650] = -1.0
    spans = mod.episodes(p, step_s=30, gap_min=60.0)  # a 60 min gap is 120 steps
    assert spans == [(0, 374), (374, 1000)]
    assert mod.episodes(np.zeros(10)) == [(0, 10)]


def _synthetic(tau=45.0, t_amb=32.0, k_c=8.0, k_d=10.0, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    n = 3 * 24 * 120  # three days of 30 s
    p = np.zeros(n)
    day = 24 * 120
    for d in range(3):
        p[d * day + 800 : d * day + 1300] = 1.0 + 0.5 * d  # charge (positive = charging)
        p[d * day + 1900 : d * day + 2300] = -0.4  # discharge
    temp = mod.free_run(t_amb, p, tau, t_amb, k_c, k_d)[:n]
    temp = temp + rng.normal(0.0, noise, n)
    grid = np.arange(n) * 30.0
    return temp, p, grid


def test_fit_recovers_known_parameters():
    temp, p, grid = _synthetic()
    spans = [(0, 2880), (2880, 5760), (5760, 8640)]
    fit = mod.fit_tau_profile(temp, p, grid, spans, "split", HS)
    c = dict(zip(fit["names"], fit["beta"], strict=True))
    assert fit["tau_min"] == pytest.approx(45.0, rel=0.1)
    assert c["t_amb"] == pytest.approx(32.0, abs=0.3)
    assert c["k_c"] == pytest.approx(8.0, rel=0.05)
    assert c["k_d"] == pytest.approx(10.0, rel=0.05)
    assert fit["rmse"] < 0.2


def test_leave_one_episode_out_beats_persistence_on_clean_data():
    temp, p, grid = _synthetic(noise=0.05, seed=1)
    spans = [(0, 2880), (2880, 5760), (5760, 8640)]
    out = mod.validate(temp, p, grid, spans, "split")
    pooled = out["loeo_pooled"]["pooled"]
    assert pooled["rmse"] < 0.3 * pooled["rmse_persistence"]
    assert out["loeo_pooled"]["pooled_above_1kw"]["bias"] == pytest.approx(0.0, abs=0.3)
    assert abs(out["fit_all"]["coef"]["k_c"] - 8.0) < 0.6


def test_multistep_prediction_matches_the_free_run():
    temp, p, grid = _synthetic()
    fit = {"tau_min": 45.0, "beta": np.array([32.0, 8.0, 10.0])}
    pr = mod.predict_rows(fit, temp, p, grid, "split", (0, 2880), HS)
    assert pr["h"].size > 100
    assert np.abs(pr["pred"] - pr["obs"]).max() < 1e-6


def test_derate_check_compares_the_cap_with_the_written_power():
    grid = np.arange(0.0, 1800.0, 30.0)
    temp = np.full(grid.size, 50.0)
    ev = {
        "sw": (np.array([100.0]), np.array([1.0])),
        "limit_c": (np.array([0.0]), np.array([40.0])),
        "band_c": (np.array([0.0]), np.array([10.0])),
        "floor_pct": (np.array([0.0]), np.array([40.0])),
        "sys_max_charge": (np.array([0.0]), np.array([2500.0])),
        "venus_max_charge": (np.array([0.0]), np.array([2500.0])),
        "set_charge": (np.array([0.0, 130.0]), np.array([2000.0, 1000.0])),
        "power": (np.array([0.0, 130.0]), np.array([-2000.0, -990.0])),
    }
    out = mod.derate_check(ev, grid, temp)
    assert out["predicted_cap_w_range"] == [1000.0, 1000.0]
    assert out["share_set_within_30w_of_cap"] == 1.0
    assert out["first_set_after_on"]["value_w"] == 1000.0
    ev["sw"] = (np.array([100.0]), np.array([0.0]))
    assert "never on" in mod.derate_check(ev, grid, temp)["status"]
