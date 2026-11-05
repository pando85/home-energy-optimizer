"""Perfect-information oracle — offline benchmark via ``scipy.optimize.milp`` (HiGHS).

A chronologically feasible, cost-minimising dispatch over the **whole** horizon
with full knowledge of prices and load.  It is an upper bound on achievable
savings and a ceiling for comparison only — not a deployable policy, and not
MPC (ADR-0003 §"Strategy comparison").

Model layout
------------
Four decision variables per interval ``t`` (``n`` intervals, so ``4n`` columns):

======================  ==========================  ================
variable                index                       type
======================  ==========================  ================
``charge[t]`` (kWh)     ``3*t``                     continuous
``discharge[t]`` (kWh)  ``3*t + 1``                 continuous
``mode[t]``             ``3*t + 2``                 binary (0/1)
``soc[t]`` (kWh)        ``3*n + t``                 continuous
======================  ==========================  ================

``soc`` is carried as an explicit variable block rather than eliminated through
prefix sums: the recurrence then costs one sparse row per interval (4 nonzeros)
instead of a dense triangular matrix, which is what keeps a full-year horizon
(35 040 intervals) tractable.

Constraints
-----------
1. **Mutual exclusion** (one binary mode per interval), with big-M at the exact
   per-interval energy limit ``M = power_kw * interval_seconds / 3600`` — no
   looser big-M, so nothing to relax numerically::

       charge[t]    - M * mode[t] <= 0
       discharge[t] + M * mode[t] <= M

   ``mode = 1`` permits charging only, ``mode = 0`` discharging only.  This is
   what blocks the negative-price pathology: with ``price[t] < 0`` the LP
   relaxation would charge *and* discharge in the same interval to harvest the
   negative price (``grid`` only has to stay non-negative, and charging keeps it
   positive).  The binary makes that structurally impossible, so a negative
   price becomes a legitimate charging opportunity instead of free money.
2. **SOC recurrence**, one equality row per interval::

       soc[t] - soc[t-1] - eta_c * charge[t] + discharge[t] / eta_d == 0

   with ``soc[-1] = capacity * initial_soc_frac`` folded into the right-hand
   side of the ``t = 0`` row as a constant.  Charge therefore *causally precedes*
   discharge: energy can only leave after it entered, via the running ``soc``.
3. **SOC bounds at every step** as *variable* bounds.  In ``terminal_mode=
   "fixed"`` they are ``capacity*soc_min_frac <= soc[t] <= capacity*soc_max_frac``.
   In ``terminal_mode="free"`` the floor is raised to the shared causal floor
   ``max(soc_min, initial_soc)`` for **every** step, so a free plan can never
   spend the seed charge it did not pay for — the same floor the causal policy
   uses in its neutral case, which is what puts all four policies on one common
   feasible set.
4. **Terminal SOC** (``fixed`` only): bounds on ``soc[n-1]`` equal to
   ``capacity * terminal_soc_frac``, or ``capacity * initial_soc_frac`` when the
   spec leaves it ``None`` (no free energy created or destroyed over the
   horizon).  In ``"free"`` mode the last SOC variable is **not** pinned; it is
   simply free within ``[initial SOC, capacity*soc_max_frac]`` (the raised floor
   and the ceiling), and its leftover inventory is instead valued in the
   objective — see "Objective" below.
5. **No intentional export**::

       discharge[t] - charge[t] <= load[t]

   i.e. ``grid[t] = load + charge - discharge >= 0``.  Charging *increases*
   import, so this row bounds discharge only and never the charge side; without a
   configured ceiling the charge variables are bounded by the power limit and SOC
   headroom alone (the same physics the causal policy uses).  There is no
   ``charge <= load`` cap, and constraint 6 never bounds the discharge side.

   An optional ``discharge_margin_kw`` (default ``0``: the row above, byte for byte)
   models an actuator that keeps a small grid import while discharging, so the battery
   can deliver at most the load minus that margin::

       discharge[t] - charge[t] <= max(0, load[t] - discharge_margin_kw * h)

   The service planner enables it so the plan does not promise energy the live
   actuator cannot deliver; the offline benchmark leaves it off.  It only tightens the
   feasible set, so the kernel re-trace (which checks the margin-free rule) still
   accepts every plan.
6. **Site import ceiling** — only when ``horizon.import_limit_kw`` is
   configured; the row block is omitted entirely when it is ``None``, because an
   unknown ceiling must stay unknown rather than become an invented number.  The
   ceiling limits the *charging decision*, never the measured load::

       charge[t] <= max(0, import_limit_kw * h - load[t])

   i.e. each interval's right-hand side is the headroom the load leaves under
   the ceiling (see :attr:`~battery_optimizer.battery.Horizon.charge_headroom_kwh`).
   A load that already meets or exceeds the ceiling gives a zero right-hand side
   and simply forces ``charge[t] = 0``; the problem stays feasible, and the
   kernel reports those intervals as an observation
   (:attr:`~battery_optimizer.battery.Schedule.ceiling_exceeding_intervals`).
   Discharge is *not* part of this row: it lowers the draw and stays bounded by
   the no-export rule alone, so the oracle may discharge to shave a peak.  The
   only remaining source of infeasibility is a terminal SOC the power limit and
   headroom together cannot reach, which surfaces as
   :class:`OracleInfeasibleError`.
7. **Unusable intervals** (``usable[t] is False``): ``charge = discharge = 0`` via
   variable bounds and zero objective coefficients, so gap exclusions are
   identical to the other policies' cost basis.  A horizon with no usable intervals
   is rejected by :class:`~battery_optimizer.battery.Horizon` before it gets here.

Objective (minimise)
--------------------
* ``fixed``: ``sum over usable t of price[t] * (charge[t] - discharge[t])``.
  The constant ``sum(price * load)`` term cannot move the argmin, so it is
  dropped from the objective; the reported ``cost_eur`` re-adds it through the
  simulator.
* ``free``: the same dispatch term **plus** an explicit terminal-inventory
  valuation.  The end SOC carries energy that neither left the horizon nor was
  returned to the seed, so it must be worth something on the objective::

       ... - terminal_value * (soc[n-1] - initial_SOC)

  which is a single objective coefficient of ``-terminal_value`` on the last
  ``soc`` variable (the ``+ terminal_value * initial_SOC`` part is a constant and
  only re-enters when the reported bounds are formed).  ``terminal_value``
  (EUR/DC-kWh) must be supplied and finite; ``0.0`` is allowed and means "value
  the terminal inventory at zero" — a pure cash-flow objective, labelled
  "zero terminal-inventory value / cash-flow objective".

Optional full-charge day
------------------------
:class:`FullChargeModel` (``full_charge=`` of :func:`build_model` and
:func:`solve_oracle`; absent means the model above, byte for byte) adds, after the
``4n`` columns and the rows above, so no existing index moves:

* a **per-slot SOC ceiling** (``ub[soc[t]] = ceiling_kwh[t]``, never above the
  spec's own ``soc_max_frac``);
* a **checkpoint**: one binary ``z[k]`` per eligible slot with ``sum z = 1``, a
  continuous shortfall ``s >= 0`` and ``soc[t_k] + s >= target - M*(1 - z[k])``
  (``M = target - floor``), and ``+ penalty * s`` in the objective.  "SOC reaches the
  target at *some* slot of the window" is a disjunction; the binaries make it exact,
  and the soft slack makes the model feasible whatever the horizon (``s = 0``
  whenever the target is reachable);
* a convex **taper**: one half-space per slot from ``taper_first_slot``,
  ``charge[t] <= h*(p_tap + (P - p_tap)*(cap - soc[t-1])/(cap*(1 - x0)))``, a ramp
  from the full power ``P`` at ``x0`` down to ``p_tap`` at the top.

Optional high-SOC dwell cost
----------------------------
``high_soc_cost=(threshold_frac, eur_per_kwh_h)`` of :func:`build_model` and
:func:`solve_oracle` (``None`` or a zero cost: the model above, byte for byte) prices the
**time** spent above ``threshold_frac * capacity`` (LFP calendar aging is worse at high
SOC).  After every other column there is one continuous ``e[t] >= 0`` per slot, with the
row ``e[t] - soc[t] >= -threshold * capacity`` and ``+ cost * h * e[t]`` in the objective, so
the solver pays ``cost`` per kWh of end-of-slot SOC above the threshold per hour held.  It is
a planning convention like the throughput cost, not a fitted aging model.  The shortfall
penalty of a full-charge checkpoint must dominate it (``penalty > cost * horizon hours``,
checked), so the checkpoint is still reached whatever the dwell price; the dwell cost then
only decides *when* the plan climbs to the target, which is what makes it finish close to
the discharge.

Optional thermal charge-power soft cap
--------------------------------------
``charge_soft_cap=(threshold_kw, eur_per_kwh)`` of :func:`build_model` and :func:`solve_oracle`
(``None`` or a zero cost: the model above, byte for byte) prices charge **power** above a
threshold (LFP ageing is worse hot, and the battery's internal temperature rises with charge
power).  After every other column (the dwell columns included) there is one continuous
``x[t] >= 0`` per slot, with the row ``x[t] - charge[t] >= -threshold_kw * h`` and
``+ cost * x[t]`` in the objective: the solver pays ``cost`` for every AC kWh charged above
``threshold_kw`` in a slot, a convex piecewise-linear penalty on charge power, so spreading the
same charge over more slots is free.  Like the throughput and dwell costs it is a planning
convention, not a fitted thermal model; the shortfall penalty of a full-charge checkpoint must
dominate it (checked), so the target is still reached whatever the cap price.

Prices are pretax, and a uniform tax factor scales energy cost without changing
the argmin, so this optimum is also the tax-multiplied optimum.

Bound semantics
---------------
The MILP *minimises cost*, therefore:

* the incumbent feasible cost is an **upper bound on optimal cost**, hence a
  **lower bound on optimal savings** versus the baseline;
* the dual bound (``mip_dual_bound``) is a **lower bound on optimal cost**, hence
  an **upper bound on savings**.

``solver_gap`` is the relative gap ``(primal - dual) / max(|primal|, |dual|)``.
"Optimal" means *within the configured tolerance*, never literally zero gap: if
HiGHS reports optimality but the observed gap exceeds ``mip_rel_gap`` /
``mip_abs_gap``, the result is demoted to ``solver_status="feasible"``,
``bound_kind="upper"``, ``is_certified=False``, keeping the feasible cost.  A
gapped solution is never presented as a proven optimum.  On ``time_limit_s`` the
best incumbent comes back as ``solver_status="time_limit"`` with
``is_certified=False``; if no incumbent was found the call raises rather than
returning garbage.  The ``Schedule`` reports ``cost_upper_bound_eur`` (the
incumbent's own total cost) and ``cost_lower_bound_eur`` (the dual bound shifted
onto the reported-cost basis: ``load_term + terminal_value*initial_SOC + dual``,
or ``None`` when no dual was reported).

Every returned schedule is re-traced and re-validated through
:func:`battery_optimizer.battery.simulate`, so the oracle's model and the
simulator cannot silently disagree — with full terminal enforcement in ``fixed``
mode, and with the terminal rule relaxed in ``free`` mode (where the plan
legitimately ends off-seed and the value is carried by the objective instead).
Idle self-discharge is not modelled anywhere in this module (a documented
assumption, not a measured property).
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, replace

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_array

from .battery import (
    _TOL_KWH,
    BatteryPhysicsError,
    BatterySpec,
    Horizon,
    Schedule,
    _denoise,
    simulate,
    validate_spec,
)

__all__ = [
    "FullChargeModel",
    "OracleError",
    "OracleInfeasibleError",
    "OracleNoIncumbentError",
    "build_model",
    "checkpoint_outcome",
    "classify_result",
    "charge_soft_cap_excess_kwh",
    "high_soc_dwell_kwh_h",
    "solve_oracle",
    "validate_charge_soft_cap",
    "validate_high_soc_cost",
]

# HiGHS exit statuses surfaced by ``scipy.optimize.milp``.
_STATUS_OPTIMAL = 0
_STATUS_LIMIT = 1
_STATUS_INFEASIBLE = 2
_STATUS_UNBOUNDED = 3

# Terminal-inventory accounting modes.  ``fixed`` is the historical, neutral
# default (return to seed, or an explicit terminal target); ``free`` lets the
# end SOC float within [initial, max] and prices the leftover in the objective.
_TERMINAL_FIXED = "fixed"
_TERMINAL_FREE = "free"
_TERMINAL_MODES = (_TERMINAL_FIXED, _TERMINAL_FREE)


class OracleError(RuntimeError):
    """The oracle could not produce a usable schedule."""


class OracleInfeasibleError(OracleError):
    """No physically feasible schedule exists for this spec and horizon.

    Raised for a spec whose terminal SOC the power limit and the per-interval
    charging headroom together cannot reach inside the horizon, or an inverted
    SOC window caught earlier by :func:`battery_optimizer.battery.validate_spec`.
    A measured load that alone exceeds the configured ceiling is *not* an
    infeasibility — the kernel reports it as an observation and the problem
    stays feasible (module docstring, invariant 7).
    """


class OracleNoIncumbentError(OracleError):
    """The solver stopped before finding any feasible schedule (e.g. timeout).

    Distinct from infeasibility: a feasible schedule may exist but was not found
    within ``time_limit_s``.  Retry with a longer limit.
    """


# ---------------------------------------------------------------------------
# Variable indexing
# ---------------------------------------------------------------------------


def _charge_idx(t: int) -> int:
    return 3 * t


def _discharge_idx(t: int) -> int:
    return 3 * t + 1


def _mode_idx(t: int) -> int:
    return 3 * t + 2


def _soc_idx(n: int, t: int) -> int:
    return 3 * n + t


def _charge_slice(n: int) -> slice:
    return slice(0, 3 * n, 3)


def _discharge_slice(n: int) -> slice:
    return slice(1, 3 * n, 3)


def _check_terminal_mode(terminal_mode: str) -> None:
    if terminal_mode not in _TERMINAL_MODES:
        raise ValueError(f"terminal_mode must be one of {_TERMINAL_MODES}, got {terminal_mode!r}")


def _validate_free_spec(spec: BatterySpec, terminal_value_eur_per_kwh) -> None:
    """Reject a ``free``-mode oracle whose terminal accounting is not defensible.

    A free plan leaves the end SOC un-pinned, so it must not also smuggle in an
    arbitrary terminal *target*, and it must state an explicit valuation for the
    leftover inventory (``0.0`` allowed: a pure cash-flow objective).
    """
    if terminal_value_eur_per_kwh is None:
        raise ValueError(
            "terminal_mode='free' requires an explicit terminal_value_eur_per_kwh "
            "(0.0 is allowed for a zero terminal-inventory-value / cash-flow objective)"
        )
    if not math.isfinite(float(terminal_value_eur_per_kwh)):
        raise ValueError(
            f"terminal_value_eur_per_kwh must be finite, got {terminal_value_eur_per_kwh!r}"
        )
    target = spec.terminal_soc_frac
    if target is not None and not math.isclose(
        float(target), float(spec.initial_soc_frac), rel_tol=1e-12, abs_tol=1e-12
    ):
        raise ValueError(
            "terminal_mode='free' forbids an arbitrary terminal target: "
            f"terminal_soc_frac={target} is neither None nor initial_soc_frac="
            f"{spec.initial_soc_frac}; the leftover inventory is valued, not pinned"
        )


# ---------------------------------------------------------------------------
# Full-charge day (optional model extension)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FullChargeModel:
    """A day on which the SOC ceiling is lifted and a soft checkpoint is required.

    ``ceiling_kwh`` is the per-slot SOC ceiling (one entry per slot; each at most the
    spec's ``soc_max_frac`` capacity, so the spec must already carry the lifted
    maximum).  ``checkpoint_slots`` are the slots at whose **end** the SOC must reach
    ``target_kwh``, the optimizer choosing which; empty means a lifted ceiling only.
    ``shortfall_penalty_eur_per_kwh`` prices the unmet part of the target.  The taper
    rows apply from ``taper_first_slot`` on when ``taper_start_soc_frac`` is not
    ``None``: ``taper_power_kw`` is the charge power at 100%.
    """

    ceiling_kwh: tuple[float, ...]
    checkpoint_slots: tuple[int, ...]
    target_kwh: float
    shortfall_penalty_eur_per_kwh: float
    taper_start_soc_frac: float | None = None
    taper_power_kw: float = 0.0
    taper_first_slot: int = 0


def _validate_full_charge(
    fc: FullChargeModel, spec: BatterySpec, n: int, floor_kwh: float, ceil_kwh: float
) -> None:
    if len(fc.ceiling_kwh) != n:
        raise ValueError(f"full_charge.ceiling_kwh needs {n} entries, got {len(fc.ceiling_kwh)}")
    ceiling = np.asarray(fc.ceiling_kwh, dtype=float)
    if not np.all(np.isfinite(ceiling)) or ceiling.max() > ceil_kwh + _TOL_KWH:
        raise ValueError("full_charge.ceiling_kwh must be finite and within the spec's SOC window")
    if ceiling.min() < floor_kwh - _TOL_KWH:
        raise ValueError("full_charge.ceiling_kwh must not fall below the SOC floor")
    if fc.checkpoint_slots:
        slots = fc.checkpoint_slots
        if len(set(slots)) != len(slots) or min(slots) < 0 or max(slots) >= n:
            raise ValueError(f"full_charge.checkpoint_slots must be distinct slots in [0, {n})")
        if not (math.isfinite(fc.target_kwh) and fc.target_kwh > floor_kwh):
            raise ValueError("full_charge.target_kwh must be finite and above the SOC floor")
        if ceiling[list(slots)].min() < fc.target_kwh - _TOL_KWH:
            raise ValueError("full_charge.ceiling_kwh is below the target at a checkpoint slot")
        if not (
            math.isfinite(fc.shortfall_penalty_eur_per_kwh)
            and fc.shortfall_penalty_eur_per_kwh > 0.0
        ):
            raise ValueError("full_charge.shortfall_penalty_eur_per_kwh must be positive")
    if fc.taper_start_soc_frac is not None:
        if not 0.5 < fc.taper_start_soc_frac < 1.0:
            raise ValueError("full_charge.taper_start_soc_frac must be in (0.5, 1)")
        if not 0.0 < fc.taper_power_kw <= spec.power_kw:
            raise ValueError("full_charge.taper_power_kw must be in (0, spec.power_kw]")
        if not 0 <= fc.taper_first_slot <= n:
            raise ValueError(f"full_charge.taper_first_slot must be in [0, {n}]")


def checkpoint_outcome(
    schedule: Schedule, fc: FullChargeModel, *, tol: float = _TOL_KWH
) -> tuple[int | None, float]:
    """``(checkpoint slot, shortfall kWh)`` read back from a solved plan.

    The checkpoint is the first eligible slot whose end SOC reaches the target
    (shortfall 0), else the eligible slot with the highest SOC (the plan got as high
    as it could).  ``(None, 0.0)`` when the model has no checkpoint.
    """
    if not fc.checkpoint_slots:
        return None, 0.0
    slots = sorted(fc.checkpoint_slots)
    soc = np.asarray(schedule.soc_kwh, dtype=float)[slots]
    reached = np.flatnonzero(soc >= fc.target_kwh - tol)
    if reached.size:
        return slots[int(reached[0])], 0.0
    best = int(np.argmax(soc))
    return slots[best], float(fc.target_kwh - soc[best])


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


def validate_high_soc_cost(high_soc_cost) -> tuple[float, float] | None:
    """Validate ``high_soc_cost`` and return ``(threshold_frac, cost)``, or ``None`` when inert."""
    if high_soc_cost is None:
        return None
    try:
        threshold, cost = high_soc_cost
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"high_soc_cost must be (threshold_frac, eur_per_kwh_h) or None, got {high_soc_cost!r}"
        ) from exc
    if isinstance(threshold, bool) or isinstance(cost, bool):
        raise ValueError(f"high_soc_cost must hold numbers, got bool in {high_soc_cost!r}")
    try:
        threshold, cost = float(threshold), float(cost)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"high_soc_cost must hold numbers, got {high_soc_cost!r}") from exc
    if not (math.isfinite(threshold) and 0.0 < threshold < 1.0):
        raise ValueError(f"high_soc_cost threshold_frac must be in (0, 1), got {threshold!r}")
    if not math.isfinite(cost) or cost < 0.0:
        raise ValueError(f"high_soc_cost eur_per_kwh_h must be finite and >= 0, got {cost!r}")
    return (threshold, cost) if cost > 0.0 else None


def validate_charge_soft_cap(charge_soft_cap) -> tuple[float, float] | None:
    """Validate ``charge_soft_cap`` and return ``(threshold_kw, cost)``, or ``None`` when inert."""
    if charge_soft_cap is None:
        return None
    try:
        threshold, cost = charge_soft_cap
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"charge_soft_cap must be (threshold_kw, eur_per_kwh) or None, got {charge_soft_cap!r}"
        ) from exc
    if isinstance(threshold, bool) or isinstance(cost, bool):
        raise ValueError(f"charge_soft_cap must hold numbers, got bool in {charge_soft_cap!r}")
    try:
        threshold, cost = float(threshold), float(cost)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"charge_soft_cap must hold numbers, got {charge_soft_cap!r}") from exc
    if not (math.isfinite(threshold) and threshold > 0.0):
        raise ValueError(f"charge_soft_cap threshold_kw must be finite and > 0, got {threshold!r}")
    if not math.isfinite(cost) or cost < 0.0:
        raise ValueError(f"charge_soft_cap eur_per_kwh must be finite and >= 0, got {cost!r}")
    return (threshold, cost) if cost > 0.0 else None


def charge_soft_cap_excess_kwh(
    schedule: Schedule, interval_seconds: float, threshold_kw: float
) -> float:
    """AC kWh charged above ``threshold_kw`` (per slot) in a solved plan.

    The quantity the soft cap multiplies: the plan's cap cost is ``eur_per_kwh`` times this,
    recoverable from the schedule because the reported ``cost_eur`` excludes it.
    """
    charge = np.asarray(schedule.charge_kwh, dtype=float)
    over = np.maximum(charge - threshold_kw * interval_seconds / 3600.0, 0.0)
    return float(over.sum())


def high_soc_dwell_kwh_h(
    schedule: Schedule, spec: BatterySpec, interval_seconds: float, threshold_frac: float
) -> float:
    """kWh*h of end-of-slot SOC above ``threshold_frac`` of capacity in a solved plan.

    The quantity the dwell cost multiplies: the plan's dwell cost is
    ``eur_per_kwh_h * high_soc_dwell_kwh_h(...)``, recoverable from the schedule because the
    reported ``cost_eur`` excludes it.
    """
    soc = np.asarray(schedule.soc_kwh, dtype=float)
    excess = np.maximum(soc - threshold_frac * spec.capacity_kwh, 0.0)
    return float(excess.sum() * interval_seconds / 3600.0)


def build_model(
    spec: BatterySpec,
    horizon: Horizon,
    *,
    terminal_mode: str = _TERMINAL_FIXED,
    terminal_value_eur_per_kwh: float | None = None,
    floor_kwh: float | None = None,
    throughput_cost_eur_per_kwh: float = 0.0,
    full_charge: FullChargeModel | None = None,
    high_soc_cost: tuple[float, float] | None = None,
    charge_soft_cap: tuple[float, float] | None = None,
    discharge_margin_kw: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, Bounds, list[LinearConstraint]]:
    """Assemble the MILP: ``(objective, integrality, bounds, constraints)``.

    Exposed for tests and diagnostics: the layout can be inspected without
    solving, and ``c @ x`` can be checked against a reported ``fun``.

    ``terminal_mode`` selects the SOC-window floor and the terminal-SOC
    treatment documented at the top of this module (``"fixed"`` pins the last
    SOC, ``"free"`` lets it float within ``[initial, max]`` and adds a
    ``-terminal_value`` objective coefficient on the last SOC variable).  The
    default is ``"fixed"`` so the historical neutral model is byte-for-byte
    unchanged.

    ``floor_kwh`` overrides the SOC floor.  ``None`` — the default, and the only
    value the whole-horizon oracle passes — is exactly the historical behaviour:
    ``max(soc_min, initial SOC)`` in free mode, the bare window floor in fixed
    mode, row for row as certified.  A **rolling** policy needs to pass the floor
    of the horizon it belongs to rather than the floor implied by the day it is
    solving, because seed protection is a property of the window: what may not be
    spent is the charge the window was handed when it opened, and everything bought
    after that is the policy's to spend, including across a day boundary.  Without
    the knob, reusing this assembly for a daily solve whose opening SOC is the
    carried value silently re-classifies an earlier day's purchased charge as
    unspendable seed — which freezes a filled battery at the ceiling and is a
    functional bug, not a conservatism.  See
    :func:`battery_optimizer.policies.policy_day_ahead`, which uses it.

    The single constraint block has ``4n`` rows — two mutual-exclusion rows, one
    SOC recurrence row and one no-export row per interval — plus ``n`` more rows
    when :attr:`battery_optimizer.battery.Horizon.import_limit_kw` is configured
    (the charging-side headroom row ``charge[t] <= max(0, ceiling - load)``).

    ``throughput_cost_eur_per_kwh`` (default ``0.0``) adds a per-kWh penalty on AC
    energy throughput — the sum of charge and discharge at the meter — to the
    objective.  It is an explicit economic convention (degradation valued as
    proportional to energy moved), not a physical aging model.  With the split
    charge/discharge variables the penalty is linear: ``+tp`` on every charge
    coefficient and ``+tp`` on every discharge coefficient, so the solver sees
    ``minimise sum((price + tp) * charge - (price - tp) * discharge)``.  A
    non-zero value raises the effective charge price and lowers the effective
    discharge price by the same amount, making marginal arbitrage less attractive
    without changing the constraint set.  ``0.0`` is byte-identical to the
    historical model.

    ``full_charge`` (default ``None``: the model above, byte for byte) appends the
    per-slot ceiling, the checkpoint binaries and shortfall slack, and the taper rows
    of :class:`FullChargeModel`.  The extra columns come last (``z`` for each
    checkpoint slot, then the shortfall ``s``) and the extra rows after every
    historical row, so no existing index moves.

    ``high_soc_cost`` (default ``None``: the model above, byte for byte) is
    ``(threshold_frac, eur_per_kwh_h)``; a positive cost appends one continuous excess
    column ``e[t]`` per slot (after the full-charge columns, so ``z`` and the shortfall keep
    their indices), the row ``e[t] - soc[t] >= -threshold * capacity`` after every other row,
    and ``cost * h`` on each ``e[t]``.  See the module section "Optional high-SOC dwell cost".

    ``charge_soft_cap`` (default ``None``: byte for byte) is ``(threshold_kw, eur_per_kwh)``; a
    positive cost appends one continuous column ``x[t]`` per slot (after the dwell columns),
    the row ``x[t] - charge[t] >= -threshold_kw * h`` after every other row, and ``cost`` on each
    ``x[t]``.  See the module section "Optional thermal charge-power soft cap".

    ``discharge_margin_kw`` (default ``0.0``: byte for byte) tightens the no-export row to
    ``discharge[t] - charge[t] <= max(0, load[t] - discharge_margin_kw * h)``; no row or column is
    added, only that right-hand side moves.  Must be finite and non-negative.
    """
    _check_terminal_mode(terminal_mode)
    free = terminal_mode == _TERMINAL_FREE
    if isinstance(discharge_margin_kw, bool) or not math.isfinite(discharge_margin_kw):
        raise ValueError(
            f"discharge_margin_kw must be a finite number, got {discharge_margin_kw!r}"
        )
    if discharge_margin_kw < 0.0:
        raise ValueError(f"discharge_margin_kw must be >= 0, got {discharge_margin_kw}")
    margin_kwh = float(discharge_margin_kw) * horizon.interval_seconds / 3600.0

    if isinstance(throughput_cost_eur_per_kwh, bool):
        raise ValueError(
            f"throughput_cost_eur_per_kwh must be a number, got bool "
            f"{throughput_cost_eur_per_kwh!r}"
        )
    if not math.isfinite(throughput_cost_eur_per_kwh):
        raise ValueError(
            f"throughput_cost_eur_per_kwh must be finite, got {throughput_cost_eur_per_kwh!r}"
        )
    if throughput_cost_eur_per_kwh < 0.0:
        raise ValueError(
            f"throughput_cost_eur_per_kwh must be >= 0, got {throughput_cost_eur_per_kwh}"
        )
    tp = float(throughput_cost_eur_per_kwh)

    dwell = validate_high_soc_cost(high_soc_cost)
    n = horizon.n
    n_checkpoints = 0 if full_charge is None else len(full_charge.checkpoint_slots)
    n_fc_cols = n_checkpoints + 1 if n_checkpoints else 0
    e0 = 4 * n + n_fc_cols
    cap = validate_charge_soft_cap(charge_soft_cap)
    x0 = e0 + (n if dwell is not None else 0)
    n_vars = x0 + (n if cap is not None else 0)
    limit = spec.interval_energy_limit_kwh(horizon.interval_seconds)
    # Free mode shares the causal floor (cannot spend the seed); fixed mode uses
    # the bare window floor and pins the terminal SOC instead.  An explicit
    # floor_kwh replaces both, and is validated against the physical window below.
    if floor_kwh is None:
        bound_kwh = spec.causal_floor_kwh() if free else spec.soc_bound_kwh("min")
    else:
        bound_kwh = float(floor_kwh)
        physical_min = spec.soc_bound_kwh("min")
        if not math.isfinite(bound_kwh):
            raise ValueError(f"floor_kwh must be finite kWh, got {floor_kwh!r}")
        if bound_kwh < physical_min - _TOL_KWH:
            raise ValueError(
                f"floor_kwh={bound_kwh} is below the physical SOC window floor "
                f"{physical_min}: no floor may authorise energy that does not exist"
            )
    floor_kwh = bound_kwh
    if floor_kwh > spec.soc_bound_kwh("max") + _TOL_KWH:
        raise ValueError(
            f"floor_kwh={floor_kwh} is above the physical SOC ceiling "
            f"{spec.soc_bound_kwh('max')}: the variable bounds would be empty"
        )
    ceil_kwh = spec.soc_bound_kwh("max")
    if full_charge is not None:
        _validate_full_charge(full_charge, spec, horizon.n, floor_kwh, ceil_kwh)
        horizon_h = horizon.n * horizon.interval_seconds / 3600.0
        if (
            dwell is not None
            and full_charge.checkpoint_slots
            and full_charge.shortfall_penalty_eur_per_kwh <= dwell[1] * horizon_h
        ):
            raise ValueError(
                "full_charge.shortfall_penalty_eur_per_kwh must exceed the high-SOC dwell cost "
                f"over the horizon ({dwell[1] * horizon_h:.6g} EUR/kWh): the checkpoint must "
                "dominate the dwell cost"
            )
        if cap is not None and full_charge.checkpoint_slots:
            # One more stored kWh costs at most 1/eta_c AC kWh above the threshold.
            planning_price = (0.0 if dwell is None else dwell[1] * horizon_h) + (
                cap[1] / spec.charge_efficiency
            )
            if full_charge.shortfall_penalty_eur_per_kwh <= planning_price:
                raise ValueError(
                    "full_charge.shortfall_penalty_eur_per_kwh must exceed the high-SOC dwell "
                    "cost over the horizon plus the charge soft-cap cost per stored kWh "
                    f"({planning_price:.6g} EUR/kWh): the checkpoint must dominate the soft cap"
                )
    soc_init = spec.soc_bound_kwh("init")
    terminal_kwh = spec.soc_bound_kwh("terminal")
    eta_c = spec.charge_efficiency
    eta_d = spec.discharge_efficiency
    usable = horizon.usable

    t_of = np.arange(n)
    c_idx = 3 * t_of
    d_idx = 3 * t_of + 1
    y_idx = 3 * t_of + 2
    s_idx = 3 * n + t_of

    # Objective: minimise sum(usable price * (charge - discharge)), and in free
    # mode credit the terminal inventory at terminal_value EUR/DC-kWh (the
    # +terminal_value*soc_init constant is dropped from the objective and
    # re-added when the reported bounds are formed).  A non-zero throughput_cost
    # adds tp to both charge and discharge coefficients, penalising AC energy
    # throughput (charge + discharge) without changing the constraint set.
    obj = np.zeros(n_vars)
    priced = np.where(usable, horizon.prices, 0.0)
    obj[c_idx] = priced + tp
    obj[d_idx] = -priced + tp
    if free and terminal_value_eur_per_kwh is not None:
        obj[_soc_idx(n, n - 1)] = -float(terminal_value_eur_per_kwh)

    lb = np.zeros(n_vars)
    ub = np.full(n_vars, np.inf)
    integrality = np.zeros(n_vars)

    ub[c_idx] = limit
    ub[d_idx] = limit
    ub[y_idx] = 1.0
    integrality[y_idx] = 1.0
    lb[s_idx] = floor_kwh
    ub[s_idx] = ceil_kwh

    # Gap-excluded intervals cannot move energy at all.
    blocked = np.flatnonzero(~usable)
    if blocked.size:
        ub[c_idx[blocked]] = 0.0
        ub[d_idx[blocked]] = 0.0

    # Fixed mode pins the last interval's SOC variable; free mode leaves it
    # bounded by the raised floor and the ceiling (i.e. [initial, max]).
    if not free:
        lb[s_idx[-1]] = terminal_kwh
        ub[s_idx[-1]] = terminal_kwh

    if full_charge is not None:
        ub[s_idx] = np.minimum(ub[s_idx], np.asarray(full_charge.ceiling_kwh, dtype=float))

    rows_i: list[int] = []
    rows_j: list[int] = []
    rows_v: list[float] = []
    con_lb: list[float] = []
    con_ub: list[float] = []

    def add_row(coefs: list[tuple[int, float]], lo: float, hi: float) -> None:
        k = len(con_lb)
        for j, v in coefs:
            rows_i.append(k)
            rows_j.append(j)
            rows_v.append(v)
        con_lb.append(lo)
        con_ub.append(hi)

    for t in range(n):
        # 1. mutual exclusion (binary mode)
        add_row([(_charge_idx(t), 1.0), (_mode_idx(t), -limit)], -np.inf, 0.0)
        add_row([(_discharge_idx(t), 1.0), (_mode_idx(t), limit)], -np.inf, limit)

    for t in range(n):
        # 2. SOC recurrence
        coefs = [
            (_soc_idx(n, t), 1.0),
            (_charge_idx(t), -eta_c),
            (_discharge_idx(t), 1.0 / eta_d),
        ]
        if t == 0:
            add_row(coefs, soc_init, soc_init)
        else:
            coefs.append((_soc_idx(n, t - 1), -1.0))
            add_row(coefs, 0.0, 0.0)

    for t in range(n):
        # 5. no intentional export
        export_rhs = float(horizon.load_kwh[t])
        if margin_kwh > 0.0:
            export_rhs = max(0.0, export_rhs - margin_kwh)
        add_row([(_discharge_idx(t), 1.0), (_charge_idx(t), -1.0)], -np.inf, export_rhs)

    # 6. site import ceiling — omitted entirely when the ceiling is unknown, so
    # an unconfigured site keeps 4n rows and an unconstrained import side.  The
    # row bounds the *charging decision* by the headroom the load leaves:
    # charge[t] <= max(0, ceiling*h - load[t]).  A load that already meets or
    # exceeds the ceiling gives a zero right-hand side and simply forces
    # charge[t] = 0; the problem stays feasible, and the kernel reports those
    # intervals as an observation (Schedule.ceiling_exceeding_intervals).
    # Discharge is not part of this row — it lowers the draw and stays bounded
    # by the no-export rule alone, so the oracle may discharge to shave a peak.
    headroom_kwh = horizon.charge_headroom_kwh
    if headroom_kwh is not None:
        for t in range(n):
            add_row(
                [(_charge_idx(t), 1.0)],
                -np.inf,
                float(headroom_kwh[t]),
            )

    if full_charge is not None:
        _add_full_charge(
            full_charge,
            spec,
            horizon,
            n_checkpoints=n_checkpoints,
            floor_kwh=floor_kwh,
            soc_init=soc_init,
            add_row=add_row,
            obj=obj,
            ub=ub,
            integrality=integrality,
        )

    if dwell is not None:
        threshold_kwh = dwell[0] * spec.capacity_kwh
        h = horizon.interval_seconds / 3600.0
        e_cols = e0 + t_of
        ub[e_cols] = max(ceil_kwh - threshold_kwh, 0.0)
        obj[e_cols] = dwell[1] * h
        for t in range(n):
            add_row([(e0 + t, 1.0), (_soc_idx(n, t), -1.0)], -threshold_kwh, np.inf)

    if cap is not None:
        cap_kwh = cap[0] * horizon.interval_seconds / 3600.0
        x_cols = x0 + t_of
        ub[x_cols] = max(limit - cap_kwh, 0.0)
        obj[x_cols] = cap[1]
        for t in range(n):
            add_row([(x0 + t, 1.0), (_charge_idx(t), -1.0)], -cap_kwh, np.inf)

    a = coo_array((rows_v, (rows_i, rows_j)), shape=(len(con_lb), n_vars), dtype=float)
    constraints = [LinearConstraint(a, np.asarray(con_lb), np.asarray(con_ub))]
    return obj, integrality, Bounds(lb, ub), constraints


def _add_full_charge(
    fc: FullChargeModel,
    spec: BatterySpec,
    horizon: Horizon,
    *,
    n_checkpoints: int,
    floor_kwh: float,
    soc_init: float,
    add_row,
    obj: np.ndarray,
    ub: np.ndarray,
    integrality: np.ndarray,
) -> None:
    """Fill the checkpoint columns and append the checkpoint and taper rows."""
    n = horizon.n
    if n_checkpoints:
        z0 = 4 * n
        s_col = z0 + n_checkpoints
        ub[z0:s_col] = 1.0
        integrality[z0:s_col] = 1.0
        ub[s_col] = fc.target_kwh
        obj[s_col] = fc.shortfall_penalty_eur_per_kwh
        big_m = fc.target_kwh - floor_kwh
        add_row([(z0 + k, 1.0) for k in range(n_checkpoints)], 1.0, 1.0)
        for k, t in enumerate(fc.checkpoint_slots):
            add_row(
                [(_soc_idx(n, t), 1.0), (s_col, 1.0), (z0 + k, -big_m)],
                fc.target_kwh - big_m,
                np.inf,
            )
    if fc.taper_start_soc_frac is not None:
        h = horizon.interval_seconds / 3600.0
        cap = spec.capacity_kwh
        slope = (spec.power_kw - fc.taper_power_kw) * h / (cap * (1.0 - fc.taper_start_soc_frac))
        rhs = h * fc.taper_power_kw + slope * cap
        for t in range(fc.taper_first_slot, n):
            if t == 0:
                add_row([(_charge_idx(t), 1.0)], -np.inf, rhs - slope * soc_init)
            else:
                add_row([(_charge_idx(t), 1.0), (_soc_idx(n, t - 1), slope)], -np.inf, rhs)


def _solver_options(
    time_limit_s: float, mip_rel_gap: float, mip_abs_gap: float
) -> dict[str, float]:
    """HiGHS options.

    ``mip_abs_gap`` is honoured by HiGHS but is not in scipy's recognised option
    list, which warns about it; the warning is emitted by ``milp`` and is
    suppressed by the caller.
    """
    return {
        "time_limit": float(time_limit_s),
        "mip_rel_gap": float(mip_rel_gap),
        "mip_abs_gap": float(mip_abs_gap),
    }


def classify_result(
    status: int,
    message: str,
    primal: float,
    dual: float | None,
    *,
    mip_rel_gap: float,
    mip_abs_gap: float,
) -> tuple[str, str, bool, float]:
    """Turn solver telemetry into ``(solver_status, bound_kind, is_certified, gap)``.

    ``primal`` is the incumbent objective (dispatch cost minus the constant load
    term, and minus the dropped ``terminal_value * initial_SOC`` constant in free
    mode) and ``dual`` the LP/dual bound HiGHS reports alongside it.  Since the
    model *minimises cost*:

    * ``primal`` is an **upper bound on optimal cost** ⇒ a **lower bound on
      optimal savings**;
    * ``dual`` is a **lower bound on optimal cost** ⇒ an **upper bound on
      savings**.

    Certification requires HiGHS to report optimality *and* the observed gap to
    be inside the configured tolerance — "optimal" means "within tolerance",
    never literal zero gap.  Any other outcome keeps the feasible incumbent and
    labels it ``bound_kind="upper"`` with ``is_certified=False``, so a gapped or
    time-limited result can never be mistaken for a proven optimum.

    The gap is reported relative to the dispatch objective; the constant
    ``sum(price * load)`` term (and the free-mode ``terminal_value * initial_SOC``
    constant) cancels out of ``primal - dual``, so the absolute gap in EUR is the
    same on either basis.
    """
    abs_gap = math.inf if dual is None else max(float(primal) - float(dual), 0.0)
    denom = max(abs(float(primal)), 0.0 if dual is None else abs(float(dual)))
    rel_gap = 0.0 if abs_gap == 0.0 else (abs_gap / denom if denom > 0.0 else abs_gap)
    within_tolerance = abs_gap <= mip_abs_gap or rel_gap <= mip_rel_gap

    if status == _STATUS_OPTIMAL and within_tolerance:
        return "optimal", "certified", True, rel_gap
    if status == _STATUS_LIMIT and "time limit" in message.lower():
        return "time_limit", "upper", False, rel_gap
    return "feasible", "upper", False, rel_gap


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _load_cost_term(horizon: Horizon) -> float:
    """The constant ``sum(price * load)`` the dispatch objective omits."""
    mask = horizon.usable
    if not mask.any():
        return 0.0
    return float(np.dot(horizon.prices[mask], horizon.load_kwh[mask]))


def solve_oracle(
    spec: BatterySpec,
    horizon: Horizon,
    *,
    time_limit_s: float = 60.0,
    mip_rel_gap: float = 1e-4,
    mip_abs_gap: float = 1e-6,
    terminal_mode: str = _TERMINAL_FIXED,
    terminal_value_eur_per_kwh: float | None = None,
    floor_kwh: float | None = None,
    throughput_cost_eur_per_kwh: float = 0.0,
    full_charge: FullChargeModel | None = None,
    high_soc_cost: tuple[float, float] | None = None,
    charge_soft_cap: tuple[float, float] | None = None,
    discharge_margin_kw: float = 0.0,
) -> Schedule:
    """Cost-minimising dispatch over the whole horizon with perfect information.

    Builds the MILP documented at the top of this module and solves it with
    ``scipy.optimize.milp`` (bundled HiGHS).  The returned schedule is physically
    feasible (re-checked by :func:`battery_optimizer.battery.simulate`);
    ``solver_status`` / ``solver_gap`` / ``bound_kind`` / ``is_certified`` say how
    much of it is proven.

    Args:
        spec: Battery physical parameters.
        horizon: Prices, gross load, the usable mask and the optional
            ``import_limit_kw`` ceiling for the horizon.  The re-trace below
            checks the same charging-headroom ceiling, so the model and the
            kernel cannot disagree about it.
        time_limit_s: Wall-clock limit handed to HiGHS.  On expiry the best
            incumbent is returned with ``solver_status="time_limit"`` and
            ``is_certified=False``; with no incumbent found,
            :class:`OracleNoIncumbentError` is raised instead.
        mip_rel_gap: Relative optimality tolerance for certification.
        mip_abs_gap: Absolute (EUR) optimality tolerance for certification.
        terminal_mode: ``"fixed"`` (default) requires the end SOC to equal the
            spec's terminal target (the seed when ``terminal_soc_frac is None``)
            and is the historical neutral behaviour.  ``"free"`` lets the end SOC
            float within ``[initial SOC, max]``, floors every step at the shared
            causal floor ``max(soc_min, initial)``, and values the leftover
            inventory in the objective — the framing the benchmark uses so the
            oracle and the open causal cycle sit on one common feasible set.
        terminal_value_eur_per_kwh: EUR/DC-kWh value of the terminal inventory.
            Required (finite; ``0.0`` allowed) when ``terminal_mode="free"``, and
            must be ``None`` when ``terminal_mode="fixed"`` (a fixed cycle
            returns to its seed, so there is nothing to value).
        floor_kwh: Override for the ``"free"`` mode SOC floor.  Left ``None`` by the
            whole-horizon oracle, which is then exactly the certified behaviour —
            ``max(soc_min, initial SOC)``, the shared causal floor.  A rolling day-ahead
            solve passes its **horizon's** seed floor here so that the day's opening SOC
            does not become unspendable seed; see :func:`build_model`.
        throughput_cost_eur_per_kwh: Per-kWh penalty on AC energy throughput
            (``sum(charge) + sum(discharge)``) added to the MILP objective.  An explicit
            economic convention — degradation valued as proportional to energy moved at
            the meter — not a physical aging model.  ``0.0`` (the default) is byte-identical
            to the historical model.  Must be finite and non-negative.  The penalty
            raises the effective charge price and lowers the effective discharge price by
            the same amount, so marginal arbitrage becomes less attractive without
            changing the constraint set.  The reported ``cost_eur`` stays the meter bill
            plus inventory adjustment; the throughput penalty is recoverable as
            ``throughput_cost_eur_per_kwh * (sum(charge) + sum(discharge))``.
        full_charge: Optional :class:`FullChargeModel` (lifted per-slot ceiling, soft
            checkpoint, taper).  ``None`` is the historical model.  With a checkpoint the
            solver's objective includes the shortfall penalty, but the reported
            ``cost_eur`` is the plan's real cost (penalty excluded); the dual bound is
            discarded when the plan misses the target, because it then belongs to the
            penalised objective.  Read the checkpoint and shortfall back with
            :func:`checkpoint_outcome`.  The spec must already carry the lifted
            ``soc_max_frac`` (the kernel re-trace validates against it).
        high_soc_cost: Optional ``(threshold_frac, eur_per_kwh_h)`` dwell cost on SOC above
            the threshold (see "Optional high-SOC dwell cost" in the module docstring).
            ``None`` or a zero cost is the historical model.  Like the throughput cost it is
            a planning-only term: the reported ``cost_eur`` is the real cost (dwell
            excluded), recoverable as ``eur_per_kwh_h * high_soc_dwell_kwh_h(...)``, and the
            dual bound is discarded because it belongs to the penalised objective.  With a
            full-charge checkpoint, the shortfall penalty must exceed the dwell cost over
            the horizon (``ValueError`` otherwise).
        charge_soft_cap: Optional ``(threshold_kw, eur_per_kwh)`` soft cap on charge power (see
            "Optional thermal charge-power soft cap" in the module docstring): every AC kWh
            charged above ``threshold_kw`` in a slot costs ``eur_per_kwh``.  ``None`` or a zero
            cost is the historical model.  Planning-only like the dwell cost: the reported
            ``cost_eur`` excludes it (``eur_per_kwh * charge_soft_cap_excess_kwh(...)``
            recovers it), the dual bound is discarded, and with a full-charge checkpoint the
            shortfall penalty must exceed the dwell cost plus ``eur_per_kwh`` per stored kWh.
        discharge_margin_kw: Grid import (kW) the actuator keeps while discharging.  Tightens
            the no-export row to ``max(0, load - discharge_margin_kw * h)`` per interval so the
            plan only discharges what can be delivered.  ``0.0`` (the default) is the historical
            model, byte for byte.  The bounds describe the margin-bound problem the solver
            solved, not the margin-free one.  Must be finite and non-negative.

    Raises:
        ValueError: If *spec* is not physically meaningful, a tolerance or the
            time limit is not positive, ``terminal_mode`` is unknown, or the free
            mode's valuation / terminal target requirements are violated.
        OracleInfeasibleError: If no feasible schedule exists.
        OracleNoIncumbentError: If the solver stopped without any feasible plan.

    Returns:
        Schedule on the same pretax, usable-interval-only cost basis as the traced
        policies (no-battery, causal) and the rolling day-ahead one.  ``cost_eur`` is
        the **total objective**
        (``meter_cost_eur + inventory_adjustment_eur``), where the adjustment is
        ``-terminal_value * (terminal_soc_kwh - initial_soc_kwh)`` — zero for a
        fixed (or free-and-unvalued) plan — so ``oracle.savings_vs(no_battery)``
        and ``causal.savings_vs(no_battery)`` stay directly comparable.
    """
    validate_spec(spec)
    _check_terminal_mode(terminal_mode)
    if not math.isfinite(time_limit_s) or time_limit_s <= 0:
        raise ValueError(f"time_limit_s must be > 0, got {time_limit_s}")
    if mip_rel_gap <= 0 or mip_abs_gap <= 0:
        raise ValueError(
            f"mip_rel_gap and mip_abs_gap must be > 0, got {mip_rel_gap} and {mip_abs_gap}"
        )

    free = terminal_mode == _TERMINAL_FREE
    if free:
        _validate_free_spec(spec, terminal_value_eur_per_kwh)
    elif terminal_value_eur_per_kwh is not None:
        raise ValueError(
            "terminal_value_eur_per_kwh is only meaningful for terminal_mode='free'; "
            "a fixed cycle returns to its seed SOC and has no inventory to value"
        )
    value = float(terminal_value_eur_per_kwh) if free else 0.0

    obj, integrality, bounds, constraints = build_model(
        spec,
        horizon,
        terminal_mode=terminal_mode,
        terminal_value_eur_per_kwh=terminal_value_eur_per_kwh if free else None,
        floor_kwh=floor_kwh,
        throughput_cost_eur_per_kwh=throughput_cost_eur_per_kwh,
        full_charge=full_charge,
        high_soc_cost=high_soc_cost,
        charge_soft_cap=charge_soft_cap,
        discharge_margin_kw=discharge_margin_kw,
    )
    options = _solver_options(time_limit_s, mip_rel_gap, mip_abs_gap)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Unrecognized options detected", category=RuntimeWarning
        )
        result = milp(
            c=obj,
            integrality=integrality,
            bounds=bounds,
            constraints=constraints,
            options=options,
        )

    status = int(result.status)
    message = str(getattr(result, "message", "") or "")

    if status == _STATUS_INFEASIBLE:
        raise OracleInfeasibleError(f"oracle problem is infeasible: {message}")
    if status == _STATUS_UNBOUNDED:  # pragma: no cover - every flow is bounded above
        raise OracleInfeasibleError(
            f"oracle problem is unbounded (should be impossible): {message}"
        )
    primal = getattr(result, "fun", None)
    if result.x is None or primal is None:
        raise OracleNoIncumbentError(
            f"oracle found no feasible schedule (status={status}): {message}; "
            f"retry with a larger time_limit_s"
        )

    n = horizon.n
    x = np.asarray(result.x, dtype=float)
    charge = _denoise(x[_charge_slice(n)], _TOL_KWH)
    discharge = _denoise(x[_discharge_slice(n)], _TOL_KWH)

    # Bounds on the reported cost: the incumbent is an upper bound on optimal
    # cost (lower bound on savings), the dual bound the converse.  Certification
    # is "within tolerance", not literal zero gap; anything else stays a bound.
    dual = getattr(result, "mip_dual_bound", None)
    solver_status, bound_kind, is_certified, rel_gap = classify_result(
        status,
        message,
        float(primal),
        None if dual is None else float(dual),
        mip_rel_gap=mip_rel_gap,
        mip_abs_gap=mip_abs_gap,
    )

    # Re-trace through the simulator.  In fixed mode with *full* enforcement
    # (the model must satisfy exactly the invariants the policies are checked
    # against, terminal SOC included); in free mode the terminal rule is relaxed
    # because the plan legitimately ends off-seed and the leftover is priced in
    # the objective instead.  Either way, a kernel disagreement surfaces as a
    # solver problem rather than letting a physics error escape.
    try:
        traced = simulate(spec, horizon, charge, discharge, enforce_terminal_soc=not free)
    except BatteryPhysicsError as exc:
        raise OracleError(
            f"oracle returned a plan the battery kernel rejects (status={status}, "
            f"gap={rel_gap:.3g}): {exc}"
        ) from exc

    meter_cost = traced.cost_eur  # == metered energy cost (inventory 0 here)
    initial_kwh = spec.soc_bound_kwh("init")
    terminal_kwh = float(traced.soc_kwh[-1])
    inventory_adjustment = -value * (terminal_kwh - initial_kwh)
    total_cost = meter_cost + inventory_adjustment

    # Dual bound lives on the dispatch objective; shift it onto the reported-cost
    # basis with the constants the objective dropped (load term and, in free
    # mode, the terminal_value * initial_SOC offset).  value == 0 in fixed mode.
    #
    # HOWEVER: when throughput_cost > 0 (or a high-SOC dwell cost or a charge soft cap is active,
    # which price the plan the same way), the solver's primal/dual bounds are for
    # the **penalized** objective (meter_cost + throughput_penalty + inventory),
    # not the reported meter-cost objective (meter_cost + inventory).  Subtracting
    # the incumbent's throughput from the dual bound is NOT a valid lower bound
    # on the unpenalized meter-cost optimum — at the unpenalized optimum the
    # throughput may be higher, and the optimal unpenalized meter cost can lie
    # strictly below the incumbent's unpenalized cost.  The dual bound is
    # discarded (lower = None).
    #
    # The incumbent's unpenalized cost IS a valid (loose) upper bound on the
    # unpenalized optimum: the constraint set is unchanged by tp, so the
    # incumbent is feasible for the unpenalized problem and its cost is an upper
    # bound.  Keeping it avoids downstream damage (benchmark savings calculations
    # and policies.days_at_time_limit need a finite upper bound).
    upper: float | None = total_cost
    dwell_active = validate_high_soc_cost(high_soc_cost) is not None
    cap_active = validate_charge_soft_cap(charge_soft_cap) is not None
    if throughput_cost_eur_per_kwh > 0.0 or dwell_active or cap_active:
        lower = None
        # upper stays total_cost — the incumbent's unpenalized cost is a valid
        # (loose) upper bound; we discard only the dual bound because it belongs
        # to the penalized objective.
        solver_status = "feasible"
        bound_kind = "upper"
        is_certified = False
        rel_gap = None
    else:
        # Zero throughput cost: dual bound is valid for the reported cost.
        load_term = _load_cost_term(horizon)
        lower = None if dual is None else load_term + value * initial_kwh + float(dual)
    if (
        full_charge is not None
        and full_charge.checkpoint_slots
        and x[4 * n + len(full_charge.checkpoint_slots)] > _TOL_KWH
    ):
        # A missed target puts the penalty in the solver's objective: the dual bound
        # no longer bounds the reported (penalty-free) cost.
        lower = None

    return replace(
        traced,
        cost_eur=total_cost,
        meter_cost_eur=meter_cost,
        inventory_adjustment_eur=inventory_adjustment,
        terminal_value_eur_per_kwh=value,
        initial_soc_kwh=initial_kwh,
        terminal_soc_kwh=terminal_kwh,
        cost_lower_bound_eur=lower,
        cost_upper_bound_eur=upper,
        solver_time_limit_s=float(time_limit_s),
        solver_status=solver_status,
        solver_gap=rel_gap,
        bound_kind=bound_kind,
        is_certified=is_certified,
    )
