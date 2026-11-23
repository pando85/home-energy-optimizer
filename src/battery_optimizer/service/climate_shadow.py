"""The climate shadow job (track B2): B0's thermal model and the comfort MPC on live data.

Read-only, like the rest of the service.  Every ``replan_every_minutes`` of
``config/climate-mpc.yaml`` (hourly), ``offset_minutes`` after the boundary, it:

1. refreshes an in-memory cache of raw Prometheus samples (incremental after the first fetch;
   the trailing ``model.reestimate.window_days + 2`` days) and builds the climate signals cut at
   the decision ``t0`` (:func:`battery_optimizer.climate.shadow.build_live_data`);
2. re-estimates ``ua``/``q0`` when the weekly refit is due (``climate_models``);
3. decides at ``t0``: observer state, horizon inputs and the MPC plan (``climate_decisions``);
4. scores every queued origin whose horizon lies in a completed local day (``climate_scores``)
   and records each completed day's validity (``climate_days``);
5. re-evaluates the preregistered gates for ``/metrics`` and ``/status``.

Home Assistant is read through the service's entity store only (the comfort-control helper, the
DHW plan helper and an optional away switch, each absent-tolerant), and the weather forecast is the
newest record of the service's own passive archive.  Nothing here sends anything to Home
Assistant; records land under ``<data_dir>/service/v1/climate_*`` (append-only).

The latest plan is also what the climate actuator acts on (ADR-0009, :mod:`.climate_actuator`),
but only a plan **solved by this process** (:attr:`ClimateShadowJob.plan_own`: not one rebuilt from
the store at load).  With ``redecide_foreign`` (set when an actuator exists) the first run after a
restart decides its boundary again even if the previous process already did, so the actuator has
an own plan at once; that second record of the same ``t0`` is flagged ``redecision`` and is never
queued for scoring twice.

A failure is counted, logged and recorded; it never touches the battery planner's readiness.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from battery_optimizer import extract_prom
from battery_optimizer.climate import plan_export, room_guard, shadow, shadow_gates, vacation
from battery_optimizer.climate.config import ClimateConfig, load_climate_config
from battery_optimizer.climate.data import SITE_FILE, Site, load_site
from battery_optimizer.climate.mpc import PlanSettings
from battery_optimizer.climate.mpc_config import MpcConfig, load_mpc_config
from battery_optimizer.climate.mpc_replay import check_configs
from battery_optimizer.features import RawSeries, merge_samples

from .config import ClimateShadowSettings, ServiceConfig
from .history import fetch_points
from .sink import ShadowSink, iter_records
from .timegrid import floor_slot, local_date

__all__ = [
    "ClimateShadowJob",
    "PrometheusClimateSamples",
    "SampleSource",
    "build_job",
    "config_paths",
    "latest_forecast",
    "next_run_time",
    "read_records",
]

log = logging.getLogger(__name__)

SampleSource = Callable[[datetime], Mapping[str, RawSeries]]

#: How far back the job and the report read their small records (scores, days, models).
RECORDS_SINCE = date(2026, 10, 1)


class PrometheusClimateSamples:
    """Rolling cache of raw samples, refreshed incrementally (synchronous: run it off the loop).

    The first call fetches the whole *window*; later calls fetch from one hour before the last
    fetch's end, replace that overlap and drop what fell out of the window."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        sources: Mapping[str, tuple[str, str]],
        window: timedelta,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._base_url = extract_prom.resolve_base_url(base_url)
        self._sources = dict(sources)
        self._window = window
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0))
        )
        self._cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._until: datetime | None = None

    def __call__(self, now: datetime) -> dict[str, RawSeries]:
        start = now - self._window
        since = (
            start
            if self._until is None or self._until < start
            else self._until - timedelta(hours=1)
        )
        since_us, start_us = extract_prom.to_micros(since), extract_prom.to_micros(start)
        with self._client_factory() as client:
            for key, (entity, metric) in self._sources.items():
                points = fetch_points(client, self._base_url, entity, metric, since, now)
                t = np.array([extract_prom.to_micros(p.ts) for p in points], dtype=np.int64)
                v = np.array([p.value for p in points], dtype=np.float64)
                old_t, old_v = self._cache.get(key, (np.zeros(0, np.int64), np.zeros(0)))
                keep = old_t < since_us
                t, v, _dup, _conflict = merge_samples(
                    np.concatenate([old_t[keep], t]), np.concatenate([old_v[keep], v])
                )
                inside = t >= start_us
                self._cache[key] = (t[inside], v[inside])
        self._until = now
        return {
            key: RawSeries(entity=e, metric=m, unit="", t_us=t, values=v)
            for key, (e, m) in self._sources.items()
            for t, v in [self._cache[key]]
        }


def next_run_time(after: datetime, interval: timedelta, offset_minutes: int) -> datetime:
    """First run instant strictly after *after*: a decision boundary plus the offset."""
    offset = timedelta(minutes=offset_minutes)
    return floor_slot(after - offset, int(interval.total_seconds())) + interval + offset


def latest_forecast(
    data_dir: Path, entity_id: str, at: datetime, max_age: timedelta
) -> dict[str, Any] | None:
    """The newest archived ``weather.get_forecasts`` record of *entity_id* requested in
    ``[at - max_age, at]``; the caller passes the decision ``t0``, so no forecast issued after it
    is read (the service's own passive archive; nothing is fetched here)."""
    best = None
    for rec in iter_records(data_dir, "weather_forecasts", at - max_age, at + timedelta(minutes=1)):
        if rec.get("entity_id") != entity_id:
            continue
        try:
            issued = datetime.fromisoformat(rec["requested_at"])
        except KeyError, TypeError, ValueError:
            continue
        if at - max_age <= issued <= at and (best is None or issued > best[0]):
            best = (issued, rec)
    return best[1] if best else None


def _cut(series: Mapping[str, RawSeries], at: datetime) -> dict[str, RawSeries]:
    """Every series up to the decision *at*: the samples before it plus the first one at or after
    it.  That sample only closes the last span before *at* (sample-and-hold: its value belongs to
    the slot from *at* on, which it alone cannot cover), so no value after *at* reaches a slot
    before it, and the last slot is not lost to the max-gap rule."""
    us = int(at.timestamp() * 1e6)
    out = {}
    for key, s in series.items():
        n = int(np.searchsorted(s.t_us, us, side="left")) + 1
        out[key] = RawSeries(s.entity, s.metric, s.unit, s.t_us[:n], s.values[:n])
    return out


def read_records(data_dir: Path, kind: str, since: datetime, until: datetime) -> list[dict]:
    return list(iter_records(data_dir, kind, since, until))


class ClimateShadowJob:
    """One instance per service; :meth:`run_once` is synchronous (the runtime runs it in a
    thread) and holds the job's whole state."""

    def __init__(
        self,
        *,
        settings: ClimateShadowSettings,
        climate_config: ClimateConfig,
        mpc_config: MpcConfig,
        prior: shadow.ShadowPrior,
        site: Site,
        sink: ShadowSink,
        data_dir: Path,
        samples: SampleSource,
        code_version: str,
        plan_settings: PlanSettings | None = None,
        metrics: Any = None,
        redecide_foreign: bool = False,
    ) -> None:
        check_configs(climate_config, mpc_config)
        if tuple(climate_config.comfort.rooms) != shadow_gates.GATE_ROOMS:
            raise ValueError(
                f"comfort.rooms {list(climate_config.comfort.rooms)} differ from the "
                f"preregistered gate rooms {list(shadow_gates.GATE_ROOMS)}"
            )
        self.settings = settings
        self.ccfg = climate_config
        self.cfg = mpc_config
        self.prior = prior
        self.site = site
        self.sink = sink
        self.data_dir = data_dir
        self.code_version = code_version
        self.metrics = metrics
        self._samples = samples
        self._plan_settings = plan_settings
        self.tz = ZoneInfo(climate_config.timezone)
        self.horizon_hours = int(mpc_config.horizon_hours)
        self.interval = timedelta(minutes=int(mpc_config.replan_every_minutes))
        self.window = timedelta(days=int(mpc_config.model.reestimate.window_days) + 2)
        self.model: dict[str, Any] | None = None
        self.queue: dict[str, dict[str, Any]] = {}  # unscored decisions with an origin, by t0
        self.decided: set[str] = set()
        self.day_regimes: dict[str, set[str]] = {}
        self.scores: list[dict[str, Any]] = []
        self.days: dict[str, dict[str, Any]] = {}
        self.report: dict[str, Any] | None = None
        self.last: dict[str, Any] = {}
        #: The latest advisory ClimatePlan (contract v1), rebuilt from the newest decision record
        #: at load and replaced by every run.  Always built; whether it is served is the mode's.
        self.plan: dict[str, Any] | None = None
        #: The plan was solved by this process (a run of :meth:`run_once`), not rebuilt at load.
        self.plan_own = False
        #: The status of the decision record the plan was built from (``planned``, ``no_state``,
        #: ``no_inputs``, ``plan_failed``, ``error``): what the actuator's fallback reason names.
        self.plan_status: str | None = None
        self.redecide_foreign = redecide_foreign
        self.refit_at: datetime | None = None
        #: Vacation windows seen (ADR-0010), start -> effective end (UTC), from the
        #: ``climate_vacations`` records: the weekly refit pauses while one overlaps its window.
        self.vacations: dict[datetime, datetime] = {}
        #: The window of the previous run, to record a cancel or a change.
        self._vacation_last: vacation.VacationWindow | None = None
        #: Why the last due refit was not run (``vacation``), else ``None``.
        self.refit_paused: str | None = None
        #: The earliest warm-up start that has begun, per window (sticky, for the phase).
        self._recovery: dict[tuple[str, str], datetime] = {}
        self._loaded = False

    # ------------------------------------------------------------------ schedule

    def next_run(self, after: datetime) -> datetime:
        return next_run_time(after, self.interval, self.settings.offset_minutes)

    def decision_time(self, now: datetime) -> datetime:
        """The decision boundary ``t0`` a run at *now* decides for."""
        return floor_slot(now, int(self.interval.total_seconds()))

    # ------------------------------------------------------------------ state

    def _write(self, kind: str, at: datetime, record: dict[str, Any]) -> None:
        self.sink.append_climate(
            kind, at, {**record, "code_version": self.code_version, "written_at": _iso(at)}
        )

    def load(self, now: datetime) -> None:
        """Resume from the store: the current model, the scoring queue and the gate records."""
        since = datetime.combine(RECORDS_SINCE, datetime.min.time(), tzinfo=UTC)
        until = now + timedelta(days=1)
        models = [
            m for m in iter_records(self.data_dir, "climate_models", since, until) if self._own(m)
        ]
        self.model = max(models, key=lambda m: m["valid_from"]) if models else None
        if self.model is not None and self.model.get("written_at"):
            self.refit_at = datetime.fromisoformat(self.model["written_at"])
        self.scores = list(iter_records(self.data_dir, "climate_scores", since, until))
        scored = {s["t0"] for s in self.scores if self._own(s)}
        for rec in iter_records(self.data_dir, "climate_days", since, until):
            self.days[rec["day"]] = rec
        for rec in iter_records(self.data_dir, "climate_vacations", since, until):
            try:
                start = datetime.fromisoformat(rec["start"])
                end = datetime.fromisoformat(rec["end"])
            except KeyError, TypeError, ValueError:
                continue
            self.vacations[start] = end
            ready = rec.get("ready_at")
            last = self._vacation_last
            if rec.get("closed"):
                if last is not None and last.start == start:
                    self._vacation_last = None
            elif end > now and ready:
                self._vacation_last = vacation.VacationWindow(
                    start, end, datetime.fromisoformat(ready)
                )
        lookback = now - timedelta(days=self.settings.score_lookback_days + 2)
        newest: dict[str, Any] | None = None
        for rec in iter_records(self.data_dir, "climate_decisions", lookback, until):
            if self._own(rec) and (newest is None or rec["t0"] > newest["t0"]):
                newest = rec
            self.decided.add(rec["t0"])
            self.day_regimes.setdefault(rec["local_day"], set()).add(rec.get("regime") or "")
            if "origin" in rec and rec["t0"] not in scored and self._own(rec):
                self.queue[rec["t0"]] = rec
        if newest is not None:  # a restart inside a plan's validity keeps serving it
            self._publish(newest, own=False)
        self._loaded = True

    def _publish(self, rec: Mapping[str, Any], *, own: bool) -> None:
        """Shape the decision into the advisory plan.  A failure here is reported as an ``error``
        plan and never reaches the shadow.  *own*: this process solved it (a run, not a load).

        The actuator reads these attributes from the event loop while a run publishes from the
        worker thread: the plan is assigned first and the flags that vouch for it after, so a
        reader can see a new plan with the old flags (at worst a fallback) and never an old,
        foreign plan marked as this process's."""
        machine = self.cfg.machine
        status = rec.get("status")
        try:
            plan = plan_export.build_plan(
                rec,
                code_version=self.code_version,
                ttl_minutes=self.settings.plan_ttl_minutes,
                water_range_c=(int(machine.water_min_c), int(machine.water_max_c)),
            )
        except Exception as exc:  # noqa: BLE001 - the plan is advisory, the shadow carries on
            log.exception("climate plan export of %s failed", rec.get("t0"))
            plan = plan_export.build_plan(
                {
                    "t0": rec["t0"],
                    "run_at": rec.get("run_at"),
                    "status": "error",
                    "error": f"{type(exc).__name__}: plan export",
                    "regime": rec.get("regime"),
                    "prior_id": rec.get("prior_id"),
                    "mpc_config_sha256": rec.get("mpc_config_sha256"),
                },
                code_version=self.code_version,
                ttl_minutes=self.settings.plan_ttl_minutes,
            )
            status = "error"
        self.plan = plan
        self.plan_status = status
        self.plan_own = own

    def _own(self, rec: Mapping[str, Any]) -> bool:
        """A record of this job's lineage: its prior and its MPC config."""
        return (
            rec.get("prior_id") == self.prior.prior_id
            and rec.get("mpc_config_sha256") == self.cfg.sha256
        )

    # ------------------------------------------------------------------ run

    def run_once(
        self, now: datetime, inputs: shadow.LiveInputs, *, force: bool = False
    ) -> dict[str, Any]:
        """Decide at the last boundary, score what is due, refresh the gates.  Never raises for a
        data problem: the outcome is the returned (and recorded) status.

        *force* decides the boundary again even if it was decided (a change of the owner's
        vacation helpers, ADR-0010); the record is flagged ``redecision`` and never scored twice."""
        if not self._loaded:
            self.load(now)
        t0 = self.decision_time(now)
        key = _iso(t0)
        redecision = key in self.decided and (
            force or (self.redecide_foreign and not self.plan_own)
        )
        if key in self.decided and not redecision:
            return {"status": "skipped", "t0": key}
        if inputs.vacation is not None:  # an HA restart keeps the window it had
            inputs = replace(
                inputs, vacation=vacation.hold_last(inputs.vacation, self._vacation_last, now)
            )
        self._track_vacation(inputs.vacation, now)
        started = time.monotonic()
        summary: dict[str, Any] = {"t0": key, "run_at": _iso(now)}
        try:
            series = _cut(self._samples(now), t0)
            start = floor_slot(t0 - self.window, 3600)
            hours = shadow.decision_hours(self.cfg, inputs, self.settings.vacation, t0)
            end = t0 + timedelta(hours=hours)
            d = shadow.build_live_data(
                series, start=start, end=end, config=self.ccfg, site=self.site
            )
            today = local_date(t0, self.tz)
            self._maybe_refit(d, today, now, inputs.vacation)
            assert self.model is not None
            d_in = d  # the decision's inputs; scoring, refit and day records read the raw ``d``
            rule = self.settings.room_spike_guard
            if rule.enabled:
                guarded, flags = room_guard.guard_series(series, shadow.ROOM_SOURCES, rule)
                audit = room_guard.audit(series, shadow.ROOM_SOURCES, rule, guarded, flags, t0)
                inputs = replace(inputs, room_guard=audit)
                if any(f.any() for f in flags.values()):
                    d_in = shadow.build_live_data(
                        guarded, start=start, end=end, config=self.ccfg, site=self.site
                    )
            rec = shadow.decide(
                self.prior,
                self.model,
                d_in,
                self.cfg,
                t0,
                inputs,
                settings=self._plan_settings,
                comfort=shadow.comfort_helper_levels(series, t0),
                vacation_settings=self.settings.vacation,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, counted, retried next hour
            log.exception("climate shadow decision at %s failed", key)
            rec = {
                "kind": "climate_decision",
                "t0": key,
                "local_day": local_date(t0, self.tz).isoformat(),
                "regime": inputs.regime,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "prior_id": self.prior.prior_id,
                "mpc_config_sha256": self.cfg.sha256,
            }
            d = None
        rec["run_at"] = _iso(now)
        rec["elapsed_s"] = round(time.monotonic() - started, 3)
        if redecision:  # the boundary was decided before the restart: never scored twice
            rec["redecision"] = True
        self._sticky_recovery(rec, now)
        self._write("climate_decisions", t0, rec)
        self._publish(rec, own=True)
        self.decided.add(key)
        self.day_regimes.setdefault(rec["local_day"], set()).add(rec.get("regime") or "")
        if "origin" in rec and not redecision:
            self.queue[key] = rec
        self._count_decision(rec)
        summary.update(status=rec["status"], regime=rec.get("regime"))
        if d is not None:
            summary["scored"] = self._score_due(d, now)
            summary["days"] = self._record_days(d, now)
        self.report = shadow_gates.evaluate(
            self.scores,
            self.days.values(),
            go_live=self.settings.go_live,
            prior_id=self.prior.prior_id,
            mpc_config_sha256=self.cfg.sha256,
        )
        self.last = {
            **summary,
            "error": rec.get("error"),
            "model_hash": (self.model or {}).get("model_hash"),
            "plan_top_offset_share": _top_share(rec),
            "at": now,
        }
        return summary

    # ------------------------------------------------------------------ vacation (ADR-0010)

    def _track_vacation(self, read: vacation.VacationRead | None, now: datetime) -> None:
        """Record every new window, and close the previous one when the owner cancels or changes
        it (its effective end is then *now*, or its start if it had not begun), so a restart
        still knows which days were a vacation."""
        window = read.window if read is not None else None
        last = self._vacation_last
        if last is not None and (window is None or window.key() != last.key()):
            end = max(last.start, min(last.end, now))
            self.vacations[last.start] = end
            self._write(
                "climate_vacations",
                now,
                {
                    "kind": "climate_vacation",
                    "start": _iso(last.start),
                    "end": _iso(end),
                    "ready_at": _iso(last.ready_at),
                    "closed": True,
                },
            )
        if window is not None and (last is None or window.key() != last.key()):
            self.vacations[window.start] = window.end
            self._write(
                "climate_vacations",
                now,
                {"kind": "climate_vacation", **window.to_dict(), "closed": False},
            )
        self._vacation_last = window

    def _sticky_recovery(self, rec: dict[str, Any], now: datetime) -> None:
        """Keep a warm-up that has begun: a later plan with a short pause at its first slot (the
        minimum off) does not flip the phase back to ``away``."""
        v = (rec.get("inputs") or {}).get("vacation")
        window = vacation.window_from_view(v)
        if window is None or not isinstance(v, dict):
            return
        key = window.key()
        planned = v.get("recovery_start")
        at = datetime.fromisoformat(planned) if planned else None
        if at is not None and at <= now and key not in self._recovery:
            self._recovery[key] = at
        begun = self._recovery.get(key)
        if begun is not None and window.absent(now):
            v["recovery_start"] = _iso(begun)
            v["phase"] = "recovery"

    def vacation_recovery(self, window: vacation.VacationWindow | None) -> datetime | None:
        """The warm-up start of *window*: the one that has begun, else the latest plan's."""
        if window is None:
            return None
        begun = self._recovery.get(window.key())
        if begun is not None:
            return begun
        v = ((self.plan or {}).get("inputs") or {}).get("vacation")
        if vacation.window_from_view(v) is None or not isinstance(v, Mapping):
            return None
        if (v.get("start"), v.get("end")) != window.key() or not v.get("recovery_start"):
            return None
        return datetime.fromisoformat(v["recovery_start"])

    def _vacation_in_window(self, today: date, now: datetime) -> bool:
        """A vacation overlaps the refit's trailing window (``window_days`` local days before
        *today*) or is under way."""
        days = int(self.cfg.model.reestimate.window_days)
        first = datetime.combine(today - timedelta(days=days), datetime.min.time(), tzinfo=self.tz)
        return any(
            start < now and end > first and end > start for start, end in self.vacations.items()
        )

    def _maybe_refit(
        self, d, today: date, now: datetime, read: vacation.VacationRead | None = None
    ) -> None:
        every = int(self.cfg.model.reestimate.every_days)
        if self.model is not None:
            due = date.fromisoformat(self.model["valid_from"]) + timedelta(days=every)
            if today < due:
                self.refit_paused = None
                return
            if self._vacation_in_window(today, now):
                # an empty house has no occupancy gains: refitting ua/q0 on it would bias the model
                # for weeks after the return, so the last parameters stay (ADR-0010)
                if self.refit_paused is None:
                    log.info("climate shadow refit paused: a vacation is in its window")
                self.refit_paused = "vacation"
                return
        self.refit_paused = None
        model = shadow.refit(self.prior, d, self.cfg, today)
        model["kind"] = "climate_model"
        model["mpc_config_sha256"] = self.cfg.sha256
        model["climate_config_sha256"] = self.ccfg.sha256
        self._write("climate_models", now, model)
        self.model = model
        self.refit_at = now
        if self.metrics is not None:
            self.metrics.climate_refits.labels(
                result="refitted" if model["refitted"] else "prior_kept"
            ).inc()

    def _score_due(self, d, now: datetime) -> int:
        oldest = now - timedelta(days=self.settings.score_lookback_days)
        n = 0
        for key in sorted(self.queue):
            dec = self.queue[key]
            t0 = datetime.fromisoformat(key)
            if t0 < oldest:
                del self.queue[key]
                self._count_score("expired")
                continue
            if not shadow.scoring_due(t0, self.horizon_hours, now, self.tz):
                continue
            try:
                rec = shadow.score(self.prior, dec, d, self.cfg)
            except Exception:  # noqa: BLE001 - one bad origin must not stop the others
                log.exception("climate shadow scoring of %s failed", key)
                rec = None
            del self.queue[key]
            if rec is None:
                self._count_score("failed")
                continue
            self._write("climate_scores", now, rec)
            self.scores.append(rec)
            self._count_score("scored")
            n += 1
        return n

    def _record_days(self, d, now: datetime) -> int:
        today = local_date(now, self.tz)
        n = 0
        for j, (label, _lo, _hi) in enumerate(d.days):
            day = label.isoformat()
            if label >= today or day in self.days or day not in self.day_regimes:
                continue
            if not d.day_complete[j]:
                continue
            rec = shadow.day_record(d, j)
            rec["regimes"] = sorted(r for r in self.day_regimes[day] if r)
            self._write("climate_days", now, rec)
            self.days[day] = rec
            n += 1
        return n

    # ------------------------------------------------------------------ metrics

    def _count_decision(self, rec: Mapping[str, Any]) -> None:
        if self.metrics is None:
            return
        self.metrics.climate_runs.labels(result=rec["status"]).inc()
        for s in (rec.get("plan") or {}).get("solves") or []:
            self.metrics.climate_solves.labels(status=s.get("status") or "error").inc()
            if s.get("solve_s") is not None:
                self.metrics.climate_solve_duration.observe(float(s["solve_s"]))

    def _count_score(self, result: str) -> None:
        if self.metrics is not None:
            self.metrics.climate_scores.labels(result=result).inc()

    def snapshot(self) -> dict[str, Any]:
        """What ``/metrics`` and ``/status`` show (no household figure beyond model errors)."""
        last = dict(self.last)
        at = last.pop("at", None)
        return {
            "loaded": True,
            "prior_id": self.prior.prior_id,
            "model_hash": (self.model or {}).get("model_hash"),
            "model_valid_from": (self.model or {}).get("valid_from"),
            "last_run_at": at,
            "last_refit_at": self.refit_at,
            "refit_paused": self.refit_paused,
            "last": last,
            "queue": len(self.queue),
            "report": self.report,
        }


def _top_share(rec: Mapping[str, Any]) -> float | None:
    plan = rec.get("plan") or {}
    heat = plan.get("plan_heat_slots")
    return plan.get("plan_top_slots", 0) / heat if heat else None


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def _resolve(path: str, base: Path) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else base / p


def config_paths(config: ServiceConfig) -> tuple[Path, Path]:
    """``(climate.yaml, climate-mpc.yaml)`` of the shadow, relative to the service config."""
    s = config.climate_shadow
    base = Path(config.source_path).parent if config.source_path else Path.cwd() / "config"
    return _resolve(s.climate_config, base), _resolve(s.mpc_config, base)


def build_job(
    config: ServiceConfig,
    *,
    data_dir: Path,
    sink: ShadowSink,
    code_version: str,
    samples: SampleSource | None = None,
    metrics: Any = None,
    redecide_foreign: bool = False,
) -> ClimateShadowJob:
    """Load the configs, the prior and the site file and wire the Prometheus cache.

    Raises ``ValueError``/``OSError`` with a readable message when something is missing; the
    runtime reports it and retries."""
    s = config.climate_shadow
    climate_path, mpc_path = config_paths(config)
    ccfg = load_climate_config(climate_path)
    cfg = load_mpc_config(mpc_path)
    if s.prior is None:
        raise ValueError("climate_shadow.prior is not set")
    prior = shadow.load_prior(data_dir, s.prior)
    site = load_site(data_dir / SITE_FILE)
    if samples is None:
        if not config.prometheus.url:
            raise ValueError("the climate shadow needs BATTERY_OPTIMIZER_PROMETHEUS_URL")
        samples = PrometheusClimateSamples(
            base_url=config.prometheus.url,
            timeout_s=config.prometheus.timeout_s,
            sources=shadow.live_sources(config.entities.get("price")),
            window=timedelta(days=int(cfg.model.reestimate.window_days) + 2),
        )
    return ClimateShadowJob(
        settings=s,
        climate_config=ccfg,
        mpc_config=cfg,
        prior=prior,
        site=site,
        sink=sink,
        data_dir=data_dir,
        samples=samples,
        code_version=code_version,
        metrics=metrics,
        redecide_foreign=redecide_foreign,
    )
