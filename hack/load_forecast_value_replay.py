#!/usr/bin/env python3
"""What is a perfect house-load forecast worth to the live battery stack? Read-only replay.

Reproduces the "Value of a perfect load forecast" block of section 7 of
``docs/planning/load-forecaster-improvement-plan.md``.  A rolling LP, re-solved every 15 minutes
over the published-price horizon, is executed against the minute-level real load under the three
actuator rules of the service (``plan``: follow the planned power; ``soc``: follow the load until
the plan's end SOC; ``value``: the value-based load-following).  Each rule runs once with the load
forecast the service issued (the ``final`` plan, ``forecast``) and once with the realised load
(``perfect``); the difference is the value of a perfect forecast.  Variants: the heat-pump or the
rest of the load known exactly (``hybrid``), terminal-value and wear sensitivities, and the
forecast corrected by the window's mean bias per slot of the day.

**It approximates the service, it is not the service**: no derate, idle drain, measured
efficiency, PD-loop lag, dwell cost or soft cap.  The fidelity block compares the replayed saving
with the realised one (grid and battery power times price) day by day.

Inputs, all read from the long-term Prometheus archive (``BATTERY_OPTIMIZER_PROMETHEUS_URL``) on a
step grid: the service's per-slot counters (actual load, final and day-ahead forecast, scored
slots), the PVPC price, 15-minute mean heat-pump / grid / battery powers, and the 60 s main-meter
and battery power.  The trailing heat-pump profile is the package's LF-0 kernel
(``policies.trailing_slot_mean_for_day``).

Usage:
    uv run python hack/load_forecast_value_replay.py --from 2026-11-04 --to 2026-11-18 \\
        [--data-dir ~/data/<store>] [--run-id <name>] [--history-days 29] [--skip-variants]

``--from`` is inclusive, ``--to`` exclusive (UTC days).  ``BATTERY_OPTIMIZER_DATA_DIR`` replaces
``--data-dir``.  The summary goes to stdout.  With ``--run-id`` the fetched series (a cache that a
second run with the same id reuses) and ``report.json`` are written to
``<data_dir>/analysis/v1/load-forecast-value-replay/<run-id>/``; the repository is never touched
and nothing is written without ``--run-id``.  The figures are private: they stay in the data dir
and on stdout.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import yaml
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, eye, hstack

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lf_common as lc  # noqa: E402  (shared plumbing of the load-forecast scripts)

_PROJECT = Path(__file__).resolve().parent.parent

from battery_optimizer.policies import trailing_slot_mean_for_day  # noqa: E402

TOOL = "load-forecast-value-replay"
SLOT_S = 900
SLOT_H = 0.25
MIN_PER_SLOT = 15

ENT_PRICE = 'max(hass_sensor_unit_u0x20ac_per_kwh{entity="sensor.esios_pvpc"})'
ENT_GRID_W = 'hass_sensor_power_w{entity="sensor.shellyem_main_channel_1_power"}'
ENT_BATT_W = "battery_optimizer_actuator_observed_battery_power_watts"
COUNTERS = {
    "act": 'max(battery_optimizer_load_actual_kwh_total{plan="final"})',
    "fcf": 'max(battery_optimizer_load_forecast_kwh_total{plan="final"})',
    "fcd": 'max(battery_optimizer_load_forecast_kwh_total{plan="day_ahead"})',
    "scf": 'max(battery_optimizer_load_forecast_scored_slots_total{plan="final"})',
    "scd": 'max(battery_optimizer_load_forecast_scored_slots_total{plan="day_ahead"})',
}


#: 15-minute mean powers, evaluated at the slot end (so they describe the slot that just closed).
def _mean15(selector: str, agg: str = "sum") -> str:
    return f"{agg}(avg_over_time({selector}[15m]))"


def _power(entity: str) -> str:
    return f'hass_sensor_power_w{{entity="sensor.{entity}"}}'


MEANS = {
    "hp_heat": _mean15(_power("aquarea_heat_power_consumed")),
    "hp_dhw": _mean15(_power("aquarea_dhw_power_consumed")),
    "grid": _mean15(ENT_GRID_W),
    "batt": _mean15(ENT_BATT_W, "avg"),
    "offgrid": _mean15(_power("venus_ac_offgrid_power")),
}


@dataclass(frozen=True)
class Plant:
    """The battery and load-following settings of the replay (the values deployed in 2026-11)."""

    cap_kwh: float = 5.12
    pmax_kw: float = 2.5
    eta: float = 0.8**0.5  # one-way efficiency, round trip 0.8
    smin_frac: float = 0.20
    smax_frac: float = 0.95
    s0_frac: float = 0.40
    import_limit_kw: float = 5.0
    wear_eur_per_kwh: float = 0.0  # set from the deployed planning config (see main)
    terminal_eur_per_kwh: float = 0.0  # value of the energy left in the battery (see main)
    lf_ceiling_w: float = 2500.0
    lf_tol_eur_per_kwh: float = 0.0  # set from the shipped config (see main)
    lf_floor_hyst_frac: float = 0.02
    margin_w_before: float = 100.0  # discharge margin below the load (W)
    margin_w_after: float = 50.0
    margin_change: datetime = datetime(2026, 11, 12, 21, 15, tzinfo=UTC)

    @property
    def smin(self) -> float:
        return self.smin_frac * self.cap_kwh

    @property
    def smax(self) -> float:
        return self.smax_frac * self.cap_kwh

    @property
    def s0(self) -> float:
        return self.s0_frac * self.cap_kwh


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def shipped_prices() -> tuple[float, float]:
    """Wear cost and load-following storage-value margin (EUR/kWh) of ``config/service.yaml``."""
    cfg = yaml.safe_load((_PROJECT / "config" / "service.yaml").read_text(encoding="utf-8"))
    wear = float(cfg["planning"]["throughput_cost_eur_per_kwh"])
    margin = float(cfg["actuation"]["load_following"]["storage_value_margin_eur_per_kwh"])
    return wear, margin


def fetch_all(
    prom: lc.Prometheus, since: datetime, until: datetime, history_days: int, rd: lc.RunDir
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Every series of the replay, as (epoch seconds, values) on its own step grid."""
    hist_start = since - timedelta(days=history_days)

    def series(name: str, expr: str, a: datetime, b: datetime, step: int):
        got = rd.cached(f"raw_{name}", lambda: lc.to_arrays(prom.grid(expr, a, b, step)))
        print(f"  {name}: {got['ts'].size} samples", file=sys.stderr, flush=True)
        return lc.from_arrays(got)

    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name, expr in COUNTERS.items():
        out[name] = series(name, expr, since, until, 30)
    out["price"] = series("price", ENT_PRICE, since, until, 60)
    one = timedelta(seconds=1)
    for name, expr in MEANS.items():
        # slot [t, t+15m) is read at its end, so the grid runs from hist_start+15m to ``until``
        out[name] = series(name, expr, hist_start + timedelta(minutes=15), until + one, SLOT_S)
    out["g_min"] = series("g_min", f"max({ENT_GRID_W})", since, until, 60)
    out["b_min"] = series("b_min", f"max({ENT_BATT_W})", since, until, 60)
    return out


def counter_slots(raw: dict[str, tuple[np.ndarray, np.ndarray]]) -> dict[int, dict[str, float]]:
    """Per closed slot the increments of the service counters, keyed by the slot close.

    A slot counts when both scored-slot counters advanced by exactly one between two
    consecutive samples and no counter went backwards (a pod restart resets them: such an
    increment cannot be attributed and is skipped).
    """
    names = list(COUNTERS)
    stamps = sorted({int(t) for n in names for t in raw[n][0]})
    pos = {t: i for i, t in enumerate(stamps)}
    table = np.full((len(stamps), len(names)), np.nan)
    for j, n in enumerate(names):
        ts, values = raw[n]
        table[[pos[int(t)] for t in ts], j] = values
    col = {n: j for j, n in enumerate(names)}
    rows: dict[int, dict[str, float]] = {}
    for i in range(1, len(stamps)):
        prev, cur = table[i - 1], table[i]
        if np.isnan(prev).any() or np.isnan(cur).any():
            continue
        d = cur - prev
        if (
            d[col["scf"]] == 1
            and d[col["scd"]] == 1
            and min(d[col[n]] for n in ("act", "fcf", "fcd")) >= 0
        ):
            close = stamps[i] // SLOT_S * SLOT_S
            rows.setdefault(
                close,
                {"act": d[col["act"]], "fcf": d[col["fcf"]], "fcd": d[col["fcd"]]},
            )
    return rows


def ffill(x: np.ndarray) -> np.ndarray:
    """Forward fill NaN (leading NaN take the first valid value)."""
    out = x.copy()
    valid = np.flatnonzero(~np.isnan(out))
    if valid.size == 0:
        return out
    idx = np.maximum.accumulate(np.where(np.isnan(out), 0, np.arange(out.size)))
    out = out[idx]
    out[: valid[0]] = out[valid[0]]
    return out


def interpolate(x: np.ndarray) -> np.ndarray:
    """Linear interpolation of the NaN gaps; the tail holds the last value, the head the first."""
    out = x.copy()
    valid = np.flatnonzero(~np.isnan(out))
    if valid.size == 0:
        return out
    return np.interp(np.arange(out.size), valid, out[valid])


@dataclass
class Slots:
    t0: int
    epoch: np.ndarray
    scored: np.ndarray
    load: np.ndarray  # measured slot load, NaN when not scored
    load_f: np.ndarray  # load with gaps filled from the grid/battery reconstruction
    lf: np.ndarray  # final-plan forecast, NaN when not scored
    lf_f: np.ndarray  # forecast with gaps filled by the same-slot mean
    ld: np.ndarray  # day-ahead forecast
    price: np.ndarray
    minute_w: np.ndarray  # (n, 15) minute load profile (W), mean equal to load_f
    hp_act: np.ndarray
    hp_hat: np.ndarray
    offgrid: np.ndarray
    local_hour: np.ndarray
    local_slot: np.ndarray
    local_day: np.ndarray
    minute_cover: float
    recon_mae: float
    missing: int
    minute_g: np.ndarray
    minute_b: np.ndarray


def hp_trailing_profile(
    hp_kwh: tuple[np.ndarray, np.ndarray], first_slot: int, last_slot: int, window_days: int
) -> dict[int, float]:
    """LF-0 kernel (same-slot mean of the previous ``window_days`` local days) of the heat pump.

    *hp_kwh* is (slot-start epoch, kWh).  Returns the profile value of every slot start in
    ``[first_slot, last_slot)`` that lies on the local-day aligned grid.
    """
    ts, kwh = hp_kwh
    day0 = datetime.fromtimestamp(first_slot, ZoneInfo(lc.LOCAL_TZ)).date()
    day1 = datetime.fromtimestamp(last_slot - 1, ZoneInfo(lc.LOCAL_TZ)).date() + timedelta(days=1)
    g0, g1 = lc.local_midnight_epoch(day0), lc.local_midnight_epoch(day1)
    n = (g1 - g0) // SLOT_S
    values = lc.on_grid((ts, kwh), g0, SLOT_S, n)
    keep = np.isfinite(values)
    load = np.where(keep, values, 0.0)
    epoch = g0 + SLOT_S * np.arange(n)
    ordinal, _, _ = lc.local_fields(epoch)
    days = lc.day_bounds(ordinal)
    profile = np.zeros(n)
    for j, (_label, start, stop) in enumerate(days):
        profile[start:stop], _ = trailing_slot_mean_for_day(load, keep, days, j, window_days)
    return {int(e): float(p) for e, p in zip(epoch, profile, strict=True)}


def build_slots(
    raw: dict[str, tuple[np.ndarray, np.ndarray]], since: datetime, until: datetime
) -> Slots:
    t0 = int(since.timestamp())
    n = int((until - since).total_seconds()) // SLOT_S
    epoch = t0 + SLOT_S * np.arange(n)
    nan = np.full(n, np.nan)
    load, lf, ld = nan.copy(), nan.copy(), nan.copy()
    for close, d in counter_slots(raw).items():
        k = (close - SLOT_S - t0) // SLOT_S
        if 0 <= k < n:
            load[k], lf[k], ld[k] = d["act"], d["fcf"], d["fcd"]
    scored = ~np.isnan(load)
    means = {name: lc.on_grid(raw[name], t0, SLOT_S, n, shift=SLOT_S) for name in MEANS}
    recon = (means["grid"] - means["batt"]) * SLOT_H / 1000.0
    ok = scored & ~np.isnan(recon)
    recon_mae = float(np.abs(recon[ok] - load[ok]).mean()) if ok.any() else float("nan")
    local_day, local_hour, local_min = lc.local_fields(epoch)
    local_slot = local_hour * 4 + local_min // 15
    # forecasts missing for a slot: same-slot mean of the other days (UTC slot of the day)
    tod = (epoch % 86400) // SLOT_S
    lf_f, ld_f = lf.copy(), ld.copy()
    for src, dst in ((lf, lf_f), (ld, ld_f)):
        for s in np.unique(tod):
            sel = tod == s
            m = np.nanmean(src[sel]) if (~np.isnan(src[sel])).any() else np.nan
            dst[sel & np.isnan(src)] = m
    load_f = interpolate(np.where(scored, load, recon))
    # price at slot start + 8 min, forward filled from the 60 s series
    minutes = lc.on_grid(raw["price"], t0, 60, n * MIN_PER_SLOT)
    price = ffill(minutes)[8::MIN_PER_SLOT][:n]
    # minute-level load profile: max(grid - battery, 0), gaps filled with the slot mean, then
    # rescaled so that each slot's minutes average to the slot energy
    g = lc.on_grid(raw["g_min"], t0, 60, n * MIN_PER_SLOT)
    b = lc.on_grid(raw["b_min"], t0, 60, n * MIN_PER_SLOT)
    lm = np.maximum(g - b, 0.0).reshape(n, MIN_PER_SLOT)
    cnt = (~np.isnan(lm)).sum(1)
    mean = np.where(cnt > 0, np.nansum(lm, 1) / np.maximum(cnt, 1), np.nan)
    prof = np.where(np.isnan(lm), mean[:, None], lm)
    prof[np.isnan(prof).any(1)] = 1.0
    target = load_f * 4000.0
    pm = prof.mean(1, keepdims=True)
    prof = np.where(pm > 1e-6, prof * (target[:, None] / np.maximum(pm, 1e-6)), target[:, None])
    # heat-pump component and its LF-0 profile (kWh per slot)
    hp_ts = np.intersect1d(raw["hp_heat"][0], raw["hp_dhw"][0])
    heat = dict(zip(*raw["hp_heat"], strict=True))
    dhw = dict(zip(*raw["hp_dhw"], strict=True))
    start_ts = hp_ts - SLOT_S
    hp_kwh = (start_ts, np.array([(heat[t] + dhw[t]) * SLOT_H / 1000.0 for t in hp_ts]))
    hist_first = int(raw["hp_heat"][0][0]) - SLOT_S
    profile = hp_trailing_profile(hp_kwh, hist_first, t0 + SLOT_S * n, 28)
    hp_act = lc.on_grid(hp_kwh, t0, SLOT_S, n)
    hp_hat = np.array([profile.get(int(e), 0.0) for e in epoch])
    return Slots(
        t0=t0,
        epoch=epoch,
        scored=scored,
        load=load,
        load_f=load_f,
        lf=lf,
        lf_f=lf_f,
        ld=ld,
        price=price,
        minute_w=prof,
        hp_act=hp_act,
        hp_hat=hp_hat,
        offgrid=means["offgrid"],
        local_hour=local_hour,
        local_slot=local_slot,
        local_day=local_day,
        minute_cover=float((cnt >= 10).mean()),
        recon_mae=recon_mae,
        missing=int((~scored).sum()),
        minute_g=g,
        minute_b=b,
    )


# ---------------------------------------------------------------------------
# Rolling-horizon LP replay
# ---------------------------------------------------------------------------


class Replay:
    """Rolling LP of the battery over the published-price horizon, executed on minute load."""

    def __init__(self, s: Slots, plant: Plant) -> None:
        self.s = s
        self.n = s.epoch.size
        self.plant = plant
        self._matrices: dict[int, csr_matrix] = {}
        change = plant.margin_change.timestamp()
        self.margin_w = np.where(s.epoch < change, plant.margin_w_before, plant.margin_w_after)
        self.hend = self._horizon_ends()

    def _horizon_ends(self) -> np.ndarray:
        """Last slot of each plan: the prices of tomorrow are known from 20:30 local."""
        s, n = self.s, self.n
        lmin = s.local_hour * 60 + s.local_slot % 4 * 15
        hend = np.zeros(n, int)
        for k in range(n):
            lastday = s.local_day[k] + (1 if lmin[k] >= 20 * 60 + 30 else 0)
            j = k
            while j + 1 < n and s.local_day[j + 1] <= lastday and j + 1 - k < 192:
                j += 1
            hend[k] = j
        return hend

    def _balance(self, size: int) -> csr_matrix:
        """Rows ``s_j - s_{j-1} - eta c_j + d_j / eta = 0`` over x = [c, d, s]."""
        if size not in self._matrices:
            p = self.plant
            ident = eye(size, format="csr")
            carry = ident - eye(size, k=-1, format="csr")
            self._matrices[size] = hstack([-p.eta * ident, ident / p.eta, carry], format="csr")
        return self._matrices[size]

    def _lp(self, price: np.ndarray, load: np.ndarray, margin_w: np.ndarray, s0: float, p: Plant):
        """Charge/discharge/SOC plan over *price*: min cost with the terminal value of the SOC."""
        size = price.size
        margin = margin_w / 1000.0 * SLOT_H
        cu = np.minimum(p.pmax_kw * SLOT_H, np.maximum(0.0, p.import_limit_kw * SLOT_H - load))
        du = np.minimum(p.pmax_kw * SLOT_H, np.maximum(0.0, load - margin))
        wear = p.wear_eur_per_kwh
        cost = np.concatenate([price + wear, -price + wear, np.zeros(size)])
        cost[-1] -= p.terminal_eur_per_kwh
        rhs = np.zeros(size)
        rhs[0] = s0
        bounds = np.concatenate(
            [
                np.column_stack([np.zeros(size), cu]),
                np.column_stack([np.zeros(size), du]),
                np.tile([min(s0, p.smin), p.smax], (size, 1)),
            ]
        )
        r = linprog(cost, A_eq=self._balance(size), b_eq=rhs, bounds=bounds, method="highs")
        if r.status != 0:
            raise RuntimeError(f"LP failed: {r.message}")
        return r, size

    def solve(self, k: int, s0: float, forecast: np.ndarray, p: Plant, dual: bool):
        e = self.hend[k]
        r, size = self._lp(
            self.s.price[k : e + 1], forecast[k : e + 1], self.margin_w[k : e + 1], s0, p
        )
        value = -r.eqlin.marginals[0] if dual else float("nan")
        return r.x[0], r.x[size], value

    def run(self, forecast: np.ndarray, rule: str, p: Plant) -> np.ndarray:
        """Columns: charge, discharge (AC kWh), grid, SOC end (kWh), battery value, engaged."""
        s_ = self.s
        soc = p.s0
        out = np.zeros((self.n, 6))
        for k in range(self.n):
            c, d, v = self.solve(k, max(soc, p.smin), forecast, p, rule == "value")
            v_ac = v / p.eta
            avail = max(0.0, soc - p.smin) * p.eta
            chg = dis = eng = 0.0
            if d > 5e-3:
                if rule == "plan":
                    cap_w, elim = d / SLOT_H * 1000.0, d
                else:
                    cap_w, elim = p.lf_ceiling_w, d
                    if (
                        rule == "value"
                        and (s_.price[k] - p.wear_eur_per_kwh) >= v_ac + p.lf_tol_eur_per_kwh
                    ):
                        elim = max(0.0, soc - p.smin - p.lf_floor_hyst_frac * p.cap_kwh) * p.eta
                        eng = 1.0
                per_min = (
                    np.minimum(cap_w, np.maximum(0.0, s_.minute_w[k] - self.margin_w[k])) / 60000.0
                )
                dis = float(min(per_min.sum(), elim, avail))
            elif c > 5e-3:
                chg = min(
                    c,
                    max(0.0, p.import_limit_kw * SLOT_H - s_.load_f[k]),
                    (p.smax - soc) / p.eta,
                )
            soc = soc + chg * p.eta - dis / p.eta
            soc = min(max(soc, p.smin if soc >= p.smin else soc), p.smax)
            out[k] = (chg, dis, s_.load_f[k] + chg - dis, soc, v_ac, eng)
        return out

    def summarise(self, o: np.ndarray, p: Plant) -> dict[str, float]:
        chg, dis = o[:, 0], o[:, 1]
        bill = float(np.sum(self.s.price * (dis - chg)))
        inv = p.terminal_eur_per_kwh * (o[-1, 3] - p.s0)
        wear = p.wear_eur_per_kwh * float(chg.sum() + dis.sum())
        return {
            "bill": bill,
            "inv": float(inv),
            "wear": wear,
            "net": bill + float(inv) - wear,
            "charge_kwh": float(chg.sum()),
            "discharge_kwh": float(dis.sum()),
            "end_soc_frac": float(o[-1, 3] / p.cap_kwh),
            "engaged_slots": int(o[:, 5].sum()),
        }

    def net_slots(self, o: np.ndarray, p: Plant) -> np.ndarray:
        dsoc = np.diff(np.concatenate([[p.s0], o[:, 3]]))
        chg, dis = o[:, 0], o[:, 1]
        return (
            self.s.price * (dis - chg)
            + p.terminal_eur_per_kwh * dsoc
            - p.wear_eur_per_kwh * (chg + dis)
        )

    def oracle(self, p: Plant) -> dict[str, float]:
        """One LP over the whole window with perfect prices and loads (an upper bound)."""
        r, size = self._lp(self.s.price, self.s.load_f, self.margin_w, p.s0, p)
        c, d, soc = r.x[:size], r.x[size : 2 * size], r.x[2 * size :]
        return {
            "bill": float(np.sum(self.s.price * (d - c))),
            "inv": float(p.terminal_eur_per_kwh * (soc[-1] - p.s0)),
            "wear": float(p.wear_eur_per_kwh * (c.sum() + d.sum())),
        }


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def case_list(s: Slots, plant: Plant, variants: bool) -> dict[str, tuple[np.ndarray, str, Plant]]:
    cases: dict[str, tuple[np.ndarray, str, Plant]] = {}
    for tag, rule in (("0", "plan"), ("1", "soc"), ("2", "value")):
        cases[f"F{tag}_forecast_{rule}"] = (s.lf_f, rule, plant)
    for tag, rule in (("0", "plan"), ("1", "soc"), ("2", "value")):
        cases[f"P{tag}_perfect_{rule}"] = (s.load_f, rule, plant)
    if not variants:
        return cases
    hp_known = np.maximum(
        np.where(np.isnan(s.hp_act), s.hp_hat, s.hp_act) + (s.lf_f - s.hp_hat), 0.01
    )
    rest_known = np.maximum(
        s.hp_hat + (s.load_f - np.where(np.isnan(s.hp_act), 0.0, s.hp_act)), 0.01
    )
    for rule in ("value", "soc"):
        cases[f"H_hpperfect_{rule}"] = (hp_known, rule, plant)
        cases[f"H_restperfect_{rule}"] = (rest_known, rule, plant)
    for tag, change in (
        ("TVm20", {"terminal_eur_per_kwh": 0.8 * plant.terminal_eur_per_kwh}),
        ("TVp20", {"terminal_eur_per_kwh": 1.2 * plant.terminal_eur_per_kwh}),
        ("WEAR0", {"wear_eur_per_kwh": 0.0}),
        ("WEAR2X", {"wear_eur_per_kwh": 2.0 * plant.wear_eur_per_kwh}),
    ):
        pv = replace(plant, **change)
        cases[f"S_{tag}_F2"] = (s.lf_f, "value", pv)
        cases[f"S_{tag}_P2"] = (s.load_f, "value", pv)
        cases[f"S_{tag}_P1"] = (s.load_f, "soc", pv)
        cases[f"S_{tag}_F1"] = (s.lf_f, "soc", pv)
    resid = np.where(s.scored, s.load - s.lf, np.nan)
    adj = np.zeros(s.epoch.size)
    for slot in np.unique(s.local_slot):
        sel = s.local_slot == slot
        if (~np.isnan(resid[sel])).any():
            adj[sel] = np.nanmean(resid[sel])
    cases["B_biascorr_value"] = (np.maximum(s.lf_f + adj, 0.01), "value", plant)
    return cases


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


W_PER_KWH_SLOT = 4000.0  # kWh per 15-minute slot to mean watts


def table(header: list[str], rows: list[list[Any]]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows, strict=True)]
    fmt = "  ".join(f"{{:>{w}}}" for w in widths)
    return "\n".join([fmt.format(*header), *(fmt.format(*(str(x) for x in r)) for r in rows)])


def error_report(s: Slots) -> None:
    """Load forecast error of the final and day-ahead plans (W, bias = actual - forecast)."""
    w = W_PER_KWH_SLOT
    ok = s.scored
    b = lc.bucket_index(s.local_hour)[ok]
    e_f, e_d = (s.load - s.lf)[ok], (s.load - s.ld)[ok]
    print("\n== House load forecast error per local hour bucket (W per slot; bias = actual - fc)")
    rows = []
    for i, label in enumerate((*lc.BUCKET_LABELS, "ALL")):
        sel = (b == i) if i < len(lc.BUCKET_LABELS) else np.ones(b.size, bool)
        stats = (
            np.abs(e_f[sel]).mean(),
            e_f[sel].mean(),
            np.abs(e_d[sel]).mean(),
            e_d[sel].mean(),
        )
        rows.append(
            [label, int(sel.sum()), f"{s.load[ok][sel].mean() * w:.0f}"]
            + [f"{x * w:.1f}" for x in stats]
        )
    header = ["bucket", "n", "mean_L", "MAE_final", "bias_final", "MAE_dayah", "bias_dayah"]
    print(table(header, rows))
    total = np.abs(e_f).sum()
    share = {
        label: round(float(np.abs(e_f[b == i]).sum() / total), 3)
        for i, label in enumerate(lc.BUCKET_LABELS)
    }
    print("share of sum|e| (final):", share)
    print("\n== Daily energy error of complete local days (forecast / actual - 1)")
    rows = []
    for d in np.unique(s.local_day[ok]):
        sel = ok & (s.local_day == d)
        if sel.sum() != 96:
            continue
        a, ff, fd = s.load[sel].sum(), s.lf[sel].sum(), s.ld[sel].sum()
        day = datetime.fromordinal(int(d)).date()
        rows.append([day, f"{a:.2f}", f"{100 * (ff / a - 1):+.1f}%", f"{100 * (fd / a - 1):+.1f}%"])
    print(table(["day", "actual_kWh", "final", "day_ahead"], rows))
    week = (s.epoch - s.t0) // (7 * 86400)
    for wk in np.unique(week):
        sel = ok & (week == wk)
        mae_f = np.abs(s.load - s.lf)[sel].mean() * w
        mae_d = np.abs(s.load - s.ld)[sel].mean() * w
        print(
            f"week {wk + 1}: n {int(sel.sum())}  MAE_W final {mae_f:.1f} day-ahead {mae_d:.1f}  "
            f"bias final {(s.load - s.lf)[sel].mean() * w:.1f}  mean L {s.load[sel].mean() * w:.1f}"
        )
    # what load-following can absorb: the error of whole hours
    hours = (s.epoch - s.t0) // 3600
    agg = []
    for h in np.unique(hours):
        sel = ok & (hours == h)
        if sel.sum() == 4:
            agg.append(abs((s.load - s.lf)[sel].sum()) * 1000.0)
    print(f"hourly-aggregated MAE W (final): {np.mean(agg):.1f}")


def component_report(s: Slots) -> None:
    """Share of the error variance of the heat pump (heating + hot water) and of the rest."""
    w = W_PER_KWH_SLOT
    hat_rest = s.lf_f - s.hp_hat
    act_rest = s.load_f - np.where(np.isnan(s.hp_act), 0.0, s.hp_act)
    ok = s.scored & ~np.isnan(s.hp_act)
    e = (s.load - s.lf)[ok]
    e_hp = (s.hp_act - s.hp_hat)[ok]
    e_rest = (act_rest - hat_rest)[ok]
    print("\n== Error components: heat pump (heating + hot water, LF-0 profile) and the rest")

    def line(label: str, parts: tuple[float, float, float]) -> None:
        print(f"{label:<8} total {parts[0]:.0f}  hp {parts[1]:.0f}  rest {parts[2]:.0f}")

    line("MAE W", tuple(np.abs(x).mean() * w for x in (e, e_hp, e_rest)))
    line("RMSE W", tuple(np.sqrt((x**2).mean()) * w for x in (e, e_hp, e_rest)))
    v = e.var()
    cov = 2 * np.cov(e_hp, e_rest)[0, 1] / v
    print(
        f"variance shares: hp {e_hp.var() / v:.2f}  rest {e_rest.var() / v:.2f}  "
        f"covariance {cov:.2f}"
    )
    line("bias W", tuple(x.mean() * w for x in (e, e_hp, e_rest)))
    print(
        f"mean W: load {s.load[ok].mean() * w:.0f}  hp actual {s.hp_act[ok].mean() * w:.0f}  "
        f"hp LF-0 {s.hp_hat[ok].mean() * w:.0f}  rest actual {act_rest[ok].mean() * w:.0f}  "
        f"rest LF-0 {hat_rest[ok].mean() * w:.0f}"
    )
    b = lc.bucket_index(s.local_hour)[ok]
    rows = []
    for i, label in enumerate(lc.BUCKET_LABELS):
        sel = b == i
        stats = (
            np.abs(e[sel]).mean(),
            np.abs(e_hp[sel]).mean(),
            np.abs(e_rest[sel]).mean(),
            e_hp[sel].mean(),
            e_rest[sel].mean(),
        )
        rows.append([label] + [f"{x * w:.0f}" for x in stats])
    print(table(["bucket", "MAE", "MAE_hp", "MAE_rest", "bias_hp", "bias_rest"], rows))


PAIRS = (
    ("F2_forecast_value", "P2_perfect_value"),
    ("F2_forecast_value", "P1_perfect_soc"),
    ("F1_forecast_soc", "P1_perfect_soc"),
    ("F0_forecast_plan", "P0_perfect_plan"),
)


def gap_report(
    s: Slots, rp: Replay, plant: Plant, outs: dict[str, np.ndarray], runs: dict[str, dict]
) -> dict[str, Any]:
    bucket = lc.bucket_index(s.local_hour)
    week = (s.epoch - s.t0) // (7 * 86400)
    report: dict[str, Any] = {}
    print("\n== Value of a perfect load forecast (replay; EUR over the window)")
    rows = []
    for a, b in PAIRS:
        gap = runs[b]["net"] - runs[a]["net"]
        bill_gap = runs[b]["bill"] - runs[a]["bill"]
        rows.append(
            [
                f"{b} - {a}",
                f"{gap:.3f}",
                f"{100 * gap / runs[a]['net']:.1f}%",
                f"{100 * bill_gap / runs[a]['bill']:.1f}%",
            ]
        )
        report[f"{b} - {a}"] = {"gap_net": gap, "pct_of_net": 100 * gap / runs[a]["net"]}
    print(table(["gap", "net_gap", "of_forecast_net", "of_forecast_bill"], rows))
    for a, b in PAIRS:
        diff = rp.net_slots(outs[b], plant) - rp.net_slots(outs[a], plant)
        by_bucket = {
            lab: round(float(diff[bucket == i].sum()), 3) for i, lab in enumerate(lc.BUCKET_LABELS)
        }
        by_week = {f"week {w + 1}": round(float(diff[week == w].sum()), 3) for w in np.unique(week)}
        print(f"\n{b} - {a}: total {diff.sum():.3f}")
        print(f"  by bucket: {by_bucket}\n  by week: {by_week}")
        if (a, b) == PAIRS[0]:
            by_hour = {int(h): round(float(diff[s.local_hour == h].sum()), 3) for h in range(24)}
            by_day = {
                str(datetime.fromordinal(int(d)).date()): round(
                    float(diff[s.local_day == d].sum()), 3
                )
                for d in np.unique(s.local_day)
            }
            print(f"  by hour: {by_hour}\n  by day: {by_day}")
        report[f"{b} - {a}"].update({"by_bucket": by_bucket, "by_week": by_week})
    return report


def fidelity_report(
    s: Slots, rp: Replay, plant: Plant, outs: dict[str, np.ndarray]
) -> dict[str, Any]:
    """Replayed saving against the realised one (grid and battery power times price, per day)."""
    g, b = s.minute_g, s.minute_b
    okm = ~np.isnan(g) & ~np.isnan(b)
    price = np.repeat(s.price, MIN_PER_SLOT)
    load = np.where(okm, np.maximum(g - b, 0.0), 0.0)
    grid = np.where(okm, np.maximum(g, 0.0), 0.0)
    real_day = (price * (load - grid) / 60000.0).reshape(-1, 1440).sum(1)
    week_of_day = np.arange(real_day.size) // 7
    print("\n== Replay fidelity: realised saving from minute data against the replay (EUR)")
    print(f"minute coverage (grid and battery power): {okm.mean():.3f}")
    print("realised by UTC day:", np.round(real_day, 2))
    out: dict[str, Any] = {"minute_coverage": float(okm.mean())}

    def versus(label: str, real: float, replay: float) -> float:
        pct = 100 * (replay / real - 1)
        print(f"  {label}: realised {real:.2f}  replay {replay:.2f}  replay/realised {pct:+.0f}%")
        return pct

    for name in ("F2_forecast_value", "P2_perfect_value"):
        o = outs[name]
        sim_day = (s.price * (o[:, 1] - o[:, 0])).reshape(-1, 96).sum(1)
        print(f"replay {name} by UTC day:", np.round(sim_day, 2))
        if not name.startswith("F2"):
            continue
        corr = float(np.corrcoef(real_day, sim_day)[0, 1])
        print(f"daily correlation realised vs replay F2: {corr:.2f}")
        out["daily_corr_F2"] = corr
        for w in np.unique(week_of_day):
            sel = week_of_day == w
            out[f"week_{w + 1}_replay_vs_realised_pct"] = versus(
                f"week {w + 1}", real_day[sel].sum(), sim_day[sel].sum()
            )
        out["window_replay_vs_realised_pct"] = versus("window", real_day.sum(), sim_day.sum())
    wk = (s.epoch - s.t0) // (7 * 86400)
    print("\n== Replayed bill by week and one-shot oracle (EUR)")
    for name, o in outs.items():
        if name[0] in "FP" and name[1].isdigit():
            sv = s.price * (o[:, 1] - o[:, 0])
            weeks = "  ".join(f"wk{w + 1} {sv[wk == w].sum():.3f}" for w in np.unique(wk))
            print(
                f"{name:22s} {weeks}  total {sv.sum():.3f}  discharge {o[:, 1].sum():.1f} kWh  "
                f"end SOC {o[-1, 3] / plant.cap_kwh:.2f}  "
                f"load-following engaged {int(o[:, 5].sum())} slots"
            )
    orc = rp.oracle(plant)
    print(
        "one-shot oracle (perfect prices and load for the whole window): "
        f"bill {orc['bill']:.3f}  inv {orc['inv']:.3f}  wear {orc['wear']:.3f}"
    )
    out["oracle"] = orc
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="since", type=lc.utc_day, required=True)
    parser.add_argument("--to", dest="until", type=lc.utc_day, required=True)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--history-days", type=int, default=29, help="heat pump before --from")
    parser.add_argument(
        "--terminal-value", type=float, default=None, help="EUR/kWh; default: mean price"
    )
    parser.add_argument("--wear-cost", type=float, default=None, help="EUR/kWh; default: config")
    parser.add_argument("--skip-variants", action="store_true", help="only the six F/P cases")
    args = parser.parse_args(argv)
    if args.until <= args.since:
        parser.error("--to must be after --from")
    rd = lc.RunDir(TOOL, args.run_id, args.data_dir)
    prom = lc.Prometheus()
    try:
        span = f"{args.since:%Y-%m-%d} .. {args.until:%Y-%m-%d}"
        print(f"fetching {span} (UTC days)", file=sys.stderr)
        raw = fetch_all(prom, args.since, args.until, args.history_days, rd)
    finally:
        prom.close()
    s = build_slots(raw, args.since, args.until)
    # terminal value: the window's mean price to the cent, the service's trailing-mean rule
    terminal = args.terminal_value
    if terminal is None:
        terminal = round(float(np.nanmean(s.price)), 2)
    shipped_wear, shipped_margin = shipped_prices()
    wear = args.wear_cost if args.wear_cost is not None else shipped_wear
    plant = replace(
        Plant(),
        terminal_eur_per_kwh=terminal,
        wear_eur_per_kwh=wear,
        lf_tol_eur_per_kwh=shipped_margin,
    )
    print(f"terminal value {terminal:.2f}, wear cost {wear:.3f} EUR per kWh")
    print(
        f"window {lc.utc_iso(s.epoch[0])} .. {lc.utc_iso(s.epoch[-1] + SLOT_S)} UTC: "
        f"{s.epoch.size} slots, scored {int(s.scored.sum())}, missing {s.missing}"
    )
    print(
        f"minute coverage {s.minute_cover:.3f}; reconstruction vs counter load MAE "
        f"{s.recon_mae:.4f} kWh per slot, mean load {np.nanmean(s.load):.4f}"
    )
    report: dict[str, Any] = {"window": [args.since.isoformat(), args.until.isoformat()]}
    error_report(s)
    component_report(s)
    rp = Replay(s, plant)
    outs: dict[str, np.ndarray] = {}
    runs: dict[str, dict] = {}
    for name, (forecast, rule, p) in case_list(s, plant, not args.skip_variants).items():
        t = time.time()
        outs[name] = rp.run(forecast, rule, p)
        runs[name] = rp.summarise(outs[name], p)
        print(
            f"  [{name}] {time.time() - t:.0f}s bill {runs[name]['bill']:.3f} "
            f"net {runs[name]['net']:.3f}",
            file=sys.stderr,
            flush=True,
        )
    print("\n== Replay cases (EUR over the window; bill = sum price x (discharge - charge))")
    rows = [
        [
            k,
            *(f"{v[c]:.3f}" for c in ("bill", "inv", "wear", "net")),
            f"{v['charge_kwh']:.1f}",
            f"{v['discharge_kwh']:.1f}",
        ]
        for k, v in runs.items()
    ]
    print(table(["case", "bill", "inv", "wear", "net", "charge", "discharge"], rows))
    report["runs"] = runs
    report["gaps"] = gap_report(s, rp, plant, outs, runs)
    report["fidelity"] = fidelity_report(s, rp, plant, outs)
    rd.write_json("report.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
