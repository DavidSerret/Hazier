"""Copernicus Atmosphere Data Store (ADS): CAMS global PM2.5 at 0.4 degrees for the map layer.

The primary source of the map colour layer. The CAMS global atmospheric composition forecast is
requested from the ADS through cdsapi, cropped to the domain on the server, at its native 0.4
degree grid (51 x 63 points), as GRIB. The zone cards keep using Open-Meteo for their 8 points
(sources/cams.py), which is cheap; the grid of 546+ points is what Open-Meteo throttled on
GitHub's shared runners.

Which run, which leads
----------------------
CAMS runs at 00 and 12 UTC. The ADS guarantees the 00 UTC run by 10:00 UTC and the 12 UTC run by
22:00 UTC, so the newest run requested is the newest one guaranteed online
(ADS_AVAILABILITY_DELAY_HOURS); if it fails, the run before it. PM2.5 is hourly up to 90 h of
lead, then 3-hourly (those hours are linear in time between the two surrounding leads).

The category basis is a 24 h trailing mean, so the window needs the 23 hours before t0 too,
some of them before the run started. They come from the run 24 h earlier, in the same request:
an ADS request is the cartesian product of its dates, times and leads, so the dates (D - 1, D)
with the single time HH ask for exactly the run and the run 24 h before it. Each hour is then
taken from the newer run when it covers it, else from the older one.

Queue and timeout
-----------------
ADS requests wait in a queue (about a minute when tested, October 2026, but it can be much
longer). The request is submitted, polled with a growing interval, and given up after
ADS_MAX_WAIT_S in total, every run tried included, so the workflow stays inside its timeout;
cams.fetch_cams_grid() then falls back to Open-Meteo at 2 degrees. Data older than 30 days is
on tape and very slow, so only the last two days are ever requested.

Credentials: ADS_API_KEY, a personal access token (pipeline/.env locally, a repository secret on
GitHub). The licence of the dataset must be accepted once on the ADS website.

The downloaded file is cached per request in pipeline/.cache/ads while developing.

Debug: `python -m hazier.sources.ads` runs the real request and prints a summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from hazier import config
from hazier.env import PIPELINE_DIR, load_env
from hazier.schema import SourceStatus
from hazier.sources.cams import CamsGrid, CamsGridResult, trailing_mean
from hazier.sources.grib import DOMAIN, GribError, read_fields

log = logging.getLogger(__name__)

CACHE_DIR = PIPELINE_DIR / ".cache" / "ads"
SOURCE = "ads"
SOURCE_LABEL = "CAMS global via the Copernicus Atmosphere Data Store (0.4 degrees)"
KG_TO_UG = 1e9  # kg/m3 to ug/m3
HOUR = timedelta(hours=1)

# Submits a request, waits until it is done (or the monotonic deadline passes) and downloads the
# result to the path. Raises AdsError.
Requester = Callable[[dict, Path, float], None]


class AdsError(Exception):
    """A failed or timed out request. The message is safe to log (it never holds the key)."""


# --- Planning (pure) -----------------------------------------------------------------------


def newest_run(now: datetime) -> datetime:
    """The newest CAMS run (00 or 12 UTC) guaranteed to be on the ADS at `now`."""
    t = now - timedelta(hours=config.ADS_AVAILABILITY_DELAY_HOURS)
    t = t.replace(minute=0, second=0, microsecond=0)
    return t - timedelta(hours=t.hour % 12)


def window_hours(t0: datetime) -> list[datetime]:
    """Every hour from t0 - (averaging - 1) h to t0 + horizon: the window and its history."""
    first = t0 - timedelta(hours=config.PM25_AVERAGING_HOURS - 1)
    n = config.PM25_AVERAGING_HOURS - 1 + config.HORIZON_HOURS + 1
    return [first + k * HOUR for k in range(n)]


def source_of(hour: datetime, run: datetime) -> tuple[datetime, int]:
    """(run, lead) providing an hour: the run itself from its start on, else the run 24 h earlier."""
    src = run if hour >= run else run - timedelta(hours=24)
    return src, int((hour - src) / HOUR)


def leads_for(lead: int) -> list[int]:
    """The leads needed for one hour: itself while hourly, else the two 3-hourly leads around it."""
    if lead <= config.ADS_MAX_HOURLY_LEAD:
        return [lead]
    lo = lead - lead % 3
    return [lo] if lo == lead else [lo, lo + 3]


@dataclass(frozen=True)
class AdsPlan:
    run: datetime
    hours: list[datetime]
    leads: list[int]  # requested for both runs (the request is a cartesian product)

    def request(self) -> dict:
        older = self.run - timedelta(hours=24)
        return {
            "variable": [config.ADS_VARIABLE],
            "date": [f"{older:%Y-%m-%d}/{self.run:%Y-%m-%d}"],
            "time": [f"{self.run:%H}:00"],
            "leadtime_hour": [str(x) for x in self.leads],
            "type": ["forecast"],
            "data_format": "grib",
            "area": [config.LAT_MAX, config.LON_MIN, config.LAT_MIN, config.LON_MAX],  # N W S E
        }


def plan(t0: datetime, run: datetime) -> AdsPlan | None:
    """The request for the window of t0 from this run; None if the run does not reach it."""
    hours = window_hours(t0)
    leads: set[int] = set()
    for h in hours:
        leads.update(leads_for(source_of(h, run)[1]))
    if max(leads) > config.ADS_MAX_LEAD:
        return None
    return AdsPlan(run, hours, sorted(leads))


def assemble(p: AdsPlan, fields: dict[tuple[datetime, int], np.ndarray]) -> np.ndarray:
    """Hourly values (hours, lat, lon) in ug/m3 from {(run, lead): grid}; NaN where missing."""
    shape = next(iter(fields.values())).shape
    out = np.full((len(p.hours),) + shape, np.nan)
    for k, h in enumerate(p.hours):
        run, lead = source_of(h, p.run)
        needed = leads_for(lead)
        grids = [fields.get((run, x)) for x in needed]
        if any(g is None for g in grids):
            continue
        if len(grids) == 1:
            out[k] = grids[0]
        else:
            w = (lead - needed[0]) / (needed[1] - needed[0])
            out[k] = (1 - w) * grids[0] + w * grids[1]
    return out


# --- The request ---------------------------------------------------------------------------


def request_ads(request: dict, target: Path, deadline: float) -> None:
    """Submit to the ADS, poll until done or the deadline, download. Raises AdsError."""
    key = os.environ.get(config.ADS_KEY_ENV, "").strip()
    if not key:
        raise AdsError(f"{config.ADS_KEY_ENV} is not set")
    import cdsapi  # only needed for real requests

    client = cdsapi.Client(
        url=config.ADS_URL, key=key, wait_until_complete=False, quiet=True, progress=False
    )
    try:
        remote = client.retrieve(config.ADS_DATASET, request)
    except Exception as exc:  # noqa: BLE001 - submission failed (auth, licence, bad request)
        raise AdsError(f"request refused: {_safe(exc)}") from None
    sleep = 2.0
    started = time.monotonic()
    last = None
    while True:
        try:
            ready = remote.results_ready
            status = remote.status
        except Exception as exc:  # noqa: BLE001 - the job failed on the server
            raise AdsError(f"request failed: {_safe(exc)}") from None
        if status != last:
            log.info("ADS: request %s after %.0f s", status, time.monotonic() - started)
            last = status
        if ready:
            break
        if time.monotonic() + sleep > deadline:
            try:
                remote.delete()
            except Exception:  # noqa: BLE001, S110 - best effort cleanup of the queued job
                pass
            raise AdsError(
                f"still {status} in the ADS queue after {time.monotonic() - started:.0f} s"
            )
        time.sleep(sleep)
        sleep = min(sleep * 1.5, config.ADS_POLL_MAX_S)
    try:
        remote.download(str(target))
    except Exception as exc:  # noqa: BLE001
        raise AdsError(f"download failed: {_safe(exc)}") from None


def _safe(exc: Exception) -> str:
    """A short error message without the key, whatever the library put in it."""
    text = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:200]
    key = os.environ.get(config.ADS_KEY_ENV, "")
    return text.replace(key, "***") if key else text


# --- Public entry point --------------------------------------------------------------------


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cache_path(cache_dir: Path, p: AdsPlan) -> Path:
    digest = hashlib.sha1(json.dumps(p.request(), sort_keys=True).encode()).hexdigest()[:10]
    return cache_dir / f"cams_{p.run:%Y%m%d%H}_{digest}.grib"


def _prune(cache_dir: Path, now: datetime) -> None:
    if not cache_dir.is_dir():
        return
    for f in cache_dir.glob("cams_*.grib"):
        try:
            run = datetime.strptime(f.name.split("_")[1], "%Y%m%d%H").replace(tzinfo=UTC)
        except (IndexError, ValueError):
            continue
        if run < now - timedelta(days=3):
            f.unlink(missing_ok=True)


def fetch_ads_grid(
    now: datetime | None = None,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    requester: Requester | None = None,
) -> CamsGridResult:
    """CAMS PM2.5 at 0.4 degrees over [now, now + 72 h] for the map, from the ADS. Never raises."""
    now = now or datetime.now(UTC)
    load_env()
    try:
        return _fetch(now, use_cache, cache_dir or CACHE_DIR, requester or request_ads)
    except AdsError as exc:
        log.error("ADS grid failed: %s", exc)
        status = SourceStatus("cams", "failed", None, f"ADS: {exc}"[:200])
        return CamsGridResult(None, status, source=SOURCE)
    except Exception as exc:  # noqa: BLE001 - the pipeline must never crash on this source
        log.error("ADS grid failed: unexpected %s", _safe(exc))
        status = SourceStatus("cams", "failed", None, "ADS: unexpected error")
        return CamsGridResult(None, status, source=SOURCE)


def _fetch(now: datetime, use_cache: bool, cache_dir: Path, requester: Requester) -> CamsGridResult:
    started = time.monotonic()
    deadline = started + config.ADS_MAX_WAIT_S
    t0 = now.replace(minute=0, second=0, microsecond=0)
    errors = []
    for k in range(config.ADS_RUN_TRIES):
        run = newest_run(now) - timedelta(hours=12 * k)
        p = plan(t0, run)
        if p is None:
            errors.append(f"{run:%Y-%m-%d %HZ} too old")
            continue
        path = _cache_path(cache_dir, p)
        from_cache = use_cache and path.is_file()
        if not from_cache:
            if time.monotonic() >= deadline:
                errors.append(f"{run:%Y-%m-%d %HZ} not tried, time is up")
                break
            tmp = path.with_suffix(".part")
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                log.info(
                    "ADS: requesting CAMS run %s, %d leads", f"{run:%Y-%m-%d %HZ}", len(p.leads)
                )
                requester(p.request(), tmp, deadline)
                os.replace(tmp, path)
            except (AdsError, OSError) as exc:
                tmp.unlink(missing_ok=True)
                errors.append(f"{run:%Y-%m-%d %HZ}: {exc}")
                log.warning("ADS: run %s failed (%s)", f"{run:%Y-%m-%d %HZ}", exc)
                continue
        try:
            result = _build(p, path, now, from_cache)
        except (GribError, OSError, ValueError) as exc:
            path.unlink(missing_ok=True)
            errors.append(f"{run:%Y-%m-%d %HZ}: unreadable file ({exc})")
            continue
        if not use_cache:
            path.unlink(missing_ok=True)
        _prune(cache_dir, now)
        log.info("ADS grid: %s in %.0f s", result.note, time.monotonic() - started)
        return result
    raise AdsError("; ".join(errors) or "no run tried")


def _build(p: AdsPlan, path: Path, now: datetime, from_cache: bool) -> CamsGridResult:
    fields = read_fields(path, DOMAIN)
    if not fields:
        raise ValueError("no fields in the file")
    lats, lons = fields[0].lats, fields[0].lons
    grids: dict[tuple[datetime, int], np.ndarray] = {}
    for f in fields:
        if not (np.array_equal(f.lats, lats) and np.array_equal(f.lons, lons)):
            raise ValueError("fields on different grids")
        grids[(f.run, f.step_hours)] = f.values * KG_TO_UG
    hourly_all = assemble(p, grids)
    basis_all = trailing_mean(hourly_all, config.PM25_AVERAGING_HOURS)
    first = config.PM25_AVERAGING_HOURS - 1
    hourly, basis = hourly_all[first:], basis_all[first:]
    times = np.array([np.datetime64(_iso(h)[:-1], "s") for h in p.hours[first:]])
    missing = int(np.isnan(basis).any(axis=0).sum())
    total = basis.shape[1] * basis.shape[2]
    note = f"run {p.run:%Y-%m-%d %HZ}, {len(lats)} x {len(lons)} points at 0.4 deg"
    result = CamsGridResult(
        CamsGrid(times, lats.astype(float), lons.astype(float), hourly, basis),
        SourceStatus("cams", "ok", _iso(now)),
        missing_points=missing,
        from_cache=from_cache,
        source=SOURCE,
        note=note,
        run_init=p.run,
    )
    if missing == total:
        raise ValueError("every point is missing")
    if missing:
        detail = f"CAMS grid (ADS): {missing} of {total} points missing"
        log.warning("%s", detail)
        result.status = SourceStatus("cams", "degraded", _iso(now), detail)
    return result


# --- Debug command -------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch the real CAMS PM2.5 grid from the ADS.")
    parser.add_argument("--no-cache", action="store_true", help="request again")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = datetime.now(UTC)
    t = time.perf_counter()
    result = fetch_ads_grid(now=now, use_cache=not args.no_cache)
    print(f"\nADS grid  now {_iso(now)}  {time.perf_counter() - t:.0f} s")
    print(f"status: {result.status.state} {result.status.detail or ''}")
    g = result.grid
    if g is None:
        return 1
    print(f"{result.note}; missing points {result.missing_points}; cache {result.from_cache}")
    print(f"lat {g.lats[0]:g} to {g.lats[-1]:g}, lon {g.lons[0]:g} to {g.lons[-1]:g}")
    print(f"times {g.times[0]} to {g.times[-1]} ({len(g.times)} hours)")
    print(
        f"24 h mean at t0: min {np.nanmin(g.basis[0]):.1f}, max {np.nanmax(g.basis[0]):.1f} ug/m3"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
