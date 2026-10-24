from datetime import UTC, datetime

import numpy as np
import pytest
from gribfiles import axis, write_grib

from hazier.sources.grib import DOMAIN, GribError, crop, read_fields, regular_axes

RUN = datetime(2026, 9, 14, 6, tzinfo=UTC)


def test_ecmwf_global_axes_are_cropped_to_the_domain_ascending():
    # ECMWF open data: 1440 x 721, north to south, longitudes starting at 180 and wrapping.
    lats, lons = regular_axes(90.0, -90.0, 721, 180.0, 1440, 0.25)
    values = lats[:, None] * 1000 + lons[None, :]  # recoverable from the value
    clats, clons, cvals = crop(values, lats, lons, DOMAIN)
    assert clats[0] == -8 and clats[-1] == 12 and len(clats) == 81
    assert clons[0] == 95 and clons[-1] == 120 and len(clons) == 101
    assert cvals[0, 0] == -8 * 1000 + 95 and cvals[-1, -1] == 12 * 1000 + 120
    assert np.all(np.diff(clats) > 0) and np.all(np.diff(clons) > 0)


def test_crop_can_thin_to_half_a_degree():
    lats, lons = regular_axes(90.0, -90.0, 721, 180.0, 1440, 0.25)
    clats, clons, cvals = crop(np.zeros((721, 1440)), lats, lons, DOMAIN, step_deg=0.5)
    assert (len(clats), len(clons)) == (41, 51) and cvals.shape == (41, 51)
    assert np.allclose(np.diff(clats), 0.5)


def test_crop_of_the_cams_grid_keeps_its_own_nodes():
    # CAMS on the ADS: 0.4 degree nodes, the first longitude in the domain is 95.2.
    lats, lons = regular_axes(12.0, -8.0, 51, 95.2, 63, 0.4)
    clats, clons, _ = crop(np.zeros((51, 63)), lats, lons, DOMAIN)
    assert clons[0] == 95.2 and clons[-1] == 120 and clats[0] == -8 and len(clats) == 51


def test_crop_outside_the_grid_raises():
    lats, lons = regular_axes(50.0, 40.0, 11, 0.0, 11, 1.0)
    with pytest.raises(GribError):
        crop(np.zeros((11, 11)), lats, lons, DOMAIN)


def test_read_fields_round_trip(tmp_path):
    lats, lons = axis(-8, 12, 1.0), axis(95, 120, 1.0)
    values = lats[:, None] + lons[None, :] / 1000
    path = tmp_path / "t.grib2"
    write_grib(
        path,
        [
            {"name": "u", "run": RUN, "step": 9, "lats": lats, "lons": lons, "values": values},
            {"name": "v", "run": RUN, "step": 9, "lats": lats, "lons": lons, "values": -values},
        ],
    )
    fields = read_fields(path)
    assert [f.short_name for f in fields] == ["u", "v"]
    u = fields[0]
    assert u.run == RUN and u.step_hours == 9 and u.valid == datetime(2026, 9, 14, 15, tzinfo=UTC)
    assert np.array_equal(u.lats, lats) and np.array_equal(u.lons, lons)
    assert np.allclose(u.values, values, atol=1e-3)
    assert [f.short_name for f in read_fields(path, short_names={"v"})] == ["v"]


def test_missing_values_become_nan(tmp_path):
    lats, lons = axis(-8, 12, 1.0), axis(95, 120, 1.0)
    missing = np.zeros((len(lats), len(lons)), dtype=bool)
    missing[3, 4] = True
    path = tmp_path / "m.grib2"
    message = {"name": "u", "run": RUN, "step": 0, "lats": lats, "lons": lons,
               "values": np.ones(missing.shape), "missing": missing}  # fmt: skip
    write_grib(path, [message])
    values = read_fields(path)[0].values
    assert np.isnan(values[3, 4]) and np.isnan(values).sum() == 1


def test_not_a_grib_file_raises(tmp_path):
    path = tmp_path / "bad.grib2"
    path.write_bytes(b"GRIB" + b"\x00" * 64)
    with pytest.raises(GribError):
        read_fields(path)
