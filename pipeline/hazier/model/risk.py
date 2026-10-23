"""Smoke risk, arrival time and source attribution per zone.

The particles of trajectories.py carry the smoke. While they move, a ZoneAccumulator (an
advection observer) adds up the smoke inside each zone, so only a few small arrays per zone
are kept instead of every particle's path.

Smoke index
-----------
At each forecast hour, the smoke index of a zone is the summed weight of the particles within
its radius (great-circle distance from the zone centre). Weights are in emission units (see
trajectories.release_particles), so the index measures the FRP-weighted amount of smoke inside
the zone. Rule of thumb: a cluster with summed FRP S over the 48 h lookback, whose plume
covers a zone for tau hours, adds about (S / 48) * tau * f, where f is the fraction of its
particles that fall inside the zone. Dispersion lowers f with distance, so far fires count less.

Levels map the index to low, moderate, high and very high (config.RISK_LEVEL_THRESHOLDS).
THESE THRESHOLDS ARE PROVISIONAL AND UNCALIBRATED. They were chosen by judgement so that a
zone downwind of a large fire complex reads high, and a brush with the plume of a few small
fires reads low. Nothing yet ties them to measured PM2.5. They must be calibrated in phase 2
against the APIMS stations and CAMS, and until then they should be read as relative.

ETA and its range
-----------------
ETA: the first forecast hour at which the index reaches "moderate"; null if it never does.

Smoke does not arrive all at once. Dispersion spreads the arrival times of the particles, so
the index climbs over several hours as more of them enter the zone. The range is the time that
climb takes around the moderate level: from the first hour the index reaches moderate / 2 to
the first hour it reaches moderate * 2 (ETA_RANGE_FACTOR). A sharp front gives a narrow range;
a slow build-up, or a zone on the edge of a plume, gives a wide one. If the index never reaches
twice the threshold the upper end stays open ("possibly after 72 h"). Because the thresholds
themselves are uncalibrated, the same range also shows how far the ETA would move if the true
threshold were half or double.

Why not percentiles of individual particle arrival times? Emission is continuous, so particles
keep arriving until the end of the forecast and their arrival times reflect the release
schedule more than the front. An ensemble built from one particle per release was also tried:
each such particle carries a whole hourly release as a single undiluted point, which inflates
the index at the edge of a plume and made the earliest ETA far too early (Penang in a synthetic
test: 26 h, while the mean index reached moderate only at 63 h).

The range covers dispersion and threshold sensitivity within one wind forecast. Wind forecast
error, which can be much larger near the equator (see docs/methodology.md), is not included,
so the true uncertainty is wider.

Attribution
-----------
The share of the zone's smoke, summed over the whole forecast, that comes from each source
region (the province, state or country of each cluster, see regions.py). Shares are rounded to
0.1% with the largest-remainder method, so they always add up to exactly 100%.

Debug: `python -m hazier.model.risk` runs FIRMS, winds (cached), trajectories and risk for
real, prints a table per zone and saves pipeline/output/trajectories.png.
"""

from __future__ import annotations

import argparse
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
from hazier.model.regions import OTHER, label_clusters
from hazier.model.trajectories import (
    Advection,
    Clustering,
    DensityGrid,
    ExportSelection,
    Particles,
    advect,
    cluster_hotspots,
    clusters_for_export,
    json_size,
    release_particles,
    select_for_export,
    trace,
)
from hazier.schema import Hotspot, RiskLevel, RiskPoint, SourceCluster, SourceShare, ZoneForecast
from hazier.sources.winds import WindField

log = logging.getLogger(__name__)

OUTPUT_DIR = PIPELINE_DIR / "output"
# Debug plot colours, in the order of the Malaysian API palette (blue, green, yellow, orange).
LEVEL_COLOURS = {"low": "#2c7fb8", "moderate": "#41ab5d", "high": "#fec44f", "very_high": "#e6550d"}


# --- Levels --------------------------------------------------------------------------------


def thresholds() -> dict[str, float]:
    """Lower bound of the smoke index for each level (provisional, see the module docstring)."""
    return dict(config.RISK_LEVEL_THRESHOLDS)


def level_of(index: float) -> RiskLevel:
    level = "low"
    for name, lower in config.RISK_LEVEL_THRESHOLDS:
        if index >= lower:
            level = name
    return level  # type: ignore[return-value]


# --- Accumulation during advection ---------------------------------------------------------


class ZoneAccumulator:
    """Advection observer: smoke inside each zone, per hour and per cluster.

    After advection:
    - load[z, k]: smoke index of zone z at hours[k] (summed weight inside the radius);
    - cluster_load[z, c]: smoke from cluster c inside zone z, summed over all hours.
    """

    def __init__(self, zones: tuple[config.Zone, ...], n_clusters: int) -> None:
        self.zones = zones
        self.n_clusters = n_clusters
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
        self._cluster = particles.cluster_index
        self.load = np.zeros((len(self.zones), len(hours)))
        self.cluster_load = np.zeros((len(self.zones), self.n_clusters))

    def observe(self, k: int, idx: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> None:
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
            w = self._weight[inside]
            self.load[z, k] += w.sum()
            self.cluster_load[z] += np.bincount(
                self._cluster[inside], weights=w, minlength=self.n_clusters
            )


# --- Zone forecasts ------------------------------------------------------------------------


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


def zone_forecasts(
    acc: ZoneAccumulator,
    clusters: list[SourceCluster],
    hours: np.ndarray,
) -> list[ZoneForecast]:
    """One ZoneForecast per zone from the accumulated smoke. cams_pm25 is filled later."""
    moderate = thresholds()["moderate"]
    out = []
    for z, zone in enumerate(acc.zones):
        # Rounded once, so the levels, the ETA and the published numbers always agree.
        series = np.round(acc.load[z], 2)
        peak_k = int(np.argmax(series))
        peak = float(series[peak_k])
        eta = _first_hour(series, hours, moderate)
        earliest, latest = eta_range(series, hours, moderate)
        out.append(
            ZoneForecast(
                id=zone.id,
                name=zone.name,
                lat=zone.lat,
                lon=zone.lon,
                radius_km=zone.radius_km,
                risk_index=peak,
                risk_level=level_of(peak),
                peak_hour=float(hours[peak_k]) if peak > 0 else None,
                eta_hours=eta,
                eta_earliest_hours=earliest,
                eta_latest_hours=latest,
                sources=source_shares(acc.cluster_load[z], clusters),
                risk_series=[
                    RiskPoint(float(h), float(i), level_of(i))
                    for h, i in zip(hours, series, strict=True)
                ],
                cams_pm25=[],
            )
        )
    return out


# --- The whole model -----------------------------------------------------------------------


@dataclass
class ModelRun:
    clustering: Clustering
    clusters: list[SourceCluster]  # kept clusters with their region
    n_particles: int  # dispersed particles behind the risk
    n_tracers: int  # undispersed display tracers, one per release
    advection: Advection
    zones: list[ZoneForecast]
    selection: ExportSelection
    density: DensityGrid
    timings: dict[str, float]


def run_model(
    hotspots: list[Hotspot], field: WindField, zones: tuple[config.Zone, ...] | None = None
) -> ModelRun:
    """Hotspots and winds in, zone forecasts and display trajectories out.

    Two particle sets are advected:
    - the dispersed particles (20 per release) carry the smoke for the risk; only per-zone
      sums and a coarse density grid are kept;
    - one undispersed tracer per release keeps its full path for the map. It shows the axis of
      each puff; its lines are smooth where a random-walk path would zigzag.
    """
    zones = config.ZONES if zones is None else zones
    timings: dict[str, float] = {}
    t = time.perf_counter()

    clustering = cluster_hotspots(hotspots)
    clusters = label_clusters(clustering.clusters)
    timings["clustering and regions"] = time.perf_counter() - t

    t = time.perf_counter()
    particles = release_particles(clusters)
    acc = ZoneAccumulator(zones, len(clusters))
    density = DensityGrid()
    adv = advect(field, particles, [acc, density])
    timings["advection with dispersion"] = time.perf_counter() - t

    t = time.perf_counter()
    tracer_particles = release_particles(clusters, particles_per_release=1)
    tracers = trace(field, tracer_particles)
    selection = select_for_export(tracers, clusters, zones, contribution=acc.cluster_load)
    timings["display tracers and selection"] = time.perf_counter() - t

    t = time.perf_counter()
    forecasts = zone_forecasts(acc, clusters, adv.hours)
    timings["zone risk"] = time.perf_counter() - t

    return ModelRun(
        clustering=clustering,
        clusters=clusters,
        n_particles=len(particles),
        n_tracers=len(tracer_particles),
        advection=adv,
        zones=forecasts,
        selection=selection,
        density=density,
        timings=timings,
    )


# --- Debug command -------------------------------------------------------------------------


def _fmt_hours(h: float | None) -> str:
    return "-" if h is None else f"{h:g} h"


def _fmt_range(z: ZoneForecast) -> str:
    if z.eta_hours is None:
        return "-"
    late = (
        f"{z.eta_latest_hours:g} h"
        if z.eta_latest_hours is not None
        else f">{config.HORIZON_HOURS} h"
    )
    return f"{z.eta_earliest_hours:g} to {late}"


# Label offsets (points) for zones whose circles sit close together on the map.
_LABEL_OFFSETS = {"singapore": (10, -20), "kuantan": (10, 6)}


def _plot(path: Path, field: WindField, run: ModelRun, title: str) -> Path:
    """Static map: time-integrated smoke, display trajectories, clusters, zones by peak level.

    No coastline (that would need cartopy and its data); the zones and the fire clusters,
    which outline Sumatra and Borneo, give enough orientation.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.colors import LogNorm
    from matplotlib.patches import Ellipse, Patch

    fig, ax = plt.subplots(figsize=(12, 9.6))

    load = run.density.load
    if load.max() > 0:
        masked = np.ma.masked_less_equal(load, load.max() * 1e-5)
        mesh = ax.imshow(
            masked, origin="lower", cmap="YlOrRd", norm=LogNorm(vmin=masked.min(), vmax=load.max()),
            extent=(config.LON_MIN, config.LON_MAX, config.LAT_MIN, config.LAT_MAX),
            alpha=0.85, zorder=1, interpolation="nearest",
        )  # fmt: skip
        fig.colorbar(mesh, ax=ax, shrink=0.7, label="smoke over 72 h (emission units x h, log)")

    lon_g, lat_g = np.meshgrid(field.lons, field.lats)
    q = ax.quiver(
        lon_g, lat_g, field.u[0], field.v[0], color="0.55", angles="xy",
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

    for z in run.zones:
        r_lat = z.radius_km / 110.57
        r_lon = z.radius_km / (111.32 * np.cos(np.radians(z.lat)))
        colour = LEVEL_COLOURS[z.risk_level]
        ax.add_patch(
            Ellipse(
                (z.lon, z.lat), 2 * r_lon, 2 * r_lat, facecolor=colour, edgecolor="black",
                alpha=0.75, lw=0.8, zorder=5,
            )
        )  # fmt: skip
        ax.annotate(
            f"{z.name}\n{z.risk_level.replace('_', ' ')}", (z.lon, z.lat),
            xytext=_LABEL_OFFSETS.get(z.id, (9, 4)), textcoords="offset points", fontsize=7.5,
            zorder=6,
        )  # fmt: skip

    handles = [
        Patch(facecolor=c, edgecolor="black", label=f"{name.replace('_', ' ')} (from {lo:g})")
        for (name, lo), c in zip(config.RISK_LEVEL_THRESHOLDS, LEVEL_COLOURS.values(), strict=True)
    ]
    handles.append(Patch(facecolor="black", label="fire clusters (area ~ FRP)"))
    ax.legend(handles=handles, loc="lower left", fontsize=8, title="peak level", title_fontsize=8)

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
    parser = argparse.ArgumentParser(description="Run FIRMS, winds, trajectories and risk.")
    parser.add_argument("--no-plot", action="store_true", help="skip the PNG")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from hazier.env import load_env
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

    tracemalloc.start()
    t0 = time.perf_counter()
    run = run_model(firms.hotspots, field)
    total = time.perf_counter() - t0
    peak_bytes = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    c = run.clustering
    fates = run.advection.fate_counts()
    shown = run.selection.trajectories
    exported_clusters = clusters_for_export(run.clusters)
    traj_kb, clusters_kb = json_size(shown) / 1024, json_size(exported_clusters) / 1024
    run_time = f"{field.run_init:%Y-%m-%dT%H:%MZ}" if field.run_init else "unknown run"
    n_releases = config.HORIZON_HOURS // config.RELEASE_INTERVAL_HOURS
    k_h = config.HORIZONTAL_DIFFUSIVITY_M2S
    spread = [math.sqrt(2 * k_h * h * 3600) / 1000 for h in (24, 72)]
    regions = {cl.region for cl in run.clusters}

    print(f"\nHazier model  now {now:%Y-%m-%dT%H:%M:%SZ}")
    print(
        f"inputs: {len(firms.hotspots)} hotspots (FIRMS {firms.status.state}), "
        f"winds {field.model} {run_time} ({'cache' if winds.from_cache else 'API'})"
    )
    print(
        f"clusters: {len(run.clusters)} kept of {c.cells} cells ({c.dropped} below "
        f"{config.CLUSTER_MIN_FRP_MW:g} MW, {c.dropped_frp_share:.1%} of FRP), "
        f"in {len(regions)} regions"
    )
    print(
        f"particles: {run.n_particles:,} = {len(run.clusters)} clusters x {n_releases} releases "
        f"x {config.PARTICLES_PER_RELEASE}, plus {run.n_tracers:,} display tracers"
    )
    print(
        f"dispersion: K = {k_h:g} m2/s (spread {spread[0]:.0f} km after 24 h, "
        f"{spread[1]:.0f} km after 72 h), seed {config.RANDOM_SEED}"
    )
    print(
        f"fates: reached {config.HORIZON_HOURS} h {fates['horizon']:,}, left domain "
        f"{fates['left_domain']:,}, stopped on missing wind {fates['missing_wind']:,}"
    )
    print("runtime: " + ", ".join(f"{k} {v:.1f} s" for k, v in run.timings.items()))
    print(f"         total {total:.1f} s (measured with tracemalloc on, which adds some overhead)")
    print(f"peak memory: {peak_bytes / 2**20:.0f} MB (tracemalloc, numpy arrays included)")
    print(
        f"export: {len(shown)} trajectories ({run.selection.near_zone} near zones) and "
        f"{len(exported_clusters)} clusters: {traj_kb:.0f} KB + {clusters_kb:.0f} KB = "
        f"{traj_kb + clusters_kb:.0f} KB"
    )

    print(f"\n{'zone':<19}{'peak':<11}{'index':>8}{'at':>7}  {'ETA':<7}{'range':<15}sources")
    for z in run.zones:
        sources = ", ".join(f"{s.region} {s.share:.0%}" for s in z.sources[:3]) or "-"
        print(
            f"{z.name:<19}{z.risk_level.replace('_', ' '):<11}{z.risk_index:>8.1f}"
            f"{_fmt_hours(z.peak_hour):>7}  {_fmt_hours(z.eta_hours):<7}{_fmt_range(z):<15}{sources}"
        )
    th = ", ".join(f"{k.replace('_', ' ')} >= {v:g}" for k, v in thresholds().items() if v > 0)
    print(f"levels (provisional, uncalibrated): {th}")

    if not args.no_plot:
        title = (
            f"{len(run.clusters)} clusters, {run.n_particles:,} particles, K = {k_h:g} m2/s, "
            f"winds {field.model} {config.PRESSURE_LEVEL_HPA} hPa {run_time}\n"
            f"t0 = {field.times[0]} UTC; shading: smoke summed over 72 h; lines: "
            f"{len(shown)} display trajectories; circles: peak level (provisional)"
        )
        try:
            out = _plot(OUTPUT_DIR / "trajectories.png", field, run, title)
            print(f"plot: {out}")
        except ImportError:
            print('plot skipped: matplotlib is not installed (pip install -e ".[dev]")')
    return 0


if __name__ == "__main__":
    sys.exit(main())
