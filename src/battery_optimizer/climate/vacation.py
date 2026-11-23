"""Vacation mode v1 (ADR-0010): the owner's absence window and what it does to a decision.

Pure functions, no I/O.  The owner sets two Home Assistant helpers, **Leave** and **Return**
(``input_datetime``, date and time, local time); the optimizer only reads them.  With
``ready_at = end - ready_margin``:

* slots whose **end** lies in ``[start, ready_at)`` carry no comfort minimum, only the building
  guard ``guard_c`` (:func:`apply_minimum`);
* slots whose end lies in ``[ready_at, end)`` carry the normal schedule's minimum **at** ``end``
  (the return comfort, reached ``ready_margin`` early);
* from ``end`` the normal schedule.

A decision whose ``ready_at`` lies within ``lookahead_hours`` plans to ``end + RETURN_TAIL_HOURS``
(:func:`horizon_hours`, capped at :data:`MAX_HORIZON_HOURS`), and a horizon that ends inside the
absence has no terminal stored-heat term (:func:`terminal_off`).

Invalid, missing or unavailable helpers mean **no vacation** (the normal schedule): the failure
mode is heating an empty house, never a cold return.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

__all__ = [
    "INVALID_REASONS",
    "MAX_HORIZON_HOURS",
    "PHASES",
    "RETURN_TAIL_HOURS",
    "VacationRead",
    "VacationSettings",
    "VacationWindow",
    "apply_minimum",
    "hold_last",
    "horizon_hours",
    "parse_helper",
    "phase",
    "read_window",
    "recovery_start",
    "terminal_off",
    "view",
    "window_from_view",
]

#: The phases of the contract (``inputs.vacation.phase``, the summary's ``vacation.phase``).
PHASES: tuple[str, ...] = ("none", "scheduled", "away", "recovery", "ready")
#: Why both helpers hold a value and the window is still rejected (the ``vacation_invalid`` metric's
#: label set).  ``helpers_unavailable`` (a helper missing or unavailable) is reported in the view
#: only: it is not an owner mistake.  An empty window (cancel), or one already over, is no reason.
INVALID_REASONS: tuple[str, ...] = ("unparseable", "end_before_start", "too_long")
#: A horizon that covers the return runs this long past ``end``.
RETURN_TAIL_HOURS = 6
#: The longest horizon (``config/climate-mpc.yaml`` accepts at most 72 h).
MAX_HORIZON_HOURS = 72

_STEP = timedelta(minutes=15)


@dataclass(frozen=True)
class VacationSettings:
    """``climate_shadow.vacation`` of the service config (ADR-0010)."""

    enabled: bool = True
    start_entity: str = "input_datetime.climate_vacation_start"
    end_entity: str = "input_datetime.climate_vacation_end"
    #: Building guard (degC): the only minimum while the house is empty.  Not a comfort setting.
    guard_c: float = 12.0
    #: The rooms reach the return comfort this long before ``end``.
    ready_margin_minutes: int = 120
    #: A decision plans to the return once ``ready_at`` is this close.
    lookahead_hours: int = 48
    #: A longer window is invalid (a typo in a year, not a vacation).
    max_days: int = 60

    def entities(self) -> tuple[str, ...]:
        return (self.start_entity, self.end_entity) if self.enabled else ()


@dataclass(frozen=True)
class VacationWindow:
    """A valid window, UTC."""

    start: datetime
    end: datetime
    ready_at: datetime

    def key(self) -> tuple[str, str]:
        return (_iso(self.start), _iso(self.end))

    def absent(self, at: datetime) -> bool:
        """Inside ``[start, ready_at)``: nobody needs comfort."""
        return self.start <= at < self.ready_at

    def to_dict(self) -> dict[str, str]:
        return {"start": _iso(self.start), "end": _iso(self.end), "ready_at": _iso(self.ready_at)}


@dataclass(frozen=True)
class VacationRead:
    """What the helpers say at one instant: a valid, not yet ended window, or why there is none
    (``invalid_reason`` ``None``: nothing set, cancelled or already over)."""

    window: VacationWindow | None
    invalid_reason: str | None = None
    #: A rejected window's helper values (UTC), echoed so the owner sees what was rejected.
    rejected_start: datetime | None = None
    rejected_end: datetime | None = None


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def parse_helper(state: str | None, tz: ZoneInfo) -> datetime | None:
    """An ``input_datetime`` state (``YYYY-MM-DD HH:MM:SS``, or a date alone: local midnight) as
    an aware UTC instant; ``None`` for anything else."""
    if state is None:
        return None
    text = state.strip()
    if not text or text.lower() in ("unknown", "unavailable", "none"):
        return None
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=tz)
    return at.astimezone(UTC)


def read_window(
    start_state: str | None,
    end_state: str | None,
    *,
    tz: ZoneInfo,
    now: datetime,
    settings: VacationSettings,
) -> VacationRead:
    """The window the two helper states describe at *now* (``None`` state: the helper is missing
    or unavailable).  Valid when both parse, ``end > start``, ``end > now`` and the window is at
    most ``max_days`` long; ``start == end`` is the owner's cancel (no window, no reason)."""
    if not settings.enabled:
        return VacationRead(None, None)
    if start_state is None and end_state is None:
        return VacationRead(None, "helpers_unavailable")
    start, end = parse_helper(start_state, tz), parse_helper(end_state, tz)
    if start is None or end is None:
        return VacationRead(None, "unparseable")
    if end == start:
        return VacationRead(None, None)
    if end < start:
        return VacationRead(None, "end_before_start", start, end)
    if end - start > timedelta(days=settings.max_days):
        return VacationRead(None, "too_long", start, end)
    if end <= now:
        return VacationRead(None, None)
    ready_at = max(start, end - timedelta(minutes=settings.ready_margin_minutes))
    return VacationRead(VacationWindow(start, end, ready_at), None)


def hold_last(read: VacationRead, last: VacationWindow | None, now: datetime) -> VacationRead:
    """*read*, or the *last* valid window while the helpers are unavailable (a restarting Home
    Assistant must not end a vacation for one decision) and that window is not over."""
    if read.invalid_reason == "helpers_unavailable" and last is not None and last.end > now:
        return VacationRead(last, None)
    return read


def horizon_hours(
    window: VacationWindow | None, t0: datetime, base_hours: int, settings: VacationSettings
) -> int:
    """The decision's horizon: *base_hours*, or up to ``end + RETURN_TAIL_HOURS`` (whole hours,
    capped at :data:`MAX_HORIZON_HOURS`) once ``ready_at`` is within ``lookahead_hours``."""
    if window is None or window.end <= t0:
        return base_hours
    if window.ready_at - t0 > timedelta(hours=settings.lookahead_hours):
        return base_hours
    need = (window.end + timedelta(hours=RETURN_TAIL_HOURS) - t0).total_seconds() / 3600.0
    return int(min(MAX_HORIZON_HOURS, max(base_hours, math.ceil(need))))


def terminal_off(window: VacationWindow | None, t0: datetime, hours: int) -> bool:
    """A horizon that ends inside ``[start, ready_at)`` keeps no stored heat: coasting is the
    point of the absence."""
    if window is None:
        return False
    return window.absent(t0 + timedelta(hours=hours))


def apply_minimum(
    minimum: np.ndarray,
    slot_ends: list[datetime],
    window: VacationWindow | None,
    return_minimum: np.ndarray,
    guard_c: float,
) -> np.ndarray:
    """``(rooms, T)`` comfort minimum with the vacation applied (see the module).  *slot_ends* are
    the slots' end instants (the minimum is compared with the end-of-slot state);
    *return_minimum* is the normal schedule's ``(rooms,)`` minimum at ``end``.  A slot whose
    normal minimum is unconstrained (``NaN``) stays so after ``ready_at``."""
    out = np.array(minimum, dtype=float, copy=True)
    if window is None:
        return out
    for i, te in enumerate(slot_ends):
        if window.start <= te < window.ready_at:
            out[:, i] = guard_c
        elif window.ready_at <= te < window.end:
            out[:, i] = return_minimum
    return out


def recovery_start(
    window: VacationWindow | None, slot_start: datetime, heating: list[int]
) -> datetime | None:
    """The first planned heating slot (start instant) inside ``[start, ready_at)``: v1's warm-up
    start.  ``None`` when the plan heats nowhere in the absence within its horizon."""
    if window is None:
        return None
    for i, h in enumerate(heating):
        at = slot_start + i * _STEP
        if at >= window.ready_at:
            break
        if h and at >= window.start:
            return at
    return None


def phase(window: VacationWindow | None, now: datetime, recovery: datetime | None) -> str:
    """``none``, ``scheduled``, ``away``, ``recovery`` or ``ready`` at *now*."""
    if window is None or now >= window.end:
        return "none"
    if now < window.start:
        return "scheduled"
    if now < window.ready_at:
        return "recovery" if recovery is not None and now >= recovery else "away"
    return "ready"


def view(
    read: VacationRead,
    now: datetime,
    *,
    guard_c: float,
    recovery: datetime | None = None,
) -> dict[str, Any]:
    """The contract's vacation object (``inputs.vacation``, the summary's ``vacation``)."""
    w = read.window
    start = w.start if w else read.rejected_start
    end = w.end if w else read.rejected_end
    return {
        "phase": phase(w, now, recovery),
        "start": _iso(start) if start is not None else None,
        "end": _iso(end) if end is not None else None,
        "ready_at": _iso(w.ready_at) if w else None,
        "recovery_start": _iso(recovery) if w and recovery is not None else None,
        "guard_c": float(guard_c),
        "invalid_reason": read.invalid_reason,
    }


def window_from_view(obj: Mapping[str, Any] | None) -> VacationWindow | None:
    """The valid window a stored ``inputs.vacation`` object names (``None`` without one; a
    rejected window echoes ``start``/``end`` but has no ``ready_at``)."""
    if not isinstance(obj, Mapping) or not all(obj.get(k) for k in ("start", "end", "ready_at")):
        return None
    try:
        start = datetime.fromisoformat(str(obj["start"]))
        end = datetime.fromisoformat(str(obj["end"]))
        ready = datetime.fromisoformat(str(obj["ready_at"]))
    except ValueError:
        return None
    return VacationWindow(start, end, ready)
