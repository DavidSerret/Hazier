import base64
import json

import numpy as np
import pytest
from helpers import hotspot

from hazier import config
from hazier.model import smoke as sm
from hazier.model import static
from hazier.model import trajectories as tj

MASK = static.StaticMask(0.004, frozenset({(round(0.308 / 0.004), round(99.448 / 0.004))}))


def test_hotspot_on_a_static_cell_is_dropped():
    on = hotspot(0.308, 99.448, 5.0, i=0)
    near = hotspot(0.311, 99.451, 3.0, i=1)  # about 450 m away: the next cell, within reach
    far = hotspot(0.330, 99.448, 40.0, i=2)  # about 2.4 km away: a fire, kept
    split = static.drop_static([on, near, far], MASK)
    assert [h.id for h in split.dropped] == [0, 1] and [h.id for h in split.kept] == [2]
    assert split.dropped_frp_mw == 8.0


def test_reach_zero_keeps_the_neighbouring_cell():
    near = hotspot(0.312, 99.452, 3.0)
    assert static.drop_static([near], MASK, reach=0).dropped == []


def test_empty_mask_keeps_everything():
    hs = [hotspot(0.308, 99.448, 5.0)]
    split = static.drop_static(hs, static.StaticMask(0.004, frozenset()))
    assert split.kept == hs and split.dropped == []


def test_missing_mask_file_gives_an_empty_mask(tmp_path):
    static.load_mask.cache_clear()
    mask = static.load_mask(tmp_path / "nothing.json")
    assert mask.cells == frozenset()


def test_mask_file_is_read(tmp_path):
    path = tmp_path / "static.json"
    path.write_text(json.dumps({"cell_deg": 0.004, "built": "2026-10-25",
                                "cells": [[0.308, 99.448, "static", 6], [-2.0, 104.1, "volcano", 3]]}))  # fmt: skip
    static.load_mask.cache_clear()
    mask = static.load_mask(path)
    assert len(mask.cells) == 2 and mask.built == "2026-10-25"


def test_shipped_mask_is_valid():
    static.load_mask.cache_clear()
    mask = static.load_mask()
    assert mask.cell_deg == 0.004
    assert all(isinstance(y, int) and isinstance(x, int) for y, x in mask.cells)


# --- the smoke grid for the map ------------------------------------------------------------


def test_smoke_grid_encodes_the_index_on_a_log_scale():
    density = tj.HourlyDensity(cell_deg=0.5)
    density.hours = np.arange(3.0)
    density.load = np.zeros((3, density.ny, density.nx))
    row, col = 16, 20  # the cell centred at 0.25 N, 105.25 E
    cell_area = (0.5 * 111.32) ** 2 * np.cos(np.radians(density.lats[row]))
    zone_area = np.pi * config.ZONE_RADIUS_KM**2
    density.load[1, row, col] = 10.0 * cell_area / zone_area  # an index of exactly 10
    density.load[2, row, col] = 0.5 * cell_area / zone_area  # below the minimum
    grid = sm.smoke_grid_for_export(density)
    data = np.frombuffer(base64.b64decode(grid.data), dtype=np.uint8).reshape(
        3, density.ny, density.nx
    )
    assert data[0].max() == 0 and data[2].max() == 0
    q = int(data[1, row, col])
    decoded = grid.min_index * 10 ** ((q - 1) / grid.steps_per_decade)
    assert decoded == pytest.approx(10.0, rel=0.06)  # one step is about 6%
    assert len(grid.lats) == density.ny and len(grid.lons) == density.nx and grid.hours == [0, 1, 2]
