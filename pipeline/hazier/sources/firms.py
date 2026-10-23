"""NASA FIRMS (VIIRS) active fire hotspots for the last 48 h.

Needs the FIRMS_MAP_KEY secret (env var, or pipeline/.env for local runs). CSV only.
Limit: 5,000 transactions per 10 minutes; one run makes one request per satellite.

fetch_hotspots() never raises: on failure it returns an empty result and a failed
SourceStatus. The pipeline keeps serving the last valid forecast in that case.

The API key is part of the request URL, so it must never reach logs or messages. Every
message that could contain a URL goes through _redact().

Debug: `python -m hazier.sources.firms` runs the real fetch and prints counts per satellite.
"""

from __future__ import annotations

import io
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests

from hazier import config
from hazier.env import load_env
from hazier.schema import Hotspot, SourceStatus

log = logging.getLogger(__name__)

API_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
KEY_ENV = "FIRMS_MAP_KEY"

CONFIDENCE_RANK = {"low": 0, "nominal": 1, "high": 2}
# VIIRS confidence is a single letter in the CSV.
_CONFIDENCE_NAMES = {"l": "low", "n": "nominal", "h": "high"}
_REQUIRED_COLUMNS = ("latitude", "longitude", "acq_date", "acq_time", "confidence", "frp")


class FirmsError(Exception):
    """A failure of one satellite request. The message is already redacted."""


class _Transient(Exception):
    """A failure worth one retry (timeout, connection error, 429, 5xx)."""


@dataclass
class SatelliteCounts:
    raw: int = 0  # rows in the CSV
    invalid: int = 0  # missing or unparseable coordinates, time or FRP
    too_old: int = 0  # older than the lookback window
    low_confidence: int = 0  # below config.FIRMS_MIN_CONFIDENCE
    kept: int = 0  # passed all filters (before cross-satellite deduplication)


@dataclass
class FirmsResult:
    hotspots: list[Hotspot]
    status: SourceStatus
    counts: dict[str, SatelliteCounts] = field(default_factory=dict)
    failed_satellites: list[str] = field(default_factory=list)
    duplicates_removed: int = 0


# --- HTTP ---------------------------------------------------------------------------------


def _redact(text: str, key: str | None) -> str:
    return text.replace(key, "***") if key else text


def _build_url(source: str, key: str) -> str:
    area = f"{config.LON_MIN},{config.LAT_MIN},{config.LON_MAX},{config.LAT_MAX}"
    return f"{API_URL}/{key}/{source}/{area}/{config.FIRMS_DAY_RANGE}"


def _request(url: str, key: str) -> str:
    try:
        resp = requests.get(url, timeout=config.FIRMS_TIMEOUT_S)
    except requests.RequestException as exc:
        # The exception text usually contains the URL, so only the class name is kept.
        raise _Transient(type(exc).__name__) from None
    status = resp.status_code
    if status == 429 or status >= 500:
        raise _Transient(f"HTTP {status}")
    if status != 200:
        raise FirmsError(f"HTTP {status}")
    text = resp.text
    # Errors such as an invalid key come back as a plain-text 200, not as a CSV.
    if not text.lstrip().startswith("latitude"):
        snippet = _redact(text.strip()[:100], key) or "empty body"
        raise FirmsError(f"unexpected response: {snippet}")
    return text


def _download(source: str, key: str) -> str:
    url = _build_url(source, key)
    for attempt in range(config.FIRMS_RETRIES + 1):
        try:
            return _request(url, key)
        except _Transient as exc:
            if attempt >= config.FIRMS_RETRIES:
                raise FirmsError(f"{exc} (gave up after {attempt + 1} attempts)") from None
            log.warning("FIRMS %s: %s, retrying in %.0f s", source, exc, config.FIRMS_BACKOFF_S)
            time.sleep(config.FIRMS_BACKOFF_S * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


# --- Parsing and filtering -----------------------------------------------------------------


def parse_hotspots(
    text: str, satellite: str, now: datetime
) -> tuple[list[Hotspot], SatelliteCounts]:
    """Parse one FIRMS CSV into hotspots, dropping invalid, old and low-confidence rows.

    The satellite label comes from the request (the source id), not from the CSV column,
    whose codes differ between the three satellites.
    """
    df = pd.read_csv(io.StringIO(text), dtype={"acq_date": str, "acq_time": str, "confidence": str})
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise FirmsError(f"CSV is missing columns: {', '.join(missing)}")

    counts = SatelliteCounts(raw=len(df))

    # acq_time is HHMM UTC and loses its leading zeros when written as a number ("530").
    hhmm = df["acq_time"].fillna("").str.strip().str.zfill(4)
    when = pd.to_datetime(
        df["acq_date"].fillna("") + " " + hhmm.str[:2] + ":" + hhmm.str[2:],
        format="%Y-%m-%d %H:%M",
        utc=True,
        errors="coerce",
    )
    lat = pd.to_numeric(df["latitude"], errors="coerce")
    lon = pd.to_numeric(df["longitude"], errors="coerce")
    frp = pd.to_numeric(df["frp"], errors="coerce")
    confidence = df["confidence"].fillna("").str.strip().str.lower().str[:1].map(_CONFIDENCE_NAMES)

    valid = when.notna() & lat.notna() & lon.notna() & frp.notna() & confidence.notna()
    counts.invalid = int((~valid).sum())

    oldest = pd.Timestamp(now - timedelta(hours=config.FIRMS_LOOKBACK_HOURS))
    recent = when >= oldest
    counts.too_old = int((valid & ~recent).sum())

    min_rank = CONFIDENCE_RANK[config.FIRMS_MIN_CONFIDENCE]
    rank = confidence.map(CONFIDENCE_RANK)
    confident = rank >= min_rank
    counts.low_confidence = int((valid & recent & ~confident).sum())

    keep = valid & recent & confident
    counts.kept = int(keep.sum())

    hotspots = [
        Hotspot(
            id=0,  # assigned after deduplication
            lat=float(la),
            lon=float(lo),
            frp_mw=float(fr),
            confidence=conf,
            acquired_at=_iso(ts),
            satellite=satellite,
        )
        for la, lo, fr, conf, ts in zip(
            lat[keep], lon[keep], frp[keep], confidence[keep], when[keep], strict=True
        )
    ]
    return hotspots, counts


def _iso(ts: pd.Timestamp | datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # Equirectangular approximation: exact enough at sub-kilometre scale.
    dx = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110.57
    return math.hypot(dx, dy)


def deduplicate(hotspots: list[Hotspot]) -> list[Hotspot]:
    """Merge detections of the same fire seen by several satellites.

    Two detections are duplicates when they are closer than FIRMS_DEDUP_DISTANCE_KM and their
    times differ by at most FIRMS_DEDUP_WINDOW_MIN. Within a duplicate group the detection with
    the highest confidence is kept, then the highest FRP. A uniform grid keeps this near
    linear: only the 3 x 3 neighbouring cells of a detection can hold a duplicate.
    """
    # A cell must be at least as wide as the distance threshold in km, in both axes.
    # 100 km per degree is below the true value everywhere in the domain, so cells are generous.
    cell = config.FIRMS_DEDUP_DISTANCE_KM / 100.0
    window = timedelta(minutes=config.FIRMS_DEDUP_WINDOW_MIN)

    ordered = sorted(
        hotspots,
        key=lambda h: (-CONFIDENCE_RANK[h.confidence], -h.frp_mw, h.acquired_at, h.lat, h.lon),
    )
    grid: dict[tuple[int, int], list[tuple[Hotspot, datetime]]] = {}
    kept: list[Hotspot] = []
    for h in ordered:
        t = _parse_iso(h.acquired_at)
        ci, cj = math.floor(h.lat / cell), math.floor(h.lon / cell)
        duplicate = any(
            abs(t - kt) <= window
            and _distance_km(h.lat, h.lon, k.lat, k.lon) <= config.FIRMS_DEDUP_DISTANCE_KM
            for di in (-1, 0, 1)
            for dj in (-1, 0, 1)
            for k, kt in grid.get((ci + di, cj + dj), ())
        )
        if not duplicate:
            grid.setdefault((ci, cj), []).append((h, t))
            kept.append(h)

    kept.sort(key=lambda h: (h.acquired_at, h.lat, h.lon))
    return [replace(h, id=i) for i, h in enumerate(kept)]


# --- Public entry point --------------------------------------------------------------------


def fetch_hotspots(api_key: str | None = None, now: datetime | None = None) -> FirmsResult:
    """Fetch, filter and deduplicate the last 48 h of VIIRS hotspots. Never raises.

    The returned status is ok when at least one satellite answered. Satellites that failed are
    listed in failed_satellites. SourceStatus.last_ok is set only on success; on failure it is
    None and the caller merges it with the previous run's value.
    """
    now = now or datetime.now(UTC)
    key = api_key or os.environ.get(KEY_ENV)
    labels = [label for label, _ in config.FIRMS_SOURCES]

    if not key:
        log.error("FIRMS: %s is not set", KEY_ENV)
        return FirmsResult([], SourceStatus("firms", False, None), failed_satellites=labels)

    all_hotspots: list[Hotspot] = []
    counts: dict[str, SatelliteCounts] = {}
    failed: list[str] = []
    for label, source in config.FIRMS_SOURCES:
        try:
            text = _download(source, key)
            found, counts[label] = parse_hotspots(text, label, now)
            all_hotspots.extend(found)
        except FirmsError as exc:
            log.error("FIRMS %s failed: %s", label, _redact(str(exc), key))
            failed.append(label)
        except Exception as exc:  # noqa: BLE001 - the pipeline must never crash on this source
            log.error("FIRMS %s failed: unexpected %s", label, type(exc).__name__)
            failed.append(label)

    if len(failed) == len(labels):
        return FirmsResult([], SourceStatus("firms", False, None), failed_satellites=failed)
    if failed:
        log.warning("FIRMS: partial data, failed satellites: %s", ", ".join(failed))

    unique = deduplicate(all_hotspots)
    return FirmsResult(
        hotspots=unique,
        status=SourceStatus("firms", True, _iso(now)),
        counts=counts,
        failed_satellites=failed,
        duplicates_removed=len(all_hotspots) - len(unique),
    )


# --- Debug command -------------------------------------------------------------------------


def _report(result: FirmsResult, now: datetime) -> None:
    bbox = f"{config.LON_MIN:g},{config.LAT_MIN:g},{config.LON_MAX:g},{config.LAT_MAX:g}"
    print(f"FIRMS fetch  bbox {bbox}  last {config.FIRMS_DAY_RANGE} days  now {_iso(now)}")
    print(f"min confidence: {config.FIRMS_MIN_CONFIDENCE}\n")
    print(f"{'satellite':<10}{'raw':>8}{'invalid':>9}{'too old':>9}{'low conf':>10}{'kept':>8}")
    for label, _ in config.FIRMS_SOURCES:
        c = result.counts.get(label)
        if c is None:
            print(f"{label:<10}{'FAILED':>8}")
        else:
            row = (c.raw, c.invalid, c.too_old, c.low_confidence, c.kept)
            print(f"{label:<10}{row[0]:>8}{row[1]:>9}{row[2]:>9}{row[3]:>10}{row[4]:>8}")
    print(f"\nduplicates removed across satellites: {result.duplicates_removed}")
    hs = result.hotspots
    by_sat = ", ".join(
        f"{label} {sum(h.satellite == label for h in hs)}" for label, _ in config.FIRMS_SOURCES
    )
    print(f"final hotspots: {len(hs)}  ({by_sat})")
    if hs:
        frp = sorted(h.frp_mw for h in hs)
        print(f"FRP MW: median {frp[len(frp) // 2]:.1f}, max {frp[-1]:.1f}")
        print(f"acquired: {hs[0].acquired_at} to {hs[-1].acquired_at}")
    state = "OK" if result.status.ok else "FAILED"
    extra = (
        f"  failed satellites: {', '.join(result.failed_satellites)}"
        if result.failed_satellites
        else ""
    )
    print(f"status: {state}{extra}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_env()
    now = datetime.now(UTC)
    result = fetch_hotspots(now=now)
    _report(result, now)
    return 0 if result.status.ok else 1


if __name__ == "__main__":
    sys.exit(main())
