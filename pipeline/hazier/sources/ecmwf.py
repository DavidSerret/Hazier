"""ECMWF open data: winds at 850 hPa on the native 0.25 degree grid, past 48 h and next 72 h.

The primary wind source. The official ECMWF open data (IFS, CC BY 4.0) is read directly: u and
v at 850 hPa from the GRIB files, through the ecmwf-opendata client, which downloads only those
two fields of each global file (byte ranges from the file index, about 1.2 MB per step). The
fields are cropped to the domain and assembled into the same WindField the Open-Meteo path
produces, so the trajectory model does not know which source was used.

Runs and steps
--------------
IFS runs at 00, 06, 12 and 18 UTC with 3-hourly steps, and a run is published about 7 to 9 h
after it starts. The forecast part (t0 to t0 + 72 h) comes from one run: the newest run at
least ECMWF_MIN_DELAY_HOURS old whose steps reach t0 + 72 h, or the one before it if that run
is not published yet or a download fails. The same run also covers the hours between its start
and t0, with its first steps.

The past 48 h, which carry the smoke emitted before t0, are built from earlier runs: each past
3-hourly time is taken from the run that started at or just before it (step 0, its analysis, or
step 3). If that run is missing, an older run is used, with a longer step, up to
ECMWF_PAST_MAX_STEP_HOURS. This is the closest thing to an analysis the open data offers, and
the same idea as Open-Meteo's archive of recent runs.

Retention: the ECMWF portal keeps the last 12 runs (about 3 days), so the oldest past time
(t0 - 48 h, published runs lagging up to about 12 h) is well inside it. A run that is gone or
fails leaves a gap at those past times.

Time interpolation and gaps
---------------------------
The model steps hourly; the data is 3-hourly. Each hour is linear in time between the two
surrounding 3-hourly slices, which is also what Open-Meteo does. A missing past slice is bridged
the same way when its neighbours are at most ECMWF_MAX_BRIDGE_HOURS apart (one missing slice);
a longer gap leaves those hours missing (NaN), particles there stop, and the status is degraded
with the number of missing past hours. The forecast part is never partial: a run with a missing
step is not used.

Cache
-----
Each cropped (run, step) field is saved in pipeline/.cache/ecmwf (git-ignored). A published
field never changes, so repeated runs while developing only download new steps. Runs older than
ECMWF_CACHE_DAYS are deleted.

Failure handling matches the other sources: fetch_ecmwf_winds() never raises. If no complete run
is found, the status is failed and winds.fetch_winds() falls back to Open-Meteo.

Debug: `python -m hazier.sources.ecmwf` runs the real fetch and prints a summary.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import shutil
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from hazier import config
from hazier.env import PIPELINE_DIR
from hazier.schema import Attribution, SourceStatus
from hazier.sources.grib import DOMAIN, GribError, crop, read_fields
from hazier.sources.winds import WindField, WindsResult

log = logging.getLogger(__name__)

CACHE_DIR = PIPELINE_DIR / ".cache" / "ecmwf"
SOURCE = "ecmwf_open_data"
MODEL = "ecmwf_ifs025"  # the same model Open-Meteo names, read from ECMWF directly
PARAMS = ("u", "v")
HOUR = timedelta(hours=1)

# Downloads u and v of one run for these steps into a GRIB file, or raises.
Retriever = Callable[[datetime, list[int], Path], None]


class EcmwfError(Exception):
    """No usable wind field could be built. The message is safe to log."""


def attribution() -> Attribution:
    """Credit required by the CC BY 4.0 licence when ECMWF open data is used."""
    return Attribution(
        name="ECMWF",
        text="Contains modified ECMWF open data (IFS winds at 850 hPa), CC BY 4.0.",
        url="https://www.ecmwf.int/en/forecasts/datasets/open-data",
    )


# --- Planning (pure) -----------------------------------------------------------------------


def floor_hours(t: datetime, hours: int) -> datetime:
    """t rounded down to a multiple of `hours` within the UTC day."""
    t = t.replace(minute=0, second=0, microsecond=0)
    return t - timedelta(hours=t.hour % hours)


def slice_times(t0: datetime) -> list[datetime]:
    """The 3-hourly times covering [t0 - past, t0 + horizon], ends included."""
    step = config.ECMWF_STEP_HOURS
    start = floor_hours(t0 - timedelta(hours=config.WIND_PAST_HOURS), step)
    end = t0 + timedelta(hours=config.HORIZON_HOURS)
    times = [start]
    while times[-1] < end:
        times.append(times[-1] + timedelta(hours=step))
    return times


def forecast_candidates(now: datetime) -> list[datetime]:
    """Runs to try for the forecast part, newest first."""
    newest = floor_hours(now - timedelta(hours=config.ECMWF_MIN_DELAY_HOURS), 6)
    return [newest - timedelta(hours=6 * k) for k in range(config.ECMWF_FORECAST_RUN_TRIES)]


def forecast_steps(run: datetime, times: list[datetime]) -> list[int] | None:
    """Steps of `run` for every time from its start on; None if the run is too short."""
    steps = [int((t - run) / HOUR) for t in times if t >= run]
    if not steps or steps[-1] > config.ECMWF_MAX_STEP_HOURS[run.hour]:
        return None
    return steps


def past_options(t: datetime) -> list[tuple[datetime, int]]:
    """(run, step) that can provide past time t, shortest step first."""
    first = floor_hours(t, 6)
    options = []
    for k in range(10):
        run = first - timedelta(hours=6 * k)
        step = int((t - run) / HOUR)
        if step > config.ECMWF_PAST_MAX_STEP_HOURS:
            break
        options.append((run, step))
    return options


def to_hourly(
    slice_times_: list[datetime],
    u: np.ndarray,
    v: np.ndarray,
    present: np.ndarray,
    targets: list[datetime],
) -> tuple[np.ndarray, np.ndarray]:
    """Hourly (u, v) at the target times, linear in time between present slices.

    Two present slices further apart than ECMWF_MAX_BRIDGE_HOURS are not interpolated across:
    the hours between them stay NaN. Targets outside the present slices are NaN too.
    """
    secs = np.array([s.timestamp() for s in slice_times_])[present]
    us, vs = u[present], v[present]
    shape = (len(targets),) + u.shape[1:]
    u_out, v_out = np.full(shape, np.nan), np.full(shape, np.nan)
    bridge = config.ECMWF_MAX_BRIDGE_HOURS * 3600
    for k, t in enumerate(targets):
        ts = t.timestamp()
        i = int(np.searchsorted(secs, ts))
        if i < len(secs) and secs[i] == ts:
            u_out[k], v_out[k] = us[i], vs[i]
            continue
        if i == 0 or i == len(secs) or secs[i] - secs[i - 1] > bridge:
            continue
        w = (ts - secs[i - 1]) / (secs[i] - secs[i - 1])
        u_out[k] = (1 - w) * us[i - 1] + w * us[i]
        v_out[k] = (1 - w) * vs[i - 1] + w * vs[i]
    return u_out, v_out


# --- Download and cache --------------------------------------------------------------------


def retrieve_open_data(run: datetime, steps: list[int], target: Path) -> None:
    """Download u and v at 850 hPa of one run, trying each mirror in turn. Raises on failure."""
    from ecmwf.opendata import Client  # only needed for real downloads

    errors = []
    for source in config.ECMWF_SOURCES:
        client = Client(
            source=source,
            model="ifs",
            resol=config.ECMWF_RESOL,
            maximum_retries=config.ECMWF_RETRIES,
            retry_after=config.ECMWF_RETRY_AFTER_S,
        )
        try:
            # The client always draws progress bars on stderr; keep them out of the logs.
            with contextlib.redirect_stderr(io.StringIO()):
                client.retrieve(
                    date=run.strftime("%Y%m%d"),
                    time=run.hour,
                    type="fc",
                    step=steps,
                    levtype="pl",
                    levelist=config.PRESSURE_LEVEL_HPA,
                    param=list(PARAMS),
                    target=str(target),
                )
            return
        except Exception as exc:  # noqa: BLE001 - any failure: try the next mirror
            errors.append(f"{source}: {type(exc).__name__}")
            log.warning("ECMWF %s %02dZ from %s failed (%s)", f"{run:%Y-%m-%d}", run.hour, source,
                        str(exc)[:160])  # fmt: skip
    raise EcmwfError("; ".join(errors))


@dataclass
class _Slice:
    lats: np.ndarray
    lons: np.ndarray
    u: np.ndarray
    v: np.ndarray


class _Store:
    """Cropped fields by (run, step), from the cache or downloaded in one request per run."""

    def __init__(self, retrieve: Retriever, cache_dir: Path, use_cache: bool) -> None:
        self.retrieve = retrieve
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        self.downloads = 0  # requests (one per run), failed ones included
        self.downloaded_steps = 0
        self.cached_steps = 0
        self.failed_runs: set[datetime] = set()

    def _path(self, run: datetime, step: int) -> Path:
        return self.cache_dir / config.ECMWF_RESOL / f"{run:%Y%m%d%H}" / f"{step:03d}.npz"

    def _load(self, run: datetime, step: int) -> _Slice | None:
        path = self._path(run, step)
        if not (self.use_cache and path.is_file()):
            return None
        try:
            with np.load(path) as z:
                return _Slice(z["lats"], z["lons"], z["u"], z["v"])
        except (OSError, KeyError, ValueError):
            return None

    def _save(self, run: datetime, step: int, s: _Slice) -> None:
        path = self._path(run, step)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(path, lats=s.lats, lons=s.lons, u=s.u, v=s.v)
        except OSError as exc:
            log.warning("ECMWF: could not write cache (%s)", type(exc).__name__)

    def get(self, run: datetime, steps: list[int]) -> dict[int, _Slice]:
        """The steps of this run that could be obtained (maybe fewer than asked)."""
        out: dict[int, _Slice] = {}
        for step in steps:
            if (s := self._load(run, step)) is not None:
                out[step] = s
        self.cached_steps += len(out)
        wanted = [s for s in steps if s not in out]
        if not wanted or run in self.failed_runs:
            return out
        tmp = self.cache_dir / f"download-{run:%Y%m%d%H}-{os.getpid()}.grib2"
        self.downloads += 1
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            self.retrieve(run, wanted, tmp)
            fields = read_fields(tmp, DOMAIN, short_names=set(PARAMS))
        except (EcmwfError, GribError, OSError) as exc:
            log.warning("ECMWF: run %s unavailable (%s)", f"{run:%Y-%m-%d %HZ}", str(exc)[:160])
            self.failed_runs.add(run)
            return out
        finally:
            tmp.unlink(missing_ok=True)
        by_step: dict[int, dict[str, np.ndarray]] = defaultdict(dict)
        axes: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for f in fields:
            if f.run == run and f.step_hours in wanted:
                by_step[f.step_hours][f.short_name] = f.values
                axes[f.step_hours] = (f.lats, f.lons)
        for step, pair in by_step.items():
            if set(pair) == set(PARAMS):
                s = _Slice(*axes[step], pair["u"], pair["v"])
                out[step] = s
                self.downloaded_steps += 1
                if self.use_cache:
                    self._save(run, step, s)
        return out

    def prune(self, now: datetime) -> None:
        root = self.cache_dir / config.ECMWF_RESOL
        if not root.is_dir():
            return
        oldest = now - timedelta(days=config.ECMWF_CACHE_DAYS)
        for d in root.iterdir():
            try:
                run = datetime.strptime(d.name, "%Y%m%d%H").replace(tzinfo=UTC)
            except ValueError:
                continue
            if run < oldest:
                shutil.rmtree(d, ignore_errors=True)


# --- Assembly ------------------------------------------------------------------------------


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _regrid(s: _Slice) -> _Slice:
    """Thin the native grid to ECMWF_GRID_STEP_DEG (no-op at the native 0.25 degrees)."""
    step = config.ECMWF_GRID_STEP_DEG
    if abs(step - 0.25) < 1e-9:
        return s
    lats, lons, u = crop(s.u, s.lats, s.lons, DOMAIN, step)
    _, _, v = crop(s.v, s.lats, s.lons, DOMAIN, step)
    return _Slice(lats, lons, u, v)


@dataclass
class EcmwfDetail:
    """What one fetch did, for the log and the run summary."""

    forecast_run: datetime | None = None
    past_runs: list[datetime] | None = None
    tried: list[str] | None = None
    downloads: int = 0
    downloaded_steps: int = 0
    cached_steps: int = 0


def fetch_ecmwf_winds(
    now: datetime | None = None,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    retrieve: Retriever | None = None,
) -> WindsResult:
    """The 850 hPa wind field for [now - 48 h, now + 72 h] from ECMWF open data. Never raises."""
    now = now or datetime.now(UTC)
    try:
        return _fetch(now, use_cache, cache_dir or CACHE_DIR, retrieve or retrieve_open_data)
    except EcmwfError as exc:
        log.error("ECMWF winds failed: %s", exc)
        status = SourceStatus("winds", "failed", None, f"ECMWF open data: {exc}"[:200])
        return WindsResult(None, status, source=SOURCE)
    except Exception as exc:  # noqa: BLE001 - the pipeline must never crash on this source
        log.error("ECMWF winds failed: unexpected %s: %s", type(exc).__name__, str(exc)[:200])
        status = SourceStatus("winds", "failed", None, "ECMWF open data: unexpected error")
        return WindsResult(None, status, source=SOURCE)


def _fetch(now: datetime, use_cache: bool, cache_dir: Path, retrieve: Retriever) -> WindsResult:
    started = time.perf_counter()
    t0 = now.replace(minute=0, second=0, microsecond=0)
    times = slice_times(t0)
    store = _Store(retrieve, cache_dir, use_cache)
    slices: dict[datetime, _Slice] = {}
    detail = EcmwfDetail(tried=[])

    # Forecast part: one complete run.
    for run in forecast_candidates(now):
        steps = forecast_steps(run, times)
        label = f"{run:%Y-%m-%d %HZ}"
        if steps is None:
            detail.tried.append(f"{label} too short")
            continue
        got = store.get(run, steps)
        if len(got) == len(steps):
            detail.forecast_run = run
            for step, s in got.items():
                slices[run + timedelta(hours=step)] = s
            break
        detail.tried.append(f"{label} {len(got)} of {len(steps)} steps")
        log.warning("ECMWF: run %s incomplete (%d of %d steps)", label, len(got), len(steps))
    if detail.forecast_run is None:
        raise EcmwfError("no complete recent run (" + ", ".join(detail.tried) + ")")
    run0 = detail.forecast_run

    # Past part: the short steps of earlier runs, falling back to longer steps of older runs.
    pending = [t for t in times if t < run0]
    used_runs: set[datetime] = set()
    depth = 0
    while pending:
        wanted: dict[datetime, list[tuple[datetime, int]]] = defaultdict(list)
        for t in pending:
            options = past_options(t)
            if depth < len(options):
                run, step = options[depth]
                wanted[run].append((t, step))
        if not wanted:
            break
        for run in sorted(wanted):
            got = store.get(run, sorted({step for _, step in wanted[run]}))
            for t, step in wanted[run]:
                if step in got:
                    slices[t] = got[step]
                    used_runs.add(run)
        pending = [t for t in pending if t not in slices]
        depth += 1
    detail.past_runs = sorted(used_runs)

    # Stack the 3-hourly slices on one grid and interpolate to hours.
    regridded = {t: _regrid(s) for t, s in slices.items()}
    ref = regridded[run0]
    ny, nx = len(ref.lats), len(ref.lons)
    u3 = np.full((len(times), ny, nx), np.nan)
    v3 = np.full_like(u3, np.nan)
    present = np.zeros(len(times), dtype=bool)
    for k, t in enumerate(times):
        s = regridded.get(t)
        if s is None:
            continue
        if not (np.array_equal(s.lats, ref.lats) and np.array_equal(s.lons, ref.lons)):
            log.warning("ECMWF: slice %s is on another grid, ignored", _iso(t))
            continue
        u3[k], v3[k], present[k] = s.u, s.v, True

    past_steps = config.WIND_PAST_HOURS // config.STEP_HOURS
    n_steps = past_steps + config.HORIZON_HOURS // config.STEP_HOURS + 1
    targets = [t0 + timedelta(hours=(k - past_steps) * config.STEP_HOURS) for k in range(n_steps)]
    u, v = to_hourly(times, u3, v3, present, targets)

    missing_hour = np.isnan(u).any(axis=(1, 2)) | np.isnan(v).any(axis=(1, 2))
    if missing_hour[past_steps:].any():  # cannot happen with a complete run, but never serve it
        raise EcmwfError("forecast hours missing after assembly")
    missing_hours = int(missing_hour.sum())
    store.prune(now)

    detail.downloads = store.downloads
    detail.downloaded_steps = store.downloaded_steps
    detail.cached_steps = store.cached_steps
    field = WindField(
        model=MODEL,
        run_init=run0,
        level_hpa=config.PRESSURE_LEVEL_HPA,
        times=np.array([np.datetime64(_iso(t)[:-1], "s") for t in targets]),
        lats=ref.lats.astype(float),
        lons=ref.lons.astype(float),
        u=u,
        v=v,
        t0_index=past_steps,
        source=SOURCE,
    )
    note = (
        f"run {run0:%Y-%m-%d %HZ}, {len(used_runs)} earlier runs for the past, "
        f"{float(ref.lats[1] - ref.lats[0]):g} deg grid"
    )
    log.info(
        "ECMWF winds: %s; %d steps downloaded in %d requests, %d from cache, %.0f s",
        note, store.downloaded_steps, store.downloads, store.cached_steps,
        time.perf_counter() - started,
    )  # fmt: skip
    if missing_hours:
        text = f"past winds: {missing_hours} of {past_steps} h missing (ECMWF runs unavailable)"
        log.warning("ECMWF: degraded, %s", text)
        status = SourceStatus("winds", "degraded", _iso(now), text)
    else:
        status = SourceStatus("winds", "ok", _iso(now))
    result = WindsResult(
        field,
        status,
        source=SOURCE,
        missing_hours=missing_hours,
        from_cache=store.downloaded_steps == 0,
        note=note,
    )
    result.ecmwf = detail
    return result


# --- Debug command -------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch the real ECMWF open data wind field.")
    parser.add_argument("--no-cache", action="store_true", help="download every step again")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = datetime.now(UTC)
    t = time.perf_counter()
    result = fetch_ecmwf_winds(now=now, use_cache=not args.no_cache)
    print(f"\nECMWF winds  now {_iso(now)}  {time.perf_counter() - t:.0f} s")
    print(f"status: {result.status.state} {result.status.detail or ''}")
    f = result.field
    if f is None:
        return 1
    t_, y, x = f.u.shape
    print(f"grid {y} x {x} at {f.lats[1] - f.lats[0]:g} deg, {t_} hourly steps, t0 {f.t0}")
    print(f"{result.note}; missing past hours: {result.missing_hours}")
    print(f"memory: u and v {2 * f.u.nbytes / 1e6:.1f} MB")
    d = result.ecmwf
    if d:
        print("past runs:", ", ".join(f"{r:%d %HZ}" for r in d.past_runs or []))
        print(f"downloads: {d.downloads} requests, {d.downloaded_steps} steps, "
              f"{d.cached_steps} from cache")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
