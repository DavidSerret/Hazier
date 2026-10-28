"""The forecast.json contract between the pipeline and the web.

web/src/data/types.ts mirrors this file field by field. Change both together and bump
SCHEMA_VERSION when the shape changes.

Conventions:
- All timestamps are ISO 8601 strings in UTC (for example "2026-09-14T06:00:00Z").
  The web converts them to Malaysia time (MYT, UTC+8) for display.
- Coordinates are (lon, lat) pairs in degrees, the order MapLibre and deck.gl expect.
- "hours" and "hour" are offsets from generated_at, in hours. generated_at is t0, the start of
  the forecast window (the hour the run was made in).

validate_forecast() checks a Forecast against the types below and a few invariants the web
relies on; the pipeline runs it before writing forecast.json.
"""

from __future__ import annotations

import base64
import json
import math
import types
import typing
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime
from typing import Literal

# 2: smoke_model.wind_source, wind_run and wind_grid_deg; pm25_grid.source.
# 3: hourly CAMS points with hourly_category; zone smoke_index; trajectory release_hour;
#    smoke_grid; pm25_grid holds hourly values.
SCHEMA_VERSION = 3

# Malaysian API categories, used for CAMS PM2.5 concentrations: the air quality shown to users.
Pm25Category = Literal["good", "moderate", "unhealthy", "very_unhealthy", "hazardous"]

# CAMS over the next 24 h compared with the last 24 h, for the zone card.
Trend = Literal["improving", "stable", "worsening"]

SourceName = Literal["firms", "winds", "cams", "apims"]
SourceState = Literal["ok", "degraded", "failed"]

# Whether Hazier expects smoke from the detected fires (its smoke reaches the arrival threshold)
# and whether CAMS expects elevated PM2.5, over the window. For verification, not for display.
AgreementFlag = Literal["both_elevated", "hazier_only", "cams_only", "both_low"]


@dataclass
class Hotspot:
    id: int
    lat: float
    lon: float
    frp_mw: float  # fire radiative power, drives glow size
    confidence: str  # VIIRS confidence class: "low", "nominal" or "high"
    acquired_at: str  # UTC
    satellite: str  # "S-NPP", "NOAA-20" or "NOAA-21"
    region: str = ""  # source region label, for example "Riau"; filled by the attribution step


@dataclass
class SourceCluster:
    """Hotspots binned into one grid cell: the unit smoke is released from."""

    id: int  # rank by total FRP, 0 is the strongest
    lat: float  # FRP-weighted centroid of the member hotspots
    lon: float
    frp_mw: float  # summed FRP of the member detections over the last 48 h (relative index)
    hotspot_count: int
    latest_at: str  # UTC of the most recent detection
    region: str = ""  # source region label; filled by the attribution step
    # UTC of the earliest detection in the 48 h lookback: smoke is released from then on.
    # Empty when unknown, and then the cluster only releases from t0.
    first_at: str = ""


@dataclass
class Trajectory:
    id: int
    cluster_id: int
    weight: float  # smoke carried by the release this line follows, in emission units
    path: list[tuple[float, float]]  # (lon, lat) per step, from release to the end
    hours: list[float]  # hours since generated_at, same length as path; starts at the release
    source_region: str = ""  # region of the cluster; filled by the attribution step
    # Hour of the release (negative before t0, when the path starts at 0). With weight, gives
    # the smoke left after removal: weight * exp(-(hour - release_hour) / lifetime).
    release_hour: float = 0.0


@dataclass
class SourceShare:
    region: str
    share: float  # 0 to 1 in steps of 0.001; the shares of a zone sum to exactly 1


@dataclass
class Pm25Point:
    time: str  # UTC
    value_ugm3: float  # CAMS PM2.5, hourly value at the zone centre
    # Mean over the trailing Pm25Baseline.averaging_hours hours (the hour and those before it),
    # the value the category is based on. Equal to value_ugm3 when the averaging is 1 hour.
    basis_ugm3: float
    category: Pm25Category  # Malaysian API category of basis_ugm3 (the official-style value)
    # The same category limits applied to the hourly value: the main number shown, an
    # indication only, since the official API is defined on the 24 h mean.
    hourly_category: Pm25Category = "good"


@dataclass
class ZoneForecast:
    """One zone: air quality from CAMS; smoke timing and origin from Hazier.

    The level shown to users is the CAMS category (cams_now, cams_peak). Hazier does not
    publish a level of its own: its smoke index is too rough for "how much PM2.5" and is only
    used internally for the arrival time and the attribution below.
    """

    id: str
    name: str
    lat: float
    lon: float
    radius_km: float
    # Hazier: when smoke from the fires detected in the last 48 h reaches the zone. 0 means it
    # is already there at t0; null means not within the horizon. The range is the time the
    # smoke takes to build up around the arrival threshold (see model/smoke.py); earliest and latest
    # are null with the ETA, and latest alone is null for "possibly after 72 h".
    smoke_eta_hours: float | None
    smoke_eta_earliest_hours: float | None
    smoke_eta_latest_hours: float | None
    smoke_sources: list[SourceShare]  # where the smoke reaching the zone comes from, largest first
    # CAMS, filled by the CAMS step; empty / null when CAMS has no data for the zone.
    cams_now: Pm25Point | None = None  # at t0: the level to show on the card
    cams_peak: Pm25Point | None = None  # the hour of the highest category basis in the window
    cams_trend: Trend | None = None  # next 24 h against the last 24 h
    cams_pm25: list[Pm25Point] = field(default_factory=list)  # every few hours, see Pm25Baseline
    agreement: AgreementFlag | None = None  # null when there is nothing to compare with
    # Hazier's smoke index over the zone at each forecast hour 0 .. 72 (internal units, 0.1
    # steps). At or above smoke_model.arrival_threshold the smoke is over the zone at 850 hPa
    # ("smoke aloft": it may not reach the ground).
    smoke_index: list[float] = field(default_factory=list)


@dataclass
class SourceStatus:
    name: SourceName
    # ok: all data arrived. degraded: usable but incomplete (the web shows a warning).
    # failed: no usable data, the last valid forecast is served instead.
    state: SourceState
    last_ok: str | None  # UTC of the last successful fetch, null if never
    detail: str | None = None  # short reason shown in the UI for degraded and failed

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    @property
    def usable(self) -> bool:
        return self.state != "failed"


@dataclass
class Attribution:
    """Credit that must be shown wherever the data is shown."""

    name: str
    text: str
    url: str


@dataclass
class Pm25Baseline:
    """How to read cams_pm25: where it comes from and how the categories were derived."""

    source: str  # for example "CAMS global via Open-Meteo"
    resolution_km: float  # about 45: one value stands for a cell much larger than a zone
    step_hours: int  # spacing of the points in cams_pm25
    averaging_hours: int  # trailing mean behind each category
    averaging_verified: bool  # whether that averaging period is confirmed as the DOE's
    # False while the Malaysian API breakpoints are not checked against the official DOE
    # document: the web must then label the categories as provisional.
    categories_verified: bool
    category_upper_bounds: dict[str, float | None]  # ug/m3 upper bound per category, null = open
    # Agreement flag rule: CAMS is "elevated" from this category on (Hazier: smoke arrives).
    cams_elevated_category: Pm25Category


@dataclass
class Pm25Grid:
    """CAMS PM2.5 on a regular grid, for the map colour layer, quantised to stay small.

    values holds one integer per (frame, lat, lon), row-major: frame first, then latitude
    (south to north), then longitude (west to east). The concentration is value * quantum_ugm3;
    null marks a missing value. Each value is the trailing averaging_hours mean; averaging_hours
    is 1 since schema 3: the map shows the hourly value, like the main number of the zone cards.
    """

    lats: list[float]  # ascending
    lons: list[float]  # ascending
    hours: list[float]  # hours since generated_at of each frame
    averaging_hours: int
    quantum_ugm3: float
    values: list[int | None]
    # Where the grid comes from, for example "CAMS global via the Copernicus Atmosphere Data
    # Store (0.4 degrees)", or the coarser Open-Meteo fallback.
    source: str = ""


@dataclass
class SmokeGrid:
    """Hazier's smoke per forecast hour on a coarse grid, for the map's smoke layer.

    data is base64 of one byte per (hour, lat, lon), row-major: hour first, then latitude
    (south to north), then longitude (west to east). A byte q stands for a smoke index of
    min_index * 10 ** ((q - 1) / steps_per_decade); 0 means below min_index. The index is in the
    same units as a zone's smoke index (smoke per zone area), so it can be compared with
    smoke_model.arrival_threshold. It is smoke at 850 hPa, not a ground concentration.
    """

    lats: list[float]  # cell centres, ascending
    lons: list[float]  # cell centres, ascending
    hours: list[float]  # hours since generated_at of each frame
    cell_deg: float
    min_index: float
    steps_per_decade: int
    data: str


@dataclass
class SmokeModel:
    """How Hazier's smoke timing and attribution were made. Not an air quality measure."""

    wind_model: str  # for example "ecmwf_ifs025"
    level_hpa: int
    lookback_hours: int  # fires detected in this window are sources
    past_hours: int  # smoke emitted up to this long before t0 is carried to t0
    removal_lifetime_hours: float  # first-order removal, weights decay as exp(-age / lifetime)
    # Smoke index (internal units) at which smoke counts as arrived. Provisional.
    arrival_threshold: float
    wind_source: str = "ecmwf_open_data"  # "ecmwf_open_data", or "open_meteo" (the fallback)
    wind_run: str | None = None  # UTC start of the model run of the forecast part, if known
    wind_grid_deg: float = 0.25  # spacing of the wind grid


@dataclass
class Forecast:
    generated_at: str  # UTC
    horizon_hours: int
    hotspots: list[Hotspot]
    clusters: list[SourceCluster]
    trajectories: list[Trajectory]
    zones: list[ZoneForecast]
    smoke_model: SmokeModel
    source_status: list[SourceStatus] = field(default_factory=list)
    attributions: list[Attribution] = field(default_factory=list)
    pm25_baseline: Pm25Baseline | None = None  # null when the CAMS step produced nothing
    pm25_grid: Pm25Grid | None = None  # null when the CAMS grid could not be fetched
    smoke_grid: SmokeGrid | None = None  # Hazier's smoke per hour for the map
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        # Compact separators keep the published file small. allow_nan=False turns a NaN that
        # slipped through into an error instead of invalid JSON.
        separators = None if indent else (",", ":")
        return json.dumps(self.to_dict(), indent=indent, separators=separators, allow_nan=False)


# --- Validation ----------------------------------------------------------------------------


def _type_errors(value: object, tp: object, path: str) -> list[str]:
    """Errors of value against a type annotation of this module (empty when it matches)."""
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        options = [_type_errors(value, a, path) for a in args]
        return [] if any(not o for o in options) else min(options, key=len)
    if origin is Literal:
        return [] if value in args else [f"{path}: {value!r} is not one of {args}"]
    if tp is type(None):
        return [] if value is None else [f"{path}: expected null"]
    if origin is list:
        if not isinstance(value, list):
            return [f"{path}: expected a list"]
        return [e for i, v in enumerate(value) for e in _type_errors(v, args[0], f"{path}[{i}]")]
    if origin is tuple:
        if not isinstance(value, tuple | list) or len(value) != len(args):
            return [f"{path}: expected {len(args)} values"]
        return [
            e
            for i, (v, a) in enumerate(zip(value, args))
            for e in _type_errors(v, a, f"{path}[{i}]")
        ]
    if origin is dict:
        if not isinstance(value, dict):
            return [f"{path}: expected an object"]
        return [
            e
            for k, v in value.items()
            for e in _type_errors(k, args[0], f"{path} key")
            + _type_errors(v, args[1], f"{path}.{k}")
        ]
    if is_dataclass(tp):
        if not isinstance(value, tp):
            return [f"{path}: expected {tp.__name__}"]
        hints = typing.get_type_hints(tp)
        return [
            e
            for f in fields(tp)
            for e in _type_errors(getattr(value, f.name), hints[f.name], f"{path}.{f.name}")
        ]
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return [f"{path}: expected a number"]
        return [] if math.isfinite(value) else [f"{path}: {value} is not finite"]
    if tp is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
        return [] if ok else [f"{path}: expected an integer"]
    if tp in (str, bool):
        return [] if isinstance(value, tp) else [f"{path}: expected {tp.__name__}"]
    return [f"{path}: unsupported annotation {tp!r}"]


def _is_utc_iso(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def validate_forecast(fc: Forecast) -> list[str]:
    """All the problems found in a Forecast; an empty list means it can be published."""
    errors = _type_errors(fc, Forecast, "forecast")
    if errors:
        return errors  # the invariants below assume the types are right
    if not _is_utc_iso(fc.generated_at):
        errors.append(f"generated_at: {fc.generated_at!r} is not UTC ISO 8601")
    if fc.schema_version != SCHEMA_VERSION:
        errors.append(f"schema_version: {fc.schema_version} != {SCHEMA_VERSION}")
    cluster_ids = [c.id for c in fc.clusters]
    if len(set(cluster_ids)) != len(cluster_ids):
        errors.append("clusters: duplicate ids")
    for t in fc.trajectories:
        if t.cluster_id not in set(cluster_ids):
            errors.append(f"trajectory {t.id}: unknown cluster {t.cluster_id}")
        if len(t.path) != len(t.hours) or len(t.path) < 2:
            errors.append(f"trajectory {t.id}: path and hours differ in length or are too short")
        if any(b < a for a, b in zip(t.hours, t.hours[1:])) or (t.hours and t.hours[0] < 0):
            errors.append(f"trajectory {t.id}: hours must start at 0 or later and increase")
    zone_ids = [z.id for z in fc.zones]
    if len(set(zone_ids)) != len(zone_ids):
        errors.append("zones: duplicate ids")
    for z in fc.zones:
        if z.smoke_sources and abs(sum(s.share for s in z.smoke_sources) - 1.0) > 1e-6:
            errors.append(f"zone {z.id}: smoke source shares do not sum to 1")
        if z.smoke_eta_hours is None and (z.smoke_eta_earliest_hours or z.smoke_eta_latest_hours):
            errors.append(f"zone {z.id}: ETA range without an ETA")
        for p in [z.cams_now, z.cams_peak, *z.cams_pm25]:
            if p is not None and not _is_utc_iso(p.time):
                errors.append(f"zone {z.id}: CAMS time {p.time!r} is not UTC ISO 8601")
    for h in fc.hotspots:
        if not _is_utc_iso(h.acquired_at):
            errors.append(f"hotspot {h.id}: acquired_at is not UTC ISO 8601")
    g = fc.pm25_grid
    if g is not None and len(g.values) != len(g.hours) * len(g.lats) * len(g.lons):
        errors.append("pm25_grid: values do not match hours x lats x lons")
    s = fc.smoke_grid
    if s is not None:
        try:
            n = len(base64.b64decode(s.data, validate=True))
        except (ValueError, TypeError):
            n = -1
        if n != len(s.hours) * len(s.lats) * len(s.lons):
            errors.append("smoke_grid: data does not match hours x lats x lons")
    return errors
