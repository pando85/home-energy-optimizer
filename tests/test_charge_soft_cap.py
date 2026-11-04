"""Thermal charge-power soft cap: model, composition, planner, config and replay wiring.

``oracle.build_model`` / ``solve_oracle`` take ``charge_soft_cap=(threshold_kw, eur_per_kwh)``:
one auxiliary column ``x[t] >= max(charge[t] - threshold_kw * h, 0)`` per slot, priced at
``cost`` per AC kWh.  ``None`` or a zero cost must leave the model byte for byte as it was.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
from scipy.optimize import milp
from service_fakes import NOW, feed, write_service_config
from test_full_charge_solver import (
    CAP,
    GOLDEN_INPUTS_HASH,
    GOLDEN_MODEL_DIGESTS,
    POWER,
    H,
    battery,
    fc_model,
    make_horizon,
    make_snapshot,
    make_spec,
    model_digest,
    solve_free,
)
from test_full_charge_solver import (
    TestGolden as _FullChargeGolden,
)
from test_rolling_horizon import MADRID, QUARTER, _make_dataset, _spec

from battery_optimizer import rolling_horizon as rh_mod
from battery_optimizer.battery import BatterySpec, Horizon
from battery_optimizer.benchmark import BenchmarkInputError, _oracle_kwargs
from battery_optimizer.oracle import (
    build_model,
    charge_soft_cap_excess_kwh,
    checkpoint_outcome,
    high_soc_dwell_kwh_h,
    solve_oracle,
    validate_charge_soft_cap,
)
from battery_optimizer.policies import DayAheadDay, policy_day_ahead
from battery_optimizer.rolling_horizon import build_frozen_load_profile, run_rolling_horizon
from battery_optimizer.service import planner as planner_mod
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import load_service_config
from battery_optimizer.service.planner import run_planner

THRESHOLD_KW = 1.5
THRESHOLD_KWH = THRESHOLD_KW * H


def short_valley(n: int = 96) -> tuple[np.ndarray, np.ndarray]:
    """A short very cheap valley (full power is optimal), a cheap shoulder, a priced peak."""
    t = np.arange(n)
    prices = np.where(t < 4, 0.02, np.where(t < 48, 0.05, 0.30))
    load = np.full(n, 0.3)
    return prices, load


def charge_kw(schedule) -> np.ndarray:
    return np.asarray(schedule.charge_kwh, dtype=float) / H


# ---------------------------------------------------------------------------
# Golden: no soft cap means today's model
# ---------------------------------------------------------------------------


class TestGoldenModel:
    @pytest.mark.parametrize("name", ["fixed", "free", "limit"])
    @pytest.mark.parametrize("cap", [None, (1.5, 0.0), (0.5, 0)])
    def test_model_is_byte_identical(self, name, cap):
        spec, hz, kwargs = _FullChargeGolden()._cases()[name]
        model = build_model(spec, hz, charge_soft_cap=cap, **kwargs)
        assert model_digest(model) == GOLDEN_MODEL_DIGESTS[name]

    def test_full_charge_and_dwell_models_are_byte_identical_too(self):
        spec, hz, kwargs = _FullChargeGolden()._cases()["free"]
        spec = replace(spec, soc_max_frac=1.0)
        fc = fc_model(hz.n, 40, 90, taper=(0.9, 0.2))
        for dwell in (None, (0.9, 0.01)):
            base = model_digest(
                build_model(spec, hz, full_charge=fc, high_soc_cost=dwell, **kwargs)
            )
            for cap in (None, (1.5, 0.0)):
                model = build_model(
                    spec, hz, full_charge=fc, high_soc_cost=dwell, charge_soft_cap=cap, **kwargs
                )
                assert model_digest(model) == base

    def test_zero_cost_solve_keeps_the_certified_bound(self):
        prices, load = short_valley()
        spec, hz = battery(0.3), make_horizon(prices, load)
        plain = solve_free(spec, hz)
        zero = solve_free(spec, hz, charge_soft_cap=(THRESHOLD_KW, 0.0))
        assert np.array_equal(plain.charge_kwh, zero.charge_kwh)
        assert zero.cost_lower_bound_eur == plain.cost_lower_bound_eur is not None
        assert zero.is_certified == plain.is_certified

    def test_planner_hash_and_plan_do_not_move_while_the_cost_is_zero(self):
        snapshot = make_snapshot()
        assert snapshot.inputs_hash() == GOLDEN_INPUTS_HASH
        assert "charge_soft_cap" not in snapshot.input_payload()
        assert replace(snapshot, charge_soft_cap_kw=1.0).inputs_hash() == GOLDEN_INPUTS_HASH


# ---------------------------------------------------------------------------
# The auxiliary rows
# ---------------------------------------------------------------------------


class TestAuxiliaryRows:
    def test_columns_and_rows_come_after_the_historical_ones(self):
        prices, load = short_valley()
        spec, hz = battery(0.3), make_horizon(prices, load)
        n = hz.n
        base = build_model(spec, hz, throughput_cost_eur_per_kwh=0.01)
        obj, integrality, bounds, (con,) = build_model(
            spec, hz, throughput_cost_eur_per_kwh=0.01, charge_soft_cap=(THRESHOLD_KW, 0.02)
        )
        assert len(obj) == len(base[0]) + n
        assert np.array_equal(obj[: 4 * n], base[0])
        assert np.array_equal(integrality[: 4 * n], base[1])
        assert np.all(integrality[4 * n :] == 0)
        base_a, ext_a = base[3][0].A.tocsr(), con.A.tocsr()
        assert ext_a.shape[0] == base_a.shape[0] + n
        assert np.array_equal(ext_a[: base_a.shape[0], : 4 * n].toarray(), base_a.toarray())
        new = ext_a[base_a.shape[0] :].toarray()
        # x[t] - charge[t] >= -threshold * h; charge[t] is column 3t.
        assert np.array_equal(new[:, 4 * n :], np.eye(n))
        assert np.array_equal(new[:, 0 : 3 * n : 3], -np.eye(n))
        assert np.allclose(con.lb[base_a.shape[0] :], -THRESHOLD_KWH)
        assert np.all(np.isinf(con.ub[base_a.shape[0] :]))
        # Priced per AC kWh above the threshold (not per hour), bounded by the slot limit.
        assert np.allclose(obj[4 * n :], 0.02)
        assert np.all(bounds.lb[4 * n :] == 0.0)
        assert np.allclose(bounds.ub[4 * n :], POWER * H - THRESHOLD_KWH)

    def test_the_cap_columns_come_after_the_dwell_columns(self):
        prices, load = short_valley()
        spec, hz = battery(0.3), make_horizon(prices, load)
        n = hz.n
        dwell_only = build_model(spec, hz, high_soc_cost=(0.9, 0.01))
        both = build_model(
            spec, hz, high_soc_cost=(0.9, 0.01), charge_soft_cap=(THRESHOLD_KW, 0.02)
        )
        assert len(both[0]) == len(dwell_only[0]) + n
        assert np.array_equal(both[0][: 5 * n], dwell_only[0])
        assert np.allclose(both[0][5 * n :], 0.02)
        a_dwell, a_both = dwell_only[3][0].A.tocsr(), both[3][0].A.tocsr()
        assert np.array_equal(a_both[: a_dwell.shape[0], : 5 * n].toarray(), a_dwell.toarray())

    def test_excess_equals_the_charge_above_the_threshold_at_the_optimum(self):
        prices, load = short_valley()
        spec, hz = battery(0.3), make_horizon(prices, load)
        n = hz.n
        obj, integrality, bounds, constraints = build_model(
            spec,
            hz,
            terminal_mode="free",
            terminal_value_eur_per_kwh=0.11,
            floor_kwh=spec.soc_bound_kwh("min"),
            charge_soft_cap=(THRESHOLD_KW, 0.005),
        )
        res = milp(obj, integrality=integrality, bounds=bounds, constraints=constraints)
        assert res.status == 0
        charge = res.x[0 : 3 * n : 3]
        excess = res.x[4 * n :]
        assert charge.max() > THRESHOLD_KWH + 1e-6, "scenario must charge above the threshold"
        assert np.allclose(excess, np.maximum(charge - THRESHOLD_KWH, 0.0), atol=1e-7)

    def test_a_high_price_removes_charge_above_the_threshold_and_a_tiny_one_changes_nothing(self):
        prices, load = short_valley()
        spec, hz = battery(0.3), make_horizon(prices, load)
        plain = solve_free(spec, hz)
        assert charge_kw(plain).max() == pytest.approx(POWER, abs=1e-6)
        tiny = solve_free(spec, hz, charge_soft_cap=(THRESHOLD_KW, 1e-9))
        assert np.allclose(tiny.charge_kwh, plain.charge_kwh, atol=1e-6)
        excess = [
            charge_soft_cap_excess_kwh(
                solve_free(spec, hz, charge_soft_cap=(THRESHOLD_KW, c)), 900, THRESHOLD_KW
            )
            for c in (0.0, 0.005, 0.05, 1.0)
        ]
        assert excess[0] > excess[1] >= excess[2] > excess[3] - 1e-9
        assert excess[3] == pytest.approx(0.0, abs=1e-6)
        capped = solve_free(spec, hz, charge_soft_cap=(THRESHOLD_KW, 1.0))
        assert charge_kw(capped).max() <= THRESHOLD_KW + 1e-6

    def test_the_cap_spreads_the_same_charge_instead_of_dropping_it(self):
        prices, load = short_valley()
        spec, hz = battery(0.3), make_horizon(prices, load)
        plain = solve_free(spec, hz)
        # 0.05 EUR/kWh exceeds the 0.03 valley-versus-shoulder price spread.
        capped = solve_free(spec, hz, charge_soft_cap=(THRESHOLD_KW, 0.05))
        # Same energy moved, over more charging slots, at a lower peak.
        assert capped.charge_kwh.sum() == pytest.approx(plain.charge_kwh.sum(), rel=1e-3)
        assert (capped.charge_kwh > 1e-6).sum() > (plain.charge_kwh > 1e-6).sum()
        assert charge_kw(capped).max() < charge_kw(plain).max()

    def test_the_reported_cost_excludes_the_cap_cost_and_the_dual_bound_is_dropped(self):
        prices, load = short_valley()
        spec, hz = battery(0.3), make_horizon(prices, load)
        s = solve_free(spec, hz, charge_soft_cap=(THRESHOLD_KW, 0.02))
        assert s.cost_eur == pytest.approx(s.meter_cost_eur + s.inventory_adjustment_eur)
        plain = solve_free(spec, hz)
        assert s.cost_eur >= plain.cost_eur - 1e-9
        assert s.cost_lower_bound_eur is None
        assert s.cost_upper_bound_eur == pytest.approx(s.cost_eur)
        assert (s.solver_status, s.bound_kind, s.is_certified) == ("feasible", "upper", False)

    def test_bad_values_are_rejected(self):
        spec, hz = battery(0.3), make_horizon(*short_valley())
        for bad in [
            (1.5,),
            "ab",
            (True, 0.01),
            (1.5, True),
            (0.0, 0.01),
            (-1.0, 0.01),
            (float("nan"), 0.01),
            (float("inf"), 0.01),
            (1.5, -0.01),
            (1.5, float("inf")),
        ]:
            with pytest.raises(ValueError, match="charge_soft_cap"):
                build_model(spec, hz, charge_soft_cap=bad)
        assert validate_charge_soft_cap(None) is None
        assert validate_charge_soft_cap((1.5, 0)) is None
        assert validate_charge_soft_cap((1.5, 0.01)) == (1.5, 0.01)

    def test_a_threshold_at_or_above_the_power_limit_is_inert(self):
        prices, load = short_valley()
        spec, hz = battery(0.3), make_horizon(prices, load)
        plain = solve_free(spec, hz)
        inert = solve_free(spec, hz, charge_soft_cap=(POWER, 0.5))
        assert np.allclose(inert.charge_kwh, plain.charge_kwh, atol=1e-6)
        inert = solve_free(spec, hz, charge_soft_cap=(10.0, 0.5))
        assert np.allclose(inert.charge_kwh, plain.charge_kwh, atol=1e-6)


# ---------------------------------------------------------------------------
# Composition with the full-charge day and the dwell cost
# ---------------------------------------------------------------------------


class TestComposition:
    def _setup(self):
        prices, load = short_valley()
        spec = replace(battery(0.3), soc_max_frac=1.0)
        hz = make_horizon(prices, load)
        return spec, hz, fc_model(hz.n, 0, hz.n)

    def test_checkpoint_columns_keep_their_index_and_the_cap_columns_come_last(self):
        spec, hz, fc = self._setup()
        n = hz.n
        k = len(fc.checkpoint_slots)
        plain = build_model(spec, hz, full_charge=fc)
        both = build_model(spec, hz, full_charge=fc, charge_soft_cap=(THRESHOLD_KW, 0.02))
        assert len(both[0]) == len(plain[0]) + n
        assert np.array_equal(both[0][: 4 * n + k + 1], plain[0])
        assert both[0][4 * n + k] == fc.shortfall_penalty_eur_per_kwh
        assert np.array_equal(both[1][: 4 * n + k + 1], plain[1])
        assert np.array_equal(both[2].ub[: 4 * n + k + 1], plain[2].ub)

    def test_the_target_is_still_reached_with_the_cap_and_with_the_dwell_cost(self):
        spec, hz, fc = self._setup()
        capped = solve_free(spec, hz, full_charge=fc, charge_soft_cap=(THRESHOLD_KW, 0.05))
        both = solve_free(
            spec,
            hz,
            full_charge=fc,
            charge_soft_cap=(THRESHOLD_KW, 0.05),
            high_soc_cost=(0.9, 0.02),
        )
        for s in (capped, both):
            assert checkpoint_outcome(s, fc)[1] == pytest.approx(0.0, abs=1e-6)
            assert charge_kw(s).max() <= POWER + 1e-6
            assert s.cost_lower_bound_eur is None
        # The dwell cost still moves the climb later, the cap still lowers the peak.
        assert checkpoint_outcome(both, fc)[0] >= checkpoint_outcome(capped, fc)[0]
        assert high_soc_dwell_kwh_h(both, spec, 900, 0.9) <= 1e-6 + high_soc_dwell_kwh_h(
            capped, spec, 900, 0.9
        )
        assert charge_kw(both).max() < POWER - 0.5
        plain = solve_free(spec, hz, full_charge=fc)
        assert charge_kw(capped).max() < charge_kw(plain).max()

    def test_the_shortfall_penalty_dominates_the_cap_cost(self):
        spec, hz, fc = self._setup()
        eta_c = spec.charge_efficiency
        limit = fc.shortfall_penalty_eur_per_kwh * eta_c
        s = solve_free(spec, hz, full_charge=fc, charge_soft_cap=(THRESHOLD_KW, 0.99 * limit))
        assert checkpoint_outcome(s, fc)[1] == pytest.approx(0.0, abs=1e-6)
        with pytest.raises(ValueError, match="soft cap"):
            build_model(spec, hz, full_charge=fc, charge_soft_cap=(THRESHOLD_KW, 1.01 * limit))

    def test_the_dominance_check_adds_the_dwell_and_the_cap_prices(self):
        spec, hz, fc = self._setup()
        horizon_hours = hz.n * H
        dwell = 0.5 * fc.shortfall_penalty_eur_per_kwh / horizon_hours  # half of the penalty
        limit = fc.shortfall_penalty_eur_per_kwh * spec.charge_efficiency
        # Each alone is dominated, together they are not.
        build_model(spec, hz, full_charge=fc, high_soc_cost=(0.9, dwell))
        build_model(spec, hz, full_charge=fc, charge_soft_cap=(THRESHOLD_KW, 0.6 * limit))
        with pytest.raises(ValueError, match="soft cap"):
            build_model(
                spec,
                hz,
                full_charge=fc,
                high_soc_cost=(0.9, dwell),
                charge_soft_cap=(THRESHOLD_KW, 0.6 * limit),
            )

    def test_a_model_without_a_checkpoint_has_no_penalty_to_dominate(self):
        spec, hz, _ = self._setup()
        no_checkpoint = fc_model(hz.n, 0, hz.n, checkpoint=False)
        s = solve_free(spec, hz, full_charge=no_checkpoint, charge_soft_cap=(THRESHOLD_KW, 100.0))
        assert charge_kw(s).max() <= THRESHOLD_KW + 1e-6


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class TestPlanner:
    def _snapshot(self, cost, **kwargs):
        prices, load = short_valley()
        return replace(
            make_snapshot(prices=prices, soc=0.3, **kwargs),
            load_kwh=tuple(float(v) for v in load),
            spec=battery(0.3),
            charge_soft_cap_kw=THRESHOLD_KW,
            charge_soft_cap_eur_per_kwh=cost,
        )

    def test_an_active_cap_changes_the_inputs_hash_and_the_plan(self):
        off, on = self._snapshot(0.0), self._snapshot(0.02)
        assert "charge_soft_cap" not in off.input_payload()
        assert off.inputs_hash() == replace(off, charge_soft_cap_kw=1.0).inputs_hash()
        assert on.inputs_hash() != off.inputs_hash()
        assert on.input_payload()["charge_soft_cap"] == {
            "threshold_kw": THRESHOLD_KW,
            "eur_per_kwh": 0.02,
        }
        peak_off = max(i.signed_power_w for i in run_planner(off).intents)
        peak_on = max(i.signed_power_w for i in run_planner(self._snapshot(1.0)).intents)
        assert peak_off > 1000 * THRESHOLD_KW + 100
        assert peak_on <= 1000 * THRESHOLD_KW + 1e-6

    def test_the_cap_and_the_dwell_cost_are_independent_inputs(self):
        both = replace(
            self._snapshot(0.02), high_soc_threshold_frac=0.9, high_soc_cost_eur_per_kwh_h=0.01
        )
        payload = both.input_payload()
        assert "charge_soft_cap" in payload and "high_soc_cost" in payload

    def test_every_solve_of_a_full_charge_evaluation_carries_the_cap(self, monkeypatch):
        seen = []
        real = planner_mod.solve_oracle

        def spy(*args, **kwargs):
            seen.append(kwargs.get("charge_soft_cap"))
            return real(*args, **kwargs)

        monkeypatch.setattr(planner_mod, "solve_oracle", spy)
        for cost, expected in ((0.0, None), (0.02, (THRESHOLD_KW, 0.02))):
            seen.clear()
            snapshot = self._snapshot(cost, full_charge=make_spec("evaluate", 24, 90))
            result = run_planner(snapshot)
            assert result.full_charge is not None and "full_charge_error" not in result.solver
            assert len(seen) == 3
            assert all(call == expected for call in seen)

    def test_commit_mode_plans_the_target_with_the_cap(self):
        snapshot = self._snapshot(
            0.02, full_charge=make_spec("commit", 0, 90, taper_start_soc_frac=None)
        )
        result = run_planner(snapshot)
        assert "full_charge_error" not in result.solver
        assert result.full_charge is not None and result.full_charge.shortfall_kwh == pytest.approx(
            0.0, abs=1e-6
        )


# ---------------------------------------------------------------------------
# Service config and runtime
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_are_off(self, tmp_path):
        config = load_service_config(write_service_config(tmp_path))
        assert config.planning.charge_soft_cap_eur_per_kwh == 0.0
        assert config.planning.charge_soft_cap_kw == 1.5

    def test_parameters_are_read_and_change_the_hash_only_when_active(self, tmp_path):
        base = load_service_config(write_service_config(tmp_path))
        moved = load_service_config(
            write_service_config(tmp_path, **{"planning.charge_soft_cap_kw": 2.0})
        )
        active = load_service_config(
            write_service_config(tmp_path, **{"planning.charge_soft_cap_eur_per_kwh": 0.01})
        )
        assert moved.planning.charge_soft_cap_kw == 2.0
        assert active.planning.charge_soft_cap_eur_per_kwh == 0.01
        assert moved.config_hash() == base.config_hash()
        assert active.config_hash() != base.config_hash()

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"planning.charge_soft_cap_eur_per_kwh": -0.01}, "charge_soft_cap_eur_per_kwh"),
            ({"planning.charge_soft_cap_eur_per_kwh": True}, "charge_soft_cap_eur_per_kwh"),
            ({"planning.charge_soft_cap_eur_per_kwh": "x"}, "charge_soft_cap_eur_per_kwh"),
            ({"planning.charge_soft_cap_kw": 0}, "charge_soft_cap_kw"),
            ({"planning.charge_soft_cap_kw": -1.5}, "charge_soft_cap_kw"),
            ({"planning.charge_soft_cap_kw": False}, "charge_soft_cap_kw"),
        ],
    )
    def test_invalid_values_are_refused(self, tmp_path, override, message):
        with pytest.raises(ValueError, match=message):
            load_service_config(write_service_config(tmp_path, **override))

    def test_the_full_charge_penalty_must_dominate_the_cap_cost(self, tmp_path):
        live = {
            "mode": "live",
            "actuation.battery_ac_power_sign": 1,
            "planning.soc_source": "entity",
            "entities.battery_soc": "sensor.test_soc",
            "full_charge.enabled": True,
        }
        # Checked with an efficiency floor of 0.5: 4.9 / 0.5 = 9.8 < 10 passes, 5.1 / 0.5 fails.
        try:
            load_service_config(
                write_service_config(
                    tmp_path, **{**live, "planning.charge_soft_cap_eur_per_kwh": 4.9}
                )
            )
        except ValueError as exc:  # unrelated live-mode requirements are not under test
            assert "shortfall_penalty" not in str(exc)
        with pytest.raises(ValueError, match="charge_soft_cap_eur_per_kwh per stored"):
            load_service_config(
                write_service_config(
                    tmp_path, **{**live, "planning.charge_soft_cap_eur_per_kwh": 5.1}
                )
            )


def test_the_configured_cap_reaches_the_plan_snapshot(tmp_path):
    from test_service_runtime import make_service, run

    clock = FakeClock(NOW)
    service = make_service(
        tmp_path,
        clock,
        entity_mode=True,
        **{"planning.charge_soft_cap_eur_per_kwh": 0.01, "planning.charge_soft_cap_kw": 1.8},
    )
    try:
        feed(service, NOW)
        assert run(service.request_solve("test")) == "ok"
        snapshot = service.plans[-1].snapshot
        assert (snapshot.charge_soft_cap_kw, snapshot.charge_soft_cap_eur_per_kwh) == (1.8, 0.01)
        assert snapshot.input_payload()["charge_soft_cap"]["eur_per_kwh"] == 0.01
    finally:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Benchmark and replay path
# ---------------------------------------------------------------------------


class TestReplayPath:
    def test_oracle_kwargs_pass_the_cap_only_when_given(self):
        kwargs, _ = _oracle_kwargs(solve_oracle, "free", 5.0, 1e-4, 1e-6)
        assert "charge_soft_cap" not in kwargs
        kwargs, _ = _oracle_kwargs(
            solve_oracle, "free", 5.0, 1e-4, 1e-6, charge_soft_cap=(1.5, 0.01)
        )
        assert kwargs["charge_soft_cap"] == (1.5, 0.01)

    def test_day_ahead_applies_the_cap_and_records_it_only_when_active(self):
        n_days, per_day = 3, 96
        n = n_days * per_day
        hour = np.tile(np.arange(per_day) // 4, n_days)
        prices = np.where(hour < 1, 0.02, np.where(hour < 6, 0.05, 0.30))
        load = np.full(n, 0.3)
        spec = BatterySpec(CAP, POWER, 0.9, 0.9, 0.2, 0.95, 0.3, None)
        hz = Horizon(900, prices, load, np.ones(n, dtype=bool), None)
        days = [
            DayAheadDay(
                label=f"d{i}",
                start=i * per_day,
                stop=(i + 1) * per_day,
                forecast_load=load[:per_day],
            )
            for i in range(n_days)
        ]
        common = dict(
            terminal_value_eur_per_kwh=0.11, time_limit_s=10.0, mip_rel_gap=1e-6, mip_abs_gap=1e-9
        )
        plain, tele_plain = policy_day_ahead(spec, hz, days, **common)
        capped, tele = policy_day_ahead(spec, hz, days, charge_soft_cap=(1.5, 0.05), **common)
        assert "charge_soft_cap" not in tele_plain
        assert tele["charge_soft_cap"]["eur_per_kwh"] == 0.05
        assert charge_soft_cap_excess_kwh(capped, 900, 1.5) < charge_soft_cap_excess_kwh(
            plain, 900, 1.5
        )
        with pytest.raises(ValueError, match="charge_soft_cap"):
            policy_day_ahead(spec, hz, days, charge_soft_cap=(1.5, -1.0), **common)

    def test_rolling_horizon_passes_the_cap_to_every_solve(self, monkeypatch):
        seen = []
        real = rh_mod.solve_oracle

        def spy(*args, **kwargs):
            seen.append(kwargs.get("charge_soft_cap"))
            return real(*args, **kwargs)

        monkeypatch.setattr(rh_mod, "solve_oracle", spy)
        start = datetime(2025, 6, 14, 22, 0, tzinfo=UTC)
        prices, load, usable, local_days, starts = _make_dataset(start, 4, base_load=0.2)
        frozen, _ = build_frozen_load_profile(
            load,
            local_days,
            usable,
            training_end_idx=96,
            window_days=28,
            interval_starts_utc=starts,
        )
        common = dict(
            prices=prices,
            load_kwh=load,
            usable=usable,
            interval_seconds=QUARTER,
            local_days=local_days,
            interval_starts_utc=starts,
            eval_start_idx=2 * 96 + 84,
            eval_end_idx=len(starts),
            tz=MADRID,
            frozen_load=frozen,
            terminal_value_eur_per_kwh=0.10,
            sigma=0.1,
            seed=3,
        )
        plain = run_rolling_horizon(_spec(), **common)
        assert seen and all(c is None for c in seen)
        assert "charge_soft_cap" not in plain.telemetry
        seen.clear()
        result = run_rolling_horizon(_spec(), charge_soft_cap=(1.5, 0.02), **common)
        assert seen and all(c == (1.5, 0.02) for c in seen)
        assert result.telemetry["charge_soft_cap"] == {"threshold_kw": 1.5, "eur_per_kwh": 0.02}
        with pytest.raises(ValueError, match="charge_soft_cap"):
            run_rolling_horizon(_spec(), charge_soft_cap=(0.0, 0.02), **common)

    def test_benchmark_refuses_a_bad_cap_before_doing_anything(self, tmp_path):
        from battery_optimizer.benchmark import benchmark_dataset

        with pytest.raises(BenchmarkInputError, match="charge_soft_cap"):
            benchmark_dataset(
                dataset=tmp_path / "missing",
                config_path=tmp_path / "missing.yaml",
                train_end="2025-01-01T00:00:00+00:00",
                charge_soft_cap=(1.5, -1.0),
            )


def test_json_roundtrip_of_the_plan_record_with_an_active_cap():
    prices, load = short_valley()
    snapshot = replace(
        make_snapshot(prices=prices, soc=0.3),
        charge_soft_cap_kw=THRESHOLD_KW,
        charge_soft_cap_eur_per_kwh=0.02,
    )
    record = run_planner(snapshot).to_record()
    assert json.loads(json.dumps(record))["inputs"]["charge_soft_cap"]["eur_per_kwh"] == 0.02
