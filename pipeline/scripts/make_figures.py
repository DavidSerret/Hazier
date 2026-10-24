"""Make the figures of the README and the methodology into docs/figures/.

Every figure comes from real data (FIRMS hotspots, ECMWF open data winds, CAMS PM2.5 for the
zones) run through the real model, except the convergence plot, which uses an analytic test
field so the integrator can be compared with an exact or reference answer.

The inputs of a run are saved as a snapshot (pipeline/.cache/figures/, git-ignored), so the
same figures can be drawn again later from exactly the same data:

    cd pipeline
    .venv\\Scripts\\python.exe scripts\\make_figures.py                       # fetch, snapshot, draw
    .venv\\Scripts\\python.exe scripts\\make_figures.py --snapshot FILE.npz   # redraw a snapshot

Fetching needs FIRMS_MAP_KEY in pipeline/.env (or the environment). Coastlines come from the
Natural Earth outlines already used for the source regions (hazier/data/regions.geojson), so
no mapping library is needed: only matplotlib (a dev dependency).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from hazier import config
from hazier.env import PIPELINE_DIR, load_env
from hazier.model import trajectories as tj
from hazier.model.regions import REGIONS_FILE
from hazier.model.smoke import ModelRun, run_model
from hazier.schema import Hotspot, ZoneForecast
from hazier.sources.cams import CATEGORY_COLOURS, CamsResult, CamsSeries, apply_to_zones
from hazier.sources.winds import WindField, grid_axes

log = logging.getLogger("make_figures")

OUT_DIR = PIPELINE_DIR.parent / "docs" / "figures"
SNAPSHOT_DIR = PIPELINE_DIR / ".cache" / "figures"
EXTRA_LIFETIMES = (24.0, 48.0, 120.0, 168.0)  # 1, 2, 5 and 7 days, next to the default 3

# Dark style, close to the web app.
BG = "#0b0f14"
LAND = "#202a37"
OUTLINE = "#3d4a5c"
PANEL = "#121821"
TEXT = "#d6dde6"
MUTED = "#8b97a6"
GRID = "#26303b"
SMOKE = "#c8c4ff"
FIRE = "#ffd678"
CATEGORY_LABELS = {
    "good": "Good",
    "moderate": "Moderate",
    "unhealthy": "Unhealthy",
    "very_unhealthy": "Very unhealthy",
    "hazardous": "Hazardous",
}
# Label offsets (points) for the crowded peninsula.
LABEL_OFFSETS = {
    "klang_valley": (-10, 6, "right"),
    "melaka_seremban": (-10, -12, "right"),
    "johor_bahru": (10, 4, "left"),
    "singapore": (10, -12, "left"),
    "ipoh": (10, 2, "left"),
    "penang": (-10, 2, "right"),
    "kuantan": (10, 2, "left"),
    "kuching": (10, 2, "left"),
}
MYT = timedelta(hours=8)


# --- Snapshot ------------------------------------------------------------------------------


def take_snapshot(now: datetime, path: Path) -> Path:
    """Fetch the real inputs and save them. Exits if FIRMS or the winds are unavailable."""
    from hazier.sources.cams import fetch_cams
    from hazier.sources.firms import fetch_hotspots
    from hazier.sources.winds import fetch_winds

    load_env()
    firms = fetch_hotspots(now=now)
    if not firms.status.usable:
        sys.exit(f"FIRMS failed: {firms.status.detail}")
    winds = fetch_winds(now=now)
    if winds.field is None:
        sys.exit(f"Winds failed: {winds.status.detail}")
    cams = fetch_cams(now=now)
    f = winds.field
    meta = {
        "now": now.isoformat(),
        "wind_source": f.source,
        "wind_model": f.model,
        "wind_run": f.run_init.isoformat() if f.run_init else None,
        "firms_state": firms.status.state,
        "cams_state": cams.status.state,
    }
    cams_json = {
        "times": [str(t) for t in cams.times] if cams.times is not None else [],
        "series": {k: [s.hourly.tolist(), s.basis.tolist()] for k, s in cams.series.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        meta=json.dumps(meta),
        hotspots=json.dumps([asdict(h) for h in firms.hotspots]),
        cams=json.dumps(cams_json),
        times=f.times.astype("int64"),
        lats=f.lats,
        lons=f.lons,
        u=f.u.astype(np.float32),
        v=f.v.astype(np.float32),
        t0_index=f.t0_index,
        level=f.level_hpa,
    )
    return path


def load_snapshot(path: Path) -> tuple[dict, list[Hotspot], WindField, CamsResult]:
    with np.load(path) as z:
        meta = json.loads(str(z["meta"]))
        hotspots = [Hotspot(**h) for h in json.loads(str(z["hotspots"]))]
        cams_json = json.loads(str(z["cams"]))
        run = meta["wind_run"]
        field = WindField(
            model=meta["wind_model"],
            run_init=datetime.fromisoformat(run) if run else None,
            level_hpa=int(z["level"]),
            times=z["times"].astype("datetime64[s]"),
            lats=z["lats"],
            lons=z["lons"],
            u=z["u"].astype(float),
            v=z["v"].astype(float),
            t0_index=int(z["t0_index"]),
            source=meta["wind_source"],
        )
    from hazier.schema import SourceStatus

    times = np.array(cams_json["times"], dtype="datetime64[s]") if cams_json["times"] else None
    series = {k: CamsSeries(np.array(a), np.array(b)) for k, (a, b) in cams_json["series"].items()}
    cams = CamsResult(times, series, SourceStatus("cams", meta["cams_state"], None))
    return meta, hotspots, field, cams


# --- Drawing helpers -----------------------------------------------------------------------


def setup_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.facecolor": BG,
            "axes.facecolor": BG,
            "savefig.facecolor": BG,
            "axes.edgecolor": GRID,
            "axes.labelcolor": TEXT,
            "axes.titlecolor": TEXT,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "text.color": TEXT,
            "grid.color": GRID,
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "legend.facecolor": PANEL,
            "legend.edgecolor": GRID,
            "legend.labelcolor": TEXT,
            "font.family": ["DejaVu Sans"],
        }
    )
    return plt


def land_rings() -> list[np.ndarray]:
    """Outer rings of the Natural Earth admin-1 outlines, as (N, 2) lon/lat arrays."""
    data = json.loads(Path(REGIONS_FILE).read_text(encoding="utf-8"))
    rings = []
    for feat in data["features"]:
        geom = feat["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        rings += [np.array(poly[0]) for poly in polys]
    return rings


def draw_land(ax, rings) -> None:
    """Land filled without edges: the coastline shows, internal borders do not."""
    from matplotlib.patches import Polygon

    for ring in rings:
        ax.add_patch(Polygon(ring, closed=True, facecolor=LAND, edgecolor="none", zorder=0))


def map_figure(plt, width_frac=0.86, fig_width=11.0, top_in=1.05, bottom_in=0.55, left=0.075):
    """A figure whose axes have exactly the map's aspect, so no space is wasted around it."""
    span_x, span_y = config.LON_MAX - config.LON_MIN, config.LAT_MAX - config.LAT_MIN
    map_w = width_frac * fig_width
    map_h = map_w * span_y / span_x
    height = map_h + top_in + bottom_in
    fig = plt.figure(figsize=(fig_width, height))
    ax = fig.add_axes((left, bottom_in / height, width_frac, map_h / height))
    return fig, ax, height


def titles(fig, height: float, title: str, subtitle: str, left: float = 0.075) -> None:
    fig.text(left, 1 - 0.3 / height, title, fontsize=15, fontweight="bold", va="top")
    fig.text(left, 1 - 0.66 / height, subtitle, fontsize=10, color=MUTED, va="top")


def draw_outlines(ax, rings) -> None:
    """Faint land outlines (coasts and provinces) drawn over the smoke, for orientation."""
    from matplotlib.collections import LineCollection

    ax.add_collection(LineCollection(rings, colors=OUTLINE, linewidths=0.35, alpha=0.7, zorder=2.5))


def map_axes(ax, extent=(config.LON_MIN, config.LON_MAX, config.LAT_MIN, config.LAT_MAX)):
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect("equal")
    ax.set_xticks(range(int(extent[0]), int(extent[1]) + 1, 5))
    ax.set_yticks(range(int(math.ceil(extent[2] / 4) * 4), int(extent[3]) + 1, 4))
    ax.set_xticklabels([f"{x}°E" for x in ax.get_xticks().astype(int)])
    ax.set_yticklabels(
        [f"{abs(y)}°{'N' if y > 0 else 'S' if y < 0 else ''}" for y in ax.get_yticks().astype(int)]
    )
    for s in ax.spines.values():
        s.set_color(GRID)


def footer(fig, text: str) -> None:
    fig.text(0.01, 0.01, text, fontsize=8.5, color=MUTED, ha="left", va="bottom")


def t0_label(field: WindField) -> str:
    t0 = field.t0.astype("datetime64[s]").astype(datetime).replace(tzinfo=UTC)
    return f"{t0:%d %b %Y %H:%M} UTC ({t0 + MYT:%H:%M} MYT)"


# --- (a) Hero map --------------------------------------------------------------------------


def fig_hero(plt, run: ModelRun, zones: list[ZoneForecast], field: WindField, meta, rings, out):
    from matplotlib.collections import LineCollection
    from matplotlib.colors import LinearSegmentedColormap, LogNorm
    from matplotlib.lines import Line2D

    fig, ax, height = map_figure(plt)
    draw_land(ax, rings)

    # Plumes: smoke over the 72 h forecast, after removal, log scale.
    load = run.density.load
    smoke = LinearSegmentedColormap.from_list(
        "smoke", [(0.55, 0.52, 0.85, 0.0), (0.62, 0.58, 0.95, 0.45), (0.92, 0.9, 1.0, 0.9)]
    )
    if load.max() > 0:
        floor = load.max() * 1e-3
        masked = np.ma.masked_less_equal(load, floor)
        ax.imshow(
            masked, origin="lower", cmap=smoke, norm=LogNorm(vmin=floor, vmax=load.max()),
            extent=(config.LON_MIN, config.LON_MAX, config.LAT_MIN, config.LAT_MAX),
            interpolation="bilinear", zorder=1,
        )  # fmt: skip

    draw_outlines(ax, rings)
    segments = [np.array(t.path) for t in run.selection.trajectories if len(t.path) > 1]
    ax.add_collection(LineCollection(segments, colors=SMOKE, linewidths=0.6, alpha=0.4, zorder=2.6))

    frp = np.array([c.frp_mw for c in run.clusters])
    lon = np.array([c.lon for c in run.clusters])
    lat = np.array([c.lat for c in run.clusters])
    size = 4 + 60 * np.sqrt(frp / frp.max())
    ax.scatter(lon, lat, s=size * 7, color=(1, 0.35, 0.08, 0.10), linewidths=0, zorder=3)
    ax.scatter(lon, lat, s=size * 2.5, color=(1, 0.55, 0.12, 0.28), linewidths=0, zorder=3)
    ax.scatter(lon, lat, s=size * 0.6, color=FIRE, linewidths=0, zorder=4)

    for z in zones:
        cat = z.cams_now.category if z.cams_now else None
        ax.scatter([z.lon], [z.lat], s=110, color=CATEGORY_COLOURS.get(cat, "#787e8a"),
                   edgecolors="white", linewidths=1.3, zorder=6)  # fmt: skip
        dx, dy, ha = LABEL_OFFSETS.get(z.id, (10, 2, "left"))
        ax.annotate(z.name, (z.lon, z.lat), xytext=(dx, dy), textcoords="offset points",
                    ha=ha, fontsize=10, fontweight="bold", color="white", zorder=7)  # fmt: skip

    map_axes(ax)
    handles = [
        Line2D([], [], color=SMOKE, lw=1.2, alpha=0.8, label="Smoke trajectories (next 72 h)"),
        Line2D([], [], marker="s", ls="", ms=10, color=(0.75, 0.72, 0.98, 0.6), label="Smoke plumes (72 h total)"),
        Line2D([], [], marker="o", ls="", ms=7, color=FIRE, label="Fire clusters, size by FRP"),
    ] + [
        Line2D([], [], marker="o", ls="", ms=8, color=CATEGORY_COLOURS[c], markeredgecolor="white",
               label=f"Zone: {CATEGORY_LABELS[c]}")
        for c in ("good", "moderate", "unhealthy", "very_unhealthy")
    ]  # fmt: skip
    ax.legend(handles=handles, loc="lower left", fontsize=9, framealpha=0.9)
    titles(fig, height, "Where the smoke goes: fires of the last 48 h, carried 72 h ahead",
           f"Forecast from {t0_label(field)}. Zone colour: CAMS PM2.5 category now "
           "(24 h mean, provisional limits).")  # fmt: skip
    footer(
        fig,
        f"{len(run.clusters)} fire clusters (NASA FIRMS VIIRS), {run.n_particles:,} particles, "
        f"850 hPa winds ECMWF IFS ({field.source}), CAMS PM2.5. Land: Natural Earth.",
    )
    fig.savefig(out, dpi=110)
    plt.close(fig)


# --- (b) Equatorial recurvature ------------------------------------------------------------


def fig_recurvature(plt, field: WindField, rings, out):
    fig, ax, height = map_figure(plt, width_frac=0.8)
    draw_land(ax, rings)
    u, v = field.u.mean(axis=0), field.v.mean(axis=0)
    speed = np.hypot(u, v)
    strm = ax.streamplot(
        field.lons, field.lats, u, v, color=speed, cmap="cool", density=1.6, linewidth=1.0,
        arrowsize=1.1, zorder=2,
    )  # fmt: skip
    pos = ax.get_position()
    cax = fig.add_axes((pos.x1 + 0.015, pos.y0 + pos.height * 0.12, 0.018, pos.height * 0.76))
    cb = fig.colorbar(strm.lines, cax=cax)
    cb.set_label("Mean wind speed (m/s)")
    cb.outline.set_edgecolor(GRID)
    ax.axhline(0, color="#f5c26b", lw=1.2, ls="--", zorder=3)
    ax.text(95.4, 0.25, "Equator", color="#f5c26b", fontsize=10, zorder=5)

    box = {"boxstyle": "round,pad=0.4", "facecolor": PANEL, "edgecolor": GRID, "alpha": 0.92}
    ax.annotate(
        "South of the equator:\nsouth-easterly trades\n(blowing towards the north-west)",
        xy=(108, -4.5), xytext=(111.5, -6.7), fontsize=10, bbox=box, zorder=6,
        arrowprops={"arrowstyle": "->", "color": TEXT, "lw": 1.2},
    )  # fmt: skip
    ax.annotate(
        "North of the equator the Coriolis force\nchanges sign and the flow recurves:\n"
        "south-westerlies (blowing towards the\nnorth-east), from Sumatra to Malaysia",
        xy=(102.5, 3.2), xytext=(104.5, 8.6), fontsize=10, bbox=box, zorder=6,
        arrowprops={"arrowstyle": "->", "color": TEXT, "lw": 1.2},
    )  # fmt: skip
    map_axes(ax)
    t_first = field.times[0].astype("datetime64[s]").astype(datetime)
    t_last = field.times[-1].astype("datetime64[s]").astype(datetime)
    titles(fig, height, "Why Sumatran smoke reaches Malaysia: the winds recurve at the equator",
           f"Streamlines of the mean 850 hPa wind, {t_first:%d %b %H:%M} to {t_last:%d %b %H:%M} "
           "UTC (ECMWF IFS, 0.25°).")  # fmt: skip
    footer(fig, "Winds: ECMWF open data (CC BY 4.0). Land: Natural Earth.")
    fig.savefig(out, dpi=110)
    plt.close(fig)


# --- (c) Convergence -----------------------------------------------------------------------

R = 6_371_000.0
CENTRE = (2.0, 108.0)


def _field(fn) -> WindField:
    lats, lons = grid_axes()
    h = np.arange(0, config.HORIZON_HOURS + 1, dtype=float)
    hh, la, lo = np.meshgrid(h, lats, lons, indexing="ij")
    u, v = fn(la, lo, hh)
    times = np.datetime64("2026-01-01T00:00:00", "s") + (h * 3600).astype("int64").astype(
        "timedelta64[s]"
    )
    shape = hh.shape
    return WindField("test", None, 850, times, lats, lons, np.broadcast_to(u, shape).astype(float),
                     np.broadcast_to(v, shape).astype(float))  # fmt: skip


def _rotation(lat, lon, hour):
    """Rotation around (2 N, 108 E) strengthening in time; linear, so interpolation is exact."""
    k = 2.5 * (1 + 0.3 * hour / 72)
    return -k * (lat - CENTRE[0]), k * (lon - CENTRE[1])


def _rk4_reference(lat, lon, dt=1 / 256, hours=72):
    def f(la, lo, t):
        u, v = _rotation(la, lo, t)
        c = 3600 / R * 180 / math.pi
        return u * c / math.cos(math.radians(la)), v * c

    t = 0.0
    for _ in range(round(hours / dt)):
        k1 = f(lat, lon, t)
        k2 = f(lat + dt / 2 * k1[1], lon + dt / 2 * k1[0], t + dt / 2)
        k3 = f(lat + dt / 2 * k2[1], lon + dt / 2 * k2[0], t + dt / 2)
        k4 = f(lat + dt * k3[1], lon + dt * k3[0], t + dt)
        lon += dt / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0])
        lat += dt / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1])
        t += dt
    return lat, lon


def _km(lat1, lon1, lat2, lon2):
    dx = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return R / 1000 * math.hypot(dx, math.radians(lat2 - lat1))


def _one(lat, lon):
    return tj.Particles(
        np.array([0]), np.array([0.0]), np.array([lat]), np.array([lon]), np.array([1.0])
    )


def convergence_errors(dts) -> tuple[list[float], list[float]]:
    """Errors (km) after 72 h: rotating field against RK4, uniform wind against the rhumb line."""
    rot = _field(_rotation)
    ref = _rk4_reference(2.0, 111.0)
    rot_err = []
    for dt in dts:
        tr = tj.trace(rot, _one(2.0, 111.0), dt_hours=dt, diffusivity_m2s=0.0)
        rot_err.append(_km(tr.lat[0, -1], tr.lon[0, -1], *ref))

    u, v, lat0, lon0 = 4.0, 3.0, -4.0, 100.0
    uni = _field(lambda la, lo, h: (np.full(la.shape, u), np.full(la.shape, v)))
    phi0 = math.radians(lat0)
    phi = phi0 + v * 72 * 3600 / R

    def merc(p):
        return math.log(math.tan(math.pi / 4 + p / 2))

    lam = math.radians(lon0) + (u / v) * (merc(phi) - merc(phi0))
    uni_err = []
    for dt in dts:
        tr = tj.trace(uni, _one(lat0, lon0), dt_hours=dt, diffusivity_m2s=0.0)
        uni_err.append(_km(tr.lat[0, -1], tr.lon[0, -1], math.degrees(phi), math.degrees(lam)))
    return rot_err, uni_err


def fig_convergence(plt, out) -> dict:
    dts = [4.0, 2.0, 1.0, 0.5, 0.25, 0.125]
    rot, uni = convergence_errors(dts)
    fig, ax = plt.subplots(figsize=(10, 6.6))
    fig.subplots_adjust(left=0.11, right=0.97, top=0.85, bottom=0.13)
    ax.loglog(
        dts,
        rot,
        "o-",
        color="#7dd3fc",
        lw=2,
        ms=7,
        label="Rotating, time-varying wind vs RK4 reference",
    )
    ax.loglog(dts, uni, "s-", color="#fbbf24", lw=2, ms=7, label="Uniform wind vs exact rhumb line")
    ref = 0.25 * rot[2] * (np.array(dts) / dts[2]) ** 2  # offset below the data to stay visible
    ax.loglog(dts, ref, "--", color=MUTED, lw=1.5, label="Slope 2 reference (error ∝ Δt²)")
    ax.axvline(1.0, color=GRID, lw=1)
    ax.text(1.04, rot[-1] * 1.5, "operational\nstep, 1 h", color=MUTED, fontsize=9)
    ax.set_xticks(dts, [f"{d:g}" for d in dts])
    ax.minorticks_off()
    ax.set_xlabel("Time step Δt (hours)")
    ax.set_ylabel("Position error after 72 h (km)")
    ax.grid(True, which="both", alpha=0.35)
    ax.legend(loc="upper left", fontsize=10)
    ratios = [rot[i] / rot[i + 1] for i in range(len(dts) - 1)]
    fig.suptitle(
        "The Heun integrator is second order", x=0.11, ha="left", fontsize=15, fontweight="bold"
    )
    fig.text(0.11, 0.875, f"Halving Δt divides the error by {np.mean(ratios[1:]):.2f} (expected 4). "
             f"At Δt = 1 h: {rot[2]:.1f} km after more than a full turn.", fontsize=10, color=MUTED)  # fmt: skip
    footer(fig, "Dispersion off. Both test fields are linear in space and time, so the wind "
           "interpolation is exact and only the integrator is measured.")  # fmt: skip
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return {"dts": dts, "rotating_km": rot, "uniform_km": uni}


# --- (d) Removal lifetime sensitivity ------------------------------------------------------


def fig_tau(plt, run: ModelRun, out) -> dict:
    by_tau = {config.REMOVAL_LIFETIME_HOURS: run.smoke, **run.sensitivity}
    taus = sorted(by_tau)
    default = {s.zone.id: s for s in run.smoke}
    zones = [z for z in config.ZONES if default[z.id].eta is not None]
    if not zones:  # no arrival anywhere: show the zones with the most smoke instead
        zones = sorted(config.ZONES, key=lambda z: -default[z.id].peak)[:4]
    names = [z.name for z in zones][::-1]
    zids = [z.id for z in zones][::-1]
    colours = plt.get_cmap("plasma")(np.linspace(0.15, 0.85, len(taus)))

    fig, axes = plt.subplots(1, 3, figsize=(12, 0.9 * len(zones) + 3.2), sharey=True)
    fig.subplots_adjust(left=0.16, right=0.98, top=0.78, bottom=0.2, wspace=0.12)
    y = np.arange(len(zids))
    off = np.linspace(-0.3, 0.3, len(taus))
    table = {}
    for k, tau in enumerate(taus):
        by_id = {s.zone.id: s for s in by_tau[tau]}
        eta = [by_id[i].eta if by_id[i].eta is not None else np.nan for i in zids]
        main = [default[i].sources[0].region if default[i].sources else None for i in zids]
        share = [
            100 * next((s.share for s in by_id[i].sources if s.region == m), 0.0) if m else np.nan
            for i, m in zip(zids, main, strict=True)
        ]
        rel = [by_id[i].peak / default[i].peak if default[i].peak else np.nan for i in zids]
        label = f"τ = {tau / 24:g} d" + (
            " (default)" if tau == config.REMOVAL_LIFETIME_HOURS else ""
        )
        axes[0].scatter(eta, y + off[k], color=colours[k], s=45, label=label, zorder=3)
        axes[1].barh(y + off[k], share, height=0.6 / len(taus), color=colours[k])
        axes[2].barh(y + off[k], rel, height=0.6 / len(taus), color=colours[k])
        table[tau] = {"eta": eta, "share": share, "relative_peak": rel}
    mains = [default[i].sources[0].region if default[i].sources else "-" for i in zids]
    axes[0].set_yticks(y, [f"{n}\n({m})" for n, m in zip(names, mains, strict=True)], fontsize=10)
    axes[0].set_xlabel("Smoke ETA (hours after t0)")
    axes[0].set_xlim(-2, max(12, np.nanmax([np.nanmax(t["eta"]) for t in table.values()]) + 3))
    axes[1].set_xlabel("Share of the main source region (%)")
    axes[1].set_xlim(0, 100)
    axes[2].set_xlabel("Peak smoke index, relative to τ = 3 d")
    axes[2].axvline(1.0, color=MUTED, lw=1, ls="--")
    for ax in axes:
        ax.grid(True, axis="x", alpha=0.35)
        ax.set_facecolor(BG)
    fig.legend(*axes[0].get_legend_handles_labels(), loc="upper left", ncol=len(taus),
               bbox_to_anchor=(0.16, 0.89), fontsize=9.5, frameon=False)  # fmt: skip
    fig.suptitle("Removal lifetime changes how much smoke, not when or from where", x=0.16,
                 ha="left", fontsize=15, fontweight="bold")  # fmt: skip
    footer(fig, "Same fires and winds, smoke removal lifetime τ from 1 to 7 days. Zones with a "
           "smoke arrival under the default τ; main source region in brackets.")  # fmt: skip
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return {"zones": zids, "taus": taus, "table": table}


# --- (e) One zone --------------------------------------------------------------------------


def pick_zone(zones: list[ZoneForecast]) -> ZoneForecast:
    """A zone whose smoke arrives later in the forecast, else one where it is present, else KL."""
    with_cams = [z for z in zones if z.cams_pm25]
    later = [z for z in with_cams if z.smoke_eta_hours]
    if later:
        return min(later, key=lambda z: z.smoke_eta_hours)
    present = [z for z in with_cams if z.smoke_eta_hours == 0]
    if present:
        return max(present, key=lambda z: z.cams_now.basis_ugm3 if z.cams_now else 0)
    return next(z for z in zones if z.id == config.DEFAULT_ZONE_ID)


def fig_zone(
    plt, zones: list[ZoneForecast], cams: CamsResult, run: ModelRun, field: WindField, out
):
    zone = pick_zone(zones)
    series = cams.series[zone.id]
    t0 = cams.times[0].astype("datetime64[s]").astype(datetime)
    hours = np.arange(len(cams.times))
    fig, ax = plt.subplots(figsize=(11, 6.4))
    fig.subplots_adjust(left=0.08, right=0.84, top=0.84, bottom=0.14)

    top = max(90.0, float(np.nanmax(series.hourly)) * 1.15)
    lower = 0.0
    for name, upper in config.PM25_CATEGORY_UPPER_BOUNDS:
        hi = min(upper, top)
        ax.axhspan(lower, hi, color=CATEGORY_COLOURS[name], alpha=0.13, lw=0, zorder=0)
        if lower < top:
            ax.text(73.5, (lower + hi) / 2, CATEGORY_LABELS[name], va="center", fontsize=9.5,
                    color=CATEGORY_COLOURS[name], clip_on=False)  # fmt: skip
        lower = upper
        if upper >= top:
            break
    ax.plot(hours, series.hourly, color="#9fb3c8", lw=1.2, alpha=0.8, label="CAMS PM2.5, hourly")
    ax.plot(
        hours, series.basis, color="white", lw=2.6, label="CAMS PM2.5, 24 h mean (category basis)"
    )

    eta, lo, hi = zone.smoke_eta_hours, zone.smoke_eta_earliest_hours, zone.smoke_eta_latest_hours
    if eta is not None:
        ax.axvspan(lo, hi if hi is not None else 72, color=SMOKE, alpha=0.18, lw=0, zorder=1)
        ax.axvline(eta, color=SMOKE, lw=2, ls="--", zorder=2)
        src = ", ".join(f"{s.region} {s.share:.0%}" for s in zone.smoke_sources[:2])
        when = "already present at t0" if eta == 0 else f"expected in {eta:g} h"
        span = "" if hi == lo else f"\nrange {lo:g} to {f'{hi:g}' if hi is not None else '>72'} h"
        box = {"boxstyle": "round,pad=0.45", "facecolor": PANEL, "edgecolor": SMOKE, "alpha": 0.95}
        x_text = max(eta, lo or 0) + 14
        ax.text(x_text, top * 0.95, f"Hazier: smoke {when}\nfrom {src}{span}", color=SMOKE,
                fontsize=10, va="top", bbox=box, zorder=5)  # fmt: skip
        ax.annotate("", xy=(eta + 0.3, top * 0.87), xytext=(x_text - 0.4, top * 0.87),
                    arrowprops={"arrowstyle": "->", "color": SMOKE, "lw": 1.3}, zorder=5)  # fmt: skip
    else:
        ax.text(
            1,
            top * 0.93,
            "No smoke arrival expected from the detected fires",
            color=SMOKE,
            fontsize=10,
            va="top",
        )

    ticks = list(range(0, 73, 12))
    ax.set_xticks(ticks, [f"{t0 + timedelta(hours=h) + MYT:%a %H:%M}" for h in ticks])
    ax.set_xlim(0, 72)
    ax.set_ylim(0, top)
    ax.set_xlabel("Malaysia time (MYT)")
    ax.set_ylabel("PM2.5 (µg/m³)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=9.5, bbox_to_anchor=(0.995, 0.75))
    fig.suptitle(f"{zone.name}: how much PM2.5 (CAMS) and when the smoke arrives (Hazier)",
                 x=0.08, ha="left", fontsize=15, fontweight="bold")  # fmt: skip
    fig.text(0.08, 0.875, f"Next 72 h from {t0_label(field)}. Category bands: Malaysian API, "
             "provisional limits.", fontsize=10, color=MUTED)  # fmt: skip
    footer(fig, "PM2.5: CAMS global forecast (Copernicus) via Open-Meteo. Smoke ETA: Hazier "
           "trajectories from NASA FIRMS fires and ECMWF winds.")  # fmt: skip
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return zone.id


# --- Main ----------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Make the README figures into docs/figures/.")
    parser.add_argument("--snapshot", type=Path, help="redraw from this snapshot (.npz)")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help=f"output folder ({OUT_DIR})")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    path = args.snapshot
    if path is None:
        now = datetime.now(UTC)
        path = take_snapshot(now, SNAPSHOT_DIR / f"snapshot-{now:%Y%m%dT%H%M}.npz")
        print(f"snapshot: {path}")
    meta, hotspots, field, cams = load_snapshot(path)
    run = run_model(hotspots, field, extra_lifetimes=EXTRA_LIFETIMES)
    zones = apply_to_zones(run.zones, cams)

    plt = setup_matplotlib()
    rings = land_rings()
    args.out.mkdir(parents=True, exist_ok=True)
    fig_hero(plt, run, zones, field, meta, rings, args.out / "hero-map.png")
    fig_recurvature(plt, field, rings, args.out / "equatorial-recurvature.png")
    conv = fig_convergence(plt, args.out / "convergence.png")
    tau = fig_tau(plt, run, args.out / "tau-sensitivity.png")
    zone_id = fig_zone(plt, zones, cams, run, field, args.out / "zone-example.png")

    print(f"t0 {field.t0} UTC, {len(hotspots)} hotspots, {len(run.clusters)} clusters, "
          f"{run.n_particles:,} particles; winds {meta['wind_source']} {meta['wind_run']}")  # fmt: skip
    print(
        "convergence (km):",
        ", ".join(
            f"dt {d:g} h: {e:.3f}" for d, e in zip(conv["dts"], conv["rotating_km"], strict=True)
        ),
    )
    print("uniform wind (km):", ", ".join(f"{e:.2e}" for e in conv["uniform_km"]))
    for t in tau["taus"]:
        row = tau["table"][t]
        print(f"tau {t / 24:g} d: eta {row['eta']}, share {[round(s, 1) for s in row['share']]}, "
              f"rel peak {[round(r, 2) for r in row['relative_peak']]}")  # fmt: skip
    print(f"zones in tau figure: {tau['zones']}; zone example: {zone_id}")
    for z in zones:
        print(f"  {z.name:<18} ETA {z.smoke_eta_hours} ({z.smoke_eta_earliest_hours} to "
              f"{z.smoke_eta_latest_hours}) {[(s.region, s.share) for s in z.smoke_sources[:2]]} "
              f"CAMS {z.cams_now.basis_ugm3 if z.cams_now else None}")  # fmt: skip
    print(f"figures in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
