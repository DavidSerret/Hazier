"""Build hazier/data/static_sources.json: heat sources that are not vegetation fires.

VIIRS detects any hot spot: gas flares, power plants, cement and steel works, palm oil mill
boilers, volcanoes. Their heat is real but their smoke is not biomass smoke, and they would be
counted as fires every day. This script maps them so the pipeline can drop their detections
before clustering (see hazier/model/static.py).

Two sources, both from NASA FIRMS:

1. FIRMS's own classification. The standard-processing (SP) archive carries a "type" field:
   0 presumed vegetation fire, 1 active volcano, 2 other static land source, 3 offshore. The
   near-real-time data the pipeline uses does not. Every 400 m cell with at least
   MIN_TYPED_DETECTIONS type 1 or 2 detections in the last ARCHIVE_DAYS of SP data (S-NPP and
   NOAA-20) is kept. Type 3 (offshore) is already handled by the offshore filter.

2. Persistence across seasons, for sources newer than the archive (SP ends a few months before
   today). A fire, even a peat fire, burns for days to weeks within one dry season; a chimney is
   seen in every season. So a cell counts as persistent when, over the last PERSIST_MONTHS
   months (SP data, then near-real-time data after the archive ends), it was detected on at
   least PERSIST_MIN_DAYS distinct days in at least PERSIST_MIN_MONTHS distinct calendar months.
   Several thresholds are scored against FIRMS's own labels on the last PERSIST_MONTHS of SP
   data, and the rule is only used when its precision reaches MIN_PRECISION: excluding a real
   fire is worse than keeping a chimney.

Grid: 0.004 degrees (about 440 m), close to the 375 m VIIRS pixel and the 400 m grid of the
FIRMS Static Thermal Anomalies mask.

Downloads are cached in pipeline/.cache/static_build (git-ignored), so a rebuild with other
thresholds costs no request. Usage, from pipeline/ (needs FIRMS_MAP_KEY in .env; about 500
small requests, 20 minutes the first time):
    python scripts/build_static_sources.py
Rebuild every few months, or when a hotspot that is obviously industrial shows up.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import requests

from hazier import config
from hazier.env import PIPELINE_DIR, load_env

API = "https://firms.modaps.eosdis.nasa.gov/api"
OUT = Path(__file__).resolve().parents[1] / "hazier" / "data" / "static_sources.json"
CACHE = PIPELINE_DIR / ".cache" / "static_build"
CELL_DEG = 0.004
ARCHIVE_DAYS = 730
MIN_TYPED_DETECTIONS = 2
PERSIST_MONTHS = 12
# Candidate (min days, min months) thresholds, scored on SP; the first reaching MIN_PRECISION
# with the best recall is used.
CANDIDATES = [(d, m) for m in (4, 6, 8, 10) for d in (8, 12, 20, 30)]
MIN_PRECISION = 0.90
SP_SOURCES = ("VIIRS_SNPP_SP", "VIIRS_NOAA20_SP")
NRT_SOURCES = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")
AREA = f"{config.LON_MIN},{config.LAT_MIN},{config.LON_MAX},{config.LAT_MAX}"

Cell = tuple[int, int]


def get(key: str, path: str) -> str | None:
    """One API request. The key is in the URL, so no exception text is ever printed."""
    err = ""
    for attempt in range(4):
        try:
            r = requests.get(f"{API}/{path.format(key=key)}", timeout=120)
            if r.status_code == 200:
                return r.text
            err = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            err = type(exc).__name__
        time.sleep(3 * (attempt + 1))
    print(f"  giving up on one request ({err})")
    return None


def availability(key: str) -> dict[str, tuple[date, date]]:
    text = get(key, "data_availability/csv/{key}/all") or ""
    return {
        row["data_id"]: (date.fromisoformat(row["min_date"]), date.fromisoformat(row["max_date"]))
        for row in csv.DictReader(io.StringIO(text))
    }


def download(key: str, source: str, start: date, end: date) -> list[dict]:
    """Detections of a source over [start, end], in 5-day requests, cached on disk."""
    rows: list[dict] = []
    d = start
    while d <= end:
        days = min(5, (end - d).days + 1)
        cached = CACHE / source / f"{d.isoformat()}_{days}.csv"
        # Near-real-time files of the last week can still grow: never trust their cache.
        fresh = source.endswith("_NRT") and d + timedelta(days=days) > date.today() - timedelta(
            days=7
        )
        if cached.is_file() and not fresh:
            text = cached.read_text(encoding="utf-8")
        else:
            text = get(key, f"area/csv/{{key}}/{source}/{AREA}/{days}/{d.isoformat()}") or ""
            if text.startswith("latitude"):
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(text, encoding="utf-8")
        if text.startswith("latitude"):
            rows += list(csv.DictReader(io.StringIO(text)))
        d += timedelta(days=days)
    return rows


def cell(row: dict) -> Cell:
    return round(float(row["latitude"]) / CELL_DEG), round(float(row["longitude"]) / CELL_DEG)


def activity(rows: list[dict]) -> dict[Cell, tuple[int, int]]:
    """(distinct days, distinct calendar months) with a detection, per cell."""
    days: dict[Cell, set[str]] = defaultdict(set)
    for r in rows:
        days[cell(r)].add(r["acq_date"])
    return {c: (len(ds), len({d[:7] for d in ds})) for c, ds in days.items()}


def persistent(act: dict[Cell, tuple[int, int]], min_days: int, min_months: int) -> set[Cell]:
    return {c for c, (d, m) in act.items() if d >= min_days and m >= min_months}


def main() -> int:
    load_env()
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not key:
        sys.exit("FIRMS_MAP_KEY is not set (pipeline/.env)")
    avail = availability(key)
    sp_end = min(avail[s][1] for s in SP_SOURCES)
    sp_start = sp_end - timedelta(days=ARCHIVE_DAYS - 1)
    print(f"SP archive {sp_start} to {sp_end}")

    sp_rows: list[dict] = []
    for s in SP_SOURCES:
        t = time.perf_counter()
        rows = download(key, s, sp_start, sp_end)
        print(f"  {s}: {len(rows):,} detections ({time.perf_counter() - t:.0f} s)")
        sp_rows += rows

    typed: dict[Cell, dict] = defaultdict(lambda: {"n": 0, "kind": ""})
    for r in sp_rows:
        if r.get("type") in ("1", "2"):
            c = typed[cell(r)]
            c["n"] += 1
            c["kind"] = "volcano" if r["type"] == "1" else "static"
    archive = {c: v for c, v in typed.items() if v["n"] >= MIN_TYPED_DETECTIONS}
    print(f"archive: {len(archive)} cells with >= {MIN_TYPED_DETECTIONS} type 1 or 2 detections")

    # Score the persistence thresholds on the last PERSIST_MONTHS of SP against FIRMS's labels.
    score_start = sp_end - timedelta(days=round(PERSIST_MONTHS * 30.44))
    score_rows = [r for r in sp_rows if date.fromisoformat(r["acq_date"]) > score_start]
    labelled: dict[Cell, set[str]] = defaultdict(set)
    for r in score_rows:
        labelled[cell(r)].add(r["type"])
    truth = {c for c, t in labelled.items() if t & {"1", "2"}}
    act = activity(score_rows)
    print(f"persistence thresholds on SP {score_start} to {sp_end} ({len(truth)} static cells):")
    print("  min days  min months  flagged  precision  recall")
    best = None
    for min_days, min_months in CANDIDATES:
        flagged = persistent(act, min_days, min_months)
        hits = len(flagged & truth)
        precision = hits / len(flagged) if flagged else 0.0
        recall = hits / len(truth) if truth else 0.0
        print(
            f"  {min_days:8d}  {min_months:10d}  {len(flagged):7d}  {precision:9.0%}  {recall:6.0%}"
        )
        if precision >= MIN_PRECISION and (best is None or recall > best[3]):
            best = (min_days, min_months, precision, recall)

    recent: set[Cell] = set()
    persistence_note = "not used: no threshold reached the required precision"
    if best:
        min_days, min_months, precision, recall = best
        # The same rule on the last PERSIST_MONTHS up to today: SP, then near real time.
        nrt_start = sp_end + timedelta(days=1)
        window_start = date.today() - timedelta(days=round(PERSIST_MONTHS * 30.44))
        rows = [r for r in sp_rows if date.fromisoformat(r["acq_date"]) >= window_start]
        for s in NRT_SOURCES:
            lo = max(nrt_start, avail.get(s, (nrt_start, date.today()))[0])
            nrt = download(key, s, lo, date.today())
            print(f"  {s} {lo} to {date.today()}: {len(nrt):,} detections")
            rows += nrt
        recent = persistent(activity(rows), min_days, min_months) - set(archive)
        persistence_note = (
            f"{window_start} to {date.today()}: >= {min_days} days in >= {min_months} months "
            f"(precision {precision:.0%}, recall {recall:.0%} against FIRMS labels on SP)"
        )
        print(f"persistence rule {min_days} days / {min_months} months: {len(recent)} new cells")
    else:
        print(f"no threshold reaches {MIN_PRECISION:.0%} precision: persistence rule not used")

    cells = [
        [round(y * CELL_DEG, 3), round(x * CELL_DEG, 3), v["kind"], v["n"]]
        for (y, x), v in sorted(archive.items())
    ] + [
        [round(y * CELL_DEG, 3), round(x * CELL_DEG, 3), "persistent", 0] for y, x in sorted(recent)
    ]
    OUT.write_text(
        json.dumps(
            {
                "description": "Heat sources that are not vegetation fires, on a 0.004 degree grid. "
                "Built by pipeline/scripts/build_static_sources.py from NASA FIRMS.",
                "built": date.today().isoformat(),
                "archive": f"{sp_start} to {sp_end} ({', '.join(SP_SOURCES)}), FIRMS type 1 or 2, "
                f">= {MIN_TYPED_DETECTIONS} detections",
                "persistence": persistence_note,
                "cell_deg": CELL_DEG,
                "columns": ["lat", "lon", "kind", "typed_detections"],
                "cells": cells,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    kinds: dict[str, int] = defaultdict(int)
    for c in cells:
        kinds[c[2]] += 1
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB): {dict(kinds)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
