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


@dataclass
class Hotspot:
    id: int
    lat: float
    lon: float
    frp_mw: float  # fire radiative power, drives glow size
    confidence: str  # VIIRS confidence class: "low", "nominal" or "high"
    acquired_at: str  # UTC
    region: str  # source region label, for example "Riau"


@dataclass
class Trajectory:
    id: int
    hotspot_id: int
    source_region: str
    path: list[tuple[float, float]]  # (lon, lat) per step
    hours: list[float]  # hours since generated_at, same length as path


@dataclass
class SourceShare:
    region: str
    share: float  # 0 to 1, shares of a zone sum to 1


@dataclass
class RiskPoint:
    hour: float  # hours since generated_at
    index: float  # 0 to 1 trajectory risk index
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
    risk_index: float  # peak risk over the horizon, 0 to 1
    risk_level: RiskLevel
    eta_hours: float | None  # hours until smoke arrives, null if it does not within the horizon
    sources: list[SourceShare]
    risk_series: list[RiskPoint]  # hourly, lets the timeline update zone risk when scrubbing
    cams_pm25: list[Pm25Point]  # CAMS baseline series


@dataclass
class SourceStatus:
    name: SourceName
    ok: bool
    last_ok: str | None  # UTC of the last successful fetch, null if never


@dataclass
class Forecast:
    generated_at: str  # UTC
    horizon_hours: int
    hotspots: list[Hotspot]
    trajectories: list[Trajectory]
    zones: list[ZoneForecast]
    source_status: list[SourceStatus] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)
