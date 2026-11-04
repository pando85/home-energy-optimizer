"""High-SOC dwell cost: model, composition with the full-charge day, planner and config wiring.

``oracle.build_model`` / ``solve_oracle`` take ``high_soc_cost=(threshold_frac, eur_per_kwh_h)``:
one auxiliary column ``e[t] >= max(soc[t] - threshold * capacity, 0)`` per slot, priced at
``cost * h``.  ``None`` or a zero cost must leave the model byte for byte as it was.
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
    checkpoint_outcome,
    high_soc_dwell_kwh_h,
    solve_oracle,
    validate_high_soc_cost,
)
from battery_optimizer.policies import DayAheadDay, policy_day_ahead
from battery_optimizer.rolling_horizon import build_frozen_load_profile, run_rolling_horizon
from battery_optimizer.service import planner as planner_mod
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import load_service_config
from battery_optimizer.service.planner import run_planner

THRESHOLD = 0.9
THRESHOLD_KWH = THRESHOLD * CAP


def evening_peak(n: int = 96) -> tuple[np.ndarray, np.ndarray]:
    """Flat cheap prices (a hair cheaper early) and a priced evening peak."""
    t = np.arange(n)
    prices = np.where(t < 72, 0.05 + 1e-5 * t, 0.30)
    load = np.full(n, 0.3)
    return prices, load


def soc_of(schedule) -> np.ndarray:
    return np.asarray(schedule.soc_kwh, dtype=float)


# ---------------------------------------------------------------------------
# Golden: no dwell cost means today's model
# ---------------------------------------------------------------------------


class TestGoldenModel:
    @pytest.mark.parametrize("name", ["fixed", "free", "limit"])
    @pytest.mark.parametrize("dwell", [None, (0.9, 0.0), (0.5, 0)])
    def test_model_is_byte_identical(self, name, dwell):
        spec, hz, kwargs = _FullChargeGolden()._cases()[name]
        model = build_model(spec, hz, high_soc_cost=dwell, **kwargs)
        assert model_digest(model) == GOLDEN_MODEL_DIGESTS[name]

    def test_full_charge_model_is_byte_identical_too(self):
        spec, hz, kwargs = _FullChargeGolden()._cases()["free"]
        spec = replace(spec, soc_max_frac=1.0)
        fc = fc_model(hz.n, 40, 90, taper=(0.9, 0.2))
        base = build_model(spec, hz, full_charge=fc, **kwargs)
        assert model_digest(
            build_model(spec, hz, full_charge=fc, high_soc_cost=None, **kwargs)
        ) == (model_digest(base))
        assert model_digest(
            build_model(spec, hz, full_charge=fc, high_soc_cost=(0.9, 0.0), **kwargs)
        ) == model_digest(base)

    def test_zero_cost_solve_keeps_the_certified_bound(self):
        prices, load = evening_peak()
        spec, hz = battery(0.5), make_horizon(prices, load)
        plain = solve_free(spec, hz)
        zero = solve_free(spec, hz, high_soc_cost=(0.9, 0.0))
        assert np.array_equal(plain.charge_kwh, zero.charge_kwh)
        assert zero.cost_lower_bound_eur == plain.cost_lower_bound_eur is not None
        assert zero.is_certified == plain.is_certified

    def test_planner_hash_and_plan_do_not_move_while_the_cost_is_zero(self):
        snapshot = make_snapshot()
        assert snapshot.inputs_hash() == GOLDEN_INPUTS_HASH
        assert "high_soc_cost" not in snapshot.input_payload()
        # The threshold alone changes nothing.
        assert replace(snapshot, high_soc_threshold_frac=0.8).inputs_hash() == GOLDEN_INPUTS_HASH


# ---------------------------------------------------------------------------
# The auxiliary rows
# ---------------------------------------------------------------------------


class TestAuxiliaryRows:
    def test_columns_and_rows_come_after_the_historical_ones(self):
        prices, load = evening_peak()
        spec, hz = battery(0.5), make_horizon(prices, load)
        n = hz.n
        base = build_model(spec, hz, throughput_cost_eur_per_kwh=0.01)
        ext = build_model(
            spec, hz, throughput_cost_eur_per_kwh=0.01, high_soc_cost=(THRESHOLD, 0.02)
        )
        obj, integrality, bounds, (con,) = ext
        assert len(obj) == len(base[0]) + n
        assert np.array_equal(obj[: 4 * n], base[0])
        assert np.array_equal(integrality[: 4 * n], base[1])
        assert np.all(integrality[4 * n :] == 0)
        # One row per slot, e[t] - soc[t] >= -threshold * capacity, nothing else touched.
        base_a, ext_a = base[3][0].A.tocsr(), con.A.tocsr()
        assert ext_a.shape[0] == base_a.shape[0] + n
        assert np.array_equal(ext_a[: base_a.shape[0], : 4 * n].toarray(), base_a.toarray())
        new = ext_a[base_a.shape[0] :].toarray()
        assert np.array_equal(new[:, 3 * n : 4 * n], -np.eye(n))
        assert np.array_equal(new[:, 4 * n :], np.eye(n))
        assert np.allclose(con.lb[base_a.shape[0] :], -THRESHOLD_KWH)
        assert np.all(np.isinf(con.ub[base_a.shape[0] :]))
        # Priced at cost * h per kWh of excess, free to be zero.
        assert np.allclose(obj[4 * n :], 0.02 * H)
        assert np.all(bounds.lb[4 * n :] == 0.0)

    def test_excess_equals_the_soc_above_the_threshold_at_the_optimum(self):
        prices, load = evening_peak()
        spec, hz = battery(0.5), make_horizon(prices, load)
        n = hz.n
        obj, integrality, bounds, constraints = build_model(
            spec,
            hz,
            terminal_mode="free",
            terminal_value_eur_per_kwh=0.11,
            floor_kwh=spec.soc_bound_kwh("min"),
            high_soc_cost=(THRESHOLD, 0.02),
        )
        res = milp(obj, integrality=integrality, bounds=bounds, constraints=constraints)
        assert res.status == 0
        soc, excess = res.x[3 * n : 4 * n], res.x[4 * n :]
        assert soc.max() > THRESHOLD_KWH - 1e-6, "scenario must reach the threshold region"
        assert np.allclose(excess, np.maximum(soc - THRESHOLD_KWH, 0.0), atol=1e-7)

    def test_a_high_price_removes_time_above_the_threshold_and_a_tiny_one_changes_nothing(self):
        prices, load = evening_peak()
        spec, hz = battery(0.5), make_horizon(prices, load)
        plain = solve_free(spec, hz)
        tiny = solve_free(spec, hz, high_soc_cost=(THRESHOLD, 1e-9))
        assert np.allclose(soc_of(tiny), soc_of(plain), atol=1e-6)
        dwell = [
            high_soc_dwell_kwh_h(solve_free(spec, hz, high_soc_cost=(THRESHOLD, c)), spec, 900, 0.9)
            for c in (0.0, 0.002, 0.02, 1.0)
        ]
        assert dwell[0] > dwell[1] >= dwell[2] > dwell[3] - 1e-9
        assert dwell[3] == pytest.approx(0.0, abs=1e-6)
        capped = solve_free(spec, hz, high_soc_cost=(THRESHOLD, 1.0))
        assert soc_of(capped).max() <= THRESHOLD_KWH + 1e-6

    def test_the_reported_cost_excludes_the_dwell_cost_and_the_dual_bound_is_dropped(self):
        prices, load = evening_peak()
        spec, hz = battery(0.5), make_horizon(prices, load)
        s = solve_free(spec, hz, high_soc_cost=(THRESHOLD, 0.02))
        assert s.cost_eur == pytest.approx(s.meter_cost_eur + s.inventory_adjustment_eur)
        dwell_cost = 0.02 * high_soc_dwell_kwh_h(s, spec, 900, THRESHOLD)
        assert dwell_cost > 0.0
        # The solver minimised the penalised objective: re-pricing the same flows without it
        # reproduces the reported cost.
        plain = solve_free(spec, hz)
        assert s.cost_eur >= plain.cost_eur - 1e-9
        assert s.cost_lower_bound_eur is None
        assert s.cost_upper_bound_eur == pytest.approx(s.cost_eur)
        assert (s.solver_status, s.bound_kind, s.is_certified) == ("feasible", "upper", False)

    def test_bad_values_are_rejected(self):
        spec, hz = battery(0.5), make_horizon(*evening_peak())
        for bad in [
            (0.9,),
            "ab",
            (True, 0.01),
            (0.9, True),
            (0.0, 0.01),
            (1.0, 0.01),
            (1.2, 0.01),
            (float("nan"), 0.01),
            (0.9, -0.01),
            (0.9, float("inf")),
        ]:
            with pytest.raises(ValueError, match="high_soc_cost"):
                build_model(spec, hz, high_soc_cost=bad)
        assert validate_high_soc_cost(None) is None
        assert validate_high_soc_cost((0.9, 0)) is None
        assert validate_high_soc_cost((0.9, 0.01)) == (0.9, 0.01)

    def test_a_threshold_at_or_above_the_ceiling_is_inert(self):
        prices, load = evening_peak()
        spec, hz = battery(0.5, soc_max=0.85), make_horizon(prices, load)
        plain = solve_free(spec, hz)
        inert = solve_free(spec, hz, high_soc_cost=(THRESHOLD, 0.5))
        assert np.allclose(soc_of(inert), soc_of(plain), atol=1e-6)


# ---------------------------------------------------------------------------
# Composition with the full-charge day
# ---------------------------------------------------------------------------


class TestWithFullCharge:
    def _setup(self):
        prices, load = evening_peak()
        spec = replace(battery(0.3), soc_max_frac=1.0)
        hz = make_horizon(prices, load)
        return spec, hz, fc_model(hz.n, 0, hz.n)

    def test_checkpoint_columns_keep_their_index_and_the_dwell_columns_come_last(self):
        spec, hz, fc = self._setup()
        n = hz.n
        k = len(fc.checkpoint_slots)
        plain = build_model(spec, hz, full_charge=fc)
        both = build_model(spec, hz, full_charge=fc, high_soc_cost=(THRESHOLD, 0.02))
        assert len(both[0]) == len(plain[0]) + n
        assert np.array_equal(both[0][: 4 * n + k + 1], plain[0])
        assert both[0][4 * n + k] == fc.shortfall_penalty_eur_per_kwh
        assert np.array_equal(both[1][: 4 * n + k + 1], plain[1])
        assert np.array_equal(both[2].ub[: 4 * n + k + 1], plain[2].ub)

    def test_the_target_is_still_reached_and_the_plan_climbs_later(self):
        spec, hz, fc = self._setup()
        free = solve_free(spec, hz, full_charge=fc)
        dwelled = solve_free(spec, hz, full_charge=fc, high_soc_cost=(THRESHOLD, 0.02))
        for s in (free, dwelled):
            slot, shortfall = checkpoint_outcome(s, fc)
            assert shortfall == pytest.approx(0.0, abs=1e-6)
        slot_free, _ = checkpoint_outcome(free, fc)
        slot_dwell, _ = checkpoint_outcome(dwelled, fc)
        assert slot_dwell > slot_free
        assert slot_dwell >= 60  # finishes just before the evening discharge at slot 72
        assert high_soc_dwell_kwh_h(dwelled, spec, 900, THRESHOLD) < 0.5 * high_soc_dwell_kwh_h(
            free, spec, 900, THRESHOLD
        )
        # The bound belongs to the penalised objective, so it is dropped.
        assert dwelled.cost_lower_bound_eur is None

    def test_the_shortfall_penalty_dominates_the_dwell_cost(self):
        spec, hz, fc = self._setup()
        horizon_hours = hz.n * H
        cost = (
            0.9 * fc.shortfall_penalty_eur_per_kwh / horizon_hours
        )  # dwell over the horizon < penalty
        s = solve_free(spec, hz, full_charge=fc, high_soc_cost=(THRESHOLD, cost))
        assert checkpoint_outcome(s, fc)[1] == pytest.approx(0.0, abs=1e-6)
        with pytest.raises(ValueError, match="must exceed the high-SOC dwell cost"):
            build_model(
                spec,
                hz,
                full_charge=fc,
                high_soc_cost=(THRESHOLD, 1.1 * fc.shortfall_penalty_eur_per_kwh / horizon_hours),
            )

    def test_a_lifted_ceiling_only_model_has_no_penalty_to_dominate(self):
        spec, hz, fc = self._setup()
        no_checkpoint = fc_model(hz.n, 0, hz.n, checkpoint=False)
        s = solve_free(spec, hz, full_charge=no_checkpoint, high_soc_cost=(THRESHOLD, 5.0))
        assert soc_of(s).max() <= THRESHOLD_KWH + 1e-6


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class TestPlanner:
    def _snapshot(self, cost, **kwargs):
        prices, load = evening_peak()
        return replace(
            make_snapshot(prices=prices, soc=0.3, **kwargs),
            load_kwh=tuple(float(v) for v in load),
            spec=battery(0.3),
            high_soc_threshold_frac=THRESHOLD,
            high_soc_cost_eur_per_kwh_h=cost,
        )

    def test_an_active_cost_changes_the_inputs_hash_and_the_plan(self):
        off, on = self._snapshot(0.0), self._snapshot(0.02)
        assert "high_soc_cost" not in off.input_payload()
        assert off.inputs_hash() == replace(off, high_soc_threshold_frac=0.5).inputs_hash()
        assert on.inputs_hash() != off.inputs_hash()
        assert on.input_payload()["high_soc_cost"] == {
            "threshold_frac": THRESHOLD,
            "eur_per_kwh_h": 0.02,
        }
        # soc_max is 0.95 here, so the plain plan climbs above the threshold and the dwell
        # cost keeps its intents at or below it.
        peak_off = max(i.soc_target_frac for i in run_planner(off).intents)
        peak_on = max(i.soc_target_frac for i in run_planner(self._snapshot(1.0)).intents)
        assert peak_off > THRESHOLD + 0.01
        assert peak_on <= THRESHOLD + 1e-6

    def test_every_solve_of_a_full_charge_evaluation_carries_the_dwell_cost(self, monkeypatch):
        seen = []
        real = planner_mod.solve_oracle

        def spy(*args, **kwargs):
            seen.append(kwargs.get("high_soc_cost"))
            return real(*args, **kwargs)

        monkeypatch.setattr(planner_mod, "solve_oracle", spy)
        for cost, expected in ((0.0, None), (0.02, (THRESHOLD, 0.02))):
            seen.clear()
            snapshot = self._snapshot(cost, full_charge=make_spec("evaluate", 24, 90))
            result = run_planner(snapshot)
            assert result.full_charge is not None and "full_charge_error" not in result.solver
            assert len(seen) == 3
            assert all(call == expected for call in seen)

    def test_commit_mode_plans_the_target_with_the_dwell_cost(self):
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
        assert config.planning.high_soc_cost_eur_per_kwh_h == 0.0
        assert config.planning.high_soc_threshold_frac == 0.90

    def test_parameters_are_read_and_change_the_hash_only_when_active(self, tmp_path):
        base = load_service_config(write_service_config(tmp_path))
        moved = load_service_config(
            write_service_config(tmp_path, **{"planning.high_soc_threshold_frac": 0.85})
        )
        active = load_service_config(
            write_service_config(tmp_path, **{"planning.high_soc_cost_eur_per_kwh_h": 0.01})
        )
        assert moved.planning.high_soc_threshold_frac == 0.85
        assert active.planning.high_soc_cost_eur_per_kwh_h == 0.01
        assert moved.config_hash() == base.config_hash()
        assert active.config_hash() != base.config_hash()

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"planning.high_soc_cost_eur_per_kwh_h": -0.01}, "high_soc_cost_eur_per_kwh_h"),
            ({"planning.high_soc_cost_eur_per_kwh_h": True}, "high_soc_cost_eur_per_kwh_h"),
            ({"planning.high_soc_cost_eur_per_kwh_h": "x"}, "high_soc_cost_eur_per_kwh_h"),
            ({"planning.high_soc_threshold_frac": 1.0}, "below 1"),
            ({"planning.high_soc_threshold_frac": 0}, "high_soc_threshold_frac"),
            ({"planning.high_soc_threshold_frac": False}, "high_soc_threshold_frac"),
        ],
    )
    def test_invalid_values_are_refused(self, tmp_path, override, message):
        with pytest.raises(ValueError, match=message):
            load_service_config(write_service_config(tmp_path, **override))

    def test_the_full_charge_penalty_must_dominate_the_dwell_cost(self, tmp_path):
        live = {
            "mode": "live",
            "actuation.battery_ac_power_sign": 1,
            "planning.soc_source": "entity",
            "entities.battery_soc": "sensor.test_soc",
            "full_charge.enabled": True,
            "planning.max_horizon_slots": 192,
        }
        # 192 slots of 900 s = 48 h: a cost of 0.2 EUR/kWh/h is 9.6 EUR/kWh, below the penalty 10.
        ok = {**live, "planning.high_soc_cost_eur_per_kwh_h": 0.2}
        try:
            load_service_config(write_service_config(tmp_path, **ok))
        except ValueError as exc:  # unrelated live-mode requirements are not under test
            assert "shortfall_penalty" not in str(exc)
        with pytest.raises(ValueError, match="shortfall_penalty_eur_per_kwh must exceed"):
            load_service_config(
                write_service_config(
                    tmp_path, **{**live, "planning.high_soc_cost_eur_per_kwh_h": 0.21}
                )
            )


def test_the_configured_dwell_cost_reaches_the_plan_snapshot(tmp_path):
    from test_service_runtime import make_service, run

    clock = FakeClock(NOW)
    service = make_service(
        tmp_path,
        clock,
        entity_mode=True,
        **{"planning.high_soc_cost_eur_per_kwh_h": 0.01, "planning.high_soc_threshold_frac": 0.92},
    )
    try:
        feed(service, NOW)
        assert run(service.request_solve("test")) == "ok"
        snapshot = service.plans[-1].snapshot
        assert (snapshot.high_soc_threshold_frac, snapshot.high_soc_cost_eur_per_kwh_h) == (
            0.92,
            0.01,
        )
        assert snapshot.input_payload()["high_soc_cost"]["eur_per_kwh_h"] == 0.01
    finally:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Benchmark and replay path
# ---------------------------------------------------------------------------


class TestReplayPath:
    def test_oracle_kwargs_pass_the_cost_only_when_given(self):
        kwargs, _ = _oracle_kwargs(solve_oracle, "free", 5.0, 1e-4, 1e-6)
        assert "high_soc_cost" not in kwargs
        kwargs, _ = _oracle_kwargs(solve_oracle, "free", 5.0, 1e-4, 1e-6, high_soc_cost=(0.9, 0.01))
        assert kwargs["high_soc_cost"] == (0.9, 0.01)

    def test_day_ahead_applies_the_cost_and_records_it_only_when_active(self):
        n_days, per_day = 3, 96
        n = n_days * per_day
        hour = np.tile(np.arange(per_day) // 4, n_days)
        prices = np.where(hour < 6, 0.05, 0.30)
        load = np.full(n, 0.3)
        spec = BatterySpec(CAP, 2.4, 0.9, 0.9, 0.2, 0.95, 0.3, None)
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
        dwell, tele = policy_day_ahead(spec, hz, days, high_soc_cost=(0.9, 0.02), **common)
        assert "high_soc_cost" not in tele_plain
        assert tele["high_soc_cost"]["eur_per_kwh_h"] == 0.02
        assert high_soc_dwell_kwh_h(dwell, spec, 900, 0.9) < high_soc_dwell_kwh_h(
            plain, spec, 900, 0.9
        )
        with pytest.raises(ValueError, match="high_soc_cost"):
            policy_day_ahead(spec, hz, days, high_soc_cost=(0.9, -1.0), **common)

    def test_rolling_horizon_passes_the_cost_to_every_solve(self, monkeypatch):
        seen = []
        real = rh_mod.solve_oracle

        def spy(*args, **kwargs):
            seen.append(kwargs.get("high_soc_cost"))
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
        assert "high_soc_cost" not in plain.telemetry
        seen.clear()
        result = run_rolling_horizon(_spec(), high_soc_cost=(0.9, 0.02), **common)
        assert seen and all(c == (0.9, 0.02) for c in seen)
        assert result.telemetry["high_soc_cost"] == {"threshold_frac": 0.9, "eur_per_kwh_h": 0.02}
        with pytest.raises(ValueError, match="high_soc_cost"):
            run_rolling_horizon(_spec(), high_soc_cost=(1.5, 0.02), **common)

    def test_benchmark_refuses_a_bad_cost_before_doing_anything(self, tmp_path):
        from battery_optimizer.benchmark import benchmark_dataset

        with pytest.raises(BenchmarkInputError, match="high_soc_cost"):
            benchmark_dataset(
                dataset=tmp_path / "missing",
                config_path=tmp_path / "missing.yaml",
                train_end="2025-01-01T00:00:00+00:00",
                high_soc_cost=(0.9, -1.0),
            )


def test_json_roundtrip_of_the_plan_record_with_an_active_cost():
    prices, load = evening_peak()
    snapshot = replace(
        make_snapshot(prices=prices, soc=0.3),
        high_soc_threshold_frac=THRESHOLD,
        high_soc_cost_eur_per_kwh_h=0.02,
    )
    record = run_planner(snapshot).to_record()
    assert json.loads(json.dumps(record))["inputs"]["high_soc_cost"]["eur_per_kwh_h"] == 0.02
