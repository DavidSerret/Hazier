"""The forecast.json contract between the pipeline and the web.

web/src/data/types.ts mirrors this file field by field. Change both together and bump
SCHEMA_VERSION when the shape changes.

Conventions:
- All timestamps are ISO 8601 strings in UTC (for example "2026-09-14T06:00:00Z").
  The web converts them to Malaysia time (MYT, UTC+8) for display.
- Coordinates are (lon, lat) pairs in degrees, the order MapLibre and deck.gl expect.
- "hours" and "hour" are offsets from generated_at, in hours.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

SCHEMA_VERSION = 1

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
    category: Pm25Category  # Malaysian API category of basis_ugm3


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
    # smoke takes to build up around the arrival threshold (see risk.py); earliest and latest
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
    """CAMS PM2.5 on the wind grid, for the map colour layer, quantised to stay small.

    values holds one integer per (frame, lat, lon), row-major: frame first, then latitude
    (south to north), then longitude (west to east). The concentration is value * quantum_ugm3;
    null marks a missing value. Each value is the trailing averaging_hours mean, the same basis
    as the zone categories, so the map and the cards use the same colours for the same air.
    """

    lats: list[float]  # ascending
    lons: list[float]  # ascending
    hours: list[float]  # hours since generated_at of each frame
    averaging_hours: int
    quantum_ugm3: float
    values: list[int | None]


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
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        # Compact separators keep the published file small. allow_nan=False turns a NaN that
        # slipped through into an error instead of invalid JSON.
        separators = None if indent else (",", ":")
        return json.dumps(self.to_dict(), indent=indent, separators=separators, allow_nan=False)
