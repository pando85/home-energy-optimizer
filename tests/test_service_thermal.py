"""``service/thermal.py``: the derate curve, the per-slot prediction, the guarded refit (pure)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from battery_optimizer.service.thermal import (
    BOUNDS,
    DEFAULT_PARAMS,
    DerateSettings,
    ThermalParams,
    episodes,
    fit_tau_profile,
    free_run,
    predict_plan,
    predict_rows,
    refit,
    usable_spans,
)

STEP = 60.0
DAY = 1440
HS = tuple(round(h * 60 / STEP) for h in (10, 20, 30, 45, 60, 90))


def settings(**kw) -> DerateSettings:
    base = dict(
        switch=True,
        limit_c=40.0,
        band_c=10.0,
        floor_pct=40.0,
        discharge_switch=False,
        limit_kw=2.5,
        min_kw=0.8,
    )
    return DerateSettings.from_readings(**{**base, **kw})


# ---------------------------------------------------------------------------
# The derate curve and the owner's switch
# ---------------------------------------------------------------------------


def test_the_committed_calibration_is_the_default_and_in_bounds():
    # PR #91 printed the gains the wrong way round: charge is the larger one (see the page).
    assert DEFAULT_PARAMS == ThermalParams(tau_min=44.0, t_amb_c=32.6, k_c=11.0, k_d=6.9)
    assert ThermalParams.from_dict(DEFAULT_PARAMS.to_dict()) == DEFAULT_PARAMS
    for key, (lo, hi) in BOUNDS.items():
        assert lo <= getattr(DEFAULT_PARAMS, key) <= hi


@pytest.mark.parametrize(
    "bad",
    [
        {"tau_min": 10.0},  # below its bound
        {"k_c": 99.0},
        {"t_amb_c": math.nan},
        {"k_d": True},
        {"tau_min": "44"},
    ],
)
def test_parameters_outside_the_bounds_or_garbled_are_refused(bad):
    with pytest.raises(ValueError):
        ThermalParams.from_dict({**DEFAULT_PARAMS.to_dict(), **bad})
    with pytest.raises(ValueError):
        ThermalParams.from_dict({"tau_min": 44.0})


def test_the_curve_is_one_up_to_the_limit_then_linear_to_the_floor():
    d = settings()
    assert d.state == "on"
    assert d.factor(35.0) == 1.0 and d.factor(40.0) == 1.0
    assert d.factor(45.0) == pytest.approx(0.7)
    assert d.factor(50.0) == pytest.approx(0.4) and d.factor(70.0) == pytest.approx(0.4)
    assert d.cap_kw(45.0) == pytest.approx(2.5 * 0.7)
    assert d.cap_kw(50.0) == pytest.approx(1.0)  # 40% of 2.5 kW, above the 0.8 kW driver minimum


def test_the_cap_never_falls_below_the_driver_minimum():
    d = settings(floor_pct=10.0)
    assert d.cap_kw(60.0) == pytest.approx(0.8)  # 10% of 2.5 kW is 0.25 kW: the driver minimum
    small = settings(limit_kw=0.5)
    assert small.cap_kw(60.0) == pytest.approx(0.5)  # never above the limit itself


def test_a_switch_that_is_off_means_no_derate_in_the_model():
    d = settings(switch=False)
    assert d.state == "off"
    assert d.factor(55.0) == 1.0 and d.cap_kw(55.0) == 2.5
    assert not d.discharge_enabled


@pytest.mark.parametrize(
    "kw",
    [
        {"switch": None},
        {"limit_c": None},
        {"band_c": None},
        {"floor_pct": None},
        {"band_c": 0.0},
        {"band_c": -3.0},
        {"floor_pct": 120.0},
        {"floor_pct": -1.0},
        {"limit_c": 10.0},
        {"limit_c": 75.0},
    ],
)
def test_an_unknown_or_invalid_setting_leaves_the_model_blind(kw):
    d = settings(**kw)
    assert d.state == "unknown"
    assert d.factor(55.0) == 1.0 and d.cap_kw(55.0) == 2.5


def test_an_unavailable_temperature_means_no_derate_is_claimed():
    d = settings()
    assert d.factor(None) == 1.0 and d.factor(math.nan) == 1.0


def test_the_discharge_side_is_capped_only_with_its_own_switch():
    assert settings().cap_kw(50.0, "discharge") == 2.5
    both = settings(discharge_switch=True)
    assert both.discharge_enabled
    assert both.cap_kw(50.0, "discharge") == pytest.approx(1.0)
    assert settings(switch=False, discharge_switch=True).cap_kw(50.0, "discharge") == 2.5


# ---------------------------------------------------------------------------
# The per-slot prediction
# ---------------------------------------------------------------------------


def test_without_a_derate_the_prediction_is_the_exact_first_order_recursion():
    p = DEFAULT_PARAMS
    out = predict_plan(p, settings(switch=False), 30.0, [2.0, 2.0, 0.0], [0.0, 0.0, 0.0], 900.0)
    a = math.exp(-15.0 / p.tau_min)
    t1 = a * 30.0 + (1 - a) * (p.t_amb_c + p.k_c * 2.0)
    t2 = a * t1 + (1 - a) * (p.t_amb_c + p.k_c * 2.0)
    t3 = a * t2 + (1 - a) * p.t_amb_c
    assert out.temp_end_c == pytest.approx([t1, t2, t3], abs=1e-9)
    assert out.temp_start_c[0] == 30.0 and out.temp_start_c[1] == pytest.approx(t1)
    assert out.charge_kw == pytest.approx([2.0, 2.0, 0.0])  # nothing is cut
    assert not out.cold_start


def test_the_derate_cuts_the_achievable_charge_and_the_heating_follows():
    p = DEFAULT_PARAMS
    plan = [2.5] * 12  # three hours at the maximum request
    hot = predict_plan(p, settings(), 30.0, plan, [0.0] * 12, 900.0)
    blind = predict_plan(p, settings(switch=False), 30.0, plan, [0.0] * 12, 900.0)
    assert hot.charge_planned_kw == pytest.approx(plan)
    assert (hot.charge_kw <= hot.charge_planned_kw + 1e-9).all()
    assert hot.charge_kw[0] == pytest.approx(2.5, abs=0.2)  # cool at the start
    assert hot.charge_kw[-1] < 2.0  # the battery is hot by the end
    assert hot.derate_factor.min() < 1.0 and hot.derate_factor[0] > 0.95
    assert hot.temp_end_c[-1] < blind.temp_end_c[-1]  # less power, less heat
    assert hot.charge_kw.sum() < blind.charge_kw.sum()
    assert hot.charge_cap_kw.min() >= 0.8 - 1e-9
    assert hot.summary()["derated_slots"] >= 1
    assert hot.summary()["achievable_charge_kwh"] < hot.summary()["planned_charge_kwh"]


def test_the_cap_is_a_feedback_inside_a_slot_not_frozen_at_its_start():
    # One long slot that starts at the knee: a cap frozen at the start would be the full limit.
    out = predict_plan(DEFAULT_PARAMS, settings(), 40.0, [2.5], [0.0], 3 * 3600.0)
    assert out.charge_kw[0] < 2.2
    assert out.derate_factor[0] < 1.0


def test_an_unavailable_temperature_starts_from_the_ambient_and_is_flagged():
    out = predict_plan(DEFAULT_PARAMS, settings(), None, [0.0, 0.0], [0.0, 0.0], 900.0)
    assert out.cold_start
    assert out.temp_start_c[0] == DEFAULT_PARAMS.t_amb_c
    assert out.derate_factor.tolist() == [1.0, 1.0]


def test_the_discharge_derate_applies_only_when_its_switch_is_on():
    plan = [2.5] * 8
    off = predict_plan(DEFAULT_PARAMS, settings(), 38.0, [0.0] * 8, plan, 900.0)
    on = predict_plan(DEFAULT_PARAMS, settings(discharge_switch=True), 38.0, [0.0] * 8, plan, 900.0)
    assert off.discharge_kw == pytest.approx(plan)
    assert on.discharge_kw.min() < 2.5
    assert (on.discharge_kw <= on.discharge_planned_kw + 1e-9).all()


def test_the_columns_are_plain_json_numbers():
    import json

    out = predict_plan(DEFAULT_PARAMS, settings(), 33.0, [1.0, 0.0], [0.0, 0.5], 900.0)
    cols = out.columns()
    assert set(cols) >= {"temp_start_c", "temp_end_c", "charge_kw", "derate_factor"}
    json.dumps(cols, allow_nan=False)
    assert all(len(v) == 2 for v in cols.values())
    with pytest.raises(ValueError):
        predict_plan(DEFAULT_PARAMS, settings(), 33.0, [1.0], [0.0, 0.0], 900.0)


# ---------------------------------------------------------------------------
# The fit: power is positive while charging
# ---------------------------------------------------------------------------


def make_buffer(
    days: int,
    truth: ThermalParams,
    *,
    noise: float = 0.0,
    seed: int = 0,
    charge_kw: float = 2.0,
    discharge_kw: float = 1.3,
    gap: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """A buffer on the 60 s grid (+ = charging), a charge and a discharge every day."""
    rng = np.random.default_rng(seed)
    p = np.zeros(days * DAY)
    for d in range(days):
        p[d * DAY + 120 : d * DAY + 210] = charge_kw + 0.15 * (d % 3)
        p[d * DAY + 1000 : d * DAY + 1090] = -discharge_kw
    temp = free_run(
        truth.t_amb_c, p, truth.tau_min, truth.t_amb_c, truth.k_c, truth.k_d, step_s=STEP
    )[: p.size]
    temp = temp + rng.normal(0.0, noise, p.size)
    if gap:
        p[3 * DAY + 300 : 3 * DAY + 420] = math.nan
        temp[3 * DAY + 300 : 3 * DAY + 420] = math.nan
    return temp, p


TRUTH = ThermalParams(tau_min=52.0, t_amb_c=31.0, k_c=9.0, k_d=5.0)


def test_the_charge_gain_is_the_coefficient_of_the_power_above_zero():
    # The regression of PR 2: the study's regressors took the discharge power as the charge.
    temp, p = make_buffer(4, TRUTH)
    spans = [(d * DAY, (d + 1) * DAY) for d in range(4)]
    fit = fit_tau_profile(temp, p, np.arange(p.size) * STEP, spans, "split", HS, step_s=STEP)
    coef = dict(zip(fit["names"], fit["beta"], strict=True))
    assert coef["k_c"] == pytest.approx(9.0, rel=0.08)
    assert coef["k_d"] == pytest.approx(5.0, rel=0.08)
    assert coef["t_amb"] == pytest.approx(31.0, abs=0.3)
    assert fit["tau_min"] == pytest.approx(52.0, rel=0.15)


def test_a_fixed_coefficient_is_held_and_the_others_still_fit():
    temp, p = make_buffer(4, TRUTH)
    spans = [(d * DAY, (d + 1) * DAY) for d in range(4)]
    fit = fit_tau_profile(
        temp,
        p,
        np.arange(p.size) * STEP,
        spans,
        "split",
        HS,
        step_s=STEP,
        fixed={"k_d": 6.0},
    )
    coef = dict(zip(fit["names"], fit["beta"], strict=True))
    assert coef["k_d"] == 6.0
    assert coef["k_c"] == pytest.approx(9.0, rel=0.15)


def test_the_ridge_pulls_towards_the_prior_and_fades_with_the_rows():
    temp, p = make_buffer(4, TRUTH)
    grid = np.arange(p.size) * STEP
    spans = [(d * DAY, (d + 1) * DAY) for d in range(4)]
    prior = {"t_amb": 20.0, "k_c": 3.0, "k_d": 3.0}
    free = fit_tau_profile(temp, p, grid, spans, "split", HS, step_s=STEP)
    soft = fit_tau_profile(
        temp, p, grid, spans, "split", HS, step_s=STEP, prior=prior, ridge_rows=500.0
    )
    hard = fit_tau_profile(
        temp, p, grid, spans, "split", HS, step_s=STEP, prior=prior, ridge_rows=1e6
    )
    kc = {
        n: dict(zip(f["names"], f["beta"], strict=True))["k_c"]
        for n, f in (("free", free), ("soft", soft), ("hard", hard))
    }
    assert abs(kc["soft"] - kc["free"]) < 0.5  # many rows: the prior barely matters
    assert abs(kc["hard"] - 3.0) < abs(kc["soft"] - 3.0)  # a heavy prior wins
    assert abs(kc["hard"] - 3.0) < 1.0


def test_multi_step_predictions_of_the_true_parameters_are_exact():
    temp, p = make_buffer(2, TRUTH)
    fit = {"tau_min": 52.0, "beta": np.array([31.0, 9.0, 5.0])}
    pr = predict_rows(fit, temp, p, np.arange(p.size) * STEP, "split", (0, DAY), HS, step_s=STEP)
    assert pr["h"].size > 100
    assert np.abs(pr["pred"] - pr["obs"]).max() < 1e-6


def test_episodes_use_the_given_step():
    p = np.zeros(1000)
    p[100:150] = 1.0
    p[600:650] = -1.0
    assert episodes(p, step_s=60.0, gap_min=60.0) == [(0, 374), (374, 1000)]
    assert len(episodes(p, step_s=60.0, gap_min=300.0)) == 2  # 450 steps of 60 s = 450 min
    assert episodes(p, step_s=30.0, gap_min=300.0) == [(0, 1000)]  # 450 steps of 30 s = 225 min


# ---------------------------------------------------------------------------
# The guarded refit
# ---------------------------------------------------------------------------


def test_an_unknown_power_splits_the_buffer_and_is_never_read_as_idle():
    temp, p = make_buffer(6, TRUTH, gap=True)
    spans = usable_spans(temp, p, step_s=STEP)
    assert spans
    for lo, hi in spans:
        assert np.isfinite(p[lo:hi]).all()
    assert not any(lo < 3 * DAY + 420 and hi > 3 * DAY + 300 for lo, hi in spans)


def test_too_little_data_is_insufficient_not_a_verdict():
    temp, p = make_buffer(1, TRUTH)
    out = refit(temp[:300], p[:300], current=DEFAULT_PARAMS, prior=DEFAULT_PARAMS, step_s=STEP)
    assert out.result == "insufficient_data" and out.params is None


def test_a_flat_idle_buffer_has_no_thermal_excitation():
    temp = np.full(6 * DAY, 32.0)
    p = np.zeros(6 * DAY)
    p[200:230] = 0.2
    p[2000:2030] = 0.2
    p[4000:4030] = 0.2
    out = refit(temp, p, current=DEFAULT_PARAMS, prior=DEFAULT_PARAMS, step_s=STEP)
    assert out.result == "insufficient_data"


def test_a_better_fit_is_accepted_and_its_gains_are_the_data_s():
    temp, p = make_buffer(7, TRUTH, noise=0.05, seed=3)
    out = refit(temp, p, current=DEFAULT_PARAMS, prior=DEFAULT_PARAMS, step_s=STEP)
    assert out.result == "accepted", out.quality
    assert out.params is not None
    assert out.params.k_c == pytest.approx(9.0, rel=0.15)
    assert out.params.k_d == pytest.approx(5.0, rel=0.25)
    assert out.params.tau_min == pytest.approx(52.0, rel=0.25)
    q = out.quality
    assert q["rmse_60_candidate"] < q["rmse_60_current"] < q["rmse_60_persistence"]
    assert q["skill_vs_persistence"] >= 0.5
    assert q["gains_held"] == []  # both directions have data above 1 kW from 3+ episodes
    for key, (lo, hi) in BOUNDS.items():
        assert lo <= getattr(out.params, key) <= hi


def test_a_fit_that_does_not_beat_the_current_parameters_is_rejected():
    temp, p = make_buffer(7, TRUTH, noise=0.05, seed=3)
    out = refit(temp, p, current=TRUTH, prior=DEFAULT_PARAMS, step_s=STEP)
    assert out.result == "rejected"
    assert "current parameters" in out.reason
    assert out.params is not None  # the candidate is reported, never applied by the caller


def test_a_fit_that_cannot_beat_persistence_is_rejected():
    rng = np.random.default_rng(5)
    p = make_buffer(7, TRUTH)[1]
    temp = 32.0 + np.cumsum(rng.normal(0.0, 0.2, p.size))  # a walk the power does not explain
    out = refit(temp, p, current=DEFAULT_PARAMS, prior=DEFAULT_PARAMS, step_s=STEP)
    assert out.result == "rejected", out.quality
    assert "persistence" in out.reason
    assert out.quality["skill_vs_persistence"] < 0.5


def test_a_gain_without_data_above_one_kw_keeps_its_current_value():
    temp, p = make_buffer(7, TRUTH, noise=0.05, seed=3, discharge_kw=0.6)
    current = ThermalParams(tau_min=44.0, t_amb_c=32.6, k_c=11.0, k_d=6.9)
    out = refit(temp, p, current=current, prior=DEFAULT_PARAMS, step_s=STEP)
    assert out.quality["gains_held"] == ["k_d"]
    assert out.result in ("accepted", "rejected")
    assert out.params is not None and out.params.k_d == 6.9


def test_the_candidate_is_clipped_into_the_bounds():
    hot = ThermalParams(tau_min=60.0, t_amb_c=31.0, k_c=14.5, k_d=5.0)
    temp, p = make_buffer(7, hot, noise=0.02, seed=1, charge_kw=2.5)
    out = refit(temp, p, current=DEFAULT_PARAMS, prior=DEFAULT_PARAMS, step_s=STEP)
    assert out.params is not None
    assert out.params.k_c <= BOUNDS["k_c"][1]
