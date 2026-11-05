"""Offline receding-horizon replay of the price-blind comfort MPC (track B, step B1).

For every replayed day (``replay.seasons`` x ``replay.months``, complete local days) the MPC plans
the next ``horizon_hours`` every ``replan_every_minutes`` from the observer's state estimate.  The
house was run by the legacy controller, never by the MPC: **every planned kWh and every planned
comfort figure is a model prediction of a plan that was not executed, not a realised outcome.**

Inputs at a decision ``t0`` (slot index) are causal, except what is declared:

* the state estimate uses measurements of slots before ``t0`` only (``thermal.observe``);
* the space-heating run in progress (minimum on/off carry-in) is read from the measured slots
  before ``t0`` (DHW slots skipped, as in the MILP);
* the weekly re-estimation (``model.reestimate``) uses days before ``t0`` only.

**Leakage, declared:**

* the season prior is fitted on the *other* season: an S3 replay reads an S2 prior (earlier), but
  an **S2 replay is scored with an S3-fitted prior, i.e. a later season's data**; the heat law,
  the room offsets and the COP table come from the same prior season.  ``model.kind: c2`` uses
  C2's S2 prior for both seasons, so **its S2 run is in-sample**;
* with ``replay.weather = perfect`` the outdoor temperature over the horizon is ``t_out_model``
  (perfect weather foresight, a stand-in for a forecast), and **the observer reads the same series
  for the past**: the debiased sensor interpolated across compressor runs (it reads up to
  ``max_gap_hours`` past a slot) and, in longer gaps, the **fused station series**, whose offset is
  Kalman-smoothed over the whole grid.  With ``persistence`` the horizon holds the last valid
  reading before ``t0`` and the observer reads the same causal (forward-filled) series;
* the DHW mask of the horizon is the measured one (an exogenous schedule); the sun proxy and the
  diurnal term are astronomical/calendar functions.  Occupancy is unknown.

The setpoints the valve cap reads follow design section 3 and the optimum start of section 6: the
day room's day setpoint from the window start minus the lead of the outdoor band
(``optimum_start``), the floor setpoint elsewhere.  The comfort minimum of a slot is the one in
force at the **end** of the slot, because the MILP compares it with the end-of-slot state.

Reported (all private figures; the data dir only):

(a) open-loop accuracy of the model inside the MPC at each decision: the legacy actions (replayed
    permission, measured water target) through the model, against the measured rooms, at the
    ``replay.accuracy_horizons_hours``; and from each midnight the day's heat-delivery error (the
    permission-to-heat link) and electricity error;
(b) per day, the midnight plan's predicted electricity and comfort against the legacy measured
    electricity and comfort, matched on the same days and summarised by heating-degree-day band;
(c) solver statistics: solve time, gap, status counts;
(d) the share of days whose plan differs materially from the legacy actual (``replay.material``).
"""

from __future__ import annotations

import logging
import multiprocessing
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from battery_optimizer.climate import b2d, cop, fit, plant, thermal
from battery_optimizer.climate.config import ClimateConfig
from battery_optimizer.climate.data import ClimateData
from battery_optimizer.climate.mpc import Plan, PlanSettings, plan
from battery_optimizer.climate.mpc_config import MpcConfig
from battery_optimizer.climate.mpc_model import Exogenous, HeatLaw, RcComfortModel

__all__ = [
    "ACTIONS",
    "ComfortLevels",
    "Context",
    "Prior",
    "causal_outdoor",
    "comfort_levels",
    "comfort_minimum",
    "decision_inputs",
    "fit_cop",
    "fit_prior",
    "heat_driven",
    "open_loop_accuracy",
    "prepare_season",
    "replay_days",
    "run_decision",
    "summarise",
]

log = logging.getLogger(__name__)

STEP_H = 0.25
SLOTS_PER_DAY = 96
WARMUP_SLOTS = fit.WARMUP_SLOTS
PRIOR_OF = {"S1": "S2", "S2": "S3", "S3": "S2"}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def causal_outdoor(d: ClimateData) -> np.ndarray:
    """The last valid outdoor reading at or before each slot (``NaN`` before the first)."""
    valid = np.asarray(d.t_out_valid, bool) & np.isfinite(d.t_out)
    idx = np.where(valid, np.arange(d.n), -1)
    idx = np.maximum.accumulate(idx)
    return np.where(idx >= 0, d.t_out[np.maximum(idx, 0)], np.nan)


def check_configs(ccfg: ClimateConfig, cfg: MpcConfig) -> None:
    """The MPC config must agree with the climate config it reads data through."""
    if cfg.timezone != ccfg.timezone:
        raise ValueError(
            f"climate-mpc timezone {cfg.timezone!r} differs from climate.yaml's {ccfg.timezone!r}"
        )
    if cfg.comfort.day_room not in ccfg.comfort.rooms:
        raise ValueError(f"comfort.day_room {cfg.comfort.day_room!r} is not a comfort room")


@dataclass(frozen=True)
class ComfortLevels:
    """The per-room comfort levels in effect: ``floor`` (always) and ``day_min`` (in the shared
    day window), one value per room.  The effective day minimum is ``max(day_min, floor)``."""

    floor: np.ndarray  # (rooms,)
    day_min: np.ndarray  # (rooms,)

    @property
    def day(self) -> np.ndarray:
        return np.maximum(self.day_min, self.floor)


def comfort_levels(
    rooms: Sequence[str],
    cfg: MpcConfig,
    override: Mapping[str, Mapping[str, float]] | None = None,
) -> ComfortLevels:
    """The levels of *rooms*: the config's (``floor_c`` / ``day_min_c`` for the day room, the
    floor for the others, each replaced by its ``*_by_room`` entry), then any *override* of the
    form ``{"floor": {room: degC}, "day_min": {room: degC}}`` (the live helper values)."""
    c = cfg.comfort
    if c.day_room not in rooms:
        raise ValueError(f"the day room {c.day_room!r} is not a modelled room {list(rooms)}")
    over = override or {}
    floor, day_min = [], []
    for r in rooms:
        f = over.get("floor", {}).get(r, c.floor_c_by_room.get(r, c.floor_c))
        base = c.day_min_c if r == c.day_room else c.floor_c
        floor.append(float(f))
        day_min.append(float(over.get("day_min", {}).get(r, c.day_min_c_by_room.get(r, base))))
    return ComfortLevels(np.array(floor), np.array(day_min))


def comfort_minimum(
    rooms: Sequence[str],
    hour_frac: np.ndarray,
    weekend: np.ndarray,
    cfg: MpcConfig,
    levels: ComfortLevels | None = None,
) -> np.ndarray:
    """``(rooms, T)`` minimum of the comfort schedule at the local times *hour_frac*: each room's
    floor everywhere, ``max(day minimum, floor)`` in the shared window (weekday or weekend start
    until the end hour).  *levels* default to the config's.  Callers pass the slot-**end** time
    (slot start + 0.25 h)."""
    c = cfg.comfort
    lv = levels or comfort_levels(rooms, cfg)
    start = np.where(weekend, c.day_start_weekend_hour, c.day_start_weekday_hour)
    in_day = (hour_frac >= start) & (hour_frac < c.day_end_hour)
    return np.where(in_day[None, :], lv.day[:, None], lv.floor[:, None])


@dataclass
class Context:
    """Everything a worker needs for one season (read-only)."""

    d: ClimateData
    cfg: MpcConfig
    settings: PlanSettings
    season: b2d.SeasonModel
    cop_model: cop.CopModel
    diurnal: np.ndarray
    hour_frac: np.ndarray
    weekend: np.ndarray
    t_out_model: np.ndarray  # the observer's outdoor history (weather-mode dependent)
    t_out_causal: np.ndarray
    space_heat: np.ndarray  # measured space heating per slot: 1 / 0, NaN unknown
    #: live helper values (``{"floor": {room: degC}, "day_min": {room: degC}}``) over the config's
    comfort_override: Mapping[str, Mapping[str, float]] | None = None

    @property
    def horizon(self) -> int:
        return int(self.cfg.horizon_hours * 4)


def space_heat_state(d: ClimateData) -> np.ndarray:
    """1.0 where space heat was delivered, 0.0 where not, ``NaN`` unknown (DHW slots are 0;
    :func:`_run_in_progress` skips them)."""
    with np.errstate(invalid="ignore"):
        heat = (d.q_heat_kwh >= plant.HEAT_ON_KWH) & ~d.dhw
    return np.where(np.isfinite(d.q_heat_kwh) | d.dhw, heat.astype(float), np.nan)


def _run_in_progress(c: np.ndarray, dhw: np.ndarray, t0: int) -> tuple[bool, int]:
    """The space-heating state of the last non-DHW slot before ``t0`` and how many non-DHW
    slots it has held (capped at a day: no carry-in).  DHW slots are transparent, as in the MILP;
    an unknown slot means no carry-in."""
    state = None
    run = 0
    for t in range(t0 - 1, max(-1, t0 - 1 - SLOTS_PER_DAY), -1):
        if dhw[t]:
            continue
        if not np.isfinite(c[t]):
            break
        if state is None:
            state = c[t]
        elif c[t] != state:
            break
        run += 1
    if state is None:
        return False, 10**6
    return bool(state > 0.5), run if run < SLOTS_PER_DAY else 10**6


def _first_at(hour_frac: np.ndarray, hour: float) -> int:
    """Index of the first slot at or after *hour* (the day's length if none; DST-safe)."""
    hits = np.flatnonzero(hour_frac >= hour)
    return int(hits[0]) if hits.size else int(hour_frac.size)


def schedule_setpoints(ctx: Context, t0: int, hi: int, rooms: Sequence[str]) -> np.ndarray:
    """``(rooms, hi - t0)`` thermostat setpoints the controller writes (design sections 3 and 6):
    each room's floor plus the margin everywhere; the day minimum plus the margin of every room
    whose day minimum is above its floor, from its optimum start (window start minus the lead of
    the outdoor band, never before 22:00 the evening before) until the window end.

    The band reads the mean outdoor temperature from 22:00 the evening before to the window start:
    ``t_out_model`` with perfect weather (declared foresight), otherwise the causal series before
    ``t0`` and its last value held after it."""
    d, cfg = ctx.d, ctx.cfg
    c, o = cfg.comfort, cfg.optimum_start
    margin = float(c.setpoint_margin_k)
    lv = comfort_levels(rooms, cfg, ctx.comfort_override)
    out = np.tile((lv.floor + margin)[:, None], (1, hi - t0))
    raised = np.flatnonzero(lv.day > lv.floor)  # rooms with a day minimum above their floor
    if cfg.replay.weather == "perfect":
        series = d.t_out_model
    else:
        held = ctx.t_out_causal[t0 - 1] if t0 > 0 else np.nan
        series = np.concatenate([ctx.t_out_causal[:t0], np.full(d.n - t0, held)])
    evening = 2 * 4  # 22:00 the evening before, in slots before local midnight
    first, last = int(d.day_of_slot[t0]), int(d.day_of_slot[hi - 1])
    for j in range(first, min(last + 1, len(d.days) - 1) + 1):
        _label, lo, end = d.days[j]
        hf = ctx.hour_frac[lo:end]
        weekend = bool(ctx.weekend[lo])
        start_h = c.day_start_weekend_hour if weekend else c.day_start_weekday_hour
        ws = lo + _first_at(hf, start_h)
        we = lo + _first_at(hf, c.day_end_hour)
        night = series[max(0, lo - evening) : ws]
        band_t = float(np.nanmean(night)) if np.isfinite(night).any() else -np.inf
        band = int(np.searchsorted(np.asarray(o.band_edges_c, float), band_t, side="right"))
        leads = o.lead_weekend_minutes if weekend else o.lead_weekday_minutes
        early = max(ws - int(round(leads[band] / 15.0)), lo - evening)
        a, b = max(early, t0), min(we, hi)
        if a < b:
            out[raised, a - t0 : b - t0] = (lv.day[raised] + margin)[:, None]
    return out


def decision_inputs(ctx: Context, t0: int) -> dict[str, Any] | None:
    """The inputs of the decision at slot *t0*, or ``None`` with no state or no weather.

    Reads the arrays before *t0* only, plus the declared foresight (weather in ``perfect`` mode,
    the DHW mask, sun and calendar) over the horizon."""
    d, cfg, h = ctx.d, ctx.cfg, ctx.horizon
    hi = t0 + h
    if hi > d.n:
        return None
    x0 = ctx.season.states[t0]
    if not np.isfinite(x0).all() or ctx.season.age[t0] < WARMUP_SLOTS:
        return None
    model = ctx.season.at(t0)
    if cfg.replay.weather == "perfect":
        t_out = d.t_out_model[t0:hi]
    else:
        last = ctx.t_out_causal[t0 - 1] if t0 > 0 else np.nan
        t_out = np.full(h, last)
    if not np.isfinite(t_out).all():
        return None
    minimum = comfort_minimum(
        model.rooms,
        ctx.hour_frac[t0:hi] + STEP_H,
        ctx.weekend[t0:hi],
        cfg,
        comfort_levels(model.rooms, cfg, ctx.comfort_override),
    )  # slot-end time: compared with the end-of-slot state
    ex = Exogenous(
        t_out=np.asarray(t_out, float),
        solar=d.solar[t0:hi],
        diurnal=ctx.diurnal[t0:hi],
        local_hour=d.local_hour[t0:hi].astype(np.int64),
        setpoints=schedule_setpoints(ctx, t0, hi, model.rooms),
    )
    prev_on, run = _run_in_progress(ctx.space_heat, d.dhw, t0)
    return {
        "model": model,
        "x0": np.asarray(x0, float),
        "ex": ex,
        "minimum": minimum,
        "dhw": np.asarray(d.dhw[t0:hi], bool),
        "prev_on": prev_on,
        "prev_run_slots": run,
    }


# ---------------------------------------------------------------------------
# Model preparation
# ---------------------------------------------------------------------------


def fit_cop(d: ClimateData, ccfg: ClimateConfig, season: str) -> cop.CopModel:
    """The declared COP model (``cop.model``) fitted on the fit months of *season*."""
    rows = cop.hourly_rows(d, ccfg)
    train = rows.select((rows.season == season) & rows.fit_month)
    reg = cop.fit_regression(train, float(ccfg.cop.defrost_knee_c))
    lo, hi = (float(v) for v in ccfg.cop.cop_range)
    return cop.CopModel(
        version=cop.COP_MODEL_VERSION,
        selected=str(ccfg.cop.model),
        coefficients=tuple(reg["coefficients"]),  # type: ignore[arg-type]
        knee_c=float(ccfg.cop.defrost_knee_c),
        table_bin_k=float(ccfg.cop.table_bin_k),
        table=cop.fit_table(train, float(ccfg.cop.table_bin_k)),
        cop_range=(lo, hi),
        train_seasons=(season,),
        n_train_rows=train.n,
        feature_set_id=d.feature_set_id,
        config_sha256=ccfg.sha256,
        lift_bin_k=float(ccfg.cop.lift_bin_k),
        lift_table=cop.fit_lift_table(train, float(ccfg.cop.lift_bin_k)),
    )


def _replay_days(d: ClimateData, cfg: MpcConfig, season: str) -> np.ndarray:
    months = set(cfg.replay.months)
    return np.array(
        [
            s == season and complete and label.month in months
            for (label, _lo, _hi), s, complete in zip(
                d.days, d.day_season, d.day_complete, strict=True
            )
        ],
        dtype=bool,
    )


def _observe_piece(model: RcComfortModel, w: np.ndarray, y: np.ndarray, lo: int, hi: int):
    start = max(0, lo - 2 * WARMUP_SLOTS)
    xs, age = model.observe(w[start:hi], y[start:hi])
    return xs[lo - start :], age[lo - start :]


@dataclass(frozen=True)
class Prior:
    """B0's season prior of the ``b2d`` model: every parameter, the heat law, the valve cap and
    the observer gain, fitted on one season (the replay's ``PRIOR_OF``, the live shadow's last
    complete season)."""

    season: str
    rooms: tuple[str, ...]
    params: dict[str, float]
    n_diurnal: int
    fit_info: dict[str, Any]
    law: HeatLaw
    law_stats: dict[str, Any]
    cap: plant.ValveCap
    process_var: float
    gain: np.ndarray

    def model(self, params: dict[str, float] | None = None) -> RcComfortModel:
        """The model with *params* (default: the prior's) and the prior's law, cap and gain."""
        return b2d.make_model(
            b2d.B2D_NAME,
            params or self.params,
            self.n_diurnal,
            self.law,
            self.cap,
            self.rooms,
            gain=self.gain,
        )


def fit_prior(
    d: ClimateData,
    ccfg: ClimateConfig,
    season: str,
    *,
    w_hist: np.ndarray,
    n_diurnal: int,
    phi: float,
) -> Prior:
    """Fit the ``b2d`` prior on the training slots of *season* (B0's ``fitm``), then the heat law
    on the prior model's observer slab states."""
    rooms = tuple(d.comfort_rooms)
    train = fit.train_slots(d, [season])
    params, fit_info = b2d.fit_b2d(d, w_hist, train, n_diurnal)
    cap = b2d.cap_for(d, ccfg, train, phi)
    ad, bd = b2d.rc_matrices(params, n_diurnal)
    pv = b2d.process_var(d, ad, bd, w_hist, train)
    gain = thermal.observer_gain(ad, pv, thermal.MEASUREMENT_SIGMA_K**2)
    placeholder = HeatLaw(0.0, 0.0, 0.0, float(ccfg.comfort.low_c))
    prior_model = b2d.make_model(
        b2d.B2D_NAME, params, n_diurnal, placeholder, cap, rooms, gain=gain
    )
    prior_states, _ = prior_model.observe(w_hist, d.t_air)
    law, law_stats = b2d.fit_heat_law(d, prior_states, cap, train, float(ccfg.comfort.low_c))
    return Prior(
        season=season,
        rooms=rooms,
        params=params,
        n_diurnal=n_diurnal,
        fit_info=fit_info,
        law=law,
        law_stats=law_stats,
        cap=cap,
        process_var=pv,
        gain=np.asarray(gain, float),
    )


def prepare_season(
    d: ClimateData,
    ccfg: ClimateConfig,
    cfg: MpcConfig,
    season: str,
    *,
    w_hist: np.ndarray,
    diurnal: np.ndarray,
    phi: float,
    c2_record: dict[str, Any] | None = None,
) -> b2d.SeasonModel:
    """The model schedule and observer states for one replayed *season*."""
    days = _replay_days(d, cfg, season)
    if not days.any():
        raise ValueError(f"no replay day in {season}")
    rooms = tuple(d.comfort_rooms)
    y = d.t_air
    states = np.full((d.n, 2), np.nan)
    age = np.full(d.n, -1, dtype=np.int64)
    first = int(np.flatnonzero(days)[0])
    last = int(np.flatnonzero(days)[-1])
    lo_slot = d.days[first][1]
    hi_slot = min(d.n, d.days[last][2] + cfg.horizon_hours * 4)
    if cfg.model.kind == b2d.C2_NAME:
        if c2_record is None:
            raise ValueError("model.kind c2 needs the C2 record")
        model, info = b2d.from_c2_record(c2_record, rooms)
        w4 = w_hist[:, :4]
        xs, ag = _observe_piece(model, w4, y, lo_slot, hi_slot)
        states[lo_slot:hi_slot], age[lo_slot:hi_slot] = xs, ag
        info["prior_season"] = "S2"
        return b2d.SeasonModel(season, "S2", [(lo_slot, model)], states, age, info)

    n_d = diurnal.shape[1]
    prior_season = PRIOR_OF[season]
    prior = fit_prior(d, ccfg, prior_season, w_hist=w_hist, n_diurnal=n_d, phi=phi)
    params, law, cap, gain = prior.params, prior.law, prior.cap, prior.gain
    fit_info, law_stats, pv = prior.fit_info, prior.law_stats, prior.process_var

    re = cfg.model.reestimate
    pieces: list[tuple[int, RcComfortModel]] = []
    refits = []
    first_day: date = d.days[first][0]
    last_day: date = d.days[last][0]
    day_index = {label: j for j, (label, _l, _h) in enumerate(d.days)}
    origin = first_day
    while origin <= last_day:
        j = day_index.get(origin)
        if j is not None:
            win_days = np.zeros(len(d.days), dtype=bool)
            for back in range(1, re.window_days + 1):
                k = day_index.get(origin - timedelta(days=back))
                if k is not None and d.day_complete[k] and d.day_fit_month[k]:
                    win_days[k] = True
            window = d.day_mask(win_days)
            p_week, ok = b2d.reestimate(d, w_hist, params, window, re.params, n_d)
            model = b2d.make_model(b2d.B2D_NAME, p_week, n_d, law, cap, rooms, gain=gain)
            pieces.append((d.days[j][1], model))
            refits.append(
                {
                    "start": origin.isoformat(),
                    "refitted": ok,
                    **{n: p_week[n] for n in re.params},
                }
            )
        origin += timedelta(days=re.every_days)
    bounds = [p[0] for p in pieces] + [hi_slot]
    for (lo, model), hi in zip(pieces, bounds[1:], strict=True):
        xs, ag = _observe_piece(model, w_hist, y, lo, hi)
        states[lo:hi], age[lo:hi] = xs, ag
    info = {
        "prior_season": prior_season,
        "prior_params": params,
        "prior_fit": fit_info,
        "heat_law": law.to_dict(),
        "heat_law_fit": law_stats,
        "process_var_k2": pv,
        "valve_phi": phi,
        "reestimates": refits,
    }
    return b2d.SeasonModel(season, prior_season, pieces, states, age, info)


# ---------------------------------------------------------------------------
# Per decision
# ---------------------------------------------------------------------------


def _kh_below(temps: np.ndarray, minimum: np.ndarray) -> tuple[float, float, float]:
    """``(day-room-or-any Kh below, slots covered share, max deficit)`` over finite readings."""
    with np.errstate(invalid="ignore"):
        deficit = np.where(np.isfinite(minimum), minimum - temps, 0.0)
    known = np.isfinite(temps)
    deficit = np.where(known, np.maximum(deficit, 0.0), 0.0)
    return float(deficit.sum() * STEP_H), float(known.mean()), float(deficit.max(initial=0.0))


def _split_kh(rooms, temps, minimum, cfg, levels=None) -> dict[str, float]:
    """Kh below the day minimum (rooms with one above their floor, day window) and below the floor
    (each room); the day room is always counted."""
    floor = (levels or comfort_levels(rooms, cfg)).floor[:, None]
    day_min = np.where(minimum > floor, minimum, np.nan)
    day_rows = [
        i for i, r in enumerate(rooms) if r == cfg.comfort.day_room or np.isfinite(day_min[i]).any()
    ]
    day_kh, day_cov, _ = _kh_below(temps[day_rows], day_min[day_rows])
    floor_kh, floor_cov, _ = _kh_below(temps, np.broadcast_to(floor, minimum.shape))
    return {"day_kh": day_kh, "floor_kh": floor_kh, "coverage": min(day_cov, floor_cov)}


#: The actions an open-loop accuracy row drives the model with: the replayed legacy permission
#: (B1, legacy regime) or the measured space-heating run (the live shadow once the rule is retired;
#: weaker, because a run is only seen where heat was delivered).
ACTIONS = ("permission", "measured_run")


def _legacy_action(ctx: Context, t0: int, inp: dict[str, Any], action: str = "permission"):
    """The actions over the horizon: the replayed permission (or the measured space-heating run)
    outside DHW and the measured water target; the run stops where either is unknown."""
    d, h = ctx.d, ctx.horizon
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, not {action!r}")
    u = d.u[t0 : t0 + h] if action == "permission" else ctx.space_heat[t0 : t0 + h]
    water = d.columns["flow_target_temp_mean_c"][t0 : t0 + h]
    on = (u == 1.0) & ~inp["dhw"]
    known = np.isfinite(u) & (~on | np.isfinite(water))
    return on.astype(float), np.nan_to_num(water), fit._run_length(known, 0, h)


def run_decision(
    ctx: Context,
    t0: int,
    *,
    midnight: bool = False,
    accuracy: bool = True,
    per_slot: bool = False,
    inp: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Plan at *t0* and score the model on the legacy actions of the same horizon.

    *per_slot* adds every slot of the plan (the live shadow keeps them); *inp* are the
    :func:`decision_inputs` when the caller already built them."""
    inp = decision_inputs(ctx, t0) if inp is None else inp
    if inp is None:
        return None
    d, cfg = ctx.d, ctx.cfg
    model: RcComfortModel = inp["model"]
    ex: Exogenous = inp["ex"]
    p: Plan = plan(
        model,
        inp["x0"],
        ex,
        inp["minimum"],
        inp["dhw"],
        ctx.cop_model,
        ctx.settings,
        prev_on=inp["prev_on"],
        prev_run_slots=inp["prev_run_slots"],
    )
    c, off = model.room_outputs(ex.local_hour)
    rec: dict[str, Any] = {
        "t0": int(t0),
        "local_hour": int(d.local_hour[t0]),
        "status": p.result.status,
        "solves": [
            {"status": s.status, "solve_s": s.solve_s, "gap": s.gap, "objective": s.objective}
            for s in p.solves
        ],
        "iterates": p.iterates,
        "chosen_iterate": int(p.chosen),
    }
    if p.result.ok:
        temps = (p.xs[1:] @ c.T).T + off
        rec.update(
            {
                "plan_kwh": float(np.nansum(p.elec_kwh)),
                "plan_q_kwh": float(np.nansum(p.q_kw) * STEP_H),
                "plan_on_slots": int((p.on > 0).sum()),
                "plan_starts": int(p.starts),
                "plan_offsets": [
                    int((p.offset_index == k).sum()) for k in range(len(cfg.water.offsets_k))
                ],
                "plan_lift_outside_table": p.lift_outside_table,
                **_slot_stats(model, p, cfg),
                "plan_terminal_shortfall_kwh_th": p.result.extra.get("terminal_shortfall_kwh_th"),
                **{
                    f"plan_{k}": v
                    for k, v in _split_kh(
                        model.rooms,
                        temps,
                        inp["minimum"],
                        cfg,
                        comfort_levels(model.rooms, cfg, ctx.comfort_override),
                    ).items()
                },
            }
        )
        if midnight or per_slot:
            rec["plan_on"] = p.on.astype(np.int8).tolist()
            rec["plan_elec_slot"] = np.round(p.elec_kwh, 4).tolist()
            rec["plan_temps"] = np.round(temps, 3).tolist()
        if per_slot:
            offsets = np.asarray(cfg.water.offsets_k)
            heat = p.on > 0
            rec["plan_offset_k"] = np.where(heat, offsets[p.offset_index], 0).astype(int).tolist()
            rec["plan_water_c"] = [None if not np.isfinite(w) else float(w) for w in p.water]
            rec["plan_valve_open"] = np.round(np.nan_to_num(p.valve_open), 3).tolist()
    if accuracy:
        rec["accuracy"] = open_loop_accuracy(ctx, t0, inp, model, midnight)
    return rec


def _slot_stats(model, p: Plan, cfg: MpcConfig) -> dict[str, Any]:
    """Per planned heating slot: the top offset, the open valves at the simulated state and the
    valve factor, and the slots the run logic switched off in simulation (every valve shut)."""
    on = p.on > 0
    rooms = len(model.rooms)
    open_rooms = np.round(np.nan_to_num(p.valve_open) * rooms).astype(int)
    out: dict[str, Any] = {
        "plan_heat_slots": int(on.sum()),
        "plan_top_slots": int((on & (p.offset_index == len(cfg.water.offsets_k) - 1)).sum()),
        "plan_open_rooms": [int((on & (open_rooms == k)).sum()) for k in range(rooms + 1)],
        "plan_runlogic_off": int((on & (open_rooms == 0)).sum()),
    }
    if hasattr(model, "valve_factor"):
        out["plan_valve_factor_sum"] = float(model.valve_factor(p.valve_open)[on].sum())
    return out


def open_loop_accuracy(
    ctx: Context,
    t0: int,
    inp: dict[str, Any],
    model: RcComfortModel,
    midnight: bool,
    *,
    action: str = "permission",
) -> dict[str, Any]:
    """Section (a): the model from ``inp["x0"]`` over ``inp["ex"]`` against the measured rooms at
    the accuracy horizons, driven by the actions (``air``, ``hall``) and by the measured heat
    (``air_q``); errors are prediction minus measurement, ``pers_*`` the persistence errors.  A
    midnight origin with the whole horizon known adds the day's heat and electricity (``day``)."""
    d, cfg, h = ctx.d, ctx.cfg, ctx.horizon
    ex: Exogenous = inp["ex"]
    on, water, run = _legacy_action(ctx, t0, inp, action)
    hall = list(model.rooms).index(cfg.comfort.day_room)
    measured_ex = Exogenous(
        t_out=ex.t_out,
        solar=ex.solar,
        diurnal=ex.diurnal,
        local_hour=ex.local_hour,
        setpoints=np.vstack([d.room_setpoint[r][t0 : t0 + h] for r in model.rooms]),
    )
    run = min(run, fit._run_length(np.isfinite(measured_ex.setpoints).all(axis=0), 0, h))
    out: dict[str, Any] = {"run_slots": int(run)}
    if run == 0:
        return out
    xs, q = model.simulate(
        inp["x0"], on[:run], water[:run], _cut(measured_ex, run), run_logic=False
    )
    _c, off = model.room_outputs(ex.local_hour[:run])
    y_air = d.t_air[t0 : t0 + run]
    y_hall = d.room_temp[cfg.comfort.day_room][t0 : t0 + run]
    pers_air = d.t_air[t0 - 1]
    pers_hall = d.room_temp[cfg.comfort.day_room][t0 - 1]
    # heat-driven (measured Q): the thermal part alone
    q_meas = np.where(inp["dhw"][:run], 0.0, d.q_heat_kw[t0 : t0 + run])
    xs_q, q_known = heat_driven(model, inp["x0"], measured_ex, q_meas)
    for hh in cfg.replay.accuracy_horizons_hours:
        j = 4 * hh - 1
        if j >= run:
            continue
        row = {
            "air": _err(xs[j + 1, 0], y_air[j]),
            "hall": _err(xs[j + 1, 0] + off[hall, j], y_hall[j]),
            "pers_air": _err(pers_air, y_air[j]),
            "pers_hall": _err(pers_hall, y_hall[j]),
        }
        if xs_q is not None and j < q_known:
            row["air_q"] = _err(xs_q[j + 1, 0], y_air[j])
        out[f"h{hh}"] = row
    if run == h and midnight:
        e_meas = np.where(inp["dhw"], 0.0, d.e_heat_kwh[t0 : t0 + h])
        q_meas_kwh = np.where(inp["dhw"], 0.0, d.q_heat_kwh[t0 : t0 + h])
        e_pred = cop.predict_electricity(
            ctx.cop_model, q * STEP_H, water, ex.t_out, starts=0.0, hours_on=STEP_H
        )
        e_pred = np.where(on > 0, e_pred, 0.0)
        if np.isfinite(e_meas).all() and np.isfinite(q_meas_kwh).all():
            out["day"] = {
                "q_pred_kwh": float(q.sum() * STEP_H),
                "q_meas_kwh": float(q_meas_kwh.sum()),
                "e_pred_kwh": float(e_pred.sum()),
                "e_meas_kwh": float(e_meas.sum()),
            }
    return out


def heat_driven(
    model: RcComfortModel, x0: np.ndarray, ex: Exogenous, q_kw: np.ndarray
) -> tuple[np.ndarray | None, int]:
    """``(states, slots)``: the thermal model alone from *x0*, driven by the heat *q_kw* (kW per
    slot, DHW slots 0) until the first unknown one; ``None`` when the first is unknown."""
    q_known = fit._run_length(np.isfinite(q_kw), 0, q_kw.size)
    if q_known == 0:
        return None, 0
    w = model.inputs(q_kw[:q_known], _cut(ex, q_known))
    return thermal.simulate(model.ad, w @ model.bd.T, np.asarray(x0, float)), q_known


def _err(pred: float, obs: float) -> float | None:
    v = float(pred) - float(obs)
    return v if np.isfinite(v) else None


def _cut(ex: Exogenous, n: int) -> Exogenous:
    return Exogenous(
        ex.t_out[:n], ex.solar[:n], ex.diurnal[:n], ex.local_hour[:n], ex.setpoints[:, :n]
    )


# ---------------------------------------------------------------------------
# Days (parallel)
# ---------------------------------------------------------------------------

_CTX: Context | None = None


def _decision_slots(ctx: Context, j: int) -> list[int]:
    _label, lo, hi = ctx.d.days[j]
    step = ctx.cfg.replan_every_minutes // 15
    return list(range(lo, hi, step))


def _run_day(j: int) -> dict[str, Any]:
    ctx = _CTX
    assert ctx is not None
    d = ctx.d
    label, lo, hi = d.days[j]
    decisions = []
    for t0 in _decision_slots(ctx, j):
        rec = run_decision(ctx, t0, midnight=t0 == lo)
        if rec is not None:
            decisions.append(rec)
    day: dict[str, Any] = {"day": label.isoformat(), "slots": hi - lo, "decisions": decisions}
    day.update(_legacy_day(ctx, j))
    return day


def _legacy_day(ctx: Context, j: int) -> dict[str, Any]:
    """The measured legacy day: electricity outside DHW, heating slots, comfort, HDD."""
    d, cfg = ctx.d, ctx.cfg
    _label, lo, hi = d.days[j]
    dhw = d.dhw[lo:hi]
    e = np.where(dhw, 0.0, d.e_heat_kwh[lo:hi])
    rooms = tuple(d.comfort_rooms)
    minimum = comfort_minimum(rooms, ctx.hour_frac[lo:hi] + STEP_H, ctx.weekend[lo:hi], cfg)
    temps = np.vstack([d.room_temp[r][lo:hi] for r in rooms])
    u = d.u[lo:hi]
    t_out = d.t_out_model[lo:hi]
    out = {
        "legacy_kwh": float(e.sum()) if np.isfinite(e).all() else None,
        "legacy_on": ((u == 1.0) & ~dhw).astype(np.int8).tolist() if np.isfinite(u).all() else None,
        "hdd": float(np.nanmean(np.maximum(cfg.replay.hdd_base_c - t_out, 0.0)))
        if np.isfinite(t_out).mean() > 0.9
        else None,
    }
    out.update({f"legacy_{k}": v for k, v in _split_kh(rooms, temps, minimum, cfg).items()})
    return out


def replay_days(ctx: Context, days: Sequence[int], workers: int = 1) -> list[dict[str, Any]]:
    """Run the decisions of *days* (local-day indices), in parallel with *workers* processes."""
    global _CTX
    _CTX = ctx
    try:
        if workers <= 1:
            return [_run_day(j) for j in days]
        mp = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp) as pool:
            return list(pool.map(_run_day, days, chunksize=1))
    finally:
        _CTX = None


def replay_day_indices(ctx: Context, season: str, max_days: int | None = None) -> list[int]:
    days = np.flatnonzero(_replay_days(ctx.d, ctx.cfg, season))
    if max_days is not None and max_days < days.size:
        days = days[np.linspace(0, days.size - 1, max_days).round().astype(int)]
    return [int(j) for j in days]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _stats(values: Sequence[float]) -> dict[str, Any]:
    v = np.array([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    if v.size == 0:
        return {"n": 0}
    return {
        "n": int(v.size),
        "mae": float(np.mean(np.abs(v))),
        "bias": float(np.mean(v)),
        "rmse": float(np.sqrt(np.mean(v**2))),
    }


def _jaccard(a: Sequence[int], b: Sequence[int]) -> float | None:
    x, y = np.asarray(a, bool), np.asarray(b, bool)
    union = (x | y).sum()
    return float((x & y).sum() / union) if union else None


def summarise(days: list[dict[str, Any]], cfg: MpcConfig) -> dict[str, Any]:
    """(a) to (d) of the module docstring from the per-day records."""
    decisions = [r for day in days for r in day["decisions"]]
    # (a) accuracy
    acc: dict[str, Any] = {}
    for hh in cfg.replay.accuracy_horizons_hours:
        rows = [r["accuracy"].get(f"h{hh}") for r in decisions if "accuracy" in r]
        rows = [x for x in rows if x]
        acc[f"{hh}h"] = {
            key: _stats([x.get(key) for x in rows])
            for key in ("air", "hall", "air_q", "pers_air", "pers_hall")
        }
    daily = [r["accuracy"]["day"] for r in decisions if "day" in r.get("accuracy", {})]
    q_err = [
        (x["q_pred_kwh"] - x["q_meas_kwh"]) / x["q_meas_kwh"]
        for x in daily
        if x["q_meas_kwh"] > 1.0
    ]
    e_err = [
        (x["e_pred_kwh"] - x["e_meas_kwh"]) / x["e_meas_kwh"]
        for x in daily
        if x["e_meas_kwh"] > 0.25
    ]
    sum_q = sum(x["q_meas_kwh"] for x in daily)
    sum_e = sum(x["e_meas_kwh"] for x in daily)
    acc["daily_heat"] = {
        "days": len(q_err),
        "mape": float(np.mean(np.abs(q_err))) if q_err else None,
        "season_bias": (sum(x["q_pred_kwh"] for x in daily) / sum_q - 1.0) if sum_q else None,
    }
    acc["daily_electricity"] = {
        "days": len(e_err),
        "mape": float(np.mean(np.abs(e_err))) if e_err else None,
        "season_bias": (sum(x["e_pred_kwh"] for x in daily) / sum_e - 1.0) if sum_e else None,
    }
    # (b) and (d): the midnight plan of each 96-slot day
    rows = []
    for day in days:
        mid = next((r for r in day["decisions"] if "plan_on" in r), None)
        if mid is None or day["slots"] != SLOTS_PER_DAY or day.get("legacy_kwh") is None:
            continue
        legacy_model = next(
            (r["accuracy"]["day"] for r in day["decisions"] if "day" in r.get("accuracy", {})),
            None,
        )
        rows.append(
            {
                "day": day["day"],
                "hdd": day.get("hdd"),
                "plan_kwh": mid["plan_kwh"],
                "legacy_kwh": day["legacy_kwh"],
                "legacy_model_kwh": legacy_model["e_pred_kwh"] if legacy_model else None,
                "plan_day_kh": mid["plan_day_kh"],
                "plan_floor_kh": mid["plan_floor_kh"],
                "legacy_day_kh": day["legacy_day_kh"],
                "legacy_floor_kh": day["legacy_floor_kh"],
                "overlap": _jaccard(mid["plan_on"], day["legacy_on"])
                if day.get("legacy_on") is not None
                else None,
            }
        )
    comparison = _compare(rows, cfg)
    # (c) solver
    solves = [s for r in decisions for s in r["solves"]]
    times = np.array([s["solve_s"] for s in solves]) if solves else np.zeros(0)
    gaps = np.array([s["gap"] for s in solves if np.isfinite(s["gap"])])
    statuses: dict[str, int] = {}
    for s in solves:
        statuses[s["status"]] = statuses.get(s["status"], 0) + 1
    solver = {
        "decisions": len(decisions),
        "solves": len(solves),
        "status_counts": statuses,
        "infeasible_or_error": statuses.get("infeasible", 0) + statuses.get("error", 0),
        "solve_s": {
            "median": float(np.median(times)) if times.size else None,
            "p95": float(np.quantile(times, 0.95)) if times.size else None,
            "max": float(times.max()) if times.size else None,
        },
        "gap": {
            "median": float(np.median(gaps)) if gaps.size else None,
            "max": float(gaps.max()) if gaps.size else None,
        },
        "lift_outside_table_mean": float(
            np.nanmean([r.get("plan_lift_outside_table", np.nan) for r in decisions])
        )
        if decisions
        else None,
        "relinearization": _relin_stats(decisions),
    }
    return {
        "accuracy": acc,
        "comparison": comparison,
        "solver": solver,
        "plan_slots": _plan_slot_stats(decisions),
    }


def _relin_stats(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Which iterate was kept and how much the last re-linearisation changed the plan."""
    rows = [r for r in decisions if r.get("iterates")]
    if not rows:
        return {"decisions": 0}
    n_it = max(len(r["iterates"]) for r in rows)
    chosen = np.array([r["chosen_iterate"] for r in rows])
    last_changed = [r["iterates"][-1]["changed"] for r in rows if len(r["iterates"]) > 1]
    gain = [
        r["iterates"][0]["objective"] - r["iterates"][r["chosen_iterate"]]["objective"]
        for r in rows
    ]
    return {
        "decisions": len(rows),
        "chosen_share": {str(i): float(np.mean(chosen == i)) for i in range(n_it)},
        "last_iterate_changed_slots": {
            "median": float(np.median(last_changed)) if last_changed else None,
            "share_zero": float(np.mean(np.array(last_changed) == 0)) if last_changed else None,
        },
        "objective_gain_vs_first_median": float(np.median(gain)),
    }


def _plan_slot_stats(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Top-offset share, valve factor and open valves over every planned heating slot."""
    rows = [r for r in decisions if "plan_heat_slots" in r]
    heat = sum(r["plan_heat_slots"] for r in rows)
    if not heat:
        return {"heat_slots": 0}
    rooms = np.sum([r["plan_open_rooms"] for r in rows], axis=0)
    out = {
        "heat_slots": int(heat),
        "top_offset_share": sum(r["plan_top_slots"] for r in rows) / heat,
        "open_rooms_share": {str(k): float(v / heat) for k, v in enumerate(rooms)},
        "runlogic_off_share": sum(r["plan_runlogic_off"] for r in rows) / heat,
    }
    if all("plan_valve_factor_sum" in r for r in rows):
        out["valve_factor_mean"] = sum(r["plan_valve_factor_sum"] for r in rows) / heat
    mid = [r for r in rows if "plan_on" in r]
    mid_heat = sum(r["plan_heat_slots"] for r in mid)
    if mid_heat:
        out["midnight_top_offset_share"] = sum(r["plan_top_slots"] for r in mid) / mid_heat
    return out


def _compare(rows: list[dict[str, Any]], cfg: MpcConfig) -> dict[str, Any]:
    if not rows:
        return {"days": 0}
    plan = np.array([r["plan_kwh"] for r in rows])
    legacy = np.array([r["legacy_kwh"] for r in rows])
    hdd = np.array([r["hdd"] if r["hdd"] is not None else np.nan for r in rows])
    lm = np.array(
        [r["legacy_model_kwh"] if r["legacy_model_kwh"] is not None else np.nan for r in rows]
    )
    out: dict[str, Any] = {
        "days": len(rows),
        "plan_over_legacy_kwh": float(plan.sum() / legacy.sum()) if legacy.sum() > 0 else None,
        "plan_over_legacy_model_kwh": float(plan[np.isfinite(lm)].sum() / lm[np.isfinite(lm)].sum())
        if np.isfinite(lm).any() and lm[np.isfinite(lm)].sum() > 0
        else None,
        "legacy_model_over_legacy_kwh": float(
            lm[np.isfinite(lm)].sum() / legacy[np.isfinite(lm)].sum()
        )
        if np.isfinite(lm).any()
        else None,
        "kh_per_day": {
            "plan_day": float(np.mean([r["plan_day_kh"] for r in rows])),
            "legacy_day": float(np.mean([r["legacy_day_kh"] for r in rows])),
            "plan_floor": float(np.mean([r["plan_floor_kh"] for r in rows])),
            "legacy_floor": float(np.mean([r["legacy_floor_kh"] for r in rows])),
        },
        "days_with_kh_below": {
            "plan_day": float(np.mean([r["plan_day_kh"] > 0.05 for r in rows])),
            "legacy_day": float(np.mean([r["legacy_day_kh"] > 0.05 for r in rows])),
            "plan_floor": float(np.mean([r["plan_floor_kh"] > 0.05 for r in rows])),
            "legacy_floor": float(np.mean([r["legacy_floor_kh"] > 0.05 for r in rows])),
        },
    }
    ok = np.isfinite(hdd) & (hdd > 0)
    if ok.sum() >= 3:
        edges = np.quantile(hdd[ok], [0, 1 / 3, 2 / 3, 1])
        bands = []
        for i in range(3):
            sel = (
                ok & (hdd >= edges[i]) & ((hdd <= edges[i + 1]) if i == 2 else (hdd < edges[i + 1]))
            )
            if sel.any() and legacy[sel].sum() > 0:
                bands.append(
                    {
                        "hdd_from": float(edges[i]),
                        "hdd_to": float(edges[i + 1]),
                        "days": int(sel.sum()),
                        "plan_over_legacy_kwh": float(plan[sel].sum() / legacy[sel].sum()),
                    }
                )
        out["by_hdd_tercile"] = bands
    m = cfg.replay.material
    rel = np.abs(plan - legacy) / np.where(legacy > 0, legacy, np.nan)
    overlap = np.array([r["overlap"] if r["overlap"] is not None else np.nan for r in rows])
    kwh_diff = np.nan_to_num(rel, nan=0.0) > m.kwh_rel
    sched_diff = np.nan_to_num(overlap, nan=1.0) < m.on_overlap_min
    out["material"] = {
        "kwh_share": float(kwh_diff.mean()),
        "schedule_share": float(sched_diff.mean()),
        "either_share": float((kwh_diff | sched_diff).mean()),
        "median_overlap": float(np.nanmedian(overlap)) if np.isfinite(overlap).any() else None,
    }
    return out


def outputs_dir(root: Path, run_id: str) -> Path:
    return root / "analysis" / "v1" / "climate-mpc" / run_id
