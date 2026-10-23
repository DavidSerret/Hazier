import json

import numpy as np
import pytest
from helpers import cluster

from hazier import config
from hazier.model import regions


@pytest.mark.parametrize(
    ("place", "lat", "lon", "expected"),
    [
        ("Pekanbaru", 0.51, 101.45, "Riau"),
        ("Jambi", -1.61, 103.61, "Jambi"),
        ("Palembang", -2.99, 104.76, "South Sumatra"),
        ("Bandar Lampung", -5.43, 105.26, "Lampung"),
        ("Pontianak", -0.03, 109.33, "West Kalimantan"),
        ("Palangka Raya", -2.21, 113.92, "Central Kalimantan"),
        ("Banjarmasin", -3.32, 114.59, "South Kalimantan"),
        ("Samarinda", -0.50, 117.15, "East Kalimantan"),
        ("Kuching", 1.55, 110.36, "Sarawak"),
        ("Kota Kinabalu", 5.98, 116.07, "Sabah"),
        ("Bandar Seri Begawan", 4.89, 114.94, "Brunei"),
        ("Mekong delta", 10.0, 105.8, "Vietnam"),
    ],
)
def test_known_places(place, lat, lon, expected):
    assert regions.region_names([lat], [lon]) == [expected]


def test_zone_centres_fall_in_their_state():
    expected = {
        "klang_valley": "Kuala Lumpur",
        "johor_bahru": "Johor",
        "ipoh": "Perak",
        "penang": "Penang",
        "kuantan": "Pahang",
        "singapore": "Singapore",
        "kuching": "Sarawak",
    }
    zones = [z for z in config.ZONES if z.id in expected]
    names = regions.region_names([z.lat for z in zones], [z.lon for z in zones])
    assert dict(zip([z.id for z in zones], names, strict=True)) == expected


def test_open_sea_is_other_and_the_shore_snaps_to_land():
    # Mid South China Sea, then a point 10 km off the Riau coast (a shore fire's centroid).
    assert regions.region_names([5.0, 1.75], [108.0, 101.6]) == [regions.OTHER, "Riau"]


def test_even_odd_rule_handles_holes():
    outer = np.array([[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]], dtype=float)
    hole = np.array([[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]], dtype=float)
    ring_region = regions.Region("Ring", "XXX", (outer, hole), (0.0, 0.0, 4.0, 4.0))
    names = regions.region_names([0.5, 2.0, 5.0], [0.5, 2.0, 5.0], (ring_region,))
    assert names == ["Ring", regions.OTHER, regions.OTHER]


def test_label_clusters():
    labelled = regions.label_clusters([cluster(0, 0.51, 101.45, 50.0), cluster(1, 5.0, 108.0, 9.0)])
    assert [c.region for c in labelled] == ["Riau", regions.OTHER]
    assert regions.label_clusters([]) == []


def test_data_file_is_small_and_documented():
    text = regions.REGIONS_FILE.read_text(encoding="utf-8")
    assert len(text.encode()) < 300_000
    data = json.loads(text)
    assert "Natural Earth" in data["source"]
    names = [f["properties"]["name"] for f in data["features"]]
    assert len(names) == len(set(names))
    for wanted in ("Riau", "Jambi", "South Sumatra", "Lampung", "West Kalimantan",
                   "Central Kalimantan", "South Kalimantan", "East Kalimantan", "Sarawak"):  # fmt: skip
        assert wanted in names
