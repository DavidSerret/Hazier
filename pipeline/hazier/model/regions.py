"""Source regions: the province, state or country each fire cluster lies in.

Outlines come from data/regions.geojson: Natural Earth admin-1, public domain, reduced to the
countries around the domain (see data/README.md). Indonesia and Malaysia are split into
provinces and states, other countries are one region each.

A point is inside a region by the even-odd rule over all its rings. Fires on the shore can
have a centroid just over the water; such a point gets the nearest region within SNAP_KM.
Anything farther from land is labelled "Other".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import numpy as np

from hazier.schema import SourceCluster

REGIONS_FILE = Path(__file__).resolve().parents[1] / "data" / "regions.geojson"
OTHER = "Other"
SNAP_KM = 25.0
_CHUNK = 2000  # points tested at once, bounds the size of the point x edge matrices


@dataclass(frozen=True)
class Region:
    name: str
    country: str
    rings: tuple[np.ndarray, ...]  # closed rings, (n, 2) arrays of (lon, lat); outers and holes
    bbox: tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max


@lru_cache(maxsize=4)
def load_regions(path: Path = REGIONS_FILE) -> tuple[Region, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    regions = []
    for feature in data["features"]:
        rings = tuple(
            np.asarray(ring, dtype=float)
            for polygon in feature["geometry"]["coordinates"]
            for ring in polygon
        )
        pts = np.concatenate(rings)
        bbox = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())
        props = feature["properties"]
        regions.append(Region(props["name"], props["country"], rings, bbox))
    return tuple(regions)


def _inside(lon: np.ndarray, lat: np.ndarray, rings: tuple[np.ndarray, ...]) -> np.ndarray:
    """Even-odd rule: cast a ray east from each point and count the edges it crosses.

    An odd count means inside. A point in a hole crosses the outer ring and the hole, an even
    count, so holes work without special cases. The polygons of one region do not overlap,
    so the rule also holds across a MultiPolygon.
    """
    odd = np.zeros(lon.shape, dtype=bool)
    px, py = lon[:, None], lat[:, None]
    for ring in rings:
        x1, y1, x2, y2 = ring[:-1, 0], ring[:-1, 1], ring[1:, 0], ring[1:, 1]
        straddles = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):  # horizontal edges never straddle
            x_cross = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
        odd ^= (straddles & (px < x_cross)).sum(axis=1) % 2 == 1
    return odd


def _distance_to_edges_km(lat: float, lon: float, ring: np.ndarray) -> float:
    """Shortest distance from a point to a ring's edges, on a local flat projection.

    Accurate to well under 1% within SNAP_KM, which is all it is used for.
    """
    kx = 111.32 * np.cos(np.radians(lat))
    ax, ay = (ring[:-1, 0] - lon) * kx, (ring[:-1, 1] - lat) * 110.57
    bx, by = (ring[1:, 0] - lon) * kx, (ring[1:, 1] - lat) * 110.57
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.clip(np.where(length2 > 0, -(ax * dx + ay * dy) / length2, 0.0), 0.0, 1.0)
    return float(np.hypot(ax + t * dx, ay + t * dy).min())


def region_names(lats, lons, regions: tuple[Region, ...] | None = None) -> list[str]:
    """Region name for each point (lat, lon in degrees)."""
    regions = load_regions() if regions is None else regions
    lat = np.atleast_1d(np.asarray(lats, dtype=float))
    lon = np.atleast_1d(np.asarray(lons, dtype=float))
    names = np.full(lat.shape, OTHER, dtype=object)
    unassigned = np.ones(lat.shape, dtype=bool)

    for region in regions:
        x0, y0, x1, y1 = region.bbox
        todo = np.flatnonzero(unassigned & (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1))
        for start in range(0, len(todo), _CHUNK):
            chunk = todo[start : start + _CHUNK]
            hit = chunk[_inside(lon[chunk], lat[chunk], region.rings)]
            names[hit] = region.name
            unassigned[hit] = False

    for i in np.flatnonzero(unassigned):
        best_name, best_km = OTHER, SNAP_KM
        for region in regions:
            x0, y0, x1, y1 = region.bbox
            margin = SNAP_KM / 100.0  # degrees, generous
            if not (x0 - margin <= lon[i] <= x1 + margin and y0 - margin <= lat[i] <= y1 + margin):
                continue
            km = min(_distance_to_edges_km(lat[i], lon[i], ring) for ring in region.rings)
            if km <= best_km:
                best_name, best_km = region.name, km
        names[i] = best_name
    return [str(n) for n in names]


def label_clusters(clusters: list[SourceCluster]) -> list[SourceCluster]:
    """Copies of the clusters with their region filled in."""
    if not clusters:
        return []
    names = region_names([c.lat for c in clusters], [c.lon for c in clusters])
    return [replace(c, region=name) for c, name in zip(clusters, names, strict=True)]


@dataclass
class OffshoreSplit:
    kept: list[SourceCluster]  # renumbered so that id == list index, strongest first
    dropped: list[SourceCluster]  # original ids kept, for logging
    dropped_frp_mw: float


def drop_offshore(clusters: list[SourceCluster]) -> OffshoreSplit:
    """Remove labelled clusters that are not on land.

    A cluster is offshore when its centroid is outside every land polygon and farther than
    SNAP_KM from the nearest one, i.e. its region is OTHER. Such clusters are almost always
    gas flares on offshore oil and gas fields (for example the one near 10.4 N 108.2 E, off
    southern Vietnam), not vegetation fires. Fires on the shore, whose centroid falls just on
    the water, are within SNAP_KM of land and are kept.

    The input must already be labelled (see label_clusters).
    """
    kept = [c for c in clusters if c.region != OTHER]
    dropped = [c for c in clusters if c.region == OTHER]
    return OffshoreSplit(
        kept=[replace(c, id=i) for i, c in enumerate(kept)],
        dropped=dropped,
        dropped_frp_mw=float(sum(c.frp_mw for c in dropped)),
    )
