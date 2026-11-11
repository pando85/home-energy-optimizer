"""The planner's marginal value of stored energy (ADR-0007 amendment 2026-11-11).

After each solve the planner prices one more kWh in the battery at the end of the plan's first
slot (two tail solves, a finite difference) and records it, in EUR per AC kWh discharged, on the
plan, the first intent and a gauge.  It is an output: it enters neither hash, and a failed extra
solve leaves the plan valid with no value.
"""

from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest
from test_full_charge_solver import CAP, POWER, T0, make_snapshot, make_spec

from battery_optimizer.battery import BatterySpec
from battery_optimizer.service import planner as planner_mod
from battery_optimizer.service.planner import (
    STORAGE_VALUE_DELTA_KWH,
    STORAGE_VALUE_MIN_DELTA_KWH,
    run_planner,
)

ETA = 0.9
TP = 0.01
FLOOR_FRAC = 0.2
FLOOR_KWH = FLOOR_FRAC * CAP


def tail_case(seed_above_floor_kwh: float, *, peak: float = 0.30, tp: float = TP):
    """A three-slot plan whose first slot is idle and whose tail is one priced slot.

    Slot 0 (price 0.50, no load) can neither charge profitably nor discharge, so the planned SOC
    at its end is the initial SOC.  Slot 1 (price *peak*, 1 kWh of load) is the only slot where
    stored energy earns anything, slot 2 earns nothing.  With the seed below the slot's
    deliverable energy every extra stored kWh is discharged there: ``value_ac = peak - tp``
    exactly, whatever the efficiency.
    """
    soc = (FLOOR_KWH + seed_above_floor_kwh) / CAP
    snapshot = make_snapshot(n=3, soc=soc, prices=np.array([0.50, peak, 0.05]))
    return replace(
        snapshot,
        load_kwh=(0.0, 1.0, 0.0),
        spec=BatterySpec(CAP, POWER, ETA, ETA, FLOOR_FRAC, 0.95, soc, None),
        terminal_value_eur_per_kwh=0.0,
        throughput_cost_eur_per_kwh=tp,
    )


class TestValue:
    def test_value_is_the_price_net_of_wear_per_ac_kwh(self):
        result = run_planner(tail_case(0.40))
        value = result.storage_value
        assert value is not None and value.status == "ok"
        assert value.eur_per_kwh == pytest.approx(0.30 - TP, abs=1e-6)
        # the planner's own convention: 1 AC kWh discharged consumes 1 / eta DC kWh
        assert value.eur_per_dc_kwh == pytest.approx((0.30 - TP) * ETA, abs=1e-6)
        assert value.eur_per_kwh == pytest.approx(value.eur_per_dc_kwh / ETA)
        assert value.delta_kwh == STORAGE_VALUE_DELTA_KWH
        assert value.soc_kwh == pytest.approx(FLOOR_KWH + 0.40)
        assert value.objective_eur is not None and value.objective_lower_eur is not None
        assert value.objective_lower_eur - value.objective_eur == pytest.approx(
            value.eur_per_dc_kwh * value.delta_kwh
        )

    def test_value_follows_the_wear_cost(self):
        no_wear = run_planner(tail_case(0.40, tp=0.0)).storage_value
        wear = run_planner(tail_case(0.40, tp=0.05)).storage_value
        assert no_wear is not None and wear is not None
        assert no_wear.eur_per_kwh == pytest.approx(0.30, abs=1e-6)
        assert wear.eur_per_kwh == pytest.approx(0.25, abs=1e-6)

    def test_energy_the_tail_cannot_use_is_worth_nothing(self):
        # 2 kWh above the floor against a slot that can deliver 0.625 kWh AC: the margin is idle
        value = run_planner(tail_case(2.0)).storage_value
        assert value is not None and value.status == "ok"
        assert value.eur_per_kwh == pytest.approx(0.0, abs=1e-6)

    def test_the_terminal_value_is_part_of_the_value(self):
        # nothing to discharge into and buying is dearer than the credit: the seed energy ends up
        # in the terminal inventory, so one more DC kWh is worth exactly the terminal value
        snapshot = replace(
            tail_case(0.40, tp=0.0),
            prices=(0.50, 0.50, 0.50),
            load_kwh=(0.0, 0.0, 0.0),
            terminal_value_eur_per_kwh=0.20,
        )
        value = run_planner(snapshot).storage_value
        assert value is not None and value.status == "ok"
        assert value.eur_per_dc_kwh == pytest.approx(0.20, abs=1e-6)
        assert value.eur_per_kwh == pytest.approx(0.20 / ETA, abs=1e-6)

    def test_the_dwell_cost_makes_idle_energy_worth_less_than_nothing(self):
        # kept through two tail slots above a threshold below the floor:
        # delta * 0.1 EUR/kWh/h * 2 slots * 0.25 h
        snapshot = replace(
            tail_case(0.40, tp=0.0),
            prices=(0.50, 0.50, 0.50),
            load_kwh=(0.0, 0.0, 0.0),
            terminal_value_eur_per_kwh=0.0,
            high_soc_threshold_frac=0.01,
            high_soc_cost_eur_per_kwh_h=0.1,
        )
        value = run_planner(snapshot).storage_value
        assert value is not None and value.eur_per_dc_kwh is not None
        assert value.eur_per_dc_kwh == pytest.approx(-0.1 * 2 * 0.25, abs=1e-6)
        assert value.eur_per_kwh == pytest.approx(-0.05 / ETA, abs=1e-6)

    def test_a_realistic_plan_has_a_finite_nonnegative_value_and_the_extra_solves_are_cheap(self):
        snapshot = replace(make_snapshot(n=192), throughput_cost_eur_per_kwh=TP)
        started = time.perf_counter()
        result = run_planner(snapshot)
        total = time.perf_counter() - started
        value = result.storage_value
        assert value is not None and value.status == "ok" and value.eur_per_kwh is not None
        assert np.isfinite(value.eur_per_kwh) and value.eur_per_kwh >= -1e-9
        assert 0.0 < value.seconds < min(total, 5.0)


class TestStepClipping:
    def test_the_step_is_clipped_at_the_soc_floor(self):
        value = run_planner(tail_case(0.10)).storage_value
        assert value is not None and value.status == "ok"
        assert value.delta_kwh == pytest.approx(0.10)
        assert value.eur_per_kwh == pytest.approx(0.30 - TP, abs=1e-6)

    @pytest.mark.parametrize("above", [0.0, STORAGE_VALUE_MIN_DELTA_KWH / 2])
    def test_a_step_clipped_to_nothing_gives_no_value(self, above):
        result = run_planner(tail_case(above))
        value = result.storage_value
        assert value is not None and value.status == "at_floor" and value.eur_per_kwh is None
        assert all(i.storage_value_eur_per_kwh is None for i in result.intents)
        assert result.plan.n == 3  # the plan itself is untouched

    def test_a_one_slot_plan_has_no_tail(self):
        one = replace(tail_case(0.40), slot_starts=(T0,), prices=(0.3,), load_kwh=(1.0,))
        value = run_planner(one).storage_value
        assert value is not None and value.status == "no_tail" and value.eur_per_kwh is None


class TestFailureIsNotFatal:
    def _failing_tail(self, monkeypatch, make_error):
        real = planner_mod.solve_oracle
        calls = []

        def spy(*args, **kwargs):
            calls.append(kwargs.get("time_limit_s"))
            if len(calls) > 1:  # the plan's own solve is the first
                return make_error(real, *args, **kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(planner_mod, "solve_oracle", spy)
        return calls

    def test_a_failing_tail_solve_leaves_the_plan_valid_without_a_value(self, monkeypatch):
        snapshot = tail_case(0.40)
        reference = run_planner(snapshot)

        def boom(real, *args, **kwargs):
            raise RuntimeError("HiGHS exploded")

        self._failing_tail(monkeypatch, boom)
        result = run_planner(snapshot)
        assert result.storage_value is not None
        assert result.storage_value.status == "solver_error"
        assert result.storage_value.eur_per_kwh is None
        assert "HiGHS exploded" in (result.storage_value.error or "")
        assert result.plan.content_hash == reference.plan.content_hash
        assert result.solver["status"] == reference.solver["status"]
        assert [i.mode for i in result.intents] == [i.mode for i in reference.intents]
        assert all(i.storage_value_eur_per_kwh is None for i in result.intents)
        record = result.to_record()
        assert record["storage_value"]["status"] == "solver_error"
        assert record["intents"][0]["storage_value_eur_per_kwh"] is None

    def test_a_tail_solve_that_uses_its_limit_gives_no_value(self, monkeypatch):
        monkeypatch.setattr(planner_mod, "STORAGE_VALUE_TIME_LIMIT_S", 0.05)

        def slow(real, *args, **kwargs):
            time.sleep(0.06)
            return real(*args, **kwargs)

        calls = self._failing_tail(monkeypatch, slow)
        result = run_planner(tail_case(0.40))
        assert result.storage_value is not None and result.storage_value.status == "time_limit"
        assert result.storage_value.eur_per_kwh is None
        assert calls[1:] == [0.05]  # stopped after the first tail solve, with its own short limit

    def test_the_tail_solves_never_get_more_than_the_plans_own_limit(self, monkeypatch):
        calls = self._failing_tail(monkeypatch, lambda real, *a, **kw: real(*a, **kw))
        run_planner(replace(tail_case(0.40), solver_time_limit_s=1.5))
        assert calls == [1.5, 1.5, 1.5]


class TestOutputNotInput:
    def test_neither_hash_depends_on_the_value(self, monkeypatch):
        snapshot = make_snapshot()
        with_value = run_planner(snapshot)
        assert with_value.storage_value is not None and with_value.storage_value.status == "ok"
        monkeypatch.setattr(
            planner_mod,
            "compute_storage_value",
            lambda *a, **k: planner_mod.StorageValue.unavailable("solver_error"),
        )
        without = run_planner(snapshot)
        assert without.storage_value is not None and without.storage_value.eur_per_kwh is None
        assert with_value.plan.content_hash == without.plan.content_hash
        assert with_value.inputs_hash == without.inputs_hash == snapshot.inputs_hash()
        assert with_value.plan_id == without.plan_id
        assert "storage_value" not in snapshot.input_payload()

    def test_only_the_first_slot_carries_the_value(self):
        result = run_planner(tail_case(0.40))
        values = [i.storage_value_eur_per_kwh for i in result.intents]
        assert values[0] == pytest.approx(0.30 - TP, abs=1e-6)
        assert values[1:] == [None, None]
        record = result.to_record()
        assert record["storage_value"]["eur_per_kwh"] == values[0]
        assert [i["storage_value_eur_per_kwh"] for i in record["intents"]] == values

    def test_the_value_is_not_computed_for_a_full_charge_commit_plan(self):
        snapshot = make_snapshot(full_charge=make_spec("commit", 0, 90, taper_start_soc_frac=None))
        result = run_planner(snapshot)
        assert "full_charge_error" not in result.solver
        assert result.storage_value is not None
        assert result.storage_value.status == "full_charge_commit"
        assert result.storage_value.eur_per_kwh is None

    def test_the_value_is_computed_for_a_full_charge_evaluation_plan(self):
        result = run_planner(make_snapshot(full_charge=make_spec("evaluate", 24, 90)))
        assert result.storage_value is not None and result.storage_value.status == "ok"

    def test_tail_solves_carry_the_plans_optional_costs(self, monkeypatch):
        seen = []
        real = planner_mod.solve_oracle

        def spy(*args, **kwargs):
            seen.append(
                (
                    kwargs.get("high_soc_cost"),
                    kwargs.get("charge_soft_cap"),
                    kwargs.get("discharge_margin_kw"),
                    kwargs.get("throughput_cost_eur_per_kwh"),
                )
            )
            return real(*args, **kwargs)

        monkeypatch.setattr(planner_mod, "solve_oracle", spy)
        snapshot = replace(
            make_snapshot(),
            high_soc_cost_eur_per_kwh_h=0.01,
            charge_soft_cap_eur_per_kwh=0.02,
            discharge_margin_kw=0.05,
            throughput_cost_eur_per_kwh=TP,
        )
        result = run_planner(snapshot)
        assert result.storage_value is not None and result.storage_value.status == "ok"
        assert len(seen) == 3 and len(set(seen)) == 1
        assert seen[0] == ((0.90, 0.01), (1.5, 0.02), 0.05, TP)
