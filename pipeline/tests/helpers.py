"""Shared builders for the model tests: synthetic wind fields, particles and clusters."""

import numpy as np

from hazier.model import trajectories as tj
from hazier.schema import Hotspot, SourceCluster
from hazier.sources.winds import WindField, grid_axes

R = 6_371_000.0
T0 = np.datetime64("2026-09-14T14:00:00", "s")


def make_field(fn, hours=72, past_hours=0):
    """WindField on the real 1 degree grid with (u, v) = fn(lat, lon, hour) at every node.

    hour is counted from t0 = T0; with past_hours the field starts that long before T0.
    """
    lats, lons = grid_axes()
    h = np.arange(-past_hours, hours + 1, dtype=float)
    hh, la, lo = np.meshgrid(h, lats, lons, indexing="ij")
    u, v = fn(la, lo, hh)
    u = np.broadcast_to(u, hh.shape).astype(float).copy()
    v = np.broadcast_to(v, hh.shape).astype(float).copy()
    times = T0 + (h * 3600).astype("int64").astype("timedelta64[s]")
    return WindField("test", None, 850, times, lats, lons, u, v, t0_index=past_hours)


def uniform(u0, v0, past_hours=0):
    return make_field(
        lambda la, lo, h: (np.full(la.shape, u0), np.full(la.shape, v0)), past_hours=past_hours
    )


def particles_at(points, release_hours=None):
    """One particle per (lat, lon), each with weight 1 / n."""
    n = len(points)
    hours = np.zeros(n) if release_hours is None else np.asarray(release_hours, dtype=float)
    return tj.Particles(
        cluster_index=np.arange(n),
        release_hour=hours,
        lat0=np.array([p[0] for p in points], dtype=float),
        lon0=np.array([p[1] for p in points], dtype=float),
        weight=np.full(n, 1 / n),
    )


def cluster(i, lat, lon, frp, region=""):
    return SourceCluster(i, lat, lon, frp, 3, "2026-09-14T05:00:00Z", region)


def hotspot(lat, lon, frp, at="2026-09-14T05:00:00Z", i=0):
    return Hotspot(i, lat, lon, frp, "nominal", at, "S-NPP")
