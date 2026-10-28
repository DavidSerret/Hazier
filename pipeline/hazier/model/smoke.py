"""Smoke arrival time and source attribution per zone.

The air quality shown to users comes from CAMS (see sources/cams.py). What Hazier adds per
zone is when the smoke of the fires detected in the last 48 h arrives, and where it comes from.
Both are derived from an internal smoke index, which is never shown as an air quality level:
it is far too rough for "how much PM2.5".

The particles of trajectories.py carry the smoke. While they move, a ZoneAccumulator (an
advection observer) adds up the smoke inside each zone, so only a few small arrays per zone
are kept instead of every particle's path.

Smoke index (internal)
----------------------
At each forecast hour, the smoke index of a zone is the summed weight of the particles within
its radius (great-circle distance from the zone centre), each weight reduced by removal:

    w * exp(-age / lifetime)

with age the time since the particle was released. Weights are in emission units (see
trajectories.release_particles), so the index measures the FRP-weighted amount of smoke still
airborne inside the zone. Rule of thumb: a cluster with summed FRP S over the 48 h lookback,
whose plume covers a zone for tau hours after travelling for t hours, adds about
(S / 48) * tau * f * exp(-t / lifetime), where f is the fraction of its particles that fall
inside the zone. Dispersion lowers f with distance, removal lowers the weight with age.

Removal
-------
Smoke particles leave the air mainly by wet deposition (rain), and more slowly by dry
deposition. A first-order loss with one lifetime stands for both. Model and observational
estimates of the atmospheric lifetime of fine smoke aerosol (organic and black carbon) are
typically 4 to 7 days as a global mean, and shorter where it rains often; in the humid tropics
a few days (about 2 to 4) is common. REMOVAL_LIFETIME_HOURS = 72 (3 days) is the middle of that
tropical range. One lifetime everywhere and at all times is a simplification: removal by
actual precipitation, which would wash out smoke under rain bands and spare it in dry spells,
is planned for phase 2. The sensitivity to 2 and 5 days is reported in docs/methodology.md.

Arrival time (ETA) and its range
--------------------------------
ETA: the first forecast hour at which the index reaches SMOKE_ARRIVAL_THRESHOLD; 0 means the
smoke is already there at t0, null that it does not arrive within the horizon. The threshold is
PROVISIONAL: it marks "a noticeable amount of smoke from a significant fire complex" in the
index's own units (see config.py and docs/methodology.md), and has not been calibrated against
observations.

Smoke does not arrive all at once. Dispersion spreads the arrival times of the particles, so
the index climbs over several hours as more of them enter the zone. The range is the time that
climb takes around the threshold: from the first hour the index reaches threshold / 2 to the
first hour it reaches threshold * 2 (ETA_RANGE_FACTOR). A sharp front gives a narrow range; a
slow build-up, or a zone on the edge of a plume, gives a wide one. If the index never reaches
twice the threshold the upper end stays open ("possibly after 72 h"). Because the threshold is
uncalibrated, the range also shows how far the ETA would move if it were half or double.

Why not percentiles of individual particle arrival times? Emission is continuous, so particles
keep arriving until the end of the forecast and their arrival times reflect the release
schedule more than the front. An ensemble built from one particle per release was also tried:
each such particle carries a whole hourly release as a single undiluted point, which inflates
the index at the edge of a plume and made the earliest ETA far too early.

The range covers dispersion and threshold sensitivity within one wind forecast. Wind forecast
error, which can be much larger near the equator (see docs/methodology.md), is not included.

Attribution
-----------
The share of the zone's smoke, summed over the forecast hours (after removal), that comes from
each source region (the province, state or country of each cluster, see regions.py). Shares are
rounded to 0.1% with the largest-remainder method, so they always add up to exactly 100%.

Debug: `python -m hazier.model.smoke` runs FIRMS, winds (cached), CAMS and the model for real,
prints the zone table and the removal sensitivity, and saves pipeline/output/trajectories.png.
"""

from __future__ import annotations

import argparse
import base64
import logging
import math
import sys
import time
import tracemalloc
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from hazier import config
from hazier.env import PIPELINE_DIR
from hazier.geo import EARTH_RADIUS_KM, great_circle_km
from hazier.model.regions import OTHER, OffshoreSplit, drop_offshore, label_clusters
from hazier.model.static import StaticSplit, drop_static
from hazier.model.trajectories import (
    Advection,
    Clustering,
    DensityGrid,
    ExportSelection,
    HourlyDensity,
    Particles,
    advect,
    cluster_hotspots,
    clusters_for_export,
    json_size,
    release_particles,
    removal_factor,
    select_for_export,
    trace,
)
from hazier.schema import Hotspot, SmokeGrid, SmokeModel, SourceCluster, SourceShare, ZoneForecast
from hazier.sources.winds import WindField

log = logging.getLogger(__name__)

OUTPUT_DIR = PIPELINE_DIR / "output"


# --- Accumulation during advection ---------------------------------------------------------


class ZoneAccumulator:
    """Advection observer: smoke inside each zone, per forecast hour and per cluster.

    Only the forecast window counts (hours >= 0): before t0 the particles are just being carried
    to where they are at t0. Weights are reduced by removal with lifetime_hours (None: the
    configured lifetime, inf: no removal). Several accumulators with different lifetimes can
    watch the same advection, since removal does not change where particles go. After advection:
    - hours: the forecast hours, 0 .. 72;
    - load[z, j]: smoke index of zone z at hours[j];
    - cluster_load[z, c]: smoke from cluster c inside zone z, summed over the forecast hours.
    """

    def __init__(
        self, zones: tuple[config.Zone, ...], n_clusters: int, lifetime_hours: float | None = None
    ) -> None:
        self.zones = zones
        self.n_clusters = n_clusters
        self.lifetime_hours = lifetime_hours
        # Half-widths of a box that contains each zone's circle: a cheap first filter.
        self._half_lat = np.array([np.degrees(z.radius_km / EARTH_RADIUS_KM) for z in zones])
        self._half_lon = np.array(
            [
                h / np.cos(np.radians(abs(z.lat) + h))
                for z, h in zip(zones, self._half_lat, strict=True)
            ]
        )
        # One box around all zones: most particles are far from every zone and stop here.
        lats = np.array([z.lat for z in zones])
        lons = np.array([z.lon for z in zones])
        self._all_box = (
            (lats - self._half_lat).min(),
            (lats + self._half_lat).max(),
            (lons - self._half_lon).min(),
            (lons + self._half_lon).max(),
        )

    def start(self, particles: Particles, hours: np.ndarray) -> None:
        self._weight = particles.weight
        self._release = particles.release_hour
        self._cluster = particles.cluster_index
        self._all_hours = hours
        self._k0 = int(np.searchsorted(hours, -1e-9))  # first step with hour >= 0
        self.hours = hours[self._k0 :]
        self.load = np.zeros((len(self.zones), len(self.hours)))
        self.cluster_load = np.zeros((len(self.zones), self.n_clusters))

    def observe(self, k: int, idx: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> None:
        if k < self._k0:
            return
        hour = self._all_hours[k]
        j = k - self._k0
        lat_lo, lat_hi, lon_lo, lon_hi = self._all_box
        near = np.flatnonzero((lat >= lat_lo) & (lat <= lat_hi) & (lon >= lon_lo) & (lon <= lon_hi))
        if near.size == 0:
            return
        idx, lat, lon = idx[near], lat[near], lon[near]
        for z, zone in enumerate(self.zones):
            box = (np.abs(lat - zone.lat) <= self._half_lat[z]) & (
                np.abs(lon - zone.lon) <= self._half_lon[z]
            )
            cand = np.flatnonzero(box)
            if cand.size == 0:
                continue
            dist = great_circle_km(lat[cand], lon[cand], zone.lat, zone.lon)
            inside = idx[cand[dist <= zone.radius_km]]
            if inside.size == 0:
                continue
            age = hour - self._release[inside]
            w = self._weight[inside] * removal_factor(age, self.lifetime_hours)
            self.load[z, j] += w.sum()
            self.cluster_load[z] += np.bincount(
                self._cluster[inside], weights=w, minlength=self.n_clusters
            )


# --- Per zone ------------------------------------------------------------------------------


@dataclass
class ZoneSmoke:
    """Hazier's smoke at one zone. Internal: the index is never shown as air quality."""

    zone: config.Zone
    hours: np.ndarray  # (K,) forecast hours 0 .. 72
    series: np.ndarray  # (K,) smoke index, rounded to 0.01
    peak: float
    peak_hour: float | None  # None when no smoke reaches the zone
    eta: float | None
    eta_earliest: float | None
    eta_latest: float | None
    sources: list[SourceShare]


def _first_hour(series: np.ndarray, hours: np.ndarray, threshold: float) -> float | None:
    hits = np.flatnonzero(series >= threshold)
    return float(hours[hits[0]]) if hits.size else None


def eta_range(
    series: np.ndarray, hours: np.ndarray, threshold: float
) -> tuple[float | None, float | None]:
    """(earliest, latest): first hours the series reaches threshold / f and threshold * f.

    f is ETA_RANGE_FACTOR; see the module docstring. Both None when the series never reaches
    the threshold itself; latest alone is None when it never reaches threshold * f. The order
    earliest <= ETA <= latest holds by construction.
    """
    if _first_hour(series, hours, threshold) is None:
        return None, None
    f = config.ETA_RANGE_FACTOR
    return _first_hour(series, hours, threshold / f), _first_hour(series, hours, threshold * f)


def source_shares(cluster_load: np.ndarray, clusters: list[SourceCluster]) -> list[SourceShare]:
    """Share of a zone's smoke by source region, in 0.1% steps summing to exactly 100%."""
    by_region: dict[str, float] = {}
    for cluster, load in zip(clusters, cluster_load, strict=True):
        if load > 0:
            region = cluster.region or OTHER
            by_region[region] = by_region.get(region, 0.0) + float(load)
    total = sum(by_region.values())
    if total <= 0:
        return []
    exact = {r: v / total * 1000 for r, v in by_region.items()}
    thousandths = {r: math.floor(x) for r, x in exact.items()}
    missing = 1000 - sum(thousandths.values())
    # Largest remainder: the regions that lost most to rounding down get the missing 0.1%s.
    for r in sorted(exact, key=lambda r: (-(exact[r] - thousandths[r]), r))[:missing]:
        thousandths[r] += 1
    shares = [SourceShare(r, t / 1000) for r, t in thousandths.items() if t > 0]
    return sorted(shares, key=lambda s: (-s.share, s.region))


def zone_smoke(
    acc: ZoneAccumulator, clusters: list[SourceCluster], threshold: float | None = None
) -> list[ZoneSmoke]:
    """Smoke index, ETA and attribution per zone from an accumulator."""
    threshold = config.SMOKE_ARRIVAL_THRESHOLD if threshold is None else threshold
    out = []
    for z, zone in enumerate(acc.zones):
        # Rounded once, so the ETA and the numbers reported always agree.
        series = np.round(acc.load[z], 2)
        peak_k = int(np.argmax(series))
        peak = float(series[peak_k])
        earliest, latest = eta_range(series, acc.hours, threshold)
        out.append(
            ZoneSmoke(
                zone=zone,
                hours=acc.hours,
                series=series,
                peak=peak,
                peak_hour=float(acc.hours[peak_k]) if peak > 0 else None,
                eta=_first_hour(series, acc.hours, threshold),
                eta_earliest=earliest,
                eta_latest=latest,
                sources=source_shares(acc.cluster_load[z], clusters),
            )
        )
    return out


def to_zone_forecasts(smoke: list[ZoneSmoke]) -> list[ZoneForecast]:
    """The published zone records. The CAMS fields are filled by cams.apply_to_zones."""
    return [
        ZoneForecast(
            id=s.zone.id,
            name=s.zone.name,
            lat=s.zone.lat,
            lon=s.zone.lon,
            radius_km=s.zone.radius_km,
            smoke_eta_hours=s.eta,
            smoke_eta_earliest_hours=s.eta_earliest,
            smoke_eta_latest_hours=s.eta_latest,
            smoke_sources=s.sources,
            smoke_index=[round(float(v), 1) for v in s.series],
        )
        for s in smoke
    ]


def smoke_model_metadata(field: WindField) -> SmokeModel:
    return SmokeModel(
        wind_model=field.model,
        level_hpa=field.level_hpa,
        lookback_hours=config.FIRMS_LOOKBACK_HOURS,
        past_hours=int(-field.hours[0]),
        removal_lifetime_hours=config.REMOVAL_LIFETIME_HOURS,
        arrival_threshold=config.SMOKE_ARRIVAL_THRESHOLD,
        wind_source=field.source,
        wind_run=f"{field.run_init:%Y-%m-%dT%H:%M:%SZ}" if field.run_init else None,
        wind_grid_deg=round(float(field.lats[1] - field.lats[0]), 4),
    )


def smoke_grid_for_export(hourly: HourlyDensity, zone_radius_km: float | None = None) -> SmokeGrid:
    """The map's smoke layer: the hourly density in smoke-index units, one byte per cell.

    A cell's load is the smoke inside it; spread over a zone's area instead, it is what a zone
    centred there would read: index = load * (zone area / cell area). So the arrival threshold
    (10) means the same on the map as on the zone card. Bytes are logarithmic: 0 below
    SMOKE_GRID_MIN_INDEX, then SMOKE_GRID_STEPS_PER_DECADE steps per factor of ten (40: about 6%
    per step), base64 encoded. Most cells are empty, so the text compresses well.
    """
    r = config.ZONE_RADIUS_KM if zone_radius_km is None else zone_radius_km
    cell_km = hourly.cell * 111.32
    cell_area = cell_km * cell_km * np.cos(np.radians(hourly.lats))[None, :, None]
    index = hourly.load * (math.pi * r * r) / cell_area
    lo, steps = config.SMOKE_GRID_MIN_INDEX, config.SMOKE_GRID_STEPS_PER_DECADE
    with np.errstate(divide="ignore"):
        q = 1 + np.floor(np.log10(np.maximum(index, lo) / lo) * steps)
    q = np.where(index >= lo, np.clip(q, 1, 255), 0).astype(np.uint8)
    return SmokeGrid(
        lats=[round(float(x), 4) for x in hourly.lats],
        lons=[round(float(x), 4) for x in hourly.lons],
        hours=[int(h) if float(h).is_integer() else float(h) for h in hourly.hours],
        cell_deg=hourly.cell,
        min_index=lo,
        steps_per_decade=steps,
        data=base64.b64encode(q.tobytes()).decode("ascii"),
    )


# --- The whole model -----------------------------------------------------------------------


@dataclass
class ModelRun:
    clustering: Clustering
    clusters: list[SourceCluster]  # kept clusters with their region
    offshore: OffshoreSplit  # clusters excluded for lying off shore (gas flares)
    static: StaticSplit  # hotspots excluded as static heat sources (industry, volcanoes)
    n_particles: int  # dispersed particles behind the smoke index
    n_past_particles: int  # of which released before t0
    n_in_air_at_t0: int  # of those, still inside the domain at t0
    n_tracers: int  # undispersed display tracers, one per release
    advection: Advection
    smoke: list[ZoneSmoke]  # with the configured removal lifetime
    zones: list[ZoneForecast]  # published records, CAMS fields still empty
    sensitivity: dict[float, list[ZoneSmoke]]  # by extra removal lifetime (hours)
    selection: ExportSelection
    density: DensityGrid
    hourly: HourlyDensity  # smoke per forecast hour, for the map
    timings: dict[str, float]


def run_model(
    hotspots: list[Hotspot],
    field: WindField,
    zones: tuple[config.Zone, ...] | None = None,
    extra_lifetimes: tuple[float, ...] = (),
) -> ModelRun:
    """Hotspots and winds in, smoke timing, attribution and display trajectories out.

    Each cluster releases from its first detection in the last 48 h (no earlier than the start
    of the wind field), so the smoke already in the air at t0 is carried there by the past
    winds and counts in the forecast.

    Two particle sets are advected:
    - the dispersed particles (20 per release) carry the smoke; only per-zone sums and a coarse
      density grid are kept. extra_lifetimes adds accumulators with other removal lifetimes to
      the same advection, for sensitivity checks;
    - one undispersed tracer per release keeps its full path for the map. It shows the axis of
      each puff; its lines are smooth where a random-walk path would zigzag.
    """
    zones = config.ZONES if zones is None else zones
    timings: dict[str, float] = {}
    t = time.perf_counter()

    static = drop_static(hotspots)
    if static.dropped:
        log.info(
            "Excluded %d hotspots on static heat sources (industry, volcanoes), %.0f MW of FRP",
            len(static.dropped),
            static.dropped_frp_mw,
        )
    clustering = cluster_hotspots(static.kept)
    offshore = drop_offshore(label_clusters(clustering.clusters))
    clusters = offshore.kept
    if offshore.dropped:
        log.info(
            "Excluded %d offshore clusters (gas flares), %.0f MW of FRP in total",
            len(offshore.dropped),
            offshore.dropped_frp_mw,
        )
    timings["clustering and regions"] = time.perf_counter() - t

    t = time.perf_counter()
    particles = release_particles(clusters, t0=field.t0, earliest_hour=float(field.hours[0]))
    acc = ZoneAccumulator(zones, len(clusters))
    extra = {tau: ZoneAccumulator(zones, len(clusters), tau) for tau in extra_lifetimes}
    density = DensityGrid()
    hourly = HourlyDensity()
    adv = advect(field, particles, [acc, density, hourly, *extra.values()])
    timings["advection with dispersion"] = time.perf_counter() - t

    t = time.perf_counter()
    tracer_particles = release_particles(
        clusters, particles_per_release=1, t0=field.t0, earliest_hour=float(field.hours[0])
    )
    # Only the forecast part of their paths is kept and shown: a puff released before t0
    # appears on the map where it is at t0.
    tracers = trace(field, tracer_particles, min_hour=0.0, dtype=np.float32)
    selection = select_for_export(tracers, clusters)
    timings["display tracers and selection"] = time.perf_counter() - t

    t = time.perf_counter()
    smoke = zone_smoke(acc, clusters)
    sensitivity = {tau: zone_smoke(a, clusters) for tau, a in extra.items()}
    timings["zone smoke"] = time.perf_counter() - t

    return ModelRun(
        clustering=clustering,
        clusters=clusters,
        offshore=offshore,
        static=static,
        n_particles=len(particles),
        n_past_particles=int((particles.release_hour < 0).sum()),
        n_in_air_at_t0=int(((particles.release_hour < 0) & (adv.end_step >= adv.t0_index)).sum()),
        n_tracers=len(tracer_particles),
        advection=adv,
        smoke=smoke,
        zones=to_zone_forecasts(smoke),
        sensitivity=sensitivity,
        selection=selection,
        density=density,
        hourly=hourly,
        timings=timings,
    )


# --- Debug command -------------------------------------------------------------------------


def fmt_eta(eta: float | None, earliest: float | None, latest: float | None) -> str:
    """ETA with its range, for tables: "already at t0", "14 h (12 to 18)", "-"."""
    if eta is None:
        return "-"
    if eta == 0 and latest == 0:
        return "already at t0"
    late = f"{latest:g}" if latest is not None else f">{config.HORIZON_HOURS}"
    return f"{eta:g} h ({earliest:g} to {late})"


def _top(sources: list[SourceShare], n: int = 2) -> str:
    return ", ".join(f"{s.region} {s.share:.0%}" for s in sources[:n]) or "-"


# Label offsets (points) for zones whose circles sit close together on the map.
_LABEL_OFFSETS = {"singapore": (10, -20), "kuantan": (10, 6)}


def _plot(path: Path, field: WindField, run: ModelRun, zones: list[ZoneForecast], title: str):
    """Static map: smoke over 72 h, display trajectories, clusters, zones by CAMS category now.

    No coastline (that would need cartopy and its data); the zones and the fire clusters,
    which outline Sumatra and Borneo, give enough orientation.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.colors import LogNorm
    from matplotlib.patches import Ellipse, Patch

    from hazier.sources.cams import CATEGORY_COLOURS

    fig, ax = plt.subplots(figsize=(12, 9.6))

    load = run.density.load
    if load.max() > 0:
        masked = np.ma.masked_less_equal(load, load.max() * 1e-5)
        mesh = ax.imshow(
            masked, origin="lower", cmap="YlOrRd", norm=LogNorm(vmin=masked.min(), vmax=load.max()),
            extent=(config.LON_MIN, config.LON_MAX, config.LAT_MIN, config.LAT_MAX),
            alpha=0.85, zorder=1, interpolation="nearest",
        )  # fmt: skip
        fig.colorbar(mesh, ax=ax, shrink=0.7, label="Hazier smoke over 72 h, after removal (log)")

    lon_g, lat_g = np.meshgrid(field.lons, field.lats)
    q = ax.quiver(
        lon_g, lat_g, field.u[field.t0_index], field.v[field.t0_index], color="0.55", angles="xy",
        scale_units="xy", scale=12, width=0.0014, alpha=0.6, zorder=2,
    )  # fmt: skip
    ax.quiverkey(q, 0.03, 0.97, 10, "10 m/s at t0", labelpos="E", fontproperties={"size": 8})

    segments = [np.array(t.path) for t in run.selection.trajectories if len(t.path) > 1]
    if segments:
        ax.add_collection(
            LineCollection(segments, colors="#3b3b8f", linewidths=0.5, alpha=0.45, zorder=3)
        )

    if run.clusters:
        frp = np.array([c.frp_mw for c in run.clusters])
        ax.scatter(
            [c.lon for c in run.clusters], [c.lat for c in run.clusters],
            s=4 + 80 * np.sqrt(frp / frp.max()), c="black", edgecolors="white",
            linewidths=0.3, zorder=4,
        )  # fmt: skip

    for z in zones:
        r_lat = z.radius_km / 110.57
        r_lon = z.radius_km / (111.32 * np.cos(np.radians(z.lat)))
        category = z.cams_now.category if z.cams_now else None
        colour = CATEGORY_COLOURS.get(category, "0.8")
        ax.add_patch(
            Ellipse(
                (z.lon, z.lat), 2 * r_lon, 2 * r_lat, facecolor=colour, edgecolor="black",
                alpha=0.8, lw=0.8, zorder=5,
            )
        )  # fmt: skip
        # Name only: the peninsula is crowded; levels and ETAs are in the printed table.
        ax.annotate(
            z.name, (z.lon, z.lat),
            xytext=_LABEL_OFFSETS.get(z.id, (9, 4)), textcoords="offset points", fontsize=7,
            zorder=6,
        )  # fmt: skip

    handles = [
        Patch(facecolor=c, edgecolor="black", label=name.replace("_", " "))
        for name, c in CATEGORY_COLOURS.items()
    ]
    handles.append(Patch(facecolor="black", label="fire clusters (area ~ FRP)"))
    ax.legend(
        handles=handles, loc="lower left", fontsize=8, title="CAMS PM2.5 now (provisional)",
        title_fontsize=8,
    )  # fmt: skip

    ax.set_xlim(config.LON_MIN, config.LON_MAX)
    ax.set_ylim(config.LAT_MIN, config.LAT_MAX)
    ax.set_aspect("equal")
    ax.grid(lw=0.3, alpha=0.4)
    ax.set_xlabel("longitude (deg E)")
    ax.set_ylabel("latitude (deg N)")
    ax.set_title(title, fontsize=10)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FIRMS, winds, CAMS and the smoke model.")
    parser.add_argument("--no-plot", action="store_true", help="skip the PNG")
    parser.add_argument("--no-grid", action="store_true", help="skip the CAMS map layer")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from hazier.env import load_env
    from hazier.sources import cams as cams_mod
    from hazier.sources.firms import fetch_hotspots
    from hazier.sources.winds import fetch_winds

    load_env()
    now = datetime.now(UTC)
    firms = fetch_hotspots(now=now)
    if not firms.status.usable:
        print(f"FIRMS failed: {firms.status.detail}")
        return 1
    winds = fetch_winds(now=now)
    if winds.field is None:
        print(f"Winds failed: {winds.status.detail}")
        return 1
    field = winds.field
    cams_zones = cams_mod.fetch_cams(now=now)
    grid = None if args.no_grid else cams_mod.fetch_cams_grid(now=now)

    lifetimes = (48.0, 120.0)  # 2 and 5 days, next to the configured 3
    tracemalloc.start()
    t0 = time.perf_counter()
    run = run_model(firms.hotspots, field, extra_lifetimes=lifetimes)
    total = time.perf_counter() - t0
    peak_bytes = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    zones = cams_mod.apply_to_zones(run.zones, cams_zones)

    c = run.clustering
    fates = run.advection.fate_counts()
    shown = run.selection.trajectories
    exported_clusters = clusters_for_export(run.clusters)
    traj_kb, clusters_kb = json_size(shown) / 1024, json_size(exported_clusters) / 1024
    run_time = f"{field.run_init:%Y-%m-%dT%H:%MZ}" if field.run_init else "unknown run"

    print(f"\nHazier  now {now:%Y-%m-%dT%H:%M:%SZ}  t0 {field.t0} UTC")
    print(
        f"inputs: {len(firms.hotspots)} hotspots (FIRMS {firms.status.state}), "
        f"winds {field.model} {run_time} ({'cache' if winds.from_cache else 'API'})"
    )
    print(
        f"clusters: {len(run.clusters)} kept of {c.cells} cells ({c.dropped} below "
        f"{config.CLUSTER_MIN_FRP_MW:g} MW), {len(run.offshore.dropped)} offshore excluded "
        f"({run.offshore.dropped_frp_mw:.0f} MW)"
    )
    print(
        f"particles: {run.n_particles:,}, of which {run.n_past_particles:,} released before t0 "
        f"({run.n_in_air_at_t0:,} still in the domain at t0); {run.n_tracers:,} display tracers"
    )
    print(
        f"fates: reached {config.HORIZON_HOURS} h {fates['horizon']:,}, left domain "
        f"{fates['left_domain']:,}, missing wind {fates['missing_wind']:,}"
    )
    print(
        f"removal lifetime {config.REMOVAL_LIFETIME_HOURS / 24:g} days, arrival threshold "
        f"{config.SMOKE_ARRIVAL_THRESHOLD:g} (provisional)"
    )
    print("runtime: " + ", ".join(f"{k} {v:.1f} s" for k, v in run.timings.items()))
    print(
        f"         model total {total:.1f} s (tracemalloc on); peak memory {peak_bytes / 2**20:.0f} MB"
    )

    wind_calls = winds.calls_used
    grid_calls = grid.calls_used if grid else 0
    run_calls = wind_calls + cams_zones.calls_used + grid_calls
    full = len(field.lats) * len(field.lons) * 2 + len(config.ZONES)
    print(
        f"Open-Meteo calls this run: winds {wind_calls}, CAMS zones {cams_zones.calls_used}, "
        f"CAMS grid {grid_calls} = {run_calls} (cache hits cost 0). Without cache: {full} per run, "
        f"{full * 4:,} per day at 4 runs (limits 600/min, 5,000/h, 10,000/day)"
    )
    sizes = f"trajectories {traj_kb:.0f} KB, clusters {clusters_kb:.0f} KB"
    if grid and grid.grid is not None:
        sizes += f", CAMS grid {json_size([cams_mod.grid_for_export(grid)]) / 1024:.0f} KB"
    print(f"export sizes: {sizes}")
    print(
        f"CAMS: zones {cams_zones.status.state}"
        + (f", grid {grid.status.state}" if grid else ", grid skipped")
    )

    print(
        f"\n{'zone':<19}{'CAMS now':<23}{'CAMS 72 h peak':<23}{'trend':<11}"
        f"{'smoke ETA':<20}{'smoke from':<34}agreement"
    )
    for z in zones:
        now_txt = (
            f"{z.cams_now.basis_ugm3:5.1f} {z.cams_now.category.replace('_', ' ')}"
            if z.cams_now
            else "no data"
        )
        peak_txt = (
            f"{z.cams_peak.basis_ugm3:5.1f} {z.cams_peak.category.replace('_', ' ')}"
            if z.cams_peak
            else "-"
        )
        eta = fmt_eta(z.smoke_eta_hours, z.smoke_eta_earliest_hours, z.smoke_eta_latest_hours)
        print(
            f"{z.name:<19}{now_txt:<23}{peak_txt:<23}{z.cams_trend or '-':<11}{eta:<20}"
            f"{_top(z.smoke_sources):<34}{z.agreement or '-'}"
        )
    print("CAMS values: 24 h trailing mean, ug/m3; categories provisional (see config.py)")

    print("\nRemoval sensitivity (internal smoke index peak, ETA, main sources):")
    by_tau = {config.REMOVAL_LIFETIME_HOURS: run.smoke, **run.sensitivity}
    taus = sorted(by_tau)
    print(f"{'zone':<19}" + "".join(f"{f'tau = {t / 24:g} d':<44}" for t in taus))
    for i, zone in enumerate(config.ZONES):
        cells = []
        for t in taus:
            s = by_tau[t][i]
            eta = fmt_eta(s.eta, s.eta_earliest, s.eta_latest)
            cells.append(f"{s.peak:6.1f}  {eta:<15}{_top(s.sources, 1):<21}")
        print(f"{zone.name:<19}" + "".join(cells))

    if not args.no_plot:
        title = (
            f"{len(run.clusters)} clusters, {run.n_particles:,} particles, winds {field.model} "
            f"{config.PRESSURE_LEVEL_HPA} hPa {run_time}, removal {config.REMOVAL_LIFETIME_HOURS / 24:g} d\n"
            f"t0 = {field.t0} UTC; shading: Hazier smoke over 72 h; lines: {len(shown)} display "
            f"trajectories; circles: CAMS category now"
        )
        try:
            out = _plot(OUTPUT_DIR / "trajectories.png", field, run, zones, title)
            print(f"plot: {out}")
        except ImportError:
            print('plot skipped: matplotlib is not installed (pip install -e ".[dev]")')
    return 0


if __name__ == "__main__":
    sys.exit(main())
