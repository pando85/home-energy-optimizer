"""Append-only shadow store under ``<data_dir>/service/v1/``.

Layout (partitioned by **UTC** day, so a DST day is never split or doubled)::

    service/v1/plans/date=YYYY-MM-DD/<plan_id>.json    one immutable file per plan
    service/v1/slots/date=YYYY-MM-DD/slots.jsonl       one row per closed slot
    service/v1/events/date=YYYY-MM-DD/events.jsonl     solve / readiness / connection events
    service/v1/pvpc_snapshots/date=YYYY-MM-DD/pvpc_snapshots.jsonl
                                                       one row per PVPC sensor state change
    service/v1/weather_forecasts/date=YYYY-MM-DD/weather_forecasts.jsonl
                                                       one row per HA weather forecast fetch
    service/v1/climate_<x>/date=YYYY-MM-DD/climate_<x>.jsonl  (x: decisions, scores, days, models)
                                                       the climate shadow (track B2): decisions,
                                                       scores, days and model refits
    service/v1/actuation/date=YYYY-MM-DD/actuation.jsonl  dry_run / live only: one row per
                                                       actuator tick that wrote (or would
                                                       write) or changed state (ADR-0007)
    service/v1/state/virtual_battery.json              the virtual battery's SOC (mutable)

Plans are written with :func:`battery_optimizer.paths.write_part_atomic` (temp
file, fsync, no-clobber hard link) — the store convention of the offline pipeline.
Slot, event and archive rows (PVPC snapshots, weather forecasts) are appended as one
``O_APPEND`` write of one complete line, then fsynced; readers skip a torn last line
rather than trusting it.  Files are
0600 and directories 0700.  Nothing here ever rewrites or deletes a record.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from battery_optimizer.paths import _ensure_private_dir, write_part_atomic

__all__ = ["CLIMATE_KINDS", "SCHEMA_VERSION", "ShadowSink", "iter_records"]

SCHEMA_VERSION = 1
_ROOT = Path("service") / "v1"
#: Append-only record kinds of the climate shadow (:mod:`.climate_shadow`).
CLIMATE_KINDS = ("climate_decisions", "climate_scores", "climate_days", "climate_models")


def _utc_day(at: datetime) -> str:
    return at.astimezone(UTC).date().isoformat()


def _dumps(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ShadowSink:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / _ROOT
        self.write_errors = 0

    def _dir(self, kind: str, day: str) -> Path:
        return self.root / kind / f"date={day}"

    def _append(self, kind: str, at: datetime, record: dict[str, Any]) -> Path:
        directory = self._dir(kind, _utc_day(at))
        _ensure_private_dir(directory)
        path = directory / f"{kind}.jsonl"
        line = (_dumps({"schema_version": SCHEMA_VERSION, **record}) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(f"short append to {path}: {written}/{len(line)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        return path

    def write_plan(self, record: dict[str, Any]) -> Path:
        generated = datetime.fromisoformat(record["generated_at"])
        path = self._dir("plans", _utc_day(generated)) / f"{record['plan_id']}.json"
        data = (_dumps({"schema_version": SCHEMA_VERSION, **record}) + "\n").encode()
        write_part_atomic(path, data)
        return path

    def append_slot(self, slot_start: datetime, record: dict[str, Any]) -> Path:
        return self._append("slots", slot_start, record)

    def append_event(self, at: datetime, record: dict[str, Any]) -> Path:
        return self._append("events", at, record)

    def append_pvpc_snapshot(self, at: datetime, record: dict[str, Any]) -> Path:
        return self._append("pvpc_snapshots", at, record)

    def append_weather_forecast(self, at: datetime, record: dict[str, Any]) -> Path:
        return self._append("weather_forecasts", at, record)

    def append_actuation(self, at: datetime, record: dict[str, Any]) -> Path:
        return self._append("actuation", at, record)

    def append_climate(self, kind: str, at: datetime, record: dict[str, Any]) -> Path:
        if kind not in CLIMATE_KINDS:
            raise ValueError(f"unknown climate record kind {kind!r}")
        return self._append(kind, at, record)

    # -- mutable state -------------------------------------------------------------

    def _state_path(self) -> Path:
        return self.root / "state" / "virtual_battery.json"

    def save_virtual_battery(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        _ensure_private_dir(path.parent)
        fd, tmp = tempfile.mkstemp(prefix=".vb.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(_dumps(state))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def load_virtual_battery(self) -> dict[str, Any] | None:
        path = self._state_path()
        try:
            return json.loads(path.read_text())
        except FileNotFoundError, ValueError:
            return None


def iter_records(
    data_dir: Path, kind: str, since: datetime, until: datetime
) -> Iterator[dict[str, Any]]:
    """Records of *kind* (``slots``/``events``/``plans``/``pvpc_snapshots``/
    ``weather_forecasts``/``climate_*``/``actuation``) from the UTC-day partitions
    overlapping ``[since, until)``; torn or corrupt lines are skipped."""
    root = data_dir / _ROOT / kind
    day = since.astimezone(UTC).date() - timedelta(days=1)
    last: date = until.astimezone(UTC).date()
    while day <= last:
        directory = root / f"date={day.isoformat()}"
        if directory.is_dir():
            files = sorted(directory.glob("*.jsonl" if kind != "plans" else "*.json"))
            for path in files:
                with path.open() as handle:
                    for line in handle:
                        if not line.endswith("\n"):
                            continue  # torn tail of an interrupted append
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue
        day += timedelta(days=1)
