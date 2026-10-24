"""Reading GRIB files with eccodes: each message decoded and cropped to the domain.

ECMWF open data (GRIB2, CCSDS packing) and the CAMS files from the ADS (GRIB1) are both regular
latitude/longitude grids, but they differ in scanning order (north to south) and in how
longitudes are written (ECMWF starts at 180 and wraps through 0). Every message is returned on
ascending latitude and longitude axes, cropped to the domain, so the rest of the pipeline never
deals with either.

eccodes installs with pip on Windows (the wheel bundles the library) and on Linux (pip pulls the
eccodeslib wheel); no system package is needed. It is imported only when a file is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from hazier import config

EPS = 1e-6

# (lat_min, lat_max, lon_min, lon_max), degrees
Bounds = tuple[float, float, float, float]
DOMAIN: Bounds = (config.LAT_MIN, config.LAT_MAX, config.LON_MIN, config.LON_MAX)


class GribError(Exception):
    """A file or message that can not be read as a regular latitude/longitude grid."""


@dataclass
class GribField:
    short_name: str
    run: datetime  # model run start (UTC)
    step_hours: int  # hours after the run start
    lats: np.ndarray  # ascending
    lons: np.ndarray  # ascending, degrees east in [0, 360)
    values: np.ndarray  # (lat, lon), NaN where missing

    @property
    def valid(self) -> datetime:
        return self.run + timedelta(hours=self.step_hours)


def regular_axes(
    lat_first: float, lat_last: float, nj: int, lon_first: float, ni: int, di: float
) -> tuple[np.ndarray, np.ndarray]:
    """Latitude and longitude of each row and column, in file order. Longitudes in [0, 360)."""
    lats = np.round(np.linspace(lat_first, lat_last, nj), 6)
    lons = np.round((lon_first + np.arange(ni) * di) % 360.0, 6)
    return lats, lons


def crop(
    values: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    bounds: Bounds = DOMAIN,
    step_deg: float | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The part of a (lat, lon) grid inside bounds, edges included, with ascending axes.

    With step_deg, only rows and columns on multiples of it are kept (0.5 from a 0.25 grid).
    """
    lat_min, lat_max, lon_min, lon_max = bounds
    lon_min, lon_max = lon_min % 360.0, lon_max % 360.0

    def keep(axis: np.ndarray, lo: float, hi: float) -> np.ndarray:
        mask = (axis >= lo - EPS) & (axis <= hi + EPS)
        if step_deg:
            ratio = axis / step_deg
            mask &= np.abs(ratio - np.round(ratio)) < EPS
        return np.flatnonzero(mask)

    rows, cols = keep(lats, lat_min, lat_max), keep(lons, lon_min, lon_max)
    if rows.size < 2 or cols.size < 2:
        raise GribError("the grid does not cover the domain")
    rows = rows[np.argsort(lats[rows])]
    cols = cols[np.argsort(lons[cols])]
    return lats[rows], lons[cols], values[np.ix_(rows, cols)]


def _run_time(data_date: int, data_time: int) -> datetime:
    day = datetime.strptime(str(data_date), "%Y%m%d").replace(tzinfo=UTC)
    return day + timedelta(hours=data_time // 100, minutes=data_time % 100)


def read_fields(
    path: Path,
    bounds: Bounds = DOMAIN,
    step_deg: float | None = None,
    short_names: set[str] | None = None,
) -> list[GribField]:
    """Every message of a GRIB file (or those with these short names), cropped. Raises GribError."""
    import eccodes  # heavy native library, only needed when reading

    out: list[GribField] = []
    with Path(path).open("rb") as f:
        while True:
            try:
                gid = eccodes.codes_grib_new_from_file(f)
            except eccodes.CodesInternalError as exc:
                raise GribError(f"unreadable GRIB message ({type(exc).__name__})") from None
            if gid is None:
                break
            try:
                name = str(eccodes.codes_get(gid, "shortName"))
                if short_names and name not in short_names:
                    continue
                if eccodes.codes_get(gid, "gridType") != "regular_ll":
                    raise GribError(f"{name}: not a regular latitude/longitude grid")
                if eccodes.codes_get(gid, "iScansNegatively") or eccodes.codes_get(
                    gid, "jPointsAreConsecutive"
                ):
                    raise GribError(f"{name}: unsupported scanning mode")
                ni, nj = int(eccodes.codes_get(gid, "Ni")), int(eccodes.codes_get(gid, "Nj"))
                lats, lons = regular_axes(
                    eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees"),
                    eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees"),
                    nj,
                    eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees"),
                    ni,
                    eccodes.codes_get(gid, "iDirectionIncrementInDegrees"),
                )
                values = np.asarray(eccodes.codes_get_values(gid), dtype=float)
                if values.size != ni * nj:
                    raise GribError(f"{name}: {values.size} values for a {nj} x {ni} grid")
                if eccodes.codes_get(gid, "bitmapPresent"):
                    values[values == eccodes.codes_get(gid, "missingValue")] = np.nan
                clats, clons, cvalues = crop(values.reshape(nj, ni), lats, lons, bounds, step_deg)
                out.append(
                    GribField(
                        short_name=name,
                        run=_run_time(
                            int(eccodes.codes_get(gid, "dataDate")),
                            int(eccodes.codes_get(gid, "dataTime")),
                        ),
                        step_hours=int(eccodes.codes_get(gid, "endStep")),
                        lats=clats,
                        lons=clons,
                        values=cvalues,
                    )
                )
            finally:
                eccodes.codes_release(gid)
    return out
