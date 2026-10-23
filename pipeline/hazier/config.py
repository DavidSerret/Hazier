"""Region, grid, zones and model parameters.

All tunable constants live here so the physics and the sources stay free of magic numbers.
"""

from dataclasses import dataclass

# Domain (degrees). Covers Sumatra, Peninsular Malaysia, Borneo and the Singapore strait.
LON_MIN, LON_MAX = 95.0, 120.0
LAT_MIN, LAT_MAX = -8.0, 12.0

# Wind grid. 1 degree gives (25 + 1) * (20 + 1) = 546 points, i.e. 546 Open-Meteo calls per run.
GRID_STEP_DEG = 1.0

# Trajectory model
HORIZON_HOURS = 72
STEP_HOURS = 1
PRESSURE_LEVEL_HPA = 850

# FIRMS: hotspots from the last 48 h
FIRMS_LOOKBACK_HOURS = 48

# Zone receptors: circles of this radius around each centre.
ZONE_RADIUS_KM = 50.0


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    lat: float
    lon: float
    radius_km: float = ZONE_RADIUS_KM


# Phase 1 receptors. Klang Valley is one zone (KL + Selangor + Putrajaya).
# Melaka/Seremban is centred between the two towns so one circle covers both.
ZONES: tuple[Zone, ...] = (
    Zone("klang_valley", "Klang Valley", 3.14, 101.69),
    Zone("johor_bahru", "Johor Bahru", 1.49, 103.74),
    Zone("melaka_seremban", "Melaka / Seremban", 2.46, 102.10),
    Zone("ipoh", "Ipoh", 4.60, 101.09),
    Zone("penang", "Penang", 5.41, 100.33),
    Zone("kuantan", "Kuantan", 3.81, 103.33),
    Zone("singapore", "Singapore", 1.35, 103.82),
    Zone("kuching", "Kuching", 1.55, 110.36),
)

DEFAULT_ZONE_ID = "klang_valley"

# Our own trajectory risk levels: lower bound of the 0 to 1 risk index for each level.
# These are NOT Malaysian API categories (those apply only to real PM2.5 concentrations).
RISK_LEVEL_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("low", 0.0),
    ("moderate", 0.25),
    ("high", 0.5),
    ("very_high", 0.75),
)

# Malaysian API categories applied to CAMS PM2.5 (ug/m3): upper bound of each category.
# TODO: verify the breakpoints against the current DOE API guidance before using them.
PM25_CATEGORY_UPPER_BOUNDS: tuple[tuple[str, float], ...] = (
    ("good", 12.0),
    ("moderate", 35.4),
    ("unhealthy", 150.4),
    ("very_unhealthy", 250.4),
    ("hazardous", float("inf")),
)
