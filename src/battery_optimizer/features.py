"""Slot-aligned sub-load feature table (P3 of the load-forecaster improvement plan).

The canonical dataset (``build``) carries one number per 15-minute slot: the gross load of the
whole-home meter.  This module builds a **separate** table next to it that carries the
observable parts of that load and the signals that drive them: the servers circuit, the heat
pump (heating, hot water, cooling), the measured outdoor temperature and the AppDaemon control
states.  It reads the raw Prometheus samples of the entities in :data:`SIGNALS` and never writes
into a canonical dataset directory, so a canonical ``dataset_id`` and the golden benchmark runs
are unaffected by it.

Where it lives
--------------
``<data_dir>/analysis/v1/features/<feature_set_id>/`` holds one Parquet part per build and a
``features_manifest.json`` with the hashes of every raw part that was read, the code version,
the parameters and the quality checks.  The id hashes exactly those inputs, so the same inputs
give the same id and identical bytes, and the store is write-once like the rest of the data
directory.

Integration rule (power series -> energy per slot)
--------------------------------------------------
Prometheus holds one sample per scrape (about one per minute, at irregular sub-second offsets).
A power sample is a **sample-and-hold** value: the value read at scrape time ``t_i`` is taken to
hold until the next sample ``t_{i+1}``.  The energy of a slot is the exact integral of that step
function over the slot, so a span that straddles a slot edge is split in proportion to time.

*Max-sample-gap rule.*  A span ``[t_i, t_{i+1})`` is **covered** only when
``t_{i+1} - t_i <= max_gap_seconds`` (default 300 s, five scrape intervals).  A longer span is a
gap: it contributes neither energy nor coverage.  The time after the last sample of a series is
also not covered.  Nothing is interpolated across a gap.

*Coverage and quality.*  ``<series>_coverage`` is the covered fraction of the slot (0 to 1).

* ``measured``: the whole slot is covered.
* ``partial``: coverage is at least ``min_coverage`` (default 0.8) but not complete.  The energy
  is the mean power over the covered time times the slot length, so a partly covered slot is
  scaled up to the full slot, and the quality column says so.
* ``unknown``: coverage is below ``min_coverage``.  The energy is NaN (null in the Parquet
  file), never zero.

Other signals
-------------
*Temperature* is the time-weighted mean over the covered time (NaN below ``min_coverage``).  A
*control state* has the value at slot start (``<name>_at_start``: the sample whose covered span
contains the slot start, NaN when that instant is in a gap) and the time-weighted mean over the
slot (``<name>_mean``, which is the fraction of the slot the boolean was on).  A *counter* such
as the washer plug's energy-today only has the as-of value at slot start.

Climate signal set
------------------
:data:`CLIMATE_SIGNALS` (``build-features --signal-set climate``) is a second, separate feature
set with its own id: rooms (current and target temperature of each thermostat), the heat pump's
water side, compressor frequency and starts, pump flow, heat/hot-water/cooling electric and
thermal energy, the aerothermal branch and the control states.  Two more kinds join the rules
above.  A *gauge* (frequency, flow) is the time-weighted mean like a temperature.  A
*counter delta* (compressor starts, the energy totals) is the counter's increase per slot: each
span's increase is spread over the span in proportion to time and then follows the power rule
(gaps are not covered, ``partial`` is scaled to the full slot, ``unknown`` is NaN).  A drop to
under half of the previous value is a counter reset and the span adds the value read after it;
a smaller drop is a rollback and adds nothing (see :func:`counter_rates`).

Source switch
-------------
A signal whose quantity moved to another meter (a plant change in the regime log) names its later
sources in :attr:`Signal.successors`: the first entity is read before the first switch instant and
each successor from its instant on, so a renamed or reassigned entity never feeds the alias outside
its window.  The pieces join into one series under the same rules: a span that crosses a switch
holds the last reading of the earlier source until the first one of the next, unless it is a gap.

Leakage note: every feature of slot ``k`` uses samples from inside slot ``k``.  A forecaster may
use only ``*_at_start`` of the slot it forecasts, or any feature of an earlier slot.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from battery_optimizer.build import (
    BuildError,
    _entry_part_name,
    _iter_prometheus_manifests,
    _overlaps,
    _parse_window_bound,
    _part_window,
    _resolve_referenced_path,
    _validate_manifest_part,
)
from battery_optimizer.extract_prom import CLIMATE_ROOMS, ENTITY_SPECS, iso_z, parse_utc
from battery_optimizer.manifest import code_version
from battery_optimizer.paths import resolve_data_dir, write_part_atomic

log = logging.getLogger(__name__)

FEATURE_SCHEMA_VERSION = 1
FEATURES_DIRNAME = "features"
MANIFEST_NAME = "features_manifest.json"
PART_KIND = "features"

INTERVAL_SECONDS = 900
DEFAULT_MAX_GAP_SECONDS = 300.0
DEFAULT_MIN_COVERAGE = 0.8
DEFAULT_LOOKBACK_SECONDS = 3600.0

#: Coverage at or above this counts as a fully measured slot (float rounding slack only).
FULL_COVERAGE = 1.0 - 1e-9

#: A non-zero reading that repeats unchanged for this long is listed for review.
CONSTANT_RUN_HOURS = 6.0

KIND_POWER = "power"
KIND_TEMPERATURE = "temperature"
KIND_STATE = "state"
KIND_COUNTER = "counter"
KIND_GAUGE = "gauge"
KIND_COUNTER_DELTA = "counter_delta"

QUALITY_MEASURED = "measured"
QUALITY_PARTIAL = "partial"
QUALITY_UNKNOWN = "unknown"

INTEGRATION_RULE = (
    "Sample-and-hold: each sample's value holds until the next sample; the slot value is the "
    "exact integral over the slot. A span is covered only if it is at most max_gap_seconds long; "
    "longer spans and the time after the last sample are gaps (no energy, no coverage, no "
    "interpolation). coverage is the covered fraction of the slot. measured = fully covered; "
    "partial = coverage >= min_coverage, energy = mean covered power x slot length; unknown = "
    "coverage < min_coverage, energy is NaN, never zero."
)


@dataclass(frozen=True)
class Signal:
    """One raw entity that feeds the feature table, and how it is aggregated per slot."""

    entity: str
    alias: str
    kind: str
    #: The metric to read when the entity carries more than one (a thermostat's current and
    #: target temperature); ``None`` reads the entity's only metric.
    metric: str | None = None
    #: Later sources of the same quantity, ``(since, entity)`` in time order (aware instants):
    #: :attr:`entity` is read before the first ``since``, each successor from its ``since`` until
    #: the next one.  The series keeps :attr:`source` as its key.
    successors: tuple[tuple[datetime, str], ...] = ()

    def __post_init__(self) -> None:
        instants = [since for since, _ in self.successors]
        if any(t.tzinfo is None for t in instants) or instants != sorted(set(instants)):
            raise ValueError(f"{self.alias}: successors need strictly increasing aware instants")

    def entity_windows(self) -> tuple[tuple[str, datetime | None, datetime | None], ...]:
        """``(entity, since, until)`` of every source in time order; ``None`` is unbounded."""
        bounds: list[datetime | None] = [None, *(since for since, _ in self.successors), None]
        entities = (self.entity, *(entity for _, entity in self.successors))
        return tuple((e, bounds[i], bounds[i + 1]) for i, e in enumerate(entities))

    @property
    def source(self) -> str:
        """Key of this signal's raw series: the entity, or ``entity#metric`` when pinned."""
        return self.entity if self.metric is None else f"{self.entity}#{self.metric}"

    @property
    def extract_key(self) -> str:
        """The ``extract --entities`` token for this signal's series (e.g. ``entity#setpoint``)."""
        if self.metric is not None:
            for key, spec in ENTITY_SPECS.items():
                if spec.entity_id == self.entity and spec.metric == self.metric:
                    return key
        return self.entity

    def columns(self) -> tuple[str, ...]:
        if self.kind == KIND_POWER:
            return (f"{self.alias}_kwh", f"{self.alias}_coverage", f"{self.alias}_quality")
        if self.kind == KIND_TEMPERATURE:
            return (f"{self.alias}_mean_c", f"{self.alias}_coverage")
        if self.kind == KIND_GAUGE:
            return (f"{self.alias}_mean", f"{self.alias}_coverage")
        if self.kind == KIND_COUNTER_DELTA:
            return (f"{self.alias}_delta", f"{self.alias}_coverage", f"{self.alias}_quality")
        if self.kind == KIND_STATE:
            return (f"{self.alias}_at_start", f"{self.alias}_mean", f"{self.alias}_coverage")
        if self.kind == KIND_COUNTER:
            return (f"{self.alias}_at_start",)
        raise ValueError(f"unknown signal kind {self.kind!r}")


#: The servers moved to the battery's backup (EPS) port on 2026-11-03 (regime log).  From then on
#: the Shelly PM mini that metered them meters an unrelated workstation (its Home Assistant entity
#: was renamed about 20 minutes later), and the battery's backup-port output is the servers' load.
#: Prometheus brackets the rewiring: the PM mini's last servers reading is at 22:04:31 UTC, then a
#: scrape gap, and from 22:11:43 UTC it reads the workstation while the backup port reads the
#: servers.  Any instant inside that gap gives the same feature table (a gap is never held).
SERVERS_ON_BACKUP_PORT_SINCE = datetime(2026, 11, 3, 22, 5, tzinfo=UTC)

SERVERS_SIGNAL = Signal(
    "sensor.shellypmmini_servers_power",
    "servers",
    KIND_POWER,
    successors=((SERVERS_ON_BACKUP_PORT_SINCE, "sensor.venus_ac_offgrid_power"),),
)

SIGNALS: tuple[Signal, ...] = (
    SERVERS_SIGNAL,
    Signal("sensor.aquarea_heat_power_consumed", "hp_heat", KIND_POWER),
    Signal("sensor.aquarea_dhw_power_consumed", "hp_dhw", KIND_POWER),
    Signal("sensor.aquarea_cool_power_consumed", "hp_cool", KIND_POWER),
    Signal("sensor.aquarea_heat_power_produced", "hp_heat_out", KIND_POWER),
    Signal("sensor.aquarea_dhw_power_produced", "hp_dhw_out", KIND_POWER),
    Signal("sensor.aquarea_cool_power_produced", "hp_cool_out", KIND_POWER),
    Signal("sensor.aquarea_power_consumed", "hp_total_reported", KIND_POWER),
    Signal("sensor.smart_plug_v2_terrace_power", "washer_plug", KIND_POWER),
    Signal("sensor.aquarea_outside_ambient_temperature", "outdoor_temp", KIND_TEMPERATURE),
    Signal("input_boolean.appdaemon_climate_enable", "climate_enable", KIND_STATE),
    Signal("input_boolean.appdaemon_dhw_enable", "dhw_enable", KIND_STATE),
    Signal("input_number.appdaemon_climate_min_hours_per_day", "climate_min_hours", KIND_STATE),
    Signal("switch.aquarea_force_dhw_mode", "dhw_force", KIND_STATE),
    Signal("sensor.terrace_lavadora_energy_today", "washer_energy_today_wh", KIND_COUNTER),
)

#: Components whose electrical input adds up to the heat pump's draw.
HEAT_PUMP_INPUT_ALIASES = ("hp_heat", "hp_dhw", "hp_cool")
#: Thermal output of each input component, for the COP sanity check.
COP_PAIRS = (
    ("heat", "hp_heat", "hp_heat_out"),
    ("dhw", "hp_dhw", "hp_dhw_out"),
    ("cool", "hp_cool", "hp_cool_out"),
)


#: Rooms that are not part of the comfort objective (large internal gain); data only.
COMFORT_EXCLUDED_ROOMS = frozenset({"office_a"})


def _room_signals() -> tuple[Signal, ...]:
    out: list[Signal] = []
    for room, entity in CLIMATE_ROOMS.items():
        current, setpoint = ENTITY_SPECS[entity], ENTITY_SPECS[f"{entity}#setpoint"]
        out.append(Signal(entity, f"room_{room}_temp", KIND_TEMPERATURE, current.metric))
        out.append(Signal(entity, f"room_{room}_setpoint", KIND_TEMPERATURE, setpoint.metric))
    return tuple(out)


#: The climate feature set: rooms, heat-pump water side, compressor, thermal and electric energy
#: (heating, hot water and cooling), the aerothermal branch, the servers circuit (an internal
#: gain of office A) and the control states.  The power signals reuse the default aliases;
#: a ``*_delta`` column is a counter's increase per slot.
CLIMATE_SIGNALS: tuple[Signal, ...] = (
    *_room_signals(),
    Signal("sensor.aquarea_outside_ambient_temperature", "outdoor_temp", KIND_TEMPERATURE),
    Signal("sensor.aquarea_tank_temperature", "tank_temp", KIND_TEMPERATURE),
    Signal("sensor.aquarea_outlet_temperature", "flow_temp", KIND_TEMPERATURE),
    Signal("sensor.aquarea_inlet_temperature", "return_temp", KIND_TEMPERATURE),
    Signal("sensor.aquarea_outlet_target_temperature", "flow_target_temp", KIND_TEMPERATURE),
    Signal("sensor.aquarea_compressor_frequency", "compressor_freq", KIND_GAUGE),
    Signal("sensor.aquarea_compressor_start_stop_counter", "compressor_starts", KIND_COUNTER_DELTA),
    Signal("sensor.aquarea_pump_flow", "pump_flow", KIND_GAUGE),
    SERVERS_SIGNAL,
    Signal("sensor.aquarea_heat_power_consumed", "hp_heat", KIND_POWER),
    Signal("sensor.aquarea_dhw_power_consumed", "hp_dhw", KIND_POWER),
    Signal("sensor.aquarea_cool_power_consumed", "hp_cool", KIND_POWER),
    Signal("sensor.aquarea_heat_power_produced", "hp_heat_out", KIND_POWER),
    Signal("sensor.aquarea_dhw_power_produced", "hp_dhw_out", KIND_POWER),
    Signal("sensor.aquarea_cool_power_produced", "hp_cool_out", KIND_POWER),
    Signal("sensor.aquarea_heat_energy_consumed", "hp_heat_energy", KIND_COUNTER_DELTA),
    Signal("sensor.aquarea_dhw_energy_consumed", "hp_dhw_energy", KIND_COUNTER_DELTA),
    Signal("sensor.aquarea_cool_energy_consumed", "hp_cool_energy", KIND_COUNTER_DELTA),
    Signal("sensor.aquarea_heat_energy_produced", "hp_heat_energy_out", KIND_COUNTER_DELTA),
    Signal("sensor.aquarea_dhw_energy_produced", "hp_dhw_energy_out", KIND_COUNTER_DELTA),
    Signal("sensor.aquarea_cool_energy_produced", "hp_cool_energy_out", KIND_COUNTER_DELTA),
    Signal("sensor.shellyem_main_channel_2_power", "branch_aero", KIND_POWER),
    Signal("sensor.shellyem_main_channel_2_energy", "branch_aero_energy", KIND_COUNTER_DELTA),
    Signal("input_boolean.appdaemon_climate_enable", "climate_enable", KIND_STATE),
    Signal("input_boolean.appdaemon_dhw_enable", "dhw_enable", KIND_STATE),
    Signal("input_number.appdaemon_climate_min_hours_per_day", "climate_min_hours", KIND_STATE),
    Signal("switch.aquarea_force_dhw_mode", "dhw_force", KIND_STATE),
)

#: ``build-features --signal-set`` choices.
SIGNAL_SETS: dict[str, tuple[Signal, ...]] = {"default": SIGNALS, "climate": CLIMATE_SIGNALS}


class FeatureError(RuntimeError):
    """Expected, user-facing failure of the feature builder."""


def feature_schema(signals: tuple[Signal, ...] = SIGNALS) -> pa.Schema:
    """Arrow schema of the feature table: the slot bounds, then every signal's columns."""
    fields = [
        pa.field("interval_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("interval_end", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
    for sig in signals:
        for col in sig.columns():
            fields.append(pa.field(col, pa.string() if col.endswith("_quality") else pa.float64()))
    return pa.schema(fields)


# ---------------------------------------------------------------------------
# Pure numerics
# ---------------------------------------------------------------------------


def merge_samples(t_us: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Sort samples by time and collapse repeated timestamps (keep the first loaded).

    Several scrape eras and pods can export the same entity.  Their timestamps do not overlap
    in the live archive, but a repeated timestamp is handled rather than assumed away.

    Returns ``(t_us, values, duplicates_dropped, conflicting_duplicates)`` where a conflict is
    a repeated timestamp whose value differs from the one kept.
    """
    t = np.asarray(t_us, dtype=np.int64)
    v = np.asarray(values, dtype=np.float64)
    if t.size == 0:
        return t, v, 0, 0
    order = np.argsort(t, kind="stable")
    t, v = t[order], v[order]
    dup = np.zeros(t.size, dtype=bool)
    dup[1:] = t[1:] == t[:-1]
    differs = np.zeros(t.size, dtype=bool)
    differs[1:] = v[1:] != v[:-1]
    keep = ~dup
    return t[keep], v[keep], int(dup.sum()), int((dup & differs).sum())


def integrate_slots(
    t: np.ndarray, v: np.ndarray, edges: np.ndarray, max_gap: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample-and-hold integral of ``v`` over the slots delimited by ``edges``.

    Args:
        t: strictly increasing sample times in seconds.
        v: sample values.
        edges: ``n_slots + 1`` increasing slot edges in seconds.
        max_gap: a span longer than this is a gap (see the module docstring).

    Returns:
        ``(integral, covered_seconds, at_start)`` per slot.  ``integral`` is the integral of the
        value over the covered time (value-seconds), ``covered_seconds`` the covered time inside
        the slot, and ``at_start`` the value held at the slot start, NaN if that instant is not
        inside a covered span.
    """
    n_slots = edges.size - 1
    n = t.size
    if n < 2:
        return np.zeros(n_slots), np.zeros(n_slots), np.full(n_slots, np.nan)
    dt = np.diff(t)
    valid = dt <= max_gap
    cum_value = np.concatenate(([0.0], np.cumsum(np.where(valid, v[:-1] * dt, 0.0))))
    cum_time = np.concatenate(([0.0], np.cumsum(np.where(valid, dt, 0.0))))

    idx = np.searchsorted(t, edges, side="right") - 1
    inside = (idx >= 0) & (idx < n - 1)
    safe = np.clip(idx, 0, n - 1)
    span = np.clip(idx, 0, n - 2)
    live = inside & valid[span]
    elapsed = np.where(live, edges - t[safe], 0.0)
    base_value = np.where(idx >= 0, cum_value[safe], 0.0)
    base_time = np.where(idx >= 0, cum_time[safe], 0.0)
    f_value = base_value + np.where(live, v[safe] * elapsed, 0.0)
    f_time = base_time + elapsed

    at_start = np.where(live[:-1], v[safe[:-1]], np.nan)
    return np.diff(f_value), np.diff(f_time), at_start


#: A decrease to less than this fraction of the previous value is a restart from zero.
COUNTER_RESET_FRACTION = 0.5


def counter_rates(t: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, int, int]:
    """Per-second increase of a monotonic counter over each span between two samples.

    A decrease to less than :data:`COUNTER_RESET_FRACTION` of the previous value is a **reset**
    (restart of the device or exporter): the counter restarted from zero, so the span's increase
    is the value read after it, as in Prometheus ``increase``.  Any other decrease is a
    **rollback** (the energy counters of the live archive slip back by 0.03 to 0.6 kWh at a
    time): the span adds nothing, and treating it as a reset would add the whole counter value.
    A rollback is taken as a permanent correction, not a glitch: in the live archive each one is
    a single step down, after which the counter climbs again in its normal small steps (it needs
    8 to 449 minutes to regain the old level), so the climb is real consumption and is counted.
    The returned rates have one entry per sample (the last one is unused, like the value of the
    last sample of a power series) so they can be integrated with :func:`integrate_slots`; a
    span's integral is then exactly its increase.

    Returns ``(rates, resets, rollbacks)``.
    """
    if t.size < 2:
        return np.zeros(t.size), 0, 0
    step = np.diff(values)
    drop = step < 0
    reset = drop & (values[1:] < COUNTER_RESET_FRACTION * values[:-1])
    increase = np.where(reset, values[1:], np.where(drop, 0.0, step))
    rates = np.append(increase / np.diff(t), 0.0)
    return rates, int(reset.sum()), int((drop & ~reset).sum())


def _quality_labels(coverage: np.ndarray, min_coverage: float) -> np.ndarray:
    labels = np.full(coverage.size, QUALITY_UNKNOWN, dtype=object)
    labels[coverage >= min_coverage] = QUALITY_PARTIAL
    labels[coverage >= FULL_COVERAGE] = QUALITY_MEASURED
    return labels


def signal_columns(
    signal: Signal,
    t_us: np.ndarray,
    values: np.ndarray,
    edges_us: np.ndarray,
    *,
    max_gap_seconds: float,
    min_coverage: float,
) -> dict[str, np.ndarray]:
    """Per-slot columns of one signal from its merged samples (empty arrays = no data)."""
    n_slots = edges_us.size - 1
    origin = int(edges_us[0])
    t = (np.asarray(t_us, dtype=np.int64) - origin) / 1e6
    edges = (np.asarray(edges_us, dtype=np.int64) - origin) / 1e6
    slot_s = float(np.median(np.diff(edges))) if n_slots else float(INTERVAL_SECONDS)
    if signal.kind == KIND_COUNTER_DELTA:
        values = counter_rates(t, np.asarray(values, dtype=np.float64))[0]
    integral, covered, at_start = integrate_slots(t, values, edges, max_gap_seconds)
    coverage = np.clip(covered / slot_s, 0.0, 1.0)
    known = coverage >= min_coverage
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = np.where(known, integral / np.where(covered > 0, covered, np.nan), np.nan)

    cols = signal.columns()
    out: dict[str, np.ndarray] = {}
    if signal.kind == KIND_POWER:
        # mean power (W) over the covered time, times the slot length in hours, in kWh.
        out[cols[0]] = mean * (slot_s / 3600.0) / 1000.0
        out[cols[1]] = coverage
        out[cols[2]] = _quality_labels(coverage, min_coverage)
    elif signal.kind == KIND_COUNTER_DELTA:
        # mean increase rate over the covered time, times the slot length: the slot's increase.
        out[cols[0]] = mean * slot_s
        out[cols[1]] = coverage
        out[cols[2]] = _quality_labels(coverage, min_coverage)
    elif signal.kind in (KIND_TEMPERATURE, KIND_GAUGE):
        out[cols[0]] = mean
        out[cols[1]] = coverage
    elif signal.kind == KIND_STATE:
        out[cols[0]] = at_start
        out[cols[1]] = mean
        out[cols[2]] = coverage
    else:
        out[cols[0]] = at_start
    return out


# ---------------------------------------------------------------------------
# Raw input
# ---------------------------------------------------------------------------


@dataclass
class RawSeries:
    """The merged raw samples of one entity."""

    entity: str
    metric: str
    unit: str
    t_us: np.ndarray
    values: np.ndarray
    series_keys: int = 0
    duplicates_dropped: int = 0
    conflicting_duplicates: int = 0


def _within(table: pa.Table, since: datetime | None, until: datetime | None) -> pa.Table:
    """Rows of *table* with ``since <= ts < until`` (``None`` is unbounded)."""
    ts_type = pa.timestamp("us", tz="UTC")
    if since is not None:
        table = table.filter(pc.greater_equal(table.column("ts"), pa.scalar(since, ts_type)))
    if until is not None:
        table = table.filter(pc.less(table.column("ts"), pa.scalar(until, ts_type)))
    return table


def load_signal_samples(
    data_dir: Path,
    signals: tuple[Signal, ...],
    *,
    window_start: datetime,
    window_end: datetime,
    lookback_seconds: float = DEFAULT_LOOKBACK_SECONDS,
) -> tuple[dict[str, RawSeries], list[dict[str, Any]], list[str]]:
    """Read the raw samples of *signals* through the successful raw manifests.

    Only manifests that declare at least one wanted entity are considered, and only parts whose
    chunk overlaps ``[window_start - lookback, window_end)`` are read.  Each part is verified
    (SHA-256, row count, schema) from one read, exactly as the canonical builder does.

    Returns ``(series_by_source, part_hashes, extraction_ids)``, keyed by :attr:`Signal.source`
    (the entity id unless the signal pins a metric).  A series with no samples is absent from the
    mapping.
    """
    import datetime as _dt

    wanted = {entity for s in signals for entity, _since, _until in s.entity_windows()}
    manifests = [
        m
        for m in _iter_prometheus_manifests(data_dir)
        if wanted.intersection(n for n in (m.get("entities") or []) if isinstance(n, str))
    ]
    if not manifests:
        raise FeatureError(
            f"no raw parts: no successful prometheus manifest declares any of the feature "
            f"entities in {data_dir / 'manifests'}; run `extract --entities ...` first"
        )
    resolved_root = data_dir.resolve()
    lo = (window_start - _dt.timedelta(seconds=lookback_seconds)).astimezone(UTC)
    hi = window_end.astimezone(UTC)
    ts_type = pa.timestamp("us", tz="UTC")
    wanted_arr = pa.array(sorted(wanted), pa.string())

    # One raw series per source: an entity, or an (entity, metric) pair for a pinned metric.
    sources: dict[str, Signal] = {s.source: s for s in signals}
    ts_parts: dict[str, list[np.ndarray]] = {k: [] for k in sources}
    val_parts: dict[str, list[np.ndarray]] = {k: [] for k in sources}
    metrics: dict[str, set[str]] = {k: set() for k in sources}
    units: dict[str, set[str]] = {k: set() for k in sources}
    keys: dict[str, set[str]] = {k: set() for k in sources}
    part_hashes: list[dict[str, Any]] = []
    extraction_ids: list[str] = []
    seen_parts: set[tuple[str, str]] = set()

    for record in manifests:
        m_start = _parse_window_bound(record.get("source_window_start"))
        m_end = _parse_window_bound(record.get("source_window_end"))
        read_any = False
        for entry in record.get("part_files", []):
            if not isinstance(entry, dict):
                continue
            cs, ce = _part_window(entry, m_start, m_end)
            if not _overlaps(cs, ce, lo, hi):
                continue
            name = _entry_part_name(entry)
            sha = str(entry.get("sha256", ""))
            if (name, sha) in seen_parts:
                continue
            seen_parts.add((name, sha))
            path = _resolve_referenced_path(data_dir, resolved_root, entry)
            try:
                table = _validate_manifest_part(path=path, entry=entry)
            except BuildError as exc:
                raise FeatureError(str(exc)) from exc
            mask = pc.and_(
                pc.and_(
                    pc.greater_equal(table.column("ts"), pa.scalar(lo, ts_type)),
                    pc.less(table.column("ts"), pa.scalar(hi, ts_type)),
                ),
                pc.is_in(table.column("entity"), value_set=wanted_arr),
            )
            selected = table.filter(mask)
            part_hashes.append({"part": name, "sha256": sha, "rows_selected": selected.num_rows})
            read_any = True
            if not selected.num_rows:
                continue
            ent = selected.column("entity")
            for entity in pc.unique(ent).to_pylist():
                of_entity = selected.filter(pc.equal(ent, entity))
                met = of_entity.column("metric")
                for metric in pc.unique(met).to_pylist():
                    sub = of_entity.filter(pc.equal(met, metric))
                    for key, sig in sources.items():
                        if sig.metric not in (None, metric):
                            continue
                        for source, since, until in sig.entity_windows():
                            if source != entity:
                                continue
                            rows = _within(sub, since, until)
                            if not rows.num_rows:
                                continue
                            ts_parts[key].append(rows.column("ts").cast(pa.int64()).to_numpy())
                            val_parts[key].append(rows.column("value").to_numpy())
                            metrics[key].add(metric)
                            units[key].update(pc.unique(rows.column("unit")).to_pylist())
                            keys[key].update(pc.unique(rows.column("series_key")).to_pylist())
        if read_any:
            extraction_ids.append(str(record.get("extraction_id")))

    series: dict[str, RawSeries] = {}
    for key in sorted(sources):
        if not ts_parts[key]:
            continue
        if len(metrics[key]) != 1 or len(units[key]) != 1:
            raise FeatureError(
                f"{sources[key].entity}: expected one metric and one unit, found metrics="
                f"{sorted(metrics[key])} units={sorted(units[key])}"
            )
        t, v, dropped, conflicts = merge_samples(
            np.concatenate(ts_parts[key]), np.concatenate(val_parts[key])
        )
        series[key] = RawSeries(
            entity=sources[key].entity,
            metric=next(iter(metrics[key])),
            unit=next(iter(units[key])),
            t_us=t,
            values=v,
            series_keys=len(keys[key]),
            duplicates_dropped=dropped,
            conflicting_duplicates=conflicts,
        )
    if not series:
        raise FeatureError(
            f"no samples of any feature entity in [{iso_z(lo)}, {iso_z(hi)}); "
            "run `extract --entities ...` first"
        )
    part_hashes.sort(key=lambda p: p["part"])
    return series, part_hashes, sorted(set(extraction_ids))


# ---------------------------------------------------------------------------
# Table assembly
# ---------------------------------------------------------------------------


def slot_edges_us(start: datetime, end: datetime) -> np.ndarray:
    """UTC slot edges in microseconds for ``[start, end)`` on the 15-minute grid."""
    lo = int(start.timestamp()) * 1_000_000
    hi = int(end.timestamp()) * 1_000_000
    step = INTERVAL_SECONDS * 1_000_000
    if lo % step or hi % step:
        raise FeatureError("start and end must be multiples of 15 minutes in UTC")
    if hi <= lo:
        raise FeatureError("start must be before end")
    return np.arange(lo, hi + step, step, dtype=np.int64)


def _nullable(arr: np.ndarray, dtype: pa.DataType) -> pa.Array:
    if dtype == pa.string():
        return pa.array(list(arr), pa.string())
    a = np.asarray(arr, dtype=np.float64)
    return pa.array(a, pa.float64(), mask=np.isnan(a))


def assemble_table(
    series: dict[str, RawSeries],
    edges_us: np.ndarray,
    *,
    signals: tuple[Signal, ...] = SIGNALS,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> pa.Table:
    """The feature table: one row per slot, unknown values null (never zero)."""
    schema = feature_schema(signals)
    n_slots = edges_us.size - 1
    columns: dict[str, pa.Array] = {
        "interval_start": pa.array(edges_us[:-1], pa.timestamp("us", tz="UTC")),
        "interval_end": pa.array(edges_us[1:], pa.timestamp("us", tz="UTC")),
    }
    empty_t = np.zeros(0, dtype=np.int64)
    empty_v = np.zeros(0, dtype=np.float64)
    for sig in signals:
        raw = series.get(sig.source)
        data = signal_columns(
            sig,
            raw.t_us if raw else empty_t,
            raw.values if raw else empty_v,
            edges_us,
            max_gap_seconds=max_gap_seconds,
            min_coverage=min_coverage,
        )
        for col in sig.columns():
            columns[col] = _nullable(data[col], schema.field(col).type)
    table = pa.table(columns, schema=schema)
    assert table.num_rows == n_slots
    return table


def column_array(table: pa.Table, name: str) -> np.ndarray:
    """A float column as numpy with NaN for null."""
    return table.column(name).to_numpy(zero_copy_only=False).astype(np.float64)


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------


def _month_labels(micros: np.ndarray) -> np.ndarray:
    return np.datetime_as_string(micros.astype("datetime64[us]").astype("datetime64[M]"))


def constant_runs(
    t_us: np.ndarray, values: np.ndarray, *, min_hours: float, limit: int = 10
) -> dict[str, Any]:
    """Runs of one identical non-zero value that last at least *min_hours*.

    A live power reading moves; a long constant run is a stuck sensor or a stale state that the
    exporter kept repeating.  Heat-pump power is quantised (400 W can repeat for hours), so the
    threshold is a screen for review, not a verdict: the longest runs are listed.
    """
    if values.size == 0:
        return {"min_hours": min_hours, "count": 0, "longest": []}
    starts = np.flatnonzero(np.concatenate(([True], values[1:] != values[:-1])))
    ends = np.concatenate((starts[1:] - 1, [values.size - 1]))
    hours = (t_us[ends] - t_us[starts]) / 3.6e9
    keep = np.flatnonzero((values[starts] != 0) & (hours >= min_hours))
    order = keep[np.argsort(-hours[keep], kind="stable")][:limit]
    return {
        "min_hours": min_hours,
        "count": int(keep.size),
        "longest": [
            {
                "start": iso_z(datetime.fromtimestamp(t_us[starts[k]] / 1e6, UTC)),
                "hours": float(hours[k]),
                "value": float(values[starts[k]]),
            }
            for k in order
        ],
    }


def series_quality(
    raw: RawSeries,
    table: pa.Table,
    signal: Signal,
    *,
    max_gap_seconds: float,
    min_coverage: float,
) -> dict[str, Any]:
    """Units, cadence, gaps per month and slot coverage of one series."""
    t = raw.t_us
    dt = np.diff(t) / 1e6 if t.size > 1 else np.zeros(0)
    gap_mask = dt > max_gap_seconds
    out: dict[str, Any] = {
        "entity": raw.entity,
        "alias": signal.alias,
        "kind": signal.kind,
        "metric": raw.metric,
        "unit": raw.unit,
        "samples": int(t.size),
        "series_keys": raw.series_keys,
        "duplicates_dropped": raw.duplicates_dropped,
        "conflicting_duplicates": raw.conflicting_duplicates,
        "first_sample": iso_z(datetime.fromtimestamp(t[0] / 1e6, UTC)) if t.size else None,
        "last_sample": iso_z(datetime.fromtimestamp(t[-1] / 1e6, UTC)) if t.size else None,
        "value_min": float(raw.values.min()) if t.size else None,
        "value_max": float(raw.values.max()) if t.size else None,
        "negative_samples": int((raw.values < 0).sum()),
    }
    if dt.size:
        out["cadence_seconds"] = {
            "median": float(np.median(dt)),
            "p95": float(np.percentile(dt, 95)),
            "p99": float(np.percentile(dt, 99)),
            "max": float(dt.max()),
        }
    if signal.kind in (KIND_POWER, KIND_TEMPERATURE):
        out["constant_runs"] = constant_runs(t, raw.values, min_hours=CONSTANT_RUN_HOURS)
    if signal.kind == KIND_COUNTER_DELTA:
        _rates, out["counter_resets"], out["counter_rollbacks"] = counter_rates(t, raw.values)
    out["gap_spans"] = int(gap_mask.sum())
    out["gap_seconds"] = float(dt[gap_mask].sum()) if dt.size else 0.0

    start_us = column_array_ts(table)
    cov_name = [c for c in signal.columns() if c.endswith("_coverage")]
    months = _month_labels(start_us)
    per_month: dict[str, dict[str, Any]] = {}
    gap_months = _month_labels(t[:-1][gap_mask]) if gap_mask.any() else np.array([], dtype=str)
    for month in sorted(set(months)):
        sel = months == month
        entry: dict[str, Any] = {
            "slots": int(sel.sum()),
            "gap_spans": int((gap_months == month).sum()),
        }
        if gap_mask.any():
            gaps_here = dt[gap_mask][gap_months == month]
            entry["gap_seconds"] = float(gaps_here.sum())
        else:
            entry["gap_seconds"] = 0.0
        if cov_name:
            cov = column_array(table, cov_name[0])[sel]
            entry["slots_below_min_coverage"] = int((cov < min_coverage).sum())
            entry["mean_coverage"] = float(cov.mean())
        per_month[str(month)] = entry
    out["per_month"] = per_month
    if cov_name:
        cov = column_array(table, cov_name[0])
        out["slots_measured"] = int((cov >= FULL_COVERAGE).sum())
        out["slots_partial"] = int(((cov >= min_coverage) & (cov < FULL_COVERAGE)).sum())
        out["slots_unknown"] = int((cov < min_coverage).sum())
    return out


def column_array_ts(table: pa.Table) -> np.ndarray:
    """``interval_start`` of a table as int64 microseconds."""
    return table.column("interval_start").cast(pa.int64()).to_numpy()


def heat_pump_input(table: pa.Table) -> np.ndarray:
    """Heat-pump electrical input per slot: heat + DHW + cooling, NaN unless all are known."""
    total = np.zeros(table.num_rows)
    for alias in HEAT_PUMP_INPUT_ALIASES:
        total = total + column_array(table, f"{alias}_kwh")
    return total


def cop_check(table: pa.Table, *, min_input_kwh: float = 0.01) -> dict[str, Any]:
    """``*_consumed`` is electrical input, not thermal output: produced / consumed is a COP > 1.

    Heat-pump thermal output exceeds electrical input by the COP, so the ratio of produced to
    consumed energy must sit well above 1.  A ratio near 1 or below would say the "consumed"
    series is the thermal one.
    """
    out: dict[str, Any] = {}
    for label, consumed_alias, produced_alias in COP_PAIRS:
        cin = column_array(table, f"{consumed_alias}_kwh")
        cout = column_array(table, f"{produced_alias}_kwh")
        both = np.isfinite(cin) & np.isfinite(cout)
        active = both & (cin >= min_input_kwh)
        entry: dict[str, Any] = {
            "slots_compared": int(both.sum()),
            "slots_active": int(active.sum()),
            "consumed_kwh": float(cin[both].sum()),
            "produced_kwh": float(cout[both].sum()),
        }
        if entry["consumed_kwh"] > 0:
            entry["cop_ratio_of_sums"] = entry["produced_kwh"] / entry["consumed_kwh"]
        if active.any():
            cop = cout[active] / cin[active]
            entry["slot_cop_median"] = float(np.median(cop))
            entry["slot_cop_p10"] = float(np.percentile(cop, 10))
            entry["slot_cop_p90"] = float(np.percentile(cop, 90))
            entry["share_of_active_slots_with_cop_below_1"] = float((cop < 1.0).mean())
        out[label] = entry
    if "hp_total_reported_kwh" not in table.column_names:
        return out  # the climate set has no reported-total sensor
    reported = column_array(table, "hp_total_reported_kwh")
    parts = heat_pump_input(table)
    both = np.isfinite(reported) & np.isfinite(parts)
    cmp: dict[str, Any] = {"slots_compared": int(both.sum())}
    if both.any() and parts[both].sum() > 0:
        cmp["reported_total_kwh"] = float(reported[both].sum())
        cmp["sum_of_components_kwh"] = float(parts[both].sum())
        cmp["ratio_of_sums"] = float(reported[both].sum() / parts[both].sum())
        denom = np.maximum(parts[both], 1e-9)
        close = np.abs(reported[both] - parts[both]) <= np.maximum(0.005, 0.05 * denom)
        cmp["share_within_5pct_or_5wh"] = float(close.mean())
    out["reported_total_vs_components"] = cmp
    return out


def main_known_mask(dataset_table: pa.Table) -> np.ndarray:
    """Slots whose main-meter energy is a measured, finite, non-negative value."""
    gross = dataset_table.column("gross_kwh").to_numpy(zero_copy_only=False).astype(np.float64)
    eq = np.array(dataset_table.column("energy_quality").to_pylist(), dtype=object)
    aq = np.array(dataset_table.column("allocation_quality").to_pylist(), dtype=object)
    ok = np.isfinite(gross) & (gross >= 0)
    ok &= eq == "measured"
    ok &= np.isin(aq, ["exact", "estimated_boundary"])
    return ok


def align_to_dataset(
    table: pa.Table, dataset_table: pa.Table
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Row indices of the feature table and the dataset for the slots both have."""
    f_ts = column_array_ts(table)
    d_ts = dataset_table.column("interval_start").cast(pa.int64()).to_numpy()
    _common, fi, di = np.intersect1d(f_ts, d_ts, return_indices=True)
    return _common, fi, di


def main_meter_check(
    table: pa.Table,
    dataset_table: pa.Table,
    *,
    tolerances_kwh: tuple[float, ...] = (0.0, 0.005, 0.01, 0.02, 0.05),
    primary_tolerance_kwh: float = 0.02,
) -> dict[str, Any]:
    """``servers + heat_pump <= main`` per slot, with the size of every violation.

    Uses the canonical dataset's gross load as ``main`` and compares only slots where the main
    meter, the servers and all three heat-pump inputs are known.  A violation is
    ``servers + heat_pump - main > tolerance`` (kWh per 15-minute slot, 0.02 kWh is 80 W).
    """
    common, fi, di = align_to_dataset(table, dataset_table)
    main = dataset_table.column("gross_kwh").to_numpy(zero_copy_only=False).astype(np.float64)[di]
    main_ok = main_known_mask(dataset_table)[di]
    servers = column_array(table, "servers_kwh")[fi]
    hp = heat_pump_input(table)[fi]
    known = main_ok & np.isfinite(servers) & np.isfinite(hp)
    excess = (servers + hp - main)[known]
    out: dict[str, Any] = {
        "slots_in_common_grid": int(common.size),
        "slots_compared": int(known.sum()),
        "primary_tolerance_kwh": primary_tolerance_kwh,
        "violations_by_tolerance": {},
    }
    n = max(int(known.sum()), 1)
    for tol in tolerances_kwh:
        bad = excess > tol
        entry: dict[str, Any] = {
            "count": int(bad.sum()),
            "share": float(bad.sum() / n),
        }
        if bad.any():
            entry["excess_kwh_max"] = float(excess[bad].max())
            entry["excess_kwh_mean"] = float(excess[bad].mean())
            entry["excess_kwh_p99"] = float(np.percentile(excess[bad], 99))
            entry["excess_kwh_total"] = float(excess[bad].sum())
        out["violations_by_tolerance"][f"{tol:g}"] = entry
    starts = common[known]
    months = _month_labels(starts)
    bad = excess > primary_tolerance_kwh
    out["violations_by_month_at_primary"] = {
        str(m): {"compared": int((months == m).sum()), "violations": int(bad[months == m].sum())}
        for m in sorted(set(months))
    }
    if known.any():
        ratio_den = main[known]
        pos = ratio_den > 0.01
        if pos.any():
            ratio = (servers[known] + hp[known])[pos] / ratio_den[pos]
            out["sub_load_to_main_ratio"] = {
                "median": float(np.median(ratio)),
                "p99": float(np.percentile(ratio, 99)),
                "max": float(ratio.max()),
            }
    srv_known = main_ok & np.isfinite(servers)
    hp_known = main_ok & np.isfinite(hp)
    out["servers_alone_violations_at_primary"] = int(
        ((servers - main)[srv_known] > primary_tolerance_kwh).sum()
    )
    out["heat_pump_alone_violations_at_primary"] = int(
        ((hp - main)[hp_known] > primary_tolerance_kwh).sum()
    )
    return out


# ---------------------------------------------------------------------------
# Identity, build and load
# ---------------------------------------------------------------------------


def features_root(data_dir: Path) -> Path:
    """``<data_dir>/analysis/v1/features`` (a sibling of the canonical dataset directories)."""
    return data_dir / "analysis" / "v1" / FEATURES_DIRNAME


def compute_feature_set_id(
    *,
    part_hashes: list[dict[str, Any]],
    code_ver: str,
    window_start: datetime,
    window_end: datetime,
    params: dict[str, Any],
    signals: tuple[Signal, ...],
) -> str:
    payload = json.dumps(
        {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "code_version": code_ver,
            "window_start": iso_z(window_start),
            "window_end": iso_z(window_end),
            "params": params,
            # A pinned metric or successors extend the entry; a signal without them keeps the
            # entry it had before either existed.
            "signals": [
                [
                    s.entity,
                    s.alias,
                    s.kind,
                    *([s.metric] if s.metric else []),
                    *([[[iso_z(t), e] for t, e in s.successors]] if s.successors else []),
                ]
                for s in signals
            ],
            "raw_parts": [[p["part"], p["sha256"]] for p in part_hashes],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def json_bytes(obj: Any) -> bytes:
    def _clean(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {k: _clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_clean(v) for v in value]
        return value

    return (json.dumps(_clean(obj), indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


def build_features(
    *,
    data_dir: str | None,
    start: str,
    end: str,
    dataset: str | Path | None = None,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    signals: tuple[Signal, ...] = SIGNALS,
) -> Path:
    """Build the feature table for ``[start, end)`` and return its directory.

    Raises:
        FeatureError: bad window or parameters, no raw input, or a corrupt raw part.
        FileExistsError: the directory already holds different bytes for the same id.
    """
    if max_gap_seconds <= 0:
        raise FeatureError("max_gap_seconds must be > 0")
    if not 0.0 < min_coverage <= 1.0:
        raise FeatureError("min_coverage must be in (0, 1]")
    needed = {"servers_kwh", *(f"{alias}_kwh" for alias in HEAT_PUMP_INPUT_ALIASES)}
    if dataset is not None and not needed <= set(feature_schema(signals).names):
        raise FeatureError(
            "--dataset checks servers + heat pump against the main meter and needs the servers "
            "and heat-pump power signals in the signal set"
        )
    root = resolve_data_dir(data_dir)
    t_start = parse_utc(start, name="start")
    t_end = parse_utc(end, name="end")
    edges = slot_edges_us(t_start, t_end)

    series, part_hashes, extraction_ids = load_signal_samples(
        root, signals, window_start=t_start, window_end=t_end
    )
    params = {
        "interval_seconds": INTERVAL_SECONDS,
        "max_gap_seconds": max_gap_seconds,
        "min_coverage": min_coverage,
        "lookback_seconds": DEFAULT_LOOKBACK_SECONDS,
    }
    ver = code_version()
    feature_set_id = compute_feature_set_id(
        part_hashes=part_hashes,
        code_ver=ver,
        window_start=t_start,
        window_end=t_end,
        params=params,
        signals=signals,
    )
    table = assemble_table(
        series, edges, signals=signals, max_gap_seconds=max_gap_seconds, min_coverage=min_coverage
    )
    payload = _parquet_bytes(table)
    sha = hashlib.sha256(payload).hexdigest()

    quality: dict[str, Any] = {
        "series": {
            sig.alias: series_quality(
                series[sig.source],
                table,
                sig,
                max_gap_seconds=max_gap_seconds,
                min_coverage=min_coverage,
            )
            for sig in signals
            if sig.source in series
        },
        "entities_without_samples": [s.extract_key for s in signals if s.source not in series],
        "cop_check": cop_check(table),
    }

    directory = features_root(root) / feature_set_id
    part_name = (
        f"{PART_KIND}-{t_start.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{t_end.strftime('%Y%m%dT%H%M%SZ')}-{sha[:8]}.parquet"
    )
    manifest: dict[str, Any] = {
        "feature_set_id": feature_set_id,
        "schema_version": FEATURE_SCHEMA_VERSION,
        "code_version": ver,
        "window": {"start": iso_z(t_start), "end": iso_z(t_end)},
        "params": params,
        "integration_rule": INTEGRATION_RULE,
        "signals": [
            {
                "entity": s.entity,
                "alias": s.alias,
                "kind": s.kind,
                "columns": list(s.columns()),
                "metric": series[s.source].metric if s.source in series else None,
                "unit": series[s.source].unit if s.source in series else None,
                **(
                    {"successors": [{"since": iso_z(t), "entity": e} for t, e in s.successors]}
                    if s.successors
                    else {}
                ),
            }
            for s in signals
        ],
        "inputs": {"raw_extraction_ids": extraction_ids, "raw_parts": part_hashes},
        "part_files": [
            {"path": f"parts/{part_name}", "name": part_name, "sha256": sha, "rows": table.num_rows}
        ],
        "quality": quality,
    }
    if signals == CLIMATE_SIGNALS:
        manifest["climate"] = {
            "rooms": {
                room: {"entity": entity, "comfort": room not in COMFORT_EXCLUDED_ROOMS}
                for room, entity in CLIMATE_ROOMS.items()
            }
        }
    write_part_atomic(directory / "parts" / part_name, payload)
    write_part_atomic(directory / MANIFEST_NAME, json_bytes(manifest))
    log.info("wrote feature set %s (%d slots) to %s", feature_set_id, table.num_rows, directory)

    if dataset is not None:
        from battery_optimizer.benchmark import load_dataset

        loaded = load_dataset(dataset, data_dir=root)
        check = main_meter_check(table, loaded.table)
        check["dataset_id"] = loaded.dataset_id
        write_part_atomic(
            directory / "quality" / f"main_meter-{loaded.dataset_id[:12]}.json", json_bytes(check)
        )
    return directory


def load_features(directory: str | Path) -> tuple[pa.Table, dict[str, Any]]:
    """Load a feature table through its manifest, verifying the part's SHA-256 and row count."""
    base = Path(directory).expanduser()
    manifest_path = base / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FeatureError(f"no {MANIFEST_NAME} in {base}")
    manifest = json.loads(manifest_path.read_text())
    tables: list[pa.Table] = []
    for entry in manifest.get("part_files", []):
        path = base / str(entry["path"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise FeatureError(f"{path.name}: sha256 differs from the manifest")
        part = pq.read_table(pa.BufferReader(pa.py_buffer(data)))
        if part.num_rows != int(entry["rows"]):
            raise FeatureError(f"{path.name}: row count differs from the manifest")
        tables.append(part)
    if not tables:
        raise FeatureError(f"{manifest_path}: no part files")
    return pa.concat_tables(tables).sort_by("interval_start"), manifest
