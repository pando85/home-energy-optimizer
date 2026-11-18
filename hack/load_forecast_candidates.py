#!/usr/bin/env python3
"""Cheap load-forecast candidates against LF-0 at every issue time and horizon. Read-only.

Reproduces the "Cheap candidates (P2 again)" block of section 7 of
``docs/planning/load-forecaster-improvement-plan.md``.  At every 15-minute issue time the
candidates forecast the next 24 h (96 slots) knowing only what was measured before the issue; the
error is scored per horizon against the measured gross load.  The headline is the price-weighted
mean absolute error (weights: the PVPC price of the target slot) relative to LF-0, by horizon
bucket (0-1 h, 1-6 h, 6-24 h, all) and for the 18-21 h local window, with a bootstrap over local
weeks for the intervals.

Candidates (names as in the plan): ``LF0`` (the package's ``trailing_mean``, 28 local days, cut at
the local midnight, or the day before for targets of a later day); ``median28`` (its
``trailing_median``); ``LF0_utc`` (same-slot mean over the previous 28 days aligned on the UTC
clock instead of the local one); ``LF0_dhwUTC`` (UTC alignment for the hot-water share only);
``now_W<min>_t<min>`` (the mean anomaly of the last W minutes against LF-0, decayed with time
constant tau); ``HPpers_*`` (the heat pump running now persisted for H minutes);
``HPknown_*`` (ceilings: the future heat-pump component known exactly, which no causal forecaster
has).  LF-0 and the median go through ``load_forecast.run_forecaster``.

Gross load is the 60 s main meter plus the battery AC power (zero before the battery), averaged
per UTC quarter hour, a slot with fewer than 12 samples being unknown.  Prometheus is read with
``query_range`` on a 60 s grid (``BATTERY_OPTIMIZER_PROMETHEUS_URL``).

Usage:
    uv run python hack/load_forecast_candidates.py --preset autumn-2026 [--run-id <name>]
    uv run python hack/load_forecast_candidates.py --preset spring-2026 [--run-id <name>]
    uv run python hack/load_forecast_candidates.py --from 2026-08-01 --to 2026-11-18T09:45 \\
        [--history-days 37] [--data-end 2026-11-18T10:00] [--period NAME=START[,END]]... \\
        [--dst-change 2026-10-25] [--check-gross] [--diag] [--bootstrap 2000]

``--from`` is the first local day of issue times, ``--to`` the exclusive end of issue times (UTC,
date or date-time); the data are read from ``--history-days`` before ``--from`` to ``--data-end``
(default ``--to`` plus one day, clipped by what exists).  ``--period`` adds a subset of issue times
(UTC ``START``, exclusive ``END``).  ``autumn-2026`` is the 2026-08-01 to 2026-11-17 window of the
plan (battery live from 2026-11-04), ``spring-2026`` its replicate, 2026-03-17 to 2026-05-14.
``BATTERY_OPTIMIZER_DATA_DIR`` replaces ``--data-dir``.  The tables go to stdout.  With
``--run-id`` the fetched series (reused by a second run with the same id), ``results_all.csv``,
``mae_by_horizon.csv`` and ``bootstrap_pw_pct.csv`` are written to
``<data_dir>/analysis/v1/load-forecast-candidates/<run-id>/``; the repository is never touched.
"""

from __future__ import annotations

import argparse
import csv
import io
import itertools
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lf_common as lc  # noqa: E402  (shared plumbing of the load-forecast scripts)

from battery_optimizer.load_forecast import run_forecaster  # noqa: E402

TOOL = "load-forecast-candidates"
SLOT_S = 900
HORIZON = 96  # slots per issue (24 h)
WINDOW_DAYS = 28
NOWCAST_WINDOWS = (1, 2, 4)  # slots
NOWCAST_TAUS = (15, 30, 60, 120)  # minutes
BASE = "LF0"

ENTITIES = {
    "grid": 'hass_sensor_power_w{entity="sensor.shellyem_main_channel_1_power"}',
    "ac": 'hass_sensor_power_w{entity="sensor.venus_ac_power"}',
    "hp_heat": 'hass_sensor_power_w{entity="sensor.aquarea_heat_power_consumed"}',
    "hp_dhw": 'hass_sensor_power_w{entity="sensor.aquarea_dhw_power_consumed"}',
    "hp_cool": 'hass_sensor_power_w{entity="sensor.aquarea_cool_power_consumed"}',
    "price": 'hass_sensor_unit_u0x20ac_per_kwh{entity="sensor.esios_pvpc"}',
}
COUNTERS = {
    "gridkwh": 'hass_sensor_energy_kwh{entity="sensor.shellyem_main_channel_1_energy"}',
    "chg": 'hass_sensor_energy_kwh{entity="sensor.venus_total_charging_energy"}',
    "dis": 'hass_sensor_energy_kwh{entity="sensor.venus_total_discharging_energy"}',
}

#: Candidates of the summary tables and of the bootstrap.
TABLE = (
    "LF0",
    "median28",
    "LF0_utc",
    "LF0_dhwUTC",
    "now_W15_t60",
    "now_W30_t60",
    "now_W60_t120",
    "HPpers_all_H30",
    "HPpers_all_H60",
    "HPknown_heat",
    "HPknown_heat+cool",
    "HPknown_dhw",
    "HPknown_all",
)
BOOTSTRAP = (
    ("median28", "all"),
    ("LF0_utc", "all"),
    ("LF0_dhwUTC", "all"),
    ("now_W15_t60", "0-1h"),
    ("now_W30_t60", "0-1h"),
    ("now_W60_t120", "1-6h"),
    ("HPpers_all_H30", "0-1h"),
    ("HPknown_heat", "all"),
    ("HPknown_dhw", "all"),
    ("HPknown_heat+cool", "all"),
    ("HPknown_all", "all"),
)

PRESETS: dict[str, dict[str, Any]] = {
    "autumn-2026": {
        "from": "2026-08-01",
        "to": "2026-11-18T09:45",
        "data_end": "2026-11-18T10:00",
        "history_days": 37,
        "dst_change": "2026-10-25",
        "periods": [
            "all=2026-01-01",
            "Aug-Sep(cooling)=2026-01-01,2026-10-01",
            "Oct1-Nov2(shoulder,no batt)=2026-10-01,2026-11-03T19:15",
            "post-DST(Oct25+)=2026-10-25",
            "batt-live(Nov4+)=2026-11-04",
        ],
        "check_gross": "2026-11-03T19:30",
        "diag": ["2026-10-11,2026-10-24", "2026-10-26,2026-11-17"],
    },
    "spring-2026": {
        "from": "2026-03-17",
        "to": "2026-05-15",
        "data_end": None,
        "history_days": 30,
        "dst_change": "2026-03-29",
        "periods": [
            "all=2026-01-01",
            "pre-DST(Mar17-28)=2026-01-01,2026-03-29",
            "post-DST(Mar29-Apr25)=2026-03-29,2026-04-26",
            "after(Apr26+)=2026-04-26",
        ],
        "check_gross": None,
        "diag": None,
    },
}


def parse_utc(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Slot table
# ---------------------------------------------------------------------------


def ffill_limit(x: np.ndarray, limit: int) -> np.ndarray:
    """Forward fill NaN by at most *limit* samples."""
    idx = np.maximum.accumulate(np.where(np.isnan(x), -1, np.arange(x.size)))
    out = np.where(idx >= 0, x[np.maximum(idx, 0)], np.nan)
    out[(np.arange(x.size) - idx) > limit] = np.nan
    return out


def rolling_mean(x: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    """Trailing mean over *window* samples (ending at the sample), NaN below *min_periods*."""
    valid = ~np.isnan(x)
    csum = np.concatenate([[0.0], np.cumsum(np.where(valid, x, 0.0))])
    ccnt = np.concatenate([[0], np.cumsum(valid)])
    i = np.arange(1, x.size + 1)
    lo = np.maximum(i - window, 0)
    n = ccnt[i] - ccnt[lo]
    with np.errstate(all="ignore"):
        return np.where(n >= min_periods, (csum[i] - csum[lo]) / n, np.nan)


def slot_mean(x_min: np.ndarray, min_samples: int = 1) -> np.ndarray:
    """Mean of each 15 minutes of a minute array, NaN with fewer than *min_samples* samples."""
    m = x_min[: x_min.size // 15 * 15].reshape(-1, 15)
    n = (~np.isnan(m)).sum(1)
    with np.errstate(all="ignore"):
        out = np.nansum(m, 1) / n
    return np.where(n >= min_samples, out, np.nan)


def fetch_minutes(
    prom: lc.Prometheus,
    rd: lc.RunDir,
    exprs: dict[str, str],
    start: int,
    stop: int,
    tag: str,
) -> dict[str, np.ndarray]:
    """Minute arrays (NaN where absent) of each expression over ``[start, stop)`` epoch seconds."""
    n = (stop - start) // 60
    out = {}
    for name, expr in exprs.items():

        def one(expr=expr):
            series = prom.grid(
                expr, datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(stop, UTC), 60
            )
            return lc.to_arrays(series)

        arrays = rd.cached(f"raw_{tag}_{name}", one)
        print(f"  {name}: {arrays['ts'].size} samples", file=sys.stderr, flush=True)
        out[name] = lc.on_grid(lc.from_arrays(arrays), start, 60, n)
    return out


class Table:
    """The 15-minute UTC slot table of the window."""

    def __init__(self, minutes: dict[str, np.ndarray], start: int) -> None:
        r = minutes
        self.start = start
        self.n = r["grid"].size // 15
        gross = r["grid"] + np.where(np.isnan(r["ac"]), 0.0, r["ac"])
        self.main = slot_mean(gross, 12)
        self.heat = slot_mean(r["hp_heat"], 12)
        self.dhw = slot_mean(r["hp_dhw"], 12)
        self.cool = slot_mean(r["hp_cool"], 12)
        self.price = slot_mean(ffill_limit(r["price"], 180))
        self.epoch = start + SLOT_S * np.arange(self.n)
        # what is known at the issue time t (a slot boundary): minutes t-5 .. t-1
        tot = r["hp_heat"] + r["hp_dhw"] + r["hp_cool"]  # NaN unless all three are known
        starts = np.arange(self.n) * 15

        def now5(x: np.ndarray) -> np.ndarray:
            rm = rolling_mean(x, 5, 4)
            return np.where(starts > 0, rm[np.maximum(starts - 1, 0)], np.nan)

        self.hp_now5 = now5(tot)
        self.heat_now5 = now5(r["hp_heat"])
        self.dhw_now5 = now5(r["hp_dhw"])
        self.cool_now5 = now5(r["hp_cool"])


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


class Candidates:
    def __init__(self, t: Table, first_issue: int, last_issue: int) -> None:
        self.t = t
        n = t.n
        self.day_label, self.hour, self.minute = lc.local_fields(t.epoch)
        self.lhour = self.hour + self.minute / 60.0
        self.days = lc.day_bounds(self.day_label)
        self.day_of_slot = np.empty(n, dtype=np.int64)
        self.pos = np.empty(n, dtype=np.int64)
        for j, (_label, a, b) in enumerate(self.days):
            self.day_of_slot[a:b] = j
            self.pos[a:b] = np.arange(b - a)
        self.issues = np.arange(first_issue, last_issue)
        k = np.arange(HORIZON)
        self.valid = (self.issues[:, None] + k[None, :]) < n
        self.j = np.minimum(self.issues[:, None] + k[None, :], n - 1)
        self.same_mask = self.day_of_slot[self.j] == self.day_of_slot[self.issues][:, None]
        self.act = np.where(self.valid, t.main[self.j], np.nan)
        self.price = np.where(self.valid, t.price[self.j], np.nan)
        self.h_mid = (k + 0.5) * 15.0

    # -- profiles (LF-0 family) ------------------------------------------------

    def local_profile(
        self, x: np.ndarray, name: str = "trailing_mean"
    ) -> tuple[np.ndarray, np.ndarray]:
        """(same, prev) forecast of every slot: cut at its own local-day start, or the day before.

        The package forecaster runs once per local day; the day before leaves a window of 27 days.
        """
        keep = np.isfinite(x)
        load = np.where(keep, x, 0.0)
        same = np.zeros(x.size)
        prev = np.zeros(x.size)
        for j, (_label, a, b) in enumerate(self.days):
            same[a:b] = run_forecaster(
                name, load_kwh=load, usable=keep, days=self.days, cutoff_idx=a, target_days=(j,)
            ).values[a:b]
            if j:
                prev[a:b] = run_forecaster(
                    name,
                    load_kwh=load,
                    usable=keep,
                    days=self.days,
                    cutoff_idx=self.days[j - 1][1],
                    target_days=(j,),
                ).values[a:b]
        return same, prev

    def utc_profile(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Same-slot mean over the previous 28 days on the UTC clock (``h - 96 n``)."""
        n = x.size
        cut_same = np.array([self.days[d][1] for d in self.day_of_slot])
        cut_prev = np.array([self.days[max(d - 1, 0)][1] for d in self.day_of_slot])
        i = np.arange(n)
        out = []
        for cut in (cut_same, cut_prev):
            total = np.zeros(n)
            count = np.zeros(n)
            for m in range(1, WINDOW_DAYS + 1):
                h = i - 96 * m
                ok = (h >= 0) & (h < cut)
                v = np.where(ok, x[np.clip(h, 0, n - 1)], np.nan)
                finite = np.isfinite(v)
                total += np.where(finite, v, 0.0)
                count += finite
            with np.errstate(all="ignore"):
                out.append(np.where(count > 0, total / count, np.nan))
        return out[0], out[1]

    def pick(self, profile: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
        return np.where(self.same_mask, profile[0][self.j], profile[1][self.j])

    # -- the candidates --------------------------------------------------------

    def build(self) -> dict[str, np.ndarray]:
        t, j, valid = self.t, self.j, self.valid
        main, heat, dhw, cool = t.main, t.heat, t.dhw, t.cool
        hp = heat + dhw + cool
        lp = self.local_profile
        lf0 = lp(main)
        f: dict[str, np.ndarray] = {
            "LF0": self.pick(lf0),
            "median28": self.pick(lp(main, "trailing_median")),
            "LF0_utc": self.pick(self.utc_profile(main)),
        }
        nodhw = lp(main - dhw)
        dhw_utc = self.utc_profile(dhw)
        f["LF0_dhwUTC"] = self.pick((nodhw[0] + dhw_utc[0], nodhw[1] + dhw_utc[1]))
        resid = main - lf0[0]

        def anomaly(window: int) -> np.ndarray:
            ok = np.isfinite(resid)
            csum = np.concatenate([[0.0], np.cumsum(np.where(ok, resid, 0.0))])
            ccnt = np.concatenate([[0.0], np.cumsum(ok.astype(float))])
            i = np.arange(main.size)
            lo = np.maximum(i - window, 0)
            s, c = csum[i] - csum[lo], ccnt[i] - ccnt[lo]
            with np.errstate(all="ignore"):
                return np.where(c >= max(1, window // 2), s / c, 0.0)

        for w, tau in itertools.product(NOWCAST_WINDOWS, NOWCAST_TAUS):
            a = anomaly(w)[self.issues][:, None]
            f[f"now_W{w * 15}_t{tau}"] = f["LF0"] + a * np.exp(-self.h_mid[None, :] / tau)

        def known(profile: tuple[np.ndarray, np.ndarray], component: np.ndarray) -> np.ndarray:
            return self.pick(profile) + np.where(valid, component[j], 0.0)

        f["HPknown_heat"] = known(lp(main - heat), heat)
        f["HPknown_heat+cool"] = known(lp(main - heat - cool), heat + cool)
        f["HPknown_all"] = known(lp(main - hp), hp)
        f["HPknown_dhw"] = known(nodhw, dhw)
        hp_lf = self.pick(lp(hp))
        nohp = self.pick(lp(main - hp))
        now5 = t.hp_now5[self.issues][:, None]
        for h in (30, 60, 120):
            f[f"HPpers_all_H{h}"] = nohp + np.where(self.h_mid[None, :] <= h, now5, hp_lf)
        now_dh = (t.dhw_now5 + t.heat_now5)[self.issues][:, None]
        dh_lf = self.pick(lp(heat + dhw))
        base_rest = self.pick(lp(main - heat - dhw))
        for h in (30, 60):
            f[f"HPpers_dhw+heat_H{h}"] = base_rest + np.where(
                self.h_mid[None, :] <= h, now_dh, dh_lf
            )
        return {k: np.maximum(v, 0.0) for k, v in f.items()}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def parse_periods(specs: list[str]) -> dict[str, tuple[int, int]]:
    out = {}
    for spec in specs:
        name, _, rest = spec.partition("=")
        a, _, b = rest.partition(",")
        out[name] = (
            int(parse_utc(a).timestamp()),
            int(parse_utc(b).timestamp()) if b else 2**62,
        )
    return out


def score(
    c: Candidates, f: dict[str, np.ndarray], periods: dict[str, tuple[int, int]]
) -> list[dict[str, Any]]:
    k = np.arange(HORIZON)
    buckets = {"0-1h": k < 4, "1-6h": (k >= 4) & (k < 24), "6-24h": k >= 24, "all": k >= 0}
    eve = (c.lhour[c.j] >= 18) & (c.lhour[c.j] < 21)
    issue_epoch = c.t.epoch[c.issues]
    absolute = {name: np.abs(v - c.act) for name, v in f.items()}
    signed = {name: v - c.act for name, v in f.items()}
    finite_price = np.isfinite(c.price)
    rows = []
    for pname, (a, b) in periods.items():
        pm = ((issue_epoch >= a) & (issue_epoch < b))[:, None]
        for bname, bm in buckets.items():
            for win, wm in (("any", None), ("18-21", eve)):
                msk = pm & bm[None, :] & finite_price
                if wm is not None:
                    msk = msk & wm
                base = None
                for name in f:
                    e = absolute[name]
                    ok = msk & np.isfinite(e)
                    w = np.where(ok, c.price, 0.0)
                    mae = e[ok].mean()
                    pw = (np.where(ok, e, 0.0) * w).sum() / w.sum()
                    bias = signed[name][ok].mean()
                    if name == BASE:
                        base = (mae, pw)
                    rows.append(
                        {
                            "period": pname,
                            "bucket": bname,
                            "win": win,
                            "cand": name,
                            "mae": mae,
                            "pw": pw,
                            "bias": bias,
                            "n": int(ok.sum()),
                            "mae_pct": 0.0 if base is None else 100 * (mae / base[0] - 1),
                            "pw_pct": 0.0 if base is None else 100 * (pw / base[1] - 1),
                        }
                    )
    return rows


def print_tables(rows: list[dict[str, Any]], periods: dict[str, Any]) -> None:
    index = {(r["period"], r["win"], r["cand"], r["bucket"]): r for r in rows}
    for pname in periods:
        for win in ("any", "18-21"):
            print(f"\n--- {pname} / target window {win} (MAE and price-weighted pw, W; % vs LF-0)")
            head = ["candidate"]
            for b in ("0-1h", "1-6h", "6-24h", "all"):
                head += [f"{b} MAE", "pw", "pw%"]
            out = []
            for cand in TABLE:
                if (pname, win, cand, "all") not in index:
                    continue
                line = [cand]
                for b in ("0-1h", "1-6h", "6-24h", "all"):
                    r = index[(pname, win, cand, b)]
                    line += [
                        f"{r['mae']:.1f}",
                        f"{r['pw']:.1f}",
                        f"{r['pw_pct']:+.1f}",
                    ]
                out.append(line)
            widths = [max(len(str(x)) for x in col) for col in zip(head, *out, strict=True)]
            fmt = "  ".join(f"{{:>{w}}}" for w in widths)
            print(fmt.format(*head))
            for line in out:
                print(fmt.format(*line))


def bootstrap(
    c: Candidates, f: dict[str, np.ndarray], dst_ordinal: int, draws: int
) -> list[dict[str, Any]]:
    """Percent change of the price-weighted absolute error against LF-0 with a 95 % interval.

    Resampling unit: local weeks of issue days (paired by day, so candidate and LF-0 share it).
    """
    issue_day = c.day_label[c.issues]
    udays = np.unique(issue_day)
    week = (udays - udays[0]) // 7
    slot_of_day = np.searchsorted(udays, issue_day)
    k = np.arange(HORIZON)
    buckets = {"0-1h": k < 4, "1-6h": (k >= 4) & (k < 24), "6-24h": k >= 24}
    eve = (c.lhour[c.j] >= 18) & (c.lhour[c.j] < 21)
    rng = np.random.default_rng(1)

    def daily(v: np.ndarray, cols: np.ndarray, win: bool) -> np.ndarray:
        m = np.zeros(c.act.shape, bool)
        m[:, cols] = True
        if win:
            m &= eve
        e = np.abs(v - c.act) * c.price * 0.25 / 1000.0
        per_issue = np.where(m & np.isfinite(e), e, 0.0).sum(axis=1)
        return np.bincount(slot_of_day, weights=per_issue, minlength=udays.size)

    out = []
    for cand, bname in BOOTSTRAP:
        cols = np.ones(HORIZON, bool) if bname == "all" else buckets[bname]
        for win in (False, True):
            base = daily(f[BASE], cols, win)
            cur = daily(f[cand], cols, win)
            for per, lo in ((None, None), ("post-DST", dst_ordinal)):
                sel = np.ones(udays.size, bool) if lo is None else udays >= lo
                ds, bs, ws = (cur - base)[sel], base[sel], week[sel]
                unique_weeks = np.unique(ws)
                est = 100 * ds.sum() / bs.sum()
                boots = []
                for _ in range(draws):
                    pick = rng.choice(unique_weeks, unique_weeks.size)
                    wi = np.concatenate([np.flatnonzero(ws == x) for x in pick])
                    boots.append(100 * ds[wi].sum() / bs[wi].sum())
                out.append(
                    {
                        "cand": cand,
                        "bucket": bname,
                        "win": "18-21" if win else "any",
                        "period": per or "all",
                        "pw_pct": round(est, 1),
                        "lo": round(float(np.percentile(boots, 2.5)), 1),
                        "hi": round(float(np.percentile(boots, 97.5)), 1),
                    }
                )
    return out


# ---------------------------------------------------------------------------
# Side checks
# ---------------------------------------------------------------------------


def check_gross(prom: lc.Prometheus, rd: lc.RunDir, t: Table, since: datetime) -> None:
    """Gross load from power (grid + battery AC) against the energy counters, from *since*."""
    stop = t.start + t.n * SLOT_S
    counters = fetch_minutes(prom, rd, COUNTERS, t.start, stop, "ctr")
    ff = {k: ffill_limit(v, 10**9) for k, v in counters.items()}
    first = int(since.timestamp())
    idx = np.arange(first, stop - SLOT_S, SLOT_S)
    a = (idx - t.start) // 60
    b = a + 15

    def delta(name: str) -> np.ndarray:
        x = ff[name]
        ok = b < x.size
        out = np.full(idx.size, np.nan)
        out[ok] = x[b[ok]] - x[a[ok]]
        return out

    counter_w = (delta("gridkwh") - delta("chg") + delta("dis")) * 4000.0
    slot = (idx - t.start) // SLOT_S
    ok = (counter_w > 0) & (counter_w < 12000) & np.isfinite(t.main[slot])
    e = t.main[slot][ok] - counter_w[ok]
    print("\n== Gross load from power vs energy counters (grid import - charge + discharge)")
    print(
        f"n {e.size}  MAE {np.abs(e).mean():.1f} W  median |e| {np.median(np.abs(e)):.1f} W  "
        f"bias {e.mean():.1f} W"
    )


def diagnose(t: Table, c: Candidates, ranges: list[str]) -> None:
    """Mean hot-water, rest and heating power by local hour in two periods (UTC dates)."""
    hour = c.hour
    rest = t.main - t.dhw
    print("\n== Mean power by local hour (W): hot water, load without hot water, heating")
    cols = {}
    for label, spec in zip(("pre", "post"), ranges, strict=True):
        a, b = (int(parse_utc(x).timestamp()) for x in spec.split(","))
        sel = (t.epoch >= a) & (t.epoch < b + 86400)
        for name, x in (("dhw", t.dhw), ("nodhw", rest), ("heat", t.heat)):
            cols[f"{name}_{label}"] = [np.nanmean(x[sel & (hour == h)]) for h in range(24)]
    print("hour " + " ".join(f"{k:>10}" for k in cols))
    for h in range(24):
        print(f"{h:>4} " + " ".join(f"{cols[k][h]:>10.0f}" for k in cols))


def csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--preset", choices=sorted(PRESETS), default=None)
    parser.add_argument("--from", dest="since", default=None, help="first local day of issues")
    parser.add_argument("--to", dest="until", default=None, help="end of issues (UTC, exclusive)")
    parser.add_argument("--data-end", default=None)
    parser.add_argument("--history-days", type=int, default=None)
    parser.add_argument("--period", action="append", default=None, help="NAME=START[,END] (UTC)")
    parser.add_argument("--dst-change", default=None, help="UTC date of the clock change")
    parser.add_argument("--check-gross", nargs="?", const="", default=None, help="from (UTC)")
    parser.add_argument(
        "--diag", action="store_true", help="hot-water profile around the clock change"
    )
    parser.add_argument("--bootstrap", type=int, default=2000, help="draws (0 to skip)")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)
    preset = PRESETS[args.preset] if args.preset else {}

    def opt(name: str, given: Any) -> Any:
        return given if given is not None else preset.get(name)

    since_s, until_s = opt("from", args.since), opt("to", args.until)
    if since_s is None or until_s is None:
        parser.error("give --preset or both --from and --to")
    history = opt("history_days", args.history_days) or 40
    first_local = date.fromisoformat(since_s)
    until = parse_utc(until_s)
    data_end_s = opt("data_end", args.data_end)
    data_end = parse_utc(data_end_s) if data_end_s else until + timedelta(days=1)
    data_end = min(data_end, datetime.now(UTC))
    periods = parse_periods(args.period or preset.get("periods") or ["all=2000-01-01"])
    dst = date.fromisoformat(opt("dst_change", args.dst_change) or "2000-01-01")
    start = lc.local_midnight_epoch(first_local - timedelta(days=history))
    stop = int(data_end.timestamp()) // 60 * 60
    rd = lc.RunDir(TOOL, args.run_id, args.data_dir)
    prom = lc.Prometheus()
    try:
        print(f"fetching {lc.utc_iso(start)} .. {lc.utc_iso(stop)} UTC", file=sys.stderr)
        minutes = fetch_minutes(prom, rd, ENTITIES, start, stop, "min")
        table = Table(minutes, start)
        first_issue = int(np.searchsorted(table.epoch, lc.local_midnight_epoch(first_local)))
        last_issue = int(np.searchsorted(table.epoch, int(until.timestamp())))
        print(
            f"{table.n} slots, issues {first_issue}..{last_issue} "
            f"({lc.utc_iso(table.epoch[first_issue])} .. "
            f"{lc.utc_iso(table.epoch[last_issue - 1])} UTC)"
        )
        if args.check_gross is not None:
            since_gross = args.check_gross or preset.get("check_gross")
            if not since_gross:
                parser.error("--check-gross needs a UTC start (no preset default)")
            check_gross(prom, rd, table, parse_utc(since_gross))
    finally:
        prom.close()
    cand = Candidates(table, first_issue, last_issue)
    f = cand.build()
    rows = score(cand, f, periods)
    print_tables(rows, periods)
    curves = {name: np.nanmean(np.abs(v - cand.act), axis=0) for name, v in f.items()}
    print("\n== Mean absolute error by horizon, all issues (W per slot)")
    horizons = (1, 2, 4, 8, 12, 24, 48, 96)
    print("candidate".ljust(24) + " ".join(f"{15 * h:>6}min" for h in horizons))
    for name in TABLE:
        if name in curves:
            print(name.ljust(24) + " ".join(f"{curves[name][h - 1]:>9.1f}" for h in horizons))
    boot = []
    if args.bootstrap:
        dst_ordinal = date.fromisoformat(str(dst)).toordinal()
        boot = bootstrap(cand, f, dst_ordinal, args.bootstrap)
        print("\n== Price-weighted MAE vs LF-0, percent, 95 % interval over local weeks")
        print("candidate".ljust(20) + "bucket  window  period      pw%     lo     hi")
        for r in boot:
            print(
                f"{r['cand']:<20}{r['bucket']:<8}{r['win']:<8}{r['period']:<10}"
                f"{r['pw_pct']:>6.1f} {r['lo']:>6.1f} {r['hi']:>6.1f}"
            )
    if args.diag:
        ranges = preset.get("diag")
        if not ranges:
            parser.error("--diag needs a preset with periods before and after a clock change")
        diagnose(table, cand, ranges)
    rd.write("results_all.csv", csv_bytes(rows))
    curve_rows = [
        {"horizon_min": 15 * (i + 1), **{n: float(curves[n][i]) for n in curves}}
        for i in range(HORIZON)
    ]
    rd.write("mae_by_horizon.csv", csv_bytes(curve_rows))
    if boot:
        rd.write("bootstrap_pw_pct.csv", csv_bytes(boot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
