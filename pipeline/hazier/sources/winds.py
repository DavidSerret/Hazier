"""Open-Meteo forecast API: winds at 850 hPa on a 1 degree grid, past 48 h and next 72 h.

The output is a WindField: u and v arrays shaped (time, lat, lon) plus the three axes, ready
for the trajectory model. Times are UTC and hourly. They start WIND_PAST_HOURS (48 h) before
t0, the current hour, and run HORIZON_HOURS (72 h) after it: 121 steps. The past part carries
the smoke emitted before t0 up to t0. Open-Meteo builds it from its archive of recent runs of
the same model (past_days), so it is close to an analysis rather than a forecast.

Wind components
---------------
Open-Meteo returns speed (m/s, requested with wind_speed_unit=ms) and direction in degrees,
meteorological convention: the direction the wind comes FROM, measured clockwise from north.
A wind from the north (0 degrees) blows towards the south, so with u the eastward component
and v the northward component (both m/s, positive towards east and north):

    u = -speed * sin(direction)
    v = -speed * cos(direction)

Check: from 0 deg (north) gives u = 0, v = -speed (blows south); from 270 deg (west) gives
u = +speed, v = 0 (blows east). The inverse is direction = atan2(-u, -v) mod 360.

Grid points and model cells
---------------------------
Open-Meteo answers with the model cell nearest to each requested point. The coordinates in the
response are those of the cell, not of the request, so results are matched to grid nodes by
request order, never by returned coordinates. The 1 degree grid is coarser than the model, so
each node carries the wind of the nearest 0.25 degree cell, up to about 0.15 degrees away.

Rate limits
-----------
Each location of a multi-location request counts as one call. Requests are spaced so that the
average stays below WIND_MAX_CALLS_PER_MIN. The total is logged for every run.

Cache
-----
Assembled data is cached on disk (pipeline/.cache/winds, git-ignored) per model run, so
repeated runs while developing do not call the API again. The model run time comes from the
model's metadata file on Open-Meteo. If that is unavailable nothing is cached, because stale
data could not be told apart from fresh data. Incomplete data is never cached.

Failure handling matches the other sources: fetch_winds() never raises. It returns a status
that is ok, degraded (some grid points are missing and hold NaN) or failed (no usable field).

Debug: `python -m hazier.sources.winds` runs the real fetch and prints a summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import requests

from hazier import config
from hazier.env import PIPELINE_DIR
from hazier.schema import SourceStatus

log = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"
META_URL = "https://api.open-meteo.com/data/{model}/static/meta.json"
CACHE_DIR = PIPELINE_DIR / ".cache" / "winds"

SPEED_VAR = f"wind_speed_{config.PRESSURE_LEVEL_HPA}hPa"
DIRECTION_VAR = f"wind_direction_{config.PRESSURE_LEVEL_HPA}hPa"


class WindsError(Exception):
    """A failure of one request. The message is safe to log (there is no API key)."""


class _Transient(Exception):
    """A failure worth one retry (timeout, connection error, 429, 5xx)."""


# --- Conversion ----------------------------------------------------------------------------


def speed_dir_to_uv(speed, direction_deg):
    """Meteorological speed and direction (FROM, degrees clockwise from north) to (u, v)."""
    rad = np.radians(np.asarray(direction_deg, dtype=float))
    speed = np.asarray(speed, dtype=float)
    return -speed * np.sin(rad), -speed * np.cos(rad)


def uv_to_speed_dir(u, v):
    """Inverse of speed_dir_to_uv: (speed, direction the wind comes FROM in degrees)."""
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    return np.hypot(u, v), np.degrees(np.arctan2(-u, -v)) % 360.0


# --- Data structure ------------------------------------------------------------------------


@dataclass
class WindField:
    model: str
    run_init: datetime | None  # model run initialisation time (UTC), None if unknown
    level_hpa: int
    times: np.ndarray  # datetime64[s], UTC, shape (T,)
    lats: np.ndarray  # degrees north, ascending, shape (Y,)
    lons: np.ndarray  # degrees east, ascending, shape (X,)
    u: np.ndarray  # eastward wind m/s, shape (T, Y, X), NaN where missing
    v: np.ndarray  # northward wind m/s, shape (T, Y, X), NaN where missing
    t0_index: int = 0  # index of t0, the start of the forecast; earlier times are the past

    @property
    def t0(self) -> np.datetime64:
        return self.times[self.t0_index]

    @property
    def hours(self) -> np.ndarray:
        """Hours since t0, shape (T,): negative in the past, 0 at t0."""
        return (self.times - self.times[self.t0_index]) / np.timedelta64(1, "h")

    def contains(self, lat, lon) -> np.ndarray:
        """True where (lat, lon) lies inside the grid, edges included. NaN gives False."""
        lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
        return (
            (lat >= self.lats[0])
            & (lat <= self.lats[-1])
            & (lon >= self.lons[0])
            & (lon <= self.lons[-1])
        )

    def sample(self, lat, lon, hour) -> tuple[np.ndarray, np.ndarray]:
        """(u, v) at arbitrary points and times: bilinear in space, linear in time.

        lat, lon and hour (hours since t0) broadcast against each other. The result is
        NaN where the point is outside the grid or the time range, and where any surrounding
        grid value that contributes (non-zero weight) is missing. A missing value is never
        silently replaced. A corner with zero weight is ignored, so a point exactly on a grid
        line or at a whole hour does not depend on the next row, column or hour.
        Axes must be evenly spaced, which they are for the Open-Meteo grid and hourly series.

        Done in two linear steps, which is the same as trilinear interpolation because linear
        interpolations commute: first the grid is interpolated to each requested hour (a small
        2-D slice), then each point is interpolated bilinearly on that slice. The integrator
        asks for all particles at one time, so it pays for one slice and 4 corners per point.
        """
        if np.ndim(hour) == 0:
            return self._sample_at_hour(
                np.asarray(lat, dtype=float), np.asarray(lon, dtype=float), float(hour)
            )
        lat, lon, hour = np.broadcast_arrays(
            np.asarray(lat, dtype=float), np.asarray(lon, dtype=float), np.asarray(hour, float)
        )
        u_out = np.full(lat.shape, np.nan)
        v_out = np.full(lat.shape, np.nan)
        for h in np.unique(hour[~np.isnan(hour)]):
            at_h = hour == h
            u_out[at_h], v_out[at_h] = self._sample_at_hour(lat[at_h], lon[at_h], float(h))
        return u_out, v_out

    def _slice(self, hour: float) -> tuple[np.ndarray, np.ndarray] | None:
        """The (u, v) grids linearly interpolated to one hour; None outside the time range."""
        hours = self.hours
        nt = len(hours)
        ft = (hour - hours[0]) / (hours[1] - hours[0])
        if not 0 <= ft <= nt - 1:
            return None
        t0 = min(int(ft), nt - 2)
        wt = ft - t0
        # A whole hour uses that hour alone, so a gap in the next hour does not leak in.
        if wt == 0:
            return self.u[t0], self.v[t0]
        if wt == 1:
            return self.u[t0 + 1], self.v[t0 + 1]
        return (
            (1 - wt) * self.u[t0] + wt * self.u[t0 + 1],
            (1 - wt) * self.v[t0] + wt * self.v[t0 + 1],
        )

    def _sample_at_hour(
        self, lat: np.ndarray, lon: np.ndarray, hour: float
    ) -> tuple[np.ndarray, np.ndarray]:
        """Bilinear interpolation of many points on the grid at one hour."""
        lat, lon = np.broadcast_arrays(lat, lon)
        u_out = np.full(lat.shape, np.nan)
        v_out = np.full(lat.shape, np.nan)
        grids = self._slice(hour)
        if grids is None:
            return u_out, v_out

        ny, nx = len(self.lats), len(self.lons)
        # Fractional grid indices. NaN inputs fail every comparison below and are skipped.
        fy = (lat - self.lats[0]) / (self.lats[1] - self.lats[0])
        fx = (lon - self.lons[0]) / (self.lons[1] - self.lons[0])
        ok = (fy >= 0) & (fy <= ny - 1) & (fx >= 0) & (fx <= nx - 1)
        if not ok.any():
            return u_out, v_out
        fy, fx = fy[ok], fx[ok]
        # Lower corner index; a point exactly on the last row uses the last cell with weight 1.
        y0 = np.minimum(fy.astype(int), ny - 2)
        x0 = np.minimum(fx.astype(int), nx - 2)
        wy, wx = fy - y0, fx - x0

        # Corners gathered from the flattened grids with one index shared by u and v.
        u_flat, v_flat = grids[0].ravel(), grids[1].ravel()
        base = y0 * nx + x0
        # Only a grid with gaps needs the zero-weight guard (NaN * 0 would still be NaN).
        guard = bool(np.isnan(u_flat).any() or np.isnan(v_flat).any())
        u_acc = np.zeros(fy.shape)
        v_acc = np.zeros(fy.shape)
        for dy, w_y in ((0, 1 - wy), (1, wy)):
            for dx, w_x in ((0, 1 - wx), (1, wx)):
                w = w_y * w_x
                corner = base + (dy * nx + dx)
                uc, vc = u_flat.take(corner), v_flat.take(corner)  # copies
                if guard:
                    unused = w == 0
                    uc[unused] = 0.0
                    vc[unused] = 0.0
                u_acc += uc * w
                v_acc += vc * w
        u_out[ok] = u_acc
        v_out[ok] = v_acc
        return u_out, v_out

    def at(self, lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
        """(u, v) time series at a point, bilinear in space. NaN if a corner is missing."""
        if not self.contains(lat, lon):
            raise ValueError(f"({lat}, {lon}) is outside the wind grid")
        hours = self.hours
        return self.sample(np.full(hours.shape, lat), np.full(hours.shape, lon), hours)


@dataclass
class WindsResult:
    field: WindField | None
    status: SourceStatus
    calls_used: int = 0  # Open-Meteo calls spent this run, retries included
    batches: int = 0
    failed_batches: int = 0
    missing_points: int = 0  # grid nodes with any NaN in the output window
    from_cache: bool = False


# --- Grid and time axis --------------------------------------------------------------------


def grid_axes() -> tuple[np.ndarray, np.ndarray]:
    """Latitude and longitude axes of the wind grid, ascending."""
    step = config.GRID_STEP_DEG
    lats = np.arange(config.LAT_MIN, config.LAT_MAX + step / 2, step)
    lons = np.arange(config.LON_MIN, config.LON_MAX + step / 2, step)
    return lats, lons


def _floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x: float) -> str:
    return format(float(x), "g")


# --- HTTP ----------------------------------------------------------------------------------


class _CallCounter:
    def __init__(self) -> None:
        self.calls = 0


def _request(params: dict, n_locations: int, counter: _CallCounter) -> list[dict]:
    counter.calls += n_locations  # counted even if the request then fails
    try:
        resp = requests.get(API_URL, params=params, timeout=config.WIND_TIMEOUT_S)
    except requests.RequestException as exc:
        raise _Transient(type(exc).__name__) from None
    status = resp.status_code
    if status == 429 or status >= 500:
        raise _Transient(f"HTTP {status}")
    try:
        data = resp.json()
    except ValueError:
        raise WindsError(f"HTTP {status}: response is not JSON") from None
    if isinstance(data, dict) and data.get("error"):
        raise WindsError(f"HTTP {status}: {str(data.get('reason', 'unknown error'))[:150]}")
    if status != 200:
        raise WindsError(f"HTTP {status}")
    locations = [data] if isinstance(data, dict) else data
    if not isinstance(locations, list) or len(locations) != n_locations:
        raise WindsError(f"expected {n_locations} locations, got an unexpected response")
    return locations


def _fetch_batch(points: list[tuple[float, float]], counter: _CallCounter) -> list[dict]:
    params = {
        "latitude": ",".join(_num(lat) for lat, _ in points),
        "longitude": ",".join(_num(lon) for _, lon in points),
        "hourly": f"{SPEED_VAR},{DIRECTION_VAR}",
        "wind_speed_unit": "ms",
        "models": config.WIND_MODEL,
        "forecast_days": config.WIND_FORECAST_DAYS,
        "past_days": config.WIND_PAST_DAYS,
        "timezone": "GMT",
        "timeformat": "unixtime",
    }
    for attempt in range(config.WIND_RETRIES + 1):
        try:
            return _request(params, len(points), counter)
        except _Transient as exc:
            if attempt >= config.WIND_RETRIES:
                raise WindsError(f"{exc} (gave up after {attempt + 1} attempts)") from None
            log.warning("Winds: %s, retrying in %.0f s", exc, config.WIND_BACKOFF_S)
            time.sleep(config.WIND_BACKOFF_S * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


def _fetch_run_init() -> datetime | None:
    """Initialisation time of the latest model run, from the model's metadata file."""
    try:
        resp = requests.get(META_URL.format(model=config.WIND_MODEL), timeout=15)
        resp.raise_for_status()
        return datetime.fromtimestamp(int(resp.json()["last_run_initialisation_time"]), UTC)
    except Exception as exc:  # noqa: BLE001 - metadata is optional, only caching depends on it
        log.warning("Winds: model run time unavailable (%s), caching disabled", type(exc).__name__)
        return None


# --- Assembly ------------------------------------------------------------------------------


def _assemble(
    points: list[tuple[float, float]],
    locations: list[dict | None],
    shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Build (axis, u, v) with shape (T, Y, X) from per-point responses. None if all are missing.

    Points are in row-major order (latitude outer, longitude inner), the order they were
    requested in. A point that is missing or malformed stays NaN.
    """
    ny, nx = shape
    axis: list[int] | None = None
    for loc in locations:
        if loc is not None:
            axis = loc["hourly"]["time"]
            break
    if axis is None:
        return None

    u = np.full((len(axis), ny, nx), np.nan)
    v = np.full((len(axis), ny, nx), np.nan)
    for i, loc in enumerate(locations):
        if loc is None:
            continue
        try:
            hourly = loc["hourly"]
            if hourly["time"] != axis:
                raise ValueError("time axis differs")
            # None (JSON null) becomes NaN when converting to float.
            speed = np.array(hourly[SPEED_VAR], dtype=float)
            direction = np.array(hourly[DIRECTION_VAR], dtype=float)
            if speed.shape != (len(axis),) or direction.shape != (len(axis),):
                raise ValueError("series length differs")
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Winds: point %s unusable (%s)", points[i], type(exc).__name__)
            continue
        y, x = divmod(i, nx)
        u[:, y, x], v[:, y, x] = speed_dir_to_uv(speed, direction)
    return np.array(axis, dtype="int64"), u, v


def _window(
    axis: np.ndarray, u: np.ndarray, v: np.ndarray, now: datetime
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Cut the hourly series to [t0 - past, t0 + horizon], padding the end with NaN.

    t0 is the current hour. Returns times, u, v and the index of t0.
    """
    t0 = int(_floor_hour(now).timestamp())
    past_steps = config.WIND_PAST_HOURS // config.STEP_HOURS
    start = t0 - config.WIND_PAST_HOURS * 3600
    hits = np.flatnonzero(axis == start)
    if hits.size == 0 or not (axis == t0).any():
        raise WindsError("the time axis does not contain the current hour and the past 48 h")
    steps = past_steps + config.HORIZON_HOURS // config.STEP_HOURS + 1
    first = int(hits[0])
    sel = slice(first, first + steps)
    times = start + np.arange(steps, dtype="int64") * config.STEP_HOURS * 3600
    uw = np.full((steps,) + u.shape[1:], np.nan)
    vw = np.full_like(uw, np.nan)
    got = u[sel].shape[0]
    uw[:got], vw[:got] = u[sel], v[sel]
    return times.astype("datetime64[s]"), uw, vw, past_steps


# --- Cache ---------------------------------------------------------------------------------


def _cache_path(cache_dir: Path, run_init: datetime, now: datetime) -> Path:
    lats, lons = grid_axes()
    spec = json.dumps(
        [
            lats.tolist(),
            lons.tolist(),
            config.PRESSURE_LEVEL_HPA,
            config.WIND_FORECAST_DAYS,
            config.WIND_PAST_DAYS,
        ]
    )
    digest = hashlib.sha1(spec.encode()).hexdigest()[:8]
    # The hourly axis starts at 00:00 UTC of the request day, so the day is part of the key.
    name = f"{config.WIND_MODEL}_{run_init:%Y%m%dT%H%M}_{now:%Y%m%d}_{digest}.npz"
    return cache_dir / name


def _load_cache(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    try:
        with np.load(path) as z:
            return z["axis"], z["u"], z["v"]
    except (OSError, KeyError, ValueError):
        return None


def _save_cache(path: Path, axis: np.ndarray, u: np.ndarray, v: np.ndarray) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, axis=axis, u=u, v=v)
    except OSError as exc:
        log.warning("Winds: could not write cache (%s)", type(exc).__name__)


# --- Public entry point --------------------------------------------------------------------


def fetch_winds(
    now: datetime | None = None,
    use_cache: bool = True,
    cache_dir: Path | None = None,
) -> WindsResult:
    """Fetch the 850 hPa wind field for [now, now + 72 h]. Never raises."""
    now = now or datetime.now(UTC)
    try:
        return _fetch_winds(now, use_cache, cache_dir or CACHE_DIR)
    except WindsError as exc:
        log.error("Winds failed: %s", exc)
        return WindsResult(None, SourceStatus("winds", "failed", None, str(exc)[:200]))
    except Exception as exc:  # noqa: BLE001 - the pipeline must never crash on this source
        log.error("Winds failed: unexpected %s", type(exc).__name__)
        return WindsResult(None, SourceStatus("winds", "failed", None, "unexpected error"))


def _fetch_winds(now: datetime, use_cache: bool, cache_dir: Path) -> WindsResult:
    lats, lons = grid_axes()
    shape = (len(lats), len(lons))
    points = [(float(lat), float(lon)) for lat in lats for lon in lons]

    run_init = _fetch_run_init()
    cache_file = _cache_path(cache_dir, run_init, now) if run_init else None

    cached = _load_cache(cache_file) if (use_cache and cache_file) else None
    result = WindsResult(None, SourceStatus("winds", "failed", None))
    counter = _CallCounter()
    if cached is not None:
        log.info("Winds: using cache %s (0 API calls)", cache_file.name)
        axis, u, v = cached
        result.from_cache = True
    else:
        locations: list[dict | None] = []
        last_start, last_size = None, 0
        for lo in range(0, len(points), config.WIND_BATCH_SIZE):
            batch = points[lo : lo + config.WIND_BATCH_SIZE]
            if last_start is not None:  # keep the average under WIND_MAX_CALLS_PER_MIN
                wait = last_size / config.WIND_MAX_CALLS_PER_MIN * 60 - (
                    time.monotonic() - last_start
                )
                if wait > 0:
                    time.sleep(wait)
            last_start, last_size = time.monotonic(), len(batch)
            result.batches += 1
            try:
                locations.extend(_fetch_batch(batch, counter))
            except WindsError as exc:
                log.error("Winds: batch %d failed: %s", result.batches, exc)
                result.failed_batches += 1
                locations.extend([None] * len(batch))
        result.calls_used = counter.calls
        log.info("Winds: Open-Meteo calls used this run: %d", counter.calls)

        assembled = _assemble(points, locations, shape)
        if assembled is None:
            result.status = SourceStatus("winds", "failed", None, "no wind data received")
            return result
        axis, u, v = assembled

    times, uw, vw, t0_index = _window(axis, u, v, now)
    missing = int(np.isnan(uw).any(axis=0).sum())
    total = len(points)
    result.missing_points = missing

    if missing == total:
        result.status = SourceStatus("winds", "failed", None, "no usable wind data")
        return result

    result.field = WindField(
        model=config.WIND_MODEL,
        run_init=run_init,
        level_hpa=config.PRESSURE_LEVEL_HPA,
        times=times,
        lats=lats,
        lons=lons,
        u=uw,
        v=vw,
        t0_index=t0_index,
    )
    if missing:
        detail = f"{missing} of {total} grid points missing"
        log.warning("Winds: degraded, %s", detail)
        result.status = SourceStatus("winds", "degraded", _iso(now), detail)
    else:
        result.status = SourceStatus("winds", "ok", _iso(now))
        if cache_file and not result.from_cache:
            _save_cache(cache_file, axis, u, v)  # only complete data is cached
    return result


# --- Debug command -------------------------------------------------------------------------

_DEBUG_POINTS = (
    ("Kuala Lumpur", 3.14, 101.69),
    ("Riau (central)", 0.5, 101.5),
)


def _report(result: WindsResult, now: datetime) -> None:
    print(f"Winds fetch  now {_iso(now)}")
    f = result.field
    if f is not None:
        run = _iso(f.run_init) if f.run_init else "unknown"
        print(f"model: {f.model}  run: {run}  level: {f.level_hpa} hPa")
        t, y, x = f.u.shape
        print(f"grid: {y} lat x {x} lon = {y * x} points, {t} hourly steps")
        print(f"lat {f.lats[0]:g} to {f.lats[-1]:g}, lon {f.lons[0]:g} to {f.lons[-1]:g}")
        print(f"time (UTC): {f.times[0]} to {f.times[-1]}, t0 {f.t0} (index {f.t0_index})")
    source = "cache" if result.from_cache else "API"
    print(
        f"API calls this run: {result.calls_used} in {result.batches} requests ({source}), "
        f"failed requests: {result.failed_batches}, missing grid points: {result.missing_points}"
    )
    detail = f"  ({result.status.detail})" if result.status.detail else ""
    print(f"status: {result.status.state.upper()}{detail}")
    if f is None:
        return
    for name, lat, lon in _DEBUG_POINTS:
        u, v = f.at(lat, lon)
        speed, direction = uv_to_speed_dir(u, v)
        print(f"\n{name} ({lat:g}N, {lon:g}E), next 24 h, bilinear from the 1 degree grid")
        print(f"{'UTC':<17}{'MYT':<7}{'speed m/s':>10}{'from deg':>10}{'u':>8}{'v':>8}")
        for i in range(f.t0_index, f.t0_index + 25, 3):
            utc = f.times[i].astype("datetime64[m]").astype(datetime)
            myt = utc + timedelta(hours=8)
            print(
                f"{utc:%Y-%m-%d %H:%M}  {myt:%H:%M} "
                f"{speed[i]:>10.1f}{direction[i]:>10.0f}{u[i]:>8.1f}{v[i]:>8.1f}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the real 850 hPa wind field and print a summary."
    )
    parser.add_argument("--no-cache", action="store_true", help="skip reading the disk cache")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = datetime.now(UTC)
    result = fetch_winds(now=now, use_cache=not args.no_cache)
    _report(result, now)
    return 0 if result.status.usable else 1


if __name__ == "__main__":
    sys.exit(main())
