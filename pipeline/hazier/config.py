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

# Source clusters. Hotspots are binned on a regular grid aligned to (LAT_MIN, LON_MIN);
# 0.25 degrees is about 28 km, so one cluster stands for one fire complex rather than one pixel.
CLUSTER_CELL_DEG = 0.25
# Clusters whose summed FRP is below this are dropped as weak or spurious sources.
# Starting value, tuned on the October 2026 data (see docs/methodology.md).
CLUSTER_MIN_FRP_MW = 25.0

# Particle release: every cluster releases PARTICLES_PER_RELEASE particles at t0 and then every
# RELEASE_INTERVAL_HOURS until the end of the horizon, as if the fires kept burning.
# The interval must be a multiple of STEP_HOURS. Hourly releases keep consecutive puffs about
# 20 km apart, well inside a 50 km zone, so zone risk does not flicker between puffs.
RELEASE_INTERVAL_HOURS = 1
PARTICLES_PER_RELEASE = 20

# Horizontal dispersion: random walk with this diffusivity (m2/s), see trajectories.py.
# 2e4 m2/s spreads a puff to a standard deviation of about 60 km after 24 h and 100 km after 72 h.
HORIZONTAL_DIFFUSIVITY_M2S = 2.0e4

# One seed for every random choice (dispersion, export sampling): identical inputs give
# identical forecasts.
RANDOM_SEED = 2026

# Trajectories exported to forecast.json. The risk uses all particles; the web gets this many
# display trajectories (one undispersed tracer per release, see trajectories.py).
TRAJ_EXPORT_MAX = 300
# A trajectory passing within this distance of a zone's edge is "near" that zone and shown first.
TRAJ_EXPORT_NEAR_KM = 50.0
# Near-zone trajectories take at most this share of the cap; the rest keeps regional context.
TRAJ_EXPORT_PRIORITY_SHARE = 0.6
# 0.01 degrees is about 1.1 km: finer than the model can resolve, coarse enough to stay small.
TRAJ_EXPORT_DECIMALS = 2

# ETA range: from the first hour the smoke index reaches moderate / factor to the first hour it
# reaches moderate * factor (see risk.py).
ETA_RANGE_FACTOR = 2.0

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

# Our own smoke risk levels: lower bound of the smoke index (see risk.py) for each level.
# These are NOT Malaysian API categories (those apply only to real PM2.5 concentrations).
# PROVISIONAL AND UNCALIBRATED: set by judgement on the October 2026 data, to be calibrated
# against APIMS and CAMS in phase 2.
RISK_LEVEL_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("low", 0.0),
    ("moderate", 10.0),
    ("high", 50.0),
    ("very_high", 200.0),
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
