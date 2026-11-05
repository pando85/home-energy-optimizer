"""Discharge margin bound: model, planner, config and runtime wiring.

``oracle.build_model`` / ``solve_oracle`` take ``discharge_margin_kw``: the no-export row becomes
``discharge[t] - charge[t] <= max(0, load[t] - discharge_margin_kw * h)``.  ``0`` must leave the
model byte for byte as it was.  The service switches it on with ``planning.discharge_margin_bound``
and feeds it ``actuation.discharge_margin_w``, the grid import the live actuator keeps.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from service_fakes import NOW, feed, write_service_config
from test_full_charge_solver import (
    GOLDEN_INPUTS_HASH,
    GOLDEN_MODEL_DIGESTS,
    H,
    battery,
    make_horizon,
    make_snapshot,
    make_spec,
    model_digest,
    solve_free,
)
from test_full_charge_solver import (
    TestGolden as _FullChargeGolden,
)

from battery_optimizer.oracle import build_model
from battery_optimizer.service import planner as planner_mod
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.config import load_service_config
from battery_optimizer.service.planner import run_planner

MARGIN_KW = 0.2
MARGIN_KWH = MARGIN_KW * H


def peak_day(n: int = 96, load_kwh: float = 0.3) -> tuple[np.ndarray, np.ndarray]:
    """A cheap night and a priced evening peak, so the plan discharges at the no-export limit."""
    t = np.arange(n)
    prices = np.where(t < 24, 0.05, np.where(t < 64, 0.12, 0.40))
    load = np.full(n, load_kwh)
    load[10] = 0.04  # below the margin's 0.05 kWh: the bound floors at zero
    return prices, load


class TestModel:
    @pytest.mark.parametrize("name", ["fixed", "free", "limit"])
    def test_zero_margin_model_is_byte_identical(self, name):
        spec, hz, kwargs = _FullChargeGolden()._cases()[name]
        model = build_model(spec, hz, discharge_margin_kw=0.0, **kwargs)
        assert model_digest(model) == GOLDEN_MODEL_DIGESTS[name]

    def test_only_the_no_export_right_hand_side_moves(self):
        prices, load = peak_day()
        spec, hz = battery(0.3), make_horizon(prices, load)
        n = hz.n
        base = build_model(spec, hz)
        obj, integrality, bounds, (con,) = build_model(spec, hz, discharge_margin_kw=MARGIN_KW)
        assert np.array_equal(obj, base[0]) and np.array_equal(integrality, base[1])
        assert np.array_equal(bounds.ub, base[2].ub) and np.array_equal(bounds.lb, base[2].lb)
        base_con = base[3][0]
        assert np.array_equal(con.A.toarray(), base_con.A.toarray())
        assert np.array_equal(con.lb, base_con.lb)
        moved = np.flatnonzero(con.ub != base_con.ub)
        assert len(moved) == n  # every row tightens
        export_rows = slice(3 * n, 4 * n)
        expected = np.maximum(0.0, load - MARGIN_KWH)
        assert np.allclose(con.ub[export_rows], expected)
        assert con.ub[3 * n + 10] == 0.0
        assert np.array_equal(np.delete(con.ub, export_rows), np.delete(base_con.ub, export_rows))

    def test_a_margin_above_the_load_floors_the_row_at_zero(self):
        prices, load = peak_day(load_kwh=0.03)
        spec, hz = battery(0.3), make_horizon(prices, load)
        n = hz.n
        con = build_model(spec, hz, discharge_margin_kw=MARGIN_KW)[3][0]
        assert np.all(con.ub[3 * n : 4 * n] == 0.0)

    @pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), True, "x"])
    def test_invalid_margins_are_refused(self, bad):
        prices, load = peak_day()
        spec, hz = battery(0.3), make_horizon(prices, load)
        with pytest.raises((ValueError, TypeError), match="discharge_margin_kw|finite|not"):
            build_model(spec, hz, discharge_margin_kw=bad)


class TestSolve:
    def test_discharge_never_exceeds_the_load_less_the_margin(self):
        prices, load = peak_day()
        spec, hz = battery(0.3), make_horizon(prices, load)
        free = solve_free(spec, hz)
        bound = solve_free(spec, hz, discharge_margin_kw=MARGIN_KW)
        limit = np.maximum(0.0, load - MARGIN_KWH)
        net = np.asarray(bound.discharge_kwh) - np.asarray(bound.charge_kwh)
        assert np.all(net <= limit + 1e-6)
        # The unbound plan does reach the plain no-export limit, so the test bites.
        assert np.max(np.asarray(free.discharge_kwh) - limit) > 1e-3

    def test_zero_margin_reproduces_the_previous_solution(self):
        prices, load = peak_day()
        spec, hz = battery(0.3), make_horizon(prices, load)
        plain = solve_free(spec, hz)
        zero = solve_free(spec, hz, discharge_margin_kw=0.0)
        assert np.array_equal(plain.charge_kwh, zero.charge_kwh)
        assert np.array_equal(plain.discharge_kwh, zero.discharge_kwh)
        assert zero.cost_eur == plain.cost_eur
        assert zero.cost_lower_bound_eur == plain.cost_lower_bound_eur
        assert zero.is_certified == plain.is_certified

    def test_an_infeasible_looking_margin_still_solves(self):
        prices, load = peak_day(load_kwh=0.03)
        spec, hz = battery(0.3), make_horizon(prices, load)
        s = solve_free(spec, hz, discharge_margin_kw=MARGIN_KW)
        assert np.asarray(s.discharge_kwh).max() <= 1e-6


class TestPlanner:
    def _snapshot(self, margin_kw, **kwargs):
        prices, load = peak_day()
        return replace(
            make_snapshot(prices=prices, soc=0.3, **kwargs),
            load_kwh=tuple(float(v) for v in load),
            spec=battery(0.3),
            discharge_margin_kw=margin_kw,
        )

    def test_hash_and_payload_move_only_when_active(self):
        default = make_snapshot()
        assert default.discharge_margin_kw == 0.0
        assert default.inputs_hash() == GOLDEN_INPUTS_HASH
        assert "discharge_margin_kw" not in default.input_payload()
        on = self._snapshot(MARGIN_KW)
        assert on.input_payload()["discharge_margin_kw"] == MARGIN_KW
        assert on.inputs_hash() != self._snapshot(0.0).inputs_hash()
        assert on.inputs_hash() != self._snapshot(0.1).inputs_hash()

    def test_planned_discharge_never_exceeds_the_forecast_load_less_the_margin(self):
        off, on = self._snapshot(0.0), self._snapshot(MARGIN_KW)
        limit_w = np.maximum(0.0, np.asarray(on.load_kwh) / H - MARGIN_KW) * 1000.0
        plan_on = run_planner(on)
        plan_off = run_planner(off)
        assert len(plan_on.intents) == len(on.load_kwh)
        for slot, intent in enumerate(plan_on.intents):
            assert -intent.signed_power_w <= limit_w[slot] + 1e-3, slot
        # Without the bound the plan does ask for more than the actuator could deliver.
        assert any(
            -i.signed_power_w > limit_w[slot] + 1.0 for slot, i in enumerate(plan_off.intents)
        )

    @pytest.mark.parametrize("mode", ["evaluate", "commit"])
    def test_every_solve_of_a_full_charge_plan_carries_the_margin(self, monkeypatch, mode):
        seen = []
        real = planner_mod.solve_oracle

        def spy(*args, **kwargs):
            seen.append(kwargs.get("discharge_margin_kw"))
            return real(*args, **kwargs)

        monkeypatch.setattr(planner_mod, "solve_oracle", spy)
        result = run_planner(
            self._snapshot(
                MARGIN_KW, full_charge=make_spec(mode, 24, 90, taper_start_soc_frac=None)
            )
        )
        assert "full_charge_error" not in result.solver
        assert seen and all(m == MARGIN_KW for m in seen)


class TestConfig:
    def test_default_is_off_and_the_hash_does_not_move(self, tmp_path):
        base = load_service_config(write_service_config(tmp_path))
        explicit = load_service_config(
            write_service_config(tmp_path, **{"planning.discharge_margin_bound": False})
        )
        assert base.planning.discharge_margin_bound is False
        assert explicit.config_hash() == base.config_hash()

    def test_the_actuation_margin_is_not_a_planning_input_while_off(self, tmp_path):
        base = load_service_config(write_service_config(tmp_path))
        moved = load_service_config(
            write_service_config(tmp_path, **{"actuation.discharge_margin_w": 120})
        )
        assert moved.config_hash() == base.config_hash()

    def test_the_switch_and_the_margin_it_reads_both_change_the_hash(self, tmp_path):
        base = load_service_config(write_service_config(tmp_path))
        on = load_service_config(
            write_service_config(tmp_path, **{"planning.discharge_margin_bound": True})
        )
        on_moved = load_service_config(
            write_service_config(
                tmp_path,
                **{"planning.discharge_margin_bound": True, "actuation.discharge_margin_w": 120},
            )
        )
        assert on.planning.discharge_margin_bound is True
        assert on.config_hash() != base.config_hash()
        assert on_moved.config_hash() != on.config_hash()

    @pytest.mark.parametrize("bad", ["yes", 1, None, "true"])
    def test_a_non_boolean_switch_is_refused(self, tmp_path, bad):
        with pytest.raises(ValueError, match="discharge_margin_bound"):
            load_service_config(
                write_service_config(tmp_path, **{"planning.discharge_margin_bound": bad})
            )

    @pytest.mark.parametrize("bad", [0, -50])
    def test_the_margin_it_reads_must_stay_positive(self, tmp_path, bad):
        with pytest.raises(ValueError, match="discharge_margin_w"):
            load_service_config(
                write_service_config(
                    tmp_path,
                    **{
                        "planning.discharge_margin_bound": True,
                        "actuation.discharge_margin_w": bad,
                    },
                )
            )


@pytest.mark.parametrize(("bound", "expected_kw"), [(False, 0.0), (True, 0.15)], ids=["off", "on"])
def test_the_actuation_margin_reaches_the_plan_snapshot(tmp_path, bound, expected_kw):
    from test_service_runtime import make_service, run

    clock = FakeClock(NOW)
    service = make_service(
        tmp_path,
        clock,
        entity_mode=True,
        **{"planning.discharge_margin_bound": bound, "actuation.discharge_margin_w": 150},
    )
    try:
        feed(service, NOW)
        assert run(service.request_solve("test")) == "ok"
        snapshot = service.plans[-1].snapshot
        assert snapshot.discharge_margin_kw == pytest.approx(expected_kw)
        assert ("discharge_margin_kw" in snapshot.input_payload()) is bound
    finally:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)
