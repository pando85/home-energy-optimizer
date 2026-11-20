"""The preregistered section-A gates of the climate shadow (hand-computed values)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from battery_optimizer.climate import shadow_gates as g

PREREG = (
    Path(__file__).resolve().parents[1] / "docs" / "planning" / "climate-preregistration-2026-27.md"
)


def test_the_thresholds_are_the_preregistered_ones():
    text = " ".join(PREREG.read_text().split())  # the doc wraps lines
    table = {c.name: (c.op, c.threshold) for c in g.CRITERIA}
    assert table == {
        "rmse_6h": ("<=", 0.30),
        "rmse_12h": ("<=", 0.40),
        "skill_6h": (">=", 0.25),
        "skill_12h": (">=", 0.25),
        "abs_bias_6h": ("<=", 0.15),
        "daily_heat_mdape": ("<=", 0.15),
    }
    for phrase in (
        "| Room-mean RMSE at 6 h | at most 0.30 K |",
        "| Room-mean RMSE at 12 h | at most 0.40 K |",
        "| Skill versus persistence | at least 25% at **both** horizons |",
        "| Mean bias at 6 h | absolute value at most 0.15 K |",
        "median absolute percentage error at most 15%, on days whose heat is above 20% of the "
        "window's median daily heat",
        "at least 6 weeks of valid heating days",
        "go-live plus 7 days (settling) through **2027-03-31**",
    ):
        assert phrase in text, phrase
    assert (g.MIN_VALID_DAYS, g.SETTLING_DAYS, g.HEAT_DAY_SHARE) == (42, 7, 0.20)
    assert g.SEASON_END == date(2027, 3, 31)
    # the pre-data amendment of 2026-10-31 (one row per change, all before any data)
    for phrase in (
        "| 2026-10-31 | Room-mean (sections 1 and 2) |",
        "| 2026-10-31 | Prediction basis (section 2) |",
        "| 2026-10-31 | Daily heat-delivery error, new regime (section 2) |",
        "| 2026-10-31 | Go-live (section 1) |",
    ):
        assert phrase in text, phrase
    assert "| (none) | | | | |" not in text


def test_the_gate_room_mean_is_b0s_comfort_rooms_with_zero_offset():
    from battery_optimizer.climate.config import load_climate_config

    climate = load_climate_config(PREREG.parents[2] / "config" / "climate.yaml")
    assert g.GATE_ROOMS == ("bedroom", "hall_kitchen", "office_t") == tuple(climate.comfort.rooms)
    assert "office_a" not in g.GATE_ROOMS
    text = " ".join(PREREG.read_text().split())
    assert "bedroom, hall_kitchen and office_t" in text and "zero offset" in text


def test_windows_follow_go_live_and_the_settling_days():
    live = date(2026, 11, 9)
    assert g.window_of(date(2026, 11, 8), "legacy", live) == "legacy"
    assert g.window_of(date(2026, 11, 8), "unknown", live) is None
    assert g.window_of(date(2026, 11, 9), "comfort", live) is None  # settling
    assert g.window_of(date(2026, 11, 15), "comfort", live) is None
    assert g.window_of(date(2026, 11, 16), "comfort", live) == "new"
    assert g.window_of(date(2026, 11, 16), "legacy", live) is None  # rule back after go-live
    assert g.window_of(date(2027, 4, 1), "comfort", live) is None
    assert g.window_of(date(2027, 1, 1), "legacy", None) == "legacy"
    recs = [
        {"local_day": "2026-11-10", "regime": "comfort"},
        {"local_day": "2026-11-12", "regime": "comfort"},
    ]
    assert g.find_go_live(recs) == date(2026, 11, 10)


def test_records_written_as_legacy_after_the_live_switch_count_in_neither_window():
    """Production went live on 2026-11-18 (13:28 UTC) while records were still tagged
    ``legacy`` (the helper regime detection found no helper): with ``go_live: 2026-11-18`` those
    records are in the settling days, so they join neither window; the first ``comfort`` day
    that counts is go-live plus seven days."""
    live = date(2026, 11, 18)
    assert g.window_of(date(2026, 11, 17), "legacy", live) == "legacy"  # the legacy window
    for day in range(18, 25):  # 11-18 itself (both regimes of that day) through 11-24
        assert g.window_of(date(2026, 11, day), "legacy", live) is None
        assert g.window_of(date(2026, 11, day), "comfort", live) is None
    assert g.window_of(date(2026, 11, 25), "comfort", live) == "new"
    assert g.window_of(date(2026, 11, 25), "legacy", live) is None


def test_a_new_window_verdict_needs_a_configured_go_live():
    scores, days = _records(date(2026, 11, 1), 60, "comfort")
    report = g.evaluate(scores, days)
    assert report["go_live"] is None and report["go_live_provisional"] == "2026-11-01"
    new = report["windows"]["new"]
    assert new["verdict"] == "UNDETERMINED" and "go_live" in new["note"]
    assert set(new["criteria"].values()) == {"UNDETERMINED"}
    configured = g.evaluate(scores, days, go_live=date(2026, 11, 1))["windows"]["new"]
    assert configured["verdict"] == "PASS" and configured["valid_heating_days"] == 53


def test_only_the_named_lineage_is_evaluated():
    mine, days = _records(date(2026, 11, 1), 42, "legacy")
    other, _ = _records(date(2026, 11, 1), 42, "legacy", e6=0.9)
    for r in other:
        r["prior_id"] = "fedcba987654"
    report = g.evaluate(mine + other, days, prior_id="abcdef012345", mpc_config_sha256="cfg")
    assert report["windows"]["legacy"]["verdict"] == "PASS"
    assert report["lineages"] == {"abcdef012345/cfg": 42, "fedcba987654/cfg": 42}
    mixed = g.evaluate(mine + other, days)["windows"]["legacy"]
    assert mixed["criteria"]["rmse_6h"] == "FAIL"


def test_rmse_skill_and_bias_by_hand():
    rows = [
        {"room_mean": {"h6": {"heat": 0.1, "persistence": 0.5}}},
        {"room_mean": {"h6": {"heat": -0.3, "persistence": 0.5}}},
        {"room_mean": {"h6": {"heat": 0.2, "persistence": -0.5}}},
        {"room_mean": {"h6": {"heat": 0.9, "persistence": None}}},  # not on the same origins
    ]
    s = g._horizon_stats(rows, 6)
    assert s["n"] == 3
    assert s["rmse"] == pytest.approx(math.sqrt((0.01 + 0.09 + 0.04) / 3))
    assert s["rmse_persistence"] == pytest.approx(0.5)
    assert s["skill"] == pytest.approx(1 - math.sqrt(0.14 / 3) / 0.5)
    assert s["bias"] == pytest.approx(0.0)


def test_daily_heat_mdape_by_hand():
    def day(meas, pred):
        return {"midnight": True, "accuracy": {"day": {"q_meas_kwh": meas, "q_pred_kwh": pred}}}

    # median 9: the 1.0 day (below 20% of it) is not scored; APE 0.1, 0.25, 0 -> median 0.1
    rows = [day(10, 11), day(12, 9), day(8, 8), day(1, 3), {"midnight": False, "accuracy": {}}]
    out = g._daily_heat(rows)
    assert out == {"days": 4, "days_scored": 3, "mdape": pytest.approx(0.1)}


def _records(first: date, n: int, regime: str, e6=0.2, e12=0.3, heat=(10.0, 11.0)):
    scores, days = [], []
    for k in range(n):
        label = (first + timedelta(days=k)).isoformat()
        sign = 1 if k % 2 else -1
        scores.append(
            {
                "local_day": label,
                "regime": regime,
                "midnight": True,
                "room_mean": {
                    "h1": {"heat": 0.05, "persistence": 0.1},
                    "h6": {"heat": sign * e6, "persistence": sign * 0.4},
                    "h12": {"heat": sign * e12, "persistence": sign * 0.5},
                    "h24": {"heat": None, "persistence": 0.3},
                },
                "accuracy": {
                    "day": {"q_meas_kwh": heat[0], "q_pred_kwh": heat[1], "e_meas_kwh": 3.0}
                },
                "plan": {
                    "status": "optimal",
                    "solve_s": 1.0,
                    "heat_slots": 10,
                    "top_slots": 4,
                    "kwh": 3.3,
                },
                "prior_id": "abcdef012345",
                "mpc_config_sha256": "cfg",
            }
        )
        days.append({"day": label, "valid_heating_day": True, "regimes": [regime, "unknown"]})
    return scores, days


def test_a_window_passes_fails_or_stays_undetermined():
    scores, days = _records(date(2026, 11, 1), 42, "legacy")
    win = g.evaluate(scores, days)["windows"]["legacy"]
    assert win["verdict"] == "PASS", win["criteria"]
    m = win["metrics"]
    assert m["rmse_6h"] == pytest.approx(0.2) and m["skill_6h"] == pytest.approx(0.5)
    assert m["rmse_12h"] == pytest.approx(0.3) and m["skill_12h"] == pytest.approx(0.4)
    assert m["abs_bias_6h"] == pytest.approx(0.0) and m["daily_heat_mdape"] == pytest.approx(0.1)
    assert win["valid_weeks"] == pytest.approx(6.0)
    assert win["plan"]["top_offset_share"] == pytest.approx(0.4)
    assert win["plan"]["plan_over_rule_electricity"]["ratio"] == pytest.approx(1.1)

    short = g.evaluate(scores[:41], days[:41])["windows"]["legacy"]
    assert short["verdict"] == "UNDETERMINED"
    assert set(short["criteria"].values()) == {"UNDETERMINED"}

    worse, worse_days = _records(date(2026, 11, 1), 42, "legacy", e6=0.31)
    failed = g.evaluate(worse, worse_days)["windows"]["legacy"]
    assert failed["criteria"]["rmse_6h"] == "FAIL" and failed["verdict"] == "FAIL"
    assert failed["criteria"]["rmse_12h"] == "PASS"

    heat, heat_days = _records(date(2026, 11, 1), 42, "legacy", heat=(10.0, 11.6))
    assert (
        g.evaluate(heat, heat_days)["windows"]["legacy"]["criteria"]["daily_heat_mdape"] == "FAIL"
    )


def test_invalid_days_and_other_regimes_do_not_count():
    scores, days = _records(date(2026, 11, 1), 42, "legacy")
    days[0]["valid_heating_day"] = False
    win = g.evaluate(scores, days)["windows"]["legacy"]
    assert win["valid_heating_days"] == 41 and win["origins"] == 41
    assert win["verdict"] == "UNDETERMINED"
    # go-live on day 30: the legacy window keeps 29 days, the new one starts 7 days later
    live = date(2026, 11, 30)
    new, new_days = _records(live, 50, "comfort")
    report = g.evaluate(scores[:29] + new, days[:29] + new_days, go_live=live)
    assert report["go_live"] == report["go_live_provisional"] == live.isoformat()
    assert report["windows"]["legacy"]["valid_heating_days"] == 28  # day 0 is invalid
    assert report["windows"]["new"]["valid_heating_days"] == 43
    assert report["windows"]["new"]["first_day"] == "2026-12-07"
    assert report["windows"]["new"]["verdict"] == "PASS"
    md = g.render_markdown(report)
    assert "## new window: PASS" in md and "## legacy window: UNDETERMINED" in md


def test_the_cli_prints_the_gate_report_from_the_store(tmp_path, capsys):
    import json

    from battery_optimizer import cli
    from battery_optimizer.climate.mpc_config import load_mpc_config
    from battery_optimizer.service.sink import ShadowSink

    data = tmp_path / "data"
    sink = ShadowSink(data)
    scores, days = _records(date(2026, 11, 1), 42, "legacy")
    sha = load_mpc_config(PREREG.parents[2] / "config" / "climate-mpc.yaml").sha256
    for s in scores:  # the lineage of the committed MPC config
        s["mpc_config_sha256"] = sha
    for s in scores:
        at = datetime.fromisoformat(s["local_day"] + "T12:00:00+00:00")
        sink.append_climate("climate_scores", at, s)
    for d in days:
        at = datetime.fromisoformat(d["day"] + "T12:00:00+00:00")
        sink.append_climate("climate_days", at, d)
    service = Path(__file__).resolve().parents[1] / "config" / "service.yaml"
    args = ["climate-shadow-report", "--data-dir", str(data), "--service-config", str(service)]
    assert cli.main(args) == 0
    assert "## legacy window: PASS" in capsys.readouterr().out
    assert cli.main([*args, "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["windows"]["legacy"]["valid_heating_days"] == 42
    assert report["lineage"]["mpc_config_sha256"] == sha
    assert cli.main([*args, "--mpc-config-sha256", "0" * 64]) == 0  # another lineage: no rows
    assert "## legacy window: UNDETERMINED" in capsys.readouterr().out
    assert cli.main([*args, "--mpc-config-sha256", "0" * 64, "--all-lineages"]) == 0
    assert "## legacy window: PASS" in capsys.readouterr().out
