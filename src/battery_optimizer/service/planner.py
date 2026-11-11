"""Live planning: the offline MPC kernel driven from a frozen input snapshot.

There is no second optimizer here.  A plan is
:func:`battery_optimizer.oracle.solve_oracle` in ``terminal_mode="free"`` with an
explicit terminal-inventory value and the configured throughput cost — the exact
call the rolling-horizon benchmark makes per event — followed by
:func:`battery_optimizer.dispatch.intents_from_schedule`, which re-checks the plan
against the shared battery kernel and emits the TODO-4.6 dispatch contract.  The
battery parameters come from ``tariff.yaml`` through
:func:`battery_optimizer.benchmark.resolve_battery_assumptions`, the mapping the
benchmark uses (``sqrt(RTE)`` per leg, configured SOC window and import ceiling).

Two deliberate differences from the benchmark, both forced by running live:

* **SOC floor.**  The benchmark floors every step at its evaluation window's
  opening SOC (seed protection, a fairness device for comparing policies).  A
  live plan has no evaluation window: the energy in the battery is real, so the
  floor is the physical window minimum (``soc_window_min``) and the leftover is
  valued at the terminal value instead.
* **Price horizon.**  The benchmark may plan past the published prices with
  exact or noise-perturbed *true* prices.  Live prices beyond the last published
  hour do not exist, so the horizon ends at the last published slot
  (``max_horizon_slots`` caps it); the terminal value is what keeps the plan from
  emptying the battery at the end of the published window.

An optional :class:`FullChargeSpec` on the snapshot prices (``evaluate``) or plans
(``commit``) a day with a lifted SOC ceiling and a soft checkpoint at 100%
(docs/planning/periodic-full-charge.md); without it nothing below changes.  An optional high-SOC
dwell cost (``planning.high_soc_*``, off at 0) prices the hours spent above a threshold SOC in every
solve of the snapshot, the full-charge ones included; so does the optional thermal soft cap on
charge power (``planning.charge_soft_cap_*``, off at 0).

Everything the solve depends on is frozen in :class:`PlanningSnapshot` before it
is handed to the worker thread, and its canonical hash (``inputs_hash``) is stored
with every plan and intent.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np

from battery_optimizer.battery import BatterySpec, Horizon, Schedule
from battery_optimizer.config import TariffConfig
from battery_optimizer.dispatch import DispatchMode, DispatchPlan, intents_from_schedule
from battery_optimizer.oracle import (
    FullChargeModel,
    charge_soft_cap_excess_kwh,
    checkpoint_outcome,
    high_soc_dwell_kwh_h,
    solve_oracle,
)

__all__ = [
    "FullChargeEvaluation",
    "FullChargeSpec",
    "PlanResult",
    "PlanningSnapshot",
    "ShadowIntent",
    "StorageValue",
    "battery_spec_from_tariff",
    "clamp_soc",
    "compute_storage_value",
    "evaluate_full_charge",
    "run_planner",
    "shadow_intents",
]

#: Marginal value of stored energy (see :func:`compute_storage_value`): the SOC step (DC kWh) of
#: the finite difference.  Large enough that the MIP gap (1e-6 relative here) and the solver's
#: tolerances stay far below the margin the actuator compares against (EUR 0.005 per kWh).
STORAGE_VALUE_DELTA_KWH = 0.25
#: A step clipped below this by the SOC floor is not trusted: no value (the actuator falls back).
STORAGE_VALUE_MIN_DELTA_KWH = 0.05
#: Wall-clock limit of each of the two tail solves (s); never above the plan's own limit.  The
#: tail solve is a strict subproblem of the plan's, so it normally ends in a fraction of a second.
STORAGE_VALUE_TIME_LIMIT_S = 5.0
#: Relative MIP gap of the tail solves (the plan's own solve keeps the kernel default, 1e-4).
STORAGE_VALUE_MIP_REL_GAP = 1e-6


def battery_spec_from_tariff(
    tariff: TariffConfig, soc_frac: float
) -> tuple[BatterySpec, float | None, dict[str, Any]]:
    """``(spec, import_limit_kw, provenance)`` via the benchmark's resolver."""
    from battery_optimizer.benchmark import resolve_battery_assumptions

    assumptions = resolve_battery_assumptions(tariff, initial_soc_frac=soc_frac)
    spec = BatterySpec(**assumptions.spec_kwargs())
    provenance = {
        "efficiency_mapping": assumptions.efficiency_mapping,
        "import_limit_source": assumptions.import_limit_source,
    }
    return spec, assumptions.import_limit_kw, provenance


def clamp_soc(
    soc_frac: float, tariff: TariffConfig, *, ceiling_frac: float | None = None
) -> tuple[float, bool]:
    """Clamp a measured SOC into the configured planning window.

    ``ceiling_frac`` replaces the window maximum: the caller passes the lifted ceiling
    (1.0) only while a full-charge run is pending, so a measured 98% is not planned as
    95%.  ``None`` keeps the configured window.
    """
    lo = float(tariff.battery.soc_window_min or 0.0)
    hi = float(tariff.battery.soc_window_max or 1.0) if ceiling_frac is None else ceiling_frac
    clamped = min(max(soc_frac, lo), hi)
    return clamped, not math.isclose(clamped, soc_frac, abs_tol=1e-12)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# The ceiling of a full-charge day: the whole nameplate.
_LIFTED_CEILING_FRAC = 1.0


@dataclass(frozen=True)
class FullChargeSpec:
    """A full-charge day for one solve (docs/planning/periodic-full-charge.md, section 5).

    ``evaluate``: the regular plan is solved as today, then more solves price the day
    (lifted, target) into a :class:`FullChargeEvaluation`.  ``commit``: the target solve
    is the plan of record.  Slots are indices into the snapshot: the checkpoint is one of
    ``range(window_start, checkpoint_end)``, the ceiling is ``ceiling_before_frac`` before
    ``window_start`` and 1.0 from it to the horizon end.
    """

    mode: Literal["evaluate", "commit"]
    window_start: int  # first slot of the full-charge day in the snapshot (0 if inside it)
    checkpoint_end: int  # exclusive end of the eligible checkpoint slots
    ceiling_before_frac: float  # 0.95: ceiling for slots before window_start
    target_soc_frac: float  # 1.0
    shortfall_penalty_eur_per_kwh: float
    taper_start_soc_frac: float | None
    taper_power_kw: float

    def __post_init__(self) -> None:
        if self.mode not in ("evaluate", "commit"):
            raise ValueError(f"full-charge mode must be 'evaluate' or 'commit', got {self.mode!r}")
        if not 0 <= self.window_start < self.checkpoint_end:
            raise ValueError("full-charge needs 0 <= window_start < checkpoint_end")
        if not 0.0 < self.ceiling_before_frac <= _LIFTED_CEILING_FRAC:
            raise ValueError("ceiling_before_frac must be in (0, 1]")
        if not 0.0 < self.target_soc_frac <= _LIFTED_CEILING_FRAC:
            raise ValueError("target_soc_frac must be in (0, 1]")


@dataclass(frozen=True)
class FullChargeEvaluation:
    """What the full-charge solves of one snapshot found.

    Costs are the plans' real ``cost_eur`` (terminal-inventory credit included, shortfall
    penalty and throughput cost excluded), so they compare across solves.
    ``forcing_cost_eur = target - lifted``: the price of the checkpoint alone.
    ``vs_normal_eur = target - normal`` is a diagnostic (the lifted ceiling also changes
    what the free plan may store).  ``normal``, ``lifted``, ``forcing`` and ``vs_normal``
    are ``None`` in commit mode.
    """

    normal_cost_eur: float | None  # evaluate mode only
    lifted_cost_eur: float | None  # evaluate mode only
    target_cost_eur: float
    forcing_cost_eur: float | None
    vs_normal_eur: float | None
    checkpoint_slot: datetime | None
    shortfall_kwh: float
    solver: dict[str, Any]  # status, gap, time per solve

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["checkpoint_slot"] = _iso(self.checkpoint_slot) if self.checkpoint_slot else None
        return out


@dataclass(frozen=True)
class StorageValue:
    """The marginal value of stored energy at the end of a plan's first slot.

    ``eur_per_kwh`` is EUR per AC kWh discharged (``eur_per_dc_kwh / discharge_efficiency``), in
    the planner's objective units: wear (throughput cost), the terminal value and the optional
    dwell and soft-cap costs included, the load term and the real bill's inventory offset not.
    ``None`` when the extra solve was not possible or not trusted: ``status`` says why
    (``ok``; ``no_tail`` a one-slot plan; ``at_floor`` the first slot ends at the SOC floor so
    the step would be clipped to nothing; ``time_limit`` a tail solve used its limit;
    ``solver_error``; ``full_charge_commit`` a full-charge day's plan, whose tail model differs).
    """

    eur_per_kwh: float | None
    eur_per_dc_kwh: float | None
    status: str
    delta_kwh: float | None = None  # the SOC step actually used (DC kWh), clipped at the floor
    soc_kwh: float | None = None  # the plan's SOC at the end of the first slot (DC kWh)
    objective_eur: float | None = None  # the tail objective at that SOC
    objective_lower_eur: float | None = None  # ... and at delta_kwh less
    seconds: float = 0.0
    error: str | None = None

    @classmethod
    def unavailable(cls, status: str, *, error: str | None = None, **fields: Any) -> StorageValue:
        return cls(None, None, status, error=error, **fields)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanningSnapshot:
    """Every input of one solve, frozen before it leaves the event loop."""

    decision_time: datetime
    interval_seconds: int
    slot_starts: tuple[datetime, ...]
    prices: tuple[float, ...]
    load_kwh: tuple[float, ...]
    soc_frac: float
    soc_measured_frac: float | None
    soc_clamped: bool
    soc_source: str
    soc_observed_at: datetime | None
    spec: BatterySpec
    import_limit_kw: float | None
    terminal_value_eur_per_kwh: float
    terminal_value_source: str
    throughput_cost_eur_per_kwh: float
    solver_time_limit_s: float
    plan_sequence: int
    expires_at: datetime
    trigger: str
    code_version: str
    config_hash: str
    forecast_meta: dict[str, Any] = field(default_factory=dict)
    price_meta: dict[str, Any] = field(default_factory=dict)
    full_charge: FullChargeSpec | None = None
    #: High-SOC dwell cost (``planning.high_soc_*``): EUR per kWh above the threshold per hour.
    high_soc_threshold_frac: float = 0.90
    high_soc_cost_eur_per_kwh_h: float = 0.0
    #: Thermal soft cap on charge power (``planning.charge_soft_cap_*``): EUR per AC kWh charged
    #: above the threshold power in a slot.
    charge_soft_cap_kw: float = 1.5
    charge_soft_cap_eur_per_kwh: float = 0.0
    #: Grid import (kW) the actuator keeps while discharging
    #: (``planning.discharge_margin_bound``); 0 is off: discharge is bounded by the load alone.
    discharge_margin_kw: float = 0.0

    def __post_init__(self) -> None:
        n = len(self.slot_starts)
        if n == 0 or len(self.prices) != n or len(self.load_kwh) != n:
            raise ValueError("snapshot slots, prices and load must be non-empty and aligned")
        if self.full_charge is not None and self.full_charge.checkpoint_end > n:
            raise ValueError("full-charge checkpoint_end is beyond the snapshot's slots")

    @property
    def first_slot(self) -> datetime:
        return self.slot_starts[0]

    def input_payload(self) -> dict[str, Any]:
        """The solve's inputs (no decision instant, trigger or sequence)."""
        payload: dict[str, Any] = {
            "interval_seconds": self.interval_seconds,
            "slot_starts": [_iso(s) for s in self.slot_starts],
            "prices": [float(p) for p in self.prices],
            "load_kwh": [round(float(v), 9) for v in self.load_kwh],
            "soc_frac": round(self.soc_frac, 9),
            "soc_source": self.soc_source,
            "spec": {
                k: getattr(self.spec, k)
                for k in (
                    "capacity_kwh",
                    "power_kw",
                    "charge_efficiency",
                    "discharge_efficiency",
                    "soc_min_frac",
                    "soc_max_frac",
                )
            },
            "import_limit_kw": self.import_limit_kw,
            "terminal_value_eur_per_kwh": self.terminal_value_eur_per_kwh,
            "throughput_cost_eur_per_kwh": self.throughput_cost_eur_per_kwh,
            "solver_time_limit_s": self.solver_time_limit_s,
            "config_hash": self.config_hash,
        }
        if self.full_charge is not None:
            payload["full_charge"] = asdict(self.full_charge)
        if self.high_soc_cost_eur_per_kwh_h > 0.0:
            # Only an active dwell cost joins the inputs, so the hash of a plan without it is
            # the hash it always was.
            payload["high_soc_cost"] = {
                "threshold_frac": self.high_soc_threshold_frac,
                "eur_per_kwh_h": self.high_soc_cost_eur_per_kwh_h,
            }
        if self.charge_soft_cap_eur_per_kwh > 0.0:
            # Likewise the thermal soft cap joins the inputs only when active.
            payload["charge_soft_cap"] = {
                "threshold_kw": self.charge_soft_cap_kw,
                "eur_per_kwh": self.charge_soft_cap_eur_per_kwh,
            }
        if self.discharge_margin_kw > 0.0:
            # Likewise the discharge margin bound.
            payload["discharge_margin_kw"] = self.discharge_margin_kw
        return payload

    def inputs_hash(self) -> str:
        text = json.dumps(self.input_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def summary(self) -> dict[str, Any]:
        return {
            "decision_time": _iso(self.decision_time),
            "first_slot": _iso(self.first_slot),
            "slots": len(self.slot_starts),
            "horizon_end": _iso(self.slot_starts[-1] + timedelta(seconds=self.interval_seconds)),
            "soc_frac": self.soc_frac,
            "soc_measured_frac": self.soc_measured_frac,
            "soc_clamped": self.soc_clamped,
            "soc_source": self.soc_source,
            "soc_observed_at": _iso(self.soc_observed_at) if self.soc_observed_at else None,
            "terminal_value_eur_per_kwh": self.terminal_value_eur_per_kwh,
            "terminal_value_source": self.terminal_value_source,
            "import_limit_kw": self.import_limit_kw,
            "trigger": self.trigger,
            "forecast": self.forecast_meta,
            "prices": self.price_meta,
            "load_forecast_kwh_total": float(sum(self.load_kwh)),
        }


@dataclass(frozen=True)
class ShadowIntent:
    """One time-bounded **shadow** intent: recorded, never sent anywhere."""

    plan_id: str
    plan_sequence: int
    sequence_id: int
    valid_from: datetime
    valid_until: datetime
    """``min(interval end, plan expiry)``; an intent is never current outside it."""
    interval_end: datetime
    mode: str
    target_power_w: float
    signed_power_w: float
    """``+`` charge, ``-`` discharge (requested AC power)."""
    soc_target_frac: float
    expected_buy_price_eur_per_kwh: float | None
    generated_at: datetime
    expires_at: datetime
    inputs_hash: str
    code_version: str
    shadow: bool = True
    storage_value_eur_per_kwh: float | None = None
    """Marginal value of stored energy at the end of this slot, EUR per AC kWh discharged
    (:func:`compute_storage_value`); set on a plan's first slot only, ``None`` elsewhere or when
    the extra solve failed.  An output of the plan, not an input: it is in neither hash."""

    @property
    def actionable(self) -> bool:
        """False for intents that start after the plan expired (forecast only)."""
        return self.valid_from < self.valid_until

    def is_current(self, now: datetime) -> bool:
        return self.actionable and self.valid_from <= now < self.valid_until

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_sequence": self.plan_sequence,
            "sequence_id": self.sequence_id,
            "valid_from": _iso(self.valid_from),
            "valid_until": _iso(self.valid_until),
            "interval_end": _iso(self.interval_end),
            "actionable": self.actionable,
            "mode": self.mode,
            "target_power_w": self.target_power_w,
            "signed_power_w": self.signed_power_w,
            "soc_target_frac": self.soc_target_frac,
            "expected_buy_price_eur_per_kwh": self.expected_buy_price_eur_per_kwh,
            "generated_at": _iso(self.generated_at),
            "expires_at": _iso(self.expires_at),
            "inputs_hash": self.inputs_hash,
            "code_version": self.code_version,
            "shadow": self.shadow,
            "storage_value_eur_per_kwh": self.storage_value_eur_per_kwh,
        }


def shadow_intents(
    plan: DispatchPlan,
    *,
    plan_id: str,
    inputs_hash: str,
    code_version: str,
    storage_value: tuple[datetime, float] | None = None,
) -> list[ShadowIntent]:
    """The plan's intents; *storage_value* is ``(slot start, EUR per AC kWh)`` of the one slot."""
    out: list[ShadowIntent] = []
    for intent in plan.intents:
        mode = DispatchMode(intent.mode)
        sign = (
            1.0 if mode is DispatchMode.CHARGE else -1.0 if mode is DispatchMode.DISCHARGE else 0.0
        )
        out.append(
            ShadowIntent(
                plan_id=plan_id,
                plan_sequence=plan.plan_sequence,
                sequence_id=intent.sequence_id,
                valid_from=intent.start,
                valid_until=min(intent.end, plan.expires_at),
                interval_end=intent.end,
                mode=mode.value,
                target_power_w=intent.target_power_w,
                signed_power_w=sign * intent.target_power_w,
                soc_target_frac=intent.soc_target_frac,
                expected_buy_price_eur_per_kwh=intent.expected_buy_price_eur_per_kwh,
                generated_at=plan.generated_at,
                expires_at=plan.expires_at,
                inputs_hash=inputs_hash,
                code_version=code_version,
                storage_value_eur_per_kwh=(
                    storage_value[1]
                    if storage_value is not None and storage_value[0] == intent.start
                    else None
                ),
            )
        )
    return out


@dataclass(frozen=True)
class PlanResult:
    snapshot: PlanningSnapshot
    plan: DispatchPlan
    plan_id: str
    inputs_hash: str
    intents: tuple[ShadowIntent, ...]
    solver: dict[str, Any]
    solve_seconds: float
    full_charge: FullChargeEvaluation | None = None
    storage_value: StorageValue | None = None

    @property
    def storage_value_eur_per_kwh(self) -> float | None:
        """The first slot's marginal value of stored energy (EUR per AC kWh), if it was found."""
        return None if self.storage_value is None else self.storage_value.eur_per_kwh

    def current_intent(self, now: datetime) -> ShadowIntent | None:
        for intent in self.intents:
            if intent.is_current(now):
                return intent
        return None

    def intent_for_slot(self, slot_start: datetime) -> ShadowIntent | None:
        """The actionable intent valid at *slot_start*, if this plan has one."""
        return self.current_intent(slot_start)

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "kind": "shadow_plan",
            "shadow": True,
            "plan_id": self.plan_id,
            "plan_sequence": self.plan.plan_sequence,
            "generated_at": _iso(self.plan.generated_at),
            "expires_at": _iso(self.plan.expires_at),
            "content_hash": self.plan.content_hash,
            "inputs_hash": self.inputs_hash,
            "code_version": self.snapshot.code_version,
            "config_hash": self.snapshot.config_hash,
            "solver": self.solver,
            "solve_seconds": self.solve_seconds,
            "snapshot": self.snapshot.summary(),
            "inputs": self.snapshot.input_payload(),
            "intents": [i.to_dict() for i in self.intents],
        }
        if self.full_charge is not None:
            record["full_charge"] = self.full_charge.to_dict()
        if self.storage_value is not None:
            record["storage_value"] = self.storage_value.to_dict()
        return record


def _horizon(snapshot: PlanningSnapshot) -> Horizon:
    return Horizon(
        interval_seconds=snapshot.interval_seconds,
        prices=np.asarray(snapshot.prices, dtype=float),
        load_kwh=np.asarray(snapshot.load_kwh, dtype=float),
        usable=np.ones(len(snapshot.slot_starts), dtype=bool),
        import_limit_kw=snapshot.import_limit_kw,
    )


def _solve(
    snapshot: PlanningSnapshot,
    spec: BatterySpec,
    horizon: Horizon,
    full_charge: FullChargeModel | None = None,
    **solver: float,
) -> Schedule:
    """The plan's solve; *solver* overrides ``time_limit_s`` and the MIP gaps (tail solves)."""
    solver = {"time_limit_s": snapshot.solver_time_limit_s, **solver}
    return solve_oracle(
        spec,
        horizon,
        **solver,
        terminal_mode="free",
        terminal_value_eur_per_kwh=snapshot.terminal_value_eur_per_kwh,
        floor_kwh=spec.soc_bound_kwh("min"),
        throughput_cost_eur_per_kwh=snapshot.throughput_cost_eur_per_kwh,
        full_charge=full_charge,
        high_soc_cost=(
            (snapshot.high_soc_threshold_frac, snapshot.high_soc_cost_eur_per_kwh_h)
            if snapshot.high_soc_cost_eur_per_kwh_h > 0.0
            else None
        ),
        charge_soft_cap=(
            (snapshot.charge_soft_cap_kw, snapshot.charge_soft_cap_eur_per_kwh)
            if snapshot.charge_soft_cap_eur_per_kwh > 0.0
            else None
        ),
        discharge_margin_kw=snapshot.discharge_margin_kw,
    )


def _regular_spec(snapshot: PlanningSnapshot, *, capped: bool) -> BatterySpec:
    """The regular plan's spec; ``capped`` plans a lifted measured SOC from the window maximum."""
    soc = min(snapshot.soc_frac, snapshot.spec.soc_max_frac) if capped else snapshot.soc_frac
    return replace(snapshot.spec, initial_soc_frac=soc, terminal_soc_frac=None)


def _lifted_spec(snapshot: PlanningSnapshot, fc: FullChargeSpec) -> BatterySpec:
    """The kernel spec of a full-charge day: ``soc_max_frac`` 1.0, looser than the LP.

    A measured SOC above the ceiling in force at the first slot is planned at that ceiling
    (the same conservative clamp the regular plan makes).
    """
    soc = snapshot.soc_frac
    if fc.window_start > 0:
        soc = min(soc, fc.ceiling_before_frac)
    return replace(
        snapshot.spec,
        soc_max_frac=_LIFTED_CEILING_FRAC,
        initial_soc_frac=soc,
        terminal_soc_frac=None,
    )


def _full_charge_model(
    spec: BatterySpec, fc: FullChargeSpec, n: int, *, checkpoint: bool
) -> FullChargeModel:
    cap = spec.capacity_kwh
    ceiling = tuple(
        cap * (fc.ceiling_before_frac if t < fc.window_start else _LIFTED_CEILING_FRAC)
        for t in range(n)
    )
    return FullChargeModel(
        ceiling_kwh=ceiling,
        checkpoint_slots=tuple(range(fc.window_start, fc.checkpoint_end)) if checkpoint else (),
        target_kwh=cap * fc.target_soc_frac,
        shortfall_penalty_eur_per_kwh=fc.shortfall_penalty_eur_per_kwh,
        taper_start_soc_frac=fc.taper_start_soc_frac,
        taper_power_kw=fc.taper_power_kw,
        taper_first_slot=fc.window_start,
    )


def _solve_summary(schedule: Schedule, seconds: float) -> dict[str, Any]:
    return {
        "status": schedule.solver_status,
        "gap": schedule.solver_gap,
        "certified": schedule.is_certified,
        "seconds": seconds,
    }


def _solve_variant(
    snapshot: PlanningSnapshot, horizon: Horizon, *, checkpoint: bool
) -> tuple[BatterySpec, FullChargeModel, Schedule, float]:
    """One lifted-ceiling solve, with the checkpoint (target) or without (lifted)."""
    fc = snapshot.full_charge
    if fc is None:
        raise ValueError("snapshot has no full-charge spec")
    spec = _lifted_spec(snapshot, fc)
    model = _full_charge_model(spec, fc, horizon.n, checkpoint=checkpoint)
    started = time.perf_counter()
    schedule = _solve(snapshot, spec, horizon, model)
    return spec, model, schedule, time.perf_counter() - started


def _evaluation(
    snapshot: PlanningSnapshot,
    model: FullChargeModel,
    target: Schedule,
    target_seconds: float,
    *,
    normal: tuple[float, dict[str, Any]] | None = None,
    lifted: tuple[Schedule, float] | None = None,
) -> FullChargeEvaluation:
    slot, shortfall = checkpoint_outcome(target, model)
    solver = {"target": _solve_summary(target, target_seconds)}
    normal_cost = lifted_cost = forcing = vs_normal = None
    if normal is not None and lifted is not None:
        normal_cost, solver["normal"] = normal
        lifted_cost = lifted[0].cost_eur
        solver["lifted"] = _solve_summary(*lifted)
        forcing = target.cost_eur - lifted_cost
        vs_normal = target.cost_eur - normal_cost
    return FullChargeEvaluation(
        normal_cost_eur=normal_cost,
        lifted_cost_eur=lifted_cost,
        target_cost_eur=target.cost_eur,
        forcing_cost_eur=forcing,
        vs_normal_eur=vs_normal,
        checkpoint_slot=None if slot is None else snapshot.slot_starts[slot],
        shortfall_kwh=shortfall,
        solver=solver,
    )


def evaluate_full_charge(snapshot: PlanningSnapshot, regular: PlanResult) -> FullChargeEvaluation:
    """The lifted and target solves of an evaluation; *regular* is the normal plan, already solved.

    Kept apart from :func:`run_planner` so a caller can publish the regular plan first and
    attach the evaluation afterwards.  Raises on a solver failure; ``run_planner`` records it.
    """
    horizon = _horizon(snapshot)
    _, _, lifted, lifted_seconds = _solve_variant(snapshot, horizon, checkpoint=False)
    _, model, target, target_seconds = _solve_variant(snapshot, horizon, checkpoint=True)
    normal_summary = {
        "status": regular.solver["status"],
        "gap": regular.solver["gap"],
        "certified": regular.solver["certified"],
        "seconds": regular.solve_seconds,
    }
    return _evaluation(
        snapshot,
        model,
        target,
        target_seconds,
        normal=(regular.solver["cost_eur"], normal_summary),
        lifted=(lifted, lifted_seconds),
    )


def _objective_eur(snapshot: PlanningSnapshot, spec: BatterySpec, schedule: Schedule) -> float:
    """The solver's objective of *schedule*, up to the constants every tail solve shares.

    ``meter cost + throughput cost * (AC charge + AC discharge) - terminal value * terminal SOC``
    plus the optional dwell and soft-cap costs: exactly what the MIP minimises (the load term is
    inside the meter cost and is the same for every initial SOC).  The reported ``cost_eur`` is
    not used: it adds ``terminal value * initial SOC``, which differs between the two solves.
    """
    h = snapshot.interval_seconds
    throughput = float(np.sum(schedule.charge_kwh) + np.sum(schedule.discharge_kwh))
    total = schedule.meter_cost_eur if schedule.meter_cost_eur is not None else schedule.cost_eur
    total += snapshot.throughput_cost_eur_per_kwh * throughput
    total -= snapshot.terminal_value_eur_per_kwh * schedule.terminal_soc_kwh
    if snapshot.high_soc_cost_eur_per_kwh_h > 0.0:
        total += snapshot.high_soc_cost_eur_per_kwh_h * high_soc_dwell_kwh_h(
            schedule, spec, h, snapshot.high_soc_threshold_frac
        )
    if snapshot.charge_soft_cap_eur_per_kwh > 0.0:
        total += snapshot.charge_soft_cap_eur_per_kwh * charge_soft_cap_excess_kwh(
            schedule, h, snapshot.charge_soft_cap_kw
        )
    return float(total)


def compute_storage_value(
    snapshot: PlanningSnapshot, spec: BatterySpec, horizon: Horizon, schedule: Schedule
) -> StorageValue:
    """The marginal value of stored energy at the end of the plan's first slot.

    The first slot's decision stays as planned.  The *tail* problem (slots 1.. of the horizon,
    same model and objective as the plan) is solved from the planned SOC ``s1`` and from
    ``s1 - delta``; ``value_dc = (J(s1 - delta) - J(s1)) / delta`` is what one more DC kWh in
    the battery saves, and ``value_ac = value_dc / discharge_efficiency`` is the price of
    discharging one more AC kWh now instead of keeping it, in the units of a slot price.

    ``delta`` is :data:`STORAGE_VALUE_DELTA_KWH`, clipped so ``s1 - delta`` stays at or above
    the SOC floor; a step smaller than :data:`STORAGE_VALUE_MIN_DELTA_KWH` gives no value.
    Never raises: a failed, timed-out or implausible extra solve is a ``None`` value with the
    reason in ``status``, and the plan stays valid.
    """
    started = time.perf_counter()

    def elapsed() -> float:
        return time.perf_counter() - started

    try:
        if horizon.n < 2:
            return StorageValue.unavailable("no_tail", seconds=elapsed())
        soc_kwh = float(schedule.soc_kwh[0])
        floor_kwh = spec.soc_bound_kwh("min")
        delta = min(STORAGE_VALUE_DELTA_KWH, soc_kwh - floor_kwh)
        if delta < STORAGE_VALUE_MIN_DELTA_KWH:
            return StorageValue.unavailable(
                "at_floor", soc_kwh=soc_kwh, delta_kwh=max(delta, 0.0), seconds=elapsed()
            )
        tail = Horizon(
            interval_seconds=horizon.interval_seconds,
            prices=horizon.prices[1:],
            load_kwh=horizon.load_kwh[1:],
            usable=horizon.usable[1:],
            import_limit_kw=horizon.import_limit_kw,
        )
        limit = min(STORAGE_VALUE_TIME_LIMIT_S, snapshot.solver_time_limit_s)
        capacity = spec.capacity_kwh
        objective: list[float] = []
        for seed_kwh in (soc_kwh, soc_kwh - delta):
            seed_frac = min(max(seed_kwh / capacity, spec.soc_min_frac), spec.soc_max_frac)
            tail_spec = replace(spec, initial_soc_frac=seed_frac, terminal_soc_frac=None)
            began = time.perf_counter()
            solved = _solve(
                snapshot,
                tail_spec,
                tail,
                time_limit_s=limit,
                mip_rel_gap=STORAGE_VALUE_MIP_REL_GAP,
            )
            if time.perf_counter() - began >= 0.9 * limit:
                # The kernel reports a penalised plan as "feasible" whatever stopped the solver,
                # so a solve that used its limit is told apart by the clock.
                return StorageValue.unavailable(
                    "time_limit", soc_kwh=soc_kwh, delta_kwh=delta, seconds=elapsed()
                )
            objective.append(_objective_eur(snapshot, tail_spec, solved))
        value_dc = (objective[1] - objective[0]) / delta
        value_ac = value_dc / spec.discharge_efficiency
        if not math.isfinite(value_ac):
            return StorageValue.unavailable(
                "solver_error",
                error="non-finite value",
                soc_kwh=soc_kwh,
                delta_kwh=delta,
                seconds=elapsed(),
            )
        return StorageValue(
            eur_per_kwh=value_ac,
            eur_per_dc_kwh=value_dc,
            status="ok",
            delta_kwh=delta,
            soc_kwh=soc_kwh,
            objective_eur=objective[0],
            objective_lower_eur=objective[1],
            seconds=elapsed(),
        )
    except Exception as exc:  # noqa: BLE001 - the value is optional: the plan stays valid
        return StorageValue.unavailable(
            "solver_error", error=f"{type(exc).__name__}: {exc}"[:200], seconds=elapsed()
        )


def run_planner(snapshot: PlanningSnapshot) -> PlanResult:
    """Solve one snapshot.  CPU-bound and pure: runs in the bounded worker.

    With ``snapshot.full_charge`` unset this is the regular plan.  In ``evaluate`` mode the
    regular plan is solved first and returned unchanged with the lifted/target
    :class:`FullChargeEvaluation` attached; a failure is recorded under
    ``solver["full_charge_error"]``, never raised.  In ``commit`` mode the target solve is
    the plan of record; if it fails, the regular plan is returned with the error recorded.
    """
    started = time.perf_counter()
    fc = snapshot.full_charge
    horizon = _horizon(snapshot)
    if fc is not None and fc.mode == "commit":
        try:
            spec, model, schedule, seconds = _solve_variant(snapshot, horizon, checkpoint=True)
            evaluation = _evaluation(snapshot, model, schedule, seconds)
            # The plan is a lifted-ceiling target solve: its tail model is not the regular one.
            return _plan_result(
                snapshot,
                spec,
                horizon,
                schedule,
                started,
                evaluation,
                StorageValue.unavailable("full_charge_commit"),
            )
        except Exception as exc:  # noqa: BLE001 - the regular plan is the safe fallback
            result = _regular(snapshot, horizon, started)
            error = f"{type(exc).__name__}: {exc}"
            return replace(result, solver={**result.solver, "full_charge_error": error})
    result = _regular(snapshot, horizon, started)
    if fc is None:
        return result
    try:
        evaluation = evaluate_full_charge(snapshot, result)
    except Exception as exc:  # noqa: BLE001 - an evaluation never costs the regular plan
        error = f"{type(exc).__name__}: {exc}"
        return replace(result, solver={**result.solver, "full_charge_error": error})
    return replace(result, full_charge=evaluation)


def _regular(snapshot: PlanningSnapshot, horizon: Horizon, started: float) -> PlanResult:
    spec = _regular_spec(snapshot, capped=snapshot.full_charge is not None)
    schedule = _solve(snapshot, spec, horizon)
    storage_value = compute_storage_value(snapshot, spec, horizon, schedule)
    return _plan_result(snapshot, spec, horizon, schedule, started, storage_value=storage_value)


def _plan_result(
    snapshot: PlanningSnapshot,
    spec: BatterySpec,
    horizon: Horizon,
    schedule: Schedule,
    started: float,
    evaluation: FullChargeEvaluation | None = None,
    storage_value: StorageValue | None = None,
) -> PlanResult:
    plan = intents_from_schedule(
        schedule,
        horizon,
        battery_spec=spec,
        interval_starts_utc=list(snapshot.slot_starts),
        generated_at=snapshot.decision_time,
        expires_at=snapshot.expires_at,
        plan_sequence=snapshot.plan_sequence,
        expected_buy_prices=list(snapshot.prices),
    )
    inputs_hash = snapshot.inputs_hash()
    plan_id = (
        f"{snapshot.decision_time.strftime('%Y%m%dT%H%M%SZ')}-{snapshot.plan_sequence}-"
        f"{plan.content_hash[:8]}"
    )
    value = None if storage_value is None else storage_value.eur_per_kwh
    intents = shadow_intents(
        plan,
        plan_id=plan_id,
        inputs_hash=inputs_hash,
        code_version=snapshot.code_version,
        storage_value=None if value is None else (snapshot.first_slot, value),
    )
    solver = {
        "kernel": "oracle.solve_oracle(terminal_mode='free') + dispatch.intents_from_schedule",
        "status": schedule.solver_status,
        "gap": schedule.solver_gap,
        "certified": schedule.is_certified,
        "cost_eur": schedule.cost_eur,
        "meter_cost_eur": schedule.meter_cost_eur,
        "terminal_soc_kwh": schedule.terminal_soc_kwh,
        "charge_kwh": float(np.sum(schedule.charge_kwh)),
        "discharge_kwh": float(np.sum(schedule.discharge_kwh)),
    }
    return PlanResult(
        snapshot=snapshot,
        plan=plan,
        plan_id=plan_id,
        inputs_hash=inputs_hash,
        intents=tuple(intents),
        solver=solver,
        solve_seconds=time.perf_counter() - started,
        full_charge=evaluation,
        storage_value=storage_value,
    )
