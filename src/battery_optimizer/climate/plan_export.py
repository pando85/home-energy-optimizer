"""The advisory ClimatePlan (contract v1): the live shadow's decision, shaped for a consumer.

The optimizer **only produces** a plan; it never acts on it and never writes anywhere
(ADR-0008, proposed).  This module is pure: it turns one stored ``climate_decision`` record into
the plan document (:func:`build_plan`), resolves the owner's mode (:func:`resolve_mode`) and
renders the plan for an HTTP reader at a given instant (:func:`plan_response`,
:func:`summary_response`).  The contract is ``docs/climate-plan-contract.md`` and its JSON Schema
``docs/schemas/climate-plan-v1.json``.

Two layers, kept apart so the idempotency key stays stable:

* the **base plan** is a pure function of the decision record (``plan_id`` is a hash of it) and is
  immutable;
* the **served view** adds what depends on the reader's instant and on the owner's mode:
  ``requested_mode`` / ``effective_mode`` / ``live_locked``, and ``summary`` (age, the current
  slot, the next heating run, predicted room temperatures).

The effective mode can never be ``live`` in this code version (:data:`LIVE_ALLOWED`): a ``live``
request is served as ``shadow`` and flagged ``live_locked``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "CONTRACT_VERSION",
    "DEFAULT_TTL_MINUTES",
    "LIVE_ALLOWED",
    "LIVE_LOCK_REASON",
    "MODES",
    "PLAN_STATES",
    "SLOT_MINUTES",
    "WATER_RANGE_C",
    "ModeState",
    "build_plan",
    "is_expired",
    "plan_response",
    "plan_state",
    "resolve_mode",
    "summarise",
    "summary_response",
]

CONTRACT_VERSION = 1
SLOT_MINUTES = 15
#: A plan is valid this long after its decision time ``t0`` (the next decision lands at about
#: ``t0 + 70 min``, so a missed run leaves a 20 min margin before the plan is treated as absent).
DEFAULT_TTL_MINUTES = 90
#: The controller's fixed water range (degC): no plan ever carries a target outside it.
WATER_RANGE_C = (20, 35)
#: Hours ahead listed in the summary's predicted room temperatures.
SUMMARY_HOURS = (1, 2, 3, 4, 5, 6)

MODES = ("off", "shadow", "live")
#: Nothing in this code version may be effective ``live``; flipping it is ADR-0008's business.
LIVE_ALLOWED = False
LIVE_LOCK_REASON = "ADR-0008 proposed, not accepted; homelab hard lock optimizer_live_allowed=false"

#: What a reader is served, in order of precedence (the first that applies).
NOT_AVAILABLE_STATES = ("climate_shadow_disabled", "mode_off", "no_plan", "expired")
PLAN_STATES = ("planned", "skipped", "error")

_REASONS = {
    "climate_shadow_disabled": "the climate shadow is disabled (climate_shadow.enabled is false)",
    "mode_off": "the climate optimizer mode is off (HA helper or climate_shadow.optimizer_mode)",
    "no_plan": "no decision has been made yet since the service started",
    "expired": "the latest plan is past its valid_until: treat it as absent",
}


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeState:
    """The owner's request and what the optimizer does with it.

    ``requested`` is the HA select (or the configured fallback); ``effective`` is ``off`` or
    ``shadow``, never ``live``.  ``live_locked`` is true while ``live`` is requested."""

    requested: str
    effective: str
    live_locked: bool
    live_lock_reason: str | None
    source: str  # "helper" | "config"

    def fields(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested,
            "effective_mode": self.effective,
            "live_locked": self.live_locked,
            "live_lock_reason": self.live_lock_reason,
            "mode_source": self.source,
        }


def resolve_mode(helper_state: str | None, configured: str) -> ModeState:
    """The mode from the HA select's state, falling back to *configured* when the helper is
    missing, unavailable, unknown or anything but one of :data:`MODES`.

    A ``live`` request is honoured as a request only: the effective mode is ``shadow``."""
    if configured not in MODES:
        raise ValueError(f"configured mode must be one of {MODES}, not {configured!r}")
    candidate = (helper_state or "").strip().lower()
    if candidate in MODES:
        requested, source = candidate, "helper"
    else:
        requested, source = configured, "config"
    live = requested == "live"
    effective = "off" if requested == "off" else "shadow"
    assert effective != "live" or LIVE_ALLOWED  # structurally impossible; kept as a tripwire
    return ModeState(
        requested=requested,
        effective=effective,
        live_locked=live and not LIVE_ALLOWED,
        live_lock_reason=LIVE_LOCK_REASON if live and not LIVE_ALLOWED else None,
        source=source,
    )


# ---------------------------------------------------------------------------
# Base plan (pure function of the decision record)
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def _water(value: Any, lo: int, hi: int) -> int | None:
    if value is None:
        return None
    w = float(value)
    return int(min(max(round(w), lo), hi)) if math.isfinite(w) else None


def _comfort(rec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    c = (rec.get("inputs") or {}).get("comfort") or {}
    rooms = list(c.get("rooms") or [])
    helpers = c.get("from_helpers") or {}
    return {
        room: {
            "floor_c": round(float(c["floor_c"][i]), 2),
            "day_min_c": round(float(c["day_min_c"][i]), 2),
            "floor_from_helper": room in (helpers.get("floor") or ()),
            "day_min_from_helper": room in (helpers.get("day_min") or ()),
        }
        for i, room in enumerate(rooms)
    }


def build_plan(
    rec: Mapping[str, Any],
    *,
    code_version: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    water_range_c: tuple[int, int] = WATER_RANGE_C,
) -> dict[str, Any]:
    """The base ClimatePlan of one ``climate_decision`` record (:func:`shadow.decide`'s).

    Deterministic: the same record gives the same plan and the same ``plan_id``.  ``status`` is
    ``planned`` (slots present), ``skipped`` (no plan was possible for a data reason: no observer
    state or no inputs) or ``error`` (the solver or the decision failed); ``skipped`` and
    ``error`` carry no slots and a ``reason``.

    What the MPC plans per 15-minute slot, and so what is exported:

    * ``heating``: space heating **on** (1) or off (0), the MPC's binary ``on``.  It permits
      heating; the heat pump still starts and stops under its own thermostats and run logic.
    * ``water_c``: the water target the plan assumes while heating, **absolute degC**: the
      software curve at the forecast outdoor temperature plus the planned offset, rounded and
      clamped to the machine range (and to :data:`WATER_RANGE_C`); ``null`` when heating is off.
    * ``dhw``: the slots the plan treated as hot-water production (the controller's plan, else
      yesterday's measured slots); the plan never heats in them.
    * ``predicted_room_c``: the model's air temperature per thermostat room at the **end** of the
      slot under this plan (monitoring only, never a setpoint).
    """
    lo = max(int(water_range_c[0]), WATER_RANGE_C[0])
    hi = min(int(water_range_c[1]), WATER_RANGE_C[1])
    if lo > hi:
        raise ValueError(f"empty water range {water_range_c}")
    t0 = datetime.fromisoformat(rec["t0"])
    generated = datetime.fromisoformat(rec.get("run_at") or rec["t0"])
    inputs = rec.get("inputs") or {}
    plan_rec = rec.get("plan") or {}
    status = rec.get("status")
    reason: str | None = None
    slots: dict[str, Any] | None = None
    comfort = _comfort(rec)
    if status == "planned" and "plan_on" in plan_rec:
        out_status = "planned"
        rooms = list(comfort)
        n = len(plan_rec["plan_on"])
        dhw_idx = {int(i) for i in inputs.get("dhw_slots") or ()}
        heating = [1 if int(v) > 0 else 0 for v in plan_rec["plan_on"]]
        waters = list(plan_rec.get("plan_water_c") or [None] * n)
        temps = plan_rec.get("plan_temps") or []
        if len(waters) != n or len(temps) != len(rooms) or any(len(r) != n for r in temps):
            raise ValueError("the decision record's per-slot arrays disagree in length")
        slots = {
            "start": _iso(t0),
            "step_minutes": SLOT_MINUTES,
            "n": n,
            "heating": heating,
            "water_c": [_water(w, lo, hi) if on else None for w, on in zip(waters, heating)],
            "dhw": [1 if i in dhw_idx else 0 for i in range(n)],
            "predicted_room_c": {
                room: [None if v is None else round(float(v), 2) for v in temps[i]]
                for i, room in enumerate(rooms)
            },
        }
        if any(h and d for h, d in zip(slots["heating"], slots["dhw"])):
            raise ValueError("a plan with heating inside a DHW slot")
        if any(h and w is None for h, w in zip(slots["heating"], slots["water_c"])):
            raise ValueError("a heating slot without a water target")
    elif status == "planned":
        raise ValueError("a planned decision without per-slot arrays")
    elif status in ("no_state", "no_inputs"):
        out_status = "skipped"
        reason = {
            "no_state": "the observer has no usable state yet (warming up or data gap)",
            "no_inputs": "the horizon inputs are incomplete",
        }[status]
    elif status == "plan_failed":
        out_status = "error"
        reason = f"the solver returned status {plan_rec.get('status') or 'unknown'!r}"
    else:
        out_status = "error"
        reason = "the decision failed: " + str(rec.get("error") or "unknown").split(":")[0][:80]
    base: dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "advisory": True,
        "generated_at": _iso(generated),
        "valid_until": _iso(t0 + timedelta(minutes=int(ttl_minutes))),
        "t0": _iso(t0),
        "code_version": code_version,
        "regime": rec.get("regime") or "unknown",
        "status": out_status,
        "reason": reason,
        "model": {
            "prior_id": rec.get("prior_id"),
            "model_hash": rec.get("model_hash"),
            "mpc_config_sha256": rec.get("mpc_config_sha256"),
            "solver_status": plan_rec.get("status"),
        },
        "inputs": {
            "comfort": comfort,
            "away": inputs.get("away"),
            "weather": inputs.get("weather"),
        },
        "slots": slots,
    }
    base["plan_id"] = hashlib.sha256(_canonical(base)).hexdigest()[:16]
    return base


# ---------------------------------------------------------------------------
# Served view
# ---------------------------------------------------------------------------


def is_expired(plan: Mapping[str, Any], now: datetime) -> bool:
    return now >= datetime.fromisoformat(plan["valid_until"])


def plan_state(
    plan: Mapping[str, Any] | None, *, enabled: bool, mode: ModeState, now: datetime
) -> str:
    """``climate_shadow_disabled``, ``mode_off``, ``no_plan``, ``expired`` or the plan's status."""
    if not enabled:
        return "climate_shadow_disabled"
    if mode.effective == "off":
        return "mode_off"
    if plan is None:
        return "no_plan"
    if is_expired(plan, now):
        return "expired"
    return str(plan["status"])


def _runs(heating: Sequence[int]) -> list[tuple[int, int]]:
    """``[(first slot, last slot + 1)]`` of every run of heating slots."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, h in enumerate(heating):
        if h and start is None:
            start = i
        elif not h and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(heating)))
    return out


def summarise(plan: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """What a dashboard needs without templating: age, expiry, the slot in progress, the next
    heating run and the predicted room temperatures (derived; the slots stay the contract)."""
    generated = datetime.fromisoformat(plan["generated_at"])
    valid_until = datetime.fromisoformat(plan["valid_until"])
    out: dict[str, Any] = {
        "as_of": _iso(now),
        "age_s": round(max(0.0, (now - generated).total_seconds())),
        "expires_in_s": round((valid_until - now).total_seconds()),
        "slot_index": None,
        "slot_start": None,
        "heating_now": None,
        "water_c_now": None,
        "dhw_now": None,
        "next_heating_start": None,
        "next_heating_stop": None,
        "heating_slots": None,
        "predicted_room_c": None,
    }
    slots = plan.get("slots")
    if not slots:
        return out
    t0 = datetime.fromisoformat(slots["start"])
    step = timedelta(minutes=slots["step_minutes"])
    n = int(slots["n"])
    idx = int((now - t0) // step)
    heating = slots["heating"]
    out["heating_slots"] = int(sum(heating))
    if not 0 <= idx < n:
        return out
    out.update(
        slot_index=idx,
        slot_start=_iso(t0 + idx * step),
        heating_now=bool(heating[idx]),
        water_c_now=slots["water_c"][idx],
        dhw_now=bool(slots["dhw"][idx]),
    )
    runs = _runs(heating)
    current = next((r for r in runs if r[0] <= idx < r[1]), None)
    upcoming = next((r for r in runs if r[0] > idx), None)
    if upcoming is not None:
        out["next_heating_start"] = _iso(t0 + upcoming[0] * step)
    stop = current or upcoming
    if stop is not None:
        out["next_heating_stop"] = _iso(t0 + stop[1] * step)
    per_hour = 60 // slots["step_minutes"]
    out["predicted_room_c"] = {
        "hours_ahead": [h for h in SUMMARY_HOURS if idx + h * per_hour - 1 < n],
        "rooms": {
            room: [
                series[idx + h * per_hour - 1] for h in SUMMARY_HOURS if idx + h * per_hour - 1 < n
            ]
            for room, series in slots["predicted_room_c"].items()
        },
    }
    return out


def _unavailable(state: str, mode: ModeState, plan: Mapping[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": "no climate plan available",
        "state": state,
        "reason": _REASONS[state],
        **mode.fields(),
    }
    if plan is not None and state == "expired":
        body.update(plan_id=plan["plan_id"], valid_until=plan["valid_until"])
    return body


def plan_response(
    plan: Mapping[str, Any] | None, *, enabled: bool, mode: ModeState, now: datetime
) -> tuple[int, dict[str, Any]]:
    """``GET /climate/plan``: ``(200, plan view)`` while a plan is current (any status), else
    ``(404, {error, state, reason, mode fields})``."""
    state = plan_state(plan, enabled=enabled, mode=mode, now=now)
    if state in NOT_AVAILABLE_STATES:
        return 404, _unavailable(state, mode, plan)
    assert plan is not None
    return 200, {**plan, **mode.fields(), "summary": summarise(plan, now)}


def summary_response(
    plan: Mapping[str, Any] | None, *, enabled: bool, mode: ModeState, now: datetime
) -> dict[str, Any]:
    """``GET /climate/plan/summary``: always 200, a flat document for a dashboard sensor.

    ``state`` is what a reader gets from ``/climate/plan`` (``planned``, ``skipped``, ``error``
    or one of the not-available states), so the mode and the live lock stay visible while the
    plan itself is withheld."""
    state = plan_state(plan, enabled=enabled, mode=mode, now=now)
    body: dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "advisory": True,
        "state": state,
        "reason": _REASONS.get(state) or (plan or {}).get("reason"),
        **mode.fields(),
    }
    if state in PLAN_STATES:
        assert plan is not None
        body.update(
            plan_id=plan["plan_id"],
            status=plan["status"],
            regime=plan["regime"],
            generated_at=plan["generated_at"],
            valid_until=plan["valid_until"],
            code_version=plan["code_version"],
            comfort=plan["inputs"]["comfort"],
            away=plan["inputs"]["away"],
            **summarise(plan, now),
        )
    return body
