"""Spike guard for the live room-temperature inputs of the climate shadow.  Read-only.

A thermostat next to the cooker reads the room plus the cooking: the hall-kitchen one rose 5.2 K in
25 minutes on 2026-11-19 (24.3 to 29.5 degC) and fell back within an hour, nearly every morning of
the season.  The observer and the MPC read that as a warm room.  This module replaces such an
excursion of a thermostat series by the level the room had before it.

**Rule** (per room, on the raw samples, a pure function of the samples up to the decision):

* a sample is a *trigger* when it is more than ``max_rise_k`` above the lowest raw sample of the
  trailing ``window_minutes`` (a rise a heat pump cannot deliver to a room: 2 K in 30 minutes,
  while in four weeks no other thermostat rose more than 1.6 K in 30 minutes, bar one window
  recovery of 2.3 K, see below);
* the excursion starts at that lowest sample, whose value is the *base*.  Every sample from then
  on that is more than ``release_k`` above the base is replaced by the base, until a sample is back
  within ``release_k`` of it or ``max_hold_minutes`` have passed since the start of the excursion
  (a genuine step, such as a window opened to the sun, is then believed after all);
* a rise back to a level the room had within the last ``memory_minutes`` (plus ``release_k``) is
  never a trigger: the recovery after a window was open is not a spike (bedroom, 2026-10-26: 19.5
  degC back to 22.9 in 85 minutes).

The guarded series keeps every timestamp, so the slot builder sees no gap.  It is applied to the
decision's inputs only: scoring, the weekly refit and the day records read the raw measurement,
which the preregistered gates are defined on.  Nothing here does I/O.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

__all__ = ["SpikeRule", "audit", "despike", "guard_series"]

_MIN_US = 60 * 1_000_000


@dataclass(frozen=True)
class SpikeRule:
    """The guard's parameters (``climate_shadow.room_spike_guard`` of the service config)."""

    enabled: bool = True
    max_rise_k: float = 2.0
    window_minutes: int = 30
    release_k: float = 1.0
    max_hold_minutes: int = 120
    memory_minutes: int = 360

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_rise_k": self.max_rise_k,
            "window_minutes": self.window_minutes,
            "release_k": self.release_k,
            "max_hold_minutes": self.max_hold_minutes,
            "memory_minutes": self.memory_minutes,
        }


def despike(t_us: np.ndarray, values: np.ndarray, rule: SpikeRule) -> tuple[np.ndarray, np.ndarray]:
    """``(values, flagged)``: *values* with every sample of a spike excursion replaced by its
    base level, and the mask of the replaced samples.  *t_us* is increasing."""
    t = np.asarray(t_us, dtype=np.int64)
    v = np.asarray(values, dtype=np.float64)
    out = v.copy()
    flagged = np.zeros(v.size, dtype=bool)
    if not rule.enabled or v.size < 2:
        return out, flagged
    window = rule.window_minutes * _MIN_US
    hold = rule.max_hold_minutes * _MIN_US
    n, lo, i = v.size, 0, 0
    while i < n:
        while t[lo] < t[i] - window:
            lo += 1
        j = lo + int(np.argmin(v[lo : i + 1]))
        if v[i] - v[j] <= rule.max_rise_k or v[i] <= _recent_max(t, out, j, rule) + rule.release_k:
            i += 1
            continue
        base = v[j]
        ceiling = base + rule.release_k
        k = i + 1  # the excursion ends at the first sample back within release_k, or at the hold
        while k < n and t[k] - t[j] <= hold and v[k] > ceiling:
            k += 1
        seg = v[j + 1 : k] > ceiling  # the part of the rise already read is included
        flagged[j + 1 : k] = seg
        out[j + 1 : k][seg] = base
        i = k
    return out, flagged


def _recent_max(t: np.ndarray, out: np.ndarray, j: int, rule: SpikeRule) -> float:
    """Highest guarded value in the ``memory_minutes`` before sample *j* (``-inf`` if none)."""
    first = int(np.searchsorted(t, t[j] - rule.memory_minutes * _MIN_US, side="left"))
    return float(out[first:j].max()) if j > first else -math.inf


def guard_series(
    series: Mapping[str, Any], sources: Mapping[str, str], rule: SpikeRule
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """*series* with the room series named by *sources* (room to series key) guarded, and
    ``room -> flagged mask`` for the audit.  Other series pass through untouched."""
    out = dict(series)
    found: dict[str, np.ndarray] = {}
    for room, key in sources.items():
        raw = series.get(key)
        if raw is None or raw.t_us.size == 0:
            continue
        values, flagged = despike(raw.t_us, raw.values, rule)
        found[room] = flagged
        if flagged.any():
            out[key] = type(raw)(raw.entity, raw.metric, raw.unit, raw.t_us, values)
    return out, found


def audit(
    series: Mapping[str, Any],
    sources: Mapping[str, str],
    rule: SpikeRule,
    guarded: Mapping[str, Any],
    flags: Mapping[str, np.ndarray],
    at: datetime,
) -> dict[str, Any]:
    """The decision record's ``inputs.room_guard``: the rule and, per room, how many samples of
    the last 24 h were replaced, whether the newest sample is, and its raw and used value."""
    since = int((at - timedelta(hours=24)).timestamp() * 1e6)
    rooms: dict[str, Any] = {}
    for room, key in sources.items():
        raw, used = series.get(key), guarded.get(key)
        flagged = flags.get(room)
        if raw is None or used is None or flagged is None or raw.t_us.size == 0:
            continue
        recent = raw.t_us >= since
        rooms[room] = {
            "replaced_24h": int(flagged[recent].sum()),
            "active": bool(flagged[-1]),
            "raw_last": round(float(raw.values[-1]), 2),
            "used_last": round(float(used.values[-1]), 2),
            "max_raw_minus_used_24h": round(
                float(np.max(raw.values[recent] - used.values[recent])) if recent.any() else 0.0, 2
            ),
        }
    return {"enabled": True, **rule.as_dict(), "rooms": rooms}
