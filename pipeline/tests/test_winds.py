import math
from datetime import UTC, datetime

import numpy as np
import pytest
import requests

from hazier import config
from hazier.sources import winds

NOW = datetime(2026, 9, 14, 14, 44, tzinfo=UTC)
RUN_INIT = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
DAY_START = int(datetime(2026, 9, 14, tzinfo=UTC).timestamp())
N_POINTS = 21 * 26


def node_speed(lat):
    return 1.0 + (lat + 8)  # 1 to 21 m/s, distinct per latitude


def node_direction(lon):
    return (lon - 95) * 10.0  # 0 to 250 degrees, distinct per longitude


class FakeResponse:
    def __init__(self, payload=None, status_code=200, not_json=False):
        self.payload = payload
        self.status_code = status_code
        self.not_json = not_json

    def json(self):
        if self.not_json:
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeAPI:
    """Stands in for requests.get: serves the Open-Meteo forecast and metadata endpoints."""

    def __init__(self):
        self.requests = []  # params of every forecast request
        self.meta_requests = 0
        self.run_init = RUN_INIT
        self.meta_fails = False
        self.failing_requests = {}  # 1-based request number -> status code
        self.null_points = set()  # (lat, lon) whose values come back as null
        self.hours = 96
        self.raises = None

    def __call__(self, url, params=None, timeout=None):
        if "static/meta.json" in url:
            self.meta_requests += 1
            if self.meta_fails:
                return FakeResponse(status_code=404)
            return FakeResponse({"last_run_initialisation_time": int(self.run_init.timestamp())})
        self.requests.append(params)
        if self.raises:
            raise self.raises
        status = self.failing_requests.get(len(self.requests))
        if status == 400:
            return FakeResponse({"error": True, "reason": "Cannot initialize WeatherVariable"}, 400)
        if status:
            return FakeResponse({}, status)
        lats = [float(x) for x in params["latitude"].split(",")]
        lons = [float(x) for x in params["longitude"].split(",")]
        axis = [DAY_START + 3600 * i for i in range(self.hours)]
        out = []
        for lat, lon in zip(lats, lons, strict=True):
            null = (lat, lon) in self.null_points
            speed = [None if null else node_speed(lat)] * self.hours
            direction = [None if null else node_direction(lon)] * self.hours
            out.append(
                {
                    # Real responses carry the model cell, not the requested point.
                    "latitude": lat + 0.1,
                    "longitude": lon - 0.1,
                    "hourly": {
                        "time": axis,
                        winds.SPEED_VAR: speed,
                        winds.DIRECTION_VAR: direction,
                    },
                }
            )
        return FakeResponse(out if len(out) > 1 else out[0])


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(winds.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def api(monkeypatch, sleeps):
    fake = FakeAPI()
    monkeypatch.setattr(winds.requests, "get", fake)
    return fake


def fetch(tmp_path, **kwargs):
    return winds.fetch_winds(now=NOW, cache_dir=tmp_path, **kwargs)


# --- unit conversion -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "u", "v"),
    [
        (0, 0.0, -10.0),  # from north: blows towards the south
        (90, -10.0, 0.0),  # from east: blows towards the west
        (180, 0.0, 10.0),  # from south: blows towards the north
        (270, 10.0, 0.0),  # from west: blows towards the east
        (45, -10 / math.sqrt(2), -10 / math.sqrt(2)),  # from north-east: towards south-west
        (225, 10 / math.sqrt(2), 10 / math.sqrt(2)),  # from south-west: towards north-east
    ],
)
def test_speed_dir_to_uv_known_cases(direction, u, v):
    got_u, got_v = winds.speed_dir_to_uv(10.0, direction)
    assert got_u == pytest.approx(u, abs=1e-9)
    assert got_v == pytest.approx(v, abs=1e-9)


def test_uv_roundtrip_and_calm():
    speed = np.array([0.5, 3.0, 12.0, 7.0])
    direction = np.array([10.0, 123.0, 200.0, 359.0])
    s, d = winds.uv_to_speed_dir(*winds.speed_dir_to_uv(speed, direction))
    assert np.allclose(s, speed) and np.allclose(d, direction)
    assert winds.uv_to_speed_dir(0.0, 0.0)[0] == 0.0


def test_missing_values_stay_nan():
    u, v = winds.speed_dir_to_uv(np.array([np.nan, 5.0]), np.array([90.0, np.nan]))
    assert np.isnan(u).all() and np.isnan(v).all()


# --- request ---------------------------------------------------------------------------


def test_request_parameters(api, tmp_path):
    fetch(tmp_path)
    p = api.requests[0]
    assert p["models"] == "ecmwf_ifs025" == config.WIND_MODEL
    assert p["hourly"] == "wind_speed_850hPa,wind_direction_850hPa"
    assert p["wind_speed_unit"] == "ms"
    assert p["forecast_days"] == config.WIND_FORECAST_DAYS
    assert p["timezone"] == "GMT" and p["timeformat"] == "unixtime"


def test_batching_and_call_count(api, tmp_path):
    result = fetch(tmp_path)
    sizes = [len(p["latitude"].split(",")) for p in api.requests]
    assert sizes == [50] * 10 + [46]
    assert result.calls_used == N_POINTS == 546
    assert result.batches == 11


def test_throttle_keeps_rate_under_limit(api, tmp_path, sleeps):
    result = fetch(tmp_path)
    waited = sum(sleeps)
    # 10 pauses of 50 calls at the configured rate; the fake API answers instantly.
    assert waited == pytest.approx(10 * 50 / config.WIND_MAX_CALLS_PER_MIN * 60, rel=0.01)
    assert result.calls_used / (waited / 60) < 600


# --- grid assembly ---------------------------------------------------------------------


def test_field_shape_axes_and_times(api, tmp_path):
    f = fetch(tmp_path).field
    assert f.u.shape == f.v.shape == (73, 21, 26)
    assert f.lats[0] == -8 and f.lats[-1] == 12 and f.lons[0] == 95 and f.lons[-1] == 120
    assert f.times.dtype == np.dtype("datetime64[s]")
    assert str(f.times[0]) == "2026-09-14T14:00:00"  # the current hour, not midnight
    assert str(f.times[-1]) == "2026-09-17T14:00:00"  # 72 h later
    assert np.all(np.diff(f.hours) == 1) and f.hours[-1] == 72


def test_values_land_on_the_right_node(api, tmp_path):
    f = fetch(tmp_path).field
    for lat, lon in [(-8, 95), (3, 101), (12, 120), (0, 110)]:
        y, x = int(lat + 8), int(lon - 95)
        u, v = winds.speed_dir_to_uv(node_speed(lat), node_direction(lon))
        assert f.u[0, y, x] == pytest.approx(u) and f.v[40, y, x] == pytest.approx(v)


def test_metadata_is_recorded(api, tmp_path):
    f = fetch(tmp_path).field
    assert f.model == "ecmwf_ifs025" and f.level_hpa == 850 and f.run_init == RUN_INIT


def test_status_ok_and_complete(api, tmp_path):
    result = fetch(tmp_path)
    assert result.status.state == "ok" and result.status.last_ok == "2026-09-14T14:44:00Z"
    assert result.missing_points == 0 and not np.isnan(result.field.u).any()


def test_at_is_bilinear_and_bounded():
    lats, lons = np.arange(0.0, 3.0), np.arange(100.0, 104.0)
    t = np.array(["2026-09-14T00:00", "2026-09-14T01:00"], dtype="datetime64[s]")
    u = np.broadcast_to(2 * lats[None, :, None] + 3 * lons[None, None, :], (2, 3, 4)).copy()
    f = winds.WindField("m", None, 850, t, lats, lons, u, -u)
    gu, gv = f.at(1.25, 101.5)
    assert gu == pytest.approx([2 * 1.25 + 3 * 101.5] * 2) and gv == pytest.approx(-gu)
    assert f.at(2.0, 103.0)[0][0] == pytest.approx(2 * 2 + 3 * 103)  # the grid edge works
    with pytest.raises(ValueError):
        f.at(5.0, 101.0)


# --- missing points and failures -------------------------------------------------------


def test_missing_point_is_nan_and_degrades(api, tmp_path):
    api.null_points = {(3.0, 101.0)}
    result = fetch(tmp_path)
    assert result.status.state == "degraded" and result.status.usable
    assert result.status.detail == "1 of 546 grid points missing"
    assert result.missing_points == 1
    f = result.field
    assert np.isnan(f.u[:, 11, 6]).all()
    assert np.isnan(f.u).any(axis=0).sum() == 1  # only that node
    assert not np.isnan(f.u[:, 11, 7]).any()


def test_failed_batch_leaves_nan_and_retries_once(api, tmp_path, sleeps):
    api.failing_requests = {3: 500, 4: 500}  # the third batch and its single retry
    result = fetch(tmp_path)
    assert result.status.state == "degraded"
    assert result.failed_batches == 1 and result.missing_points == 50
    assert result.calls_used == 546 + 50  # the retry costs calls too
    assert config.WIND_BACKOFF_S in sleeps
    assert np.isnan(result.field.u[:, 4:6, :]).any()  # rows 100 to 149 are the third batch


def test_retry_recovers_from_timeout(api, tmp_path, monkeypatch):
    real = api.__call__
    state = {"n": 0}

    def flaky(url, params=None, timeout=None):
        if "static/meta.json" not in url:
            state["n"] += 1
            if state["n"] == 1:
                raise requests.Timeout(f"timed out: {url}")
        return real(url, params, timeout)

    monkeypatch.setattr(winds.requests, "get", flaky)
    result = fetch(tmp_path)
    assert result.status.state == "ok" and result.failed_batches == 0


def test_client_error_is_not_retried(api, tmp_path, sleeps):
    api.failing_requests = {1: 400}
    result = fetch(tmp_path)
    assert result.failed_batches == 1 and result.status.state == "degraded"
    assert len(api.requests) == 11  # no extra request for the failed batch
    assert config.WIND_BACKOFF_S not in sleeps


def test_wrong_location_count_fails_the_batch(api, tmp_path, monkeypatch):
    real = api.__call__

    def short(url, params=None, timeout=None):
        resp = real(url, params, timeout)
        if "static/meta.json" not in url and len(api.requests) == 2:
            resp.payload = resp.payload[:-1]
        return resp

    monkeypatch.setattr(winds.requests, "get", short)
    result = fetch(tmp_path)
    assert result.failed_batches == 1 and result.missing_points == 50


def test_all_requests_fail_returns_failed_status(api, tmp_path):
    api.failing_requests = {i: 503 for i in range(1, 40)}
    result = fetch(tmp_path)
    assert result.field is None
    assert result.status.state == "failed" and result.status.last_ok is None
    assert result.failed_batches == 11


def test_unexpected_exception_never_crashes(api, tmp_path):
    api.raises = RuntimeError("boom")
    result = fetch(tmp_path)
    assert result.field is None and result.status.state == "failed"


def test_time_axis_without_current_hour_fails(api, tmp_path):
    later = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
    result = winds.fetch_winds(now=later, cache_dir=tmp_path)
    assert result.field is None and result.status.state == "failed"
    assert "current hour" in result.status.detail


# --- cache -----------------------------------------------------------------------------


def test_cache_avoids_api_calls_within_a_model_run(api, tmp_path):
    first = fetch(tmp_path)
    n_requests = len(api.requests)
    second = fetch(tmp_path)
    assert second.from_cache and second.calls_used == 0
    assert len(api.requests) == n_requests
    assert second.status.state == "ok"
    assert np.array_equal(first.field.u, second.field.u)
    assert second.field.run_init == RUN_INIT


def test_new_model_run_refetches(api, tmp_path):
    fetch(tmp_path)
    api.run_init = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)
    result = fetch(tmp_path)
    assert not result.from_cache and result.calls_used == 546


def test_use_cache_false_refetches(api, tmp_path):
    fetch(tmp_path)
    result = fetch(tmp_path, use_cache=False)
    assert not result.from_cache and result.calls_used == 546


def test_incomplete_data_is_not_cached(api, tmp_path):
    api.null_points = {(3.0, 101.0)}
    fetch(tmp_path)
    assert list(tmp_path.glob("*.npz")) == []


def test_no_cache_without_model_run_time(api, tmp_path):
    api.meta_fails = True
    result = fetch(tmp_path)
    assert result.status.state == "ok" and result.field.run_init is None
    assert list(tmp_path.glob("*.npz")) == []
    assert not fetch(tmp_path).from_cache


def test_corrupt_cache_file_is_ignored(api, tmp_path):
    fetch(tmp_path)
    (cache_file,) = tmp_path.glob("*.npz")
    cache_file.write_bytes(b"not an npz")
    result = fetch(tmp_path)
    assert not result.from_cache and result.status.state == "ok"
