# Data files

## regions.geojson

Outlines used to name the source region of each fire cluster (`hazier/model/regions.py`).

- **Source:** [Natural Earth](https://www.naturalearthdata.com/) 1:10m Cultural Vectors, "Admin 1 - States, Provinces", version 5.1.2, from the GeoJSON copy in [nvkelso/natural-earth-vector](https://github.com/nvkelso/natural-earth-vector) (tag v5.1.2).
- **License:** public domain ([terms of use](https://www.naturalearthdata.com/about/terms-of-use/)). Made with Natural Earth.
- **Built by:** `pipeline/scripts/build_regions.py`, which keeps the countries overlapping the domain, clips them to the domain plus 1 degree, simplifies the outlines to about 1 km, drops islets smaller than about 6 km², rounds coordinates to 3 decimals and merges features that share a label.
- **Contents:** 50 regions, about 9,500 points, 158 KB. Indonesian provinces and Malaysian states are kept with their English names. Singapore, Brunei, Thailand, Vietnam, Cambodia, the Philippines and Myanmar are one region each.

Known simplifications:

- North Kalimantan, a province since 2012, is still part of East Kalimantan in this edition of Natural Earth.
- Very small islands are dropped. A fire there, or a coastal fire whose cluster centroid falls on the water, is given the nearest region within 25 km.

To rebuild (downloads the 40 MB source to a temporary folder):

```
cd pipeline
.venv\Scripts\python.exe scripts\build_regions.py
```
