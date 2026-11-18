#!/usr/bin/env python3
"""Price-information replay of the live battery stack: what is tomorrow's price worth at 14:00?

Read-only analysis of the battery planner's terminal-value gap
(``docs/planning/price-outlook-improvement-plan.md`` sections 4 and 5).  A rolling LP replays the
live stack: every 15 minutes it plans over a price vector that depends on the *information mode*,
and the executed battery action follows the real load by the value-based load-following rule of
ADR-0007, billed at the real PVPC price.  The modes differ only in what the planner may see of
tomorrow:

* ``published_only``: tomorrow from 20:30 local (the service today).
* ``exact``: tomorrow's real prices from 14:00 (the ceiling; ``always_36h`` is the upper bound).
* ``omie_period``: from 13:30, the repo's ``omie_period`` estimate (OMIE D+1), real from 20:30.
* ``persistence``: from 14:00, today's real prices at the same clock time, real from 20:30.

The value of the energy left at the end of the plan is the *terminal value* (rule):
``const:<eur_per_kwh>``, ``trailing_mean`` (the service rule: mean PVPC of the 28 local days
before today) or ``refill`` (strategy B of plan section 5: the cheapest overnight (00:00 to 08:00
local) PVPC valle slot of the most recently published day, plus wear, divided by the charge
efficiency, capped by the trailing mean; ``refill_p3`` takes every P3 slot of that day, the midday
trough of weekends included).  Every variant is *marked* at ``MARK_TV``, so nets are comparable
across rules.

Subcommands (``--from`` inclusive, ``--to`` exclusive; a date means local midnight, an ISO instant
with an offset is taken as is, so ``--from 2026-11-04T00:00:00+00:00`` starts at a UTC day):

* ``extract``: read-only Prometheus pull of the inputs (realised PVPC, gross house load, and with
  ``--minute`` the minute load of the live window) into the private data dir.  Gross load is the
  grid power (15-minute mean, import only) for a slot that ends at or before
  ``battery.commissioned_at`` (``config/service.yaml``) and ``grid - charge + discharge`` (the
  battery AC energy counters) after it.  OMIE comes from the repo's raw store
  (``omie_prices.load_omie_es_prices``); ``--fetch-omie`` first fetches the missing days with the
  repo's own ``extract_omie`` (public files, idempotent).
* ``report``: the summary tables of plan section 4: net per mode, gap against ``published_only``,
  percent of the saving, share of the ``exact`` gap captured, weekly-block bootstrap interval,
  hour-bucket and mechanism breakdown, terminal-value sensitivity (``--tv-sensitivity``) and the
  service-like trailing-mean terminal value.
* ``reanalysis``: the pre-registered re-analysis of plan section 5: strategy A (constants
  ``--a-constants``), B (``refill``, and ``refill_p3`` for the literal reading) and C
  (``persistence`` and ``omie_period`` horizon extension), all against the live trailing mean, with
  the adoption rule check (interval above zero, no weekday worse) and C against B.

Usage::

    uv run python hack/price_info_replay.py report --from 2026-08-01 --to 2026-11-17 \\
        --fetch-omie [--run-id aug-nov]
    uv run python hack/price_info_replay.py report --minute --from 2026-11-04T00:00:00+00:00 \\
        --to 2026-11-17
    uv run python hack/price_info_replay.py reanalysis --from <day> --to <day> [--workers 16]

Environment: ``BATTERY_OPTIMIZER_PROMETHEUS_URL`` (extract only), ``BATTERY_OPTIMIZER_DATA_DIR``
(or ``--data-dir``).  Everything is written under
``<data_dir>/analysis/v1/price-info-replay/<run-id>/`` (``inputs.npz``, ``report.txt``) and printed;
the repository is never touched.  The figures are private: keep amounts in the data dir.

Modelling notes: the planner is an LP approximation (no solver time limit, no thermal or dwell
terms), loads are planned with the causal LF-0 forecast (trailing 28 local-day mean of the same
clock slot, minimum 14 days) or, with ``--load perfect``, with the real load.  A local day on
which the PVPC sensor read 0 all day is a data hole: its prices are replaced by the repo's
``omie_period`` estimate for planning and the hole day and the day before are left out of every
statistic.

The live window (``--minute``) runs with the discharge margin of the deployment (the base value of
``config/service.yaml`` before ``MARGIN_CHANGE_UTC``, ``DEPLOYED_MARGIN_W`` after it) and the
measured minute load.  The 2026-11-18 reference run of that window used the service's own recorded
load and forecast slots; this script derives both from the counters and the causal LF-0 instead, so
its figures agree with that reference to within a few percent of the gap, not to the cent.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# One BLAS thread per worker process: the replay parallelises over (mode, rule) jobs.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
from scipy.optimize import linprog  # noqa: E402
from scipy.sparse import diags, hstack, identity  # noqa: E402

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "src"))

from battery_optimizer import extract_prom  # noqa: E402
from battery_optimizer.battery_telemetry import load_battery_telemetry  # noqa: E402
from battery_optimizer.benchmark import resolve_battery_assumptions  # noqa: E402
from battery_optimizer.config import load_config, resolve_config_path  # noqa: E402
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
H = 0.25  # hours per slot
SLOTS_PER_DAY = 96

# ---------------------------------------------------------------------------
# Replay constants.  The battery, the import limit, the wear cost, the load-following ceiling and
# tolerance are read from config/tariff.yaml and config/service.yaml (``load_settings``); what the
# repo does not expose is named here and was fixed by the 2026-11-18 replays.
# ---------------------------------------------------------------------------

INITIAL_SOC_FRAC = 0.40  # SOC at the start of the replay
DEFAULT_TV = 0.21  # EUR/DC-kWh, the constant terminal value of the 2026-11-18 replays
MARK_TV = 0.21  # EUR/DC-kWh at which the end inventory is marked in every reported net
# Days before the window that the forecast, the fit and the trailing mean need (28 each) plus the
# 28 a fit needs to patch a data hole inside that history.
HISTORY_DAYS = 57
LF_FLOOR_HYST_FRAC = 0.02  # load-following releases this SOC fraction above the physical floor
#: The deployed discharge margin since the service override of this instant; before it the base
#: value of config/service.yaml applied.  Used by the live window (``--minute``) only.
MARGIN_CHANGE_UTC = datetime(2026, 11, 12, 21, 15, tzinfo=UTC)
DEPLOYED_MARGIN_W = 50.0  # discharge margin of the 28-day replays and of the live window after it
LF0_DAYS = 28  # LF-0: mean of the same clock slot over this many local days
LF0_MIN_DAYS = 14
HOLE_EXCLUDE_DAYS = 1  # decision days before a data hole that are left out as well

#: Planning start (minutes after local midnight) from which tomorrow's prices enter, per mode.
START_T = {
    "published_only": 20 * 60 + 30,
    "exact": 14 * 60,
    "always_36h": 0,
    "omie_period": 13 * 60 + 30,
    "persistence": 14 * 60,
}
PUB_T = 20 * 60 + 30  # local minute from which the real next-day prices are public
MODES = ("published_only", "exact", "always_36h", "omie_period", "persistence")
ESTIMATED_MODES = ("omie_period", "persistence")
TV_SENSITIVITY_MODES = ("published_only", "exact", "omie_period", "persistence")
BUCKETS = (
    ("00-06", 0, 360),
    ("06-08", 360, 480),
    ("08-14", 480, 840),
    ("14-18", 840, 1080),
    ("18-22", 1080, 1320),
    ("22-24", 1320, 1441),
)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
REFILL_RULES = ("refill", "refill_p3")
MIN_BLOCKS_FOR_CI = 8  # fewer weekly blocks than this: no interval is printed
BOOTSTRAP_DRAWS = 10000
BOOTSTRAP_SEED = 7
# Prometheus names the service itself exports (not Home Assistant entities).
OBSERVED_BATTERY_POWER_METRIC = "battery_optimizer_actuator_observed_battery_power_watts"


@dataclass(frozen=True)
class Settings:
    """Battery and control constants taken from the repo's configuration."""

    cap: float
    pmax: float
    eta: float
    smin: float
    smax: float
    limit: float
    s0: float
    wear: float
    lf_ceil_w: float
    lf_tol: float
    lf_floor_hyst: float
    max_horizon: int
    margin_base_w: float


def load_settings() -> Settings:
    """Battery spec and control constants of ``config/tariff.yaml`` and ``config/service.yaml``."""
    tariff = load_config(resolve_config_path(None))
    assumptions = resolve_battery_assumptions(tariff, initial_soc_frac=INITIAL_SOC_FRAC)
    spec = assumptions.spec_kwargs()
    if abs(spec["charge_efficiency"] - spec["discharge_efficiency"]) > 1e-12:
        raise SystemExit("the replay LP assumes equal charge and discharge efficiency")
    service = load_service_config(None)
    cap = float(spec["capacity_kwh"])
    return Settings(
        cap=cap,
        pmax=float(spec["power_kw"]),
        eta=float(spec["charge_efficiency"]),
        smin=float(spec["soc_min_frac"]) * cap,
        smax=float(spec["soc_max_frac"]) * cap,
        limit=float(assumptions.import_limit_kw),
        s0=INITIAL_SOC_FRAC * cap,
        wear=float(service.planning.throughput_cost_eur_per_kwh),
        lf_ceil_w=float(service.actuation.max_discharge_cap_w),
        lf_tol=float(service.actuation.load_following.storage_value_margin_eur_per_kwh),
        lf_floor_hyst=LF_FLOOR_HYST_FRAC * cap,
        max_horizon=int(service.planning.max_horizon_slots),
        margin_base_w=float(service.actuation.discharge_margin_w),
    )


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def local_midnight(day: date) -> datetime:
    """UTC instant of local midnight of *day*."""
    return datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(UTC)


def parse_instant(value: str) -> datetime:
    """A date (local midnight) or an ISO-8601 instant with an offset, as an aware UTC datetime."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return local_midnight(date.fromisoformat(value))
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r}: give a date or an instant with an offset")
    return parsed.astimezone(UTC)


def _slot_aligned(moment: datetime) -> bool:
    return int(moment.timestamp()) % SLOT_S == 0


def ordinal_week(ordinal: int) -> str:
    year, week, _ = date.fromordinal(int(ordinal)).isocalendar()
    return f"{year}-W{week:02d}"


# ---------------------------------------------------------------------------
# Prometheus (read-only range queries on a step grid)
# ---------------------------------------------------------------------------


def fetch_grid(
    client: httpx.Client,
    base_url: str,
    query: str,
    first: int,
    step: int,
    n: int,
    per_chunk: int,
) -> np.ndarray:
    """One ``query_range`` series on the grid ``first + step * j`` (``NaN`` where nothing).

    The package's own extraction avoids ``query_range`` because it repeats the last value on its
    step grid; the experiments of plan sections 4 and 5 are defined on this grid, so they use it.
    Several series answering one query (they should not) are merged by maximum.
    """
    out = np.full(n, np.nan)
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
                if offset % step:
                    continue
                j = offset // step
                value = float(text)
                if 0 <= j < n and np.isfinite(value) and not value < out[j]:
                    out[j] = value
    return out


# ---------------------------------------------------------------------------
# Window, extraction and the inputs file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """The evaluation window and the frame (UTC 15-minute grid) the replay needs around it."""

    start: datetime  # first executed slot
    end: datetime  # exclusive
    frame_start: datetime
    frame_end: datetime  # end of the day after the last evaluated local day

    @property
    def n(self) -> int:
        return int((self.frame_end - self.frame_start).total_seconds()) // SLOT_S

    @property
    def k0(self) -> int:
        return int((self.start - self.frame_start).total_seconds()) // SLOT_S

    @property
    def k1(self) -> int:
        return int((self.end - self.frame_start).total_seconds()) // SLOT_S

    def key(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "frame_start": self.frame_start.isoformat(),
            "frame_end": self.frame_end.isoformat(),
        }


def make_window(start: datetime, end: datetime) -> Window:
    if end <= start:
        raise SystemExit("--to must be after --from")
    if not (_slot_aligned(start) and _slot_aligned(end)):
        raise SystemExit("--from and --to must be on a 15-minute boundary")
    first_day = start.astimezone(TZ).date()
    last_day = (end - timedelta(seconds=1)).astimezone(TZ).date()
    return Window(
        start=start,
        end=end,
        frame_start=local_midnight(first_day - timedelta(days=HISTORY_DAYS)),
        frame_end=local_midnight(last_day + timedelta(days=2)),
    )


def default_run_id(window: Window, minute: bool) -> str:
    stem = f"{window.start:%Y%m%dT%H%M}-{window.end:%Y%m%dT%H%M}"
    return stem + ("-minute" if minute else "")


def run_dir(data_dir: str | None, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id) or ".." in run_id:
        raise SystemExit(f"--run-id {run_id!r}: use only [A-Za-z0-9._-] and no '..'")
    root = resolve_data_dir(data_dir) / "analysis" / "v1" / "price-info-replay" / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_bytes(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def extract_inputs(window: Window, minute: bool) -> dict:
    """Read-only Prometheus pull of the replay inputs on the frame grid."""
    service = load_service_config(None)
    roles = service.entities
    grid_entity = roles.get("grid_power")
    price_entity = roles.get("price")
    counters = load_battery_telemetry()
    power_metric = extract_prom.ENTITY_SPECS[grid_entity].metric
    price_metric = extract_prom.ENTITY_SPECS[price_entity].metric
    esc = extract_prom.escape_promql_string
    base_url = extract_prom.resolve_base_url()
    first = int(window.frame_start.timestamp())
    n = window.n
    now = int(datetime.now(UTC).timestamp())
    commissioned = counters.commissioned_at
    if commissioned is None or not counters.has_counters:
        raise SystemExit("battery.commissioned_at and the battery counters must be configured")
    out: dict = {"commissioned_at": commissioned.isoformat()}
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        # PVPC at 8 minutes into each slot (an hourly step function), the grid as the mean power
        # of the 15 minutes ending at the slot end.
        print(f"extract: price and grid, {n} slots", file=sys.stderr)
        out["price"] = fetch_grid(
            client,
            base_url,
            f'max({price_metric}{{entity="{esc(price_entity)}"}})',
            first + 480,
            SLOT_S,
            n,
            400,
        )
        grid_w = fetch_grid(
            client,
            base_url,
            f'sum(avg_over_time({power_metric}{{entity="{esc(grid_entity)}"}}[15m]))',
            first + SLOT_S,
            SLOT_S,
            n,
            400,
        )
        out["grid_kwh"] = grid_w * H / 1000.0
        c0 = max(0, int((commissioned - window.frame_start).total_seconds()) // SLOT_S - 1)
        charge = np.full(n, np.nan)
        discharge = np.full(n, np.nan)
        for name, array in (("charge", charge), ("discharge", discharge)):
            entity = getattr(counters, f"{name}_entity")
            metric = extract_prom.BATTERY_ENERGY_METRIC
            print(f"extract: battery {name} counter", file=sys.stderr)
            total = fetch_grid(
                client,
                base_url,
                f'max({metric}{{entity="{esc(entity)}"}})',
                first + c0 * SLOT_S,
                SLOT_S,
                n - c0 + 1,
                400,
            )
            delta = np.diff(total)
            array[c0:] = np.where(delta >= 0, delta, np.nan)
        out["charge_kwh"], out["discharge_kwh"] = charge, discharge
        if minute:
            m0 = int((window.start - window.frame_start).total_seconds())
            n_min = (window.k1 - window.k0) * 15
            print(f"extract: minute grid power and battery power, {n_min} minutes", file=sys.stderr)
            out["minute_grid_w"] = fetch_grid(
                client,
                base_url,
                f'max({power_metric}{{entity="{esc(grid_entity)}"}})',
                first + m0,
                60,
                n_min,
                2880,
            )
            out["minute_battery_w"] = fetch_grid(
                client,
                base_url,
                f"max({OBSERVED_BATTERY_POWER_METRIC})",
                first + m0,
                60,
                n_min,
                2880,
            )
    out["extracted_at"] = datetime.fromtimestamp(now, UTC).isoformat()
    return out


def load_or_extract(window: Window, minute: bool, directory: Path, refetch: bool) -> dict:
    """The inputs of the run dir (reused when it matches the window), else a fresh pull."""
    path = directory / "inputs.npz"
    key = {**window.key(), "minute": minute}
    if path.is_file() and not refetch:
        with np.load(path, allow_pickle=False) as stored:
            meta = json.loads(str(stored["meta"]))
            if meta.get("window") == key:
                print(f"inputs: reusing {path}", file=sys.stderr)
                return {k: stored[k] for k in stored.files if k != "meta"} | {
                    "commissioned_at": meta["commissioned_at"]
                }
    pulled = extract_inputs(window, minute)
    meta = {
        "window": key,
        "commissioned_at": pulled.pop("commissioned_at"),
        "extracted_at": pulled.pop("extracted_at"),
    }
    buffer = io.BytesIO()
    np.savez_compressed(buffer, meta=np.array(json.dumps(meta)), **pulled)
    _write_bytes(path, buffer.getvalue())
    print(f"inputs: written to {path}", file=sys.stderr)
    return {**pulled, "commissioned_at": meta["commissioned_at"]}


def load_omie(window: Window, starts: Sequence[datetime], data_dir: str | None, fetch: bool):
    """OMIE ES prices (EUR/kWh) on the frame grid, through the repo's raw store."""
    first = window.frame_start.astimezone(TZ).date()
    last = (window.frame_end - timedelta(seconds=1)).astimezone(TZ).date()
    stored = load_omie_es_prices(data_dir, start_date=first, end_date=last)
    if stored.missing_dates and fetch:
        print(
            f"omie: fetching {len(stored.missing_dates)} missing day(s) with extract_omie",
            file=sys.stderr,
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


# ---------------------------------------------------------------------------
# The slot frame
# ---------------------------------------------------------------------------


def _fill(values: np.ndarray, limit: int, *, linear: bool) -> np.ndarray:
    """Fill the first *limit* slots of every gap (forward-fill or linear towards the next value).

    A gap longer than *limit* keeps its remaining slots missing; leading and trailing gaps are
    never filled.  An incomplete replay window is refused later.
    """
    out = values.copy()
    known = np.flatnonzero(np.isfinite(out))
    for left, right in zip(known[:-1], known[1:], strict=True):
        if right - left == 1:
            continue
        span = np.arange(left + 1, min(right, left + 1 + limit))
        if linear:
            out[span] = np.interp(span, [left, right], [out[left], out[right]])
        else:
            out[span] = out[left]
    return out


def gross_load(raw: dict, window: Window, starts: Sequence[datetime]) -> np.ndarray:
    """Gross house load per slot (kWh): grid alone before commissioning, else grid - chg + dis."""
    commissioned = datetime.fromisoformat(raw["commissioned_at"])
    grid = raw["grid_kwh"]
    ends = np.array([(s + timedelta(seconds=SLOT_S)) > commissioned for s in starts])
    plain = np.maximum(grid, 0.0)
    with_battery = np.maximum(grid - raw["charge_kwh"] + raw["discharge_kwh"], 0.0)
    return _fill(np.where(ends, with_battery, plain), 8, linear=True)


def minute_profile(raw: dict, load: np.ndarray, window: Window) -> np.ndarray:
    """Per-slot minute load (W, shape ``(n, 15)``): measured inside the window, flat elsewhere.

    A minute is ``max(grid - battery, 0)``; a missing minute takes the mean of the known minutes of
    its slot (flat when none is known); the profile is rescaled so its mean matches the slot kWh.
    """
    n = load.size
    flat = np.repeat(load[:, None] * 4000.0, 15, axis=1)
    k0, k1 = window.k0, window.k1
    grid, battery = raw["minute_grid_w"], raw["minute_battery_w"]
    prof = np.maximum(grid - battery, 0.0).reshape(k1 - k0, 15)
    count = (~np.isnan(prof)).sum(1)
    mean = np.where(count > 0, np.nansum(prof, 1) / np.maximum(count, 1), np.nan)
    prof = np.where(np.isnan(prof), mean[:, None], prof)
    prof[np.isnan(prof).any(1)] = 1.0
    target = load[k0:k1] * 4000.0
    level = prof.mean(1)
    prof = np.where(
        level[:, None] > 1e-6, prof * (target / np.maximum(level, 1e-6))[:, None], target[:, None]
    )
    out = flat.copy()
    out[k0:k1] = prof
    if out.shape[0] != n:
        raise AssertionError("minute profile shape")
    return out


def lf0_forecast(load: np.ndarray, ldate: np.ndarray, lslot: np.ndarray) -> np.ndarray:
    """Causal LF-0: mean of the same clock slot over the previous 28 local days (min 14)."""
    days = np.unique(ldate)
    day_row = {int(d): i for i, d in enumerate(days)}
    total = np.zeros((days.size, SLOTS_PER_DAY))
    count = np.zeros((days.size, SLOTS_PER_DAY))
    rows = np.array([day_row[int(d)] for d in ldate])
    valid = np.isfinite(load)
    np.add.at(total, (rows[valid], lslot[valid]), load[valid])
    np.add.at(count, (rows[valid], lslot[valid]), 1.0)
    cell = np.where(count > 0, total / np.maximum(count, 1.0), np.nan)
    rolled = np.full_like(cell, np.nan)
    for i in range(days.size):
        window = cell[max(0, i - LF0_DAYS) : i]
        known = np.isfinite(window).sum(0)
        with np.errstate(invalid="ignore"):
            mean = np.nansum(window, 0) / np.maximum(known, 1)
        rolled[i] = np.where(known >= LF0_MIN_DAYS, mean, np.nan)
    return rolled[rows, lslot]


class Data:
    """The slot frame of the replay: prices, loads, estimates and the calendar."""

    def __init__(
        self,
        starts: list[datetime],
        price: np.ndarray,
        load: np.ndarray,
        omie: np.ndarray,
        minute: np.ndarray | None,
        margin_w: np.ndarray,
    ) -> None:
        self.starts = starts
        self.n = len(starts)
        self.price, self.L, self.omie = price, load, omie
        local = [s.astimezone(TZ) for s in starts]
        self.ldates = [x.date() for x in local]
        self.ldate = np.array([x.toordinal() for x in self.ldates])
        self.lmin = np.array([x.hour * 60 + x.minute for x in local])
        self.lslot = self.lmin // 15
        self.day_start: dict[int, int] = {}
        self.day_end: dict[int, int] = {}
        for i, d in enumerate(self.ldate.tolist()):
            self.day_start.setdefault(d, i)
            self.day_end[d] = i + 1
        self.days = [(date.fromordinal(d), a, self.day_end[d]) for d, a in self.day_start.items()]
        self.Lf = lf0_forecast(load, self.ldate, self.lslot)
        self.minute = minute if minute is not None else np.repeat(load[:, None] * 4000.0, 15, 1)
        self.margin_w = margin_w
        self.side = OutlookSide(omie)
        self.est: dict[tuple[str, int], tuple[int, int, np.ndarray] | None] = {}

    def estimate(self, mode: str, day: int):
        """The repo outlook's prices for local day ``day + 1`` decided on local day ``day``.

        Returns ``(a1, b1, values)`` or ``None`` when any slot of tomorrow cannot be estimated
        (then the day is planned as ``published_only``).
        """
        a1, b1 = self.day_start.get(day + 1), self.day_end.get(day + 1)
        a0 = self.day_start.get(day)
        if a1 is None or a0 is None:
            return None
        at = int(np.searchsorted(self.lmin[a0 : self.day_end[day]], START_T[mode]))
        result = run_price_outlook(
            mode,
            prices=self.price,
            published_end_idx=self.day_end[day],
            decision_idx=a0 + at,
            plan_end_idx=b1,
            interval_starts_utc=self.starts,
            days=self.days,
            tz=TZ,
            side=self.side,
        )
        tomorrow = result.values[self.day_end[day] - (a0 + at) :]
        flags = result.provenance[self.day_end[day] - (a0 + at) :]
        if tomorrow.size != b1 - a1 or not np.all(flags == SLOT_ESTIMATED):
            return None
        return a1, b1, np.array(tomorrow, dtype=float)

    def precompute_estimates(self, k0: int, k1: int) -> None:
        for mode in ESTIMATED_MODES:
            for day in range(int(self.ldate[k0]), int(self.ldate[k1 - 1]) + 1):
                self.est[(mode, day)] = self.estimate(mode, day)


def build_data(
    raw: dict, window: Window, omie: np.ndarray, starts: list[datetime], cfg: Settings, minute: bool
) -> tuple[Data, list[int], dict]:
    """The replay frame from the raw inputs, with the hole days patched and listed."""
    price = _fill(raw["price"], 8, linear=False)
    load = gross_load(raw, window, starts)
    if minute:
        profile = minute_profile(raw, load, window)
        change = int((MARGIN_CHANGE_UTC - window.frame_start).total_seconds()) // SLOT_S
        margin = np.where(np.arange(len(starts)) < change, cfg.margin_base_w, DEPLOYED_MARGIN_W)
    else:
        profile = None
        margin = np.full(len(starts), DEPLOYED_MARGIN_W)
    d = Data(starts, price, load, omie, profile, margin)
    holes: list[int] = []
    for day in sorted(d.day_start):
        a, b = d.day_start[day], d.day_end[day]
        if b - a >= SLOTS_PER_DAY - 4 and np.all(d.price[a:b] == 0.0):
            holes.append(day)
    first_day = int(d.ldate[window.k0])
    for day in holes:
        patch = d.estimate("omie_period", day - 1)
        if patch is not None:
            d.price[patch[0] : patch[1]] = patch[2]
        elif day >= first_day - 1:
            raise SystemExit(
                f"PVPC hole on {date.fromordinal(day)}: no omie_period estimate to patch it with"
            )
        else:
            print(
                f"hole on {date.fromordinal(day)} is unpatched history: left out of the means",
                file=sys.stderr,
            )
            d.price[d.day_start[day] : d.day_end[day]] = np.nan
    note = {"hole_days": [date.fromordinal(day).isoformat() for day in holes]}
    bad = sorted({day - j for day in holes for j in range(0, HOLE_EXCLUDE_DAYS + 1)})
    return d, bad, note


def check_window(d: Data, window: Window) -> None:
    """Refuse a window whose inputs have gaps inside the replayed range."""
    k0, k1 = window.k0, window.k1
    problems = []
    for name, array, upper in (
        ("price", d.price, d.n),
        ("load", d.L, k1),
        ("LF-0 forecast", d.Lf, k1),
    ):
        missing = int(np.isnan(array[k0:upper]).sum())
        if missing:
            problems.append(f"{missing} slot(s) of {name} missing")
    if problems:
        raise SystemExit("inputs incomplete inside the window: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# The LP replay engine
# ---------------------------------------------------------------------------

_A_CACHE: dict[int, object] = {}


def _balance(n_slots: int, eta: float):
    key = (n_slots, eta)
    if key not in _A_CACHE:
        step = diags([np.ones(n_slots), -np.ones(n_slots - 1)], [0, -1], format="csr")
        eye = identity(n_slots, format="csr")
        _A_CACHE[key] = hstack([-eta * eye, eye / eta, step], format="csr")
    return _A_CACHE[key]


def solve(cfg: Settings, p, load, margin_kwh, s0, tv: float):
    """One LP over the planning prices; first-slot charge and discharge (AC kWh), storage value."""
    n = len(p)
    cu = np.minimum(cfg.pmax * H, np.maximum(0.0, cfg.limit * H - load))
    du = np.minimum(cfg.pmax * H, np.maximum(0.0, load - margin_kwh))
    cost = np.concatenate([p + cfg.wear, -p + cfg.wear, np.zeros(n)])
    cost[-1] -= tv
    b = np.zeros(n)
    b[0] = s0
    lo = np.concatenate([np.zeros(2 * n), np.full(n, cfg.smin if s0 >= cfg.smin else s0)])
    hi = np.concatenate([cu, du, np.full(n, cfg.smax)])
    result = linprog(
        cost,
        A_eq=_balance(n, cfg.eta),
        b_eq=b,
        bounds=np.column_stack([lo, hi]),
        method="highs",
    )
    if result.status != 0:
        raise RuntimeError(f"LP failed: {result.message}")
    return result.x[0], result.x[n], -result.eqlin.marginals[0]


def horizon(cfg: Settings, d: Data, k: int, mode: str, data_end: int):
    """End index (inclusive) and planning price vector of decision slot *k* under *mode*."""
    day, lm = int(d.ldate[k]), int(d.lmin[k])
    tomorrow = lm >= START_T[mode]
    last = day + 1 if tomorrow else day
    end = min(d.day_end.get(last, data_end) - 1, data_end - 1, k + cfg.max_horizon - 1)
    p = d.price[k : end + 1].copy()
    if tomorrow and lm < PUB_T and mode in ESTIMATED_MODES:
        est = d.est.get((mode, day))
        if est is None:
            end = min(end, d.day_end[day] - 1)
            p = d.price[k : end + 1].copy()
        else:
            a1, b1, values = est
            a, b = max(a1, k), min(b1, end + 1)
            if b > a:
                p[a - k : b - k] = values[a - a1 : b - a1]
    return end, p


def run_replay(
    cfg: Settings, d: Data, k0: int, k1: int, mode: str, plan_load, tv_arr, data_end: int
) -> np.ndarray:
    """Roll the LP over ``[k0, k1)``; columns: charge, discharge, grid, SOC, value, engaged."""
    s = cfg.s0
    out = np.zeros((d.n, 6))
    for k in range(k0, k1):
        end, p = horizon(cfg, d, k, mode, data_end)
        margin = d.margin_w[k]
        c, dis_plan, value = solve(
            cfg,
            p,
            plan_load[k : end + 1],
            d.margin_w[k : end + 1] / 1000.0 * H,
            max(s, cfg.smin),
            float(tv_arr[k]),
        )
        v_ac = value / cfg.eta
        minute = d.minute[k]
        avail = max(0.0, s - cfg.smin) * cfg.eta
        chg = dis = engaged = 0.0
        if dis_plan > 5e-3:
            limit_kwh = dis_plan
            if (d.price[k] - cfg.wear) >= v_ac + cfg.lf_tol:
                limit_kwh = max(0.0, s - cfg.smin - cfg.lf_floor_hyst) * cfg.eta
                engaged = 1.0
            per_minute = np.minimum(cfg.lf_ceil_w, np.maximum(0.0, minute - margin)) / 60000.0
            dis = float(min(per_minute.sum(), limit_kwh, avail))
        elif c > 5e-3:
            chg = min(c, max(0.0, cfg.limit * H - d.L[k]), (cfg.smax - s) / cfg.eta)
        s = s + chg * cfg.eta - dis / cfg.eta
        s = min(max(s, cfg.smin if s >= cfg.smin else s), cfg.smax)
        out[k] = (chg, dis, d.L[k] + chg - dis, s, v_ac, engaged)
    return out


# ---------------------------------------------------------------------------
# Terminal-value rules
# ---------------------------------------------------------------------------


def parse_rule(rule: str) -> tuple[str, float | None]:
    if rule in ("trailing_mean", *REFILL_RULES):
        return rule, None
    match = re.fullmatch(r"const:(-?\d+(?:\.\d+)?)", rule)
    if not match:
        raise SystemExit(
            f"terminal-value rule {rule!r}: use const:<value>, trailing_mean, refill or refill_p3"
        )
    return "const", float(match.group(1))


def trailing_mean_array(d: Data) -> np.ndarray:
    """The service rule per slot: mean of the daily mean PVPC of the 28 days before today."""
    days = np.unique(d.ldate)
    daily = np.full(days.size, np.nan)
    for i, x in enumerate(days.tolist()):
        known = d.price[d.day_start[x] : d.day_end[x]]
        known = known[np.isfinite(known)]
        if known.size:
            daily[i] = known.mean()
    mean = np.full(days.size, np.nan)
    for i in range(days.size):
        window = daily[max(0, i - LF0_DAYS) : i]
        window = window[np.isfinite(window)]
        if window.size >= LF0_MIN_DAYS:
            mean[i] = window.mean()
    row = {int(x): i for i, x in enumerate(days)}
    return np.array([mean[row[int(x)]] for x in d.ldate])


def tv_array(cfg: Settings, d: Data, rule: str) -> np.ndarray:
    """Planning terminal value (EUR/DC-kWh) of every decision slot under *rule*."""
    kind, value = parse_rule(rule)
    if kind == "const":
        return np.full(d.n, value)
    trailing = trailing_mean_array(d)
    if kind == "trailing_mean":
        return trailing
    # refill: cheapest valle (P3) slot of the most recently published local day, over the charge
    # efficiency plus wear, capped by the trailing mean.  ``refill`` looks at the overnight slots
    # (00:00-08:00 local) only: the refill the planner can use after midnight.  ``refill_p3``
    # takes every P3 slot of the day, which on a weekend or holiday includes the midday trough.
    night_only = kind == "refill"
    cheapest: dict[int, float] = {}
    for day in np.unique(d.ldate).tolist():
        a, b = d.day_start[day], d.day_end[day]
        valle = [
            d.price[i]
            for i in range(a, b)
            if tariff_period(d.ldates[i], int(d.lmin[i]) // 60) == 3
            and (not night_only or d.lmin[i] < 8 * 60)
            and np.isfinite(d.price[i])
        ]
        cheapest[day] = min(valle) if valle else np.nan
    out = np.empty(d.n)
    for k in range(d.n):
        published = int(d.ldate[k]) + (1 if d.lmin[k] >= PUB_T else 0)
        refill = (cheapest.get(published, np.nan) + cfg.wear) / cfg.eta
        out[k] = min(trailing[k], refill) if np.isfinite(refill) else trailing[k]
    return out


# ---------------------------------------------------------------------------
# Jobs (one replay each), run in worker processes
# ---------------------------------------------------------------------------

_WORKER: dict = {}


def _init_worker(cfg: Settings, d: Data, k0: int, k1: int, data_end: int) -> None:
    _WORKER.update(cfg=cfg, d=d, k0=k0, k1=k1, data_end=data_end, tv={})


def _job(spec: tuple[str, str, str]) -> tuple[tuple[str, str, str], np.ndarray]:
    load, mode, rule = spec
    w = _WORKER
    d: Data = w["d"]
    if rule not in w["tv"]:
        w["tv"][rule] = tv_array(w["cfg"], d, rule)
    plan_load = d.Lf if load == "lf0" else d.L
    o = run_replay(w["cfg"], d, w["k0"], w["k1"], mode, plan_load, w["tv"][rule], w["data_end"])
    return spec, o[w["k0"] : w["k1"]]


def run_jobs(
    specs: Sequence[tuple[str, str, str]],
    cfg: Settings,
    d: Data,
    window: Window,
    workers: int,
) -> dict[tuple[str, str, str], np.ndarray]:
    t0 = time.time()
    done: dict = {}
    args = (cfg, d, window.k0, window.k1, d.n)
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=args) as pool:
        for spec, o in pool.map(_job, specs):
            done[spec] = o
            print(
                f"replay: {spec} done ({len(done)}/{len(specs)}, {time.time() - t0:.0f}s)",
                file=sys.stderr,
            )
    return done


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


class Stats:
    """Per-slot marked net, per-day sums and the bootstrap on one replayed window."""

    def __init__(self, cfg: Settings, d: Data, window: Window, bad_days: Sequence[int]) -> None:
        self.cfg, self.d = cfg, d
        self.k0, self.k1 = window.k0, window.k1
        self.ldays = d.ldate[self.k0 : self.k1]
        self.lmin = d.lmin[self.k0 : self.k1]
        self.price = d.price[self.k0 : self.k1]
        self.keep = ~np.isin(self.ldays, list(bad_days))
        self.ud = np.unique(self.ldays[self.keep])
        self.weekday = np.array([date.fromordinal(int(x)).weekday() for x in self.ud])

    def per_slot(self, o: np.ndarray) -> np.ndarray:
        """Marked net per slot: bill saving + MARK_TV * dSOC - wear."""
        chg, dis, soc = o[:, 0], o[:, 1], o[:, 3]
        prev = np.concatenate([[self.cfg.s0], soc[:-1]])
        return self.price * (dis - chg) + MARK_TV * (soc - prev) - self.cfg.wear * (chg + dis)

    def by_day(self, values: np.ndarray, sel: np.ndarray | None = None) -> np.ndarray:
        mask = self.keep if sel is None else (self.keep & sel)
        sums = np.zeros(self.ud.size)
        index = np.searchsorted(self.ud, self.ldays[mask])
        np.add.at(sums, index, values[mask])
        return sums

    def daily(self, o: np.ndarray) -> np.ndarray:
        return self.by_day(self.per_slot(o))

    def bootstrap(self, diff: np.ndarray) -> tuple[tuple[float, float] | None, int]:
        """95 % interval of the summed daily gain, weekly-block bootstrap (ratio estimator)."""
        weeks = np.array([ordinal_week(x) for x in self.ud])
        labels, inverse = np.unique(weeks, return_inverse=True)
        sums = np.bincount(inverse, weights=diff)
        counts = np.bincount(inverse).astype(float)
        n_blocks, total = labels.size, counts.sum()
        if n_blocks < MIN_BLOCKS_FOR_CI:
            return None, n_blocks
        rng = np.random.default_rng(BOOTSTRAP_SEED)
        pick = rng.integers(0, n_blocks, (BOOTSTRAP_DRAWS, n_blocks))
        draws = sums[pick].sum(1) / counts[pick].sum(1) * total
        low, high = np.percentile(draws, [2.5, 97.5])
        return (float(low), float(high)), n_blocks


def _ci_text(ci: tuple[float, float] | None, n_blocks: int) -> str:
    if ci is None:
        return f"n/a ({n_blocks} weekly blocks, too few)"
    return f"[{ci[0]:.3f}, {ci[1]:.3f}] ({n_blocks} weekly blocks)"


def _date_text(ordinal: int) -> str:
    return date.fromordinal(int(ordinal)).isoformat()


def net_table(out: list[str], st: Stats, runs: dict, load: str, rule: str, modes) -> dict:
    """The headline table; returns the daily net per mode."""
    daily = {m: st.daily(runs[(load, m, rule)]) for m in modes}
    base = daily["published_only"]
    ceiling = daily["exact"].sum() - base.sum() if "exact" in daily else 0.0
    out.append(
        f"\n===== load={load} terminal value={rule}: {st.ud.size} local days "
        f"({_date_text(st.ud[0])}..{_date_text(st.ud[-1])}), "
        f"{'hole days excluded' if (~st.keep).any() else 'no hole days'} ====="
    )
    out.append(
        f"{'mode':16s} {'net EUR':>9s} {'EUR/day':>8s} {'gap vs pub':>10s} {'gap %':>7s} "
        f"{'share of exact gap':>18s}  95% CI of gap (weekly block bootstrap)"
    )
    for m in modes:
        gap = daily[m].sum() - base.sum()
        ci_text = ""
        if m != "published_only":
            ci_text = _ci_text(*st.bootstrap(daily[m] - base))
        share = 100 * gap / ceiling if ceiling else 0.0
        out.append(
            f"{m:16s} {daily[m].sum():9.3f} {daily[m].mean():8.4f} {gap:10.3f} "
            f"{100 * gap / base.sum():6.1f}% {share:17.0f}%  {ci_text}"
        )
    return daily


def mechanism_report(out: list[str], st: Stats, runs: dict, load: str, rule: str, daily) -> None:
    """Day counts, hour buckets and the mechanism of the gap (lf0 only)."""
    modes = [m for m in ("exact", "always_36h", "omie_period", "persistence") if m in daily]
    base = daily["published_only"]
    for m in ("exact", "omie_period", "persistence"):
        if m not in daily:
            continue
        diff = daily[m] - base
        top = np.sort(diff)[-max(1, diff.size // 10) :].sum()
        out.append(
            f"  {m}: days better {int((diff > 0.005).sum())}, worse {int((diff < -0.005).sum())}, "
            f"equal {int((abs(diff) <= 0.005).sum())}; best day +{diff.max():.3f}, worst "
            f"{diff.min():.3f}; share of gap from top-10% days {100 * top / diff.sum():.0f}%"
        )
    ref = {m: runs[(load, m, rule)] for m in ("published_only", *modes)}
    slot = {m: st.per_slot(o) for m, o in ref.items()}
    out.append("gap vs published_only by local-hour bucket of the executed slot (marked net):")
    out.append(f"{'mode':14s}" + "".join(f"{b[0]:>9s}" for b in BUCKETS))
    for m in modes:
        row = []
        for _, a, b in BUCKETS:
            sel = st.keep & (st.lmin >= a) & (st.lmin < b)
            row.append((slot[m][sel] - slot["published_only"][sel]).sum())
        out.append(f"{m:14s}" + "".join(f"{v:9.3f}" for v in row))
    if "exact" not in ref:
        return
    pub, exact = ref["published_only"], ref["exact"]
    out.append("exact - published_only, kWh charged / discharged per bucket:")
    for name, col in (("chg", 0), ("dis", 1)):
        row = []
        for _, a, b in BUCKETS:
            sel = st.keep & (st.lmin >= a) & (st.lmin < b)
            row.append(exact[sel, col].sum() - pub[sel, col].sum())
        out.append(f"  d{name}  " + "".join(f"{v:9.2f}" for v in row))
    k = st.keep
    out.append(
        f"  totals: published chg/dis {pub[k, 0].sum():.1f}/{pub[k, 1].sum():.1f} kWh, "
        f"exact {exact[k, 0].sum():.1f}/{exact[k, 1].sum():.1f}"
    )
    for m in ("published_only", "exact"):
        o = ref[m]
        out.append(
            f"  {m}: avg price paid on charge "
            f"{np.sum(st.price[k] * o[k, 0]) / o[k, 0].sum():.4f}, avg price on discharge "
            f"{np.sum(st.price[k] * o[k, 1]) / o[k, 1].sum():.4f} EUR/kWh"
        )
    window = (st.lmin >= 840) & (st.lmin < 1230)
    out.append(
        f"days (of {st.ud.size}) where executed battery action in 14:00-20:30 local differs "
        "from published_only:"
    )
    net_pub = pub[:, 1] - pub[:, 0]
    for m in modes:
        diff = np.abs((ref[m][:, 1] - ref[m][:, 0]) - net_pub)
        in_window = np.zeros(st.ud.size)
        any_slot = np.zeros(st.ud.size)
        for target, sel in ((in_window, window & k), (any_slot, k)):
            index = np.searchsorted(st.ud, st.ldays[sel])
            np.maximum.at(target, index, diff[sel])
        out.append(
            f"  {m:14s} >10 Wh in 14-20:30: {int((in_window > 0.01).sum())} days; >1 Wh: "
            f"{int((in_window > 0.001).sum())}; any slot of the day differs >10 Wh: "
            f"{int((any_slot > 0.01).sum())}"
        )
    afternoon, evening = (st.lmin >= 840) & (st.lmin < 1230), st.lmin >= 1230

    def delta(col: int, sel: np.ndarray) -> np.ndarray:
        return st.by_day(exact[:, col] - pub[:, col], sel)

    d_chg_a, d_dis_a = delta(0, afternoon), delta(1, afternoon)
    d_dis_e, d_chg_e = delta(1, evening), delta(0, evening)
    gap = daily["exact"] - base
    sig = np.abs(gap) > 0.005
    out.append(
        f"mechanism (exact vs published_only), {int(sig.sum())} days with |gap|>0.005 EUR; sum "
        f"gap there {gap[sig].sum():.3f} of {gap.sum():.3f}"
    )
    if sig.any():
        out.append(
            f"  mean dkWh on those days: chg 14-20:30 {d_chg_a[sig].mean():+.2f}, dis 14-20:30 "
            f"{d_dis_a[sig].mean():+.2f}, chg 20:30-24 {d_chg_e[sig].mean():+.2f}, dis 20:30-24 "
            f"{d_dis_e[sig].mean():+.2f}"
        )
    classes: dict[str, list[float]] = {}
    for i in range(st.ud.size):
        if not sig[i]:
            name = "none"
        elif d_chg_a[i] < -0.15:
            name = "afternoon charge skipped/cut"
        elif d_chg_a[i] > 0.15:
            name = "extra afternoon charge"
        elif d_dis_a[i] + d_dis_e[i] > 0.15:
            name = "more evening discharge"
        elif d_dis_a[i] + d_dis_e[i] < -0.15:
            name = "less evening discharge (hold)"
        else:
            name = "other"
        classes.setdefault(name, []).append(gap[i])
    out.append(f"  {'class':32s} {'count':>5s} {'sum':>8s} {'mean':>8s}")
    for name in sorted(classes):
        v = np.array(classes[name])
        out.append(f"  {name:32s} {v.size:5d} {v.sum():8.3f} {v.mean():8.3f}")
    for hour in (22, 0, 6):
        sel = (st.lmin == hour * 60) & k
        out.append(
            f"  SOC at {hour:02d}:00 local: published {pub[sel, 3].mean():.2f} exact "
            f"{exact[sel, 3].mean():.2f} kWh (mean)"
        )
    out.append(
        "  mean storage value v_ac (EUR/kWh AC) at 14:00/17:00/20:00 local: "
        + "; ".join(
            f"{h}h pub {pub[(st.lmin == h * 60) & k, 4].mean():.3f} "
            f"exact {exact[(st.lmin == h * 60) & k, 4].mean():.3f}"
            for h in (14, 17, 20)
        )
    )
    out.append("cash-only gap vs published_only by bucket (bill - wear, EUR; no inventory mark):")
    out.append(f"{'':14s}" + "".join(f"{b[0]:>9s}" for b in BUCKETS))
    for m in [m for m in ("exact", "omie_period", "persistence") if m in ref]:
        row = []
        for _, a, b in BUCKETS:
            sel = k & (st.lmin >= a) & (st.lmin < b)

            def cash(o: np.ndarray, sel: np.ndarray = sel) -> float:
                return float(
                    (st.price * (o[:, 1] - o[:, 0]) - st.cfg.wear * (o[:, 0] + o[:, 1]))[sel].sum()
                )

            row.append(cash(ref[m]) - cash(pub))
        out.append(f"{m:14s}" + "".join(f"{v:9.3f}" for v in row))
    s2030 = exact[(st.lmin == 1230) & k, 3] - pub[(st.lmin == 1230) & k, 3]
    s2345 = exact[(st.lmin == 1425) & k, 3] - pub[(st.lmin == 1425) & k, 3]
    out.append(
        f"  days with exact SOC at 20:30 lower by >0.1 kWh: {int((s2030 < -0.1).sum())}, higher: "
        f"{int((s2030 > 0.1).sum())}; at 23:45 lower {int((s2345 < -0.1).sum())}, higher "
        f"{int((s2345 > 0.1).sum())} (of {s2345.size})"
    )
    d = st.d
    early = np.array(
        [d.price[d.day_start[int(x) + 1] : d.day_start[int(x) + 1] + 32].mean() for x in st.ud]
    )
    midday = np.array(
        [d.price[d.day_start[int(x) + 1] + 32 : d.day_start[int(x) + 1] + 56].mean() for x in st.ud]
    )
    out.append("exact-published_only gap by tomorrow 00-08 local mean price (known from 14:00):")
    for lo, hi in ((-1, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 1)):
        sel = (early >= lo) & (early < hi)
        if sel.sum():
            out.append(
                f"  tomorrow 00-08 in [{lo:.2f},{hi:.2f}): {int(sel.sum()):3d} days, mean gap "
                f"{gap[sel].mean():+.4f} EUR/day, total {gap[sel].sum():+.3f}"
            )
    if gap.std() > 0:
        out.append(
            f"  corr(gap, tomorrow 00-08 price) = {np.corrcoef(early, gap)[0, 1]:+.2f}; "
            f"corr(gap, tomorrow 08-14 price) = {np.corrcoef(midday, gap)[0, 1]:+.2f}"
        )
    out.append(
        "  gap by weekday of decision day (Mon..Sun, EUR total): "
        + " ".join(f"{gap[st.weekday == i].sum():+.3f}" for i in range(7))
    )


def sensitivity_report(out: list[str], st: Stats, runs: dict, load: str, constants) -> None:
    """Planner terminal value sensitivity (net marked at MARK_TV; gaps against published)."""
    out.append("TV sensitivity (planner TV; net marked at MARK_TV; gap = mode - published_only):")
    for tv in constants:
        rule = f"const:{tv:g}"
        net = {m: st.daily(runs[(load, m, rule)]).sum() for m in TV_SENSITIVITY_MODES}
        out.append(
            f"  TV {tv:g}: published {net['published_only']:.3f}  exact "
            f"{net['exact'] - net['published_only']:+.3f}  omie "
            f"{net['omie_period'] - net['published_only']:+.3f}  persistence "
            f"{net['persistence'] - net['published_only']:+.3f}"
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _prepare(args: argparse.Namespace):
    window = make_window(args.start, args.end)
    directory = run_dir(args.data_dir, args.run_id or default_run_id(window, args.minute))
    cfg = load_settings()
    raw = load_or_extract(window, args.minute, directory, args.refetch)
    starts = [window.frame_start + timedelta(seconds=SLOT_S * i) for i in range(window.n)]
    omie, missing = load_omie(window, starts, args.data_dir, args.fetch_omie)
    if missing:
        print(
            f"omie: {len(missing)} day(s) without a stored price ({missing[0]}..{missing[-1]}); "
            "days without tomorrow's OMIE plan as published_only",
            file=sys.stderr,
        )
    d, bad, note = build_data(raw, window, omie, starts, cfg, args.minute)
    check_window(d, window)
    d.precompute_estimates(window.k0, window.k1)
    st = Stats(cfg, d, window, bad)
    return window, directory, cfg, d, st, note


def _emit(out: list[str], directory: Path, name: str) -> None:
    text = "\n".join(out) + "\n"
    print(text, end="")
    _write_bytes(directory / name, text.encode())
    print(f"written to {directory / name}", file=sys.stderr)


def _header(window: Window, args: argparse.Namespace, cfg: Settings, note: dict) -> list[str]:
    return [
        f"price information replay {window.start.isoformat()} .. {window.end.isoformat()} "
        f"({'minute execution, live margins' if args.minute else 'slot execution'}), "
        f"capacity {cfg.cap} kWh, power {cfg.pmax} kW, wear {cfg.wear}, mark TV {MARK_TV}",
        f"PVPC hole days found (patched; left out of the statistics when inside the window): "
        f"{note['hole_days'] or 'none'}",
    ]


def cmd_extract(args: argparse.Namespace) -> int:
    window = make_window(args.start, args.end)
    directory = run_dir(args.data_dir, args.run_id or default_run_id(window, args.minute))
    load_or_extract(window, args.minute, directory, True)
    starts = [window.frame_start + timedelta(seconds=SLOT_S * i) for i in range(window.n)]
    _, missing = load_omie(window, starts, args.data_dir, args.fetch_omie)
    print(f"omie: {len(missing)} day(s) without a stored price", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    window, directory, cfg, d, st, note = _prepare(args)
    loads = args.load.split(",")
    sens = [float(x) for x in args.tv_sensitivity.split(",")] if args.tv_sensitivity else []
    specs = [(load, m, f"const:{DEFAULT_TV:g}") for load in loads for m in MODES]
    if sens:
        specs += [
            (load, m, f"const:{tv:g}")
            for load in loads[:1]
            for m in TV_SENSITIVITY_MODES
            for tv in sens
            if f"const:{tv:g}" != f"const:{DEFAULT_TV:g}"
        ]
    if not args.no_trailing_mean:
        specs += [(loads[0], m, "trailing_mean") for m in TV_SENSITIVITY_MODES]
    runs = run_jobs(list(dict.fromkeys(specs)), cfg, d, window, args.workers)
    out = _header(window, args, cfg, note)
    rule = f"const:{DEFAULT_TV:g}"
    for load in loads:
        daily = net_table(out, st, runs, load, rule, MODES)
        if load == "lf0":
            mechanism_report(out, st, runs, load, rule, daily)
    if sens:
        sensitivity_report(out, st, runs, loads[0], sorted({*sens, DEFAULT_TV}))
    if not args.no_trailing_mean:
        tm = trailing_mean_array(d)[window.k0 : window.k1][st.keep]
        out.append(
            f"service-like terminal value: TV = trailing 28-day mean PVPC (mean over window "
            f"{np.nanmean(tm):.3f}) instead of a constant:"
        )
        net_table(out, st, runs, loads[0], "trailing_mean", TV_SENSITIVITY_MODES)
    _emit(out, directory, "report.txt")
    return 0


def cmd_reanalysis(args: argparse.Namespace) -> int:
    window, directory, cfg, d, st, note = _prepare(args)
    constants = [float(x) for x in args.a_constants.split(",")]
    tm = "trailing_mean"
    candidates: dict[str, tuple[str, str, str]] = {
        f"A const {c:g}": ("lf0", "published_only", f"const:{c:g}") for c in constants
    }
    candidates["B refill (night)"] = ("lf0", "published_only", "refill")
    candidates["B refill (any P3)"] = ("lf0", "published_only", "refill_p3")
    candidates["C persistence"] = ("lf0", "persistence", tm)
    candidates["C omie_period"] = ("lf0", "omie_period", tm)
    base_spec = ("lf0", "published_only", tm)
    ceiling_spec = ("lf0", "exact", tm)
    specs = list(dict.fromkeys([base_spec, ceiling_spec, *candidates.values()]))
    runs = run_jobs(specs, cfg, d, window, args.workers)
    out = _header(window, args, cfg, note)
    base = st.daily(runs[base_spec])
    ceiling = st.daily(runs[ceiling_spec]) - base
    out.append(
        f"\nPre-registered re-analysis (plan section 5): {st.ud.size} local days "
        f"({_date_text(st.ud[0])}..{_date_text(st.ud[-1])}); baseline published_only with the "
        f"trailing-mean terminal value, ceiling exact from 14:00 with the same."
    )
    out.append(
        f"baseline net {base.sum():.3f}; ceiling gap {ceiling.sum():+.3f} "
        f"({100 * ceiling.sum() / base.sum():+.1f}% of the baseline saving), "
        f"CI {_ci_text(*st.bootstrap(ceiling))}"
    )
    out.append(
        f"\n{'candidate':16s} {'gain':>8s} {'% saving':>9s} {'% ceiling':>10s} "
        f"{'CI of gain':>34s} {'>0':>3s}  weekday gains Mon..Sun {'':23s} adopt"
    )
    gains: dict[str, np.ndarray] = {}
    for name, spec in candidates.items():
        gain = st.daily(runs[spec]) - base
        gains[name] = gain
        ci, n_blocks = st.bootstrap(gain)
        above = ci is not None and ci[0] > 0
        weekdays = [gain[st.weekday == i].sum() for i in range(7)]
        worse = [i for i in range(7) if (st.weekday == i).any() and weekdays[i] < -1e-9]
        adopt = above and not worse
        out.append(
            f"{name:16s} {gain.sum():8.3f} {100 * gain.sum() / base.sum():8.1f}% "
            f"{100 * gain.sum() / ceiling.sum() if ceiling.sum() else 0.0:9.0f}% "
            f"{_ci_text(ci, n_blocks):>34s} {'yes' if above else 'no':>3s}  "
            + " ".join(f"{x:+.3f}" for x in weekdays)
            + f"  {'YES' if adopt else 'no'}"
            + (f" (worse: {','.join(WEEKDAYS[i] for i in worse)})" if worse else "")
        )
    out.append("\nC against B (prefer B unless C beats B with an interval above zero):")
    for c_name in ("C persistence", "C omie_period"):
        diff = gains[c_name] - gains["B refill (night)"]
        ci, n_blocks = st.bootstrap(diff)
        beats = ci is not None and ci[0] > 0
        out.append(
            f"  {c_name} - B refill (night): {diff.sum():+.3f} CI {_ci_text(ci, n_blocks)}; "
            f"C beats B with an interval above zero: {'yes' if beats else 'no'}"
        )
    tmb = trailing_mean_array(d)[window.k0 : window.k1][st.keep]
    for rule in REFILL_RULES:
        tvb = tv_array(cfg, d, rule)[window.k0 : window.k1][st.keep]
        out.append(
            f"\n{rule} terminal value over the window: mean {tvb.mean():.3f}, min "
            f"{tvb.min():.3f}, max {tvb.max():.3f} EUR/DC-kWh; trailing mean {tmb.mean():.3f}"
        )
    _emit(out, directory, "reanalysis.txt")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--from", dest="start", type=parse_instant, required=True)
        p.add_argument("--to", dest="end", type=parse_instant, required=True)
        p.add_argument("--minute", action="store_true", help="minute execution, live margins")
        p.add_argument("--data-dir", default=None)
        p.add_argument("--run-id", default=None)
        p.add_argument("--fetch-omie", action="store_true", help="fetch missing OMIE days first")

    p = sub.add_parser("extract", help="pull the inputs into the data dir")
    common(p)
    p.set_defaults(func=cmd_extract)

    def runner(p: argparse.ArgumentParser) -> None:
        common(p)
        p.add_argument("--refetch", action="store_true", help="ignore the inputs of the run dir")
        p.add_argument("--workers", type=int, default=min(24, os.cpu_count() or 1))

    p = sub.add_parser("report", help="the summary tables of plan section 4")
    runner(p)
    p.add_argument("--load", default="lf0", help="comma list of lf0, perfect (default lf0)")
    p.add_argument("--tv-sensitivity", default="0.12,0.15,0.25", help="planner constants, '' off")
    p.add_argument("--no-trailing-mean", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("reanalysis", help="the pre-registered strategies A, B and C of section 5")
    runner(p)
    p.add_argument("--a-constants", default="0.12,0.15,0.21,0.25")
    p.set_defaults(func=cmd_reanalysis)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
