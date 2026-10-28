"""Region, grid, zones and model parameters.

All tunable constants live here so the physics and the sources stay free of magic numbers.
"""

from dataclasses import dataclass

# Domain (degrees). Covers Sumatra, Peninsular Malaysia, Borneo and the Singapore strait.
LON_MIN, LON_MAX = 95.0, 120.0
LAT_MIN, LAT_MAX = -8.0, 12.0

# Open-Meteo wind grid (the fallback wind source, see below). 1 degree gives (25 + 1) * (20 + 1)
# = 546 points, i.e. 546 Open-Meteo calls per run.
GRID_STEP_DEG = 1.0

# Trajectory model
HORIZON_HOURS = 72
STEP_HOURS = 1
PRESSURE_LEVEL_HPA = 850

# --- Winds, primary source: ECMWF open data (sources/ecmwf.py) -----------------------------
#
# The official ECMWF open data: IFS at 0.25 degrees, u and v at 850 hPa, read directly from the
# GRIB files (CC BY 4.0). Runs at 00, 06, 12 and 18 UTC, 3-hourly steps. The files are global;
# only the u and v fields are downloaded (byte ranges, about 1.2 MB per step) and cropped to the
# domain. The portal keeps the last 12 runs (about 3 days). Google, Azure and AWS are official
# mirrors. The portal is tried first; AWS last, as it answers bursts of byte-range requests with
# "SlowDown" (seen in testing, October 2026).
ECMWF_SOURCES: tuple[str, ...] = ("ecmwf", "google", "azure", "aws")
ECMWF_RESOL = "0p25"
# Grid of the wind field. 0.25 is the native grid (81 x 101 points over the domain); 0.5 keeps
# every second point. Both are small: the field is 121 hours x 8,181 points x 2 at 0.25 degrees,
# about 16 MB in float64, and the trajectory cost does not depend on the grid size.
ECMWF_GRID_STEP_DEG = 0.25
ECMWF_STEP_HOURS = 3
# A run is published about 7 to 9 hours after its start. Runs younger than this are not tried.
ECMWF_MIN_DELAY_HOURS = 6
# Longest step of each run (UTC hour of the run -> hours). 00 and 12 UTC go to 144 h at 3-hourly
# steps; 06 and 18 UTC are documented as 90 h by the client and 144 h by the portal: 90 is safe.
ECMWF_MAX_STEP_HOURS = {0: 144, 6: 90, 12: 144, 18: 90}
# Runs tried for the forecast part, newest first. The first one with every step wins.
ECMWF_FORECAST_RUN_TRIES = 4
# Past winds come from the short steps of earlier runs: for a past time, the run that started at
# or just before it (step 0 or 3), else an older run, up to this step. Beyond that it is a gap.
ECMWF_PAST_MAX_STEP_HOURS = 12
# Hourly values are linear in time between 3-hourly slices. Two slices further apart than this
# (a missing slice in the past) are not bridged: those hours stay missing.
ECMWF_MAX_BRIDGE_HOURS = 6
# HTTP behaviour of the client (its defaults retry 500 times every 2 minutes).
ECMWF_RETRIES = 2
ECMWF_RETRY_AFTER_S = 10
# Cropped fields are cached per run and step (immutable once published); older runs are pruned.
ECMWF_CACHE_DAYS = 4

# --- Winds, fallback: Open-Meteo (sources/winds.py) ----------------------------------------
#
# Used only when ECMWF open data fails. On GitHub's shared runners Open-Meteo throttles hard
# (October 2026: 100 of 546 points missing after 8 minutes), so it is a last resort.
# One global model is named explicitly, never "best_match",
# so a run is reproducible and the model is recorded in the output.
# ecmwf_ifs025 is ECMWF IFS at 0.25 degrees. Pressure-level fields are 3-hourly upstream and
# interpolated to hourly by Open-Meteo. The grid cell nearest to each requested point is used.
WIND_MODEL = "ecmwf_ifs025"

# The API's hourly axis starts at 00:00 UTC of the current day. To cover "now" to "now + 72 h"
# at any hour of the day, 23 + 72 = 95 hourly steps are needed, i.e. 4 whole days.
WIND_FORECAST_DAYS = 4
# Past winds, to carry the smoke emitted before t0 (since the earliest hotspot of the 48 h
# lookback) up to t0. past_days=2 starts the axis at 00:00 UTC two days ago, so it always covers
# t0 - 48 h. Open-Meteo counts a location as more than one call only beyond 10 variables or
# 2 weeks of data, so 6 days of 2 variables still cost one call per location.
WIND_PAST_DAYS = 2
WIND_PAST_HOURS = 48  # the wind field starts this long before t0

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

# Static heat sources (gas flares, power plants, mills, volcanoes) are not vegetation fires.
# Their cells come from hazier/data/static_sources.json (FIRMS archive type field and a
# persistence rule, see scripts/build_static_sources.py). A hotspot within this many cells
# (0.004 degrees, about 440 m) of a static cell is dropped before clustering, which covers
# the 375 m pixel and its geolocation error.
STATIC_REACH_CELLS = 1

# Particle release: every cluster releases PARTICLES_PER_RELEASE particles at t0 and then every
# RELEASE_INTERVAL_HOURS until the end of the horizon, as if the fires kept burning.
# The interval must be a multiple of STEP_HOURS. Hourly releases keep consecutive puffs about
# 20 km apart, well inside a 50 km zone, so a zone's smoke does not flicker between puffs.
RELEASE_INTERVAL_HOURS = 1
PARTICLES_PER_RELEASE = 20

# Particles are advected in batches of at most this many, so peak memory stays flat however
# many particles a busy day produces. Batches use separate random streams derived from
# RANDOM_SEED, so results are reproducible for a given batch size.
ADVECT_BATCH_PARTICLES = 250_000

# Horizontal dispersion: random walk with this diffusivity (m2/s), see trajectories.py.
# 2e4 m2/s spreads a puff to a standard deviation of about 60 km after 24 h and 100 km after 72 h.
HORIZONTAL_DIFFUSIVITY_M2S = 2.0e4

# One seed for every random choice (dispersion, export sampling): identical inputs give
# identical forecasts.
RANDOM_SEED = 2026

# Trajectories exported to forecast.json: a few flow lines for the map. The smoke model uses all
# particles, and the smoke itself is shown by the density grid below. The lines are shared out
# between clusters in proportion to the smoke they emit, so a small fire gets none.
TRAJ_EXPORT_MAX = 60

# Smoke density grid for the map: Hazier's smoke per hour on a coarse grid (see model/smoke.py).
# 0.5 degrees (about 55 km) matches the spread of a puff after a day of travel (60 km), so a
# finer grid would only show particle noise. Values are written on a log scale in one byte:
# 0 below SMOKE_GRID_MIN_INDEX, then SMOKE_GRID_STEPS_PER_DECADE steps per factor of ten.
SMOKE_GRID_CELL_DEG = 0.5
SMOKE_GRID_MIN_INDEX = 1.0  # a tenth of the arrival threshold
SMOKE_GRID_STEPS_PER_DECADE = 40
# 0.01 degrees is about 1.1 km: finer than the model can resolve, coarse enough to stay small.
TRAJ_EXPORT_DECIMALS = 2

# Smoke removal: particle weights decay as exp(-age / lifetime), age counted from the release.
# 3 days is typical for fine smoke aerosol in the humid tropics, see model/smoke.py.
REMOVAL_LIFETIME_HOURS = 72.0

# Arrival of smoke at a zone (internal, for the ETA and the agreement flag; NOT an air quality
# level): the first hour the smoke index reaches this value. PROVISIONAL, see model/smoke.py.
# With the 3-day removal, 10 is about what one cluster of 360 MW (summed FRP over 48 h, the
# upper quartile of the 23 Oct 2026 clusters) gives when a third of its plume covers a zone
# after a day of travel: (360 / 48) * 5.5 h * 1/3 * exp(-24 / 72). Smaller fires count only
# when several add up. A lone cluster at the 25 MW minimum can not reach it even with its whole
# plume over the zone.
SMOKE_ARRIVAL_THRESHOLD = 10.0

# ETA range: from the first hour the smoke index reaches threshold / factor to the first hour it
# reaches threshold * factor (see model/smoke.py).
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

# --- CAMS PM2.5 and the Malaysian API categories ---------------------------------------------

# Malaysian API (Indeks Pencemar Udara, IPU) categories applied to CAMS PM2.5 (ug/m3): upper
# bound of each category, for API bands 0-50, 51-100, 101-200, 201-300 and above 300.
#
# TODO: BREAKPOINTS PROVISIONAL, SOURCES CONFLICT. The official calculation document of the
# Department of Environment ("Pengiraan Indeks Pencemar Udara (IPU) / Air Pollutant Index (API)
# Calculation", https://apims.doe.gov.my/pdf/API_Calculation.pdf) could not be opened (HTTP 404
# on 24 Oct 2026). Two candidate tables are known, and they disagree on where "unhealthy"
# starts:
#
#   Candidate A (in use), ExpatGo, 31 Aug 2026, "comparison" table, which says it may differ
#   from the DOE's own calculation:
#       good 0-15, moderate 16-35, unhealthy 36-75, very unhealthy 76-250, hazardous 251+
#       ("unhealthy", API 101, starts just above 35 ug/m3)
#
#   Candidate B, a formula attributed to the DOE by another source:
#       API 101 ("unhealthy") at 75.5 ug/m3
#       The other band edges of this table could not be found or confirmed, so it can not be
#       used as it stands. If it is right, candidate A classifies 36 to 75 ug/m3 one band too
#       high.
#
# Candidate A is consistent with the 35 ug/m3 Malaysian ambient air quality target. PM2.5 was
# added to the API on 16 Aug 2018 (national haze action plan). A third site (aqihub.info) lists
# 1-hour values of 0-103 for "good", implausible for PM2.5, not used.
# Flip PM25_BREAKPOINTS_VERIFIED to True only after checking the DOE document; the flag is
# published in forecast.json so the web can label the categories as provisional until then.
PM25_BREAKPOINTS_VERIFIED = False
PM25_CATEGORY_UPPER_BOUNDS: tuple[tuple[str, float], ...] = (
    ("good", 15.0),
    ("moderate", 35.0),
    ("unhealthy", 75.0),
    ("very_unhealthy", 250.0),
    ("hazardous", float("inf")),
)

# Averaging period of the PM2.5 sub-index, in hours: the category of a time is taken from the
# trailing mean of CAMS over this many hours (the hour itself and the ones before it).
# VERIFIED: the DOE computes the PM2.5 sub-index from a 24-hour moving average, reported hourly,
# according to several sources citing the DOE's general information on the API (among them
# Malay Mail, 8 Oct 2026, "Hazy math: DOE explains why your AQI says 250 when the official API
# is 175"). The one site that says 1 hour (aqihub.info) is the one with implausible values.
PM25_AVERAGING_HOURS = 24
PM25_AVERAGING_VERIFIED = True

# CAMS request (Open-Meteo air quality API): the global domain, explicitly, so the result does
# not silently switch to the European one. The past day gives the history for the trailing mean.
CAMS_DOMAIN = "cams_global"
CAMS_RESOLUTION_KM = 45.0  # 0.4 degrees
CAMS_PAST_DAYS = 1
CAMS_FORECAST_DAYS = 4  # as WIND_FORECAST_DAYS: covers the current hour plus 72 h at any time
CAMS_TIMEOUT_S = 60
CAMS_RETRIES = 1
CAMS_BACKOFF_S = 10.0
# The series in forecast.json has one point every this many hours. Hourly: the hourly value is
# the main number shown, because the 24 h mean lags hours behind a change in the air.
CAMS_EXPORT_STEP_HOURS = 1

# Agreement between Hazier and CAMS over the window (see cams.agreement_flag): Hazier sees smoke
# when its smoke index reaches SMOKE_ARRIVAL_THRESHOLD, CAMS when its peak category reaches
# this one. PROVISIONAL: Malaysian cities often sit at "moderate" PM2.5 without any smoke, so
# the CAMS side starts at "unhealthy" (24 h mean above 35 ug/m3).
CAMS_ELEVATED_MIN_CATEGORY = "unhealthy"

# Trend for the zone card: the CAMS mean over the next 24 h against the mean over the last
# 24 h. "worsening" / "improving" when it changes by more than TREND_RELATIVE of the last
# mean and by at least TREND_MIN_UGM3 (so tiny changes on clean days stay "stable").
TREND_RELATIVE = 0.15
TREND_MIN_UGM3 = 3.0

# CAMS map layer, primary source: the Copernicus Atmosphere Data Store (sources/ads.py).
# The CAMS global forecast at its native 0.4 degrees (51 x 63 points over the domain), PM2.5
# hourly to 90 h, then 3-hourly. Runs at 00 and 12 UTC, guaranteed on the ADS by 10:00 and 22:00
# UTC. Requests wait in a queue, typically about a minute. Data older than 30 days is on tape.
ADS_URL = "https://ads.atmosphere.copernicus.eu/api"
ADS_KEY_ENV = "ADS_API_KEY"
ADS_DATASET = "cams-global-atmospheric-composition-forecasts"
ADS_VARIABLE = "particulate_matter_2.5um"
ADS_AVAILABILITY_DELAY_HOURS = 10  # the newest run tried is the newest one guaranteed online
ADS_RUN_TRIES = 2  # if that run fails, the one before it
ADS_MAX_HOURLY_LEAD = 90  # leads beyond this are 3-hourly upstream
ADS_MAX_LEAD = 120
# The whole ADS step (queue and download, every run tried) gives up after this long and the
# Open-Meteo fallback takes over. Sized to keep the workflow inside its timeout.
ADS_MAX_WAIT_S = 15 * 60
ADS_POLL_MAX_S = 20  # polling interval grows from 2 s up to this

# CAMS map layer, fallback: Open-Meteo at 2 degrees (13 x 11 = 143 calls), coarse but cheap.
CAMS_FALLBACK_GRID_STEP_DEG = 2.0
CAMS_GRID_BATCH_SIZE = 50
CAMS_GRID_MAX_CALLS_PER_MIN = 250
# Exported values are rounded to this step (ug/m3) and written as integers.
CAMS_GRID_QUANTUM_UGM3 = 1.0
# The map layer has one frame every this many hours (the web interpolates in between): hourly
# frames would triple its size for little visible change.
CAMS_GRID_EXPORT_STEP_HOURS = 3

# --- Export (python -m hazier) ---------------------------------------------------------------

# forecast.json goes here (pipeline/output is git-ignored); the workflow copies it into the site.
OUTPUT_DIRNAME = "site"
FORECAST_FILENAME = "forecast.json"
# Light history, one CSV line per run. Locally it goes to this git-ignored path (relative to
# pipeline/), so a local run can never put a file in the repository. On GitHub the workflow
# passes --history with a path inside a checkout of the separate "data" branch and commits it
# there, never to main (it keeps the scheduled workflow alive and feeds phase 2 verification).
HISTORY_RELPATH = "output/history/runs.csv"
# Raw hotspots in forecast.json: the strongest by FRP. All 15,000+ of a busy day would add
# about 2.2 MB; the clusters, all exported, already carry the full picture.
EXPORT_HOTSPOTS_MAX = 1500
