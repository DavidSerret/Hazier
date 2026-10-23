"""Smoke trajectories: hotspot clustering, particle release, advection and dispersion.

This is the physics core. It answers one question: if the fires seen in the last 48 h keep
burning, where does their smoke travel over the next 72 h? Risk, ETA and attribution per zone
are computed in smoke.py from what this module produces.

Time runs from 48 h before t0 (the current hour) to 72 h after it. Smoke emitted since each
fire was first seen is carried with the past winds up to t0, so the smoke already in the air
at t0 is part of the forecast; from t0 on the fires are assumed to keep burning.

Steps
-----
1. cluster_hotspots: bin the hotspots on a 0.25 degree grid, one cluster per occupied cell.
2. release_particles: every cluster releases 20 particles every hour, from its first detection
   in the last 48 h until the end of the forecast (continuous emission).
3. advect: move every particle with the 850 hPa wind (Heun, RK2) plus a random walk for
   horizontal dispersion. Observers gather what later steps need while the particles move,
   so the full path of every particle is never stored. Particles go in batches so memory
   stays flat.
4. select_for_export: a light subset of display trajectories for forecast.json.

Equations of motion
-------------------
A particle moves with the wind. In longitude (lambda) and latitude (phi), on a sphere of
radius R, with u and v the eastward and northward wind in m/s:

    d(phi)/dt    = v / R
    d(lambda)/dt = u / (R cos(phi))

(angles in radians). See degrees_per_hour for the derivation.

Integrator: Heun (RK2 predictor-corrector)
------------------------------------------
    predictor:  x* = x + dt f(x, t)
    corrector:  x' = x + dt/2 [f(x, t) + f(x*, t + dt)]

Heun and the midpoint method are both second order (halving dt cuts the error about 4 times,
which the tests check). Heun is chosen because:
- it is the scheme HYSPLIT, the reference trajectory model, uses, so results are comparable;
- with dt = 1 h it samples the wind only at whole hours, the times of the data, while the
  midpoint method needs winds at half hours, which would be pure time interpolation.

Winds are interpolated bilinearly in space and linearly in time (WindField.sample).

Dispersion: random walk
-----------------------
The 1 degree winds miss the eddies that spread a plume sideways. They are represented as
horizontal diffusion with a constant diffusivity K: after each Heun step every particle gets
an independent random displacement in x and y, each drawn from a normal distribution with
standard deviation

    sigma = sqrt(2 K dt)

which makes the spread of a puff grow as sqrt(2 K t), the solution of the diffusion equation.
K = 2e4 m2/s by default. Richardson's law (diffusivity growing as the 4/3 power of the eddy
size, K ~ 0.1 eps^(1/3) L^(4/3)) gives about 2e4 m2/s for eddies the size of the grid
(L ~ 100 km) with a free-troposphere dissipation rate eps ~ 1e-4 m2/s3. It is an order of
magnitude estimate: the resulting spread is about 60 km after 24 h and 100 km after 72 h,
comparable to the zone size and the grid spacing. It should be tuned against observations
in phase 2.

The random numbers come from one generator seeded with config.RANDOM_SEED, so the same
inputs always give the same forecast. With diffusivity 0 the model is fully deterministic,
which is how the integrator itself is tested.

Stopping rules
--------------
A particle stops, keeping its last good position, when:
- it leaves the domain (the predictor, the corrected or the dispersed position falls outside
  the wind grid), because there is no wind to move it further;
- it meets missing wind (NaN in a degraded run). It is stopped and counted, never moved with
  a made-up wind, so a NaN can not end up in a position.
Otherwise it runs to the end of the horizon.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, replace
from enum import IntEnum
from typing import Protocol

import numpy as np

from hazier import config
from hazier.geo import EARTH_RADIUS_M, great_circle_km
from hazier.schema import Hotspot, SourceCluster, Trajectory
from hazier.sources.winds import WindField

log = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600.0


# --- 1. Clustering -------------------------------------------------------------------------


@dataclass
class Clustering:
    clusters: list[SourceCluster]  # kept clusters, strongest first, id == list index
    cells: int  # occupied grid cells before the FRP filter
    dropped: int  # clusters below the minimum FRP
    dropped_frp_share: float  # share of the total FRP that was dropped


def cluster_hotspots(
    hotspots: list[Hotspot],
    cell_deg: float | None = None,
    min_frp_mw: float | None = None,
) -> Clustering:
    """Group hotspots into grid-cell clusters.

    A hotspot goes to cell (floor((lat - LAT_MIN) / cell), floor((lon - LON_MIN) / cell)). The
    binning is deterministic: the same hotspots give the same clusters in any order. A fire
    complex cut by a cell edge becomes two clusters, which is harmless at this scale: a cell is
    about 28 km and the wind moves smoke 10 to 30 km every hour.

    Each cluster keeps:
    - its FRP-weighted centroid, so smoke is released where the burning is strongest rather
      than at the middle of scattered pixels;
    - frp_mw, the summed FRP of every detection in the last 48 h. A fire seen on several
      overpasses counts each time, so this mixes intensity and persistence. It is used as a
      proxy for emission strength (see release_particles), not as a physical emission rate;
    - hotspot_count, and first_at and latest_at, the times of the earliest and the most recent
      detection.

    Clusters below min_frp_mw are dropped. The rest are ranked by frp_mw (strongest first,
    ties broken by cell) and numbered in that order.
    """
    cell = config.CLUSTER_CELL_DEG if cell_deg is None else cell_deg
    min_frp = config.CLUSTER_MIN_FRP_MW if min_frp_mw is None else min_frp_mw
    if not hotspots:
        return Clustering([], 0, 0, 0.0)

    lat = np.array([h.lat for h in hotspots])
    lon = np.array([h.lon for h in hotspots])
    frp = np.array([h.frp_mw for h in hotspots])
    acquired = np.array([h.acquired_at.rstrip("Z") for h in hotspots], dtype="datetime64[s]")

    rows = np.floor((lat - config.LAT_MIN) / cell).astype(int)
    cols = np.floor((lon - config.LON_MIN) / cell).astype(int)
    keys, member = np.unique(np.stack([rows, cols], axis=1), axis=0, return_inverse=True)
    member = member.ravel()
    n = len(keys)

    frp_sum = np.bincount(member, weights=frp, minlength=n)
    count = np.bincount(member, minlength=n)
    # FRP-weighted centroid; a cell whose FRP is all zero falls back to the plain mean.
    w = np.where(frp_sum[member] > 0, frp, 1.0)
    w_sum = np.bincount(member, weights=w, minlength=n)
    c_lat = np.bincount(member, weights=w * lat, minlength=n) / w_sum
    c_lon = np.bincount(member, weights=w * lon, minlength=n) / w_sum
    latest = np.full(n, np.iinfo(np.int64).min)
    np.maximum.at(latest, member, acquired.astype(np.int64))
    earliest = np.full(n, np.iinfo(np.int64).max)
    np.minimum.at(earliest, member, acquired.astype(np.int64))

    keep = frp_sum >= min_frp
    order = np.lexsort((keys[:, 1], keys[:, 0], -frp_sum))  # last key is the primary one
    order = order[keep[order]]
    clusters = [
        SourceCluster(
            id=i,
            lat=float(c_lat[k]),
            lon=float(c_lon[k]),
            frp_mw=float(frp_sum[k]),
            hotspot_count=int(count[k]),
            latest_at=f"{np.datetime64(int(latest[k]), 's')}Z",
            first_at=f"{np.datetime64(int(earliest[k]), 's')}Z",
        )
        for i, k in enumerate(order)
    ]
    total = frp_sum.sum()
    dropped_share = float(frp_sum[~keep].sum() / total) if total > 0 else 0.0
    return Clustering(clusters, n, int((~keep).sum()), dropped_share)


# --- 2. Release ----------------------------------------------------------------------------


@dataclass
class Particles:
    cluster_index: np.ndarray  # (P,) index into the cluster list, equal to the cluster id
    release_hour: np.ndarray  # (P,) hours since t0
    lat0: np.ndarray  # (P,) release position: the cluster centroid
    lon0: np.ndarray  # (P,)
    weight: np.ndarray  # (P,) smoke carried, in emission units (see release_particles)

    def __len__(self) -> int:
        return len(self.weight)


def release_particles(
    clusters: list[SourceCluster],
    particles_per_release: int | None = None,
    interval_hours: int | None = None,
    horizon_hours: int | None = None,
    t0: np.datetime64 | None = None,
    earliest_hour: float | None = None,
) -> Particles:
    """Particles released by every cluster every interval_hours until the horizon.

    Fires emit continuously; frequent releases turn that into a train of puffs. Nothing is
    released at the horizon itself because it would have no time to move. Each release holds
    particles_per_release particles that start together at the cluster centroid and spread
    apart through dispersion.

    When releases start. Without t0, every cluster starts at t0 (hour 0). With t0 (the forecast
    start, as a UTC datetime64), each cluster starts at the first whole interval at or after its
    earliest detection (first_at): the fire is taken to have been burning since it was first
    seen in the lookback, so its smoke is already in the air at t0. A cluster first seen
    between t0 and now still starts at t0. Releases never start before earliest_hour (default
    -LOOKBACK_HOURS; pass the start of the wind field), and a cluster without first_at starts
    at t0. With the defaults a cluster seen 30 h before t0 releases at -30, -29, ..., 71.

    Weights. Under the persistent-fire assumption each cluster emits at a constant rate,
    proportional to its summed FRP S over the 48 h lookback. That rate is taken as S / 48 h,
    and each release carries interval_hours of it, split equally among its particles:

        weight = S / LOOKBACK_HOURS * interval_hours / particles_per_release

    The same rate applies before and after t0. The unit (an "emission unit") is the smoke
    emitted in one hour by a source averaging 1 MW of detected FRP per hour of observation.
    Absolute rather than relative weights matter: the smoke index of a zone must grow with the
    size of the fires, not with their share of the day's total, or a single small fire on a
    quiet day would look like an episode.

    Particles are ordered by cluster (strongest first), then release hour.
    """
    n_per = config.PARTICLES_PER_RELEASE if particles_per_release is None else particles_per_release
    interval = config.RELEASE_INTERVAL_HOURS if interval_hours is None else interval_hours
    horizon = config.HORIZON_HOURS if horizon_hours is None else horizon_hours
    floor_hour = -config.FIRMS_LOOKBACK_HOURS if earliest_hour is None else earliest_hour
    first_allowed = math.ceil(floor_hour / interval) * interval

    starts = []
    for c in clusters:
        start = 0
        if t0 is not None and c.first_at:
            seen = np.datetime64(c.first_at.rstrip("Z"), "s") - np.datetime64(t0, "s")
            offset = float(seen / np.timedelta64(1, "h"))
            start = min(math.ceil(offset / interval) * interval, 0)
        starts.append(max(start, first_allowed))
    release_hours = [np.arange(s0, horizon, interval, dtype=float) for s0 in starts]
    counts = np.array([len(h) for h in release_hours], dtype=int)

    strength = np.array([c.frp_mw for c in clusters], dtype=float)
    per_particle = strength / config.FIRMS_LOOKBACK_HOURS * interval / n_per
    per_release_cluster = np.repeat(np.arange(len(clusters)), counts)
    cluster_index = np.repeat(per_release_cluster, n_per)
    hours = np.concatenate(release_hours) if release_hours else np.array([], dtype=float)
    return Particles(
        cluster_index=cluster_index,
        release_hour=np.repeat(hours, n_per),
        lat0=np.array([c.lat for c in clusters], dtype=float)[cluster_index],
        lon0=np.array([c.lon for c in clusters], dtype=float)[cluster_index],
        weight=per_particle[cluster_index],
    )


# --- 3. Advection --------------------------------------------------------------------------


class Fate(IntEnum):
    HORIZON = 0  # ran to the end of the forecast window
    LEFT_DOMAIN = 1  # crossed the edge of the wind grid
    MISSING_WIND = 2  # met a missing (NaN) wind value in a degraded run


_MOVED = -1  # internal status: the particle advanced normally


class Observer(Protocol):
    """Receives particle positions during advection.

    start is called once before the first step. observe is called at time step k with the
    indices of particles that have a position at hours[k] and those positions; with batching
    it can be called several times for the same k, each time with different particles, so
    observers must add up rather than overwrite totals. hours are counted from t0 and are
    negative before it. Values passed to observe are only valid during the call.
    """

    def start(self, particles: Particles, hours: np.ndarray) -> None: ...

    def observe(self, k: int, idx: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> None: ...


@dataclass
class Advection:
    """What happened to every particle; the positions went to the observers."""

    hours: np.ndarray  # (K,) hours since t0, negative before it
    times: np.ndarray  # (K,) datetime64[s], UTC
    release_step: np.ndarray  # (P,) index of the first position
    end_step: np.ndarray  # (P,) index of the last position
    fate: np.ndarray  # (P,) Fate values
    t0_index: int  # index of t0 (hour 0) in hours

    def fate_counts(self) -> dict[str, int]:
        return {f.name.lower(): int((self.fate == f).sum()) for f in Fate}

    @property
    def forecast_hours(self) -> np.ndarray:
        return self.hours[self.t0_index :]


def degrees_per_hour(u, v, lat) -> tuple[np.ndarray, np.ndarray]:
    """Convert wind (m/s) to the rate of change of position, in degrees per hour.

    Moving v metres north on a sphere of radius R changes latitude by v / R radians. A circle
    of latitude has radius R cos(lat), so moving u metres east changes longitude by
    u / (R cos(lat)) radians. Over dt seconds:

        dlat = v * dt / R
        dlon = u * dt / (R * cos(lat))

    in radians, converted to degrees here, with dt = 3600 s. At the equator 10 m/s is about
    0.32 degrees per hour. The domain stays within 12 degrees of the equator, so
    cos(lat) >= 0.97 and the 1 / cos(lat) term never blows up.

    Returns (dlon, dlat).
    """
    k = SECONDS_PER_HOUR / EARTH_RADIUS_M
    lat = np.asarray(lat, dtype=float)
    dlat = np.degrees(np.asarray(v, dtype=float) * k)
    dlon = np.degrees(np.asarray(u, dtype=float) * k / np.cos(np.radians(lat)))
    return dlon, dlat


def _step(
    field: WindField,
    lat: np.ndarray,
    lon: np.ndarray,
    t: float,
    dt: float,
    sigma_m: float,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One Heun step plus the dispersion random walk, for many particles at time t (hours).

    Returns the new positions and a status per particle: _MOVED, or the Fate that stops it.
    New positions are meaningful only where the status is _MOVED. The caller stores nothing
    else, so NaN produced along the way for stopped particles never reaches a position.
    """
    u1, v1 = field.sample(lat, lon, t)
    dlon1, dlat1 = degrees_per_hour(u1, v1, lat)
    lat_p = lat + dt * dlat1
    lon_p = lon + dt * dlon1

    u2, v2 = field.sample(lat_p, lon_p, t + dt)
    dlon2, dlat2 = degrees_per_hour(u2, v2, lat_p)
    lat_n = lat + 0.5 * dt * (dlat1 + dlat2)
    lon_n = lon + 0.5 * dt * (dlon1 + dlon2)

    if sigma_m > 0:
        # Independent normal displacements in metres, converted to degrees at the new position.
        xi = rng.standard_normal((2, lat.size))
        lon_scale = np.cos(np.radians(lat_n))
        lat_n = lat_n + np.degrees(sigma_m * xi[1] / EARTH_RADIUS_M)
        lon_n = lon_n + np.degrees(sigma_m * xi[0] / (EARTH_RADIUS_M * lon_scale))

    # Checked in order; the first reason that applies is the one recorded.
    outside_here = ~field.contains(lat, lon)
    missing_here = ~outside_here & (np.isnan(u1) | np.isnan(v1))
    stopped = outside_here | missing_here
    left_pred = ~stopped & ~field.contains(lat_p, lon_p)
    stopped |= left_pred
    missing_pred = ~stopped & (np.isnan(u2) | np.isnan(v2))
    stopped |= missing_pred
    left_new = ~stopped & ~field.contains(lat_n, lon_n)

    status = np.full(lat.shape, _MOVED, dtype=np.int8)
    status[missing_here | missing_pred] = Fate.MISSING_WIND
    status[outside_here | left_pred | left_new] = Fate.LEFT_DOMAIN
    return lat_n, lon_n, status


def advect(
    field: WindField,
    particles: Particles,
    observers: tuple[Observer, ...] | list[Observer] = (),
    dt_hours: float | None = None,
    diffusivity_m2s: float | None = None,
    seed: int | None = None,
    batch_size: int | None = None,
) -> Advection:
    """Move all particles forward on the wind field, vectorised over particles.

    The time axis covers the whole field, from its first time (48 h before t0 for the
    operational field, so smoke emitted before t0 is carried up to t0) to its last (t0 + 72 h),
    in steps of dt_hours; hours are counted from t0. At step k the particles released at that
    hour join in, the observers see every particle that has a position at hours[k], then every
    active particle takes one step to hours[k + 1]. The loop is over time steps only.

    Only the current position of each particle is kept, so memory grows with the number of
    particles, not particles x steps; observers decide what else to keep. Particles go in
    batches of at most batch_size (ADVECT_BATCH_PARTICLES), which bounds memory whatever the
    number of fires. Each batch has its own random stream derived from the seed and the batch
    number, so a run is reproducible for a given batch size. Observers receive global particle
    indices and do not need to know about batches.

    dt_hours defaults to STEP_HOURS (1 h) and diffusivity_m2s to HORIZONTAL_DIFFUSIVITY_M2S.
    Both are parameters so the tests can halve dt and switch dispersion off.
    """
    dt = float(config.STEP_HOURS if dt_hours is None else dt_hours)
    k_h = config.HORIZONTAL_DIFFUSIVITY_M2S if diffusivity_m2s is None else diffusivity_m2s
    if k_h < 0:
        raise ValueError("diffusivity must be >= 0")
    sigma_m = float(np.sqrt(2 * k_h * dt * SECONDS_PER_HOUR))
    base_seed = config.RANDOM_SEED if seed is None else seed
    size = config.ADVECT_BATCH_PARTICLES if batch_size is None else batch_size
    if size < 1:
        raise ValueError("batch_size must be >= 1")

    field_hours = field.hours
    start, end = float(field_hours[0]), float(field_hours[-1])
    n_steps = round((end - start) / dt)
    if not np.isclose(start + n_steps * dt, end) or not np.isclose(start / dt, round(start / dt)):
        raise ValueError(f"dt = {dt} h does not divide the field's time axis")
    hours = start + np.arange(n_steps + 1) * dt
    release_step = np.rint((particles.release_hour - start) / dt).astype(int)
    if not np.allclose(start + release_step * dt, particles.release_hour):
        raise ValueError("release hours must be multiples of dt")
    if (release_step < 0).any() or (release_step > n_steps).any():
        raise ValueError("release hours must lie within the wind field")

    for obs in observers:
        obs.start(particles, hours)
    fate = np.full(len(particles), Fate.HORIZON, dtype=np.int8)
    end_step = release_step.copy()
    for b, lo in enumerate(range(0, len(particles), size)):
        hi = min(lo + size, len(particles))
        rng = np.random.default_rng([base_seed, b])
        _advect_batch(field, particles, lo, hi, release_step, hours, dt, sigma_m, rng,
                      observers, fate, end_step)  # fmt: skip

    times = field.t0 + np.rint(hours * SECONDS_PER_HOUR).astype("timedelta64[s]")
    return Advection(hours, times, release_step, end_step, fate, round(-start / dt))


def _advect_batch(
    field: WindField,
    particles: Particles,
    lo: int,
    hi: int,
    release_step: np.ndarray,
    hours: np.ndarray,
    dt: float,
    sigma_m: float,
    rng: np.random.Generator,
    observers: tuple[Observer, ...] | list[Observer],
    fate: np.ndarray,
    end_step: np.ndarray,
) -> None:
    """The time loop for particles lo .. hi - 1. Writes their fate and end_step in place."""
    n_steps = len(hours) - 1
    release = release_step[lo:hi]
    if release.size == 0:
        return
    lat = np.full(hi - lo, np.nan)
    lon = np.full(hi - lo, np.nan)
    active = np.zeros(hi - lo, dtype=bool)
    # Particles grouped by release step, so each step finds its new particles without a scan.
    by_release = np.argsort(release, kind="stable")
    bounds = np.searchsorted(release[by_release], np.arange(n_steps + 2))

    for k in range(int(release.min()), n_steps + 1):
        new = by_release[bounds[k] : bounds[k + 1]]
        lat[new] = particles.lat0[lo + new]
        lon[new] = particles.lon0[lo + new]
        active[new] = True
        idx = np.flatnonzero(active)
        if idx.size == 0:
            continue
        for obs in observers:
            obs.observe(k, idx + lo, lat[idx], lon[idx])
        if k == n_steps:
            continue
        lat_n, lon_n, status = _step(field, lat[idx], lon[idx], hours[k], dt, sigma_m, rng)
        moved = status == _MOVED
        lat[idx[moved]] = lat_n[moved]
        lon[idx[moved]] = lon_n[moved]
        end_step[lo + idx[moved]] = k + 1
        halted = idx[~moved]
        fate[lo + halted] = status[~moved]
        active[halted] = False


# --- Observers -----------------------------------------------------------------------------


class PathRecorder:
    """Observer that keeps every position. Memory grows as particles x steps, so it is meant
    for small sets: the tests and the display tracers.

    min_hour limits the record to hours >= min_hour (the display tracers keep only the
    forecast, hour 0 on); dtype float32 halves the memory where 1e-5 degrees is precise enough.
    """

    lat: np.ndarray
    lon: np.ndarray
    k_min: int  # first recorded time step

    def __init__(self, min_hour: float | None = None, dtype: type = np.float64) -> None:
        self.min_hour = min_hour
        self.dtype = dtype

    def start(self, particles: Particles, hours: np.ndarray) -> None:
        if self.min_hour is None:
            self.k_min = 0
        else:
            self.k_min = int(np.searchsorted(hours, self.min_hour - 1e-9))
        shape = (len(particles), len(hours) - self.k_min)
        self.lat = np.full(shape, np.nan, dtype=self.dtype)
        self.lon = np.full(shape, np.nan, dtype=self.dtype)

    def observe(self, k: int, idx: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> None:
        if k >= self.k_min:
            self.lat[idx, k - self.k_min] = lat
            self.lon[idx, k - self.k_min] = lon


def removal_factor(age_hours, lifetime_hours: float | None = None) -> np.ndarray:
    """Share of a particle's smoke still airborne after age_hours: exp(-age / lifetime).

    First-order removal, the same for every particle whatever the weather; the lifetime and its
    justification are in smoke.py. lifetime_hours None uses REMOVAL_LIFETIME_HOURS, inf disables
    removal. The particles themselves keep moving; only the smoke they carry shrinks.
    """
    tau = config.REMOVAL_LIFETIME_HOURS if lifetime_hours is None else lifetime_hours
    return np.exp(-np.asarray(age_hours, dtype=float) / tau)


class DensityGrid:
    """Observer: time-integrated smoke on a regular grid, for maps.

    load[row, col] sums weight x removal x hours of every particle in the cell, over the
    forecast window (hours >= 0; the past only carries the smoke up to t0).
    """

    def __init__(self, cell_deg: float = 0.1, lifetime_hours: float | None = None) -> None:
        self.cell = cell_deg
        self.lifetime_hours = lifetime_hours
        self.ny = int(np.ceil((config.LAT_MAX - config.LAT_MIN) / cell_deg))
        self.nx = int(np.ceil((config.LON_MAX - config.LON_MIN) / cell_deg))
        self.load = np.zeros((self.ny, self.nx))

    def start(self, particles: Particles, hours: np.ndarray) -> None:
        self._weight = particles.weight
        self._release = particles.release_hour
        self._hours = hours
        self._dt = float(hours[1] - hours[0]) if len(hours) > 1 else 1.0

    def observe(self, k: int, idx: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> None:
        if idx.size == 0 or self._hours[k] < 0:
            return
        row = np.clip(((lat - config.LAT_MIN) / self.cell).astype(int), 0, self.ny - 1)
        col = np.clip(((lon - config.LON_MIN) / self.cell).astype(int), 0, self.nx - 1)
        age = self._hours[k] - self._release[idx]
        w = self._weight[idx] * removal_factor(age, self.lifetime_hours) * self._dt
        flat = np.bincount(row * self.nx + col, weights=w, minlength=self.ny * self.nx)
        self.load += flat.reshape(self.ny, self.nx)


@dataclass
class TrajectorySet:
    """Full paths of a (small) set of particles on a common time axis.

    lat[p, k] and lon[p, k] give the position of particle p at hours[k]. They are NaN before
    its release (k < release_step[p]) and after it stops (k > end_step[p]); in between, every
    value is a finite position inside the domain. When only part of the time axis was recorded
    (min_hour), release_step and end_step index the recorded columns: a particle released
    earlier starts at column 0, and one that stopped earlier has end_step = -1 < release_step.
    """

    hours: np.ndarray  # (K,) hours since t0
    times: np.ndarray  # (K,) datetime64[s], UTC
    lat: np.ndarray  # (P, K)
    lon: np.ndarray  # (P, K)
    cluster_index: np.ndarray  # (P,)
    weight: np.ndarray  # (P,)
    release_step: np.ndarray  # (P,)
    end_step: np.ndarray  # (P,)
    fate: np.ndarray  # (P,)

    def fate_counts(self) -> dict[str, int]:
        return {f.name.lower(): int((self.fate == f).sum()) for f in Fate}


def trace(
    field: WindField,
    particles: Particles,
    dt_hours: float | None = None,
    diffusivity_m2s: float = 0.0,
    seed: int | None = None,
    min_hour: float | None = None,
    dtype: type = np.float64,
) -> TrajectorySet:
    """Advect and keep the full path of every particle (from min_hour on, if given).

    Deterministic by default (no dispersion): used for the display tracers, whose paths show
    the axis of each puff on the map, and by the tests of the integrator.
    """
    recorder = PathRecorder(min_hour, dtype)
    adv = advect(field, particles, [recorder], dt_hours, diffusivity_m2s, seed)
    k_min = recorder.k_min
    release = np.maximum(adv.release_step - k_min, 0)
    end = adv.end_step - k_min
    gone = end < 0  # stopped before the first recorded step
    return TrajectorySet(
        hours=adv.hours[k_min:],
        times=adv.times[k_min:],
        lat=recorder.lat,
        lon=recorder.lon,
        cluster_index=particles.cluster_index,
        weight=particles.weight,
        release_step=np.where(gone, 0, release),
        end_step=np.where(gone, -1, end),
        fate=adv.fate,
    )


# --- 4. Export -----------------------------------------------------------------------------


@dataclass
class ExportSelection:
    trajectories: list[Trajectory]
    near_zone: int  # how many of them were chosen because they pass near a zone


def _compact_hour(h: float) -> float | int:
    h = float(h)
    return int(h) if h.is_integer() else round(h, 2)


def _near_zone(tracers: TrajectorySet, zone: config.Zone, reach_km: float) -> np.ndarray:
    """True for tracers whose path comes within reach_km of the zone centre."""
    # Cheap box test on each path's extent first; great-circle distances only for candidates.
    reach_lat = np.degrees(reach_km / (EARTH_RADIUS_M / 1000))
    reach_lon = reach_lat / np.cos(np.radians(min(abs(zone.lat) + reach_lat, 89.0)))
    lat_min, lat_max = np.fmin.reduce(tracers.lat, axis=1), np.fmax.reduce(tracers.lat, axis=1)
    lon_min, lon_max = np.fmin.reduce(tracers.lon, axis=1), np.fmax.reduce(tracers.lon, axis=1)
    box = (
        (lat_min <= zone.lat + reach_lat)
        & (lat_max >= zone.lat - reach_lat)
        & (lon_min <= zone.lon + reach_lon)
        & (lon_max >= zone.lon - reach_lon)
    )
    near = np.zeros(len(box), dtype=bool)
    cand = np.flatnonzero(box)
    if cand.size:
        d = great_circle_km(tracers.lat[cand], tracers.lon[cand], zone.lat, zone.lon)
        near[cand] = np.fmin.reduce(np.where(np.isnan(d), np.inf, d), axis=1) <= reach_km
    return near


def select_for_export(
    tracers: TrajectorySet,
    clusters: list[SourceCluster],
    zones: tuple[config.Zone, ...] | None = None,
    max_trajectories: int | None = None,
    near_km: float | None = None,
    priority_share: float | None = None,
    decimals: int | None = None,
    seed: int | None = None,
    contribution: np.ndarray | None = None,
) -> ExportSelection:
    """Choose the display trajectories for forecast.json.

    Showing only the strongest clusters would hide what matters most to a reader: a small fire
    whose smoke heads for their city. So the cap is filled in two passes:

    1. Near-zone trajectories: those passing within near_km of a zone's edge. Zones take turns
       (round robin), so a zone touched only by a small fire still gets its share. Each zone
       offers its candidates in a random order weighted by how much smoke their cluster brings
       to that zone (contribution[z, c], from the smoke step), so the lines drawn to a city
       match its source breakdown; without contribution, by trajectory weight. This pass takes
       at most priority_share of the cap, so the map keeps some regional context even when
       many trajectories pass near zones.
    2. The rest of the cap: the other trajectories, in a random order weighted by weight.

    The weighted random order is Efraimidis-Spirakis sampling without replacement: sorting by
    log(u) / w with u uniform in (0, 1) draws items with probability proportional to w. The
    seed makes the selection reproducible. Trajectories that never moved are left out.
    Coordinates are rounded to `decimals` and whole hours become integers.
    """
    zones = config.ZONES if zones is None else zones
    cap = config.TRAJ_EXPORT_MAX if max_trajectories is None else max_trajectories
    near_km = config.TRAJ_EXPORT_NEAR_KM if near_km is None else near_km
    share = config.TRAJ_EXPORT_PRIORITY_SHARE if priority_share is None else priority_share
    d = config.TRAJ_EXPORT_DECIMALS if decimals is None else decimals
    rng = np.random.default_rng(config.RANDOM_SEED if seed is None else seed)

    n = len(tracers.weight)
    moved = tracers.end_step > tracers.release_step
    tiny = np.finfo(float).tiny
    log_u = np.log(rng.random(n))
    order = np.argsort(-(log_u / np.maximum(tracers.weight, tiny)), kind="stable")

    chosen = np.zeros(n, dtype=bool)
    selected: list[int] = []
    queues = []
    for z, zone in enumerate(zones):
        cand = np.flatnonzero(moved & _near_zone(tracers, zone, zone.radius_km + near_km))
        if contribution is not None and contribution[z].sum() > 0:
            w = contribution[z, tracers.cluster_index[cand]]
        else:
            w = tracers.weight[cand]
        queues.append(list(cand[np.argsort(-(log_u[cand] / np.maximum(w, tiny)), kind="stable")]))
    budget = min(cap, int(cap * share))
    pointers = [0] * len(queues)
    progress = True
    while len(selected) < budget and progress:
        progress = False
        for z, queue in enumerate(queues):
            while pointers[z] < len(queue) and chosen[queue[pointers[z]]]:
                pointers[z] += 1
            if pointers[z] < len(queue) and len(selected) < budget:
                p = queue[pointers[z]]
                chosen[p] = True
                selected.append(p)
                progress = True
    n_near = len(selected)

    for p in order:
        if len(selected) >= cap:
            break
        if moved[p] and not chosen[p]:
            chosen[p] = True
            selected.append(int(p))

    selected.sort(key=lambda p: (tracers.cluster_index[p], tracers.release_step[p], p))
    out = []
    for p in selected:
        steps = slice(tracers.release_step[p], tracers.end_step[p] + 1)
        cluster = clusters[tracers.cluster_index[p]]
        out.append(
            Trajectory(
                id=len(out),
                cluster_id=cluster.id,
                weight=float(f"{tracers.weight[p]:.4g}"),
                path=[
                    (round(float(x), d), round(float(y), d))
                    for x, y in zip(tracers.lon[p, steps], tracers.lat[p, steps], strict=True)
                ],
                hours=[_compact_hour(h) for h in tracers.hours[steps]],
                source_region=cluster.region,
            )
        )
    return ExportSelection(out, n_near)


def clusters_for_export(clusters: list[SourceCluster]) -> list[SourceCluster]:
    """Clusters rounded for forecast.json: position to 3 decimals (about 100 m), FRP to 0.1 MW."""
    return [
        replace(c, lat=round(c.lat, 3), lon=round(c.lon, 3), frp_mw=round(c.frp_mw, 1))
        for c in clusters
    ]


def json_size(items: list) -> int:
    """Bytes of a list of schema dataclasses written as compact JSON, as in forecast.json."""
    text = json.dumps([asdict(i) for i in items], separators=(",", ":"), allow_nan=False)
    return len(text.encode())
