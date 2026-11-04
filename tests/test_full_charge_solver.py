"""Solver and planner tests for the periodic full charge (PR 1 of the plan).

Covers ``oracle.FullChargeModel`` (per-slot ceiling, checkpoint binaries, soft shortfall,
taper rows) and ``service.planner`` (``FullChargeSpec``, evaluate and commit modes,
``clamp_soc``).  The no-argument path must stay byte-identical to the historical model.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.optimize import milp

from battery_optimizer.battery import (
    BatteryPhysicsError,
    BatterySpec,
    Horizon,
    check_feasible,
)
from battery_optimizer.config import BatteryConfig
from battery_optimizer.oracle import (
    FullChargeModel,
    OracleInfeasibleError,
    build_model,
    checkpoint_outcome,
    solve_oracle,
)
from battery_optimizer.service import planner as planner_mod
from battery_optimizer.service.planner import (
    FullChargeEvaluation,
    FullChargeSpec,
    PlanningSnapshot,
    clamp_soc,
    evaluate_full_charge,
    run_planner,
)

CAP = 5.12
POWER = 2.4
H = 0.25  # hours per 15-minute slot
T0 = datetime(2026, 11, 4, 20, 30, tzinfo=UTC)

# Digests of build_model output on the unmodified code (main at 569e186): the model with no
# full-charge argument must not move by a single byte.
GOLDEN_MODEL_DIGESTS = {
    "fixed": "4c4ea31408ddc6c3fef62a02078483711e65f555adbbad64c141e7339378914a",
    "free": "c5bc5af37e359c5ca6af344dbf0780b379fb9e8e9c063843433d49ad1d4779f6",
    "limit": "15bee17e175737ad1bbb2fd8be9b7e6a4d43cfc66455da76557dd761a7da6aea",
}
GOLDEN_INPUTS_HASH = "29feba3d3f6119b21409bd5b71c2693b6553e7bd92edee3abd8dc8a7ae7d7814"


def model_digest(model) -> str:
    obj, integrality, bounds, constraints = model
    h = hashlib.sha256()
    for arr in (obj, integrality, bounds.lb, bounds.ub):
        h.update(np.ascontiguousarray(arr, dtype=np.float64).tobytes())
    for con in constraints:
        a = con.A.tocsr()
        a.sort_indices()
        for arr in (
            a.indptr.astype(np.int64),
            a.indices.astype(np.int64),
            a.data.astype(np.float64),
            np.asarray(con.lb, dtype=np.float64),
            np.asarray(con.ub, dtype=np.float64),
        ):
            h.update(np.ascontiguousarray(arr).tobytes())
        h.update(repr(a.shape).encode())
    return h.hexdigest()


def day_arrays(n: int = 96) -> tuple[np.ndarray, np.ndarray]:
    t = np.arange(n)
    prices = 0.12 + 0.05 * np.sin(2 * np.pi * t / 96) + 0.01 * (t % 7)
    load = 0.05 + 0.03 * np.cos(2 * np.pi * t / 48) ** 2
    return prices, load


def battery(soc: float = 0.5, soc_max: float = 0.95) -> BatterySpec:
    return BatterySpec(CAP, POWER, 0.9, 0.9, 0.2, soc_max, soc, None)


def make_horizon(prices, load, import_limit_kw=None) -> Horizon:
    n = len(prices)
    return Horizon(900, prices, load, np.ones(n, dtype=bool), import_limit_kw)


def fc_model(
    n: int,
    window_start: int,
    checkpoint_end: int,
    *,
    ceiling_before: float = 0.95,
    penalty: float = 10.0,
    taper: tuple[float, float] | None = None,
    checkpoint: bool = True,
) -> FullChargeModel:
    ceiling = tuple(CAP * (ceiling_before if t < window_start else 1.0) for t in range(n))
    return FullChargeModel(
        ceiling_kwh=ceiling,
        checkpoint_slots=tuple(range(window_start, checkpoint_end)) if checkpoint else (),
        target_kwh=CAP,
        shortfall_penalty_eur_per_kwh=penalty,
        taper_start_soc_frac=None if taper is None else taper[0],
        taper_power_kw=0.0 if taper is None else taper[1],
        taper_first_slot=window_start,
    )


def solve_free(spec, horizon, **kwargs):
    return solve_oracle(
        spec,
        horizon,
        terminal_mode="free",
        terminal_value_eur_per_kwh=0.11,
        floor_kwh=spec.soc_bound_kwh("min"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Golden: no full-charge argument means today's model
# ---------------------------------------------------------------------------


class TestGolden:
    def _cases(self):
        prices, load = day_arrays()
        spec = battery(0.5)
        hz = make_horizon(prices, load)
        hz_limit = make_horizon(prices, load, import_limit_kw=3.0)
        free = dict(
            terminal_mode="free",
            terminal_value_eur_per_kwh=0.11,
            floor_kwh=spec.soc_bound_kwh("min"),
        )
        return {
            "fixed": (spec, hz, {}),
            "free": (spec, hz, {**free, "throughput_cost_eur_per_kwh": 0.01}),
            "limit": (
                spec,
                hz_limit,
                {"terminal_mode": "free", "terminal_value_eur_per_kwh": 0.11},
            ),
        }

    @pytest.mark.parametrize("name", ["fixed", "free", "limit"])
    def test_model_is_byte_identical(self, name):
        spec, hz, kwargs = self._cases()[name]
        assert model_digest(build_model(spec, hz, **kwargs)) == GOLDEN_MODEL_DIGESTS[name]
        assert (
            model_digest(build_model(spec, hz, full_charge=None, **kwargs))
            == GOLDEN_MODEL_DIGESTS[name]
        )

    def test_extension_adds_columns_and_rows_after_the_historical_ones(self):
        spec, hz, kwargs = self._cases()["free"]
        spec = replace(spec, soc_max_frac=1.0)
        n = hz.n
        base = build_model(spec, hz, **kwargs)
        ext = build_model(spec, hz, full_charge=fc_model(n, 40, 90, taper=(0.9, 0.2)), **kwargs)
        assert len(ext[0]) == len(base[0]) + 50 + 1
        assert np.array_equal(ext[0][: 4 * n], base[0])
        base_a, ext_a = base[3][0].A.tocsr(), ext[3][0].A.tocsr()
        assert np.array_equal(ext_a[: base_a.shape[0], : 4 * n].toarray(), base_a.toarray())

    def test_planner_inputs_hash_and_plan_do_not_move_without_a_spec(self):
        snapshot = make_snapshot()
        assert snapshot.inputs_hash() == GOLDEN_INPUTS_HASH
        assert "full_charge" not in snapshot.input_payload()
        record = run_planner(snapshot).to_record()
        assert "full_charge" not in record
        assert set(record) == {
            "kind",
            "shadow",
            "plan_id",
            "plan_sequence",
            "generated_at",
            "expires_at",
            "content_hash",
            "inputs_hash",
            "code_version",
            "config_hash",
            "solver",
            "solve_seconds",
            "snapshot",
            "inputs",
            "intents",
        }
        assert run_planner(snapshot).full_charge is None


# ---------------------------------------------------------------------------
# Oracle model
# ---------------------------------------------------------------------------


class TestLiftedCeiling:
    def test_ceiling_is_per_slot(self):
        n = 48
        prices = np.full(n, 0.20)
        prices[30:36] = -0.05  # cheap block inside the window
        load = np.full(n, 0.02)
        spec = battery(0.5, soc_max=1.0)
        hz = make_horizon(prices, load)
        model = fc_model(n, 24, n, checkpoint=False)
        schedule = solve_free(spec, hz, full_charge=model)
        soc = np.asarray(schedule.soc_kwh)
        assert soc[:24].max() <= 0.95 * CAP + 1e-6
        assert soc[24:].max() > 0.95 * CAP + 1e-3  # the lifted part of the day is used
        bounds = build_model(
            spec,
            hz,
            terminal_mode="free",
            terminal_value_eur_per_kwh=0.11,
            floor_kwh=spec.soc_bound_kwh("min"),
            full_charge=model,
        )[2]
        soc_ub = bounds.ub[3 * n : 4 * n]
        assert np.allclose(soc_ub[:24], 0.95 * CAP)
        assert np.allclose(soc_ub[24:], CAP)

    def test_ceiling_above_spec_is_rejected(self):
        n = 8
        prices, load = day_arrays(n)
        with pytest.raises(ValueError, match="SOC window"):
            build_model(battery(0.5), make_horizon(prices, load), full_charge=fc_model(n, 2, 8))

    def test_bad_models_are_rejected(self):
        n = 8
        prices, load = day_arrays(n)
        spec = battery(0.5, soc_max=1.0)
        hz = make_horizon(prices, load)
        good = fc_model(n, 2, 8)
        with pytest.raises(ValueError, match="entries"):
            build_model(spec, hz, full_charge=replace(good, ceiling_kwh=good.ceiling_kwh[:-1]))
        with pytest.raises(ValueError, match="distinct"):
            build_model(spec, hz, full_charge=replace(good, checkpoint_slots=(2, 2)))
        with pytest.raises(ValueError, match="penalty"):
            build_model(spec, hz, full_charge=replace(good, shortfall_penalty_eur_per_kwh=0.0))
        with pytest.raises(ValueError, match="taper_power_kw"):
            build_model(
                spec, hz, full_charge=replace(good, taper_start_soc_frac=0.9, taper_power_kw=9.0)
            )
        with pytest.raises(ValueError, match="below the target"):
            build_model(
                spec,
                hz,
                full_charge=replace(good, ceiling_kwh=tuple([0.95 * CAP] * n)),
            )


def random_case(rng: np.random.Generator, n: int):
    prices = rng.uniform(0.04, 0.30, n)
    if rng.random() < 0.3:
        prices[rng.integers(0, n, max(1, n // 12))] = rng.uniform(-0.05, 0.0)
    load = rng.uniform(0.0, 0.16, n)
    soc = float(rng.uniform(0.2, 0.95))
    spec = battery(soc, soc_max=1.0)
    start = int(rng.integers(0, n - 1))
    end = int(rng.integers(start + 1, n + 1))
    return spec, make_horizon(prices, load), start, end


def raw_solve(spec, hz, model: FullChargeModel):
    obj, integrality, bounds, constraints = build_model(
        spec,
        hz,
        terminal_mode="free",
        terminal_value_eur_per_kwh=0.11,
        floor_kwh=spec.soc_bound_kwh("min"),
        full_charge=model,
    )
    result = milp(
        c=obj,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options={"mip_rel_gap": 1e-9, "time_limit": 60.0},
    )
    assert result.status == 0, result.message
    return result


class TestCheckpointMilp:
    @pytest.mark.parametrize("n", [4, 8, 24, 48, 96, 192])
    def test_equals_brute_force_enumeration(self, n):
        """One MILP with checkpoint binaries == the best of one single-checkpoint solve per slot."""
        rng = np.random.default_rng(1000 + n)
        for _ in range(2 if n >= 96 else 4):
            spec, hz, start, end = random_case(rng, n)
            taper = (0.8, 0.2) if rng.random() < 0.5 else None
            joint = raw_solve(spec, hz, fc_model(n, start, end, taper=taper))
            brute = min(
                raw_solve(
                    spec,
                    hz,
                    replace(fc_model(n, start, end, taper=taper), checkpoint_slots=(t,)),
                ).fun
                for t in range(start, end)
            )
            assert joint.fun == pytest.approx(brute, abs=1e-6)

    def test_reaches_target_and_reports_checkpoint(self):
        n = 96
        prices, load = day_arrays(n)
        spec = battery(0.4, soc_max=1.0)
        hz = make_horizon(prices, load)
        model = fc_model(n, 48, 94)
        schedule = solve_free(spec, hz, full_charge=model)
        slot, shortfall = checkpoint_outcome(schedule, model)
        assert shortfall == 0.0
        assert slot is not None and 48 <= slot < 94
        assert schedule.soc_kwh[slot] == pytest.approx(CAP, abs=1e-6)
        assert schedule.soc_kwh[:48].max() <= 0.95 * CAP + 1e-6

    def test_checkpoint_falls_in_cheap_slots(self):
        n = 96
        prices = np.full(n, 0.25)
        prices[60:66] = 0.01
        load = np.full(n, 0.03)
        spec = battery(0.5, soc_max=1.0)
        model = fc_model(n, 48, 94)
        schedule = solve_free(spec, make_horizon(prices, load), full_charge=model)
        slot, shortfall = checkpoint_outcome(schedule, model)
        assert shortfall == 0.0 and slot is not None and 60 <= slot <= 66
        assert np.asarray(schedule.charge_kwh)[48:60].sum() < 1e-6  # nothing bought at 0.25


class TestSoftTarget:
    def test_window_too_short_reports_shortfall_and_does_not_raise(self):
        n = 6  # 1.5 h from 20%: cannot reach 100%
        prices = np.full(n, 0.15)
        load = np.full(n, 0.02)
        spec = battery(0.2, soc_max=1.0)
        model = fc_model(n, 0, n)
        schedule = solve_free(spec, make_horizon(prices, load), full_charge=model)
        slot, shortfall = checkpoint_outcome(schedule, model)
        assert shortfall > 0.3
        assert shortfall == pytest.approx(CAP - schedule.soc_kwh.max(), abs=1e-6)
        assert schedule.soc_kwh[slot] == pytest.approx(schedule.soc_kwh.max())
        assert schedule.cost_lower_bound_eur is None  # the dual bound includes the penalty

    def test_low_soc_and_price_spike_are_always_feasible(self):
        rng = np.random.default_rng(7)
        for _ in range(12):
            n = int(rng.integers(2, 40))
            prices = rng.uniform(0.05, 0.3, n)
            prices[rng.integers(0, n)] = 50.0  # a price spike
            load = rng.uniform(0.0, 0.2, n)
            spec = battery(float(rng.uniform(0.2, 0.3)), soc_max=1.0)
            model = fc_model(n, 0, n)
            schedule = solve_free(spec, make_horizon(prices, load), full_charge=model)
            _, shortfall = checkpoint_outcome(schedule, model)
            assert 0.0 <= shortfall <= CAP

    def test_penalty_dwarfs_prices(self):
        # Even a very expensive slot is bought when it is the only way to cut the shortfall.
        n = 4
        prices = np.full(n, 1.0)
        load = np.full(n, 0.0)
        spec = battery(0.9, soc_max=1.0)
        model = fc_model(n, 0, n)
        schedule = solve_free(spec, make_horizon(prices, load), full_charge=model)
        assert checkpoint_outcome(schedule, model)[1] == pytest.approx(0.0, abs=1e-6)

    def test_no_checkpoint_without_slots(self):
        n = 8
        prices, load = day_arrays(n)
        spec = battery(0.5, soc_max=1.0)
        model = fc_model(n, 2, 8, checkpoint=False)
        schedule = solve_free(spec, make_horizon(prices, load), full_charge=model)
        assert checkpoint_outcome(schedule, model) == (None, 0.0)


class TestTaper:
    def test_rows_bind_and_still_reach_full_in_three_hours(self):
        n = 12  # 3 h; a 0.80 start makes the ramp bind (see the model docs)
        prices = np.full(n, 0.10)
        load = np.zeros(n)
        spec = battery(0.2, soc_max=1.0)
        model = fc_model(n, 0, n, taper=(0.8, 0.2))
        schedule = solve_free(spec, make_horizon(prices, load), full_charge=model)
        assert checkpoint_outcome(schedule, model)[1] == pytest.approx(0.0, abs=1e-6)
        charge = np.asarray(schedule.charge_kwh)
        soc_before = np.concatenate(([0.2 * CAP], np.asarray(schedule.soc_kwh)[:-1]))
        slope = (POWER - 0.2) * H / (CAP * 0.2)
        bound = H * 0.2 + slope * (CAP - soc_before)
        assert np.all(charge <= bound + 1e-6)
        full_power = POWER * H
        assert np.any(charge < full_power - 1e-3)  # the taper actually limited a slot
        assert np.any(np.isclose(charge, bound, atol=1e-6) & (bound < full_power - 1e-3))

    def test_taper_disabled_with_none(self):
        n = 8
        prices, load = day_arrays(n)
        spec = battery(0.5, soc_max=1.0)
        hz = make_horizon(prices, load)
        with_none = build_model(spec, hz, full_charge=fc_model(n, 0, n, taper=None))
        with_taper = build_model(spec, hz, full_charge=fc_model(n, 0, n, taper=(0.9, 0.2)))
        assert with_taper[3][0].A.shape[0] == with_none[3][0].A.shape[0] + n

    def test_taper_is_slower_than_no_taper(self):
        n = 8
        prices = np.full(n, 0.10)
        load = np.zeros(n)
        spec = battery(0.2, soc_max=1.0)
        hz = make_horizon(prices, load)
        free = solve_free(spec, hz, full_charge=fc_model(n, 0, n))
        slow = solve_free(spec, hz, full_charge=fc_model(n, 0, n, taper=(0.8, 0.2)))
        assert checkpoint_outcome(free, fc_model(n, 0, n))[1] == 0.0
        assert checkpoint_outcome(slow, fc_model(n, 0, n, taper=(0.8, 0.2)))[1] > 0.0


class TestTerminalModeUntouched:
    def test_fixed_pin_and_free_credit_are_unchanged(self):
        n = 24
        prices, load = day_arrays(n)
        spec = battery(0.5, soc_max=1.0)
        hz = make_horizon(prices, load)
        model = fc_model(n, 8, 20, taper=(0.9, 0.2))
        fixed_plain = build_model(spec, hz)
        fixed_ext = build_model(spec, hz, full_charge=model)
        last = 4 * n - 1
        assert fixed_ext[2].lb[last] == fixed_plain[2].lb[last] == 0.5 * CAP
        assert fixed_ext[2].ub[last] == fixed_plain[2].ub[last] == 0.5 * CAP
        free_plain = build_model(spec, hz, terminal_mode="free", terminal_value_eur_per_kwh=0.11)
        free_ext = build_model(
            spec, hz, terminal_mode="free", terminal_value_eur_per_kwh=0.11, full_charge=model
        )
        assert free_ext[0][last] == free_plain[0][last] == -0.11
        assert free_ext[2].ub[last] == CAP  # the lifted ceiling at the horizon end


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def make_snapshot(
    n: int = 96,
    soc: float = 0.5,
    full_charge: FullChargeSpec | None = None,
    *,
    prices=None,
) -> PlanningSnapshot:
    default_prices, load = day_arrays(n)
    prices = default_prices if prices is None else prices
    return PlanningSnapshot(
        decision_time=T0,
        interval_seconds=900,
        slot_starts=tuple(T0 + timedelta(minutes=15 * i) for i in range(n)),
        prices=tuple(float(p) for p in prices),
        load_kwh=tuple(float(v) for v in load),
        soc_frac=soc,
        soc_measured_frac=soc,
        soc_clamped=False,
        soc_source="entity",
        soc_observed_at=T0,
        spec=battery(soc),
        import_limit_kw=None,
        terminal_value_eur_per_kwh=0.11,
        terminal_value_source="test",
        throughput_cost_eur_per_kwh=0.0,
        solver_time_limit_s=20.0,
        plan_sequence=1,
        expires_at=T0 + timedelta(hours=1),
        trigger="test",
        code_version="test",
        config_hash="cfg",
        full_charge=full_charge,
    )


def make_spec(mode="evaluate", window_start=24, checkpoint_end=90, **overrides) -> FullChargeSpec:
    base = dict(
        mode=mode,
        window_start=window_start,
        checkpoint_end=checkpoint_end,
        ceiling_before_frac=0.95,
        target_soc_frac=1.0,
        shortfall_penalty_eur_per_kwh=10.0,
        taper_start_soc_frac=0.9,
        taper_power_kw=0.2,
    )
    base.update(overrides)
    return FullChargeSpec(**base)


class TestRunPlannerEvaluate:
    def test_regular_plan_is_untouched_and_three_solves_are_priced(self):
        plain = run_planner(make_snapshot())
        result = run_planner(make_snapshot(full_charge=make_spec("evaluate")))
        assert result.plan.content_hash == plain.plan.content_hash
        assert [i.to_dict() for i in result.intents] == [
            {**i.to_dict(), "inputs_hash": result.inputs_hash} for i in plain.intents
        ]
        ev = result.full_charge
        assert isinstance(ev, FullChargeEvaluation)
        assert ev.normal_cost_eur == pytest.approx(plain.solver["cost_eur"])
        assert ev.lifted_cost_eur is not None and ev.target_cost_eur is not None
        assert ev.forcing_cost_eur == pytest.approx(ev.target_cost_eur - ev.lifted_cost_eur)
        assert ev.vs_normal_eur == pytest.approx(ev.target_cost_eur - ev.normal_cost_eur)
        assert ev.forcing_cost_eur >= -1e-6  # the constraint cannot make the day cheaper
        assert ev.shortfall_kwh == 0.0
        assert ev.checkpoint_slot is not None
        first, last = T0 + timedelta(minutes=15 * 24), T0 + timedelta(minutes=15 * 90)
        assert first <= ev.checkpoint_slot < last
        assert set(ev.solver) == {"normal", "lifted", "target"}
        assert all({"status", "gap", "seconds"} <= set(v) for v in ev.solver.values())
        assert "full_charge_error" not in result.solver

    def test_record_has_the_evaluation_only_when_present(self):
        result = run_planner(make_snapshot(full_charge=make_spec("evaluate")))
        record = result.to_record()
        assert record["full_charge"]["checkpoint_slot"].startswith("2026-11-0")
        assert record["inputs"]["full_charge"]["mode"] == "evaluate"
        assert "full_charge" not in run_planner(make_snapshot()).to_record()
        assert make_snapshot(full_charge=make_spec()).inputs_hash() != GOLDEN_INPUTS_HASH
        json.dumps(record)

    def test_evaluate_full_charge_can_attach_to_an_already_published_plan(self):
        snapshot = make_snapshot(full_charge=make_spec("evaluate"))
        regular = run_planner(replace(snapshot, full_charge=None))
        ev = evaluate_full_charge(snapshot, regular)
        assert ev.normal_cost_eur == pytest.approx(regular.solver["cost_eur"])
        assert ev.forcing_cost_eur is not None

    def test_failures_are_recorded_never_raised(self, monkeypatch):
        real = planner_mod._solve
        calls = {"n": 0}

        def flaky(snapshot, spec, horizon, full_charge=None):
            if full_charge is not None:
                calls["n"] += 1
                raise OracleInfeasibleError("boom")
            return real(snapshot, spec, horizon, full_charge)

        monkeypatch.setattr(planner_mod, "_solve", flaky)
        plain = run_planner(make_snapshot())
        result = run_planner(make_snapshot(full_charge=make_spec("evaluate")))
        assert calls["n"] == 1
        assert result.full_charge is None
        assert "OracleInfeasibleError: boom" in result.solver["full_charge_error"]
        assert result.plan.content_hash == plain.plan.content_hash

    def test_a_lifted_soc_is_planned_from_the_window_maximum_in_the_regular_plan(self):
        result = run_planner(make_snapshot(soc=0.98, full_charge=make_spec("evaluate")))
        assert result.full_charge is not None
        assert result.plan.intents[0].soc_target_frac <= 0.95 + 1e-9

    def test_a_missed_target_is_reported_not_raised(self):
        snapshot = make_snapshot(
            n=12,
            soc=0.2,
            full_charge=make_spec("evaluate", window_start=0, checkpoint_end=4),
        )
        ev = run_planner(snapshot).full_charge
        assert ev is not None and ev.shortfall_kwh > 0.5


class TestRunPlannerCommit:
    def test_target_solve_is_the_plan_of_record(self):
        snapshot = make_snapshot(full_charge=make_spec("commit"))
        result = run_planner(snapshot)
        ev = result.full_charge
        assert ev is not None
        assert ev.normal_cost_eur is None and ev.lifted_cost_eur is None
        assert ev.forcing_cost_eur is None and ev.vs_normal_eur is None
        assert ev.shortfall_kwh == 0.0
        top = max(i.soc_target_frac for i in result.plan.intents)
        assert top == pytest.approx(1.0, abs=1e-6)
        plain = run_planner(make_snapshot())
        assert max(i.soc_target_frac for i in plain.plan.intents) <= 0.95 + 1e-9
        assert result.solver["cost_eur"] == pytest.approx(ev.target_cost_eur)

    def test_kernel_recheck_needs_the_lifted_spec(self):
        snapshot = make_snapshot(full_charge=make_spec("commit"))
        n = len(snapshot.slot_starts)
        spec = planner_mod._lifted_spec(snapshot, snapshot.full_charge)
        hz = planner_mod._horizon(snapshot)
        model = planner_mod._full_charge_model(spec, snapshot.full_charge, n, checkpoint=True)
        schedule = planner_mod._solve(snapshot, spec, hz, model)
        assert spec.soc_max_frac == 1.0
        assert (
            check_feasible(
                spec, hz, schedule.charge_kwh, schedule.discharge_kwh, enforce_terminal_soc=False
            )
            == []
        )
        strict = replace(spec, soc_max_frac=0.95)
        assert check_feasible(
            strict, hz, schedule.charge_kwh, schedule.discharge_kwh, enforce_terminal_soc=False
        )
        with pytest.raises(BatteryPhysicsError):
            from battery_optimizer.battery import simulate

            simulate(
                strict, hz, schedule.charge_kwh, schedule.discharge_kwh, enforce_terminal_soc=False
            )

    def test_failure_falls_back_to_the_regular_plan_with_the_error(self, monkeypatch):
        real = planner_mod._solve

        def flaky(snapshot, spec, horizon, full_charge=None):
            if full_charge is not None:
                raise RuntimeError("no incumbent")
            return real(snapshot, spec, horizon, full_charge)

        monkeypatch.setattr(planner_mod, "_solve", flaky)
        plain = run_planner(make_snapshot())
        result = run_planner(make_snapshot(soc=0.98, full_charge=make_spec("commit")))
        assert result.full_charge is None
        assert "no incumbent" in result.solver["full_charge_error"]
        assert result.plan.intents  # a usable plan, planned from the 95% clamp
        assert plain.plan.intents

    def test_lifted_soc_is_not_clamped_inside_the_window(self):
        inside = make_snapshot(
            soc=0.98, full_charge=make_spec("commit", window_start=0, checkpoint_end=80)
        )
        result = run_planner(inside)
        assert result.full_charge is not None and result.full_charge.shortfall_kwh == 0.0
        # Started from 98%, not clamped to 95%.
        assert result.plan.intents[0].soc_target_frac > 0.95


class TestSpecValidation:
    def test_spec_rejects_nonsense(self):
        with pytest.raises(ValueError, match="mode"):
            make_spec("sometimes")
        with pytest.raises(ValueError, match="window_start"):
            make_spec(window_start=10, checkpoint_end=10)
        with pytest.raises(ValueError, match="ceiling_before_frac"):
            make_spec(ceiling_before_frac=1.2)
        with pytest.raises(ValueError, match="target_soc_frac"):
            make_spec(target_soc_frac=0.0)

    def test_snapshot_rejects_a_checkpoint_beyond_the_horizon(self):
        with pytest.raises(ValueError, match="checkpoint_end"):
            make_snapshot(n=40, full_charge=make_spec(checkpoint_end=90))


class TestClampSoc:
    @staticmethod
    def tariff(window_max=0.95):
        battery_cfg = BatteryConfig(
            capacity_kwh=CAP,
            power_kw=POWER,
            round_trip_efficiency=0.8,
            soc_window_min=0.2,
            soc_window_max=window_max,
            import_limit_kw=None,
        )
        return SimpleNamespace(battery=battery_cfg)

    def test_default_clamps_to_the_window(self):
        assert clamp_soc(0.98, self.tariff()) == (0.95, True)
        assert clamp_soc(0.5, self.tariff()) == (0.5, False)
        assert clamp_soc(0.1, self.tariff()) == (0.2, True)

    def test_lifted_ceiling_only_when_passed(self):
        assert clamp_soc(0.98, self.tariff(), ceiling_frac=1.0) == (0.98, False)
        assert clamp_soc(1.0, self.tariff(), ceiling_frac=1.0) == (1.0, False)
        assert clamp_soc(0.98, self.tariff(), ceiling_frac=None) == (0.95, True)
        assert clamp_soc(0.1, self.tariff(), ceiling_frac=1.0) == (0.2, True)


# ---------------------------------------------------------------------------
# Solve-time regression
# ---------------------------------------------------------------------------


def test_solve_time_regression_bound():
    """60 random cases of 4 to 192 slots: the checkpoint MILP stays within a generous multiple."""
    rng = np.random.default_rng(2026)
    normal: list[float] = []
    target: list[float] = []
    for _ in range(60):
        n = int(rng.integers(4, 193))
        spec, hz, start, end = random_case(rng, n)
        started = time.perf_counter()
        solve_free(spec, hz)
        normal.append(time.perf_counter() - started)
        started = time.perf_counter()
        solve_free(spec, hz, full_charge=fc_model(n, start, end, taper=(0.9, 0.2)))
        target.append(time.perf_counter() - started)
    # Measured (taper on, random windows): median about 13x the normal solve, maximum about
    # 1.5 s.  The bounds only catch a blow-up, never a slow runner.
    assert statistics.median(target) <= 40 * statistics.median(normal) + 0.5
    assert max(target) < 10.0
