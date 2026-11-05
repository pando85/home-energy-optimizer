"""The advisory ClimatePlan (contract v1): shape, schema, idempotency, safety clamps, modes."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from climate_shadow_fakes import MPC, SETTINGS, forecast_record, prior
from jsonschema import Draft202012Validator
from test_climate_shadow import MIDNIGHT, live_data, model_rec

from battery_optimizer.climate import plan_export as pe
from battery_optimizer.climate import shadow

REPO = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "docs" / "schemas" / "climate-plan-v1.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)
T0 = MIDNIGHT + timedelta(hours=10)
RUN_AT = T0 + timedelta(minutes=10)
SHADOW = pe.resolve_mode("shadow", "off")


def validate(body) -> None:
    errors = sorted(VALIDATOR.iter_errors(body), key=lambda e: list(e.path))
    assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


@pytest.fixture(scope="module")
def decision() -> dict:
    """A real decision record: the MPC on a synthetic house, with helper comfort levels."""
    sp = prior()
    inputs = shadow.LiveInputs(
        regime="comfort",
        away=False,
        dhw_plan=(
            '{"day":"2026-11-16","start":"2026-11-16T11:00:00+01:00",'
            '"end":"2026-11-16T12:00:00+01:00","forced":true,"target_written":true,"done":null}'
        ),
        forecast=forecast_record(T0 - timedelta(minutes=55)),
    )
    comfort = {"floor": {"bedroom": 20.5}, "day_min": {"hall_kitchen": 23.5}}
    rec = shadow.decide(
        sp, model_rec(sp), live_data(T0), MPC, T0, inputs, settings=SETTINGS, comfort=comfort
    )
    assert rec["status"] == "planned"
    rec["run_at"] = RUN_AT.isoformat()
    return rec


@pytest.fixture(scope="module")
def plan(decision) -> dict:
    return pe.build_plan(decision, code_version="abc1234")


def served(plan, mode=SHADOW, now=RUN_AT + timedelta(minutes=5)):
    code, body = pe.plan_response(plan, enabled=True, mode=mode, now=now)
    assert code == 200
    return body


# ---------------------------------------------------------------------------
# Shape and schema
# ---------------------------------------------------------------------------


def test_a_produced_plan_validates_against_the_committed_schema(plan):
    body = served(plan)
    validate(body)
    assert body["version"] == 1 and body["advisory"] is True and body["status"] == "planned"
    assert body["code_version"] == "abc1234" and body["regime"] == "comfort"
    assert body["t0"] == T0.isoformat() and body["generated_at"] == RUN_AT.isoformat()
    assert body["valid_until"] == (T0 + timedelta(minutes=90)).isoformat()


def test_the_schema_is_itself_valid_and_rejects_a_tampered_plan(plan):
    Draft202012Validator.check_schema(SCHEMA)
    body = served(plan)
    for mutate in (
        lambda b: b.update(version=2),
        lambda b: b.update(advisory=False),
        lambda b: b.update(effective_mode="live"),
        lambda b: b["slots"]["water_c"].__setitem__(b["slots"]["heating"].index(1), 36),
        lambda b: b["slots"]["water_c"].__setitem__(b["slots"]["heating"].index(1), 19),
        lambda b: b["slots"].update(step_minutes=30),
        lambda b: b.update(status="skipped"),  # slots present without a plan
        lambda b: b.update(live_locked=True),  # locked while shadow was requested
        lambda b: b.update(valid_until="tomorrow"),
        lambda b: b.pop("plan_id"),
    ):
        bad = copy.deepcopy(body)
        mutate(bad)
        assert list(VALIDATOR.iter_errors(bad)), mutate


def test_the_committed_example_validates():
    example = json.loads((REPO / "docs" / "schemas" / "climate-plan-v1.example.json").read_text())
    validate(example)
    assert example["live_locked"] is True and example["effective_mode"] == "shadow"


def test_the_slots_are_what_the_mpc_planned(plan, decision):
    s = plan["slots"]
    rec = decision["plan"]
    assert s["start"] == T0.isoformat() and s["step_minutes"] == 15 and s["n"] == 96
    assert s["heating"] == [1 if v else 0 for v in rec["plan_on"]]
    assert 0 < sum(s["heating"]) < 96  # a real plan: some heating, not all
    assert all(len(v) == 96 for k, v in s.items() if isinstance(v, list))
    assert all(len(v) == 96 for v in s["predicted_room_c"].values())
    assert list(s["predicted_room_c"]) == ["bedroom", "hall_kitchen", "office_t"]
    for on, water, planned in zip(s["heating"], s["water_c"], rec["plan_water_c"], strict=True):
        assert (water is not None) is bool(on)
        if on:
            assert water == planned and isinstance(water, int) and 20 <= water <= 35
    # the DHW slots of the controller's plan (11:00 to 12:00 local = 10:00 to 11:00 UTC = 4..7)
    assert [i for i, v in enumerate(s["dhw"]) if v][:4] == [4, 5, 6, 7]
    assert not any(h and d for h, d in zip(s["heating"], s["dhw"], strict=True))
    assert plan["inputs"]["comfort"]["bedroom"] == {
        "floor_c": 20.5,
        "day_min_c": 21.0,
        "floor_from_helper": True,
        "day_min_from_helper": False,
    }
    assert plan["inputs"]["comfort"]["hall_kitchen"]["day_min_c"] == 23.5
    assert plan["inputs"]["weather"] == "forecast" and plan["inputs"]["away"] is False


def test_the_plan_is_deterministic_and_its_id_follows_its_content(decision, plan):
    again = pe.build_plan(copy.deepcopy(decision), code_version="abc1234")
    assert again == plan and again["plan_id"] == plan["plan_id"]
    # the mode and the instant of the request are not part of the id
    assert served(plan, pe.resolve_mode("live", "off"))["plan_id"] == plan["plan_id"]
    assert served(plan, now=RUN_AT + timedelta(minutes=40))["plan_id"] == plan["plan_id"]
    changed = copy.deepcopy(decision)
    changed["plan"]["plan_on"][3] ^= 1
    changed["plan"]["plan_water_c"][3] = None if changed["plan"]["plan_on"][3] == 0 else 28.0
    assert pe.build_plan(changed, code_version="abc1234")["plan_id"] != plan["plan_id"]
    assert pe.build_plan(decision, code_version="other")["plan_id"] != plan["plan_id"]


def test_the_water_target_is_clamped_to_the_controllers_range(decision):
    rec = copy.deepcopy(decision)
    on = [i for i, v in enumerate(rec["plan"]["plan_on"]) if v][:3]
    for i, w in zip(on, (14.2, 41.0, 27.6), strict=True):
        rec["plan"]["plan_water_c"][i] = w
    water = pe.build_plan(rec, code_version="x")["slots"]["water_c"]
    assert [water[i] for i in on] == [20, 35, 28]
    # a config wider than the controller's fixed range cannot widen the export; narrower narrows
    wide = pe.build_plan(rec, code_version="x", water_range_c=(10, 60))["slots"]["water_c"]
    assert [wide[i] for i in on] == [20, 35, 28]
    narrow = pe.build_plan(rec, code_version="x", water_range_c=(22, 30))["slots"]["water_c"]
    assert [narrow[i] for i in on] == [22, 30, 28]
    with pytest.raises(ValueError, match="empty water range"):
        pe.build_plan(rec, code_version="x", water_range_c=(36, 40))


def test_a_malformed_record_is_refused_not_exported(decision):
    heating_in_dhw = copy.deepcopy(decision)
    i = decision["plan"]["plan_on"].index(1)
    heating_in_dhw["inputs"]["dhw_slots"] = [i]
    with pytest.raises(ValueError, match="DHW"):
        pe.build_plan(heating_in_dhw, code_version="x")
    short = copy.deepcopy(decision)
    short["plan"]["plan_water_c"] = short["plan"]["plan_water_c"][:-1]
    with pytest.raises(ValueError, match="length"):
        pe.build_plan(short, code_version="x")
    bare = copy.deepcopy(decision)
    del bare["plan"]["plan_on"]
    with pytest.raises(ValueError, match="per-slot"):
        pe.build_plan(bare, code_version="x")


@pytest.mark.parametrize(
    ("status", "plan_status", "extra", "expect"),
    [
        ("no_state", None, {}, ("skipped", "observer")),
        ("no_inputs", None, {}, ("skipped", "inputs")),
        ("plan_failed", "infeasible", {}, ("error", "infeasible")),
        ("error", None, {"error": "KeyError: /secret/path"}, ("error", "KeyError")),
    ],
)
def test_a_failed_decision_is_a_plan_without_slots_and_with_a_reason(
    status, plan_status, extra, expect
):
    rec = {"t0": T0.isoformat(), "run_at": RUN_AT.isoformat(), "regime": "legacy", "status": status}
    rec.update(extra)
    if plan_status:
        rec["plan"] = {"status": plan_status}
    plan = pe.build_plan(rec, code_version="x")
    body = served(plan)
    validate(body)
    assert body["status"] == expect[0] and body["slots"] is None
    assert expect[1] in body["reason"] and "/secret" not in body["reason"]
    assert body["summary"]["heating_now"] is None


# ---------------------------------------------------------------------------
# Validity
# ---------------------------------------------------------------------------


def test_a_plan_is_absent_from_its_valid_until_on(plan):
    last = datetime.fromisoformat(plan["valid_until"]) - timedelta(seconds=1)
    assert pe.plan_response(plan, enabled=True, mode=SHADOW, now=last)[0] == 200
    code, body = pe.plan_response(plan, enabled=True, mode=SHADOW, now=T0 + timedelta(minutes=90))
    assert code == 404 and body["state"] == "expired" and body["plan_id"] == plan["plan_id"]
    assert "slots" not in body
    short = pe.build_plan(
        {"t0": T0.isoformat(), "status": "no_state"}, code_version="x", ttl_minutes=30
    )
    assert short["valid_until"] == (T0 + timedelta(minutes=30)).isoformat()


# ---------------------------------------------------------------------------
# Summary for a dashboard
# ---------------------------------------------------------------------------


def test_the_summary_reads_the_slot_in_progress(plan):
    s = plan["slots"]
    now = T0 + timedelta(minutes=25)  # slot 1
    summary = served(plan, now=now)["summary"]
    assert (
        summary["slot_index"] == 1
        and summary["slot_start"] == (T0 + timedelta(minutes=15)).isoformat()
    )
    assert summary["heating_now"] is bool(s["heating"][1])
    assert summary["water_c_now"] == s["water_c"][1]
    assert summary["age_s"] == 900 and summary["expires_in_s"] == 3900
    assert summary["heating_slots"] == sum(s["heating"])
    rooms = summary["predicted_room_c"]
    assert rooms["hours_ahead"] == [1, 2, 3, 4, 5, 6]
    # h hours after the slot start = the end of slot idx + 4h - 1
    assert rooms["rooms"]["bedroom"][0] == s["predicted_room_c"]["bedroom"][1 + 4 - 1]
    assert rooms["rooms"]["office_t"][5] == s["predicted_room_c"]["office_t"][1 + 24 - 1]


def test_next_heating_start_and_stop_follow_the_runs():
    rec = {
        "t0": T0.isoformat(),
        "run_at": RUN_AT.isoformat(),
        "status": "planned",
        "regime": "comfort",
        "inputs": {
            "comfort": {
                "rooms": ["a"],
                "floor_c": [21.0],
                "day_min_c": [21.0],
                "from_helpers": {},
            },
            "dhw_slots": [],
        },
        "plan": {
            "status": "optimal",
            "plan_on": [0, 1, 1, 0, 0, 1, 0, 0],
            "plan_water_c": [None, 28, 29, None, None, 30, None, None],
            "plan_temps": [[21.0] * 8],
        },
    }
    plan = pe.build_plan(rec, code_version="x")

    def at(slot):
        return pe.summarise(plan, T0 + timedelta(minutes=15 * slot + 1))

    def t(slot):
        return (T0 + timedelta(minutes=15 * slot)).isoformat()

    off = at(0)
    assert (off["heating_now"], off["next_heating_start"], off["next_heating_stop"]) == (
        False,
        t(1),
        t(3),
    )
    mid = at(1)
    assert (mid["heating_now"], mid["water_c_now"]) == (True, 28)
    assert (mid["next_heating_start"], mid["next_heating_stop"]) == (t(5), t(3))
    assert (at(3)["next_heating_start"], at(3)["next_heating_stop"]) == (t(5), t(6))
    assert (at(6)["next_heating_start"], at(6)["next_heating_stop"]) == (None, None)
    # the horizon is 2 h: only the hours that exist are listed
    assert at(0)["predicted_room_c"]["hours_ahead"] == [1, 2]
    beyond = pe.summarise(plan, T0 + timedelta(hours=3))
    assert beyond["slot_index"] is None and beyond["heating_now"] is None


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("helper", "configured", "requested", "effective", "locked", "source"),
    [
        ("off", "off", "off", "off", False, "helper"),
        ("shadow", "off", "shadow", "shadow", False, "helper"),
        ("live", "off", "live", "shadow", True, "helper"),
        (" Shadow ", "off", "shadow", "shadow", False, "helper"),
        (None, "off", "off", "off", False, "config"),
        (None, "shadow", "shadow", "shadow", False, "config"),
        (None, "live", "live", "shadow", True, "config"),
        ("unavailable", "shadow", "shadow", "shadow", False, "config"),
        ("unknown", "off", "off", "off", False, "config"),
        ("", "shadow", "shadow", "shadow", False, "config"),
        ("go live!", "shadow", "shadow", "shadow", False, "config"),
        ("on", "off", "off", "off", False, "config"),
    ],
)
def test_the_mode_comes_from_the_helper_else_the_config(
    helper, configured, requested, effective, locked, source
):
    m = pe.resolve_mode(helper, configured)
    assert (m.requested, m.effective, m.live_locked, m.source) == (
        requested,
        effective,
        locked,
        source,
    )
    assert (m.live_lock_reason is not None) is locked
    if locked:
        assert "ADR-0008" in m.live_lock_reason and "optimizer_live_allowed=false" in (
            m.live_lock_reason
        )


def test_the_effective_mode_is_never_live():
    assert pe.LIVE_ALLOWED is False
    for helper in (None, "off", "shadow", "live", "LIVE", "live ", "x", "unavailable"):
        for configured in pe.MODES:
            assert pe.resolve_mode(helper, configured).effective in ("off", "shadow")
    with pytest.raises(ValueError):
        pe.resolve_mode("live", "on")


def test_each_state_is_served_as_documented(plan):
    now = RUN_AT + timedelta(minutes=5)
    live = pe.resolve_mode("live", "off")

    def get(plan_, *, enabled=True, mode=SHADOW, when=now):
        code, body = pe.plan_response(plan_, enabled=enabled, mode=mode, now=when)
        summary = pe.summary_response(plan_, enabled=enabled, mode=mode, now=when)
        return code, body, summary

    code, body, summary = get(plan)
    assert (code, body["effective_mode"], summary["state"]) == (200, "shadow", "planned")
    code, body, summary = get(plan, mode=live)
    assert code == 200 and body["requested_mode"] == "live" and body["effective_mode"] == "shadow"
    assert body["live_locked"] is True and summary["live_locked"] is True
    validate(body)
    code, body, summary = get(plan, mode=pe.resolve_mode("off", "shadow"))
    assert code == 404 and body["state"] == "mode_off" and "off" in body["reason"]
    assert summary["state"] == "mode_off"
    assert "slots" not in summary and "heating_now" not in summary
    code, body, summary = get(plan, enabled=False)
    assert code == 404 and body["state"] == "climate_shadow_disabled"
    code, body, summary = get(None)
    assert code == 404 and body["state"] == "no_plan" and summary["state"] == "no_plan"
    code, body, summary = get(plan, when=T0 + timedelta(hours=2))
    assert code == 404 and body["state"] == "expired" and summary["state"] == "expired"
    # every 404 still says what the owner asked for and what is in effect
    for st in (get(None, mode=live), get(plan, enabled=False, mode=live)):
        assert st[1]["requested_mode"] == "live" and st[1]["live_locked"] is True


def test_the_summary_is_flat_small_and_complete(plan):
    summary = pe.summary_response(plan, enabled=True, mode=SHADOW, now=RUN_AT)
    assert summary["state"] == "planned" and summary["plan_id"] == plan["plan_id"]
    for key in (
        "heating_now",
        "water_c_now",
        "next_heating_start",
        "next_heating_stop",
        "predicted_room_c",
        "comfort",
        "age_s",
        "expires_in_s",
        "valid_until",
        "requested_mode",
        "effective_mode",
        "live_locked",
        "regime",
    ):
        assert key in summary, key
    assert len(json.dumps(summary)) < 3000
    # one HA sensor's attributes hold the whole plan: the limit is about 16 KB
    assert len(json.dumps(served(plan))) < 8000
