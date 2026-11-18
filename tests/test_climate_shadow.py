"""Live climate shadow (track B2): inputs, decisions, scoring, persistence and the job."""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from climate_shadow_fakes import (
    CCFG,
    MPC,
    SETTINGS,
    SITE,
    FakeSamples,
    forecast_record,
    house,
    prior,
    utc,
)

from battery_optimizer import extract_prom
from battery_optimizer.build import RawPoint
from battery_optimizer.climate import b2d, mpc_config, mpc_replay, shadow, thermal
from battery_optimizer.climate.mpc_model import Exogenous
from battery_optimizer.features import RawSeries
from battery_optimizer.service.climate_shadow import (
    ClimateShadowJob,
    _cut,
    latest_forecast,
    next_run_time,
)
from battery_optimizer.service.config import ClimateShadowSettings
from battery_optimizer.service.sink import ShadowSink, iter_records

TZ = ZoneInfo("Europe/Madrid")
# Monday 2026-11-16, local midnight is 23:00 UTC the day before
DAY = date(2026, 11, 16)
MIDNIGHT = utc(2026, 11, 15, 23)


def live_data(t0: datetime, days: int = 4, samples=None):
    start = t0 - timedelta(days=days)
    # samples up to and including t0, as the job's cut keeps them
    series = samples if samples is not None else house(start, t0 + timedelta(minutes=5))
    return shadow.build_live_data(
        series, start=start, end=t0 + timedelta(hours=24), config=CCFG, site=SITE
    )


def model_rec(sp):
    return {
        "params": dict(sp.prior.params),
        "model_hash": shadow.model_hash(sp.prior_id, sp.prior.params),
    }


# ---------------------------------------------------------------------------
# Regime and Home Assistant inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "exists", "synced", "expected"),
    [
        (None, False, True, "legacy"),
        ("on", True, True, "comfort"),
        ("off", True, True, "comfort_off"),
        ("unavailable", True, True, "unknown"),
        ("on", True, False, "unknown"),
        (None, False, False, "unknown"),
    ],
)
def test_regime_detection(state, exists, synced, expected):
    assert shadow.detect_regime(state, exists=exists, synced=synced) == expected


def _slots(start: datetime, n: int) -> np.ndarray:
    return np.array([int((start + timedelta(minutes=15 * k)).timestamp() * 1e6) for k in range(n)])


def _plan(**fields) -> str:
    """A helper value exactly as homelab ``comfort_control.py`` writes it."""
    return json.dumps(fields, separators=(",", ":"))


def _parse(text, day=DAY, start=MIDNIGHT, n=96):
    return shadow.parse_dhw_plan(text, starts_us=_slots(start, n), tz=TZ, day=day)


WINDOW = {"start": "2026-11-16T02:00:00+01:00", "end": "2026-11-16T03:00:00+01:00"}


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "unknown",
        "unavailable",
        "garbage",
        "[1, 2]",
        _plan(day="2026-11-15", **WINDOW, forced=True, target_written=True, done=None),  # stale
        _plan(done="2026-11-15"),  # yesterday finished, nothing about today
        _plan(day="2026-11-16", start=WINDOW["start"], end=None, done=None),  # half a window
        _plan(day="2026-11-16", start="2026-11-16T02:00:00", end="2026-11-16T03:00:00"),  # naive
    ],
)
def test_a_dhw_plan_that_says_nothing_about_today_is_none(text):
    assert _parse(text) is None


def test_the_dhw_plan_of_today_with_a_window():
    text = _plan(day="2026-11-16", **WINDOW, forced=True, target_written=False, done="2026-11-15")
    mask, covered = _parse(text)
    assert np.flatnonzero(mask).tolist() == [8, 9, 10, 11]  # local 02:00 to 02:45
    assert covered.all()  # every slot of the horizon is today's
    # a horizon from 10:00 holds today's rest (56 slots) and tomorrow's first 40
    mask, covered = _parse(text, start=MIDNIGHT + timedelta(hours=10))
    assert covered.sum() == 56 and not covered[56:].any() and not mask.any()


@pytest.mark.parametrize(
    "text",
    [
        # after the window the app writes start and end as null
        _plan(day="2026-11-16", start=None, end=None, forced=False, target_written=True, done=None),
        _plan(day="2026-11-16", start=None, end=None, done="2026-11-16"),
        _plan(done="2026-11-16"),  # today's window finished, the plan of the day cleared
    ],
)
def test_a_finished_or_empty_dhw_window_means_no_dhw_today(text):
    mask, covered = _parse(text)
    assert not mask.any() and covered.all()


def test_forecast_series_interpolates_and_holds_the_ends():
    rec = {
        "forecast": [
            {"datetime": "2026-11-16T00:00:00+00:00", "temperature": 4.0},
            {"datetime": "2026-11-16T01:00:00+00:00", "temperature": 8.0},
            {"datetime": "bad", "temperature": 1.0},
        ]
    }
    out = shadow.forecast_series(rec, _slots(utc(2026, 11, 15, 23, 30), 8))
    # midpoints 23:37.5 (held first), ..., 00:07.5 -> 4.5, 00:52.5 -> 7.5, 01:22.5 (held last)
    assert out[0] == 4.0 and out[2] == pytest.approx(4.5) and out[5] == pytest.approx(7.5)
    assert out[-1] == 8.0
    assert np.isnan(shadow.forecast_series(None, _slots(MIDNIGHT, 2))).all()


def test_the_latest_archived_forecast_is_read(tmp_path):
    sink = ShadowSink(tmp_path)
    old, new = utc(2026, 11, 15, 20, 5), utc(2026, 11, 15, 22, 5)
    sink.append_weather_forecast(old, forecast_record(old))
    sink.append_weather_forecast(new, forecast_record(new))
    later = utc(2026, 11, 15, 23, 5)
    sink.append_weather_forecast(later, forecast_record(later))  # after the run: never read
    # read at the decision t0 (23:00), not at the run: the 23:05 fetch comes after it
    got = latest_forecast(
        tmp_path, "weather.forecast_home", utc(2026, 11, 15, 23), timedelta(hours=3)
    )
    assert got["requested_at"] == new.isoformat()
    too_old = latest_forecast(
        tmp_path, "weather.forecast_home", utc(2026, 11, 16, 6), timedelta(hours=3)
    )
    assert too_old is None


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def test_a_decision_plans_from_the_forecast_and_reads_nothing_after_t0():
    sp = prior()
    t0 = MIDNIGHT + timedelta(hours=10)
    d = live_data(t0)
    fc = forecast_record(t0 - timedelta(minutes=55))
    inputs = shadow.LiveInputs(regime="legacy", forecast=fc)
    rec = shadow.decide(sp, model_rec(sp), d, MPC, t0, inputs, settings=SETTINGS)
    assert rec["status"] == "planned", rec.get("status")
    assert rec["inputs"]["weather"] == "forecast"
    assert rec["inputs"]["dhw_source"] == "history"  # no plan helper: yesterday's DHW slots
    # 03:00 UTC is local 04:00: 16 slots after t0 = 10:00 local is the next day's 04:00
    assert rec["inputs"]["dhw_slots"] == [72]
    plan = rec["plan"]
    assert len(plan["plan_on"]) == 96 and len(plan["plan_offset_k"]) == 96
    assert set(plan["plan_offset_k"]) <= set(MPC.water.offsets_k)
    assert len(rec["origin"]["x0"]) == 2
    # measured data after t0 changes nothing
    later = live_data(t0, samples=_perturbed_after(t0))
    rec2 = shadow.decide(sp, model_rec(sp), later, MPC, t0, inputs, settings=SETTINGS)
    assert rec2["origin"] == rec["origin"] and rec2["inputs"] == rec["inputs"]

    def plan_part(r):  # everything but the wall-clock solve times
        return {k: v for k, v in r["plan"].items() if k != "solves"}

    assert plan_part(rec2) == plan_part(rec)
    assert [x["status"] for x in rec2["plan"]["solves"]] == [
        x["status"] for x in rec["plan"]["solves"]
    ]


def _without(series, start: datetime, minutes: int):
    """*series* with every sample of ``[start, start + minutes)`` missing (a scrape outage)."""
    lo, hi = (int(x.timestamp() * 1e6) for x in (start, start + timedelta(minutes=minutes)))
    out = {}
    for k, s in series.items():
        keep = (s.t_us < lo) | (s.t_us >= hi)
        out[k] = RawSeries(s.entity, s.metric, s.unit, s.t_us[keep], s.values[keep])
    return out


@pytest.mark.parametrize(("minutes", "status"), [(16, "planned"), (180, "no_state")])
def test_a_short_scrape_outage_does_not_stop_the_observer(minutes, status):
    """Regression (2026-11-03): two slots without samples in the last 48 h restarted the observer
    and every decision was ``no_state`` for the next 24 h."""
    sp = prior()
    t0 = MIDNIGHT + timedelta(hours=10)
    start = t0 - timedelta(days=4)
    samples = _without(house(start, t0 + timedelta(minutes=5)), t0 - timedelta(hours=5), minutes)
    d = live_data(t0, samples=samples)
    inputs = shadow.LiveInputs(
        regime="legacy", forecast=forecast_record(t0 - timedelta(minutes=55))
    )
    rec = shadow.decide(sp, model_rec(sp), d, MPC, t0, inputs, settings=SETTINGS)
    assert rec["status"] == status, rec.get("status")
    # the 3 h outage is longer than the bridge: the observer restarts and waits for its warm-up
    assert ("origin" in rec) is (status == "planned")


def test_bridge_outages_holds_heat_over_short_runs_only():
    w = np.column_stack([np.arange(20.0), np.ones(20), np.ones(20), np.ones(20)])
    cooling = np.zeros(20, dtype=bool)
    w[3:5, 0] = np.nan  # two slots: bridged with the slot before
    w[8:14, 0] = np.nan  # six slots: left unknown
    w[16, 0] = np.nan  # not before ``upto``: left unknown
    out = shadow.bridge_outages(w, cooling, upto=16, max_slots=4)
    assert out[3:5, 0].tolist() == [2.0, 2.0]
    assert np.isnan(out[8:14, 0]).all() and np.isnan(out[16, 0])
    assert np.isnan(w[3, 0])  # the input is not changed
    cooling[3] = True  # a cooling slot is unknown by design
    assert np.isnan(shadow.bridge_outages(w, cooling, upto=16, max_slots=4)[3, 0])


def _perturbed_after(t0):
    data = house(t0 - timedelta(days=4), t0 + timedelta(hours=6))
    cut = int(t0.timestamp() * 1e6)
    for s in data.values():
        s.values[s.t_us >= cut] += 5.0
    return data


def test_the_plan_helper_and_away_reach_the_plan():
    sp = prior()
    t0 = MIDNIGHT + timedelta(hours=10)
    d = live_data(t0)
    window = {"start": "2026-11-16T12:00:00+01:00", "end": "2026-11-16T13:00:00+01:00"}
    plan = _plan(day="2026-11-16", **window, forced=True, target_written=True, done=None)
    inputs = shadow.LiveInputs(regime="comfort", dhw_plan=plan, away=True)
    rec = shadow.decide(sp, model_rec(sp), d, MPC, t0, inputs, settings=SETTINGS)
    assert rec["inputs"]["weather"] == "persistence"  # no forecast archived
    # today's slots from the plan, tomorrow's from yesterday's measured DHW (04:00 local)
    assert rec["inputs"]["dhw_source"] == "plan_today+history"
    assert rec["inputs"]["dhw_slots"] == [8, 9, 10, 11, 72]
    # a finished window: no DHW today, so only tomorrow's history slot (no double plan)
    done = _plan(day="2026-11-16", start=None, end=None, forced=False, target_written=True)
    rec = shadow.decide(
        sp, model_rec(sp), d, MPC, t0, replace(inputs, dhw_plan=done), settings=SETTINGS
    )
    assert rec["inputs"]["dhw_slots"] == [72]
    floor = MPC.comfort.floor_c + MPC.comfort.setpoint_margin_k
    assert all(v == floor for row in rec["inputs"]["setpoints"] for v in row)


def test_without_away_the_day_room_gets_its_optimum_start_setpoint():
    sp = prior()
    t0 = MIDNIGHT  # Monday 00:00: the 12:00 window, leads of hours before it
    d = live_data(t0)
    rec = shadow.decide(
        sp, model_rec(sp), d, MPC, t0, shadow.LiveInputs(regime="legacy"), settings=SETTINGS
    )
    hall = list(sp.prior.rooms).index("hall_kitchen")
    day_sp = MPC.comfort.day_min_c + MPC.comfort.setpoint_margin_k
    row = rec["inputs"]["setpoints"][hall]
    assert row[47] == day_sp and row[-1] < day_sp  # 11:45 in the early start; 23:45 not
    assert rec["midnight"] is True


# ---------------------------------------------------------------------------
# The per-room comfort helpers (read-only, from Prometheus)
# ---------------------------------------------------------------------------

AT = MIDNIGHT + timedelta(hours=10)


def _helper(entity: str, samples: list[tuple[timedelta, float]]) -> RawSeries:
    t = np.array([int((AT + dt).timestamp() * 1e6) for dt, _v in samples], dtype=np.int64)
    return RawSeries(
        entity, shadow.COMFORT_METRIC, "", t, np.array([v for _dt, v in samples], float)
    )


def _levels(**series):
    return shadow.comfort_helper_levels(series, AT)


def test_the_helpers_are_read_from_prometheus_by_entity():
    sources = shadow.live_sources(None)
    assert sources["comfort_floor_bedroom"] == (
        "input_number.comfort_floor_bedroom",
        shadow.COMFORT_METRIC,
    )
    assert sources["comfort_day_min_hall_kitchen"] == (
        "input_number.comfort_day_minimum_hall_kitchen",
        shadow.COMFORT_METRIC,
    )
    # two helpers x four rooms, plus the three window hours
    assert sum(k.startswith("comfort_") for k in sources) == 8 + 3
    for field, entity in {
        "start_weekday": "input_number.comfort_day_start_weekday_hour",
        "start_weekend": "input_number.comfort_day_start_weekend_hour",
        "end": "input_number.comfort_day_end_hour",
    }.items():
        assert sources[f"comfort_window_{field}"] == (entity, shadow.COMFORT_METRIC)


def _fake_prometheus(series: dict[tuple[str, str], float], seen: list[str]):
    """An HTTP client over a Prometheus holding ``{(metric name, entity): value}``, answering the
    ``__name__`` regex selector (anchored, as PromQL does) or a plain metric selector."""
    import re

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        seen.append(query)
        t = float(request.url.params["time"])
        if m := re.fullmatch(r'\{__name__=~"(.*)", entity="(.*)"\}\[\d+s\]', query):
            name, entity = re.compile(m[1]), m[2]
            hit = [k for k in series if name.fullmatch(k[0]) and k[1] == entity]
        else:
            m = re.fullmatch(r'(\w+)\{entity="(.*)"\}\[\d+s\]', query)
            hit = [k for k in series if k == (m[1], m[2])]
        result = [
            {"metric": {"__name__": k[0], "entity": k[1]}, "values": [[t - 60, str(series[k])]]}
            for k in hit
        ]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_comfort_helpers_are_read_whatever_unit_suffix_the_exporter_gives_the_metric():
    from battery_optimizer.service.history import fetch_points

    series = {
        ("hass_input_number_state_celsius", "input_number.comfort_floor_bedroom"): 21.0,
        ("hass_input_number_state_h", "input_number.comfort_day_end_hour"): 21.5,
        ("hass_input_number_state", "input_number.comfort_day_start_weekday_hour"): 13.0,
        ("hass_input_number_state_celsius", "input_number.comfort_floor_office_t"): 22.0,
        # not an input_number state: never read, even for a helper's entity
        ("hass_input_boolean_state", "input_number.comfort_day_start_weekend_hour"): 7.0,
        ("hass_sensor_state", "input_number.comfort_floor_hall_kitchen"): 9.0,
        ("my_hass_input_number_state_celsius", "input_number.comfort_floor_hall_kitchen"): 9.0,
    }
    seen: list[str] = []
    now = utc(2026, 11, 16, 12)
    sources = shadow.live_sources(None)

    def read(key):
        entity, metric = sources[key]
        with _fake_prometheus(series, seen) as client:
            return fetch_points(
                client, "http://prometheus.test", entity, metric, now - timedelta(hours=1), now
            )

    assert [p.value for p in read("comfort_floor_bedroom")] == [21.0]  # _celsius
    assert [p.value for p in read("comfort_window_end")] == [21.5]  # _h
    assert [p.value for p in read("comfort_window_start_weekday")] == [13.0]  # unit-less
    assert [p.value for p in read("comfort_floor_office_t")] == [22.0]
    assert read("comfort_window_start_weekend") == []  # a boolean state is not read
    assert read("comfort_floor_hall_kitchen") == []  # other metric names are not read
    assert all(q.startswith('{__name__=~"hass_input_number_state(_') for q in seen)


def test_the_regex_metric_selector_is_an_anchored_name_match():
    spec = extract_prom.EntitySpec("input_number.x", "hass_input_number_state(_[a-z]+)?", "")
    assert extract_prom.build_range_query(spec, 60) == (
        '{__name__=~"hass_input_number_state(_[a-z]+)?", entity="input_number.x"}[60s]'
    )
    plain = extract_prom.EntitySpec("input_number.x", "hass_input_number_state", "")
    assert extract_prom.build_range_query(plain, 60) == (
        'hass_input_number_state{entity="input_number.x"}[60s]'
    )


def test_absent_helper_series_fall_back_to_the_config():
    assert _levels() == {"floor": {}, "day_min": {}, "window": {}}
    empty = RawSeries("e", shadow.COMFORT_METRIC, "", np.zeros(0, np.int64), np.zeros(0))
    assert _levels(comfort_floor_bedroom=empty) == {"floor": {}, "day_min": {}, "window": {}}


def test_the_helper_value_in_effect_at_the_decision_is_used():
    minute = timedelta(minutes=1)
    got = _levels(
        comfort_floor_bedroom=_helper("f", [(-60 * minute, 21.0), (-5 * minute, 20.0)]),
        # a sample after the decision is never read
        comfort_floor_office_t=_helper("f", [(-5 * minute, 22.0), (+2 * minute, 19.0)]),
        comfort_day_min_hall_kitchen=_helper("d", [(-1 * minute, 24.5)]),
    )
    assert got["floor"] == {"bedroom": 20.0, "office_t": 22.0}
    assert got["day_min"] == {"hall_kitchen": 24.5}


@pytest.mark.parametrize(
    ("kind", "value", "kept"),
    [
        ("floor", 17.9, False),
        ("floor", 18.0, True),
        ("floor", 23.0, True),
        ("floor", 23.1, False),
        ("day_min", 17.9, False),
        ("day_min", 26.0, True),
        ("day_min", 26.1, False),
        ("floor", float("nan"), False),
    ],
)
def test_out_of_range_helper_values_are_ignored(kind, value, kept):
    series = {f"comfort_{kind}_bedroom": _helper("x", [(-timedelta(minutes=1), value)])}
    got = shadow.comfort_helper_levels(series, AT)
    assert (got[kind] == {"bedroom": value}) if kept else (got[kind] == {})


def test_a_stale_helper_sample_is_ignored():
    stale = _helper("x", [(-timedelta(hours=3), 22.0)])
    assert _levels(comfort_floor_bedroom=stale)["floor"] == {}


def test_helper_values_reach_the_plan_inputs_and_are_recorded():
    sp = prior()
    t0 = AT
    d = live_data(t0)
    legacy = shadow.LiveInputs(regime="legacy")
    base = shadow.decide(sp, model_rec(sp), d, MPC, t0, legacy, settings=SETTINGS)
    rooms = list(sp.prior.rooms)
    off = rooms.index("office_t")
    assert base["inputs"]["comfort"]["from_helpers"] == {}
    assert base["inputs"]["comfort"]["floor_c"] == [21.0, 21.0, 21.0]
    # no helper at all, or an empty read, is exactly the config
    nothing = shadow.decide(sp, model_rec(sp), d, MPC, t0, legacy, settings=SETTINGS, comfort={})
    assert nothing["inputs"] == base["inputs"]
    live = {"floor": {"office_t": 22.0}, "day_min": {"bedroom": 22.5}}
    rec = shadow.decide(sp, model_rec(sp), d, MPC, t0, legacy, settings=SETTINGS, comfort=live)
    c = rec["inputs"]["comfort"]
    assert c["rooms"] == rooms and c["floor_c"][off] == 22.0
    assert c["day_min_c"][rooms.index("bedroom")] == 22.5
    assert c["from_helpers"] == {"floor": ["office_t"], "day_min": ["bedroom"]}
    # the floor setpoint of office_t is 22.0 + margin at every slot; away uses the helper floors
    assert set(rec["inputs"]["setpoints"][off]) == {22.5}
    away = shadow.decide(
        sp, model_rec(sp), d, MPC, t0, replace(legacy, away=True), settings=SETTINGS, comfort=live
    )
    for i, row in enumerate(away["inputs"]["setpoints"]):
        assert set(row) == ({22.5} if i == off else {21.5})


# ---------------------------------------------------------------------------
# The day-window helpers (house-wide input_number hours)
# ---------------------------------------------------------------------------

MINUTE = timedelta(minutes=1)


def _window_series(**hours):
    return {
        f"comfort_window_{field}": _helper("w", [(-5 * MINUTE, h)]) for field, h in hours.items()
    }


def test_the_window_helper_values_in_effect_are_read():
    got = shadow.comfort_helper_levels(
        _window_series(start_weekday=14.0, start_weekend=10.5, end=21.25), AT
    )
    assert got["window"] == {"start_weekday": 14.0, "start_weekend": 10.5, "end": 21.25}
    after = {"comfort_window_end": _helper("w", [(-5 * MINUTE, 21.0), (+2 * MINUTE, 23.0)])}
    assert shadow.comfort_helper_levels(after, AT)["window"] == {"end": 21.0}


@pytest.mark.parametrize("value", [-0.25, 24.25, float("nan"), float("inf")])
def test_an_invalid_window_helper_value_is_ignored(value):
    assert shadow.comfort_helper_levels(_window_series(end=value), AT)["window"] == {}


@pytest.mark.parametrize("value", [0.0, 24.0])
def test_the_window_range_is_inclusive(value):
    assert shadow.comfort_helper_levels(_window_series(end=value), AT)["window"] == {"end": value}


def test_a_stale_window_helper_sample_is_ignored():
    stale = {"comfort_window_end": _helper("w", [(-timedelta(hours=3), 20.0)])}
    assert shadow.comfort_helper_levels(stale, AT)["window"] == {}


def _window(over=None, cfg=MPC):
    return mpc_replay.comfort_window(cfg, over)


def test_the_window_is_the_config_without_a_helper():
    w = _window()
    assert (w.start_weekday, w.start_weekend, w.end, w.from_helper) == (12.0, 9.0, 22.0, ())
    assert _window({}) == w


def test_the_window_helpers_replace_the_config_and_round_to_the_slot():
    w = _window({"start_weekday": 14.1, "start_weekend": 10.4, "end": 21.9})
    assert (w.start_weekday, w.start_weekend, w.end) == (14.0, 10.5, 22.0)
    assert w.from_helper == ("start_weekday", "start_weekend", "end")
    one = _window({"end": 20.0})
    assert (one.start_weekday, one.start_weekend, one.end) == (12.0, 9.0, 20.0)
    assert one.from_helper == ("end",)
    assert _window({"start_weekday": 0.0}).start_weekday == 0.0
    assert _window({"end": 24.0}).end == 24.0


@pytest.mark.parametrize(
    ("over", "expected", "used"),
    [
        # a start at or after the (config) end falls back to its config value
        ({"start_weekday": 22.0}, (12.0, 9.0, 22.0), ()),
        ({"start_weekend": 23.5}, (12.0, 9.0, 22.0), ()),
        # an unusable start does not cost a good helper end or the other start
        (
            {"start_weekday": 21.0, "start_weekend": 7.0, "end": 20.0},
            (12.0, 7.0, 20.0),
            ("start_weekend", "end"),
        ),
        # an end before a config start is the culprit: it falls back, the helper starts stand
        ({"end": 10.0}, (12.0, 9.0, 22.0), ()),
        ({"start_weekday": 15.0, "end": 8.0}, (15.0, 9.0, 22.0), ("start_weekday",)),
        # an end equal to a start is not a window
        ({"end": 12.0}, (12.0, 9.0, 22.0), ()),
        # rounding can make a start meet the end
        ({"start_weekday": 21.9, "end": 22.0}, (12.0, 9.0, 22.0), ("end",)),
    ],
)
def test_a_start_that_is_not_before_the_end_falls_back_to_the_config(over, expected, used):
    w = _window(over)
    assert (w.start_weekday, w.start_weekend, w.end) == expected
    assert w.from_helper == used
    assert w.start_weekday < w.end and w.start_weekend < w.end


def test_the_window_moves_the_comfort_minimum():
    rooms = ("hall_kitchen",)
    hours = np.array([8.0, 10.0, 12.25, 15.0, 20.0, 22.25])  # slot-end times
    wd = np.zeros(6, bool)
    cfg = mpc_replay.comfort_minimum(rooms, hours, wd, MPC)
    lv = mpc_replay.comfort_levels(rooms, MPC, {"window": {"start_weekday": 9.0, "end": 20.0}})
    moved = mpc_replay.comfort_minimum(rooms, hours, wd, MPC, lv)
    assert (cfg[0] == lv.day[0]).tolist() == [False, False, True, True, True, False]
    assert (moved[0] == lv.day[0]).tolist() == [False, True, True, True, False, False]


def _decide(t0, comfort=None, cfg=MPC):
    sp = prior()
    legacy = shadow.LiveInputs(regime="legacy")
    return shadow.decide(
        sp, model_rec(sp), live_data(t0), cfg, t0, legacy, settings=SETTINGS, comfort=comfort
    )


def test_the_optimum_start_follows_the_helper_window_start():
    base = _decide(MIDNIGHT)
    helped = _decide(MIDNIGHT, {"window": {"start_weekday": 8.0}})
    # the helper start is exactly a config start of 08:00
    cfg8 = mpc_config.override(MPC, ("comfort", "day_start_weekday_hour"), 8)
    assert helped["inputs"]["setpoints"] == _decide(MIDNIGHT, None, cfg8)["inputs"]["setpoints"]
    assert helped["inputs"]["setpoints"] != base["inputs"]["setpoints"]
    sp = prior()
    hall = list(sp.prior.rooms).index("hall_kitchen")
    day_sp = MPC.comfort.day_min_c + MPC.comfort.setpoint_margin_k

    def raised(rec):
        return [i for i, v in enumerate(rec["inputs"]["setpoints"][hall]) if v == day_sp]

    assert raised(helped)[0] < raised(base)[0]  # earlier window, earlier optimum start
    assert raised(helped)[-1] == 4 * 22 - 1 and raised(base)[-1] == 4 * 22 - 1  # unchanged end
    # a later end shortens nothing before it and extends the raised block to 23:45
    late = _decide(MIDNIGHT, {"window": {"end": 24.0}})
    assert raised(late)[-1] == 95
    # a weekday start the config rejects (not before the end) changes nothing
    assert _decide(MIDNIGHT, {"window": {"start_weekday": 23.0}})["inputs"] == base["inputs"]


def test_window_helpers_are_recorded_per_decision_and_leave_the_config_hash_alone():
    base = _decide(MIDNIGHT)
    assert base["inputs"]["comfort"]["window_h"] == {
        "start_weekday": 12.0,
        "start_weekend": 9.0,
        "end": 22.0,
    }
    assert base["inputs"]["comfort"]["from_helpers"] == {}
    over = {"floor": {"office_t": 22.0}, "window": {"start_weekday": 14.0, "end": 21.0}}
    rec = _decide(MIDNIGHT, over)
    c = rec["inputs"]["comfort"]
    assert c["window_h"] == {"start_weekday": 14.0, "start_weekend": 9.0, "end": 21.0}
    assert c["from_helpers"] == {"floor": ["office_t"], "window": ["end", "start_weekday"]}
    # a rejected window helper is not recorded as used
    bad = _decide(MIDNIGHT, {"window": {"start_weekday": 23.0}})
    assert bad["inputs"]["comfort"]["from_helpers"] == {}
    # the helper values are inputs, never part of the configuration identity
    assert rec["mpc_config_sha256"] == base["mpc_config_sha256"] == MPC.sha256


# ---------------------------------------------------------------------------
# Scoring: hand-computed values
# ---------------------------------------------------------------------------


def test_scoring_due_waits_for_the_day_of_the_last_horizon_slot():
    t0 = MIDNIGHT + timedelta(hours=10)  # last slot 09:45 local the next day
    assert not shadow.scoring_due(t0, 24, MIDNIGHT + timedelta(days=1, hours=23), TZ)
    assert shadow.scoring_due(t0, 24, MIDNIGHT + timedelta(days=2), TZ)
    assert shadow.scoring_due(MIDNIGHT, 24, MIDNIGHT + timedelta(days=1), TZ)


def test_the_gate_error_is_the_heat_driven_comfort_room_mean_with_zero_offset():
    sp = prior({"office_a": 1.2, "bedroom": -0.4})
    t0 = MIDNIGHT + timedelta(hours=10)
    d0 = live_data(t0)
    dec = shadow.decide(
        sp, model_rec(sp), d0, MPC, t0, shadow.LiveInputs(regime="legacy"), settings=SETTINGS
    )
    d = live_data(t0 + timedelta(days=2), days=6)
    rec = shadow.score(sp, dec, d, MPC)
    assert rec is not None and rec["action"] == "permission"
    assert rec["gate_rooms"] == ["bedroom", "hall_kitchen", "office_t"]
    # by hand: simulate the model from x0 with the measured heat and outdoor temperature
    i = int(np.searchsorted(d.starts_us, int(t0.timestamp() * 1e6)))
    model = sp.prior.model()
    # the outdoor series of the prior fit and the gate: t_out_model, the last reading held
    t_out = np.where(np.isfinite(d.t_out_model), d.t_out_model, mpc_replay.causal_outdoor(d))
    assert np.allclose(shadow.outdoor_series(d), t_out, equal_nan=True)
    w = model.inputs(
        np.where(d.dhw, 0.0, d.q_heat_kw)[i : i + 96],
        Exogenous(
            t_out[i : i + 96],
            d.solar[i : i + 96],
            b2d.diurnal_matrix(*b2d.slot_calendar(d), 2)[i : i + 96],
            d.local_hour[i : i + 96],
            np.zeros((3, 96)),
        ),
    )
    xs = thermal.simulate(model.ad, w @ model.bd.T, np.array(dec["origin"]["x0"]))
    air = np.vstack([d.room_temp[r] for r in ("bedroom", "hall_kitchen", "office_t")]).mean(0)
    assert np.allclose(air, d.t_air, equal_nan=True)
    four = np.vstack([d.room_temp[r] for r in shadow.DESIGN_ROOMS]).mean(axis=0)
    offset = (1.2 - 0.4) / 4  # the prior's offsets reach the four-room diagnostic only
    for hh in (1, 6, 12, 24):
        j = 4 * hh - 1
        cell = rec["room_mean"][f"h{hh}"]
        assert cell["heat"] == pytest.approx(xs[j + 1, 0] - air[i + j], abs=1e-9)
        assert cell["persistence"] == pytest.approx(air[i - 1] - air[i + j], abs=1e-9)
        diag = rec["design_rooms"][f"h{hh}"]
        assert diag["heat"] == pytest.approx(xs[j + 1, 0] + offset - four[i + j], abs=1e-9)
        assert diag["persistence"] == pytest.approx(four[i - 1] - four[i + j], abs=1e-9)
    # the same, with the weather the plan used (no forecast archived: the reading held)
    assert dec["inputs"]["weather"] == "persistence"
    assert rec["room_mean"]["h6"]["heat_plan_weather"] is not None


def test_no_live_signal_switches_source():
    # live_sources fetches Signal.entity only; a signal with successors (the servers since they
    # moved to the battery backup port) would need every entity of its windows fetched.
    assert not any(s.successors for s in shadow.LIVE_SIGNALS)
    assert "servers" not in {s.alias for s in shadow.LIVE_SIGNALS}


def test_a_decision_without_an_origin_is_not_scored():
    assert shadow.score(prior(), {"t0": MIDNIGHT.isoformat()}, None, MPC) is None


def test_day_validity_follows_the_preregistration():
    d = live_data(MIDNIGHT + timedelta(days=2), days=4)
    j = next(k for k, (label, _l, _h) in enumerate(d.days) if label == DAY)
    rec = shadow.day_record(d, j)
    assert rec["valid_heating_day"] and rec["space_heat_slots"] > 0
    assert rec["coverage"] == {"rooms": 1.0, "energy": 1.0, "outdoor": 1.0}

    def without(alias):
        series = house(MIDNIGHT - timedelta(days=2), MIDNIGHT + timedelta(days=2))
        key = next(s.source for s in shadow.LIVE_SIGNALS if s.alias == alias)
        s = series[key]
        keep = s.t_us < int((MIDNIGHT + timedelta(hours=20)).timestamp() * 1e6)
        series[key] = type(s)(s.entity, s.metric, s.unit, s.t_us[keep], s.values[keep])
        d2 = live_data(MIDNIGHT + timedelta(days=2), samples=series)
        j2 = next(k for k, (label, _l, _h) in enumerate(d2.days) if label == DAY)
        return shadow.day_record(d2, j2)

    # office A is not a gate room: its gap does not matter
    assert without("room_office_a_temp")["valid_heating_day"]
    # a gate room missing for 4 h: below 90% coverage, not a valid day
    bad = without("room_bedroom_temp")
    assert bad["coverage"]["rooms"] < 0.9 and not bad["valid_heating_day"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_the_prior_round_trips_and_is_content_addressed(tmp_path):
    sp = prior({"office_a": 1.5})
    path = shadow.save_prior(tmp_path, sp)
    assert path.parent.name == sp.prior_id
    back = shadow.load_prior(tmp_path, sp.prior_id[:6])
    assert back.to_dict() == sp.to_dict() and back.prior_id == sp.prior_id
    assert back.room_mean_offsets(["office_a", "bedroom"])[0] == pytest.approx(0.75)
    tampered = json.loads(path.read_text())
    tampered["params"]["ua"] = 0.2
    path.chmod(0o600)
    path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="hashes to"):
        shadow.load_prior(tmp_path, sp.prior_id)


def test_climate_records_round_trip_through_the_sink(tmp_path):
    sink = ShadowSink(tmp_path)
    at = utc(2026, 11, 16, 0, 10)
    rec = {"kind": "climate_score", "t0": at.isoformat(), "room_mean": {"h6": {"heat": 0.1}}}
    sink.append_climate("climate_scores", at, rec)
    with pytest.raises(ValueError):
        sink.append_climate("plans", at, rec)
    got = list(iter_records(tmp_path, "climate_scores", at, at + timedelta(hours=1)))
    assert got == [{"schema_version": 1, **rec}]


# ---------------------------------------------------------------------------
# The job with fake data sources
# ---------------------------------------------------------------------------


def make_job(tmp_path, samples, **settings):
    return ClimateShadowJob(
        settings=ClimateShadowSettings(enabled=True, prior="aaaaaa", **settings),
        climate_config=CCFG,
        mpc_config=MPC,
        prior=prior(),
        site=SITE,
        sink=ShadowSink(tmp_path),
        data_dir=tmp_path,
        samples=samples,
        code_version="test",
        plan_settings=SETTINGS,
    )


def read(tmp_path, kind):
    return list(iter_records(tmp_path, kind, utc(2026, 10, 1), utc(2027, 1, 1)))


def test_next_run_is_the_boundary_plus_the_offset():
    assert next_run_time(utc(2026, 11, 16, 9, 59), timedelta(hours=1), 10) == utc(
        2026, 11, 16, 10, 10
    )
    assert next_run_time(utc(2026, 11, 16, 10, 10), timedelta(hours=1), 10) == utc(
        2026, 11, 16, 11, 10
    )


def test_the_job_decides_refits_scores_and_resumes(tmp_path):
    samples = FakeSamples(MIDNIGHT - timedelta(days=12), MIDNIGHT + timedelta(days=3))
    job = make_job(tmp_path, samples)
    legacy = shadow.LiveInputs(regime="legacy")
    runs = [MIDNIGHT, MIDNIGHT + timedelta(hours=10), MIDNIGHT + timedelta(days=1)]
    for at in runs:
        summary = job.run_once(at + timedelta(minutes=10), legacy)
        assert summary["status"] == "planned", summary
    assert job.run_once(runs[-1] + timedelta(minutes=20), legacy)["status"] == "skipped"
    decisions = read(tmp_path, "climate_decisions")
    assert [d["t0"] for d in decisions] == [r.isoformat() for r in runs]
    assert all(d["regime"] == "legacy" and d["code_version"] == "test" for d in decisions)
    models = read(tmp_path, "climate_models")
    assert len(models) == 1 and models[0]["window"]["days"] == 7
    assert decisions[0]["model_hash"] == models[0]["model_hash"]
    # the midnight origin of DAY is scored at the next midnight, the 10:00 one not yet
    scores = read(tmp_path, "climate_scores")
    assert [s["t0"] for s in scores] == [MIDNIGHT.isoformat()]
    assert "day" in scores[0]["accuracy"] and scores[0]["plan"]["kwh"] is not None
    days = read(tmp_path, "climate_days")
    assert [d["day"] for d in days] == [DAY.isoformat()] and days[0]["regimes"] == ["legacy"]
    # a restarted job resumes the model and the queue, and scores the rest when due
    again = make_job(tmp_path, samples)
    again.run_once(MIDNIGHT + timedelta(days=2, minutes=10), legacy)
    assert len(read(tmp_path, "climate_models")) == 1  # the weekly refit is not due yet
    scored = {s["t0"] for s in read(tmp_path, "climate_scores")}
    assert scored == {r.isoformat() for r in runs}
    snap = again.snapshot()
    assert snap["model_hash"] == models[0]["model_hash"] and snap["queue"] == 1
    assert snap["report"]["windows"]["legacy"]["verdict"] == "UNDETERMINED"


def test_the_job_reads_the_helpers_and_falls_back_without_them(tmp_path):
    samples = FakeSamples(MIDNIGHT - timedelta(days=12), MIDNIGHT + timedelta(days=3))
    legacy = shadow.LiveInputs(regime="legacy")
    at = MIDNIGHT + timedelta(hours=10)
    bare = make_job(tmp_path / "bare", samples)
    assert bare.run_once(at + timedelta(minutes=10), legacy)["status"] == "planned"

    def with_helpers(now):
        out = dict(samples(now))
        t = np.arange(int((MIDNIGHT - timedelta(days=1)).timestamp()), int(now.timestamp()), 60)
        out["comfort_floor_office_t"] = RawSeries(
            "input_number.comfort_floor_office_t",
            shadow.COMFORT_METRIC,
            "",
            t * 1_000_000,
            np.where(t < int(at.timestamp()) - 1800, 21.0, 22.0),
        )
        return out

    live = make_job(tmp_path / "live", with_helpers)
    assert live.run_once(at + timedelta(minutes=10), legacy)["status"] == "planned"
    (b,) = read(tmp_path / "bare", "climate_decisions")
    (a,) = read(tmp_path / "live", "climate_decisions")
    assert b["inputs"]["comfort"]["from_helpers"] == {}
    assert a["inputs"]["comfort"]["from_helpers"] == {"floor": ["office_t"]}
    assert a["inputs"]["comfort"]["floor_c"] == [21.0, 21.0, 22.0]  # the value in effect at t0


def test_a_data_failure_is_recorded_not_raised(tmp_path):
    def broken(_now):
        raise OSError("prometheus down")

    job = make_job(tmp_path, broken)
    summary = job.run_once(MIDNIGHT + timedelta(minutes=10), shadow.LiveInputs(regime="unknown"))
    assert summary["status"] == "error"
    (rec,) = read(tmp_path, "climate_decisions")
    assert rec["status"] == "error" and "prometheus down" in rec["error"]


# ---------------------------------------------------------------------------
# The Prometheus sample cache and the cut at t0
# ---------------------------------------------------------------------------


class FakePrometheus:
    """``fetch_points`` over minute samples of two entities; can fail once for one entity."""

    def __init__(self, start: datetime, end: datetime) -> None:
        self.t = [
            start + timedelta(minutes=k) for k in range(int((end - start).total_seconds() // 60))
        ]
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.fail: str | None = None

    def __call__(self, _client, _base, entity, _metric, start, end):
        self.calls.append((entity, start, end))
        if entity == self.fail:
            self.fail = None
            raise OSError("prometheus timeout")
        value = 1.0 if entity == "sensor.a" else 2.0
        return [RawPoint(ts=t, value=value, series_key="k") for t in self.t if start <= t < end]


@pytest.fixture
def prometheus(monkeypatch):
    from battery_optimizer.service import climate_shadow as module

    fake = FakePrometheus(utc(2026, 11, 16, 0), utc(2026, 11, 16, 12))
    monkeypatch.setattr(module, "fetch_points", fake)
    cache = module.PrometheusClimateSamples(
        base_url="http://prometheus.test",
        timeout_s=1.0,
        sources={"a": ("sensor.a", "m"), "b": ("sensor.b", "m")},
        window=timedelta(hours=2),
        client_factory=lambda: contextlib.nullcontext(object()),
    )
    return fake, cache


def _times(series):
    return series.t_us.tolist()


def test_the_sample_cache_fetches_incrementally_and_drops_the_old(prometheus):
    fake, cache = prometheus
    t = utc(2026, 11, 16, 6)
    first = cache(t)
    assert {c[1] for c in fake.calls} == {t - timedelta(hours=2)}  # the whole window once
    assert len(first["a"].t_us) == 120 and set(first["b"].values) == {2.0}
    fake.calls.clear()
    second = cache(t + timedelta(minutes=30))
    # only from one hour before the last fetch's end (the overlap is replaced, not doubled)
    assert {c[1] for c in fake.calls} == {t - timedelta(hours=1)}
    times = _times(second["a"])
    assert len(times) == 120 == len(set(times)) and times == sorted(times)
    assert times[0] == int((t - timedelta(minutes=90)).timestamp() * 1e6)  # older ones dropped


def test_a_partial_fetch_failure_leaves_a_consistent_cache(prometheus):
    fake, cache = prometheus
    t = utc(2026, 11, 16, 6)
    cache(t)
    fake.fail = "sensor.b"
    with pytest.raises(OSError):
        cache(t + timedelta(minutes=30))
    fake.calls.clear()
    out = cache(t + timedelta(minutes=60))
    # the failed refresh did not advance the cache: the next one starts from the old end again
    assert {c[1] for c in fake.calls} == {t - timedelta(hours=1)}
    for key in ("a", "b"):
        times = _times(out[key])
        assert len(times) == 120 == len(set(times)) and times == sorted(times)


def test_the_cut_keeps_the_first_sample_at_or_after_t0_only():
    t0 = utc(2026, 11, 16, 6)
    us = [int((t0 + timedelta(minutes=m)).timestamp() * 1e6) for m in (-10, -5, 2, 7)]
    series = {"x": RawSeries("e", "m", "", np.array(us), np.array([1.0, 2.0, 3.0, 4.0]))}
    out = _cut(series, t0)["x"]
    assert out.t_us.tolist() == us[:3] and out.values.tolist() == [1.0, 2.0, 3.0]
    exact = _cut(series, t0 - timedelta(minutes=5))["x"]
    assert exact.t_us.tolist() == us[:2]


def test_resume_and_gates_keep_one_lineage(tmp_path):
    sink = ShadowSink(tmp_path)
    job = make_job(tmp_path, FakeSamples(MIDNIGHT - timedelta(days=12), MIDNIGHT))
    at = MIDNIGHT - timedelta(hours=3)
    own = {"prior_id": job.prior.prior_id, "mpc_config_sha256": MPC.sha256}
    other = {"prior_id": "0123456789ab", "mpc_config_sha256": MPC.sha256}
    for i, lineage in enumerate((own, other)):
        t0 = (at + timedelta(hours=i)).isoformat()
        rec = {"t0": t0, "local_day": "2026-11-15", "regime": "legacy", "origin": {"x0": [1, 2]}}
        sink.append_climate("climate_decisions", at, {**rec, **lineage})
        sink.append_climate("climate_scores", at, {**rec, **lineage, "room_mean": {}})
    sink.append_climate(
        "climate_decisions",
        at,
        {
            **other,
            "t0": (at + timedelta(hours=2)).isoformat(),
            "local_day": "2026-11-15",
            "regime": "legacy",
            "origin": {"x0": [1, 2]},
        },
    )
    job.load(MIDNIGHT)
    assert job.queue == {}  # the other prior's unscored decision is never scored with this prior
    from battery_optimizer.climate import shadow_gates

    report = shadow_gates.evaluate(
        job.scores, [], prior_id=job.prior.prior_id, mpc_config_sha256=MPC.sha256
    )
    assert sum(report["lineages"].values()) == 2 and len(report["lineages"]) == 2
