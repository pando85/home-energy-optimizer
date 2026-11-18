#!/usr/bin/env python3
"""Is appliance-level (NILM) knowledge worth building for load forecasting? Read-only probe.

Reproduces the "NILM feasibility" block of section 7 of
``docs/planning/load-forecaster-improvement-plan.md`` (ADR-0002).  From the 60 s power of the main
meter, of its branch ``shellyem_main_channel_2`` (heat pump, washer, dishwasher), of the server
meters and of the heat pump it builds:

1. the **residual-episode inventory**: the house residual ``main1 - main2 - servers`` (and the
   channel-2 part not explained by the heat pump) is cut into episodes above a rolling baseline,
   each classified by size and duration (hob and oven sessions, kettle-like bursts, the washer and
   dishwasher through the Home Connect remaining-time sensors, ...);
2. the **energy accounting** of the residual by class, overall and in the 18-21 h window, and the
   overlap between episodes;
3. the **cycle identification** of the washer and dishwasher since ``--cycles-from``: Home Connect
   start and end, the electrical start in channel 2 (heat pump removed), the programmed lead and
   whether the start was delayed, the Home Connect cycle counters and the washer energy sensor;
4. the **forecast probe**: for forecasts of the next 15 to 120 minutes of the residual energy, a
   least-squares combination of the trailing day profile, persistence and the identity flags
   (cooking cluster, washer, dishwasher), fitted before ``--split`` and scored after it.

Optional sections: ``--inventory`` (first day, coverage and update cadence of the candidate
sensors), ``--recent`` (the episode table over the battery-live days, with and without the battery
AC power added back).

Usage:
    uv run python hack/load_nilm_probe.py --from 2026-09-15 --to 2026-11-03 \\
        --cycles-from 2026-10-15 [--cycles-to 2026-11-18T10:20] [--split 2026-10-10] \\
        [--inventory] [--recent] [--data-dir ~/data/<store>] [--run-id <name>]

``--from`` is inclusive and ``--to`` exclusive (UTC; a date or a date-time).  ``--cycles-to``
defaults to now.  The data are the 60 s ``query_range`` grid of the Prometheus archive
(``BATTERY_OPTIMIZER_PROMETHEUS_URL``).  ``BATTERY_OPTIMIZER_DATA_DIR`` replaces ``--data-dir``.
The tables go to stdout.  With ``--run-id`` the fetched series (reused by a second run with the
same id) and ``episodes.csv`` are written to
``<data_dir>/analysis/v1/load-nilm-probe/<run-id>/``; the repository is never touched.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import warnings
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lf_common as lc  # noqa: E402  (shared plumbing of the load-forecast scripts)

TOOL = "load-nilm-probe"
TZ = ZoneInfo(lc.LOCAL_TZ)
HP_BRANCH_RATIO = 1.28  # channel 2 draws about 1.28 x the heat pump meter (branch overhead)
HP_OVERHEAD = 0.28
COOKING_CORE = {"r2 hob/oven session element"}
COOKING_ALL = COOKING_CORE | {
    "r2 short burst >=1.5kW (kettle/toaster/micro)",
    "r2 mid-power long (600-1500W, cooking/other)",
}

SERIES = {
    "main1": ("hass_sensor_power_w", "sensor.shellyem_main_channel_1_power"),
    "main2": ("hass_sensor_power_w", "sensor.shellyem_main_channel_2_power"),
    "srv_old": ("hass_sensor_power_w", "sensor.shellypmmini_servers_power"),
    "srv_z800": ("hass_sensor_power_w", "sensor.shellypmmini_z800_power"),
    "hp_heat": ("hass_sensor_power_w", "sensor.aquarea_heat_power_consumed"),
    "hp_dhw": ("hass_sensor_power_w", "sensor.aquarea_dhw_power_consumed"),
    "hp_cool": ("hass_sensor_power_w", "sensor.aquarea_cool_power_consumed"),
    "venus_ac": ("hass_sensor_power_w", "sensor.venus_ac_power"),
    "wash_e_today": ("hass_sensor_energy_wh", "sensor.terrace_lavadora_energy_today"),
    "wash_cycles": ("hass_sensor_state", "sensor.lavadora_cycles"),
    "wash_rem": ("hass_sensor_timestamp_seconds", "sensor.lavadora_remaining_time"),
    "wash_delay_end": ("hass_sensor_timestamp_seconds", "sensor.lavadora_delayed_end"),
    "wash_stat_chg": ("hass_state_change_total", "sensor.lavadora_current_status"),
    "dw_rem": ("hass_sensor_timestamp_seconds", "sensor.lavavajillas_remaining_time"),
    "dw_delay_start": ("hass_sensor_timestamp_seconds", "sensor.lavavajillas_delayed_start"),
    "dw_stat_chg": ("hass_state_change_total", "sensor.lavavajillas_current_status"),
}
INVENTORY = (
    ("hass_sensor_power_w", "sensor.shellyem_main_channel_1_power"),
    ("hass_sensor_power_w", "sensor.shellyem_main_channel_2_power"),
    ("hass_sensor_power_w", "sensor.circuit_1_power"),
    ("hass_sensor_power_w", "sensor.shellypmmini_servers_power"),
    ("hass_sensor_power_w", "sensor.shellypmmini_z800_power"),
    ("hass_sensor_power_w", "sensor.aquarea_power_consumed"),
    ("hass_sensor_power_w", "sensor.aquarea_heat_power_consumed"),
    ("hass_sensor_power_w", "sensor.aquarea_dhw_power_consumed"),
    ("hass_sensor_power_w", "sensor.shellyplus2pm_office_a_power"),
    ("hass_sensor_power_w", "sensor.smart_plug_v2_terrace_power"),
    ("hass_sensor_power_w", "sensor.venus_ac_power"),
    ("hass_sensor_energy_wh", "sensor.terrace_lavadora_energy_today"),
    ("hass_sensor_state", "sensor.lavadora_cycles"),
)


def parse_utc(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


warnings.filterwarnings("ignore", category=RuntimeWarning)

# ---------------------------------------------------------------------------
# Minute-series helpers (NaN = unknown), pandas-free
# ---------------------------------------------------------------------------


def _windows(x: np.ndarray, window: int, center: bool) -> np.ndarray:
    left = window // 2 if center else window - 1
    right = window // 2 if center else 0
    padded = np.pad(x.astype(float), (left, right), constant_values=np.nan)
    return np.lib.stride_tricks.sliding_window_view(padded, window)


def rolling(x: np.ndarray, window: int, func, *, center: bool = False, min_periods: int = 1):
    """*func* over the non-NaN values of each window (NaN when fewer than *min_periods*)."""
    out = np.empty(x.size)
    view = _windows(x, window, center)
    for lo in range(0, x.size, 20000):
        chunk = view[lo : lo + 20000]
        out[lo : lo + 20000] = np.where(
            (~np.isnan(chunk)).sum(1) >= min_periods, func(chunk), np.nan
        )
    return out


def rolling_quantile(x, window, q, *, center=True, min_periods=1):
    return rolling(
        x, window, lambda c: np.nanquantile(c, q, axis=1), center=center, min_periods=min_periods
    )


def rolling_median(x, window, *, center=True, min_periods=1):
    return rolling(
        x, window, lambda c: np.nanmedian(c, axis=1), center=center, min_periods=min_periods
    )


def interpolate(x: np.ndarray, limit: int | None = None) -> np.ndarray:
    """Linear interpolation of interior gaps (first *limit* values of each gap when given).

    The tail after the last valid value repeats it (same limit); the head stays unknown.
    """
    out = x.copy()
    valid = np.flatnonzero(~np.isnan(x))
    if valid.size == 0:
        return out
    gap = np.isnan(x)
    gap[: valid[0]] = False
    filled = np.interp(np.arange(x.size), valid, x[valid])
    since_valid = np.arange(x.size) - np.maximum.accumulate(
        np.where(np.isnan(x), -1, np.arange(x.size))
    )
    take = gap if limit is None else gap & (since_valid <= limit)
    out[take] = filled[take]
    return out


def local_hours(epoch: np.ndarray) -> np.ndarray:
    _, hour, _ = lc.local_fields(epoch)
    return hour


class Minutes:
    """The 60 s series of the window on one UTC minute grid."""

    def __init__(self, arrays: dict[str, np.ndarray], t0: int) -> None:
        self.a = arrays
        self.t0 = t0
        self.n = next(iter(arrays.values())).size
        self.epoch = t0 + 60 * np.arange(self.n)

    def sl(self, since: datetime, until: datetime) -> slice:
        lo = max(0, (int(since.timestamp()) - self.t0) // 60)
        hi = min(self.n, (int(until.timestamp()) - self.t0) // 60)
        return slice(lo, hi)


def fetch_minutes(prom: lc.Prometheus, rd: lc.RunDir, start: datetime, stop: datetime) -> Minutes:
    t0 = int(start.timestamp())
    n = (int(stop.timestamp()) - t0) // 60
    out = {}
    for name, (metric, entity) in SERIES.items():
        expr = f'{metric}{{entity="{entity}"}}'

        def one(expr=expr):
            return lc.to_arrays(prom.grid(expr, start, stop, 60))

        got = rd.cached(f"raw_{name}", one)
        print(f"  {name}: {got['ts'].size} samples", file=sys.stderr, flush=True)
        out[name] = lc.on_grid(lc.from_arrays(got), t0, 60, n)
    return Minutes(out, t0)


# ---------------------------------------------------------------------------
# Residual episodes
# ---------------------------------------------------------------------------


def baseline(x: np.ndarray) -> np.ndarray:
    """Rolling 10 % quantile over 91 min, smoothed by a 31 min rolling median."""
    return rolling_median(rolling_quantile(x, 91, 0.10, min_periods=30), 31)


def find_episodes(x: np.ndarray, epoch: np.ndarray, thr: float = 200.0, merge: int = 3):
    """Runs of *x* above its baseline by more than *thr* W, joined across gaps of *merge* minutes.

    Returns (episodes, baseline).  An episode: start/end minute index, duration (min), energy
    above the baseline (Wh), peak and mean-on excess (W), baseline at the start and the number of
    steps larger than 300 W.
    """
    base = baseline(x)
    exc = x - base
    on = np.nan_to_num(exc, nan=-1.0) > thr
    eps = []
    i, n = 0, x.size
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = last = i
        while j < n and (on[j] or j - last <= merge):
            if on[j]:
                last = j
            j += 1
        eps.append((i, last))
        i = last + 1
    rows = []
    for a, b in eps:
        e = np.clip(exc[a : b + 1], 0.0, None)
        pos = e[e > 0]
        rows.append(
            {
                "a": a,
                "b": b,
                "start": int(epoch[a]),
                "end": int(epoch[b]),
                "dur": b - a + 1,
                "wh": float(np.nansum(e) / 60),
                "peak": float(np.nanmax(e)),
                "mean_on": float(np.nanmean(pos)) if pos.size else float("nan"),
                "base": float(base[a]),
                "nsteps": int((np.abs(np.diff(e)) > 300).sum()),
            }
        )
    return rows, base


def presence(x: np.ndarray) -> np.ndarray:
    """Whether a sensor reported within +-2 min."""
    return (
        rolling(np.where(np.isnan(x), 0.0, 1.0), 5, lambda c: np.nanmax(c, axis=1), center=True) > 0
    )


def classify(r: dict[str, Any], wash_on: np.ndarray, dw_on: np.ndarray) -> str:
    if r["src"] == "r2":
        if r["peak"] >= 1500 and r["dur"] >= 8:
            return "r2 hob/oven session element"
        if r["peak"] >= 1500:
            return "r2 short burst >=1.5kW (kettle/toaster/micro)"
        if r["mean_on"] >= 600 and r["dur"] >= 12:
            return "r2 mid-power long (600-1500W, cooking/other)"
        if r["mean_on"] >= 600:
            return "r2 mid burst (microwave-like)"
        if r["mean_on"] < 700 and r["dur"] >= 30 and r["nsteps"] <= 2:
            return "r2 low periodic ~35min (cycling load)"
        return "r2 small <600W"
    sl = slice(r["a"], r["b"] + 1)
    washer, dish = wash_on[sl].mean(), dw_on[sl].mean()
    if washer >= 0.5 and washer >= dish:
        return "b washer (HA-confirmed)"
    if dish >= 0.5:
        return "b dishwasher (HA-confirmed)"
    if r["mean_on"] >= 1000:
        return "b high-power, no HA state (HA-unconfirmed)"
    return "b small"


class Window:
    """Residual series, episodes and accounting of ``[since, until)``."""

    def __init__(self, m: Minutes, since: datetime, until: datetime) -> None:
        s = m.sl(since, until)
        self.epoch = m.epoch[s]
        g = {k: v[s] for k, v in m.a.items()}
        self.g = g
        self.hp = g["hp_heat"] + g["hp_dhw"] + g["hp_cool"]  # NaN unless all three are known
        self.r2 = g["main1"] - g["main2"] - g["srv_old"]
        self.b = g["main2"] - HP_BRANCH_RATIO * self.hp
        self.R = g["main1"] - g["srv_old"] - self.hp
        hp0 = np.nan_to_num(self.hp)
        trailing = rolling(hp0, 8, lambda c: np.nanmax(c, axis=1))
        ahead = np.concatenate([hp0[1:], [0.0]])
        self.hpm = (trailing > 0) | (ahead > 0)
        self.e2, self.b2 = find_episodes(self.r2, self.epoch)
        self.eb, self.bb = find_episodes(np.where(self.hpm, np.nan, self.b), self.epoch)
        for r in self.e2:
            r["src"] = "r2"
        for r in self.eb:
            r["src"] = "b"
        self.episodes = self.e2 + self.eb
        self.wash_on = presence(g["wash_rem"])
        self.dw_on = presence(g["dw_rem"])
        for r in self.episodes:
            r["cls"] = classify(r, self.wash_on, self.dw_on)
        hours = local_hours(self.epoch)
        self.eve = (hours >= 18) & (hours < 21)
        self.days = self.epoch.size / 1440
        self.cls = np.full(self.epoch.size, "", dtype=object)
        self.e = np.zeros(self.epoch.size)


def accounting(w: Window) -> dict[str, Any]:
    exc2 = np.clip(w.r2 - w.b2, 0, None)
    excb = np.clip(w.b - w.bb, 0, None)
    for r in w.episodes:
        sl = slice(r["a"], r["b"] + 1)
        src = exc2 if r["src"] == "r2" else excb
        w.e[sl] += np.nan_to_num(src[sl])
        w.cls[sl] = r["cls"]
    resid = np.nan_to_num(w.R)
    tot = resid.sum() / 60 / 1000
    tote = resid[w.eve].sum() / 60 / 1000
    print(
        f"days {w.days:.1f}  R kWh {tot:.1f}  R kWh 18-21 {tote:.1f}  "
        f"share of R in 18-21: {tote / tot:.3f}"
    )
    starts_h = {id(r): local_hours(np.array([r["start"]]))[0] for r in w.episodes}
    rows = []
    for c in sorted({r["cls"] for r in w.episodes}):
        g = [r for r in w.episodes if r["cls"] == c]
        mask = w.cls == c
        wh = w.e[mask].sum() / 60 / 1000
        whe = w.e[mask & w.eve].sum() / 60 / 1000
        hours = Counter(starts_h[id(r)] for r in g)
        rows.append(
            {
                "cls": c,
                "n": len(g),
                "per_day": round(len(g) / w.days, 2),
                "med_mean_W": round(float(np.median([r["mean_on"] for r in g]))),
                "p90_peak": round(float(np.quantile([r["peak"] for r in g], 0.9))),
                "med_dur_min": float(np.median([r["dur"] for r in g])),
                "med_Wh": round(float(np.median([r["wh"] for r in g]))),
                "share_R": round(100 * wh / tot, 1),
                "share_R_1821": round(100 * whe / tote, 1),
                "n_1821": sum(18 <= starts_h[id(r)] <= 20 for r in g),
                "modal_hours": ",".join(str(h) for h, _ in hours.most_common(3)),
            }
        )
    rows.sort(key=lambda r: -r["share_R"])
    print_rows(rows)
    base2 = np.minimum(np.clip(np.nan_to_num(w.b2), 0, None), np.nan_to_num(w.r2))
    basebb = np.clip(np.nan_to_num(interpolate(w.bb)), 0, None)
    parts = {
        "r2 baseline (rolling-10%)": base2,
        "b baseline (rolling-10%, ch2 not HP)": basebb,
        "0.28*HP branch overhead (ch2/HP ratio)": HP_OVERHEAD * np.nan_to_num(w.hp),
        "episodes (all clusters above)": w.e,
    }
    acc = {k: (v.sum() / 60 / 1000, v[w.eve].sum() / 60 / 1000) for k, v in parts.items()}
    acc["remainder (sub-threshold fluctuation, HP-concurrent appliance, calib err)"] = (
        tot - sum(v[0] for v in acc.values()),
        tote - sum(v[1] for v in acc.values()),
    )
    for k, v in acc.items():
        print(
            f"{k:70s} {v[0]:7.1f} kWh {100 * v[0] / tot:5.1f}%   "
            f"18-21h {v[1]:6.1f} kWh {100 * v[1] / tote:5.1f}%"
        )
    n_r2 = sum(r["src"] == "r2" for r in w.episodes)
    print(
        f"events/day {len(w.episodes) / w.days:.1f}  r2 {n_r2 / w.days:.1f}  "
        f"b {(len(w.episodes) - n_r2) / w.days:.1f}"
    )
    count = np.zeros(w.epoch.size, int)
    a2 = np.zeros(w.epoch.size, bool)
    ab = a2.copy()
    for r in w.episodes:
        sl = slice(r["a"], r["b"] + 1)
        count[sl] += 1
        (a2 if r["src"] == "r2" else ab)[sl] = True
    active = count > 0
    print(
        f"active minutes frac {active.mean():.3f}; of active minutes, share with >=2 concurrent "
        f"episodes {(count >= 2).sum() / active.sum():.3f}"
    )
    print(f"r2&b co-active share of r2-active minutes {(a2 & ab).sum() / a2.sum():.3f}")
    print(f"minutes frac HP-masked in b: {w.hpm.mean():.3f}; evening {w.hpm[w.eve].mean():.3f}")
    ev = resid[w.eve]
    for label, classes in (
        ("hob/oven session elements", COOKING_CORE),
        ("hob/oven + kettle-like bursts + mid-power long", COOKING_ALL),
    ):
        cook_e = np.zeros(w.epoch.size)
        for r in w.episodes:
            if r["cls"] in classes:
                sl = slice(r["a"], r["b"] + 1)
                cook_e[sl] += np.nan_to_num(exc2[sl])
        share = 1 - np.var(ev - cook_e[w.eve]) / np.var(ev)
        print(
            f"cooking ({label}) explain {100 * share:.0f}% of the 18-21 h residual "
            "variance (per minute)"
        )
    return {"total_kwh": tot, "evening_kwh": tote, "classes": rows}


def print_rows(rows: list[dict[str, Any]]) -> None:
    head = list(rows[0])
    widths = [max(len(str(x)) for x in [h] + [r[h] for r in rows]) for h in head]
    fmt = "  ".join(f"{{:>{x}}}" for x in widths)
    print(fmt.format(*head))
    for r in rows:
        print(fmt.format(*(str(r[h]) for h in head)))


# ---------------------------------------------------------------------------
# Washer and dishwasher cycles
# ---------------------------------------------------------------------------


def runs(present: np.ndarray, epoch: np.ndarray, gap: int = 45) -> list[tuple[int, int]]:
    """Index ranges of consecutive reports separated by at most *gap* minutes."""
    idx = np.flatnonzero(present)
    if idx.size == 0:
        return []
    cuts = np.flatnonzero(np.diff(idx) > gap)
    starts = np.concatenate([[idx[0]], idx[cuts + 1]])
    ends = np.concatenate([idx[cuts], [idx[-1]]])
    return list(zip(starts.tolist(), ends.tolist(), strict=True))


def cycles(m: Minutes, since: datetime, until: datetime) -> list[dict[str, Any]]:
    s = m.sl(since - timedelta(days=1), until)
    epoch = m.epoch[s]
    g = {k: v[s] for k, v in m.a.items()}
    hp = np.nan_to_num(g["hp_heat"] + g["hp_dhw"] + g["hp_cool"])
    b = g["main2"] - HP_BRANCH_RATIO * hp
    trailing = rolling(hp, 8, lambda c: np.nanmax(c, axis=1))
    hpm = (trailing > 0) | (np.concatenate([hp[1:], [0.0]]) > 0)
    base = rolling_quantile(np.where(hpm, np.nan, b), 181, 0.10, min_periods=30)
    base = interpolate(base)
    first = np.flatnonzero(~np.isnan(base))
    if first.size:
        base[: first[0]] = base[first[0]]
    bx = np.clip(b - base, 0, None)
    t0 = since.timestamp()
    rows = []

    def hhmm(ts: float, fmt: str = "%H:%M") -> str:
        return datetime.fromtimestamp(ts, TZ).strftime(fmt)

    for app, rem, delay in (
        ("dishwasher", "dw_rem", "dw_delay_start"),
        ("washer", "wash_rem", "wash_delay_end"),
    ):
        delayed = ~np.isnan(g[delay])
        for a, z in runs(~np.isnan(g[rem]), epoch):
            if epoch[z] < t0:
                continue
            sl = slice(a, z + 1)
            active = np.flatnonzero(bx[sl] > 250)
            lo = max(a - 5, 0)
            row = {
                "app": app,
                "seen": hhmm(epoch[a], "%m-%d %H:%M"),
                "ha_end": hhmm(epoch[z]),
                "delayed": bool(delayed[lo : z + 1].any()),
                "kWh_ch2_excess": round(float(np.nansum(bx[sl]) / 60 / 1000), 2),
                "hp_masked_frac": round(float(hpm[sl].mean()), 2),
                "lead_h": None,
                "elec_start": None,
                "elec_end": None,
                "dur_h": None,
                "start_hour": None,
            }
            if active.size:
                f_, l_ = a + active[0], a + active[-1]
                row.update(
                    lead_h=round((epoch[f_] - epoch[a]) / 3600, 1),
                    elec_start=hhmm(epoch[f_]),
                    elec_end=hhmm(epoch[l_]),
                    dur_h=round((epoch[l_] - epoch[f_]) / 3600, 2),
                    start_hour=datetime.fromtimestamp(epoch[f_], TZ).hour,
                )
            rows.append(row)
    return rows


def cycles_report(m: Minutes, since: datetime, until: datetime) -> None:
    rows = cycles(m, since, until)
    cols = [
        "app",
        "seen",
        "ha_end",
        "lead_h",
        "delayed",
        "elec_start",
        "elec_end",
        "dur_h",
        "kWh_ch2_excess",
        "hp_masked_frac",
    ]
    for app in ("dishwasher", "washer"):
        sub = [{c: r[c] for c in cols} for r in rows if r["app"] == app]
        if sub:
            print_rows(sub)
    for app in ("dishwasher", "washer"):
        mine = [r for r in rows if r["app"] == app]
        delayed = [r for r in mine if r["delayed"]]
        evening = [r for r in delayed if r["start_hour"] is not None and 18 <= r["start_hour"] < 21]
        real = [r for r in evening if (r["lead_h"] or 0) >= 0.5]
        print(
            f"{app}: {len(mine)} cycles since {since:%Y-%m-%d}; flagged delayed {len(delayed)}; "
            f"of those electrical start 18-21 h local {len(evening)} "
            f"(with a programmed lead of 0.5 h or more: {len(real)})"
        )
    s = m.sl(since, until)
    t = m.epoch[s]
    for key, label in (
        ("wash_cycles", "washer cycles counter"),
        ("dw_stat_chg", "dishwasher status transitions"),
        ("wash_stat_chg", "washer status transitions"),
    ):
        v = m.a[key][s]
        ok = ~np.isnan(v)
        if ok.any():
            print(
                f"{label}: from {v[ok][0]:.0f} to {v[ok][-1]:.0f}, delta {v[ok][-1] - v[ok][0]:.0f}"
            )
    e = m.a["wash_e_today"][s]
    days = np.array([datetime.fromtimestamp(x, TZ).date() for x in t])
    daily = {
        str(d): float(np.nanmax(e[days == d]))
        for d in sorted(set(days))
        if (~np.isnan(e[days == d])).any() and np.nanmax(e[days == d]) > 0
    }
    print("washer energy sensor (daily max Wh):", {k: round(v) for k, v in daily.items()})
    print(f"sum kWh {sum(daily.values()) / 1000:.3f}")


# ---------------------------------------------------------------------------
# Forecast probe
# ---------------------------------------------------------------------------

HORIZONS = (15, 30, 60, 120)
CONFIGS = {
    "profile only": ["prof{hz}"],
    "persistence only": ["pers{hz}"],
    "profile+persistence (no ID)": ["prof{hz}", "pers{hz}"],
    "+ cluster ID cook/burst": ["prof{hz}", "pers{hz}", "cook", "burst"],
    "+ HA wash/dw flags": ["prof{hz}", "pers{hz}", "wash", "dw"],
    "+ all ID": ["prof{hz}", "pers{hz}", "cook", "burst", "wash", "dw"],
}


def probe_frame(w: Window) -> dict[str, np.ndarray]:
    """Per 15-minute start: next-h energy, persistence, trailing day profile and the flags."""
    resid = interpolate(w.R, limit=5)
    exc2 = np.nan_to_num(np.clip(w.r2 - w.b2, 0, None))
    big = (exc2 > 1000).astype(float)
    m30 = rolling(big, 30, lambda c: np.nansum(c, axis=1))
    m10 = rolling(big, 10, lambda c: np.nansum(c, axis=1))
    cook = ((m30 >= 8) & (m10 >= 1)).astype(float)
    burst = ((m30 >= 1) & (m30 < 8) & (m10 >= 1)).astype(float)
    cum = np.cumsum(np.where(np.isnan(resid), np.nanmedian(resid), resid))
    # local day x local quarter-hour mean of the residual, expanding over previous days only
    ordinal, hour, minute = lc.local_fields(w.epoch)
    days = np.unique(ordinal)
    di = np.searchsorted(days, ordinal)
    qh = hour * 4 + minute // 15
    total = np.zeros((days.size, 96))
    count = np.zeros((days.size, 96))
    ok = ~np.isnan(resid)
    np.add.at(total, (di[ok], qh[ok]), resid[ok])
    np.add.at(count, (di[ok], qh[ok]), 1)
    with np.errstate(all="ignore"):
        per_day = np.where(count > 0, total / count, np.nan)
    seen = np.cumsum(~np.isnan(per_day), axis=0)
    csum = np.cumsum(np.nan_to_num(per_day), axis=0)
    with np.errstate(all="ignore"):
        expanding = np.where(seen >= 3, csum / seen, np.nan)
    prof = np.vstack([np.full((1, 96), np.nan), expanding[:-1]])
    # 15-minute starts
    first = int(np.ceil(w.epoch[0] / 900) * 900)
    starts = np.arange(first, int(w.epoch[-1]) + 1, 900)
    mi = (starts - w.epoch[0]) // 60
    s_ord, s_hour, s_min = lc.local_fields(starts)
    s_day = np.searchsorted(days, s_ord)
    s_qh = s_hour * 4 + s_min // 15
    out: dict[str, np.ndarray] = {
        "lh": s_hour + s_min / 60.0,
        "day": s_ord.astype(float),
        "cook": cook[mi],
        "burst": burst[mi],
        "wash": w.wash_on[mi].astype(float),
        "dw": w.dw_on[mi].astype(float),
    }
    rm5 = rolling(resid, 5, lambda c: np.nanmean(c, axis=1))
    out["Pnow"] = rm5[mi]
    for hz in HORIZONS:
        y = np.full(starts.size, np.nan)
        pers = np.full(starts.size, np.nan)
        pro = np.full(starts.size, np.nan)
        for i, m_ in enumerate(mi):
            end = m_ + hz
            if end >= w.epoch.size:
                continue
            y[i] = (cum[end] - cum[m_]) / 60 / 1000
            lo = max(m_ - 14, 0)
            pers[i] = np.nanmean(resid[lo : m_ + 1]) * hz / 60 / 1000
            vals = []
            for k in range(hz // 15):
                q = s_qh[i] + k
                d = s_day[i]
                if q >= 96:
                    d, q = d + 1, q - 96
                vals.append(prof[d, q] if d < days.size else np.nan)
            vals = np.array(vals)
            pro[i] = np.nansum(vals) * 0.25 / 1000 if (~np.isnan(vals)).any() else np.nan
        out[f"y{hz}"], out[f"pers{hz}"], out[f"prof{hz}"] = y, pers, pro
    return out


def fit_eval(frame, mask, hz, feats, split_day):
    ok = (
        mask
        & ~np.isnan(frame[f"y{hz}"])
        & ~np.isnan(frame[f"pers{hz}"])
        & ~np.isnan(frame[f"prof{hz}"])
    )
    tr, te = ok & (frame["day"] < split_day), ok & (frame["day"] >= split_day)

    def design(sel):
        cols = [frame[f.format(hz=hz)][sel] for f in feats]
        return np.column_stack([*cols, np.ones(sel.sum())])

    beta = np.linalg.lstsq(design(tr), frame[f"y{hz}"][tr], rcond=None)[0]
    err = design(te) @ beta - frame[f"y{hz}"][te]
    return 1000 * np.abs(err).mean(), 1000 * np.sqrt((err**2).mean())


def probe_report(w: Window, split: datetime) -> None:
    frame = probe_frame(w)
    split_day = float(split.astimezone(TZ).date().toordinal())
    eve = (frame["lh"] >= 17) & (frame["lh"] < 20.75)
    for name, mask in (
        ("EVENING starts 17:00-20:45", eve),
        ("ALL starts", np.ones(eve.size, bool)),
    ):
        print(
            f"== {name}  (test MAE and RMSE of the next-h residual energy, Wh; "
            f"fit before {split:%Y-%m-%d})"
        )
        for k, label in ((0, "MAE"), (1, "RMSE")):
            print(label.ljust(30) + "".join(f"{hz:>9}" for hz in HORIZONS))
            for cfg, feats in CONFIGS.items():
                vals = [fit_eval(frame, mask, hz, feats, split_day)[k] for hz in HORIZONS]
                print(cfg.ljust(30) + "".join(f"{v:>9.1f}" for v in vals))
    print("== conditional mean of next-h energy (Wh), evening starts: all, and given each flag")
    for hz in HORIZONS:
        sel = eve & ~np.isnan(frame["y60"]) & ~np.isnan(frame[f"y{hz}"])
        out = {"all": (round(1000 * np.nanmean(frame[f"y{hz}"][sel])), int(sel.sum()))}
        for flag in ("cook", "burst", "wash", "dw"):
            s2 = sel & (frame[flag] == 1)
            out[flag] = (
                round(1000 * np.nanmean(frame[f"y{hz}"][s2])) if s2.any() else None,
                int(s2.sum()),
            )
        print(hz, out)
    print("== next 60 / 120 min energy (Wh) by present power bin and cooking flag")
    edges = np.array([0, 300, 600, 1000, 2000, 9000])
    sel = ~np.isnan(frame["y60"]) & ~np.isnan(frame["Pnow"])
    bins = np.searchsorted(edges, frame["Pnow"], side="left") - 1  # right-closed (a, b]
    print("bin".ljust(14) + "cook".rjust(6) + "n".rjust(7) + "y60_Wh".rjust(9) + "y120_Wh".rjust(9))
    for b in range(len(edges) - 1):
        for flag in (0.0, 1.0):
            s2 = sel & (bins == b) & (frame["cook"] == flag)
            if s2.any():
                y120 = np.nanmean(frame["y120"][s2])
                print(
                    f"({edges[b]}, {edges[b + 1]}]".ljust(14) + f"{flag:>6.1f}{int(s2.sum()):>7}"
                    f"{1000 * np.nanmean(frame['y60'][s2]):>9.0f}{1000 * y120:>9.0f}"
                )


# ---------------------------------------------------------------------------
# Optional sections
# ---------------------------------------------------------------------------


def recent_report(m: Minutes, since: datetime, until: datetime) -> None:
    s = m.sl(since, until)
    g = {k: v[s] for k, v in m.a.items()}
    epoch = m.epoch[s]
    ok = ~np.isnan(g["main1"]) & ~np.isnan(g["venus_ac"])
    corr = float(np.corrcoef(g["main1"][ok], g["venus_ac"][ok])[0, 1])
    print(
        f"corr(main1, venus_ac) {corr:.2f}  venus_ac mean {np.nanmean(g['venus_ac']):.0f}  "
        f"z800 {np.nanmean(g['srv_z800']):.0f}  main2 {np.nanmean(g['main2']):.0f}  "
        f"main1 {np.nanmean(g['main1']):.0f}"
    )
    days = (until - since).total_seconds() / 86400
    for name, r in (
        ("net (no battery fix)", g["main1"] - g["main2"] - g["srv_z800"]),
        ("gross = main1 + venus_ac", g["main1"] + g["venus_ac"] - g["main2"] - g["srv_z800"]),
    ):
        eps, _ = find_episodes(r, epoch)
        print(
            f"{name}: median base {np.nanmedian(r):.0f}  p5 {np.nanpercentile(r, 5):.0f}  "
            f"episodes/day {len(eps) / days:.1f}"
        )
        for label, pick in (
            ("hob/oven", lambda e: e["peak"] >= 1500 and e["dur"] >= 8),
            ("burst>=1.5kW", lambda e: e["peak"] >= 1500 and e["dur"] < 8),
            ("other", lambda e: e["peak"] < 1500),
        ):
            sub = [e for e in eps if pick(e)]
            print(
                f"  {label:<14} n {len(sub):>3}  per_day {len(sub) / days:.1f}  "
                f"Wh {sum(e['wh'] for e in sub):.0f}"
            )
        print(
            f"  episode kWh {sum(e['wh'] for e in eps) / 1000:.1f} "
            f"of r kWh {np.nansum(r) / 60 / 1000:.1f}"
        )


def inventory(prom: lc.Prometheus, days: int) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    print("== Sensor inventory: first day with data, days with data, samples/day, update cadence")
    for metric, entity in INVENTORY:
        sel = f'{metric}{{entity="{entity}"}}'
        ts, v = prom.grid(
            f"sum(count_over_time({sel}[1d]))", now - timedelta(days=days), now, 86400
        )
        if ts.size == 0:
            print(f"{entity}: no data")
            continue
        cad = []
        for ago in (2, 60):
            a = now - timedelta(days=ago)
            lt, lv = prom.grid(
                f'hass_last_updated_time_seconds{{entity="{entity}"}}',
                a,
                a + timedelta(hours=6),
                60,
            )
            u = np.unique(np.round(lv))
            cad.append(float(np.median(np.diff(u))) if u.size > 1 else float("nan"))
        print(
            f"{entity}: first {lc.utc_iso(ts[0])[:10]}  days {ts.size}  median samples/day "
            f"{np.median(v):.0f}  update interval s (2 d ago / 60 d ago) "
            f"{cad[0]:.0f} / {cad[1]:.0f}"
        )


def episodes_csv(w: Window) -> bytes:
    buffer = io.StringIO()
    fields = ["src", "cls", "start", "end", "dur", "wh", "peak", "mean_on", "base", "nsteps"]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(w.episodes)
    return buffer.getvalue().encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="since", type=parse_utc, required=True)
    parser.add_argument("--to", dest="until", type=parse_utc, required=True)
    parser.add_argument("--cycles-from", type=parse_utc, default=None)
    parser.add_argument("--cycles-to", type=parse_utc, default=None, help="default: now")
    parser.add_argument("--split", type=parse_utc, default=parse_utc("2026-10-10"))
    parser.add_argument("--recent", action="store_true")
    parser.add_argument("--recent-from", type=parse_utc, default=parse_utc("2026-11-04T12:00"))
    parser.add_argument("--recent-to", type=parse_utc, default=parse_utc("2026-11-18"))
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--inventory-days", type=int, default=400)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)
    if args.until <= args.since:
        parser.error("--to must be after --from")
    cycles_to = args.cycles_to or datetime.now(UTC).replace(second=0, microsecond=0)
    starts = [args.since]
    stops = [args.until]
    if args.cycles_from:
        starts.append(args.cycles_from - timedelta(days=1))
        stops.append(cycles_to)
    if args.recent:
        starts.append(args.recent_from)
        stops.append(args.recent_to)
    start = min(starts).replace(hour=0, minute=0) - timedelta(days=1)
    stop = max(stops)
    rd = lc.RunDir(TOOL, args.run_id, args.data_dir)
    prom = lc.Prometheus()
    try:
        print(f"fetching {start:%Y-%m-%d %H:%M} .. {stop:%Y-%m-%d %H:%M} UTC", file=sys.stderr)
        m = fetch_minutes(prom, rd, start, stop)
        if args.inventory:
            inventory(prom, args.inventory_days)
    finally:
        prom.close()
    w = Window(m, args.since, args.until)
    print(f"\n== Residual episodes {args.since:%Y-%m-%d} .. {args.until:%Y-%m-%d} UTC")
    print(
        f"{len(w.e2)} residual episodes (r2 = main1 - main2 - servers), {len(w.eb)} channel-2 "
        f"episodes (b = main2 - {HP_BRANCH_RATIO} x heat pump, heat pump minutes masked)"
    )
    print("\n== Energy accounting of the residual R = main1 - servers - heat pump")
    accounting(w)
    if args.cycles_from:
        print(f"\n== Washer and dishwasher cycles since {args.cycles_from:%Y-%m-%d} (local times)")
        cycles_report(m, args.cycles_from, cycles_to)
    print("\n== Forecast probe: identity flags vs persistence")
    probe_report(w, args.split)
    if args.recent:
        print(
            f"\n== Episodes over {args.recent_from:%Y-%m-%d %H:%M} .. "
            f"{args.recent_to:%Y-%m-%d %H:%M}"
        )
        recent_report(m, args.recent_from, args.recent_to)
    rd.write("episodes.csv", episodes_csv(w))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
