"""Static heat sources: detections that are not vegetation fires, removed before clustering.

VIIRS sees every hot spot: gas flares on land, power plants, cement and steel works, palm oil
mill boilers, active volcanoes. They burn in the same place day after day, and their heat says
nothing about biomass smoke. Left in, they become "fires" that never go out.

The cells of known static sources are in hazier/data/static_sources.json, built by
scripts/build_static_sources.py from NASA FIRMS: the "type" field of the standard-processing
archive (1 active volcano, 2 other static land source) and, for sources newer than the archive,
a persistence rule on recent near-real-time detections. The near-real-time data the pipeline
reads has no type field, so the mask is the only way to tell them apart.

A hotspot is dropped when it falls within STATIC_REACH_CELLS cells (0.004 degrees, about
440 m) of a static cell, which covers the 375 m pixel and its geolocation error. The run logs
how many were dropped and their FRP. Offshore sources (flares at sea) are removed later, by
the offshore rule on clusters (model/regions.py).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from hazier import config
from hazier.schema import Hotspot

log = logging.getLogger(__name__)

STATIC_FILE = Path(__file__).resolve().parents[1] / "data" / "static_sources.json"


@dataclass
class StaticMask:
    cell_deg: float
    cells: frozenset[tuple[int, int]]  # (round(lat / cell), round(lon / cell))
    built: str = ""


@dataclass
class StaticSplit:
    kept: list[Hotspot]
    dropped: list[Hotspot] = field(default_factory=list)

    @property
    def dropped_frp_mw(self) -> float:
        return sum(h.frp_mw for h in self.dropped)


@lru_cache(maxsize=4)
def load_mask(path: Path = STATIC_FILE) -> StaticMask:
    """The static cells. An empty mask (and a warning) if the file is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        step = float(data["cell_deg"])
        cells = frozenset((round(c[0] / step), round(c[1] / step)) for c in data["cells"])
        return StaticMask(step, cells, str(data.get("built", "")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning(
            "Static source mask unavailable (%s): no static sources removed", type(exc).__name__
        )
        return StaticMask(0.004, frozenset())


def drop_static(
    hotspots: list[Hotspot], mask: StaticMask | None = None, reach: int | None = None
) -> StaticSplit:
    """Split hotspots into kept and dropped (near a static cell)."""
    mask = load_mask() if mask is None else mask
    reach = config.STATIC_REACH_CELLS if reach is None else reach
    if not mask.cells:
        return StaticSplit(list(hotspots))
    offsets = [(dy, dx) for dy in range(-reach, reach + 1) for dx in range(-reach, reach + 1)]
    split = StaticSplit([])
    for h in hotspots:
        y, x = round(h.lat / mask.cell_deg), round(h.lon / mask.cell_deg)
        if any((y + dy, x + dx) in mask.cells for dy, dx in offsets):
            split.dropped.append(h)
        else:
            split.kept.append(h)
    return split
