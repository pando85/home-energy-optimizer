"""Day-ahead vs final plan: tracker, restart rebuild, /plan, slot rows, metrics, offline KPIs."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from service_fakes import (
    ENTITY_MODE,
    NO_BATTERY,
    NOW,
    REPO,
    TARIFF,
    FakeHAServer,
    feed,
    service_config,
    utc,
)

from battery_optimizer.service.api import create_app
from battery_optimizer.service.clock import FakeClock
from battery_optimizer.service.plan_kpis import build_kpi_report
from battery_optimizer.service.plan_tracker import (
    DayAheadTracker,
    SlotPlan,
    revision_kwh,
    slot_plans_from_record,
)
from battery_optimizer.service.runtime import ShadowService
from battery_optimizer.service.sink import ShadowSink

STEP = timedelta(minutes=15)


def _run(coro):
    return asyncio.run(coro)


def _service(tmp_path, clock, **overrides):
    service = ShadowService(
        config=service_config(tmp_path, **overrides),
        tariff=TARIFF,
        data_dir=tmp_path / "data",
        clock=clock,
        code_version="test-sha",
        ws_transport=FakeHAServer(),
    )
    service.ha.stats.connected = True
    service.ha.stats.synced = True
    service.observer.mark_synced(clock.now())
    return service


@pytest.fixture
def cleanup():
    services = []
    yield services.append
    for service in services:
        service._executor.shutdown(wait=True)
        service._io.shutdown(wait=True)
        service._history_pool.shutdown(wait=True)


def _slot_plan(plan_id: str, seq: int, generated: datetime, slot: datetime, **kw) -> SlotPlan:
    values = {
        "interval_seconds": 900,
        "mode": "charge",
        "signed_power_w": 1000.0,
        "soc_target_frac": 0.5,
        "load_kwh": 0.1,
        "expected_buy_price_eur_per_kwh": 0.1,
        **kw,
    }
    return SlotPlan(plan_id, seq, generated, slot, **values)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

SLOT = utc(2026, 11, 4, 12, 0)


def test_the_first_plan_wins_whatever_the_arrival_order():
    early = _slot_plan("a", 1, SLOT - timedelta(hours=22), SLOT, signed_power_w=2000.0)
    late = _slot_plan("b", 2, SLOT - timedelta(minutes=10), SLOT, signed_power_w=500.0)
    for order in ((early, late), (late, early)):
        tracker = DayAheadTracker()
        tracker.observe(order, SLOT - timedelta(minutes=10))
        got = tracker.get(SLOT)
        assert got is not None and got.plan_id == "a"
        assert got.lead_seconds == 22 * 3600
    # Equal generation instants are ordered by the plan sequence.
    t = DayAheadTracker()
    t.observe([_slot_plan("y", 8, SLOT - STEP, SLOT), _slot_plan("x", 7, SLOT - STEP, SLOT)], SLOT)
    assert t.get(SLOT).plan_id == "x"


def test_a_plan_made_inside_the_slot_is_not_its_day_ahead_plan():
    tracker = DayAheadTracker()
    catch_up = _slot_plan("c", 5, SLOT + timedelta(seconds=40), SLOT)
    assert tracker.observe([catch_up], SLOT + timedelta(minutes=1)) == 0
    assert tracker.get(SLOT) is None  # missing, not zero
    assert tracker.upcoming(SLOT) == []


def test_the_tracker_prunes_past_slots_and_stays_bounded():
    tracker = DayAheadTracker()
    now = SLOT
    horizon = [
        _slot_plan("p", 1, now - STEP, now + i * STEP, signed_power_w=float(i)) for i in range(192)
    ]
    tracker.observe(horizon, now)
    assert len(tracker) == 192
    # Hours later only the retained look-back and the horizon of newer plans remain.
    later = now + timedelta(hours=5)
    assert tracker.prune(later) > 0
    assert min(
        p.slot_start for p in tracker.upcoming(later - timedelta(days=1))
    ) >= later - timedelta(hours=2, minutes=15)
    # An old slot is never admitted again (a rebuild reads yesterday's files).
    assert tracker.observe(horizon[:4], later) == 0
    assert all(p.slot_start + STEP > later for p in tracker.upcoming(later))


def test_revision_needs_both_plans():
    a = _slot_plan("a", 1, SLOT - STEP, SLOT, mode="charge", signed_power_w=2000.0)  # +0.5 kWh
    b = _slot_plan("b", 2, SLOT - STEP, SLOT, mode="discharge", signed_power_w=-1000.0)  # -0.25
    assert revision_kwh(a, b) == pytest.approx(0.75)
    assert revision_kwh(None, b) is None and revision_kwh(a, None) is None
    idle = _slot_plan("i", 3, SLOT - STEP, SLOT, mode="idle", signed_power_w=0.0)
    assert idle.energy_kwh() == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Runtime: capture, restart rebuild, /plan, slot row, metrics
# ---------------------------------------------------------------------------


def _two_plans(tmp_path, cleanup, **overrides):
    """A first plan at 10:50 and a later one at 10:52 with another SOC, both before 11:00."""
    clock = FakeClock(NOW)
    service = _service(tmp_path, clock, **(overrides or ENTITY_MODE))
    cleanup(service)
    feed(service, NOW, soc=0.2)
    assert _run(service.request_solve("a")) == "ok"
    clock.advance(120)
    feed(service, clock.now(), soc=0.9)
    assert _run(service.request_solve("b")) == "ok"
    a, b = service.plans
    return clock, service, a, b


def test_day_ahead_is_the_first_covering_plan_and_final_the_plan_of_record(tmp_path, cleanup):
    clock, service, a, b = _two_plans(tmp_path, cleanup)
    first = b.plan.horizon_start
    tracked = service.day_ahead.get(first)
    assert tracked.plan_id == a.plan_id and tracked.lead_seconds == 600
    assert tracked.load_kwh == pytest.approx(a.snapshot.load_kwh[0])
    clock.set(first)
    feed(service, first, soc=0.9)
    opened = _run(service.open_slot(first))
    assert opened.day_ahead.plan_id == a.plan_id
    assert opened.final.plan_id == b.plan_id and opened.final.lead_seconds == 480
    assert opened.final.load_kwh == pytest.approx(b.snapshot.load_kwh[0])
    assert opened.final.signed_power_w == opened.intent.signed_power_w
    assert opened.final.soc_target_frac == opened.intent.soc_target_frac


def test_restart_rebuilds_the_day_ahead_plans_from_the_plan_files(tmp_path, cleanup):
    clock, service, a, b = _two_plans(tmp_path, cleanup)
    first = b.plan.horizon_start
    sink = ShadowSink(tmp_path / "data")
    # Yesterday's plan covered the slot and wins; one from three UTC days back is not even read.
    record = json.loads(next((sink.root / "plans").rglob(f"{a.plan_id}.json")).read_text())
    for name, generated, seq in (
        ("yesterday", utc(2026, 10, 19, 15, 0), 1),
        ("ancient", utc(2026, 10, 16, 15, 0), 0),
    ):
        old = json.loads(json.dumps(record))
        old.update(plan_id=name, plan_sequence=seq, generated_at=generated.isoformat())
        for intent in old["intents"]:
            intent.update(plan_id=name, plan_sequence=seq, generated_at=generated.isoformat())
        sink.write_plan(old)

    restarted = _service(tmp_path, FakeClock(NOW + timedelta(minutes=3)), **ENTITY_MODE)
    cleanup(restarted)
    assert len(restarted.day_ahead) == 0
    assert _run(restarted.rebuild_day_ahead()) == 3  # a, b and "yesterday"
    got = restarted.day_ahead.get(first)
    assert (
        got.plan_id == "yesterday"
        and got.lead_seconds == (first - utc(2026, 10, 19, 15)).total_seconds()
    )
    assert got.load_kwh is not None
    # Without the old file the rebuild agrees with the process that never stopped.
    for path in (sink.root / "plans").rglob("yesterday.json"):
        path.unlink()
    again = _service(tmp_path, FakeClock(NOW + timedelta(minutes=3)), **ENTITY_MODE)
    cleanup(again)
    _run(again.rebuild_day_ahead())
    assert {s.slot_start: s.plan_id for s in again.day_ahead.upcoming(NOW)} == {
        s.slot_start: s.plan_id for s in service.day_ahead.upcoming(NOW)
    }
    # A plan that arrives after the restart never displaces the rebuilt day-ahead plan.
    again.day_ahead.observe(slot_plans_from_record(record), NOW)
    assert again.day_ahead.get(first).plan_id == a.plan_id


def test_a_corrupt_plan_file_does_not_stop_the_rebuild(tmp_path, cleanup):
    clock, service, a, b = _two_plans(tmp_path, cleanup)
    sink = ShadowSink(tmp_path / "data")
    bad = next((sink.root / "plans").rglob(f"{b.plan_id}.json")).with_name("bad.json")
    bad.write_text('{"intents": [{"valid_from": "nope"}]}\n')
    restarted = _service(tmp_path, FakeClock(NOW), **ENTITY_MODE)
    cleanup(restarted)
    assert _run(restarted.rebuild_day_ahead()) == 2
    assert restarted.day_ahead.get(b.plan.horizon_start).plan_id == a.plan_id


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bo.test") as client:
        return await client.get(path)


def test_plan_exposes_day_ahead_and_final_blocks_and_stays_compatible(tmp_path, cleanup):
    clock, service, a, b = _two_plans(tmp_path, cleanup)
    first = b.plan.horizon_start
    app = create_app(service, manage_lifecycle=False)

    before = _run(_get(app, "/plan")).json()
    assert before["current_slot"]["final"] is None  # the plan starts at the next boundary
    assert before["current_slot"]["decision"] is None

    clock.set(first)
    feed(service, first, soc=0.9)
    _run(service.open_slot(first))
    body = _run(_get(app, "/plan")).json()
    # Backward compatible: what HA templates read is untouched.
    intent = body["current_intent"]
    assert {"signed_power_w", "soc_target_frac", "plan_id"} <= set(intent)
    assert body["plan"]["intents"][0]["signed_power_w"] == b.intents[0].signed_power_w
    slot = body["current_slot"]
    assert slot["slot_start"] == first.isoformat() and slot["decision"] == "intent"
    assert slot["day_ahead"]["plan_id"] == a.plan_id and slot["day_ahead"]["lead_seconds"] == 600
    assert slot["final"]["plan_id"] == b.plan_id
    assert slot["final"]["signed_power_w"] == intent["signed_power_w"]
    assert slot["final"]["soc_target_frac"] == intent["soc_target_frac"]
    assert slot["day_ahead"]["load_forecast_w"] == pytest.approx(a.snapshot.load_kwh[0] * 4000)
    forward = body["day_ahead"]["slots"]
    assert forward[0]["slot_start"] == first.isoformat()
    assert forward[0]["plan_id"] == a.plan_id
    assert forward[0]["latest"]["plan_id"] == b.plan_id
    assert [s["slot_start"] for s in forward] == sorted(s["slot_start"] for s in forward)
    assert len(forward) == len(b.intents)


def test_plan_without_a_plan_still_lists_the_rebuilt_day_ahead(tmp_path, cleanup):
    _, service, a, b = _two_plans(tmp_path, cleanup)
    restarted = _service(tmp_path, FakeClock(NOW + timedelta(minutes=3)), **ENTITY_MODE)
    cleanup(restarted)
    _run(restarted.rebuild_day_ahead())
    view = restarted.plan_view()
    assert view["plan"] is None and view["current_intent"] is None
    assert view["day_ahead"]["slots"][0]["plan_id"] == a.plan_id
    assert view["day_ahead"]["slots"][0]["latest"] is None
    assert view["current_slot"]["final"] is None


def _gauge(service, name, plan):
    return service.metrics.registry.get_sample_value(name, {"plan": plan})


def _counter(service, name, **labels):
    return service.metrics.registry.get_sample_value(name, labels)


def test_slot_open_gauges_describe_the_current_slot_and_skip_what_is_missing(tmp_path, cleanup):
    clock, service, a, b = _two_plans(tmp_path, cleanup)
    first = b.plan.horizon_start
    assert _gauge(service, "battery_optimizer_slot_battery_power_watts", "final") is None
    clock.set(first)
    feed(service, first, soc=0.9)
    _run(service.open_slot(first))
    for plan, source in (("day_ahead", a), ("final", b)):
        intent = source.intents[0]
        assert _gauge(service, "battery_optimizer_slot_battery_power_watts", plan) == (
            intent.signed_power_w
        )
        assert _gauge(service, "battery_optimizer_slot_soc_target_ratio", plan) == (
            intent.soc_target_frac
        )
        assert _gauge(service, "battery_optimizer_slot_load_forecast_watts", plan) == pytest.approx(
            source.snapshot.load_kwh[0] * 4000
        )
    assert _gauge(service, "battery_optimizer_slot_plan_lead_seconds", "day_ahead") == 600
    assert _gauge(service, "battery_optimizer_slot_plan_lead_seconds", "final") == 480

    # No day-ahead plan (a first deploy): no sample, not a zero.
    second = first + STEP
    service.day_ahead = DayAheadTracker()
    clock.set(second)
    feed(service, second, soc=0.9)
    _run(service.open_slot(second))
    assert _gauge(service, "battery_optimizer_slot_battery_power_watts", "day_ahead") is None
    assert _gauge(service, "battery_optimizer_slot_plan_lead_seconds", "day_ahead") is None
    assert "battery_optimizer_slot_battery_power_watts{" in service.metrics.render().decode()


def test_slot_close_scores_both_plans_and_records_them(tmp_path, cleanup):
    clock, service, a, b = _two_plans(tmp_path, cleanup, **NO_BATTERY)
    first = b.plan.horizon_start
    clock.set(first)
    feed(service, first, grid=800.0, energy=1000.0)
    opened = _run(service.open_slot(first))
    # Give the plans distinct, known battery power: +0.5 kWh vs -0.1 kWh over the slot.
    opened.day_ahead = replace(opened.day_ahead, mode="charge", signed_power_w=2000.0)
    opened.final = replace(opened.final, mode="discharge", signed_power_w=-400.0)
    for minute in (5, 10, 15):
        feed(
            service,
            first + timedelta(minutes=minute),
            grid=800.0,
            energy=1000.0 + 0.2 * minute / 15,
        )
    clock.set(first + timedelta(minutes=15, seconds=20))
    record = _run(service.close_slot(first))

    da, fin = record["day_ahead"], record["final"]
    assert da["plan_id"] == a.plan_id and fin["plan_id"] == b.plan_id
    assert da["lead_seconds"] == 600 and fin["lead_seconds"] == 480
    assert (da["charge_kwh"], da["discharge_kwh"]) == (pytest.approx(0.5), 0.0)
    assert (fin["charge_kwh"], fin["discharge_kwh"]) == (0.0, pytest.approx(0.1))
    assert da["load_forecast_kwh"] == pytest.approx(a.snapshot.load_kwh[0])
    assert fin["load_forecast_kwh"] == pytest.approx(b.snapshot.load_kwh[0])
    assert da["soc_target_frac"] == a.intents[0].soc_target_frac
    assert record["plan_revision_kwh"] == pytest.approx(0.6)
    assert record["intent"]["plan_id"] == b.plan_id  # existing fields unchanged
    row = json.loads(
        next((tmp_path / "data" / "service" / "v1" / "slots").rglob("*.jsonl")).read_text()
    )
    assert row["day_ahead"]["plan_id"] == a.plan_id and row["plan_revision_kwh"] == pytest.approx(
        0.6
    )

    actual = record["gross_load_kwh"]
    assert actual == pytest.approx(0.2)
    for plan, source in (("day_ahead", a), ("final", b)):
        forecast = source.snapshot.load_kwh[0]
        assert (
            _counter(service, "battery_optimizer_load_forecast_scored_slots_total", plan=plan) == 1
        )
        assert _counter(service, "battery_optimizer_load_forecast_kwh_total", plan=plan) == (
            pytest.approx(forecast)
        )
        assert _counter(service, "battery_optimizer_load_actual_kwh_total", plan=plan) == (
            pytest.approx(actual)
        )
        assert _counter(
            service, "battery_optimizer_load_forecast_abs_error_kwh_total", plan=plan
        ) == (pytest.approx(abs(actual - forecast)))
    assert _counter(service, "battery_optimizer_plan_revision_kwh_total") == pytest.approx(0.6)
    name = "battery_optimizer_shadow_day_ahead_energy_kwh_total"
    assert _counter(service, name, direction="charge") == pytest.approx(0.5)
    assert _counter(service, name, direction="discharge") == 0.0


def test_a_slot_without_a_day_ahead_plan_scores_only_the_final_one(tmp_path, cleanup):
    clock, service, a, b = _two_plans(tmp_path, cleanup, **NO_BATTERY)
    service.day_ahead = DayAheadTracker()
    first = b.plan.horizon_start
    clock.set(first)
    feed(service, first, grid=800.0, energy=1000.0)
    _run(service.open_slot(first))
    for minute in (5, 10, 15):
        feed(
            service,
            first + timedelta(minutes=minute),
            grid=800.0,
            energy=1000.0 + 0.2 * minute / 15,
        )
    clock.set(first + timedelta(minutes=15, seconds=20))
    record = _run(service.close_slot(first))
    assert record["day_ahead"] is None and record["plan_revision_kwh"] is None
    assert record["final"]["plan_id"] == b.plan_id
    assert (
        _counter(service, "battery_optimizer_load_forecast_scored_slots_total", plan="day_ahead")
        == 0
    )
    assert (
        _counter(service, "battery_optimizer_load_forecast_scored_slots_total", plan="final") == 1
    )
    assert _counter(service, "battery_optimizer_plan_revision_kwh_total") == 0


def test_an_unmeasured_load_is_not_scored(tmp_path, cleanup):
    clock, service, a, b = _two_plans(tmp_path, cleanup, **NO_BATTERY)
    first = b.plan.horizon_start
    clock.set(first)
    _run(service.open_slot(first))
    clock.set(first + timedelta(minutes=15, seconds=20))
    record = _run(service.close_slot(first))  # no energy counter: gross load unknown
    assert record["gross_load_kwh"] is None
    assert record["day_ahead"]["load_forecast_kwh"] is not None
    assert (
        _counter(service, "battery_optimizer_load_forecast_scored_slots_total", plan="final") == 0
    )


# ---------------------------------------------------------------------------
# Offline KPI tool on a tiny synthetic store
# ---------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Madrid")
S1, S2 = utc(2026, 11, 4, 12, 0), utc(2026, 11, 4, 12, 15)


def _plan_record(plan_id, seq, generated, slots):
    """``slots``: ``(slot_start, mode, signed_power_w, soc_target, load_kwh)``."""
    intents = [
        {
            "plan_id": plan_id,
            "plan_sequence": seq,
            "valid_from": start.isoformat(),
            "interval_end": (start + STEP).isoformat(),
            "mode": mode,
            "target_power_w": abs(power),
            "signed_power_w": power,
            "soc_target_frac": soc,
            "expected_buy_price_eur_per_kwh": 0.1,
            "generated_at": generated.isoformat(),
        }
        for start, mode, power, soc, _ in slots
    ]
    return {
        "plan_id": plan_id,
        "plan_sequence": seq,
        "generated_at": generated.isoformat(),
        "inputs": {
            "interval_seconds": 900,
            "slot_starts": [s[0].isoformat() for s in slots],
            "load_kwh": [s[4] for s in slots],
        },
        "intents": intents,
    }


def _slot_row(start, intent, price, load, charge, discharge, soc_end):
    return {
        "slot_start": start.isoformat(),
        "local_day": "2026-11-04",
        "interval_seconds": 900,
        "price_eur_per_kwh": price,
        "decision": "intent",
        "intent": intent,
        "gross_load_kwh": load,
        "measured": {
            "battery_charge_kwh": charge,
            "battery_discharge_kwh": discharge,
            "soc_end_frac": soc_end,
        },
    }


def _synthetic_store(tmp_path):
    data = tmp_path / "store"
    sink = ShadowSink(data)
    day_before = utc(2026, 11, 3, 14, 0)
    morning = utc(2026, 11, 4, 6, 0)
    final_at = utc(2026, 11, 4, 11, 50)
    sink.write_plan(
        _plan_record(
            "dayahead",
            1,
            day_before,
            [(S1, "charge", 2000.0, 0.55, 0.10), (S2, "discharge", -1000.0, 0.45, 0.20)],
        )
    )
    sink.write_plan(  # a later plan that must not displace the day-ahead one
        _plan_record(
            "morning", 2, morning, [(S1, "idle", 0.0, 0.9, 0.9), (S2, "idle", 0.0, 0.9, 0.9)]
        )
    )
    final = _plan_record(
        "final",
        3,
        final_at,
        [(S1, "charge", 1000.0, 0.5, 0.14), (S2, "discharge", -1000.0, 0.4, 0.20)],
    )
    sink.write_plan(final)
    i1, i2 = final["intents"]
    sink.append_slot(S1, _slot_row(S1, i1, 0.10, 0.15, 0.2, 0.0, 0.5))
    sink.append_slot(S2, _slot_row(S2, i2, 0.20, 0.25, 0.0, 0.3, 0.4))
    return data


def test_the_offline_report_compares_day_ahead_final_and_actual(tmp_path):
    data = _synthetic_store(tmp_path)
    report, slots = build_kpi_report(data, utc(2026, 11, 4), utc(2026, 11, 5), tz=TZ)
    assert report["slots"] == 2 and report["slots_with_day_ahead"] == 2
    assert [s.day_ahead.plan_id for s in slots] == ["dayahead", "dayahead"]
    assert [s.final.plan_id for s in slots] == ["final", "final"]
    assert report["day_ahead_lead_hours"]["max"] == pytest.approx(22.25)

    da, fin = report["load"]["day_ahead"], report["load"]["final"]
    assert da["mae_kwh_per_slot"] == pytest.approx(0.05) and da[
        "bias_kwh_per_slot"
    ] == pytest.approx(0.05)
    assert fin["mae_kwh_per_slot"] == pytest.approx(0.03) and fin[
        "bias_kwh_per_slot"
    ] == pytest.approx(0.03)
    assert da["daily"][0]["error_pct"] == pytest.approx(-25.0)  # forecast 0.30 vs actual 0.40
    assert da["daily"][0]["complete"] is False and da["mean_abs_daily_error_pct"] is None

    battery = report["battery"]
    assert battery["comparable_slots"] == 2
    assert battery["day_ahead"]["charge_kwh"] == pytest.approx(0.5)
    assert battery["final"]["charge_kwh"] == pytest.approx(0.25)
    assert battery["actual"]["discharge_kwh"] == pytest.approx(0.3)
    assert battery["actual"]["avg_price_charging_eur_per_kwh"] == pytest.approx(0.10)
    assert battery["actual"]["avg_price_discharging_eur_per_kwh"] == pytest.approx(0.20)
    assert battery["day_ahead"]["saving_eur"] == pytest.approx(0.0)
    assert battery["final"]["saving_eur"] == pytest.approx(0.025)
    assert battery["actual"]["saving_eur"] == pytest.approx(0.04)
    assert battery["plan_revision"]["total_kwh"] == pytest.approx(0.25)
    assert battery["plan_revision"]["slots_revised"] == 1
    assert report["soc"]["day_ahead"]["bias_frac"] == pytest.approx(-0.05)
    assert report["soc"]["final"]["mae_frac"] == pytest.approx(0.0)


def test_the_offline_report_handles_an_empty_window(tmp_path):
    report, slots = build_kpi_report(
        tmp_path / "nothing", utc(2026, 11, 4), utc(2026, 11, 5), tz=TZ
    )
    assert slots == [] and report["slots"] == 0
    assert report["load"]["final"]["mae_kwh_per_slot"] is None
    assert report["battery"]["comparable_slots"] == 0


def test_the_hack_script_prints_and_writes_outside_the_repo(tmp_path, capsys):
    data = _synthetic_store(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "day_ahead_kpis", REPO / "hack" / "day_ahead_kpis.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = ["--from", "2026-11-04", "--to", "2026-11-05", "--data-dir", str(data)]
    assert module.main([*args, "--run-id", "t1"]) == 0
    out = capsys.readouterr().out
    assert "plan revision" in out and "day_ahead" in out and "MAE 0.0500" in out
    out_dir = data / "analysis" / "v1" / "day-ahead-kpis" / "t1"
    assert json.loads((out_dir / "report.json").read_text())["slots"] == 2
    header, *rows = (out_dir / "slots.csv").read_text().splitlines()
    assert "day_ahead_power_w" in header and len(rows) == 2
    assert module.main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["battery"]["comparable_slots"] == 2
