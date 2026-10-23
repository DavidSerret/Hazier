"""The pipeline: fetch every source, run the smoke model, write forecast.json and the history.

`python -m hazier` runs it. One run:

1. FIRMS hotspots, winds (past 48 h and next 72 h), CAMS PM2.5 for the zones and the map grid.
2. The smoke model (model/smoke.py), then the CAMS fields of each zone.
3. A Forecast, validated against the schema (schema.validate_forecast), written atomically to
   pipeline/output/site/forecast.json, which the workflow publishes to GitHub Pages.
4. One line appended to data/history/runs.csv.
5. A one-screen summary.

Fallback
--------
A source that fails is marked in source_status and the run goes on with what is available:
- CAMS failing (zones, map grid or both): the forecast is still written, without the CAMS
  fields or the map layer, and source_status says so; the smoke timing is unaffected.
- FIRMS or winds failing: no smoke forecast can be made. Nothing is written, the previous
  forecast.json is left untouched, and the process exits with code 1.
- A forecast that fails validation is not written either (exit code 2).
The history line is appended in every case, so the record shows failed runs too.

How the previous forecast survives on GitHub Pages: the simplest option is to do nothing. A
Pages site keeps serving its last deployment until the next one, so the workflow just stops
before deploying when this exits non-zero, and the last valid forecast.json stays online. No
download is needed. The web should call a forecast stale when generated_at is older than about
9 h (one missed 6-hourly run plus margin).

last_ok of a failed source comes from the history: the run time of the last line where that
source was usable.

Exit codes: 0 forecast written, 1 no usable forecast (previous kept), 2 validation failed
(previous kept).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from hazier import config
from hazier.env import PIPELINE_DIR, load_env
from hazier.model.regions import region_names
from hazier.model.smoke import ModelRun, fmt_eta, run_model, smoke_model_metadata
from hazier.model.trajectories import clusters_for_export
from hazier.schema import Forecast, Hotspot, SourceStatus, ZoneForecast, validate_forecast
from hazier.sources.cams import (
    CamsGridResult,
    CamsResult,
    apply_to_zones,
    attributions,
    baseline_metadata,
    fetch_cams,
    fetch_cams_grid,
    grid_for_export,
)
from hazier.sources.firms import fetch_hotspots
from hazier.sources.winds import WindField, fetch_winds

log = logging.getLogger(__name__)

REPO_DIR = PIPELINE_DIR.parent
OUTPUT_DIR = PIPELINE_DIR / "output" / config.OUTPUT_DIRNAME
HISTORY_FILE = REPO_DIR / config.HISTORY_RELPATH

EXIT_OK, EXIT_NO_FORECAST, EXIT_INVALID = 0, 1, 2
HISTORY_SOURCES = ("firms", "winds", "cams")


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Building the forecast -----------------------------------------------------------------


def hotspots_for_export(hotspots: list[Hotspot], limit: int | None = None) -> list[Hotspot]:
    """The strongest hotspots by FRP (EXPORT_HOTSPOTS_MAX), rounded, with their region."""
    limit = config.EXPORT_HOTSPOTS_MAX if limit is None else limit
    strongest = sorted(hotspots, key=lambda h: (-h.frp_mw, h.acquired_at, h.id))[:limit]
    if not strongest:
        return []
    regions = region_names([h.lat for h in strongest], [h.lon for h in strongest])
    out = [
        replace(h, lat=round(h.lat, 3), lon=round(h.lon, 3), frp_mw=round(h.frp_mw, 1), region=r)
        for h, r in zip(strongest, regions, strict=True)
    ]
    return sorted(out, key=lambda h: (h.acquired_at, h.id))


def cams_status(zones: CamsResult, grid: CamsGridResult) -> SourceStatus:
    """One "cams" status for the zone values and the map layer together."""
    z, g = zones.status, grid.status
    if z.state == "ok" and g.state == "ok":
        return z
    parts = []
    if z.state != "ok":
        parts.append(f"zones: {z.detail or z.state}")
    if g.state != "ok":
        parts.append(f"map layer: {g.detail or g.state}")
    state = "failed" if z.state == "failed" and g.state == "failed" else "degraded"
    return SourceStatus("cams", state, z.last_ok or g.last_ok, "; ".join(parts)[:200])


def build_forecast(
    hotspots: list[Hotspot],
    field_: WindField,
    model: ModelRun,
    cams_zones: CamsResult,
    grid: CamsGridResult,
    statuses: list[SourceStatus],
) -> Forecast:
    """The Forecast from the results of every step. generated_at is t0, the current hour."""
    return Forecast(
        generated_at=f"{field_.t0}Z",
        horizon_hours=config.HORIZON_HOURS,
        hotspots=hotspots_for_export(hotspots),
        clusters=clusters_for_export(model.clusters),
        trajectories=model.selection.trajectories,
        zones=apply_to_zones(model.zones, cams_zones),
        smoke_model=smoke_model_metadata(field_),
        source_status=statuses,
        attributions=attributions(),
        pm25_baseline=baseline_metadata() if cams_zones.series else None,
        pm25_grid=grid_for_export(grid),
    )


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file, so a crash never leaves a half-written forecast."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --- History -------------------------------------------------------------------------------


def history_header() -> list[str]:
    columns = ["run_at", "t0", "outcome", *HISTORY_SOURCES, "hotspots", "clusters"]
    for z in config.ZONES:
        columns += [f"{z.id}_pm25", f"{z.id}_eta", f"{z.id}_source"]
    return columns


def history_row(report: RunReport) -> dict[str, str]:
    """One CSV line: run, source states, and per zone CAMS now (24 h mean), smoke ETA, top source."""
    row = {
        "run_at": _iso(report.run_at),
        "t0": report.t0 or "",
        "outcome": report.outcome,
        "hotspots": str(report.counts.get("hotspots", "")),
        "clusters": str(report.counts.get("clusters", "")),
    }
    for name in HISTORY_SOURCES:
        status = report.statuses.get(name)
        row[name] = status.state if status else ""
    by_id = {z.id: z for z in report.zones}
    for zone in config.ZONES:
        z = by_id.get(zone.id)
        row[f"{zone.id}_pm25"] = f"{z.cams_now.basis_ugm3:g}" if z and z.cams_now else ""
        eta = z.smoke_eta_hours if z else None
        row[f"{zone.id}_eta"] = "" if eta is None else f"{eta:g}"
        row[f"{zone.id}_source"] = z.smoke_sources[0].region if z and z.smoke_sources else ""
    return row


def append_history(path: Path, row: dict[str, str]) -> int:
    """Append a row, writing the header for a new file. Returns the number of data rows.

    If the columns changed (for example a zone was added), the old file is kept aside with a
    date in its name and a new one is started, so no row is ever misaligned.
    """
    header = history_header()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        with path.open(encoding="utf-8", newline="") as f:
            first = next(csv.reader(f), None)
        if first != header:
            aside = path.with_name(f"{path.stem}-until-{row['run_at'][:10]}{path.suffix}")
            path.rename(aside)
            log.warning("History columns changed: kept the old file as %s", aside.name)
    new = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if new:
            writer.writeheader()
        writer.writerow(row)
    with path.open(encoding="utf-8", newline="") as f:
        return sum(1 for _ in f) - 1


def last_ok_from_history(path: Path) -> dict[str, str]:
    """Per source, the run time of the last history line where it was usable."""
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    try:
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                for name in HISTORY_SOURCES:
                    if row.get(name) in ("ok", "degraded"):
                        found[name] = row["run_at"]
    except (OSError, csv.Error) as exc:
        log.warning("History unreadable (%s), last_ok not merged", type(exc).__name__)
    return found


# --- The run -------------------------------------------------------------------------------


@dataclass
class RunReport:
    run_at: datetime
    outcome: str = "no_forecast"  # written, no_forecast or invalid
    exit_code: int = EXIT_NO_FORECAST
    t0: str | None = None
    statuses: dict[str, SourceStatus] = field(default_factory=dict)
    forecast_path: Path | None = None
    size_bytes: int = 0
    gzip_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    zones: list[ZoneForecast] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    history_file: Path | None = None
    history_rows: int = 0
    total_s: float = 0.0


def run_pipeline(
    now: datetime | None = None,
    output_dir: Path | None = None,
    history_file: Path | None = None,
    write_history: bool = True,
) -> RunReport:
    """One full run. Never raises; the outcome and exit code are in the report."""
    start = time.perf_counter()
    now = now or datetime.now(UTC)
    output_dir = OUTPUT_DIR if output_dir is None else output_dir
    history_file = HISTORY_FILE if history_file is None else history_file
    report = RunReport(run_at=now)
    load_env()

    def timed(name, fn, *args, **kwargs):
        t = time.perf_counter()
        result = fn(*args, **kwargs)
        report.timings[name] = time.perf_counter() - t
        return result

    firms = timed("FIRMS", fetch_hotspots, now=now)
    winds = timed("winds", fetch_winds, now=now)
    cams_zones = timed("CAMS zones", fetch_cams, now=now)
    grid = timed("CAMS grid", fetch_cams_grid, now=now)

    previous = last_ok_from_history(history_file)
    statuses = [firms.status, winds.status, cams_status(cams_zones, grid)]
    statuses = [
        replace(s, last_ok=previous.get(s.name))
        if s.last_ok is None and previous.get(s.name)
        else s
        for s in statuses
    ]
    report.statuses = {s.name: s for s in statuses}
    report.counts["hotspots"] = len(firms.hotspots)
    report.calls = {"winds": winds.calls_used, "CAMS zones": cams_zones.calls_used,
                    "CAMS grid": grid.calls_used}  # fmt: skip
    if winds.from_cache:
        report.notes.append("winds from cache")
    if grid.from_cache:
        report.notes.append("CAMS grid from cache")

    if not firms.status.usable or winds.field is None:
        missing = [
            n
            for n, ok in (("FIRMS", firms.status.usable), ("winds", winds.field is not None))
            if not ok
        ]
        report.errors.append(f"no smoke forecast without {' and '.join(missing)}")
    else:
        report.t0 = f"{winds.field.t0}Z"
        try:
            model = timed("smoke model", run_model, firms.hotspots, winds.field)
            t = time.perf_counter()
            forecast = build_forecast(
                firms.hotspots, winds.field, model, cams_zones, grid, statuses
            )
            report.zones = forecast.zones
            report.counts.update(
                clusters=len(model.clusters),
                particles=model.n_particles,
                particles_before_t0=model.n_past_particles,
                trajectories=len(forecast.trajectories),
                hotspots_exported=len(forecast.hotspots),
            )
            errors = validate_forecast(forecast)
            if errors:
                report.outcome, report.exit_code, report.errors = "invalid", EXIT_INVALID, errors
            else:
                text = forecast.to_json()
                path = output_dir / config.FORECAST_FILENAME
                write_atomic(path, text)
                data = text.encode()
                report.outcome, report.exit_code = "written", EXIT_OK
                report.forecast_path = path
                report.size_bytes, report.gzip_bytes = len(data), len(gzip.compress(data))
            report.timings["export"] = time.perf_counter() - t
        except Exception as exc:  # never crash: keep the previous forecast, record the failure
            log.exception("Smoke model or export failed")
            report.errors.append(f"model or export failed: {type(exc).__name__}: {exc}"[:200])

    if write_history:
        try:
            report.history_rows = append_history(history_file, history_row(report))
            report.history_file = history_file
        except OSError as exc:
            log.error("Could not append the history (%s)", type(exc).__name__)
    report.total_s = time.perf_counter() - start
    return report


# --- Summary -------------------------------------------------------------------------------


def _duration(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{seconds:.1f} s"


def format_summary(r: RunReport) -> str:
    """The end-of-run summary, one screen."""
    head = {
        "written": "forecast written",
        "no_forecast": "NO FORECAST, previous file kept",
        "invalid": "INVALID FORECAST, previous file kept",
    }[r.outcome]
    lines = [f"Hazier run {_iso(r.run_at)}  t0 {r.t0 or '-'}  {head} (exit {r.exit_code})"]
    for name in HISTORY_SOURCES:
        s = r.statuses.get(name)
        if s:
            detail = f" ({s.detail})" if s.detail else ""
            lines.append(f"  {name:<6} {s.state}{detail}")
    c = r.counts
    if "clusters" in c:
        lines.append(
            f"  model  {c['hotspots']:,} hotspots, {c['clusters']} clusters, {c['particles']:,} "
            f"particles ({c['particles_before_t0']:,} before t0), {c['trajectories']} trajectories"
        )
    if r.zones:
        lines.append(f"  {'zone':<19}{'CAMS now':<24}{'trend':<11}{'smoke ETA':<19}main source")
        for z in r.zones:
            now = (
                f"{z.cams_now.basis_ugm3:5.1f} {z.cams_now.category.replace('_', ' ')}"
                if z.cams_now
                else "no data"
            )
            eta = fmt_eta(z.smoke_eta_hours, z.smoke_eta_earliest_hours, z.smoke_eta_latest_hours)
            src = (
                f"{z.smoke_sources[0].region} {z.smoke_sources[0].share:.0%}"
                if z.smoke_sources
                else "-"
            )
            lines.append(f"  {z.name:<19}{now:<24}{z.cams_trend or '-':<11}{eta:<19}{src}")
    if r.forecast_path:
        lines.append(
            f"  output {r.forecast_path}  {r.size_bytes / 1024:.0f} KB ({r.gzip_bytes / 1024:.0f} KB "
            f"gzipped), validated"
        )
    for e in r.errors[:5]:
        lines.append(f"  error  {e}")
    if r.history_file:
        lines.append(f"  history {r.history_file} (+1 line, {r.history_rows} runs)")
    stages = ", ".join(f"{k} {_duration(v)}" for k, v in r.timings.items())
    lines.append(f"  time   {_duration(r.total_s)} total ({stages})")
    calls = sum(r.calls.values())
    parts = ", ".join(f"{k} {v}" for k, v in r.calls.items())
    notes = f"; {', '.join(r.notes)}" if r.notes else ""
    lines.append(
        f"  calls  Open-Meteo {calls:,} this run ({parts}{notes}); free limits 600/min, "
        f"5,000/h, 10,000/day"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hazier", description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help=f"output folder (default {OUTPUT_DIR})")
    parser.add_argument("--history", type=Path, help=f"history CSV (default {HISTORY_FILE})")
    parser.add_argument("--no-history", action="store_true", help="do not append to the history")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    report = run_pipeline(
        output_dir=args.output, history_file=args.history, write_history=not args.no_history
    )
    print("\n" + format_summary(report))
    return report.exit_code
