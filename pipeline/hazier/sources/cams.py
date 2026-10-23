"""Open-Meteo air quality API (CAMS): the PM2.5 shown to users, per zone and as a map layer.

CAMS is far more accurate than Hazier's own smoke index for "how much PM2.5", so the air
quality level users see is the CAMS PM2.5 and its Malaysian API category. Hazier adds the
arrival time and the origin of the smoke (see model/risk.py).

This module fetches:
- the hourly PM2.5 at the 8 zone centres, for the zone cards (category now, peak, trend);
- the hourly PM2.5 on the 1 degree wind grid, for the map colour layer.
Both cover the same window as the trajectory forecast (the current hour to 72 h ahead).

Data
----
- API: https://air-quality-api.open-meteo.com/v1/air-quality, variable `pm2_5` (ug/m3), domain
  `cams_global`, requested explicitly so the result never silently switches to the European
  model.
- Resolution: CAMS global is 0.4 degrees, about 45 km, 3-hourly upstream (Open-Meteo returns
  hourly values). Each zone gets the value of the cell around its centre: a 45 km cell against
  a zone circle 100 km across. It can not resolve a city, it is a regional baseline.
- Open-Meteo returns the cell nearest to each point, with the cell's own coordinates, so
  results are matched to points by request order.
- The request includes the previous day (past_days=1) so the first hours of the window have
  the history needed for the trailing mean.
- Calls: each location counts as one call. The zones cost 8 per run; the grid 546, sent in
  batches of 50 and throttled like the winds, and cached per CAMS run while developing.

Categories
----------
The Malaysian API sub-index for PM2.5 is based on a 24 h average, reported hourly as a moving
average. The category of each time is therefore taken from the trailing mean of CAMS over
config.PM25_AVERAGING_HOURS (the hour and the ones before it) and not from the raw hourly value,
which would exaggerate short peaks. The 24 h moving average is confirmed by several sources
citing the DOE. THE BREAKPOINTS ARE NOT: the sources conflict (see config.py), and the output
carries `categories_verified: false` until they are checked against the official DOE document.
The map layer uses the same 24 h mean, so the map and the cards colour the same air the same.

Trend
-----
For the card: the mean over the next 24 h against the mean over the last 24 h (the category
basis at t0). "worsening" or "improving" when it changes by more than TREND_RELATIVE of the
last mean and by at least TREND_MIN_UGM3, otherwise "stable".

Agreement flag
--------------
For verification in phase 2, each zone gets a flag comparing Hazier and CAMS over the window:

    both_elevated   Hazier expects smoke and CAMS expects elevated PM2.5
    hazier_only     only Hazier does
    cams_only       only CAMS does
    both_low        neither does

Hazier "expects smoke" when its smoke index reaches the arrival threshold (it has an ETA);
CAMS "expects elevated PM2.5" when its peak category is at least CAMS_ELEVATED_MIN_CATEGORY.
The flag says nothing about which one is right; it marks the zones where the two differ. It is
null when CAMS has no data for a zone.

Failure handling matches the other sources: the fetch functions never raise and return a
status that is ok, degraded (some points have no data) or failed (none).

Debug: `python -m hazier.sources.cams` runs the real fetch (zones and grid) and prints a summary.
The full table with Hazier's smoke timing is printed by `python -m hazier.model.risk`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import requests

from hazier import config
from hazier.env import PIPELINE_DIR
from hazier.schema import (
    AgreementFlag,
    Attribution,
    Pm25Baseline,
    Pm25Category,
    Pm25Grid,
    Pm25Point,
    SourceStatus,
    Trend,
    ZoneForecast,
)

log = logging.getLogger(__name__)

API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
META_URL = "https://air-quality-api.open-meteo.com/data/{domain}/static/meta.json"
CACHE_DIR = PIPELINE_DIR / ".cache" / "cams"
VARIABLE = "pm2_5"
SOURCE_LABEL = "CAMS global via Open-Meteo"
# Debug plot colours, in the order of the Malaysian API palette.
CATEGORY_COLOURS = {
    "good": "#2c7fb8",
    "moderate": "#41ab5d",
    "unhealthy": "#fec44f",
    "very_unhealthy": "#e6550d",
    "hazardous": "#a50f15",
}


class CamsError(Exception):
    """A failure of a request. The message is safe to log (there is no API key)."""


class _Transient(Exception):
    """A failure worth one retry (timeout, connection error, 429, 5xx)."""


# --- Attribution and metadata --------------------------------------------------------------


def attributions() -> list[Attribution]:
    """Credit the web must show next to CAMS and Open-Meteo data (both are mandatory)."""
    return [
        Attribution(
            name="CAMS",
            text=(
                "Contains modified Copernicus Atmosphere Monitoring Service (CAMS) information: "
                "global atmospheric composition forecasts, PM2.5."
            ),
            url="https://atmosphere.copernicus.eu/",
        ),
        Attribution(
            name="Open-Meteo",
            text="Weather and air quality data by Open-Meteo.com (CC BY 4.0).",
            url="https://open-meteo.com/",
        ),
    ]


def baseline_metadata() -> Pm25Baseline:
    """How cams_pm25 was derived, for the web to explain and to label as provisional."""
    return Pm25Baseline(
        source=SOURCE_LABEL,
        resolution_km=config.CAMS_RESOLUTION_KM,
        step_hours=config.CAMS_EXPORT_STEP_HOURS,
        averaging_hours=config.PM25_AVERAGING_HOURS,
        averaging_verified=config.PM25_AVERAGING_VERIFIED,
        categories_verified=config.PM25_BREAKPOINTS_VERIFIED,
        category_upper_bounds={
            name: (None if bound == float("inf") else bound)
            for name, bound in config.PM25_CATEGORY_UPPER_BOUNDS
        },
        cams_elevated_category=config.CAMS_ELEVATED_MIN_CATEGORY,
    )


# --- Categories, trend and the agreement flag ----------------------------------------------


def pm25_category(value: float) -> Pm25Category:
    """Malaysian API category of a PM2.5 value (ug/m3). The upper bound of a band is inclusive."""
    for name, upper in config.PM25_CATEGORY_UPPER_BOUNDS:
        if value <= upper:
            return name  # type: ignore[return-value]
    raise ValueError(f"no category for {value}")  # unreachable: the last bound is infinite


def _category_rank(name: str) -> int:
    return [n for n, _ in config.PM25_CATEGORY_UPPER_BOUNDS].index(name)


def agreement_flag(smoke_arrives: bool, cams_category: Pm25Category) -> AgreementFlag:
    """Compare Hazier's smoke arrival with CAMS's peak category. See the module docstring."""
    cams = _category_rank(cams_category) >= _category_rank(config.CAMS_ELEVATED_MIN_CATEGORY)
    if smoke_arrives and cams:
        return "both_elevated"
    if smoke_arrives:
        return "hazier_only"
    return "cams_only" if cams else "both_low"


def trend(last_24h: float, next_24h: float) -> Trend:
    """Next 24 h mean against the last 24 h mean (ug/m3). See the module docstring."""
    margin = max(config.TREND_RELATIVE * last_24h, config.TREND_MIN_UGM3)
    change = next_24h - last_24h
    if change > margin:
        return "worsening"
    if change < -margin:
        return "improving"
    return "stable"


def trailing_mean(values: np.ndarray, hours: int) -> np.ndarray:
    """Mean of each value and the hours - 1 before it. NaN where there is not enough history
    or any of the values is missing."""
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if hours <= len(values):
        windows = np.lib.stride_tricks.sliding_window_view(values, hours)
        out[hours - 1 :] = windows.mean(axis=-1)
    return out


# --- Data ----------------------------------------------------------------------------------


@dataclass
class CamsSeries:
    hourly: np.ndarray  # (T,) CAMS PM2.5 at the zone centre, ug/m3
    basis: np.ndarray  # (T,) trailing mean over the averaging period, ug/m3


@dataclass
class CamsResult:
    times: np.ndarray | None  # (T,) datetime64[s], UTC; the same hours as the forecast window
    series: dict[str, CamsSeries]  # by zone id; zones without data are absent
    status: SourceStatus
    missing_zones: list[str] = field(default_factory=list)
    calls_used: int = 0  # Open-Meteo calls (one per location)


@dataclass
class CamsGrid:
    times: np.ndarray  # (T,) datetime64[s], UTC
    lats: np.ndarray  # (Y,) ascending
    lons: np.ndarray  # (X,) ascending
    hourly: np.ndarray  # (T, Y, X) ug/m3, NaN where missing
    basis: np.ndarray  # (T, Y, X) trailing mean, ug/m3, NaN where missing


@dataclass
class CamsGridResult:
    grid: CamsGrid | None
    status: SourceStatus
    calls_used: int = 0  # retries included; 0 on a cache hit
    batches: int = 0
    failed_batches: int = 0
    missing_points: int = 0
    from_cache: bool = False


def _floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- HTTP ----------------------------------------------------------------------------------


class _Counter:
    def __init__(self) -> None:
        self.calls = 0


def _request(params: dict, n_locations: int, counter: _Counter) -> list[dict]:
    counter.calls += n_locations  # counted even if the request then fails
    try:
        resp = requests.get(API_URL, params=params, timeout=config.CAMS_TIMEOUT_S)
    except requests.RequestException as exc:
        raise _Transient(type(exc).__name__) from None
    status = resp.status_code
    if status == 429 or status >= 500:
        raise _Transient(f"HTTP {status}")
    try:
        data = resp.json()
    except ValueError:
        raise CamsError(f"HTTP {status}: response is not JSON") from None
    if isinstance(data, dict) and data.get("error"):
        raise CamsError(f"HTTP {status}: {str(data.get('reason', 'unknown error'))[:150]}")
    if status != 200:
        raise CamsError(f"HTTP {status}")
    locations = [data] if isinstance(data, dict) else data
    if not isinstance(locations, list) or len(locations) != n_locations:
        raise CamsError(f"expected {n_locations} locations, got an unexpected response")
    return locations


def _fetch_points(points: list[tuple[float, float]], counter: _Counter) -> list[dict]:
    """One request for these (lat, lon) points, with one retry on transient failures."""
    params = {
        "latitude": ",".join(format(lat, "g") for lat, _ in points),
        "longitude": ",".join(format(lon, "g") for _, lon in points),
        "hourly": VARIABLE,
        "domains": config.CAMS_DOMAIN,
        "past_days": config.CAMS_PAST_DAYS,
        "forecast_days": config.CAMS_FORECAST_DAYS,
        "timezone": "GMT",
        "timeformat": "unixtime",
    }
    for attempt in range(config.CAMS_RETRIES + 1):
        try:
            return _request(params, len(points), counter)
        except _Transient as exc:
            if attempt >= config.CAMS_RETRIES:
                raise CamsError(f"{exc} (gave up after {attempt + 1} attempts)") from None
            log.warning("CAMS: %s, retrying in %.0f s", exc, config.CAMS_BACKOFF_S)
            time.sleep(config.CAMS_BACKOFF_S * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


def _fetch_run_init() -> datetime | None:
    """Initialisation time of the latest CAMS run, from its metadata file (for the cache)."""
    try:
        resp = requests.get(META_URL.format(domain=config.CAMS_DOMAIN), timeout=15)
        resp.raise_for_status()
        return datetime.fromtimestamp(int(resp.json()["last_run_initialisation_time"]), UTC)
    except Exception as exc:  # noqa: BLE001 - metadata is optional, only caching depends on it
        log.warning("CAMS: run time unavailable (%s), grid caching disabled", type(exc).__name__)
        return None


# --- Assembly ------------------------------------------------------------------------------


def _window(now: datetime) -> tuple[int, int, np.ndarray]:
    """(t0 as unix seconds, number of steps, window times) for [t0, t0 + horizon]."""
    t0 = int(_floor_hour(now).timestamp())
    steps = config.HORIZON_HOURS // config.STEP_HOURS + 1
    times = (t0 + np.arange(steps, dtype="int64") * config.STEP_HOURS * 3600).astype(
        "datetime64[s]"
    )
    return t0, steps, times


def _locate(axis: np.ndarray, t0: int, steps: int) -> slice:
    """The window's slice of the API axis; it must also hold the history of the trailing mean."""
    hits = np.flatnonzero(axis == t0)
    if hits.size == 0 or hits[0] < config.PM25_AVERAGING_HOURS - 1:
        raise CamsError("the forecast time axis does not cover the current hour and its history")
    return slice(int(hits[0]), int(hits[0]) + steps)


def _series(loc: dict, axis: np.ndarray, window: slice, steps: int) -> CamsSeries | None:
    """Window of one location's response, or None if any value it needs is missing."""
    try:
        hourly = loc["hourly"]
        if np.array(hourly["time"], dtype="int64").tolist() != axis.tolist():
            raise ValueError("time axis differs")
        # None (JSON null) becomes NaN when converting to float.
        values = np.array(hourly[VARIABLE], dtype=float)
        if values.shape != axis.shape:
            raise ValueError("series length differs")
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("CAMS: a location is unusable (%s)", type(exc).__name__)
        return None
    basis = trailing_mean(values, config.PM25_AVERAGING_HOURS)
    cut_values, cut_basis = values[window], basis[window]
    if len(cut_values) < steps or np.isnan(cut_values).any() or np.isnan(cut_basis).any():
        return None
    return CamsSeries(cut_values, cut_basis)


def fetch_cams(
    zones: tuple[config.Zone, ...] | None = None, now: datetime | None = None
) -> CamsResult:
    """Fetch CAMS PM2.5 at the zone centres over [now, now + 72 h]. Never raises."""
    zones = config.ZONES if zones is None else zones
    now = now or datetime.now(UTC)
    counter = _Counter()
    try:
        locations = _fetch_points([(z.lat, z.lon) for z in zones], counter)
        t0, steps, times = _window(now)
        axis = np.array(locations[0]["hourly"]["time"], dtype="int64")
        window = _locate(axis, t0, steps)
        series: dict[str, CamsSeries] = {}
        missing: list[str] = []
        for zone, loc in zip(zones, locations, strict=True):
            s = _series(loc, axis, window, steps)
            if s is None:
                missing.append(zone.id)
            else:
                series[zone.id] = s
    except CamsError as exc:
        log.error("CAMS failed: %s", exc)
        status = SourceStatus("cams", "failed", None, str(exc)[:200])
        return CamsResult(None, {}, status, [], counter.calls)
    except Exception as exc:  # noqa: BLE001 - the pipeline must never crash on this source
        log.error("CAMS failed: unexpected %s", type(exc).__name__)
        status = SourceStatus("cams", "failed", None, "unexpected error")
        return CamsResult(None, {}, status, [], counter.calls)

    log.info("CAMS zones: Open-Meteo calls used this run: %d", counter.calls)
    if not series:
        status = SourceStatus("cams", "failed", None, "no usable data for any zone")
        return CamsResult(None, {}, status, missing, counter.calls)
    if missing:
        names = {z.id: z.name for z in zones}
        detail = "no CAMS data for " + ", ".join(names[m] for m in missing)
        log.warning("CAMS: degraded, %s", detail)
        status = SourceStatus("cams", "degraded", _iso(now), detail)
    else:
        status = SourceStatus("cams", "ok", _iso(now))
    return CamsResult(times, series, status, missing, counter.calls)


# --- The map layer -------------------------------------------------------------------------


def _grid_cache_path(cache_dir: Path, run_init: datetime, now: datetime) -> Path:
    from hazier.sources.winds import grid_axes

    lats, lons = grid_axes()
    spec = json.dumps(
        [lats.tolist(), lons.tolist(), config.CAMS_PAST_DAYS, config.CAMS_FORECAST_DAYS]
    )
    digest = hashlib.sha1(spec.encode()).hexdigest()[:8]
    # The hourly axis starts at 00:00 UTC of the request day (minus the past days), so the day
    # is part of the key.
    return cache_dir / f"{config.CAMS_DOMAIN}_{run_init:%Y%m%dT%H%M}_{now:%Y%m%d}_{digest}.npz"


def fetch_cams_grid(
    now: datetime | None = None, use_cache: bool = True, cache_dir: Path | None = None
) -> CamsGridResult:
    """Fetch CAMS PM2.5 on the wind grid over [now, now + 72 h], for the map. Never raises."""
    now = now or datetime.now(UTC)
    try:
        return _fetch_grid(now, use_cache, cache_dir or CACHE_DIR)
    except CamsError as exc:
        log.error("CAMS grid failed: %s", exc)
        return CamsGridResult(None, SourceStatus("cams", "failed", None, str(exc)[:200]))
    except Exception as exc:  # noqa: BLE001 - the pipeline must never crash on this source
        log.error("CAMS grid failed: unexpected %s", type(exc).__name__)
        return CamsGridResult(None, SourceStatus("cams", "failed", None, "unexpected error"))


def _fetch_grid(now: datetime, use_cache: bool, cache_dir: Path) -> CamsGridResult:
    from hazier.sources.winds import grid_axes

    lats, lons = grid_axes()
    points = [(float(lat), float(lon)) for lat in lats for lon in lons]
    result = CamsGridResult(None, SourceStatus("cams", "failed", None))

    run_init = _fetch_run_init()
    cache_file = _grid_cache_path(cache_dir, run_init, now) if run_init else None
    cached = None
    if use_cache and cache_file and cache_file.is_file():
        try:
            with np.load(cache_file) as z:
                cached = z["axis"], z["values"]
        except (OSError, KeyError, ValueError):
            cached = None

    if cached is not None:
        log.info("CAMS grid: using cache %s (0 API calls)", cache_file.name)
        axis, raw = cached
        result.from_cache = True
    else:
        counter = _Counter()
        locations: list[dict | None] = []
        last_start, last_size = None, 0
        for lo in range(0, len(points), config.CAMS_GRID_BATCH_SIZE):
            batch = points[lo : lo + config.CAMS_GRID_BATCH_SIZE]
            if last_start is not None:  # keep the average under CAMS_GRID_MAX_CALLS_PER_MIN
                wait = last_size / config.CAMS_GRID_MAX_CALLS_PER_MIN * 60 - (
                    time.monotonic() - last_start
                )
                if wait > 0:
                    time.sleep(wait)
            last_start, last_size = time.monotonic(), len(batch)
            result.batches += 1
            try:
                locations.extend(_fetch_points(batch, counter))
            except CamsError as exc:
                log.error("CAMS grid: batch %d failed: %s", result.batches, exc)
                result.failed_batches += 1
                locations.extend([None] * len(batch))
        result.calls_used = counter.calls
        log.info("CAMS grid: Open-Meteo calls used this run: %d", counter.calls)
        good = next((loc for loc in locations if loc is not None), None)
        if good is None:
            result.status = SourceStatus("cams", "failed", None, "no CAMS grid data received")
            return result
        axis = np.array(good["hourly"]["time"], dtype="int64")
        raw = np.full((len(points), len(axis)), np.nan)
        for i, loc in enumerate(locations):
            if loc is None:
                continue
            try:
                if np.array(loc["hourly"]["time"], dtype="int64").tolist() != axis.tolist():
                    continue
                raw[i] = np.array(loc["hourly"][VARIABLE], dtype=float)
            except (KeyError, TypeError, ValueError):
                continue

    t0, steps, times = _window(now)
    window = _locate(axis, t0, steps)
    ny, nx = len(lats), len(lons)
    hourly = np.full((steps, ny, nx), np.nan)
    basis = np.full((steps, ny, nx), np.nan)
    for i in range(len(points)):
        y, x = divmod(i, nx)
        v = raw[i][window]
        if len(v) == steps:
            hourly[:, y, x] = v
            basis[:, y, x] = trailing_mean(raw[i], config.PM25_AVERAGING_HOURS)[window]
    missing = int(np.isnan(basis).any(axis=0).sum())
    result.missing_points = missing
    if missing == len(points):
        result.status = SourceStatus("cams", "failed", None, "no usable CAMS grid data")
        return result
    result.grid = CamsGrid(times, lats, lons, hourly, basis)
    if missing:
        detail = f"CAMS grid: {missing} of {len(points)} points missing"
        log.warning("%s", detail)
        result.status = SourceStatus("cams", "degraded", _iso(now), detail)
    else:
        result.status = SourceStatus("cams", "ok", _iso(now))
        if cache_file and not result.from_cache:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(cache_file, axis=axis, values=raw)
            except OSError as exc:
                log.warning("CAMS grid: could not write cache (%s)", type(exc).__name__)
    return result


def grid_for_export(result: CamsGridResult) -> Pm25Grid | None:
    """The map layer for forecast.json: every CAMS_EXPORT_STEP_HOURS, quantised to integers.

    One frame every 3 h (the CAMS upstream step), values rounded to CAMS_GRID_QUANTUM_UGM3 and
    written as integers, null where missing: 25 frames x 546 points.
    """
    grid = result.grid
    if grid is None:
        return None
    step = config.CAMS_EXPORT_STEP_HOURS // config.STEP_HOURS
    q = config.CAMS_GRID_QUANTUM_UGM3
    frames = grid.basis[::step]
    hours = (grid.times[::step] - grid.times[0]) / np.timedelta64(1, "h")
    values = [None if np.isnan(v) else round(float(v) / q) for v in frames.ravel()]
    return Pm25Grid(
        lats=[float(x) for x in grid.lats],
        lons=[float(x) for x in grid.lons],
        hours=[int(h) if float(h).is_integer() else float(h) for h in hours],
        averaging_hours=config.PM25_AVERAGING_HOURS,
        quantum_ugm3=q,
        values=values,
    )


# --- Filling the zone forecasts ------------------------------------------------------------


def _point(times: np.ndarray, series: CamsSeries, k: int) -> Pm25Point:
    return Pm25Point(
        time=f"{times[k]}Z",
        value_ugm3=round(float(series.hourly[k]), 1),
        basis_ugm3=round(float(series.basis[k]), 1),
        category=pm25_category(float(series.basis[k])),
    )


def apply_to_zones(zones: list[ZoneForecast], cams: CamsResult) -> list[ZoneForecast]:
    """Copies of the zone forecasts with the CAMS level now, peak, trend, series and flag.

    cams_pm25 has a point every CAMS_EXPORT_STEP_HOURS (CAMS is 3-hourly upstream). The peak
    is taken over every hour, at the hour where the category basis is highest. The trend
    compares the mean of the next 24 hourly values with the 24 h mean at t0. Zones without
    CAMS data keep empty CAMS fields and no flag.
    """
    day = 24 // config.STEP_HOURS
    step = config.CAMS_EXPORT_STEP_HOURS // config.STEP_HOURS
    out = []
    for zone in zones:
        series = cams.series.get(zone.id)
        if series is None or cams.times is None:
            out.append(
                replace(
                    zone, cams_now=None, cams_peak=None, cams_trend=None, cams_pm25=[],
                    agreement=None,
                )
            )  # fmt: skip
            continue
        points = [_point(cams.times, series, k) for k in range(0, len(cams.times), step)]
        peak = _point(cams.times, series, int(np.argmax(series.basis)))
        out.append(
            replace(
                zone,
                cams_now=_point(cams.times, series, 0),
                cams_peak=peak,
                cams_trend=trend(float(series.basis[0]), float(series.hourly[1 : day + 1].mean())),
                cams_pm25=points,
                agreement=agreement_flag(zone.smoke_eta_hours is not None, peak.category),
            )
        )
    return out


# --- Debug command -------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch real CAMS PM2.5 (zones and map grid).")
    parser.add_argument("--no-grid", action="store_true", help="zones only")
    parser.add_argument("--no-cache", action="store_true", help="skip reading the grid cache")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    now = datetime.now(UTC)
    cams = fetch_cams(now=now)
    print(f"CAMS  now {_iso(now)}  domain {config.CAMS_DOMAIN} (~{config.CAMS_RESOLUTION_KM:g} km)")
    detail = f" ({cams.status.detail})" if cams.status.detail else ""
    print(f"zones: {cams.calls_used} calls, status {cams.status.state.upper()}{detail}")
    if not config.PM25_BREAKPOINTS_VERIFIED:
        print("WARNING: PM2.5 breakpoints not verified against the DOE document (see config.py).")
    print(f"\n{'zone':<19}{'now (24 h mean)':<26}{'72 h peak':<26}trend")
    day = 24 // config.STEP_HOURS
    for zone in config.ZONES:
        s = cams.series.get(zone.id)
        if s is None:
            print(f"{zone.name:<19}no data")
            continue
        now_txt = f"{s.basis[0]:5.1f} {pm25_category(s.basis[0]).replace('_', ' ')}"
        peak_txt = f"{s.basis.max():5.1f} {pm25_category(s.basis.max()).replace('_', ' ')}"
        tr = trend(float(s.basis[0]), float(s.hourly[1 : day + 1].mean()))
        print(f"{zone.name:<19}{now_txt:<26}{peak_txt:<26}{tr}")

    if not args.no_grid:
        grid = fetch_cams_grid(now=now, use_cache=not args.no_cache)
        source = "cache" if grid.from_cache else "API"
        print(
            f"\ngrid: {grid.calls_used} calls in {grid.batches} requests ({source}), failed "
            f"requests {grid.failed_batches}, missing points {grid.missing_points}, status "
            f"{grid.status.state.upper()}"
        )
        exported = grid_for_export(grid)
        if exported is not None:
            from hazier.model.trajectories import json_size

            g = grid.grid
            print(
                f"grid shape {g.basis.shape}, exported {len(exported.hours)} frames x "
                f"{len(exported.lats) * len(exported.lons)} points, "
                f"{json_size([exported]) / 1024:.0f} KB; 24 h mean at t0 from "
                f"{np.nanmin(g.basis[0]):.0f} to {np.nanmax(g.basis[0]):.0f} ug/m3"
            )
    per_run = 546 * 2 + len(config.ZONES)
    print(
        f"\nOpen-Meteo calls per run without cache: winds 546 + CAMS grid 546 + CAMS zones "
        f"{len(config.ZONES)} = {per_run}; at 4 runs a day {per_run * 4:,} "
        f"(limits 600/min, 5,000/h, 10,000/day)"
    )
    return 0 if cams.status.usable else 1


if __name__ == "__main__":
    sys.exit(main())
