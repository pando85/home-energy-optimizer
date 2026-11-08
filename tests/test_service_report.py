"""shadow-report on a synthetic store with hand-computed expectations."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest
from service_fakes import REPO, TARIFF, TZ, utc, write_service_config

from battery_optimizer.cli import main
from battery_optimizer.service.shadow_report import (
    build_shadow_report,
    parse_window,
    render_markdown,
)
from battery_optimizer.service.sink import ShadowSink, iter_records
from battery_optimizer.service.timegrid import local_day_bounds_utc

S0 = utc(2026, 10, 20, 10, 0)
STEP = timedelta(minutes=15)
ETA = math.sqrt(0.8)


def _row(
    start,
    *,
    price,
    grid,
    intent=None,
    decision="intent",
    soc0=None,
    soc1=None,
    violation=None,
    battery_w=0.0,
):
    return {
        "kind": "shadow_slot",
        "slot_start": start.isoformat(),
        "slot_end": (start + STEP).isoformat(),
        "observed_from": start.isoformat(),
        "local_day": "2026-10-20",
        "interval_seconds": 900,
        "soc_source": "entity",
        "battery_mapped": True,
        "price_eur_per_kwh": price,
        "decision": decision,
        "intent": intent,
        "measured": {
            "grid_kwh": grid,
            "battery_charge_kwh": 0.0,
            "battery_discharge_kwh": 0.0,
            "battery_power_avg_w": battery_w,
            "soc_start_frac": soc0,
            "soc_end_frac": soc1,
        },
        "soc_violation": violation,
    }


def _intent(mode, watts):
    sign = 1 if mode == "charge" else -1
    return {"mode": mode, "target_power_w": watts, "signed_power_w": sign * watts}


@pytest.fixture
def store(tmp_path):
    data = tmp_path / "data"
    sink = ShadowSink(data)
    rows = [
        _row(S0, price=0.10, grid=0.30, intent=_intent("charge", 2000), soc0=0.5, soc1=0.5),
        _row(
            S0 + STEP,
            price=0.30,
            grid=0.40,
            intent=_intent("discharge", 1000),
            soc0=0.5,
            soc1=0.5,
            violation=True,
        ),
        _row(
            S0 + 2 * STEP,
            price=0.30,
            grid=0.10,
            intent=_intent("discharge", 1000),
            soc0=0.5,
            soc1=0.5,
        ),
        _row(S0 + 3 * STEP, price=0.20, grid=None, decision="no_plan", soc0=0.5, soc1=0.5),
    ]
    for row in rows:
        sink.append_slot(utc(2026, 10, 20, 10, 0), row)
    # A torn tail from an interrupted append must be ignored, not crash the report.
    path = next((data / "service" / "v1" / "slots").rglob("*.jsonl"))
    with path.open("a") as handle:
        handle.write('{"slot_start": "2026-10-20T11:00')
    sink.write_plan(
        {
            "plan_id": "p1",
            "generated_at": (S0 - timedelta(minutes=2)).isoformat(),
            "snapshot": {"terminal_value_eur_per_kwh": 0.2},
        }
    )
    sink.append_event(S0, {"kind": "solve_skipped", "at": S0.isoformat(), "reason": "busy"})
    return data


def test_report_hand_computed(store):
    report = build_shadow_report(store, since=S0, until=S0 + 4 * STEP, tariff=TARIFF, tz=TZ)
    o = report["overall"]
    assert report["comparison_basis"] == "native_battery"
    assert o["slots_expected"] == 4 and o["slots_recorded"] == 4
    assert o["slots_with_intent"] == 3 and o["coverage"] == pytest.approx(0.75)
    assert o["decisions"] == {"intent": 3, "no_plan": 1}
    assert o["planned_charge_kwh"] == pytest.approx(0.5)
    assert o["planned_discharge_kwh"] == pytest.approx(0.5)
    assert o["power_mae_w"] == pytest.approx(4000 / 3)
    assert o["power_bias_w"] == pytest.approx(0.0)
    # native: 0.1*0.3 + 0.3*0.4 + 0.3*0.1
    assert o["native_cost_eur"] == pytest.approx(0.18)
    # counterfactual: charge 0.5 -> grid 0.8; discharge 0.25 -> 0.15; discharge clipped to 0.1 -> 0
    assert o["counterfactual_cost_eur"] == pytest.approx(0.08 + 0.045 + 0.0)
    assert report["savings_eur"] == pytest.approx(0.055)
    assert o["clipped_kwh"] == pytest.approx(0.15)
    assert o["excluded_no_load"] == 1 and o["comparable_slots"] == 3
    assert o["soc_violations"] == 1
    assert o["solves_skipped"] == {"busy": 1}
    assert o["plans"] == 0  # made before the window: decides its slots, not counted in it
    # With idle drain (26 W = 0.026 kW), each 15-min interval loses 0.0065 kWh.
    # The "no_plan" interval has no load, so drain doesn't apply there (3 intervals).
    drain_per_interval = 0.026 * 900.0 / 3600.0
    stored = 0.5 * ETA - 0.25 / ETA - 0.1 / ETA - 3 * drain_per_interval
    assert report["inventory"]["counterfactual_stored_delta_kwh"] == pytest.approx(stored)
    assert report["savings_inventory_adjusted_eur"] == pytest.approx(0.055 + 0.2 * stored)
    assert report["replay"]["start_soc_frac"] == 0.5
    assert not report["exit_gate"]["met"]
    md = render_markdown(report)
    assert "# Shadow vs native report" in md and "## Assumptions" in md
    assert "| 2026-10-20 |" in md


def test_late_intent_replays_for_the_rest_of_the_slot_and_is_not_coverage(tmp_path):
    sink = ShadowSink(tmp_path)
    row = _row(
        S0, price=0.10, grid=0.30, intent=_intent("charge", 2000), soc0=0.5, soc1=0.5, battery_w=900
    )
    # Adopted 5 minutes into the slot: the intent was in force for 10 of its 15 minutes.
    sink.append_slot(S0, {**row, "decision": "late_intent", "adopted_after_s": 300.0})
    report = build_shadow_report(tmp_path, since=S0, until=S0 + STEP, tariff=TARIFF, tz=TZ)
    o = report["overall"]
    assert o["decisions"] == {"late_intent": 1}
    assert o["slots_with_intent"] == 0 and o["coverage"] == 0
    assert o["planned_charge_kwh"] == pytest.approx(0.5 * 2 / 3)
    assert o["power_bias_w"] == pytest.approx(2000 * 2 / 3 - 900)
    # counterfactual: load 0.3 + charge 1/3 kWh at 0.10
    assert o["counterfactual_cost_eur"] == pytest.approx(0.1 * (0.3 + 0.5 * 2 / 3))


def test_report_counts_dst_day_slots(tmp_path):
    since, until = local_day_bounds_utc(date(2026, 10, 25), TZ)
    report = build_shadow_report(tmp_path, since=since, until=until, tariff=TARIFF, tz=TZ)
    assert report["overall"]["slots_expected"] == 100
    assert report["overall"]["coverage"] == 0
    assert report["comparison_basis"] == "no_battery_pre_commissioning"
    since, until = local_day_bounds_utc(date(2026, 3, 29), TZ)
    report = build_shadow_report(tmp_path, since=since, until=until, tariff=TARIFF, tz=TZ)
    assert report["overall"]["slots_expected"] == 92


def test_parse_window_local_dates_and_default():
    since, until = parse_window("2026-10-25", "2026-10-26", TZ, utc(2026, 11, 1))
    assert (until - since) == timedelta(hours=25)
    since, until = parse_window(None, None, TZ, utc(2026, 11, 1, 12))
    assert until == local_day_bounds_utc(date(2026, 11, 1), TZ)[0]
    assert since == local_day_bounds_utc(date(2026, 10, 18), TZ)[0]
    with pytest.raises(ValueError):
        parse_window("2026-10-25T00:00:00", None, TZ, utc(2026, 11, 1))


def test_shadow_report_cli_json(store, tmp_path, capsys):
    config = write_service_config(tmp_path)
    code = main(
        [
            "shadow-report",
            "--data-dir", str(store),
            "--config", str(REPO / "config" / "tariff.yaml"),
            "--service-config", str(config),
            "--since", S0.isoformat(),
            "--until", (S0 + 4 * STEP).isoformat(),
            "--format", "json",
        ]
    )  # fmt: skip
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["savings_eur"] == pytest.approx(0.055)


def test_sink_is_append_only_and_private(store):
    plans = list((store / "service" / "v1" / "plans").rglob("*.json"))
    assert len(plans) == 1 and oct(plans[0].stat().st_mode & 0o777) == "0o600"
    rows = list(iter_records(store, "slots", S0, S0 + 4 * STEP))
    assert len(rows) == 4  # torn tail skipped
    sink = ShadowSink(store)
    with pytest.raises(FileExistsError):
        sink.write_plan(
            {
                "plan_id": "p1",
                "generated_at": (S0 - timedelta(minutes=2)).isoformat(),
                "snapshot": {"different": True},
            }
        )
