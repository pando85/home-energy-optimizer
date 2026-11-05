"""Live shadow of B0's thermal model and the price-blind comfort MPC (track B, step B2). Read-only.

The resident service runs this every ``replan_every_minutes`` of ``config/climate-mpc.yaml``
(hourly).  It reads, never writes: Prometheus history, a few Home Assistant states the service
already holds and the service's own weather-forecast archive.  **The MPC never acts**: every plan
is a model prediction of a plan that is not executed.

Data path (the same definitions as the B1 replay, :mod:`.mpc_replay`):

* raw Prometheus samples of :data:`LIVE_SIGNALS` go through the feature builder's own slot rules
  (:func:`battery_optimizer.features.assemble_table`) and :func:`.data.build_climate_data`, so a
  live slot is computed exactly as an offline feature-set slot: debiased outdoor sensor, DHW and
  cooling masks, the comfort-room mean ``t_air`` and the replayed legacy permission ``u`` (from the
  PVPC price history);
* the model is :class:`ShadowPrior` (B0's ``b2d`` prior fitted offline on the last complete season,
  with its heat law, valve cap, observer gain and COP model) with ``ua`` and ``q0`` re-estimated
  every ``model.reestimate.every_days`` on the trailing ``window_days`` (:func:`refit`);
* one outdoor series everywhere (:func:`outdoor_series`): B0's ``t_out_model`` (the debiased unit
  sensor), with the last valid reading held where it is unknown.  The prior fit, the weekly refit,
  the observer (two warm-up days before the decision) and the gate scoring all read it; live, the
  data end at the decision, so it is causal;
* over the horizon the outdoor temperature is the **issued forecast** of the archive (B1's
  ``perfect`` series replaced by a real forecast), else the last reading held; the DHW mask of
  today's slots is the comfort controller's plan (``input_text.comfort_dhw_plan``, see
  :func:`parse_dhw_plan`) and every other slot, or every slot without a plan for today, repeats
  yesterday's measured DHW slots; setpoints follow the design schedule with optimum start (and
  away); the run in progress comes from the measured slots before the decision.

Scoring (:func:`score`), once the local day holding the last horizon slot is complete.  The
**gate prediction** (preregistration amendment of 2026-10-31) is the model's comfort-room mean
``t_air`` (B0's three ``comfort.rooms``, zero offset) from the persisted origin state, driven by the
**measured heat and the measured outdoor temperature**: B0's 7.3 basis and B1's ``air_q``.  It tests
the thermal model, not the operational forecast.  Diagnostics only: the same with the outdoor series
the plan used (``heat_plan_weather``), the four thermostat rooms of design section 3 with their
prior-season offsets (``design_rooms``), and B1's action-driven block (the replayed permission in
the legacy regime; the **measured space-heating run** in the comfort regime, a weaker test of the
heat law).  Midnight origins add the day's heat delivery (``day``), and every complete local day
gets its validity (:func:`day_record`).

Nothing here does I/O except :func:`load_prior` / :func:`save_prior`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from battery_optimizer.climate import b2d, fit, mpc_replay, plant
from battery_optimizer.climate.config import ClimateConfig
from battery_optimizer.climate.cop import CopModel
from battery_optimizer.climate.data import ClimateData, Site, build_climate_data, room_offsets
from battery_optimizer.climate.mpc import PlanSettings
from battery_optimizer.climate.mpc_config import MpcConfig, override
from battery_optimizer.climate.mpc_model import Exogenous, HeatLaw
from battery_optimizer.climate.shadow_gates import GATE_ROOMS
from battery_optimizer.extract_prom import CLIMATE_ROOMS, ENTITY_SPECS
from battery_optimizer.features import (
    CLIMATE_SIGNALS,
    DEFAULT_MAX_GAP_SECONDS,
    DEFAULT_MIN_COVERAGE,
    KIND_GAUGE,
    RawSeries,
    Signal,
    assemble_table,
    signal_columns,
    slot_edges_us,
)

__all__ = [
    "DESIGN_ROOMS",
    "LIVE_SIGNALS",
    "PRICE_KEY",
    "PRIORS_DIR",
    "REGIMES",
    "LiveInputs",
    "ShadowPrior",
    "bridge_outages",
    "build_live_data",
    "comfort_helper_levels",
    "day_record",
    "decide",
    "detect_regime",
    "fit_shadow_prior",
    "forecast_series",
    "live_config",
    "live_sources",
    "load_prior",
    "model_hash",
    "observer_inputs",
    "outdoor_series",
    "parse_dhw_plan",
    "refit",
    "save_prior",
    "score",
    "scoring_due",
]

SCHEMA_VERSION = 1
PRIOR_VERSION = 1
PRIORS_DIR = Path("analysis") / "v1" / "climate-shadow" / "priors"
PRIOR_FILE = "prior.json"
STEP_US = 900 * 1_000_000
WARMUP_SLOTS = mpc_replay.WARMUP_SLOTS
#: Longest run of slots without a heat reading (a Prometheus or Home Assistant outage) the live
#: observer bridges; a longer one restarts it and costs ``WARMUP_SLOTS`` before the next plan.
MAX_BRIDGE_SLOTS = 4

#: The thermostat rooms of design section 3: the preregistered room-mean.
DESIGN_ROOMS = tuple(CLIMATE_ROOMS)

#: Regime of a record: ``legacy`` (no comfort-control helper in HA), ``comfort`` (the helper is
#: on), ``comfort_off`` (present but off) and ``unknown`` (HA not synced or the state unavailable).
REGIMES = ("legacy", "comfort", "comfort_off", "unknown")

#: The climate signals the live job reads; the others of the climate feature set stay unknown.
_LIVE_ALIASES = frozenset(
    {
        *(f"room_{r}_{k}" for r in DESIGN_ROOMS for k in ("temp", "setpoint")),
        "outdoor_temp",
        "flow_target_temp",
        "compressor_freq",
        "hp_heat",
        "hp_heat_out",
        "hp_dhw",
        "hp_dhw_out",
        "hp_cool",
        "hp_cool_out",
        "dhw_force",
        "climate_enable",
        "climate_min_hours",
    }
)
LIVE_SIGNALS: tuple[Signal, ...] = tuple(s for s in CLIMATE_SIGNALS if s.alias in _LIVE_ALIASES)
PRICE_KEY = "price"
PRICE_METRIC = "hass_sensor_unit_u0x20ac_per_kwh"

#: The comfort controller's per-room helpers (``input_number``), read from Prometheus like every
#: other input: ``floor`` is the always-minimum, ``day_min`` the minimum in the shared day window.
#: ``(helper kind, entity prefix, accepted range in degC, both ends inclusive)``; a value outside
#: its range is ignored (the controller's own ranges).
COMFORT_HELPERS = {
    "floor": ("input_number.comfort_floor_", (18.0, 23.0)),
    "day_min": ("input_number.comfort_day_minimum_", (18.0, 26.0)),
}
COMFORT_METRIC = "hass_input_number_state"
#: A helper sample older than this at the decision is not trusted (the exporter emits one every
#: scrape, so an older last sample means an outage or a removed helper).
COMFORT_MAX_AGE_S = 2 * 3600


def live_sources(price_entity: str | None) -> dict[str, tuple[str, str]]:
    """``source key -> (entity id, Prometheus metric)`` of every series the job fetches."""
    out = {s.source: (s.entity, ENTITY_SPECS[s.extract_key].metric) for s in LIVE_SIGNALS}
    if price_entity:
        out[PRICE_KEY] = (price_entity, PRICE_METRIC)
    for kind, (prefix, _range) in COMFORT_HELPERS.items():
        for room in DESIGN_ROOMS:
            out[f"comfort_{kind}_{room}"] = (f"{prefix}{room}", COMFORT_METRIC)
    return out


def comfort_helper_levels(
    series: Mapping[str, RawSeries], at: datetime
) -> dict[str, dict[str, float]]:
    """``{"floor": {room: degC}, "day_min": {room: degC}}`` in effect at *at*: the last helper
    sample at or before it, when it is at most :data:`COMFORT_MAX_AGE_S` old and inside the
    controller's range.  A missing, stale, non-finite or out-of-range value is simply absent, so
    the config's value stands for that room and helper."""
    at_us = int(at.timestamp() * 1e6)
    out: dict[str, dict[str, float]] = {kind: {} for kind in COMFORT_HELPERS}
    for kind, (_prefix, (lo, hi)) in COMFORT_HELPERS.items():
        for room in DESIGN_ROOMS:
            raw = series.get(f"comfort_{kind}_{room}")
            if raw is None or raw.t_us.size == 0:
                continue
            i = int(np.searchsorted(raw.t_us, at_us, side="right")) - 1
            if i < 0 or at_us - int(raw.t_us[i]) > COMFORT_MAX_AGE_S * 1_000_000:
                continue
            value = float(raw.values[i])
            if math.isfinite(value) and lo <= value <= hi:
                out[kind][room] = value
    return out


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def build_live_data(
    series: Mapping[str, RawSeries],
    *,
    start: datetime,
    end: datetime,
    config: ClimateConfig,
    site: Site,
) -> ClimateData:
    """The climate signals on ``[start, end)`` from raw samples (slots without samples unknown).

    The price series, when present, is the slot's time-weighted mean of the PVPC sensor, which
    the legacy rule replay reads for ``u``."""
    edges = slot_edges_us(start, end)
    table = assemble_table(dict(series), edges, signals=CLIMATE_SIGNALS)
    prices = None
    raw = series.get(PRICE_KEY)
    if raw is not None:
        sig = Signal(raw.entity, PRICE_KEY, KIND_GAUGE)
        prices = signal_columns(
            sig,
            raw.t_us,
            raw.values,
            edges,
            max_gap_seconds=DEFAULT_MAX_GAP_SECONDS,
            min_coverage=DEFAULT_MIN_COVERAGE,
        )[f"{PRICE_KEY}_mean"]
    return build_climate_data(table, config=config, site=site, prices=prices, feature_set_id="live")


def outdoor_series(d: ClimateData) -> np.ndarray:
    """B0's ``t_out_model``, with the last valid reading held where it is unknown (a compressor
    run longer than the debiasing cap, or the slots after the last valid reading)."""
    return np.where(np.isfinite(d.t_out_model), d.t_out_model, mpc_replay.causal_outdoor(d))


def observer_inputs(d: ClimateData, diurnal: np.ndarray) -> np.ndarray:
    """``w`` of the history on :func:`outdoor_series` (cooling slots unknown)."""
    w = b2d.full_inputs(d, diurnal)
    w[:, 1] = np.where(d.cooling, np.nan, outdoor_series(d))
    return w


def bridge_outages(
    w: np.ndarray, cooling: np.ndarray, upto: int, max_slots: int = MAX_BRIDGE_SLOTS
) -> np.ndarray:
    """*w* with the heat input ``Q`` held over the short runs of slots before *upto* that have no
    reading.

    The observer restarts on any unknown input, and a plan needs ``WARMUP_SLOTS`` of unbroken
    state, so one 8-minute scrape outage (a Home Assistant restart) would stop the shadow for a
    day.  Only a run of at most *max_slots* with a known ``Q`` before it is bridged, with that
    last ``Q`` (causal); cooling slots are unknown by design and never bridged."""
    out = w.copy()
    q = out[:, 0]
    n = min(upto, q.size)
    i = 1
    while i < n:
        if np.isfinite(q[i]) or cooling[i] or not np.isfinite(q[i - 1]):
            i += 1
            continue
        j = i
        while j < n and not np.isfinite(q[j]) and not cooling[j]:
            j += 1
        if j - i <= max_slots and np.isfinite(out[i:j, 1:]).all():
            q[i:j] = q[i - 1]
        i = j
    return out


def live_config(cfg: MpcConfig) -> MpcConfig:
    """The MPC config the live decision reads: the horizon series of the data are the declared
    inputs (forecast, planned DHW), which is what B1 calls ``perfect``."""
    return override(cfg, ("replay", "weather"), "perfect")


# ---------------------------------------------------------------------------
# Prior
# ---------------------------------------------------------------------------


def _nan_none(a: Any) -> Any:
    if isinstance(a, np.ndarray):
        a = a.tolist()
    if isinstance(a, list):
        return [_nan_none(x) for x in a]
    if isinstance(a, dict):
        return {k: _nan_none(v) for k, v in a.items()}
    if isinstance(a, float) and not math.isfinite(a):
        return None
    if isinstance(a, np.floating | np.integer):
        return _nan_none(a.item())
    return a


def _canonical(obj: Any) -> bytes:
    return json.dumps(
        _nan_none(obj), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


@dataclass(frozen=True)
class ShadowPrior:
    """The offline part of the live model, written by ``hack/climate_shadow_prior.py``."""

    prior: mpc_replay.Prior
    cop_model: CopModel
    #: Median offset of every thermostat room from ``t_air`` by local hour (prior season).
    room_offsets: dict[str, list[float | None]]
    lineage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        p = self.prior
        return _nan_none(
            {
                "version": PRIOR_VERSION,
                "season": p.season,
                "rooms": list(p.rooms),
                "params": p.params,
                "n_diurnal": p.n_diurnal,
                "fit_info": p.fit_info,
                "heat_law": p.law.to_dict(),
                "heat_law_fit": p.law_stats,
                "valve": {
                    "phi": p.cap.phi,
                    "margin_k": p.cap.margin_k,
                    "offsets": p.cap.offsets,
                },
                "process_var_k2": p.process_var,
                "gain": p.gain,
                "cop_model": self.cop_model.to_dict(),
                "room_offsets": self.room_offsets,
                "lineage": self.lineage,
            }
        )

    @property
    def prior_id(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict())).hexdigest()[:12]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ShadowPrior:
        if raw.get("version") != PRIOR_VERSION:
            raise ValueError(f"unsupported shadow prior version {raw.get('version')!r}")
        valve = raw["valve"]
        offsets = np.array(
            [[np.nan if v is None else float(v) for v in row] for row in valve["offsets"]], float
        )
        law = raw["heat_law"]
        prior = mpc_replay.Prior(
            season=str(raw["season"]),
            rooms=tuple(raw["rooms"]),
            params={k: float(v) for k, v in raw["params"].items()},
            n_diurnal=int(raw["n_diurnal"]),
            fit_info=dict(raw.get("fit_info") or {}),
            law=HeatLaw(*(float(law[k]) for k in ("h_e", "qm0", "qm1", "t_ref"))),
            law_stats=dict(raw.get("heat_law_fit") or {}),
            cap=plant.ValveCap(float(valve["phi"]), float(valve["margin_k"]), offsets),
            process_var=float(raw["process_var_k2"]),
            gain=np.array(raw["gain"], float),
        )
        return cls(
            prior=prior,
            cop_model=CopModel.from_dict(raw["cop_model"]),
            room_offsets={k: list(v) for k, v in raw["room_offsets"].items()},
            lineage=dict(raw.get("lineage") or {}),
        )

    def room_mean_offsets(self, rooms: Sequence[str]) -> np.ndarray:
        """``(24,)`` mean over *rooms* of their offsets from ``t_air`` (unknown hours count 0);
        for the four-room diagnostic only, the gate reads ``t_air`` itself."""
        missing = [r for r in rooms if r not in self.room_offsets]
        if missing:
            raise ValueError(f"the prior has no offsets for the room(s) {missing}")
        rows = np.array(
            [[0.0 if v is None else float(v) for v in self.room_offsets[r]] for r in rooms]
        )
        return rows.mean(axis=0)


def fit_shadow_prior(
    d: ClimateData,
    ccfg: ClimateConfig,
    cfg: MpcConfig,
    season: str,
    *,
    phi: float,
    lineage: Mapping[str, Any],
) -> ShadowPrior:
    """B1's prior (:func:`.mpc_replay.fit_prior`) on :func:`outdoor_series`, its COP model and
    every thermostat room's offset, all from the training slots of *season*."""
    hour_frac, weekend = b2d.slot_calendar(d)
    diurnal = b2d.diurnal_matrix(hour_frac, weekend, cfg.model.diurnal_harmonics)
    w = observer_inputs(d, diurnal)
    prior = mpc_replay.fit_prior(d, ccfg, season, w_hist=w, n_diurnal=diurnal.shape[1], phi=phi)
    cop_model = mpc_replay.fit_cop(d, ccfg, season)
    train = fit.train_slots(d, [season]) & d.train
    offsets = room_offsets(d.room_temp, d.rooms, d.t_air, d.local_hour, train)
    return ShadowPrior(
        prior=prior,
        cop_model=cop_model,
        room_offsets={r: _nan_none(offsets[i]) for i, r in enumerate(d.rooms)},
        lineage={
            **dict(lineage),
            "history_outdoor": "t_out_model, the last valid reading held where unknown",
            "feature_set_id": d.feature_set_id,
            "climate_config_sha256": ccfg.sha256,
            "mpc_config_sha256": cfg.sha256,
            "site_sha256": d.site_sha256,
        },
    )


def save_prior(root: Path, prior: ShadowPrior) -> Path:
    """Write ``<root>/analysis/v1/climate-shadow/priors/<prior_id>/prior.json`` (write-once)."""
    from battery_optimizer.features import json_bytes
    from battery_optimizer.paths import write_part_atomic

    path = root / PRIORS_DIR / prior.prior_id / PRIOR_FILE
    if path.exists():
        return path
    write_part_atomic(path, json_bytes(prior.to_dict()))
    return path


def load_prior(root: Path, prior_id: str) -> ShadowPrior:
    """The prior whose id starts with *prior_id*; its content must hash to its directory name."""
    base = root / PRIORS_DIR
    matches = sorted(p for p in base.glob(f"{prior_id}*") if (p / PRIOR_FILE).is_file())
    if len(matches) != 1:
        raise ValueError(f"climate shadow prior {prior_id!r} matches {len(matches)} under {base}")
    prior = ShadowPrior.from_dict(json.loads((matches[0] / PRIOR_FILE).read_text()))
    if prior.prior_id != matches[0].name:
        raise ValueError(f"{matches[0]}: the content hashes to {prior.prior_id}, not its name")
    return prior


# ---------------------------------------------------------------------------
# Weekly re-estimation
# ---------------------------------------------------------------------------


def model_hash(prior_id: str, params: Mapping[str, float]) -> str:
    """Identity of the model a decision used: the prior and the (re-estimated) parameters."""
    payload = {"prior_id": prior_id, "params": {k: float(v) for k, v in params.items()}}
    return hashlib.sha256(_canonical(payload)).hexdigest()[:16]


def refit(
    sp: ShadowPrior, d: ClimateData, cfg: MpcConfig, today: date, *, w: np.ndarray | None = None
) -> dict[str, Any]:
    """B1's weekly step at *today*: ``model.reestimate.params`` on the ``window_days`` complete,
    fit-month local days before *today*, the rest at the prior (the prior when no segment)."""
    re_cfg = cfg.model.reestimate
    if w is None:
        hour_frac, weekend = b2d.slot_calendar(d)
        w = observer_inputs(d, b2d.diurnal_matrix(hour_frac, weekend, cfg.model.diurnal_harmonics))
    day_index = {label: j for j, (label, _l, _h) in enumerate(d.days)}
    window = np.zeros(len(d.days), dtype=bool)
    for back in range(1, re_cfg.window_days + 1):
        k = day_index.get(today - timedelta(days=back))
        if k is not None and d.day_complete[k] and d.day_fit_month[k]:
            window[k] = True
    params, ok = b2d.reestimate(
        d, w, sp.prior.params, d.day_mask(window), tuple(re_cfg.params), sp.prior.n_diurnal
    )
    labels = [d.days[j][0] for j in np.flatnonzero(window)]
    return {
        "valid_from": today.isoformat(),
        "prior_id": sp.prior_id,
        "params": params,
        "reestimated": list(re_cfg.params),
        "refitted": bool(ok),
        "window": {
            "days": len(labels),
            "first": labels[0].isoformat() if labels else None,
            "last": labels[-1].isoformat() if labels else None,
        },
        "model_hash": model_hash(sp.prior_id, params),
    }


# ---------------------------------------------------------------------------
# Inputs read from Home Assistant and the forecast archive
# ---------------------------------------------------------------------------


def detect_regime(state: str | None, *, exists: bool, synced: bool) -> str:
    """The control regime from ``input_boolean.comfort_control_enable`` (see :data:`REGIMES`)."""
    if not synced:
        return "unknown"
    if not exists:
        return "legacy"
    value = (state or "").strip().lower()
    if value == "on":
        return "comfort"
    if value == "off":
        return "comfort_off"
    return "unknown"


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return at.astimezone(UTC) if at.tzinfo is not None else None


def parse_dhw_plan(
    text: str | None, *, starts_us: np.ndarray, tz: ZoneInfo, day: date
) -> tuple[np.ndarray, np.ndarray] | None:
    """``(mask, covered)`` of the comfort controller's DHW plan on the slots *starts_us*, or
    ``None`` when it says nothing about *day* (the decision's local date).

    The homelab app (``comfort_control.py``) writes ``input_text.comfort_dhw_plan`` as JSON:
    ``{"day": "YYYY-MM-DD", "start": <ISO with offset> | null, "end": ... | null, "forced": ..,
    "target_written": .., "done": "YYYY-MM-DD" | null}``, or ``{"done": ...}`` alone.  The plan
    covers *day* only, so ``covered`` is the slots of that local date and the caller fills the
    rest of the horizon:

    * ``day`` equal to *day*: DHW where a slot starts in ``[start, end)``; ``start`` and ``end``
      both null (the window is over, or none was chosen) is no DHW for the rest of the day;
    * otherwise ``done`` equal to *day*: today's window is finished, no DHW for the rest of it;
    * anything else (a stale plan of another day, empty, ``unknown``, unparseable, one of
      ``start``/``end`` null or without an offset): ``None``."""
    if text is None or text.strip().lower() in ("", "unknown", "unavailable", "none"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, Mapping):
        return None
    t = np.asarray(starts_us, dtype=np.int64)
    local = [datetime.fromtimestamp(int(u) / 1e6, tz=UTC).astimezone(tz).date() for u in t]
    covered = np.array([x == day for x in local], dtype=bool)
    if obj.get("day") == day.isoformat():
        if obj.get("start") is None and obj.get("end") is None:
            return np.zeros(t.size, dtype=bool), covered
        a, b = _instant(obj.get("start")), _instant(obj.get("end"))
        if a is None or b is None or b <= a:
            return None
        lo, hi = int(a.timestamp() * 1e6), int(b.timestamp() * 1e6)
        return covered & (t >= lo) & (t < hi), covered
    if obj.get("done") == day.isoformat():
        return np.zeros(t.size, dtype=bool), covered
    return None


def forecast_series(record: Mapping[str, Any] | None, starts_us: np.ndarray) -> np.ndarray:
    """Outdoor temperature of a ``weather.get_forecasts`` record at the slot midpoints: linear
    between the entries, the first and last entry held outside them; ``NaN`` without entries."""
    t = np.asarray(starts_us, dtype=np.int64) + STEP_US // 2
    if not record:
        return np.full(t.size, np.nan)
    points = []
    for entry in record.get("forecast") or []:
        if not isinstance(entry, Mapping):
            continue
        try:
            at = datetime.fromisoformat(str(entry["datetime"]))
            value = float(entry["temperature"])
        except KeyError, TypeError, ValueError:
            continue
        if at.tzinfo is None or not math.isfinite(value):
            continue
        points.append((int(at.timestamp() * 1e6), value))
    if not points:
        return np.full(t.size, np.nan)
    points.sort()
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    return np.interp(t.astype(float), xs, ys)


@dataclass(frozen=True)
class LiveInputs:
    """What the service read from Home Assistant and its own archive for one decision."""

    regime: str
    regime_state: str | None = None
    dhw_plan: str | None = None
    away: bool | None = None
    forecast: Mapping[str, Any] | None = None


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _slot_index(d: ClimateData, at: datetime) -> int | None:
    us = int(at.timestamp() * 1e6)
    i = int(np.searchsorted(d.starts_us, us))
    return i if i < d.n and int(d.starts_us[i]) == us else None


def _season_model(d: ClimateData, model, t0: int, w: np.ndarray) -> b2d.SeasonModel:
    lo = max(0, t0 - 2 * WARMUP_SLOTS)
    states = np.full((d.n, 2), np.nan)
    age = np.full(d.n, -1, dtype=np.int64)
    xs, ag = model.observe(w[lo : t0 + 1], d.t_air[lo : t0 + 1])
    states[lo : t0 + 1], age[lo : t0 + 1] = xs, ag
    return b2d.SeasonModel("live", "", [(0, model)], states, age, {})


def _context(
    sp: ShadowPrior,
    d: ClimateData,
    cfg: MpcConfig,
    model,
    t0: int,
    settings: PlanSettings,
    comfort: Mapping[str, Mapping[str, float]] | None = None,
) -> mpc_replay.Context:
    hour_frac, weekend = b2d.slot_calendar(d)
    diurnal = b2d.diurnal_matrix(hour_frac, weekend, cfg.model.diurnal_harmonics)
    if diurnal.shape[1] != sp.prior.n_diurnal:
        raise ValueError("model.diurnal_harmonics differs from the prior's")
    w = bridge_outages(observer_inputs(d, diurnal), d.cooling, t0)
    causal = mpc_replay.causal_outdoor(d)
    return mpc_replay.Context(
        d=d,
        cfg=cfg,
        settings=settings,
        season=_season_model(d, model, t0, w),
        cop_model=sp.cop_model,
        diurnal=diurnal,
        hour_frac=hour_frac,
        weekend=weekend,
        t_out_model=w[:, 1],
        t_out_causal=causal,
        space_heat=mpc_replay.space_heat_state(d),
        comfort_override=comfort,
    )


def _round(a: np.ndarray, nd: int) -> list[float | None]:
    return [None if not np.isfinite(v) else round(float(v), nd) for v in np.asarray(a, float)]


def decide(
    sp: ShadowPrior,
    model_rec: Mapping[str, Any],
    d: ClimateData,
    cfg: MpcConfig,
    t0_at: datetime,
    inputs: LiveInputs,
    *,
    settings: PlanSettings | None = None,
    comfort: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    """The decision record at *t0_at* (a slot start of *d*; *d* extends one horizon past it).

    *comfort* is :func:`comfort_helper_levels` at *t0_at*: the per-room helper values in effect at
    the decision, held over the horizon (the controller's schedule at the decision is the best
    knowledge of the schedule after it); absent values fall back to the config.

    ``origin`` (the observer state the predictions start from) is kept whenever it exists, so the
    prediction is scored even if the plan fails; ``plan`` is the MPC's (never executed)."""
    cfg = live_config(cfg)
    settings = settings or PlanSettings.from_config(cfg)
    h = int(cfg.horizon_hours * 4)
    t0 = _slot_index(d, t0_at)
    if t0 is None or t0 + h > d.n or t0 < 1:
        raise ValueError(f"the live grid does not hold the horizon of {t0_at.isoformat()}")
    model = sp.prior.model(dict(model_rec["params"]))
    hi = t0 + h
    outdoor = outdoor_series(d)
    # horizon outdoor: the issued forecast, else the last reading held
    fc = forecast_series(inputs.forecast, d.starts_us[t0:hi])
    weather = "forecast" if np.isfinite(fc).all() else "persistence"
    horizon_t_out = fc if weather == "forecast" else np.full(h, outdoor[t0 - 1])
    history_t_out = outdoor.copy()
    history_t_out[t0:hi] = horizon_t_out
    # horizon DHW: the controller's plan for today's slots, yesterday's measured slots elsewhere
    back = np.arange(t0, hi) - mpc_replay.SLOTS_PER_DAY
    horizon_dhw = np.where(back >= 0, d.dhw[np.maximum(back, 0)], False)
    local_day = t0_at.astimezone(d.tz).date()
    plan = parse_dhw_plan(inputs.dhw_plan, starts_us=d.starts_us[t0:hi], tz=d.tz, day=local_day)
    dhw_source = "history"
    if plan is not None:
        plan_mask, covered = plan
        horizon_dhw = np.where(covered, plan_mask, horizon_dhw)
        dhw_source = "plan_today+history" if not covered.all() else "plan"
    dhw = d.dhw.copy()
    dhw[t0:hi] = horizon_dhw
    live = replace(d, t_out_debiased=history_t_out, t_out_filled=None, dhw=dhw)
    ctx = _context(sp, live, cfg, model, t0, settings, comfort)
    local = t0_at.astimezone(d.tz)
    rec: dict[str, Any] = {
        "kind": "climate_decision",
        "t0": t0_at.astimezone(UTC).isoformat(),
        "local_day": local.date().isoformat(),
        "local_hour": local.hour,
        "midnight": bool(t0 == d.days[int(d.day_of_slot[t0])][1]),
        "regime": inputs.regime,
        "regime_state": inputs.regime_state,
        "prior_id": sp.prior_id,
        "model_hash": model_rec["model_hash"],
        "model_params": dict(model_rec["params"]),
        "mpc_config_sha256": cfg.sha256,
        "inputs": {
            "weather": weather,
            "forecast_requested_at": (inputs.forecast or {}).get("requested_at"),
            "forecast_minus_observed_k": None
            if not (np.isfinite(fc[0]) and np.isfinite(outdoor[t0 - 1]))
            else round(float(fc[0] - outdoor[t0 - 1]), 3),
            "t_out": _round(horizon_t_out, 2),
            "dhw_source": dhw_source,
            "dhw_slots": [int(i) for i in np.flatnonzero(horizon_dhw)],
            "away": inputs.away,
        },
    }
    levels = mpc_replay.comfort_levels(model.rooms, cfg, comfort)
    rec["inputs"]["comfort"] = {
        "rooms": list(model.rooms),
        "floor_c": levels.floor.tolist(),
        "day_min_c": levels.day_min.tolist(),
        "from_helpers": {k: sorted(v) for k, v in (comfort or {}).items() if v},
    }
    x0 = ctx.season.states[t0]
    age = int(ctx.season.age[t0])
    if np.isfinite(x0).all() and age >= WARMUP_SLOTS:
        rec["origin"] = {"x0": [float(v) for v in x0], "age_slots": age}
    inp = mpc_replay.decision_inputs(ctx, t0)
    if inp is None:
        rec["status"] = "no_inputs" if "origin" in rec else "no_state"
        return rec
    if inputs.away:
        floor = levels.floor[:, None]
        away_sp = floor + float(cfg.comfort.setpoint_margin_k)
        inp["ex"] = replace(
            inp["ex"], setpoints=np.broadcast_to(away_sp, inp["ex"].setpoints.shape).copy()
        )
        inp["minimum"] = np.broadcast_to(floor, inp["minimum"].shape).copy()
    rec["inputs"].update(
        {
            "prev_on": bool(inp["prev_on"]),
            "prev_run_slots": int(min(inp["prev_run_slots"], 10**6)),
            "setpoints": [_round(row, 2) for row in inp["ex"].setpoints],
        }
    )
    plan_rec = mpc_replay.run_decision(ctx, t0, accuracy=False, per_slot=True, inp=inp)
    assert plan_rec is not None
    plan_rec.pop("t0", None)
    plan_rec.pop("local_hour", None)
    rec["plan"] = plan_rec
    ok = plan_rec["status"] in ("optimal", "gap", "time_limit")
    rec["status"] = "planned" if ok else "plan_failed"
    return rec


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def scoring_due(t0_at: datetime, horizon_hours: int, now: datetime, tz: ZoneInfo) -> bool:
    """An origin is scored once the local day holding its last horizon slot has ended."""
    last = t0_at + timedelta(hours=horizon_hours) - timedelta(minutes=15)
    day_end = datetime.combine(last.astimezone(tz).date() + timedelta(days=1), time(0), tzinfo=tz)
    return now >= day_end


def _room_mean(d: ClimateData, rooms: Sequence[str]) -> np.ndarray:
    return np.vstack([d.room_temp[r] for r in rooms]).mean(axis=0)  # NaN unless all known


def check_gate_rooms(d: ClimateData) -> None:
    """The gate's room-mean is ``t_air`` over the preregistered rooms; refuse any other set."""
    if tuple(d.comfort_rooms) != GATE_ROOMS:
        raise ValueError(
            f"comfort.rooms {list(d.comfort_rooms)} differ from the preregistered gate rooms "
            f"{list(GATE_ROOMS)}"
        )


def score(
    sp: ShadowPrior,
    decision: Mapping[str, Any],
    d: ClimateData,
    cfg: MpcConfig,
) -> dict[str, Any] | None:
    """The score record of one decision with an origin, from the measured *d*; ``None`` when *d*
    does not hold the origin and its horizon."""
    if "origin" not in decision:
        return None
    check_gate_rooms(d)
    cfg = live_config(cfg)
    h = int(cfg.horizon_hours * 4)
    t0_at = datetime.fromisoformat(decision["t0"])
    t0 = _slot_index(d, t0_at)
    if t0 is None or t0 < 1 or t0 + h > d.n:
        return None
    hi = t0 + h
    model = sp.prior.model(dict(decision["model_params"]))
    ctx = _context(sp, d, cfg, model, t0, PlanSettings.from_config(cfg))
    ex = Exogenous(
        t_out=outdoor_series(d)[t0:hi],
        solar=d.solar[t0:hi],
        diurnal=ctx.diurnal[t0:hi],
        local_hour=d.local_hour[t0:hi].astype(np.int64),
        setpoints=np.zeros((len(model.rooms), h)),
    )
    x0 = np.array(decision["origin"]["x0"], float)
    inp = {"x0": x0, "ex": ex, "dhw": d.dhw[t0:hi]}
    regime = decision.get("regime")
    action = "measured_run" if regime in ("comfort", "comfort_off") else "permission"
    midnight = bool(decision.get("midnight"))
    acc = mpc_replay.open_loop_accuracy(ctx, t0, inp, model, midnight, action=action)
    # the gate basis: measured heat and measured outdoor (B0 7.3, B1 air_q), whatever the action
    q_meas = np.where(d.dhw[t0:hi], 0.0, d.q_heat_kw[t0:hi])
    xs_q, n_q = mpc_replay.heat_driven(model, x0, ex, q_meas)
    # diagnostic: the same with the outdoor series the plan used (forecast or the reading held)
    planned = np.array([np.nan if v is None else v for v in decision["inputs"]["t_out"]], float)
    xs_f, n_f = (None, 0)
    if planned.size == h and np.isfinite(planned).all():
        xs_f, n_f = mpc_replay.heat_driven(model, x0, replace(ex, t_out=planned), q_meas)
    four = _room_mean(d, DESIGN_ROOMS)
    four_off = sp.room_mean_offsets(DESIGN_ROOMS)

    def err(xs: np.ndarray | None, n: int, j: int, y: float, off: float = 0.0) -> float | None:
        e = xs[j + 1, 0] + off - y if xs is not None and j < n else np.nan
        return float(e) if np.isfinite(e) else None

    def diff(a: float, b: float) -> float | None:
        return float(a - b) if np.isfinite(a - b) else None

    room_mean: dict[str, dict[str, float | None]] = {}
    design: dict[str, dict[str, float | None]] = {}
    for hh in cfg.replay.accuracy_horizons_hours:
        j = 4 * hh - 1
        y = d.t_air[t0 + j]  # the gate: comfort-room mean, zero offset
        room_mean[f"h{hh}"] = {
            "heat": err(xs_q, n_q, j, y),
            "heat_plan_weather": err(xs_f, n_f, j, y),
            "persistence": diff(d.t_air[t0 - 1], y),
        }
        off = four_off[int(d.local_hour[t0 + j])]
        design[f"h{hh}"] = {
            "heat": err(xs_q, n_q, j, four[t0 + j], off),
            "persistence": diff(four[t0 - 1], four[t0 + j]),
        }
    plan = decision.get("plan") or {}
    solves = plan.get("solves") or []
    out: dict[str, Any] = {
        "kind": "climate_score",
        "t0": decision["t0"],
        "local_day": decision["local_day"],
        "midnight": midnight,
        "regime": regime,
        "prior_id": decision.get("prior_id"),
        "mpc_config_sha256": decision.get("mpc_config_sha256"),
        "model_hash": decision.get("model_hash"),
        "action": action,
        "weather": decision["inputs"]["weather"],
        "gate_rooms": list(GATE_ROOMS),
        "room_mean": room_mean,
        "design_rooms": design,
        "accuracy": acc,
        "plan": {
            "status": plan.get("status"),
            "solve_s": float(sum(s.get("solve_s") or 0.0 for s in solves)),
            "heat_slots": plan.get("plan_heat_slots"),
            "top_slots": plan.get("plan_top_slots"),
            "kwh": plan.get("plan_kwh") if midnight else None,
        },
    }
    return out


def day_record(d: ClimateData, j: int) -> dict[str, Any]:
    """Validity of local day *j* (preregistration section 1): space heating in part of the day,
    and the gate rooms, the heat-pump energy and the outdoor series present in at least 90% of
    the slots."""
    label, lo, hi = d.days[j]
    n = hi - lo
    rooms = np.isfinite(np.vstack([d.room_temp[r][lo:hi] for r in GATE_ROOMS])).all(axis=0)
    energy = np.isfinite(d.q_heat_kwh[lo:hi]) & np.isfinite(d.e_heat_kwh[lo:hi])
    outdoor = np.isfinite(d.t_out[lo:hi])
    heating = mpc_replay.space_heat_state(d)[lo:hi] == 1.0
    coverage = {
        "rooms": float(rooms.mean()),
        "energy": float(energy.mean()),
        "outdoor": float(outdoor.mean()),
    }
    return {
        "kind": "climate_day",
        "day": label.isoformat(),
        "slots": int(n),
        "complete": bool(d.day_complete[j]),
        "coverage": coverage,
        "space_heat_slots": int(heating.sum()),
        "valid_heating_day": bool(
            d.day_complete[j] and heating.any() and min(coverage.values()) >= 0.9
        ),
        "gate_rooms": list(GATE_ROOMS),
    }
