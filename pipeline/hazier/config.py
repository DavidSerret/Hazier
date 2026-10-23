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

# Winds (Open-Meteo forecast API). One global model is named explicitly, never "best_match",
# so a run is reproducible and the model is recorded in the output.
# ecmwf_ifs025 is ECMWF IFS at 0.25 degrees. Pressure-level fields are 3-hourly upstream and
# interpolated to hourly by Open-Meteo. The grid cell nearest to each requested point is used.
WIND_MODEL = "ecmwf_ifs025"

# The API's hourly axis starts at 00:00 UTC of the current day. To cover "now" to "now + 72 h"
# at any hour of the day, 23 + 72 = 95 hourly steps are needed, i.e. 4 whole days.
WIND_FORECAST_DAYS = 4

# Open-Meteo counts each location of a multi-location request as one call.
# Free limits: 600 / min, 5,000 / h, 10,000 / day. One run is 546 calls; at 250 / min it takes
# about 2 minutes and stays far below every limit, even at 4 runs a day.
WIND_BATCH_SIZE = 50
WIND_MAX_CALLS_PER_MIN = 250

WIND_TIMEOUT_S = 60
WIND_RETRIES = 1
WIND_BACKOFF_S = 10.0

# FIRMS: hotspots from the last 48 h. The area API takes whole days (1 to 5), so 2 days is
# fetched and anything older than the lookback is dropped afterwards.
FIRMS_LOOKBACK_HOURS = 48
FIRMS_DAY_RANGE = 2

# VIIRS near-real-time sources: (label used in the data, FIRMS source id)
FIRMS_SOURCES: tuple[tuple[str, str], ...] = (
    ("S-NPP", "VIIRS_SNPP_NRT"),
    ("NOAA-20", "VIIRS_NOAA20_NRT"),
    ("NOAA-21", "VIIRS_NOAA21_NRT"),
)

# Quality filter: detections below this VIIRS confidence class are dropped ("low" < "nominal" < "high").
FIRMS_MIN_CONFIDENCE = "nominal"

# Deduplication across satellites. The three satellites overpass within about an hour of each
# other and see the same ~375 m pixel, so two detections closer than both thresholds are one fire.
# Detections of the same spot at overpasses hours apart are kept as separate observations.
FIRMS_DEDUP_DISTANCE_KM = 0.5
FIRMS_DEDUP_WINDOW_MIN = 180

# HTTP behaviour: one retry after a short backoff on timeouts, connection errors, 429 and 5xx.
FIRMS_TIMEOUT_S = 30
FIRMS_RETRIES = 1
FIRMS_BACKOFF_S = 3.0

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
