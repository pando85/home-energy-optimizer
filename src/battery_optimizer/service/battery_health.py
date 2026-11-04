"""Read-only battery health telemetry: temperature exposure, charge sessions, cell balance.

A pure state machine fed with observed entity states and the instant they were observed; it
does no I/O, reads no clock and writes nothing anywhere.  The runtime feeds it from the entity
store (the existing GET / ``state_changed`` path) and exposes :meth:`BatteryHealth.snapshot`
in ``/status`` and as Prometheus metrics.  It informs; it never decides (the periodic full
charge scheduler reads the same fields later).

* **Temperature.**  Seconds *strictly above* each threshold (35 and 40 degC by default) are
  integrated by sample-and-hold: the last temperature is held until the next observation, an
  advance of the clock (a scrape, a ``/status`` call, the runtime's periodic tick) or the
  entity turning unavailable.  Home Assistant reports a sensor only when it changes, so a
  steady temperature is silent for hours; the periodic advance is what counts that time.  One
  advance credits at most ``max_gap_s``; the rest is not counted and is reported in
  ``uncounted_s`` (the process or the event loop was stalled, a gap the data cannot vouch
  for).  While the temperature is ``unknown``/``unavailable`` nothing is counted.
* **Charge sessions.**  A session starts when the sign-corrected battery AC power (positive =
  charging) exceeds ``charge_threshold_w`` and ends ``session_end_hold_s`` after it last
  fell to the threshold or below (the end is dated to the first sample below), so the taper
  at the top of a charge is one session.  An unavailable power reading counts as below.
  Peak temperature is the highest temperature observed during the session, including the one
  held when it started.
* **Cells.**  Maximum and minimum cell voltage, and their spread (``None`` unless both are
  known and consistent: a minimum above the maximum means the reads were not simultaneous).
* **Balance monitor.**  Omnibattery's last comparable settled top-of-charge delta, its status,
  trend and four-reading average, the balancing flag and the age of the last valid read.

Unknown is never zero: an unavailable, unknown or non-numeric state is ``None`` everywhere,
in ``/status`` and in the metrics (a missing sample, not a 0).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .entity_store import UNAVAILABLE_STATES

__all__ = [
    "BALANCE_STATUSES",
    "HEALTH_ROLES",
    "BatteryHealth",
    "ChargeSession",
    "HealthParams",
]

#: Roles :meth:`BatteryHealth.observe` understands.  ``charge_power`` is the battery AC power.
HEALTH_ROLES: tuple[str, ...] = (
    "temperature",
    "charge_power",
    "cell_voltage_max",
    "cell_voltage_min",
    "cell_delta",
    "balance_status",
    "last_balance_read",
    "delta_trend",
    "delta_avg_4w",
    "balancing_mode",
)

#: Statuses of ``sensor.*_balance_status`` (Omnibattery); anything else reads ``unknown``.
BALANCE_STATUSES: tuple[str, ...] = ("green", "yellow", "orange", "red", "unknown")

#: Completed sessions kept in memory (the Prometheus counters keep the totals).
_SESSION_MEMORY = 20


@dataclass(frozen=True)
class HealthParams:
    """Thresholds of the tracker (service config ``battery_health``)."""

    hot_thresholds_c: tuple[float, ...] = (35.0, 40.0)
    #: Positive sign-corrected AC power above which the battery counts as charging (W).
    charge_threshold_w: float = 100.0
    #: +1 when the AC power entity is positive while charging, -1 when negative.
    charge_power_sign: int = 1
    #: Seconds the power must stay at or below the threshold before a session is over.
    session_end_hold_s: float = 300.0
    #: Longest span one advance of the clock may credit to the held temperature.
    max_gap_s: float = 300.0


@dataclass
class ChargeSession:
    started_at: datetime
    peak_temperature_c: float | None = None
    ended_at: datetime | None = None
    #: Why it ended: ``below_threshold`` or ``unavailable`` (``None`` while it runs).
    end_reason: str | None = None

    def duration_s(self, now: datetime) -> float:
        return max(0.0, ((self.ended_at or now) - self.started_at).total_seconds())

    def to_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_s": round(self.duration_s(now), 1),
            "peak_temperature_c": self.peak_temperature_c,
            "end_reason": self.end_reason,
        }


def parse_number(state: str | None) -> float | None:
    """A finite float, or ``None`` for a missing, unavailable, unknown or non-numeric state."""
    if state is None or state.strip().lower() in UNAVAILABLE_STATES:
        return None
    try:
        value = float(state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_text(state: str | None) -> str | None:
    """The state text, or ``None`` when missing, unavailable or unknown."""
    if state is None or state.strip().lower() in UNAVAILABLE_STATES:
        return None
    return state.strip()


def parse_timestamp(state: str | None) -> datetime | None:
    """An aware ISO-8601 instant in UTC, or ``None`` (missing, unavailable, naive, garbled)."""
    text = parse_text(state)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


@dataclass
class BatteryHealth:
    """The telemetry state; see the module docstring.  Not thread-safe (event loop only)."""

    params: HealthParams = field(default_factory=HealthParams)
    #: Seconds above each threshold since the tracker was created (cumulative).
    hot_seconds: dict[float, float] = field(init=False)
    #: Seconds a stalled advance did not credit (cumulative).
    uncounted_s: float = field(default=0.0, init=False)
    sessions_completed: int = field(default=0, init=False)
    sessions: deque[ChargeSession] = field(
        default_factory=lambda: deque(maxlen=_SESSION_MEMORY), init=False
    )

    def __post_init__(self) -> None:
        self.hot_seconds = {t: 0.0 for t in self.params.hot_thresholds_c}
        self._temperature: float | None = None
        self._clock: datetime | None = None  # the instant everything is integrated up to
        self._session: ChargeSession | None = None
        self._below_since: datetime | None = None  # first sample below the threshold
        self._voltage_max: float | None = None
        self._voltage_min: float | None = None
        self._delta: float | None = None
        self._status: str | None = None
        self._status_seen = False
        self._last_read: datetime | None = None
        self._trend: str | None = None
        self._delta_avg: float | None = None
        self._balancing: bool | None = None

    # ------------------------------------------------------------------ feeding

    def advance(self, now: datetime) -> None:
        """Integrate the held temperature up to *now* and close a session whose hold expired."""
        last = self._clock
        if last is None or now <= last:
            if last is None:
                self._clock = now
            return
        elapsed = (now - last).total_seconds()
        if self._temperature is not None:
            credited = min(elapsed, self.params.max_gap_s)
            for threshold in self.hot_seconds:
                if self._temperature > threshold:
                    self.hot_seconds[threshold] += credited
            self.uncounted_s += elapsed - credited
        self._clock = now
        if (
            self._session is not None
            and self._below_since is not None
            and (now - self._below_since).total_seconds() >= self.params.session_end_hold_s
        ):
            self._end_session(self._below_since, "below_threshold")

    def observe(self, role: str, state: str | None, at: datetime) -> None:
        """Feed one observation: *state* is the HA state string (``None`` = missing)."""
        if role not in HEALTH_ROLES:
            raise KeyError(f"unknown battery health role {role!r}")
        self.advance(at)
        if role == "temperature":
            self._temperature = parse_number(state)
            if self._session is not None and self._temperature is not None:
                self._note_temperature(self._session)
        elif role == "charge_power":
            self._observe_power(parse_number(state), at)
        elif role == "cell_voltage_max":
            self._voltage_max = parse_number(state)
        elif role == "cell_voltage_min":
            self._voltage_min = parse_number(state)
        elif role == "cell_delta":
            self._delta = parse_number(state)
        elif role == "balance_status":
            text = parse_text(state)
            self._status_seen = True
            self._status = text.lower() if text and text.lower() in BALANCE_STATUSES else "unknown"
        elif role == "last_balance_read":
            self._last_read = parse_timestamp(state)
        elif role == "delta_trend":
            self._trend = parse_text(state)
        elif role == "delta_avg_4w":
            self._delta_avg = parse_number(state)
        elif role == "balancing_mode":
            text = parse_text(state)
            self._balancing = None if text is None else {"on": True, "off": False}.get(text.lower())

    def _observe_power(self, raw_w: float | None, at: datetime) -> None:
        power = None if raw_w is None else raw_w * self.params.charge_power_sign
        if power is not None and power > self.params.charge_threshold_w:
            self._below_since = None
            if self._session is None:
                self._session = ChargeSession(started_at=at)
                self._note_temperature(self._session)
            return
        if self._session is None:
            return
        if power is None:
            self._end_session(at, "unavailable")
        elif self._below_since is None:
            self._below_since = at

    def _note_temperature(self, session: ChargeSession) -> None:
        if self._temperature is not None and (
            session.peak_temperature_c is None or self._temperature > session.peak_temperature_c
        ):
            session.peak_temperature_c = self._temperature

    def _end_session(self, at: datetime, reason: str) -> None:
        session = self._session
        if session is None:
            return
        session.ended_at = max(at, session.started_at)
        session.end_reason = reason
        self.sessions.append(session)
        self.sessions_completed += 1
        self._session = None
        self._below_since = None

    # ------------------------------------------------------------------ reading

    @property
    def temperature_c(self) -> float | None:
        return self._temperature

    @property
    def spread_mv(self) -> float | None:
        high, low = self._voltage_max, self._voltage_min
        if high is None or low is None or low > high:
            return None
        return round((high - low) * 1000.0, 3)

    def snapshot(self, now: datetime) -> dict[str, Any]:
        """The ``/status`` block and the source of the metrics (advances the clock to *now*)."""
        self.advance(now)
        last_read_age = (
            None if self._last_read is None else max(0.0, (now - self._last_read).total_seconds())
        )
        session = self._session
        last = self.sessions[-1] if self.sessions else None
        return {
            "temperature_c": self._temperature,
            "hot_seconds": {f"{t:g}": round(s, 1) for t, s in self.hot_seconds.items()},
            "uncounted_s": round(self.uncounted_s, 1),
            "charge_session": {
                "active": session is not None,
                "current": session.to_dict(now) if session else None,
                "last": last.to_dict(now) if last else None,
                "completed": self.sessions_completed,
            },
            "cell_voltage": {
                "max_v": self._voltage_max,
                "min_v": self._voltage_min,
                "spread_mv": self.spread_mv,
            },
            "balance": {
                "cell_delta_mv": self._delta,
                "status": self._status if self._status_seen else None,
                "delta_trend": self._trend,
                "delta_avg_4w_mv": self._delta_avg,
                "balancing_mode": self._balancing,
                "last_read_at": self._last_read.isoformat() if self._last_read else None,
                "last_read_age_s": None if last_read_age is None else round(last_read_age, 1),
            },
        }
