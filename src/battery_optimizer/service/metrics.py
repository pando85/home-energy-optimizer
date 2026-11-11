"""Prometheus metrics (prefix ``battery_optimizer_``) on a private registry.

Labels are bounded by configuration (roles, configured entity IDs, fixed reason
and result enums).  No plan ID, timestamp, price or household reading is ever a
label: plan identity lives in the store and ``/plan``, not in time-series
cardinality.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

__all__ = ["NOT_READY_REASONS", "ServiceMetrics"]

PREFIX = "battery_optimizer"

#: Every reason the service can report for not being ready (fixed label set).
NOT_READY_REASONS: tuple[str, ...] = (
    "ha_unconfigured",
    "ha_disconnected",
    "ha_not_synced",
    "grid_power_stale",
    "soc_unconfigured",
    "soc_stale",
    "prices_missing",
    "prices_insufficient",
    "history_unavailable",
    "forecast_unavailable",
    "tariff_invalid",
    "no_recent_plan",
)


class _RuntimeCollector(Collector):
    """Values computed at scrape time from the runtime's state."""

    def __init__(self, snapshot: Callable[[], dict[str, Any]]) -> None:
        self._snapshot = snapshot

    def collect(self) -> Iterable[Metric]:
        snap = self._snapshot()
        ha = snap.get("ha", {})

        def gauge(name: str, doc: str, value: float | None, labels: dict[str, str] | None = None):
            family = GaugeMetricFamily(f"{PREFIX}_{name}", doc, labels=list((labels or {}).keys()))
            if value is not None:
                family.add_metric(list((labels or {}).values()), float(value))
            return family

        yield gauge("ready", "1 when inputs are fresh and a recent plan exists", snap["ready"])
        yield gauge("inputs_ready", "1 when every planning input is fresh", snap["inputs_ready"])
        reasons = GaugeMetricFamily(
            f"{PREFIX}_not_ready_reason", "1 for each active not-ready reason", labels=["reason"]
        )
        active = set(snap.get("reasons", ()))
        for reason in NOT_READY_REASONS:
            reasons.add_metric([reason], 1.0 if reason in active else 0.0)
        yield reasons

        yield gauge(
            "ha_connected", "1 while the HA WebSocket is authenticated", ha.get("connected")
        )
        yield gauge("ha_synced", "1 after the post-connect get_states resync", ha.get("synced"))
        for name, key, doc in (
            ("ha_reconnects", "reconnects", "WebSocket reconnect attempts"),
            ("ha_resyncs", "resyncs", "Full get_states resynchronisations"),
            ("ha_state_events", "events", "state_changed events received for tracked entities"),
        ):
            family = CounterMetricFamily(f"{PREFIX}_{name}", doc)
            family.add_metric([], float(ha.get(key, 0)))
            yield family
        failures = CounterMetricFamily(
            f"{PREFIX}_ha_failures", "HA session failures by stage", labels=["stage"]
        )
        for stage in (
            "connect",
            "auth",
            "subscribe",
            "get_states",
            "heartbeat",
            "disconnect",
            "protocol",
        ):
            failures.add_metric([stage], float(ha.get("failures", {}).get(stage, 0)))
        yield failures
        updates = CounterMetricFamily(
            f"{PREFIX}_ha_state_updates",
            "Entity observations by classification (accepted, refreshed, duplicate, out_of_order)",
            labels=["result"],
        )
        for result in ("accepted", "refreshed", "duplicate", "out_of_order"):
            updates.add_metric([result], float(snap.get("updates", {}).get(result, 0)))
        yield updates

        ages = GaugeMetricFamily(
            f"{PREFIX}_input_age_seconds",
            "Seconds since HA last reported the entity (absent if never seen)",
            labels=["role", "entity"],
        )
        for item in snap.get("inputs", ()):
            if item.get("age_s") is not None:
                ages.add_metric([item["role"], item["entity"]], float(item["age_s"]))
        yield ages
        stale = GaugeMetricFamily(
            f"{PREFIX}_input_stale",
            "1 when the input is missing or older than its limit",
            labels=["role", "entity"],
        )
        for item in snap.get("inputs", ()):
            stale.add_metric([item["role"], item["entity"]], 0.0 if item.get("fresh") else 1.0)
        yield stale

        yield gauge(
            "price_horizon_seconds",
            "Seconds from now to the end of the last published price",
            snap.get("price_horizon_s"),
        )
        yield gauge(
            "prices_tomorrow_published",
            "1 when next-day PVPC prices are present",
            snap.get("tomorrow_published"),
        )
        yield gauge(
            "plan_age_seconds",
            "Seconds since the active plan was generated",
            snap.get("plan_age_s"),
        )
        yield gauge(
            "plan_horizon_slots", "Slots covered by the latest plan", snap.get("plan_slots")
        )
        yield gauge(
            "current_intent_power_watts",
            "Shadow intent for the current slot: + charge, - discharge (never sent)",
            snap.get("current_intent_w"),
        )
        yield gauge(
            "current_intent_active",
            "1 when a valid shadow intent covers now",
            snap.get("current_intent_active"),
        )
        yield gauge(
            "solve_in_progress", "1 while the planner worker is busy", snap.get("worker_busy")
        )
        yield gauge(
            "history_usable_slots",
            "Usable gross-load slots in the forecast history",
            snap.get("history_usable_slots"),
        )
        yield gauge(
            "history_age_seconds",
            "Seconds since the load history was fetched",
            snap.get("history_age_s"),
        )
        yield gauge(
            "terminal_value_eur_per_kwh",
            "EUR/DC-kWh value of end-of-horizon SOC used by the planner",
            snap.get("terminal_value"),
        )
        yield gauge(
            "virtual_battery_soc_ratio",
            "SOC of the open-loop virtual (counterfactual) battery",
            snap.get("virtual_soc"),
        )
        yield gauge(
            "observer_late_samples",
            "Samples that arrived after their slot had been closed",
            snap.get("late_samples"),
        )
        yield from _slot_plan_families(snap.get("slot_plans"))
        climate = snap.get("climate_shadow")
        if climate is not None:
            yield from _climate_families(climate)
        climate_plan = snap.get("climate_plan")
        if climate_plan is not None:
            yield from _climate_plan_families(climate_plan)
        health = snap.get("battery_health")
        if health is not None:
            yield from _health_families(health)
        actuator = snap.get("actuator")
        if actuator is not None:
            yield from _actuator_families(actuator)
        full_charge = snap.get("full_charge")
        if full_charge is not None:
            yield from _full_charge_families(full_charge)
        thermal = snap.get("thermal")
        if thermal is not None:
            yield from _thermal_families(thermal)


#: Values of the ``plan`` label: the first plan that covered a slot, and the plan of record.
PLAN_LABELS = ("day_ahead", "final")


def _slot_plan_families(plans: dict[str, Any] | None) -> Iterable[Metric]:
    """Gauges of the slot in progress, per plan (``battery_optimizer_slot_*``).

    Frozen at the slot open; no sample for a plan the slot does not have (no day-ahead plan after
    a first deploy, no final intent for a slot without a decision), and none before the first
    slot has opened.  Battery power is signed like ``current_intent_power_watts``: + charge.
    """
    specs = (
        ("battery_power_watts", "Planned battery power of the slot: + charge, - discharge"),
        ("soc_target_ratio", "Planned SOC at the end of the slot"),
        ("load_forecast_watts", "Forecast mean house load over the slot"),
        (
            "plan_lead_seconds",
            "Slot start minus the plan's generation time (negative: made inside the slot)",
        ),
    )
    values: dict[str, Callable[[Any], float | None]] = {
        "battery_power_watts": lambda p: p.signed_power_w,
        "soc_target_ratio": lambda p: p.soc_target_frac,
        "load_forecast_watts": lambda p: p.load_forecast_w,
        "plan_lead_seconds": lambda p: p.lead_seconds,
    }
    for suffix, doc in specs:
        family = GaugeMetricFamily(f"{PREFIX}_slot_{suffix}", doc, labels=["plan"])
        for label in PLAN_LABELS:
            slot_plan = (plans or {}).get(label)
            value = None if slot_plan is None else values[suffix](slot_plan)
            if value is not None:
                family.add_metric([label], float(value))
        yield family


def _full_charge_families(f: dict[str, Any]) -> Iterable[Metric]:
    """The periodic full charge's scrape-time gauges (``battery_optimizer_full_charge_``)."""
    from .config import FULL_CHARGE_MODES
    from .full_charge import ALERT_REASONS, CADENCE_CLASSES, STATES

    name = f"{PREFIX}_full_charge"

    def gauge(suffix: str, doc: str, value: float | None) -> GaugeMetricFamily:
        family = GaugeMetricFamily(f"{name}_{suffix}", doc, labels=[])
        if value is not None:
            family.add_metric([], float(value))
        return family

    mode = GaugeMetricFamily(
        f"{name}_mode",
        "1 for full_charge.mode (shadow simulates, live arms Omnibattery)",
        labels=["mode"],
    )
    for m in FULL_CHARGE_MODES:
        mode.add_metric([m], 1.0 if m == f.get("mode") else 0.0)
    yield mode
    state = GaugeMetricFamily(f"{name}_state", "1 for the scheduler's state", labels=["state"])
    for s in STATES:
        state.add_metric([s], 1.0 if s == f.get("state") else 0.0)
    yield state
    yield gauge(
        "days_since_last",
        "Local days since the last full charge (absent until one is known)",
        f.get("days_since"),
    )
    yield gauge(
        "last_timestamp_seconds",
        "Unix time of the last full charge (a new valid balance reading, or a simulated run)",
        f.get("last_timestamp"),
    )
    yield gauge(
        "scheduled_day_timestamp_seconds",
        "Unix time of the start of the local day a run is scheduled for (absent: none)",
        f.get("scheduled_day_timestamp"),
    )
    yield gauge(
        "forcing_cost_eur", "Forcing cost of the last priced decision (EUR)", f.get("forcing_cost")
    )
    yield gauge("threshold_eur", "Threshold of the last priced decision (EUR)", f.get("threshold"))
    yield gauge("soc_peak_percent", "Peak SOC of the current or last run", f.get("soc_peak_pct"))
    cadence = GaugeMetricFamily(
        f"{name}_cadence_class", "1 for the active cadence class", labels=["class"]
    )
    for c in CADENCE_CLASSES:
        cadence.add_metric([c], 1.0 if c == f.get("cadence_class") else 0.0)
    yield cadence
    yield gauge("cadence_min_days", "Never before this many days after the last", f.get("min_days"))
    yield gauge("cadence_max_days", "Forced this many days after the last", f.get("max_days"))
    alerts = set(f.get("alerts") or ())
    alert = GaugeMetricFamily(f"{name}_alert", "1 for each active alert", labels=["reason"])
    for reason in ALERT_REASONS:
        alert.add_metric([reason], 1.0 if reason in alerts else 0.0)
    yield alert


def _health_families(h: dict[str, Any]) -> Iterable[Metric]:
    """Battery health telemetry (prefix ``battery_optimizer_battery_``); read only.

    An unknown reading is an absent sample, never a 0.
    """
    from .battery_health import BALANCE_STATUSES

    name = f"{PREFIX}_battery"
    session = h.get("charge_session") or {}
    shown = session.get("current") or session.get("last") or {}
    cells = h.get("cell_voltage") or {}
    balance = h.get("balance") or {}

    def gauge(suffix: str, doc: str, value: float | None) -> GaugeMetricFamily:
        family = GaugeMetricFamily(f"{name}_{suffix}", doc, labels=[])
        if value is not None:
            family.add_metric([], float(value))
        return family

    yield gauge("temperature_celsius", "Battery internal temperature", h.get("temperature_c"))
    hot = CounterMetricFamily(
        f"{name}_hot_seconds",
        "Seconds the battery temperature was strictly above the threshold (since start)",
        labels=["threshold_celsius"],
    )
    for threshold, seconds in (h.get("hot_seconds") or {}).items():
        hot.add_metric([threshold], float(seconds))
    yield hot
    uncounted = CounterMetricFamily(
        f"{name}_hot_uncounted_seconds",
        "Seconds a stalled clock advance did not credit to the held temperature",
    )
    uncounted.add_metric([], float(h.get("uncounted_s") or 0.0))
    yield uncounted
    yield gauge(
        "charge_session_active",
        "1 while a charge session (AC power above the threshold) is running",
        1.0 if session.get("active") else 0.0,
    )
    yield gauge(
        "charge_session_peak_temperature_celsius",
        "Peak temperature of the running charge session, else of the last one",
        shown.get("peak_temperature_c"),
    )
    yield gauge(
        "charge_session_duration_seconds",
        "Duration of the running charge session, else of the last one",
        shown.get("duration_s"),
    )
    sessions = CounterMetricFamily(
        f"{name}_charge_sessions", "Charge sessions completed (since start)"
    )
    sessions.add_metric([], float(session.get("completed") or 0))
    yield sessions
    yield gauge("cell_voltage_max_volts", "Highest cell voltage", cells.get("max_v"))
    yield gauge("cell_voltage_min_volts", "Lowest cell voltage", cells.get("min_v"))
    yield gauge(
        "cell_voltage_spread_millivolts",
        "Live spread between the highest and the lowest cell voltage",
        cells.get("spread_mv"),
    )
    yield gauge(
        "cell_delta_millivolts",
        "Last comparable settled top-of-charge cell delta (balance monitor)",
        balance.get("cell_delta_mv"),
    )
    yield gauge(
        "cell_delta_avg_4w_millivolts",
        "Average of the last four balance readings",
        balance.get("delta_avg_4w_mv"),
    )
    status = GaugeMetricFamily(
        f"{name}_balance_status", "1 for the balance monitor's status", labels=["status"]
    )
    if balance.get("status") is not None:
        for s in BALANCE_STATUSES:
            status.add_metric([s], 1.0 if s == balance["status"] else 0.0)
    yield status
    mode = balance.get("balancing_mode")
    yield gauge(
        "balancing_mode",
        "1 while Omnibattery's balancing mode is on",
        None if mode is None else (1.0 if mode else 0.0),
    )
    last_read = balance.get("last_read_at")
    yield gauge(
        "last_balance_read_timestamp_seconds",
        "Unix time of the last valid balance measurement",
        None if last_read is None else datetime.fromisoformat(last_read).timestamp(),
    )
    yield gauge(
        "last_balance_read_age_seconds",
        "Seconds since the last valid balance measurement",
        balance.get("last_read_age_s"),
    )


#: Upper bounds (W) of ``actuator_discharge_grid_watts``; ``+Inf`` is added by the client.
DISCHARGE_GRID_BUCKETS_W = (
    *(-500.0, -200.0, -100.0, -50.0, -25.0, 0.0, 25.0, 50.0),
    *(75.0, 100.0, 125.0, 150.0, 200.0, 300.0, 500.0),
)
#: Values of its ``power_band`` label: the measured battery discharge power (W).
POWER_BANDS = ("lt500", "500_1000", "ge1000")


def power_band(discharge_w: float) -> str:
    """The ``power_band`` label of a measured battery discharge power (W, >= 0)."""
    if discharge_w < 500.0:
        return "lt500"
    return "500_1000" if discharge_w < 1000.0 else "ge1000"


def _actuator_families(a: dict[str, Any]) -> Iterable[Metric]:
    """The actuator's scrape-time gauges (prefix ``battery_optimizer_actuator_``; ADR-0007)."""
    from .actuator import ACTUATOR_MODES, GUARD_REASONS

    name = f"{PREFIX}_actuator"

    def gauge(suffix: str, doc: str, value: float | None) -> GaugeMetricFamily:
        family = GaugeMetricFamily(f"{name}_{suffix}", doc, labels=[])
        if value is not None:
            family.add_metric([], float(value))
        return family

    mode = GaugeMetricFamily(f"{name}_mode", "1 for the actuation mode", labels=["mode"])
    for m in ACTUATOR_MODES:
        mode.add_metric([m], 1.0 if m == a.get("mode") else 0.0)
    yield mode
    yield gauge("kill_switch_on", "1 while the owner's kill switch is on", a.get("kill_switch"))
    yield gauge(
        "in_control", "1 while the actuator owns the Omnibattery controls", a.get("in_control")
    )
    active = set(a.get("reasons") or ())
    ok = GaugeMetricFamily(
        f"{name}_guard_ok", "1 when the last tick applied the intent (no guard reason)", labels=[]
    )
    if a.get("ticked"):
        ok.add_metric([], 0.0 if active else 1.0)
    yield ok
    reasons = GaugeMetricFamily(
        f"{name}_guard_reason", "1 for each guard reason active at the last tick", labels=["reason"]
    )
    for reason in GUARD_REASONS:
        reasons.add_metric([reason], 1.0 if reason in active else 0.0)
    yield reasons
    yield gauge(
        "alert", "1 while writes fail (fell back to safe idle, or even that failed)", a["alert"]
    )
    for suffix, key, doc in (
        ("desired_pd_target_watts", "desired_pd_target_w", "Requested PD grid-import target"),
        ("desired_allow_charge", "desired_allow_charge", "Requested charge permission (1 = on)"),
        (
            "desired_allow_discharge",
            "desired_allow_discharge",
            "Requested discharge permission (1 = on)",
        ),
        (
            "desired_max_discharge_watts",
            "desired_max_discharge_w",
            "Requested system discharge cap",
        ),
        (
            "observed_pd_target_watts",
            "observed_pd_target_w",
            "PD grid-import target as Home Assistant reports it",
        ),
        (
            "observed_battery_power_watts",
            "observed_battery_ac_power_w",
            "Measured battery AC power: + charge, - discharge (absent when stale)",
        ),
        (
            "observed_grid_power_watts",
            "observed_grid_power_w",
            "Measured grid power: + import (absent when stale)",
        ),
        ("house_load_watts", "house_load_w", "Estimated house load: grid - battery AC power"),
        (
            "clamped_watts",
            "clamped_w",
            "Planned battery power the last tick could not request: the PD target or discharge "
            "cap was cut by the import cap or the entity's max (0 = none cut)",
        ),
        (
            "discharge_margin_watts",
            "discharge_margin_w",
            "The configured actuation.discharge_margin_w: the grid import (PD target) kept "
            "while discharging",
        ),
    ):
        yield gauge(suffix, doc, a.get(key))
    yield gauge(
        "reserve_engaged",
        "1 while reserve protection holds: the measured SOC is at or below the tariff's SOC "
        "floor, so the tick requests the bounded recovery charge instead of the planned idle "
        "or discharge (ADR-0007 amendment 2026-11-08)",
        a.get("reserve_engaged"),
    )
    lf = a.get("load_following")
    if lf is not None:  # only with actuation.load_following.enabled
        yield gauge(
            "load_following_engaged",
            "1 while load-following discharge is engaged: the discharge cap is at the ceiling "
            "(SOC at or above the plan's end-of-slot SOC in a discharge slot)",
            lf.get("engaged"),
        )
        yield gauge(
            "load_following_raise_watts",
            "How far load-following raised the system discharge cap above the planned cap",
            lf.get("raise_w"),
        )


#: Values of the ``status`` label of the gate state set.
GATE_STATUSES = ("PASS", "FAIL", "UNDETERMINED")
CLIMATE_REGIMES = ("legacy", "comfort", "comfort_off", "unknown")
CLIMATE_SOLVE_STATUSES = ("optimal", "gap", "time_limit", "infeasible", "error")


CLIMATE_PLAN_STATES = (
    "planned",
    "skipped",
    "error",
    "mode_off",
    "no_plan",
    "expired",
)
CLIMATE_PLAN_MODES = ("off", "shadow", "live")


def _climate_plan_families(c: dict[str, Any]) -> Iterable[Metric]:
    """The advisory climate plan's scrape-time gauges (``battery_optimizer_climate_plan_*``).

    The heating and water gauges are present only while a ``planned`` plan is current and the
    effective mode is not ``off``: an absent series means "no plan to show", never "heating off"."""
    name = f"{PREFIX}_climate_plan"

    def family(suffix: str, doc: str, labels: list[str]) -> GaugeMetricFamily:
        return GaugeMetricFamily(f"{name}_{suffix}", doc, labels=labels)

    mode = c["mode"]
    state = c["state"]
    summary = c.get("summary") or {}
    requested = family("mode_requested", "1 for the mode the owner requested", ["mode"])
    effective = family("mode_effective", "1 for the mode in effect (never live)", ["mode"])
    for m in CLIMATE_PLAN_MODES:
        requested.add_metric([m], 1.0 if m == mode.requested else 0.0)
        effective.add_metric([m], 1.0 if m == mode.effective else 0.0)
    yield requested
    yield effective
    locked = family(
        "live_locked", "1 when live is requested and refused (ADR-0008 not accepted)", []
    )
    locked.add_metric([], 1.0 if mode.live_locked else 0.0)
    yield locked
    st = family("state", "1 for what /climate/plan serves (a plan status or why none)", ["state"])
    for k in CLIMATE_PLAN_STATES:
        st.add_metric([k], 1.0 if k == state else 0.0)
    yield st
    age = family("age_seconds", "Seconds since the latest plan was generated", [])
    if c.get("age_s") is not None:
        age.add_metric([], float(c["age_s"]))
    yield age
    valid = family("valid", "1 while the latest plan is inside its validity", [])
    valid.add_metric([], 1.0 if state in ("planned", "skipped", "error") else 0.0)
    yield valid
    shown = state == "planned" and summary.get("heating_now") is not None
    heating = family("heating_planned_now", "1 when the plan heats in the current slot", [])
    water = family("water_target_celsius", "Planned water target of the current slot", [])
    if shown:
        heating.add_metric([], 1.0 if summary["heating_now"] else 0.0)
        if summary.get("water_c_now") is not None:
            water.add_metric([], float(summary["water_c_now"]))
    yield heating
    yield water


def _climate_families(c: dict[str, Any]) -> Iterable[Metric]:
    """The climate shadow's scrape-time gauges (prefix ``battery_optimizer_climate_shadow_``)."""
    name = f"{PREFIX}_climate_shadow"

    def family(suffix: str, doc: str, labels: list[str]) -> GaugeMetricFamily:
        return GaugeMetricFamily(f"{name}_{suffix}", doc, labels=labels)

    up = family("up", "1 when the job is loaded (configs, prior, site file)", [])
    up.add_metric([], 1.0 if c.get("loaded") and not c.get("error") else 0.0)
    yield up
    for suffix, key, doc in (
        ("last_run_timestamp_seconds", "last_run_at", "Unix time of the last decision run"),
        ("last_refit_timestamp_seconds", "last_refit_at", "Unix time of the last weekly refit"),
    ):
        g = family(suffix, doc, [])
        if c.get(key) is not None:
            g.add_metric([], c[key].timestamp())
        yield g
    regime = family("regime", "1 for the control regime recorded at the last run", ["regime"])
    current = (c.get("last") or {}).get("regime")
    for r in CLIMATE_REGIMES:
        regime.add_metric([r], 1.0 if r == current else 0.0)
    yield regime
    queue = family("scoring_queue", "Decisions waiting for their horizon to be observed", [])
    queue.add_metric([], float(c.get("queue") or 0))
    yield queue
    top_last = family(
        "last_plan_top_offset_share_ratio",
        "Share of the last plan's heating slots at the top water offset",
        [],
    )
    if (c.get("last") or {}).get("plan_top_offset_share") is not None:
        top_last.add_metric([], float(c["last"]["plan_top_offset_share"]))
    yield top_last

    report = c.get("report") or {}
    windows = report.get("windows") or {}
    rmse = family(
        "rmse_kelvin", "Room-mean RMSE of the open-loop prediction", ["window", "horizon"]
    )
    skill = family("skill_ratio", "1 - RMSE(model) / RMSE(persistence)", ["window", "horizon"])
    bias = family("abs_bias_kelvin", "Absolute mean room-mean error", ["window", "horizon"])
    n = family("origins", "Scored forecast origins on valid heating days", ["window", "horizon"])
    mdape = family(
        "daily_heat_mdape_ratio", "Median absolute percentage error of daily heat", ["window"]
    )
    weeks = family("valid_weeks", "Valid heating days in the window / 7", ["window"])
    top = family(
        "top_offset_share_ratio", "Share of planned heating slots at the top offset", ["window"]
    )
    gate = family(
        "gate",
        "Preregistered section-A criterion status (state set; criterion verdict = the window)",
        ["window", "criterion", "status"],
    )
    for w, win in windows.items():
        for h, st in (win.get("horizons") or {}).items():
            n.add_metric([w, h], float(st.get("n") or 0))
            for fam, value in ((rmse, st.get("rmse")), (skill, st.get("skill"))):
                if value is not None:
                    fam.add_metric([w, h], float(value))
            if st.get("bias") is not None:
                bias.add_metric([w, h], abs(float(st["bias"])))
        if (win.get("daily_heat") or {}).get("mdape") is not None:
            mdape.add_metric([w], float(win["daily_heat"]["mdape"]))
        weeks.add_metric([w], float(win.get("valid_weeks") or 0.0))
        if (win.get("plan") or {}).get("top_offset_share") is not None:
            top.add_metric([w], float(win["plan"]["top_offset_share"]))
        for criterion, status in {**win.get("criteria", {}), "verdict": win.get("verdict")}.items():
            for st in GATE_STATUSES:
                gate.add_metric([w, criterion, st], 1.0 if st == status else 0.0)
    yield from (rmse, skill, bias, n, mdape, weeks, top, gate)


def _thermal_families(t: dict[str, Any]) -> Iterable[Metric]:
    """The thermal shadow's scrape-time gauges (prefix ``battery_optimizer_thermal_``); read only.

    An unknown reading is an absent sample, never a 0.
    """
    from .thermal import DERATE_STATES
    from .thermal_tracker import MODEL_STATUSES

    name = f"{PREFIX}_thermal"
    derate = t.get("derate") or {}
    model = t.get("model") or {}
    current = t.get("current_slot") or {}

    def gauge(suffix: str, doc: str, value: float | None) -> GaugeMetricFamily:
        family = GaugeMetricFamily(f"{name}_{suffix}", doc, labels=[])
        if value is not None:
            family.add_metric([], float(value))
        return family

    yield gauge(
        "temperature_celsius",
        "Battery internal temperature (fresh reading)",
        t.get("temperature_c"),
    )
    state = GaugeMetricFamily(
        f"{name}_derate_state",
        "1 for the state of Omnibattery's Temperature Charge Limit (off, on, unknown)",
        labels=["state"],
    )
    for s in DERATE_STATES:
        state.add_metric([s], 1.0 if s == derate.get("state") else 0.0)
    yield state
    yield gauge(
        "derate_factor",
        "Charge derate factor of the curve at the measured temperature (1 = none; absent unknown)",
        derate.get("factor_now"),
    )
    derating = derate.get("derating_now")
    yield gauge(
        "derating",
        "1 while the curve is in force and below 1 at the measured temperature",
        None if derating is None else (1.0 if derating else 0.0),
    )
    setting = GaugeMetricFamily(
        f"{name}_limit_setting",
        "Omnibattery's temperature charge limit settings as read (limit_c, band_c, floor_pct, "
        "limit_kw)",
        labels=["name"],
    )
    for key in ("limit_c", "band_c", "floor_pct", "limit_kw"):
        if derate.get(key) is not None:
            setting.add_metric([key], float(derate[key]))
    yield setting
    yield gauge(
        "predicted_temperature_celsius",
        "Predicted temperature at the end of the slot in progress (newest plan's shadow)",
        current.get("temp_end_c"),
    )
    yield gauge(
        "predicted_derate_factor",
        "Lowest predicted derate factor inside the slot in progress",
        current.get("derate_factor"),
    )
    yield gauge(
        "predicted_achievable_charge_kw",
        "Predicted achievable mean charge power of the slot in progress",
        current.get("charge_kw"),
    )
    status = GaugeMetricFamily(
        f"{name}_model_status", "1 for the thermal model's status", labels=["status"]
    )
    for s in MODEL_STATUSES:
        status.add_metric([s], 1.0 if s == model.get("status") else 0.0)
    yield status
    parameter = GaugeMetricFamily(
        f"{name}_parameter",
        "Thermal model parameter (tau_min, t_amb_c, k_c, k_d)",
        labels=["name"],
    )
    for key in ("tau_min", "t_amb_c", "k_c", "k_d"):
        if model.get(key) is not None:
            parameter.add_metric([key], float(model[key]))
    yield parameter
    fitted_at = model.get("fitted_at")
    yield gauge(
        "last_fit_timestamp_seconds",
        "Unix time of the accepted refit that produced the parameters (absent while prior)",
        None if fitted_at is None else datetime.fromisoformat(fitted_at).timestamp(),
    )
    yield gauge(
        "buffer_hours",
        "Hours with a temperature reading in the one-minute observation buffer",
        (t.get("buffer") or {}).get("hours_with_temperature"),
    )


class ServiceMetrics:
    def __init__(
        self,
        snapshot: Callable[[], dict[str, Any]] | None = None,
        *,
        actuation: bool = False,
        full_charge: bool = False,
        thermal: bool = False,
    ) -> None:
        # ``*_created`` series double the exposition for no use here (process-wide switch).
        disable_created_metrics()
        self.registry = CollectorRegistry(auto_describe=False)
        r = self.registry
        self.build_info = Gauge(
            f"{PREFIX}_build_info",
            "Build and configuration identity (value is always 1)",
            ["code_version", "version", "soc_source", "mode"],
            registry=r,
        )
        self.config_override_info = Gauge(
            f"{PREFIX}_config_override_info",
            "Config paths set by the site overrides file (value is always 1)",
            ["path"],
            registry=r,
        )
        self.solve_duration = Histogram(
            f"{PREFIX}_solve_duration_seconds",
            "Wall time of planner solves (successful ones)",
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
            registry=r,
        )
        self.solves = Counter(f"{PREFIX}_solves", "Planner runs by result", ["result"], registry=r)
        self.skipped = Counter(
            f"{PREFIX}_skipped_solves",
            "Solve requests not started, by reason",
            ["reason"],
            registry=r,
        )
        self.history_refresh = Counter(
            f"{PREFIX}_history_refresh",
            "Prometheus history refreshes by result",
            ["result"],
            registry=r,
        )
        self.sink_errors = Counter(
            f"{PREFIX}_sink_write_errors", "Failed shadow-store writes", registry=r
        )
        self.pvpc_snapshots = Counter(
            f"{PREFIX}_pvpc_snapshots",
            "PVPC publication snapshots by result (written, failed)",
            ["result"],
            registry=r,
        )
        self.weather_forecast_fetches = Counter(
            f"{PREFIX}_weather_forecast_fetches",
            "weather.get_forecasts archive fetches by result (ok = fetched and written)",
            ["result"],
            registry=r,
        )
        # Set only on a success, so the series is absent until the first one.
        self.archive_last_success = Gauge(
            f"{PREFIX}_archive_last_success_timestamp_seconds",
            "Unix time of the last successful archive write",
            ["archive"],
            registry=r,
        )
        self.rest_polls = Counter(
            f"{PREFIX}_ha_rest_polls", "REST GET refreshes by result", ["result"], registry=r
        )
        self.shadow_slots = Counter(
            f"{PREFIX}_shadow_slots", "Closed slots by decision outcome", ["decision"], registry=r
        )
        self.shadow_planned_energy = Counter(
            f"{PREFIX}_shadow_planned_energy_kwh",
            "Energy the shadow decisions requested",
            ["direction"],
            registry=r,
        )
        self.shadow_actual_energy = Counter(
            f"{PREFIX}_shadow_actual_battery_energy_kwh",
            "Battery energy the native controller actually moved",
            ["direction"],
            registry=r,
        )
        # Day-ahead vs final plan KPIs (docs/service.md).  The final plan's battery energy is
        # shadow_planned_energy and the actual is shadow_actual_battery_energy; the signed load
        # error is the difference of two counters (actual - forecast), which keeps all monotonic.
        self.shadow_day_ahead_energy = Counter(
            f"{PREFIX}_shadow_day_ahead_energy_kwh",
            "Energy the day-ahead plans requested of the slots that had one",
            ["direction"],
            registry=r,
        )
        self.plan_revision_energy = Counter(
            f"{PREFIX}_plan_revision_kwh",
            "Sum over slots with both plans of |day-ahead - final| requested net battery energy",
            registry=r,
        )
        self.load_forecast_scored_slots = Counter(
            f"{PREFIX}_load_forecast_scored_slots",
            "Closed slots with a measured load and a load forecast, by plan",
            ["plan"],
            registry=r,
        )
        self.load_forecast_kwh = Counter(
            f"{PREFIX}_load_forecast_kwh",
            "House load the plan forecast, over its scored slots",
            ["plan"],
            registry=r,
        )
        self.load_actual_kwh = Counter(
            f"{PREFIX}_load_actual_kwh",
            "House load measured, over the slots scored for the plan",
            ["plan"],
            registry=r,
        )
        self.load_forecast_abs_error_kwh = Counter(
            f"{PREFIX}_load_forecast_abs_error_kwh",
            "Sum of |actual - forecast| house load per scored slot, by plan",
            ["plan"],
            registry=r,
        )
        # Gauges (cumulative since process start): PVPC can be negative, so a running
        # cost can fall, which a Counter cannot express.  The report is authoritative.
        self.shadow_native_cost = Gauge(
            f"{PREFIX}_shadow_native_cost_eur",
            "Pretax PVPC cost of the measured grid import over comparable slots since start",
            registry=r,
        )
        self.shadow_cf_cost = Gauge(
            f"{PREFIX}_shadow_counterfactual_cost_eur",
            "Pretax PVPC cost had the shadow decisions run (open-loop replay) since start",
            registry=r,
        )
        self.shadow_comparable = Counter(
            f"{PREFIX}_shadow_comparable_slots",
            "Slots with price, measured load and a replayable decision outcome",
            registry=r,
        )
        self.shadow_soc_violations = Counter(
            f"{PREFIX}_shadow_soc_violations",
            "Decisions that would have left the SOC window from the measured SOC",
            registry=r,
        )
        self.shadow_clipped = Counter(
            f"{PREFIX}_shadow_clipped_energy_kwh",
            "Requested energy the replay had to clip (SOC, power, import ceiling, no export)",
            registry=r,
        )
        self.climate_runs = Counter(
            f"{PREFIX}_climate_shadow_runs",
            "Climate shadow decision runs by outcome",
            ["result"],
            registry=r,
        )
        self.climate_solves = Counter(
            f"{PREFIX}_climate_shadow_solves",
            "Comfort-MPC MILP solves by status",
            ["status"],
            registry=r,
        )
        self.climate_solve_duration = Histogram(
            f"{PREFIX}_climate_shadow_solve_duration_seconds",
            "Wall time of one comfort-MPC MILP solve",
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
            registry=r,
        )
        self.climate_plan_served = Counter(
            f"{PREFIX}_climate_plan_served",
            "Requests answered by the advisory climate plan endpoints",
            ["endpoint", "result"],
            registry=r,
        )
        self.climate_scores = Counter(
            f"{PREFIX}_climate_shadow_scores",
            "Forecast origins leaving the scoring queue by outcome",
            ["result"],
            registry=r,
        )
        self.climate_refits = Counter(
            f"{PREFIX}_climate_shadow_refits",
            "Weekly re-estimations by outcome (refitted, prior_kept)",
            ["result"],
            registry=r,
        )
        for result in ("planned", "plan_failed", "no_state", "no_inputs", "error"):
            self.climate_runs.labels(result=result)
        for status in CLIMATE_SOLVE_STATUSES:
            self.climate_solves.labels(status=status)
        for endpoint in ("plan", "summary"):
            for result in ("ok", "unavailable"):
                self.climate_plan_served.labels(endpoint=endpoint, result=result)
        for result in ("scored", "expired", "failed"):
            self.climate_scores.labels(result=result)
        for result in ("refitted", "prior_kept"):
            self.climate_refits.labels(result=result)
        for result in ("written", "failed"):
            self.pvpc_snapshots.labels(result=result)
        for result in ("ok", "failed"):
            self.weather_forecast_fetches.labels(result=result)
        for reason in ("busy", *(r for r in NOT_READY_REASONS if r != "no_recent_plan")):
            self.skipped.labels(reason=reason)
        for result in ("ok", "error", "timeout", "discarded"):
            self.solves.labels(result=result)
        for decision in (
            "intent",
            "late_intent",
            "no_plan",
            "plan_expired",
            "not_ready",
            "not_observed",
        ):
            self.shadow_slots.labels(decision=decision)
        for direction in ("charge", "discharge"):
            self.shadow_planned_energy.labels(direction=direction)
            self.shadow_actual_energy.labels(direction=direction)
            self.shadow_day_ahead_energy.labels(direction=direction)
        for plan in PLAN_LABELS:
            self.load_forecast_scored_slots.labels(plan=plan)
            self.load_forecast_kwh.labels(plan=plan)
            self.load_actual_kwh.labels(plan=plan)
            self.load_forecast_abs_error_kwh.labels(plan=plan)
        if actuation:  # dry_run / live only: the shadow exposition stays as it was
            self._register_actuation()
        if full_charge:  # only with the feature enabled
            self._register_full_charge()
        if thermal:  # only with planning.thermal on: mode off keeps the exposition as it was
            self._register_thermal()
        if snapshot is not None:
            r.register(_RuntimeCollector(snapshot))

    def _register_actuation(self) -> None:
        from .actuator import SAFE_IDLE_REASONS, WRITE_FAILURE_REASONS, WRITE_ROLES

        r = self.registry
        self.actuator_writes = Counter(
            f"{PREFIX}_actuator_writes",
            "Omnibattery writes by role and result (ok = sent and read back; dry_run = not sent)",
            ["role", "result"],
            registry=r,
        )
        self.actuator_write_failures = Counter(
            f"{PREFIX}_actuator_write_failures",
            "Failed write attempts by reason (each retry counts)",
            ["reason"],
            registry=r,
        )
        self.actuator_safe_idles = Counter(
            f"{PREFIX}_actuator_safe_idles",
            "Entries into safe idle (both direction switches off) by reason",
            ["reason"],
            registry=r,
        )
        self.actuator_heartbeats = Counter(
            f"{PREFIX}_actuator_heartbeats",
            "Heartbeats to the HA watchdog helper by result",
            ["result"],
            registry=r,
        )
        # Discharge-time grid distribution (docs/planning/discharge-margin-tuning.md, step 1).
        self.actuator_discharge_grid = Histogram(
            f"{PREFIX}_actuator_discharge_grid_watts",
            "Measured grid power (+ import) at every grid-power update while the actuator is in "
            "control, guard ok and discharging, by measured battery discharge power band",
            ["power_band"],
            buckets=DISCHARGE_GRID_BUCKETS_W,
            registry=r,
        )
        self.actuator_export_seconds = Counter(
            f"{PREFIX}_actuator_export_seconds",
            "Seconds with grid power below 0 W while discharging, integrated only between two "
            "fresh consecutive grid-power updates",
            registry=r,
        )
        for band in POWER_BANDS:
            self.actuator_discharge_grid.labels(power_band=band)
        # 0 until the first write confirmed by readback.
        self.actuator_last_write = Gauge(
            f"{PREFIX}_actuator_last_successful_write_timestamp_seconds",
            "Unix time of the last write confirmed by readback",
            registry=r,
        )
        for role in WRITE_ROLES:
            for result in ("ok", "failed", "dry_run"):
                self.actuator_writes.labels(role=role, result=result)
        for reason in WRITE_FAILURE_REASONS:
            self.actuator_write_failures.labels(reason=reason)
        for reason in SAFE_IDLE_REASONS:
            self.actuator_safe_idles.labels(reason=reason)
        for result in ("ok", "failed", "dry_run"):
            self.actuator_heartbeats.labels(result=result)

    def _register_full_charge(self) -> None:
        from .full_charge import OUTCOMES

        self.full_charge_runs = Counter(
            f"{PREFIX}_full_charge_runs",
            "Periodic full-charge runs by outcome",
            ["outcome"],
            registry=self.registry,
        )
        for outcome in OUTCOMES:
            self.full_charge_runs.labels(outcome=outcome)

    def _register_thermal(self) -> None:
        from .thermal_tracker import LEADS

        r = self.registry
        self.thermal_refits = Counter(
            f"{PREFIX}_thermal_refits",
            "Daily thermal refits by result (accepted, rejected, insufficient_data, error)",
            ["result"],
            registry=r,
        )
        self.thermal_predictions = Counter(
            f"{PREFIX}_thermal_predictions",
            "Per-plan thermal predictions by result (ok, error)",
            ["result"],
            registry=r,
        )
        self.thermal_scored_slots = Counter(
            f"{PREFIX}_thermal_scored_slots",
            "Closed slots whose predicted end temperature was scored against the measurement",
            ["lead"],
            registry=r,
        )
        self.thermal_abs_error = Counter(
            f"{PREFIX}_thermal_temperature_abs_error_celsius",
            "Sum of |predicted - measured| temperature at the end of scored slots, by lead",
            ["lead"],
            registry=r,
        )
        self.thermal_squared_error = Counter(
            f"{PREFIX}_thermal_temperature_squared_error_celsius_squared",
            "Sum of (predicted - measured)^2 temperature at the end of scored slots, by lead",
            ["lead"],
            registry=r,
        )
        # A gauge: the signed sum falls when the model runs cold.
        self.thermal_error_sum = Gauge(
            f"{PREFIX}_thermal_temperature_error_sum_celsius",
            "Sum of (predicted - measured) temperature at the end of scored slots, by lead",
            ["lead"],
            registry=r,
        )
        self.thermal_power_scored_slots = Counter(
            f"{PREFIX}_thermal_derate_scored_slots",
            "Closed slots with a predicted or measured derate whose achievable charge was scored",
            registry=r,
        )
        self.thermal_power_abs_error = Counter(
            f"{PREFIX}_thermal_achievable_charge_abs_error_kw",
            "Sum of |predicted achievable - observed| mean charge power over derate-scored slots",
            registry=r,
        )
        self.thermal_power_error_sum = Gauge(
            f"{PREFIX}_thermal_achievable_charge_error_sum_kw",
            "Sum of (predicted achievable - observed) mean charge power over derate-scored slots",
            registry=r,
        )
        for result in ("accepted", "rejected", "insufficient_data", "error"):
            self.thermal_refits.labels(result=result)
        for result in ("ok", "error"):
            self.thermal_predictions.labels(result=result)
        for lead in LEADS:
            self.thermal_scored_slots.labels(lead=lead)
            self.thermal_abs_error.labels(lead=lead)
            self.thermal_squared_error.labels(lead=lead)
            self.thermal_error_sum.labels(lead=lead).set(0.0)

    def render(self) -> bytes:
        return generate_latest(self.registry)
