"""Shared plumbing of the load-forecast analysis scripts in ``hack/`` (read-only).

``load_forecast_value_replay.py``, ``load_forecast_candidates.py`` and ``load_nilm_probe.py`` read
the long-term Prometheus archive on a fixed time grid, keep the fetched series in the private data
dir and print their summary to stdout.  This module holds what they share and nothing else:

* :func:`fetch_grid`: one ``query_range`` series on a step grid.  The package's own extraction
  deliberately avoids ``query_range`` (it fabricates samples on its step grid, see
  ``battery_optimizer.extract_prom``); these analyses are defined on that grid (the experiments of
  ``docs/planning/load-forecaster-improvement-plan.md`` section 7 were run this way), so they use it
  and say so.  The base URL comes from ``BATTERY_OPTIMIZER_PROMETHEUS_URL`` through
  ``extract_prom.resolve_base_url``; nothing about an installation is hard-coded here.
* :class:`RunDir`: the ``<data_dir>/analysis/v1/<tool>/<run-id>/`` directory (outside the
  repository, enforced by ``resolve_data_dir``) with a fetch cache and the JSON report.
* small local-time and error-statistics helpers on plain numpy arrays (no pandas: it is not a
  dependency of the project).
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "src"))

from battery_optimizer.extract_prom import resolve_base_url  # noqa: E402
from battery_optimizer.paths import resolve_data_dir, write_part_atomic  # noqa: E402

MAX_POINTS = 10_000
"""Points per ``query_range`` request (Prometheus refuses more than 11 000)."""

LOCAL_TZ = "Europe/Madrid"

#: Hour-of-day buckets of the load-forecast studies (local hours, inclusive edges).
BUCKET_EDGES = (0, 6, 8, 14, 18, 22, 24)
BUCKET_LABELS = ("00-06", "06-08", "08-14", "14-18", "18-22", "22-24")


def run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or ".." in value:
        raise ValueError(f"{value!r}: use only [A-Za-z0-9._-] and no '..'")
    return value


def utc_day(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)


def f(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None or not np.isfinite(value) else f"{value:.{digits}f}"


def pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None or not np.isfinite(value) else f"{value:+.{digits}f}%"


class RunDir:
    """``<data_dir>/analysis/v1/<tool>/<run-id>/``: fetch cache and report, never the repository."""

    def __init__(self, tool: str, run: str | None, data_dir: str | None) -> None:
        self.root = resolve_data_dir(data_dir)
        self.path = None if run is None else self.root / "analysis" / "v1" / tool / run_id(run)

    def cached(
        self, name: str, fetch: Callable[[], dict[str, np.ndarray]]
    ) -> dict[str, np.ndarray]:
        """Arrays of *fetch*, kept as ``<name>.npz`` when there is a run directory."""
        target = None if self.path is None else self.path / f"{name}.npz"
        if target is not None and target.exists():
            with np.load(target) as z:
                return {k: z[k] for k in z.files}
        arrays = fetch()
        if target is not None:
            buffer = io.BytesIO()
            np.savez(buffer, **arrays)
            write_part_atomic(target, buffer.getvalue())
        return arrays

    def write(self, name: str, data: bytes) -> None:
        """Write a result file of the run (replacing the one of an earlier run with this id)."""
        if self.path is None:
            return
        (self.path / name).unlink(missing_ok=True)
        write_part_atomic(self.path / name, data)
        print(f"written to {self.path / name}", file=sys.stderr)

    def write_json(self, name: str, report: dict[str, Any]) -> None:
        text = json.dumps(report, indent=2, sort_keys=True, default=float) + "\n"
        self.write(name, text.encode("utf-8"))


class Prometheus:
    """Minimal read-only ``query_range`` client (GET only) with bounded retries."""

    def __init__(self, base_url: str | None = None, timeout_s: float = 180.0) -> None:
        self.base_url = resolve_base_url(base_url)
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0))

    def close(self) -> None:
        self._client.close()

    def _get(self, expr: str, start: float, end: float, step: int) -> list[dict[str, Any]]:
        params = {"query": expr, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": str(step)}
        last: Exception | None = None
        for attempt in range(4):
            if attempt:
                time.sleep(min(2.0**attempt, 20.0))
            try:
                response = self._client.get(f"{self.base_url}/api/v1/query_range", params=params)
            except httpx.TransportError as exc:
                last = exc
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last = RuntimeError(f"HTTP {response.status_code}")
                continue
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                raise RuntimeError(f"prometheus query failed ({expr!r}): {payload.get('error')}")
            return payload["data"]["result"]
        raise RuntimeError(f"gave up querying prometheus ({expr!r}): {last}")

    def grid(
        self, expr: str, start: datetime, end: datetime, step: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """The samples of *expr* at ``start + k * step < end``, as (epoch seconds, values).

        The request is split so that no request exceeds :data:`MAX_POINTS`.  When several series
        match, the last one returned for a timestamp wins: pass an aggregation (``max``/``sum``).
        """
        span = step * MAX_POINTS
        out: dict[int, float] = {}
        t = start.timestamp()
        stop = end.timestamp()
        while t < stop:
            chunk_end = min(t + span, stop)
            for series in self._get(expr, t, chunk_end - 1, step):
                for ts, value in series["values"]:
                    out[int(float(ts))] = float(value)
            t = chunk_end
        keys = np.array(sorted(out), dtype=np.int64)
        return keys, np.array([out[int(k)] for k in keys], dtype=float)


def on_grid(
    series: tuple[np.ndarray, np.ndarray], t0: int, step: int, n: int, *, shift: int = 0
) -> np.ndarray:
    """Put (timestamps, values) on ``t0 + k * step`` (NaN where absent).

    *shift* seconds are subtracted from the timestamps first: ``shift=step`` turns a value taken
    at a slot's end into the slot's start.
    """
    ts, values = series
    out = np.full(n, np.nan)
    k, rem = np.divmod(ts - shift - t0, step)
    ok = (rem == 0) & (k >= 0) & (k < n)
    out[k[ok]] = values[ok]
    return out


def to_arrays(series: tuple[np.ndarray, np.ndarray]) -> dict[str, np.ndarray]:
    return {"ts": series[0], "v": series[1]}


def from_arrays(arrays: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    return arrays["ts"], arrays["v"]


def local_fields(
    epoch: np.ndarray, tz_name: str = LOCAL_TZ
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per timestamp: local date ordinal, local hour and local minute."""
    tz = ZoneInfo(tz_name)
    ordinal = np.empty(epoch.size, dtype=np.int64)
    hour = np.empty(epoch.size, dtype=np.int64)
    minute = np.empty(epoch.size, dtype=np.int64)
    for i, ts in enumerate(epoch):
        t = datetime.fromtimestamp(int(ts), tz)
        ordinal[i] = t.toordinal()
        hour[i] = t.hour
        minute[i] = t.minute
    return ordinal, hour, minute


def day_bounds(ordinals: np.ndarray) -> list[tuple[int, int, int]]:
    """``(label, start, stop)`` per run of equal day ordinals (the forecasters' partition)."""
    bounds: list[tuple[int, int, int]] = []
    start = 0
    for i in range(1, ordinals.size + 1):
        if i == ordinals.size or ordinals[i] != ordinals[start]:
            bounds.append((int(ordinals[start]), start, i))
            start = i
    return bounds


def bucket_index(hour: np.ndarray) -> np.ndarray:
    """Index into :data:`BUCKET_LABELS` of each local hour."""
    return np.searchsorted(np.array(BUCKET_EDGES[1:]), hour, side="right")


def local_midnight_epoch(day: date, tz_name: str = LOCAL_TZ) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(tz_name)).timestamp())


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M")


def day_add(day: datetime, days: int) -> datetime:
    return day + timedelta(days=days)
