#!/usr/bin/env python3
"""Walk-forward accuracy of the 14:00 estimates of tomorrow's PVPC (read-only analysis).

At the 14:00 decision on day D tomorrow's PVPC is not published (REE releases it at about 20:15)
but OMIE's day-ahead price for D+1 is public from about 13:00.  For every target day T with
D = T - 1 this script estimates the 24 hourly PVPC prices of T from what was known at 13:30 on D
and scores them against the realised prices
(``docs/planning/price-outlook-improvement-plan.md``, section 4):

* ``omie_period``: the repo's estimator (``battery_optimizer.omie_outlook``, run through
  ``price_outlook.run_price_outlook``): ``PVPC = beta * OMIE_hour + intercept(2.0TD period)``
  fitted on the last 28 published local days.
* ``omie_q4``: the 15-minute variant: the four quarter-hour OMIE prices of the hour as separate
  regressors, plus the period intercepts (PVPC itself is hourly).
* ``persistence``: the repo's outlook: today's published price at the same clock time.
* ``weekly``: the same weekday of last week (published for D at the decision).
* ``omie_2slope``: **diagnostic only**: separate slopes for the day mean of OMIE and for the
  within-day deviation, plus the period intercepts.  Chosen after seeing the windows, not
  validated out of sample.

Metrics per window and estimator (hourly rows; the daily ones average over target days): MAE and
bias (EUR/kWh and percent of the mean price), RMSE, hit rates within +-10/20/30 percent and within
1/2/3 cents, within-day Spearman, overlap of the cheapest 4 / 8 and the dearest 4 hours and the
regret of the pick, daily spread error, hour buckets, weekday splits, the share of the squared
error of ``omie_period`` that is a daily level offset, the within-day and between-day slope of
PVPC on OMIE, a check that the PVPC sensor is constant within an hour, and the agreement of the
repo's ``omie_period`` and ``persistence`` with in-script reference fits (``--no-reference`` off).

Usage::

    uv run python hack/price_estimate_accuracy.py --fetch-omie [--run-id <name>] \\
        [--window NAME:FROM:TO ...] [--data-dir ~/data/<store>]

``--window`` takes **target** local dates, ``FROM`` inclusive and ``TO`` exclusive (default: the two
windows of the 2026-11-18 re-check, ``Oct-Nov:2026-10-01:2026-11-18`` and
``Jun-Sep:2026-06-01:2026-10-01``).  ``--fetch-omie`` first fetches the OMIE days the store lacks
with the repo's own ``extract_omie``.  Needs ``BATTERY_OPTIMIZER_PROMETHEUS_URL`` for the pull and
``BATTERY_OPTIMIZER_DATA_DIR`` (or ``--data-dir``).  The pulled hourly series, the report and the
metrics table are written to ``<data_dir>/analysis/v1/price-estimate-accuracy/<run-id>/``; the
repository is never touched.  A local day on which the PVPC sensor read 0 all day is a data hole:
it is masked, a hole target is skipped and the hole rows stay out of every fit.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from scipy.stats import spearmanr

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "src"))

from battery_optimizer import extract_prom  # noqa: E402
from battery_optimizer.extract_omie import MARKET_TZ, extract_omie  # noqa: E402
from battery_optimizer.omie_outlook import tariff_period  # noqa: E402
from battery_optimizer.omie_prices import load_omie_es_prices  # noqa: E402
from battery_optimizer.paths import resolve_data_dir  # noqa: E402
from battery_optimizer.price_outlook import (  # noqa: E402
    SLOT_ESTIMATED,
    OutlookSide,
    run_price_outlook,
)
from battery_optimizer.service.config import load_service_config  # noqa: E402

TZ = ZoneInfo(MARKET_TZ.key)
SLOT_S = 900
FIT_DAYS = 28  # the repo's omie_period window (``OMIE_PERIOD_WINDOW_DAYS``)
HISTORY_DAYS = FIT_DAYS + 3  # days before the first target the fits need
DEFAULT_WINDOWS = ("Oct-Nov:2026-10-01:2026-11-18", "Jun-Sep:2026-06-01:2026-10-01")
ESTIMATORS = ("omie_period", "omie_q4", "omie_2slope", "persistence", "weekly")
BUCKET_ESTIMATORS = ("omie_period", "persistence", "weekly")
BUCKETS = (
    ("00-08", 0, 8),
    ("08-14", 8, 14),
    ("14-18", 14, 18),
    ("18-22", 18, 22),
    ("22-24", 22, 24),
)
MIN_REAL_FOR_PERCENT = 0.05  # EUR/kWh: below it a percent error is not meaningful (medAPE, MAPE)
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 0
DECISION_OMIE = (13, 30)  # local time on D from which the OMIE price of D+1 is used
DECISION_PERSISTENCE = (14, 0)


# ---------------------------------------------------------------------------
# Time and Prometheus helpers
# ---------------------------------------------------------------------------


def local_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(UTC)


@dataclass(frozen=True)
class Window:
    name: str
    first: date  # first target day, inclusive
    last: date  # last target day, inclusive


def parse_window(text: str) -> Window:
    match = re.fullmatch(r"([A-Za-z0-9._-]+):(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})", text)
    if not match:
        raise argparse.ArgumentTypeError(f"{text!r}: use NAME:FROM:TO (dates, TO exclusive)")
    first, end = date.fromisoformat(match[2]), date.fromisoformat(match[3])
    if end <= first:
        raise argparse.ArgumentTypeError(f"{text!r}: TO must be after FROM")
    return Window(match[1], first, end - timedelta(days=1))


def fetch_grid(client, base_url: str, query: str, first: int, step: int, n: int) -> np.ndarray:
    """One ``query_range`` series on the grid ``first + step * j`` (``NaN`` where nothing)."""
    out = np.full(n, np.nan)
    per_chunk = 400
    for j0 in range(0, n, per_chunk):
        j1 = min(j0 + per_chunk, n)
        params = {
            "query": query,
            "start": first + j0 * step,
            "end": first + (j1 - 1) * step,
            "step": step,
        }
        payload: dict = {}
        for attempt in range(4):
            try:
                response = client.get(f"{base_url}/api/v1/query_range", params=params)
                response.raise_for_status()
                payload = response.json()
                break
            except httpx.HTTPError, ValueError:
                if attempt == 3:
                    raise
                time.sleep(2.0 * (attempt + 1))
        if payload.get("status") != "success":
            raise RuntimeError(f"prometheus query failed: {payload.get('error')!r} ({query!r})")
        for series in payload["data"]["result"]:
            for stamp, text in series["values"]:
                offset = int(float(stamp)) - first
                value = float(text)
                if offset % step == 0 and 0 <= offset // step < n and np.isfinite(value):
                    out[offset // step] = value
    return out


def _write_bytes(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Inputs: hourly PVPC (Prometheus), OMIE (repo raw store)
# ---------------------------------------------------------------------------


def frame_bounds(windows: list[Window]) -> tuple[datetime, datetime]:
    start = min(w.first for w in windows) - timedelta(days=HISTORY_DAYS)
    end = max(w.last for w in windows) + timedelta(days=1)
    return local_midnight(start), local_midnight(end + timedelta(days=1))


def pull_pvpc(start: datetime, end: datetime) -> dict[str, np.ndarray]:
    """PVPC at minute 30 of every UTC hour, and at 8 minutes into every quarter-hour."""
    entity = load_service_config(None).entities.get("price")
    spec = extract_prom.ENTITY_SPECS[entity]
    query = f'max({spec.metric}{{entity="{extract_prom.escape_promql_string(entity)}"}})'
    first = int(start.timestamp())
    n_hours = int((end - start).total_seconds()) // 3600
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        base_url = extract_prom.resolve_base_url()
        print(f"pull: PVPC, {n_hours} hours", file=sys.stderr)
        hourly = fetch_grid(client, base_url, query, first + 1800, 3600, n_hours)
        quarter = fetch_grid(client, base_url, query, first + 480, SLOT_S, n_hours * 4)
    return {"pvpc_hour": hourly, "pvpc_quarter": quarter}


def load_inputs(start: datetime, end: datetime, directory: Path, refetch: bool) -> dict:
    path = directory / "inputs.npz"
    key = {"start": start.isoformat(), "end": end.isoformat()}
    if path.is_file() and not refetch:
        with np.load(path, allow_pickle=False) as stored:
            if json.loads(str(stored["meta"])) == key:
                print(f"inputs: reusing {path}", file=sys.stderr)
                return {k: stored[k] for k in stored.files if k != "meta"}
    pulled = pull_pvpc(start, end)
    buffer = io.BytesIO()
    np.savez_compressed(buffer, meta=np.array(json.dumps(key)), **pulled)
    _write_bytes(path, buffer.getvalue())
    print(f"inputs: written to {path}", file=sys.stderr)
    return pulled


def load_omie(start: datetime, end: datetime, starts, data_dir: str | None, fetch: bool):
    """OMIE ES prices (EUR/kWh) on the 15-minute frame grid, through the repo's raw store."""
    first = start.astimezone(TZ).date()
    last = (end - timedelta(seconds=1)).astimezone(TZ).date()
    stored = load_omie_es_prices(data_dir, start_date=first, end_date=last)
    if stored.missing_dates and fetch:
        print(
            f"omie: fetching {len(stored.missing_dates)} day(s) with extract_omie", file=sys.stderr
        )
        extract_omie(
            data_dir=data_dir,
            config_path=None,
            start=stored.missing_dates[0],
            end=stored.missing_dates[-1],
        )
        stored = load_omie_es_prices(data_dir, start_date=first, end_date=last)
    index = {s: i for i, s in enumerate(stored.slot_start)}
    values = np.full(len(starts), np.nan)
    for k, slot in enumerate(starts):
        i = index.get(slot)
        if i is not None:
            values[k] = stored.price_es_eur_mwh[i] / 1000.0
    return values, stored.missing_dates


class Frame:
    """Hourly table (UTC hours) and the 15-minute view the repo outlooks need."""

    def __init__(self, start: datetime, end: datetime, pvpc_hour, omie_quarter) -> None:
        n_hours = int((end - start).total_seconds()) // 3600
        self.hour_starts = [start + timedelta(hours=h) for h in range(n_hours)]
        self.starts = [start + timedelta(seconds=SLOT_S * i) for i in range(n_hours * 4)]
        pvpc = pvpc_hour.copy()
        pvpc[np.abs(pvpc) < 1e-6] = np.nan  # the sensor reading 0 (outage) is a hole, not a price
        self.pvpc = pvpc
        quarters = omie_quarter.reshape(n_hours, 4)
        self.q = quarters
        self.omie = np.where(np.isfinite(quarters).all(1), quarters.mean(1), np.nan)
        local = [t.astimezone(TZ) for t in self.hour_starts]
        self.date = np.array([x.toordinal() for x in local])
        self.hour = np.array([x.hour for x in local])
        self.period = np.array([tariff_period(x.date(), x.hour) for x in local])
        self.rows: dict[int, np.ndarray] = {}
        for day in np.unique(self.date).tolist():
            self.rows[day] = np.flatnonzero(self.date == day)
        # 15-minute view for the repo outlooks: PVPC repeated over the hour
        self.price15 = np.repeat(pvpc, 4)
        self.omie15 = omie_quarter
        local15 = [t.astimezone(TZ) for t in self.starts]
        ord15 = np.array([x.toordinal() for x in local15])
        self.lmin15 = np.array([x.hour * 60 + x.minute for x in local15])
        self.day15: dict[int, tuple[int, int]] = {}
        for day in np.unique(ord15).tolist():
            idx = np.flatnonzero(ord15 == day)
            self.day15[day] = (int(idx[0]), int(idx[-1]) + 1)
        self.days15 = [(date.fromordinal(d), a, b) for d, (a, b) in sorted(self.day15.items())]


# ---------------------------------------------------------------------------
# Estimators
# ---------------------------------------------------------------------------


def _design_period(omie: np.ndarray, period: np.ndarray) -> np.ndarray:
    return np.column_stack([omie] + [(period == p).astype(float) for p in (1, 2, 3)])


def _design_q4(f: Frame, rows: np.ndarray) -> np.ndarray:
    return np.column_stack(
        [f.q[rows, k] for k in range(4)] + [(f.period[rows] == p) for p in (1, 2, 3)]
    ).astype(float)


def _design_two_slope(f: Frame, rows: np.ndarray) -> np.ndarray:
    omie = f.omie[rows]
    day_mean = np.zeros(rows.size)
    for day in np.unique(f.date[rows]):
        sel = f.date[rows] == day
        day_mean[sel] = omie[sel].mean()
    return np.column_stack(
        [omie - day_mean, day_mean] + [(f.period[rows] == p) for p in (1, 2, 3)]
    ).astype(float)


def fit_rows(f: Frame, day: int, need_quarters: bool = False) -> np.ndarray:
    """Rows of the 28 local days ending on *day* with a known PVPC and OMIE hour."""
    rows = np.concatenate([f.rows[d] for d in range(day - FIT_DAYS + 1, day + 1) if d in f.rows])
    ok = np.isfinite(f.pvpc[rows]) & np.isfinite(f.omie[rows])
    if need_quarters:
        ok &= np.isfinite(f.q[rows]).all(1)
    return rows[ok]


def fit_predict(f: Frame, day: int, target: int, name: str) -> np.ndarray:
    """Reference least-squares estimators on the hourly table (walk-forward on *day*)."""
    t_rows = f.rows[target]
    if name == "omie_period":
        rows = fit_rows(f, day)
        coef, *_ = np.linalg.lstsq(
            _design_period(f.omie[rows], f.period[rows]), f.pvpc[rows], rcond=None
        )
        return _design_period(f.omie[t_rows], f.period[t_rows]) @ coef
    if name == "omie_q4":
        rows = fit_rows(f, day, need_quarters=True)
        coef, *_ = np.linalg.lstsq(_design_q4(f, rows), f.pvpc[rows], rcond=None)
        return _design_q4(f, t_rows) @ coef
    if name == "omie_2slope":
        rows = fit_rows(f, day)
        coef, *_ = np.linalg.lstsq(_design_two_slope(f, rows), f.pvpc[rows], rcond=None)
        return _design_two_slope(f, t_rows) @ coef
    raise ValueError(name)


def clock_map(f: Frame, day: int) -> np.ndarray:
    """PVPC of local day *day* by clock hour (mean over a repeated hour, gaps filled)."""
    rows = f.rows[day]
    out = np.full(24, np.nan)
    for h in range(24):
        v = f.pvpc[rows[f.hour[rows] == h]]
        v = v[np.isfinite(v)]
        if v.size:
            out[h] = v.mean()
    known = np.flatnonzero(np.isfinite(out))
    if known.size == 0:
        return out
    idx = np.arange(24)
    last = np.maximum.accumulate(np.where(np.isfinite(out), idx, -1))
    filled = np.where(last >= 0, out[np.maximum(last, 0)], out[known[0]])
    return filled


def clock_predict(f: Frame, source_day: int, target: int) -> np.ndarray:
    return clock_map(f, source_day)[f.hour[f.rows[target]]]


def repo_estimate(f: Frame, name: str, day: int, target: int, side) -> np.ndarray | None:
    """The repo outlook *name*, decided on local day *day*, for the hours of *target*."""
    a0, b0 = f.day15[day]
    b1 = f.day15[target][1]
    wanted = DECISION_OMIE if name == "omie_period" else DECISION_PERSISTENCE
    at = a0 + int(np.searchsorted(f.lmin15[a0:b0], wanted[0] * 60 + wanted[1]))
    result = run_price_outlook(
        name,
        prices=f.price15,
        published_end_idx=b0,
        decision_idx=at,
        plan_end_idx=b1,
        interval_starts_utc=f.starts,
        days=f.days15,
        tz=TZ,
        side=side,
    )
    tomorrow = result.values[b0 - at :]
    flags = result.provenance[b0 - at :]
    if tomorrow.size != f.rows[target].size * 4 or not np.all(flags == SLOT_ESTIMATED):
        return None
    return np.asarray(tomorrow, dtype=float).reshape(-1, 4).mean(1)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def day_stats(real: np.ndarray, pred: np.ndarray) -> dict[str, float]:
    """Within-day ranking and spread scores of one target day."""
    out = {}
    out["sp"] = float(spearmanr(real, pred)[0]) if np.std(pred) > 0 else np.nan

    def top(k: int, dearest: bool) -> tuple[float, float]:
        a = np.argsort(-real if dearest else real, kind="stable")[:k]
        b = np.argsort(-pred if dearest else pred, kind="stable")[:k]
        regret = real[b].mean() - real[a].mean()
        return len(set(a) & set(b)) / k, float(-regret if dearest else regret)

    for k, dearest, name in ((4, False, "c4"), (8, False, "c8"), (4, True, "e4")):
        out[name], out[name + "_reg"] = top(k, dearest)
    out["spr_err"] = float((pred.max() - pred.min()) - (real.max() - real.min()))
    out["spr_real"] = float(real.max() - real.min())
    return out


def summarize(rows: dict[str, np.ndarray]) -> tuple[dict[str, float], list[dict[str, float]]]:
    """Hourly and daily metrics of one estimator on one window."""
    real, pred = rows["real"], rows["pred"]
    err = pred - real
    ae = np.abs(err)
    mean = real.mean()
    rel = ae / np.maximum(np.abs(real), 1e-9)
    ok = real >= MIN_REAL_FOR_PERCENT
    o = {
        "n_h": float(real.size),
        "mean_price": mean,
        "MAE": ae.mean(),
        "MAE_pct": 100 * ae.mean() / mean,
        "bias": err.mean(),
        "bias_pct": 100 * err.mean() / mean,
        "RMSE": float(np.sqrt((err**2).mean())),
        "medAPE_pct": 100 * float(np.median(rel[ok])),
        "MAPE_pct": 100 * rel[ok].mean(),
    }
    for pct in (10, 20, 30):
        o[f"in{pct}"] = 100 * float((ae <= pct / 100 * np.abs(real)).mean())
    for cents in (1, 2, 3):
        o[f"in{cents}c"] = 100 * float((ae <= cents / 100).mean())
    days = []
    for day in np.unique(rows["target"]):
        sel = rows["target"] == day
        days.append(day_stats(real[sel], pred[sel]))
    col = {k: np.array([d[k] for d in days]) for k in days[0]}
    o["sp_mean"], o["sp_med"] = float(np.nanmean(col["sp"])), float(np.nanmedian(col["sp"]))
    for k in ("c4", "c8", "e4"):
        o[k + "_ovl"], o[k + "_reg"] = 100 * col[k].mean(), col[k + "_reg"].mean()
    o["spread_MAE"], o["spread_bias"] = np.abs(col["spr_err"]).mean(), col["spr_err"].mean()
    o["spread_real"] = col["spr_real"].mean()
    o["spread_MAE_pct"] = 100 * o["spread_MAE"] / o["spread_real"]
    return o, days


def level_offset_share(rows: dict[str, np.ndarray]) -> float:
    """Share of the squared error that is the daily mean error (a level offset)."""
    err = rows["pred"] - rows["real"]
    total = float((err**2).sum())
    level = 0.0
    for day in np.unique(rows["target"]):
        e = err[rows["target"] == day]
        level += e.size * float(e.mean()) ** 2
    return level / total


def slopes(f: Frame, targets: list[int]) -> tuple[float, float]:
    """Within-day and between-day OLS slope of PVPC on OMIE over the target days."""
    dev_x, dev_y, mean_x, mean_y = [], [], [], []
    for day in targets:
        rows = f.rows[day]
        rows = rows[np.isfinite(f.pvpc[rows]) & np.isfinite(f.omie[rows])]
        x, y = f.omie[rows], f.pvpc[rows]
        dev_x.append(x - x.mean())
        dev_y.append(y - y.mean())
        mean_x.append(x.mean())
        mean_y.append(y.mean())
    dx, dy = np.concatenate(dev_x), np.concatenate(dev_y)
    within = float((dx * dy).sum() / (dx * dx).sum())
    between = float(np.polyfit(mean_x, mean_y, 1)[0])
    return within, between


def hit_bootstrap(rows: dict[str, np.ndarray]) -> np.ndarray:
    """95 % intervals of MAE, within 10 %, within 20 % and MAE percent (day blocks)."""
    days = np.unique(rows["target"])
    err = np.abs(rows["pred"] - rows["real"])
    n = np.array([(rows["target"] == d).sum() for d in days], float)
    ae = np.array([err[rows["target"] == d].sum() for d in days])
    real_abs = np.abs(rows["real"])
    h10 = np.array(
        [(err[rows["target"] == d] <= 0.1 * real_abs[rows["target"] == d]).sum() for d in days],
        float,
    )
    h20 = np.array(
        [(err[rows["target"] == d] <= 0.2 * real_abs[rows["target"] == d]).sum() for d in days],
        float,
    )
    price = np.array([rows["real"][rows["target"] == d].sum() for d in days])
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    pick = rng.integers(0, days.size, (BOOTSTRAP_DRAWS, days.size))
    total_n = n[pick].sum(1)
    draws = np.column_stack(
        [
            ae[pick].sum(1) / total_n,
            100 * h10[pick].sum(1) / total_n,
            100 * h20[pick].sum(1) / total_n,
            100 * ae[pick].sum(1) / price[pick].sum(1),
        ]
    )
    return np.percentile(draws, [2.5, 97.5], axis=0)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def collect(f: Frame, windows: list[Window], side, reference: bool):
    """Estimates of every target day of every window; returns row arrays per estimator."""
    first = min(w.first for w in windows).toordinal()
    last = max(w.last for w in windows).toordinal()
    cols = {name: {k: [] for k in ("real", "pred", "target", "hour", "per")} for name in ESTIMATORS}
    reference_pairs = {"omie_period": [], "persistence": []}
    skipped = []
    unscored = dict.fromkeys(ESTIMATORS, 0)
    for target in range(first, last + 1):
        day = target - 1
        if target not in f.rows or day not in f.rows:
            continue
        t_rows = f.rows[target]
        if not any(w.first.toordinal() <= target <= w.last.toordinal() for w in windows):
            continue
        if np.isnan(f.pvpc[t_rows]).any() or np.isnan(f.omie[t_rows]).any():
            skipped.append(date.fromordinal(target).isoformat())
            continue
        estimates = {
            "omie_period": repo_estimate(f, "omie_period", day, target, side),
            "omie_q4": fit_predict(f, day, target, "omie_q4"),
            "omie_2slope": fit_predict(f, day, target, "omie_2slope"),
            "persistence": repo_estimate(f, "persistence", day, target, side),
            "weekly": clock_predict(f, target - 7, target) if target - 7 in f.rows else None,
        }
        if reference:
            reference_pairs["omie_period"].append(
                (estimates["omie_period"], fit_predict(f, day, target, "omie_period"))
            )
            reference_pairs["persistence"].append(
                (estimates["persistence"], clock_predict(f, day, target))
            )
        for name, pred in estimates.items():
            if pred is None:
                unscored[name] += t_rows.size
                continue
            scored = np.isfinite(pred)  # a source hour that is a data hole leaves a hole
            unscored[name] += int((~scored).sum())
            c = cols[name]
            c["real"].append(f.pvpc[t_rows][scored])
            c["pred"].append(pred[scored])
            c["target"].append(np.full(t_rows.size, target)[scored])
            c["hour"].append(f.hour[t_rows][scored])
            c["per"].append(f.period[t_rows][scored])
    arrays = {
        n: {k: np.concatenate(v) if v else np.array([]) for k, v in c.items()}
        for n, c in cols.items()
    }
    return arrays, reference_pairs, skipped, unscored


def window_rows(arrays: dict, window: Window) -> dict[str, dict[str, np.ndarray]]:
    out = {}
    for name, rows in arrays.items():
        sel = (rows["target"] >= window.first.toordinal()) & (
            rows["target"] <= window.last.toordinal()
        )
        out[name] = {k: v[sel] for k, v in rows.items()}
    return out


def report_window(out: list[str], f: Frame, window: Window, rows: dict, csv_rows: list) -> None:
    """The tables of one window."""
    targets = np.unique(rows["omie_period"]["target"]).tolist()
    out.append(
        f"\n===== window {window.name}: targets {window.first}..{window.last} "
        f"({len(targets)} scored days) ====="
    )
    metrics, daily = {}, {}
    for name in ESTIMATORS:
        metrics[name], daily[name] = summarize(rows[name])
        for key, value in metrics[name].items():
            csv_rows.append((window.name, name, key, f"{value:.6g}"))
    keys = list(metrics["omie_period"])
    out.append(f"{'':16s}" + "".join(f"{n:>13s}" for n in ESTIMATORS))
    for key in keys:
        out.append(f"{key:16s}" + "".join(f"{metrics[n][key]:13.3f}" for n in ESTIMATORS))
    ci = hit_bootstrap(rows["omie_period"])
    out.append(
        "omie_period 95% bootstrap interval (day blocks) of MAE, in10, in20, MAE%: "
        + ", ".join(f"[{ci[0, i]:.3f}, {ci[1, i]:.3f}]" for i in range(4))
    )
    share = level_offset_share(rows["omie_period"])
    within, between = slopes(f, targets)
    out.append(
        f"omie_period: {100 * share:.0f}% of the squared error is a daily level offset; PVPC on "
        f"OMIE slope {within:.2f} within the day, {between:.2f} between days"
    )
    out.append("\nby hour bucket (target local hour):")
    out.append(
        f"{'estimator':12s} {'bucket':6s} {'mean':>7s} {'MAE':>7s} {'MAE%':>7s} {'bias':>7s} "
        f"{'in10':>6s} {'in20':>6s}"
    )
    for name in BUCKET_ESTIMATORS:
        r = rows[name]
        for label, lo, hi in BUCKETS:
            sel = (r["hour"] >= lo) & (r["hour"] < hi)
            err = r["pred"][sel] - r["real"][sel]
            real = r["real"][sel]
            out.append(
                f"{name:12s} {label:6s} {real.mean():7.3f} {np.abs(err).mean():7.3f} "
                f"{100 * np.abs(err).mean() / real.mean():7.1f} {err.mean():7.3f} "
                f"{100 * (np.abs(err) <= 0.1 * np.abs(real)).mean():6.1f} "
                f"{100 * (np.abs(err) <= 0.2 * np.abs(real)).mean():6.1f}"
            )
    r = rows["omie_period"]
    out.append("\nomie_period splits:")
    weekday = np.array([date.fromordinal(int(t)).weekday() for t in r["target"]])
    for label, sel in (
        ("weekday", weekday < 5),
        ("weekend", weekday >= 5),
        *((f"period P{p}", r["per"] == p) for p in (1, 2, 3)),
        *((f"dow {i}", weekday == i) for i in range(7)),
    ):
        err = r["pred"][sel] - r["real"][sel]
        real = r["real"][sel]
        out.append(
            f"  {label:10s} n {sel.sum():5d} MAE {np.abs(err).mean():.3f} bias {err.mean():+.3f} "
            f"mean {real.mean():.3f} in10 {100 * (np.abs(err) <= 0.1 * np.abs(real)).mean():5.1f} "
            f"in20 {100 * (np.abs(err) <= 0.2 * np.abs(real)).mean():5.1f}"
        )
    real = r["real"]
    out.append(
        f"realised hourly price: mean {real.mean():.3f}, std {real.std():.3f}, "
        f"min {real.min():.3f}, median {np.median(real):.3f}, max {real.max():.3f}; "
        f"hours below zero: {int((real < 0).sum())}; "
        f"days with a minimum below 0.03: "
        f"{sum(real[r['target'] == t].min() < 0.03 for t in targets)} of {len(targets)}"
    )
    per_day = {
        t: (
            float(np.abs(r["pred"][r["target"] == t] - real[r["target"] == t]).mean()),
            float((r["pred"][r["target"] == t] - real[r["target"] == t]).mean()),
            float(
                100
                * (
                    np.abs(r["pred"][r["target"] == t] - real[r["target"] == t])
                    <= 0.2 * np.abs(real[r["target"] == t])
                ).mean()
            ),
        )
        for t in targets
    }
    worst = sorted(targets, key=lambda t: -per_day[t][0])[:8]
    out.append("worst 8 days by MAE (date, MAE, bias, % within 20%):")
    for t in worst:
        out.append(
            f"  {date.fromordinal(t)}  {per_day[t][0]:.3f}  {per_day[t][1]:+.3f}  "
            f"{per_day[t][2]:5.1f}"
        )
    in20 = np.array([per_day[t][2] for t in targets])
    out.append(
        "share of hours within 20% by day, quantiles 10/25/50/75/90: "
        + " ".join(f"{q:.1f}" for q in np.percentile(in20, [10, 25, 50, 75, 90]))
    )


def within_hour_check(f: Frame, windows: list[Window]) -> str:
    """Share of UTC hours in which the PVPC sensor read more than one value."""
    first = min(w.first for w in windows).toordinal()
    last = max(w.last for w in windows).toordinal()
    quarters = f.price15.reshape(-1, 4)
    sel = np.array([first <= d <= last for d in f.date])
    ok = sel & np.isfinite(quarters).all(1)
    changed = ok & (np.ptp(quarters, axis=1) > 1e-9)
    return (
        f"PVPC within-hour check: {int(changed.sum())} of {int(ok.sum())} UTC hours with four "
        "quarter-hour samples show more than one value"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--window", dest="windows", action="append", type=parse_window)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--run-id", default="default")
    parser.add_argument("--fetch-omie", action="store_true", help="fetch missing OMIE days first")
    parser.add_argument("--refetch", action="store_true", help="ignore the inputs of the run dir")
    parser.add_argument("--no-reference", action="store_true", help="skip the reference fits check")
    args = parser.parse_args(argv)
    windows = args.windows or [parse_window(w) for w in DEFAULT_WINDOWS]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.run_id) or ".." in args.run_id:
        raise SystemExit(f"--run-id {args.run_id!r}: use only [A-Za-z0-9._-] and no '..'")
    directory = (
        resolve_data_dir(args.data_dir)
        / "analysis"
        / "v1"
        / "price-estimate-accuracy"
        / args.run_id
    )
    directory.mkdir(parents=True, exist_ok=True)
    start, end = frame_bounds(windows)
    pulled = load_inputs(start, end, directory, args.refetch)
    starts = [
        start + timedelta(seconds=SLOT_S * i)
        for i in range(int((end - start).total_seconds()) // SLOT_S)
    ]
    omie15, missing = load_omie(start, end, starts, args.data_dir, args.fetch_omie)
    if missing:
        print(
            f"omie: {len(missing)} day(s) without a stored price: {missing[0]}..{missing[-1]}",
            file=sys.stderr,
        )
    f = Frame(start, end, pulled["pvpc_hour"], omie15)
    side = OutlookSide(f.omie15)
    arrays, reference_pairs, skipped, unscored = collect(f, windows, side, not args.no_reference)
    out = [
        "walk-forward accuracy of the 14:00 estimates of tomorrow's PVPC (hourly, EUR/kWh)",
        f"skipped target days (a missing PVPC or OMIE hour): {skipped or 'none'}",
        "hours an estimator could not price (left out of its scores): "
        + ", ".join(f"{k} {v}" for k, v in unscored.items()),
        within_hour_check(f, windows),
    ]
    csv_rows: list[tuple[str, str, str, str]] = []
    for window in windows:
        report_window(out, f, window, window_rows(arrays, window), csv_rows)
    if not args.no_reference:
        for name, pairs in reference_pairs.items():
            pairs = [(a, b) for a, b in pairs if a is not None]
            diff = np.concatenate([np.abs(a - b) for a, b in pairs])
            both = np.isfinite(diff)
            out.append(
                f"\nrepo {name} against the in-script reference: {int(both.sum())} hours "
                f"compared, max |difference| {diff[both].max():.6f} EUR/kWh, mean "
                f"{diff[both].mean():.6f}, {int((diff[both] > 1e-6).sum())} differ by more than "
                f"1e-6; {int((~both).sum())} hours not comparable (a hole in the reference)"
            )
    text = "\n".join(out) + "\n"
    print(text, end="")
    _write_bytes(directory / "report.txt", text.encode())
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(("window", "estimator", "metric", "value"))
    writer.writerows(csv_rows)
    _write_bytes(directory / "metrics.csv", buffer.getvalue().encode())
    print(f"written to {directory}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
