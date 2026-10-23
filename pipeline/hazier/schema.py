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

# Our own trajectory risk levels.
RiskLevel = Literal["low", "moderate", "high", "very_high"]

# Malaysian API categories, used only for CAMS PM2.5 concentrations.
Pm25Category = Literal["good", "moderate", "unhealthy", "very_unhealthy", "hazardous"]

SourceName = Literal["firms", "winds", "cams", "apims"]
SourceState = Literal["ok", "degraded", "failed"]


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
class RiskPoint:
    hour: float  # hours since generated_at
    index: float  # smoke index, >= 0 and unbounded (see hazier/model/risk.py)
    level: RiskLevel


@dataclass
class Pm25Point:
    time: str  # UTC
    value_ugm3: float  # CAMS PM2.5
    category: Pm25Category  # Malaysian API category of the concentration


@dataclass
class ZoneForecast:
    id: str
    name: str
    lat: float
    lon: float
    radius_km: float
    risk_index: float  # peak smoke index over the horizon
    risk_level: RiskLevel  # level of that peak
    peak_hour: float | None  # hour of the peak, null if no smoke reaches the zone
    eta_hours: float | None  # first hour the index reaches "moderate", null if it never does
    # Uncertainty of the ETA from the dispersion ensemble (10th and 90th percentiles). Both
    # null when eta_hours is null; eta_latest_hours null alone means "possibly after 72 h".
    eta_earliest_hours: float | None
    eta_latest_hours: float | None
    sources: list[SourceShare]  # source regions of the smoke reaching the zone, largest first
    risk_series: list[RiskPoint]  # hourly, lets the timeline update zone risk when scrubbing
    cams_pm25: list[Pm25Point]  # CAMS baseline series


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
class Forecast:
    generated_at: str  # UTC
    horizon_hours: int
    hotspots: list[Hotspot]
    clusters: list[SourceCluster]
    trajectories: list[Trajectory]
    zones: list[ZoneForecast]
    # Lower bound of the smoke index for each level, so the web can draw the bands.
    risk_thresholds: dict[str, float]
    source_status: list[SourceStatus] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        # Compact separators keep the published file small. allow_nan=False turns a NaN that
        # slipped through into an error instead of invalid JSON.
        separators = None if indent else (",", ":")
        return json.dumps(self.to_dict(), indent=indent, separators=separators, allow_nan=False)
