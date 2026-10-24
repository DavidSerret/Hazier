"""Small synthetic GRIB files for the ECMWF and ADS tests (no network).

Written with eccodes from its bundled samples, north to south like the real files, so the
reader's reordering is exercised too.
"""

from datetime import datetime
from pathlib import Path

import eccodes
import numpy as np


def axis(lo: float, hi: float, step: float) -> np.ndarray:
    return np.round(np.arange(lo, hi + step / 2, step), 6)


def write_grib(path: Path, messages: list[dict]) -> None:
    """Each message: name, run (datetime), step (h), lats and lons (ascending), values (lat, lon).

    Optional: "missing" (a mask of values to write as missing through a bitmap).
    """
    with Path(path).open("wb") as f:
        for m in messages:
            lats, lons = np.asarray(m["lats"]), np.asarray(m["lons"])
            values = np.asarray(m["values"], dtype=float)[::-1]  # north first
            sample = "regular_ll_pl_grib2" if m["name"] in ("u", "v") else "regular_ll_sfc_grib2"
            gid = eccodes.codes_grib_new_from_samples(sample)
            try:
                run: datetime = m["run"]
                keys = {
                    "Ni": len(lons),
                    "Nj": len(lats),
                    "latitudeOfFirstGridPointInDegrees": float(lats[-1]),
                    "latitudeOfLastGridPointInDegrees": float(lats[0]),
                    "longitudeOfFirstGridPointInDegrees": float(lons[0]),
                    "longitudeOfLastGridPointInDegrees": float(lons[-1]),
                    "iDirectionIncrementInDegrees": float(lons[1] - lons[0]),
                    "jDirectionIncrementInDegrees": float(lats[1] - lats[0]),
                    "dataDate": int(f"{run:%Y%m%d}"),
                    "dataTime": run.hour * 100,
                    "stepUnits": 1,
                    "endStep": int(m["step"]),
                }
                for k, v in keys.items():
                    eccodes.codes_set(gid, k, v)
                eccodes.codes_set(gid, "shortName", m["name"])
                if m["name"] in ("u", "v"):
                    eccodes.codes_set(gid, "level", 850)
                missing = m.get("missing")
                if missing is not None:
                    eccodes.codes_set(gid, "bitmapPresent", 1)
                    values = values.copy()
                    values[np.asarray(missing)[::-1]] = 9999.0
                    eccodes.codes_set(gid, "missingValue", 9999.0)
                eccodes.codes_set_values(gid, values.ravel())
                eccodes.codes_write(gid, f)
            finally:
                eccodes.codes_release(gid)
