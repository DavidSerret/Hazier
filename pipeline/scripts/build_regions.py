"""Build hazier/data/regions.geojson from Natural Earth admin-1 (states and provinces).

Source: Natural Earth 1:10m Cultural Vectors, "Admin 1 - States, Provinces", version 5.1.2.
Public domain: https://www.naturalearthdata.com/about/terms-of-use/
GeoJSON copy: https://github.com/nvkelso/natural-earth-vector (tag v5.1.2)

Processing:
1. keep the countries that overlap the domain;
2. label each feature: the province or state (English name) in Indonesia and Malaysia, the
   country elsewhere (Singapore and Brunei are one region each);
3. clip every ring to the domain plus a margin (Sutherland-Hodgman);
4. simplify each ring (Douglas-Peucker, 0.01 degrees, about 1 km);
5. drop rings that become degenerate or tiny, round to 3 decimals (about 100 m);
6. merge features that share a label into one MultiPolygon.

Only the standard library is used, so the script runs without extra dependencies.

Usage, from pipeline/:
    python scripts/build_regions.py                  # downloads the 40 MB source to a temp dir
    python scripts/build_regions.py --source FILE    # uses a local copy of the source
"""

import argparse
import json
import math
import tempfile
import urllib.request
from itertools import pairwise
from pathlib import Path

from hazier import config

SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/"
    "geojson/ne_10m_admin_1_states_provinces.geojson"
)
OUT = Path(__file__).resolve().parents[1] / "hazier" / "data" / "regions.geojson"

# Countries overlapping 95 to 120 E, 8 S to 12 N.
BY_PROVINCE = {"IDN", "MYS"}  # labelled by province or state
BY_COUNTRY = {
    "SGP": "Singapore",
    "BRN": "Brunei",
    "THA": "Thailand",
    "VNM": "Vietnam",
    "KHM": "Cambodia",
    "PHL": "Philippines",
    "MMR": "Myanmar",
}
MARGIN_DEG = 1.0
TOLERANCE_DEG = 0.01
MIN_RING_AREA_DEG2 = 0.0005  # about 6 km2: smaller islets are dropped
DECIMALS = 3


def clip_ring(ring, xmin, ymin, xmax, ymax):
    """Sutherland-Hodgman clipping of a closed ring to a rectangle. None if nothing is left."""

    def clip(points, inside, cut):
        out = []
        prev = points[-1]
        for cur in points:
            if inside(cur):
                if not inside(prev):
                    out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
            prev = cur
        return out

    def at_x(x0):
        return lambda p, q: (x0, p[1] + (q[1] - p[1]) * (x0 - p[0]) / (q[0] - p[0]))

    def at_y(y0):
        return lambda p, q: (p[0] + (q[0] - p[0]) * (y0 - p[1]) / (q[1] - p[1]), y0)

    pts = [tuple(p[:2]) for p in ring[:-1]]
    for inside, cut in (
        (lambda p: p[0] >= xmin, at_x(xmin)),
        (lambda p: p[0] <= xmax, at_x(xmax)),
        (lambda p: p[1] >= ymin, at_y(ymin)),
        (lambda p: p[1] <= ymax, at_y(ymax)),
    ):
        if not pts:
            return None
        pts = clip(pts, inside, cut)
    if len(pts) < 3:
        return None
    return pts + [pts[0]]


def simplify(points, tol):
    """Douglas-Peucker, iterative so long coastlines do not hit the recursion limit."""
    n = len(points)
    if n <= 4:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, best_i = -1.0, None
        for i in range(a + 1, b):
            px, py = points[i]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, best_i = d, i
        if best_i is not None and best > tol:
            keep[best_i] = True
            stack.extend([(a, best_i), (best_i, b)])
    return [p for p, k in zip(points, keep, strict=True) if k]


def ring_area(ring):
    return 0.5 * abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in pairwise(ring)))


def clean_ring(ring, bbox):
    clipped = clip_ring(ring, *bbox)
    if clipped is None:
        return None
    pts = simplify(clipped, TOLERANCE_DEG)
    rounded = []
    for x, y in pts:
        p = [round(x, DECIMALS), round(y, DECIMALS)]
        if not rounded or p != rounded[-1]:
            rounded.append(p)
    if rounded[0] != rounded[-1]:
        rounded.append(rounded[0])
    if len(rounded) < 4 or ring_area(rounded) < MIN_RING_AREA_DEG2:
        return None
    return rounded


def label_for(props):
    country = props["adm0_a3"]
    if country in BY_PROVINCE:
        return props.get("name_en") or props["name"], country
    return BY_COUNTRY[country], country


def build(source: Path) -> dict:
    data = json.loads(source.read_text(encoding="utf-8"))
    bbox = (
        config.LON_MIN - MARGIN_DEG,
        config.LAT_MIN - MARGIN_DEG,
        config.LON_MAX + MARGIN_DEG,
        config.LAT_MAX + MARGIN_DEG,
    )
    merged: dict[str, dict] = {}
    for feature in data["features"]:
        props = feature["properties"]
        if props["adm0_a3"] not in BY_PROVINCE and props["adm0_a3"] not in BY_COUNTRY:
            continue
        geom = feature["geometry"]
        polygons = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        kept = []
        for polygon in polygons:
            outer = clean_ring(polygon[0], bbox)
            if outer is None:
                continue
            holes = [h for h in (clean_ring(r, bbox) for r in polygon[1:]) if h is not None]
            kept.append([outer, *holes])
        if not kept:
            continue
        label, country = label_for(props)
        entry = merged.setdefault(label, {"country": country, "polygons": []})
        entry["polygons"].extend(kept)

    features = [
        {
            "type": "Feature",
            "properties": {"name": label, "country": entry["country"]},
            "geometry": {"type": "MultiPolygon", "coordinates": entry["polygons"]},
        }
        for label, entry in sorted(merged.items())
    ]
    return {
        "type": "FeatureCollection",
        "source": "Natural Earth 1:10m admin-1 states and provinces v5.1.2 (public domain), "
        "clipped to the Hazier domain and simplified by scripts/build_regions.py",
        "features": features,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, help="local copy of the Natural Earth GeoJSON")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    if args.source:
        collection = build(args.source)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ne_10m_admin_1_states_provinces.geojson"
            print(f"downloading {SOURCE_URL}")
            urllib.request.urlretrieve(SOURCE_URL, path)
            collection = build(path)

    text = json.dumps(collection, separators=(",", ":"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    n_points = sum(
        len(ring)
        for f in collection["features"]
        for polygon in f["geometry"]["coordinates"]
        for ring in polygon
    )
    print(f"{len(collection['features'])} regions, {n_points} points, {len(text) / 1024:.0f} KB")
    print(", ".join(f["properties"]["name"] for f in collection["features"]))


if __name__ == "__main__":
    main()
