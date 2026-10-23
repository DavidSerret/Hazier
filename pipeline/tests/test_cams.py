import logging
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
import requests

from hazier import config
from hazier.schema import SmokeModel, ZoneForecast
from hazier.sources import cams

NOW = datetime(2026, 9, 14, 14, 44, tzinfo=UTC)
DAY_START = int(datetime(2026, 9, 13, tzinfo=UTC).timestamp())  # past_days=1: starts yesterday
N_HOURS = 120  # 24 h of history + 96 h forecast
NOW_INDEX = 24 + 14  # position of 14:00 UTC today on that axis
ZONE_IDS = [z.id for z in config.ZONES]


def default_series(zone_index):
    """Distinct, smooth hourly PM2.5 per zone: 20 + 5 * zone index, rising 0.1 per hour."""
    return [20.0 + 5 * zone_index + 0.1 * h for h in range(N_HOURS)]


class FakeResponse:
    def __init__(self, payload=None, status_code=200, not_json=False):
        self.payload = payload
        self.status_code = status_code
        self.not_json = not_json

    def json(self):
        if self.not_json:
            raise ValueError("not json")
        return self.payload


class FakeAPI:
    """Stands in for requests.get on the air quality endpoint."""

    def __init__(self):
        self.requests = []
        self.series = {i: default_series(i) for i in range(len(ZONE_IDS))}
        self.status_codes = []  # status per request number, 1-based; missing means 200
        self.raises = None
        self.drop_locations = 0
        self.axis_start = DAY_START

    def __call__(self, url, params=None, timeout=None):
        self.requests.append(params)
        if self.raises:
            raise self.raises
        code = (
            self.status_codes[len(self.requests) - 1]
            if len(self.requests) <= len(self.status_codes)
            else 200
        )
        if code == 400:
            return FakeResponse({"error": True, "reason": "Invalid parameter"}, 400)
        if code != 200:
            return FakeResponse({}, code)
        lats = params["latitude"].split(",")
        axis = [self.axis_start + 3600 * h for h in range(N_HOURS)]
        out = [
            {
                # Real responses carry the model cell, not the requested point.
                "latitude": float(lat) + 0.05,
                "longitude": 100.0,
                "hourly": {"time": axis, "pm2_5": self.series[i]},
            }
            for i, lat in enumerate(lats)
        ]
        out = out[: len(out) - self.drop_locations]
        return FakeResponse(out if len(out) > 1 else out[0])


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(cams.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def api(monkeypatch, sleeps):
    fake = FakeAPI()
    monkeypatch.setattr(cams.requests, "get", fake)
    return fake


# --- category mapping at the breakpoints -----------------------------------------------

BOUNDS = dict(config.PM25_CATEGORY_UPPER_BOUNDS)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "good"),
        (BOUNDS["good"], "good"),  # the upper bound belongs to the band
        (BOUNDS["good"] + 0.1, "moderate"),
        (BOUNDS["moderate"], "moderate"),
        (BOUNDS["moderate"] + 0.1, "unhealthy"),
        (BOUNDS["unhealthy"], "unhealthy"),
        (BOUNDS["unhealthy"] + 0.1, "very_unhealthy"),
        (BOUNDS["very_unhealthy"], "very_unhealthy"),
        (BOUNDS["very_unhealthy"] + 0.1, "hazardous"),
        (5000.0, "hazardous"),
    ],
)
def test_category_at_the_breakpoints(value, expected):
    assert cams.pm25_category(value) == expected


def test_breakpoints_are_increasing_and_flagged_unverified():
    uppers = [b for _, b in config.PM25_CATEGORY_UPPER_BOUNDS]
    assert uppers == sorted(uppers) and uppers[-1] == float("inf")
    assert [n for n, _ in config.PM25_CATEGORY_UPPER_BOUNDS] == [
        "good", "moderate", "unhealthy", "very_unhealthy", "hazardous"
    ]  # fmt: skip
    # Until someone checks the DOE document the output must say so.
    assert config.PM25_BREAKPOINTS_VERIFIED is False
    assert cams.baseline_metadata().categories_verified is False
    # The 24 h averaging itself is confirmed.
    assert cams.baseline_metadata().averaging_verified is True


# --- agreement flag --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("smoke_arrives", "cams_category", "expected"),
    [
        (True, "unhealthy", "both_elevated"),  # the minimum CAMS category counts
        (True, "hazardous", "both_elevated"),
        (True, "moderate", "hazier_only"),
        (True, "good", "hazier_only"),
        (False, "unhealthy", "cams_only"),
        (False, "very_unhealthy", "cams_only"),
        (False, "moderate", "both_low"),  # urban "moderate" PM2.5 is not smoke
        (False, "good", "both_low"),
    ],
)
def test_agreement_flag_cases(smoke_arrives, cams_category, expected):
    assert cams.agreement_flag(smoke_arrives, cams_category) == expected


# --- trend -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("last", "following", "expected"),
    [
        (50.0, 50.0, "stable"),
        (50.0, 57.0, "stable"),  # +14%: within 15%
        (50.0, 58.0, "worsening"),  # +16%
        (50.0, 42.0, "improving"),  # -16%
        (10.0, 12.0, "stable"),  # +20% but only 2 ug/m3: too small to matter
        (10.0, 13.5, "worsening"),  # +3.5 ug/m3
        (10.0, 6.5, "improving"),
    ],
)
def test_trend_cases(last, following, expected):
    assert cams.trend(last, following) == expected


# --- trailing mean ---------------------------------------------------------------------


def test_trailing_mean():
    x = np.arange(1.0, 7.0)  # 1 .. 6
    assert np.isnan(cams.trailing_mean(x, 3)[:2]).all()
    assert cams.trailing_mean(x, 3)[2:].tolist() == [2.0, 3.0, 4.0, 5.0]
    assert cams.trailing_mean(x, 1).tolist() == x.tolist()  # one hour: the values themselves
    assert np.isnan(cams.trailing_mean(x, 10)).all()  # not enough history anywhere
    gap = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
    assert np.isnan(cams.trailing_mean(gap, 2)[1:3]).all()  # a gap spoils the windows it is in
    assert cams.trailing_mean(gap, 2)[3:].tolist() == [3.5, 4.5]


# --- request and assembly --------------------------------------------------------------


def test_request_parameters_and_call_count(api):
    result = cams.fetch_cams(now=NOW)
    assert len(api.requests) == 1  # all zones in one request
    p = api.requests[0]
    assert p["domains"] == "cams_global" and p["hourly"] == "pm2_5"
    assert p["past_days"] == 1 and p["forecast_days"] == 4
    assert p["timezone"] == "GMT" and p["timeformat"] == "unixtime"
    assert p["latitude"].split(",") == [format(z.lat, "g") for z in config.ZONES]
    assert p["longitude"].split(",") == [format(z.lon, "g") for z in config.ZONES]
    assert result.calls_used == 8


def test_window_and_matching_by_order(api):
    result = cams.fetch_cams(now=NOW)
    assert result.status.state == "ok" and result.status.last_ok == "2026-09-14T14:44:00Z"
    assert len(result.times) == 73
    assert str(result.times[0]) == "2026-09-14T14:00:00"  # the current hour
    assert str(result.times[-1]) == "2026-09-17T14:00:00"  # 72 h later
    assert set(result.series) == set(ZONE_IDS)
    # Zone 3 (Ipoh) holds the series built for index 3, although the response says 100 E.
    ipoh = result.series["ipoh"]
    assert ipoh.hourly[0] == pytest.approx(20 + 15 + 0.1 * NOW_INDEX)
    assert ipoh.hourly[72] == pytest.approx(20 + 15 + 0.1 * (NOW_INDEX + 72))


def test_basis_is_the_24h_trailing_mean(api):
    series = cams.fetch_cams(now=NOW).series["klang_valley"]
    # Series 20 + 0.1 h: the mean of the 24 hours up to and including hour i is value(i) - 1.15.
    assert series.basis[0] == pytest.approx(series.hourly[0] - 1.15)
    assert series.basis[40] == pytest.approx(series.hourly[40] - 1.15)


def test_basis_can_use_raw_hourly_values(api, monkeypatch):
    monkeypatch.setattr(config, "PM25_AVERAGING_HOURS", 1)
    series = cams.fetch_cams(now=NOW).series["klang_valley"]
    assert np.array_equal(series.basis, series.hourly)


def test_a_short_spike_is_smoothed_by_the_averaging(api):
    spike = [20.0] * N_HOURS
    spike[NOW_INDEX + 10] = 120.0  # a one-hour spike, 100 above the rest
    api.series[0] = spike
    series = cams.fetch_cams(now=NOW).series["klang_valley"]
    assert series.hourly.max() == 120.0
    assert series.basis.max() == pytest.approx(20.0 + 100 / 24)  # spread over 24 hours
    assert cams.pm25_category(series.hourly.max()) == "very_unhealthy"
    assert cams.pm25_category(series.basis.max()) == "moderate"


# --- failures --------------------------------------------------------------------------


def test_server_error_retries_once_then_fails(api, sleeps):
    api.status_codes = [503, 503]
    result = cams.fetch_cams(now=NOW)
    assert len(api.requests) == 2 and sleeps == [config.CAMS_BACKOFF_S]
    assert result.status.state == "failed" and result.status.last_ok is None
    assert result.series == {} and result.times is None


def test_timeout_then_success(api, monkeypatch):
    real, state = api.__call__, {"n": 0}

    def flaky(url, params=None, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.Timeout(f"timed out: {url}")
        return real(url, params, timeout)

    monkeypatch.setattr(cams.requests, "get", flaky)
    assert cams.fetch_cams(now=NOW).status.state == "ok"
    assert state["n"] == 2


def test_client_error_is_not_retried(api, sleeps):
    api.status_codes = [400]
    result = cams.fetch_cams(now=NOW)
    assert len(api.requests) == 1 and sleeps == []
    assert result.status.state == "failed" and "Invalid parameter" in result.status.detail


def test_wrong_number_of_locations_fails(api):
    api.drop_locations = 1
    assert cams.fetch_cams(now=NOW).status.state == "failed"


def test_missing_values_make_the_zone_missing_and_degrade(api):
    broken = default_series(2)
    broken[NOW_INDEX + 30] = None  # a gap inside the window
    api.series[2] = broken
    result = cams.fetch_cams(now=NOW)
    assert result.status.state == "degraded" and result.status.usable
    assert result.status.detail == "no CAMS data for Melaka / Seremban"
    assert result.missing_zones == ["melaka_seremban"]
    assert "melaka_seremban" not in result.series and len(result.series) == 7


def test_missing_history_makes_the_zone_missing(api):
    short_history = default_series(0)
    short_history[NOW_INDEX - 5] = None  # inside the 24 h before the first hour
    api.series[0] = short_history
    result = cams.fetch_cams(now=NOW)
    assert result.missing_zones == ["klang_valley"]


def test_all_zones_missing_is_failed(api):
    api.series = {i: [None] * N_HOURS for i in range(8)}
    result = cams.fetch_cams(now=NOW)
    assert result.status.state == "failed" and result.series == {}


def test_time_axis_without_the_current_hour_fails(api):
    result = cams.fetch_cams(now=datetime(2026, 9, 25, 3, tzinfo=UTC))
    assert result.status.state == "failed" and "current hour" in result.status.detail


def test_axis_without_history_fails(api):
    api.axis_start = int(datetime(2026, 9, 14, 12, tzinfo=UTC).timestamp())  # no past day
    assert cams.fetch_cams(now=NOW).status.state == "failed"


def test_unexpected_exception_never_crashes(api):
    api.raises = RuntimeError("boom")
    result = cams.fetch_cams(now=NOW)
    assert result.status.state == "failed" and result.series == {}


def test_no_secrets_or_urls_are_logged(api, caplog):
    caplog.set_level(logging.DEBUG)
    api.raises = requests.ConnectionError("failed for https://air-quality-api.open-meteo.com/x")
    cams.fetch_cams(now=NOW)
    assert "open-meteo.com" not in caplog.text  # only the exception class name is kept


# --- filling the zone forecasts --------------------------------------------------------


def zone_forecast(zone, smoke_arrives):
    eta = 5.0 if smoke_arrives else None
    return ZoneForecast(zone.id, zone.name, zone.lat, zone.lon, zone.radius_km, eta, eta, eta, [])


def test_apply_to_zones_fills_series_peak_and_flag(api):
    result = cams.fetch_cams(now=NOW)
    zones = [zone_forecast(z, False) for z in config.ZONES]
    filled = cams.apply_to_zones(zones, result)
    kl = filled[0]
    # The level for the card: the category of the 24 h mean at t0.
    assert kl.cams_now == kl.cams_pm25[0] and kl.cams_now.time == "2026-09-14T14:00:00Z"
    assert kl.cams_now.category == cams.pm25_category(kl.cams_now.basis_ugm3)
    # Rising by 0.1 per hour: the next 24 h mean (25.05) is 2.4 above the last 24 h mean
    # (22.65), inside the margin of max(15% of 22.65, 3) = 3.4: stable.
    assert kl.cams_trend == "stable"
    assert [p.time for p in kl.cams_pm25[:2]] == ["2026-09-14T14:00:00Z", "2026-09-14T17:00:00Z"]
    assert len(kl.cams_pm25) == 25 and kl.cams_pm25[-1].time == "2026-09-17T14:00:00Z"
    first = kl.cams_pm25[0]
    assert first.value_ugm3 == pytest.approx(23.8, abs=0.05)  # 20 + 0.1 * 38
    assert first.basis_ugm3 == pytest.approx(22.7, abs=0.05)
    assert first.category == "moderate"
    # The series rises every hour, so the peak is the last one (even between exported points).
    assert kl.cams_peak.time == "2026-09-17T14:00:00Z"
    assert kl.cams_peak.value_ugm3 == pytest.approx(20 + 0.1 * (NOW_INDEX + 72), abs=0.05)
    assert kl.cams_peak.category == cams.pm25_category(kl.cams_peak.basis_ugm3)


def test_peak_is_found_between_the_exported_points(api, monkeypatch):
    monkeypatch.setattr(config, "PM25_AVERAGING_HOURS", 1)  # the basis is the hourly value
    spike = default_series(0)
    spike[NOW_INDEX + 1] = 5000.0  # hour 1: not on the 3-hourly export grid
    api.series[0] = spike
    kl = cams.apply_to_zones([zone_forecast(config.ZONES[0], False)], cams.fetch_cams(now=NOW))[0]
    assert kl.cams_peak.time == "2026-09-14T15:00:00Z"
    assert "2026-09-14T15:00:00Z" not in [p.time for p in kl.cams_pm25]


def test_agreement_per_zone(api):
    api.series[0] = [120.0] * N_HOURS  # Klang Valley: heavy PM2.5 all along
    api.series[1] = [60.0] * N_HOURS  # Johor Bahru: "unhealthy" with the provisional bands
    api.series[2] = [10.0] * N_HOURS  # Melaka / Seremban: clean
    api.series[3] = [10.0] * N_HOURS  # Ipoh: clean
    arrives = [True, False, True, False, False, False, False, False]
    zones = [zone_forecast(z, a) for z, a in zip(config.ZONES, arrives, strict=True)]
    flags = [z.agreement for z in cams.apply_to_zones(zones, cams.fetch_cams(now=NOW))]
    assert flags[:4] == ["both_elevated", "cams_only", "hazier_only", "both_low"]


def test_zones_without_cams_data_get_no_flag(api):
    api.series[0] = [None] * N_HOURS
    zones = [replace(zone_forecast(z, True), cams_pm25=[]) for z in config.ZONES]
    filled = cams.apply_to_zones(zones, cams.fetch_cams(now=NOW))
    assert filled[0].cams_pm25 == [] and filled[0].cams_peak is None
    assert filled[0].cams_now is None and filled[0].cams_trend is None
    assert filled[0].agreement is None
    assert filled[1].agreement is not None


def test_failed_cams_leaves_the_zones_without_baseline(api):
    api.status_codes = [500, 500]
    filled = cams.apply_to_zones([zone_forecast(z, True) for z in config.ZONES],
                                 cams.fetch_cams(now=NOW))  # fmt: skip
    assert all(z.cams_pm25 == [] and z.cams_now is None and z.agreement is None for z in filled)
    assert all(z.smoke_eta_hours == 5.0 for z in filled)  # Hazier's own timing is untouched


# --- metadata --------------------------------------------------------------------------


def test_attribution_names_cams_and_open_meteo():
    credits = {a.name: a for a in cams.attributions()}
    assert set(credits) == {"CAMS", "Open-Meteo"}
    assert "Copernicus Atmosphere Monitoring Service" in credits["CAMS"].text
    assert "Open-Meteo" in credits["Open-Meteo"].text
    assert all(a.url.startswith("https://") for a in credits.values())


def test_baseline_metadata():
    meta = cams.baseline_metadata()
    assert meta.resolution_km == 45.0 and meta.step_hours == 3 and meta.averaging_hours == 24
    assert meta.category_upper_bounds["hazardous"] is None  # open ended, valid JSON
    assert meta.category_upper_bounds["good"] == BOUNDS["good"]
    assert meta.cams_elevated_category == "unhealthy"


def test_forecast_with_baseline_serialises_without_nan():
    from hazier.schema import Forecast

    smoke_model = SmokeModel("ecmwf_ifs025", 850, 48, 48, 72.0, 10.0)
    fc = Forecast("2026-09-14T14:00:00Z", 72, [], [], [], [], smoke_model,
                  attributions=cams.attributions(), pm25_baseline=cams.baseline_metadata())  # fmt: skip
    text = fc.to_json()  # allow_nan=False: raises on a stray infinity or NaN
    assert '"categories_verified":false' in text and '"hazardous":null' in text


# --- the map layer (CAMS on the wind grid) ---------------------------------------------

GRID_POINTS = 21 * 26


def grid_value(lat, lon):
    """Distinct PM2.5 per grid node, constant in time: 10 + (lat + 8) + 0.5 * (lon - 95)."""
    return 10.0 + (lat + 8) + 0.5 * (lon - 95)


class FakeGridAPI:
    """Air quality endpoint for any number of points, plus the CAMS metadata file."""

    def __init__(self):
        self.requests = []
        self.failing = set()  # 1-based request numbers answered with HTTP 500
        self.null_points = set()
        self.run_init = int(datetime(2026, 9, 14, 0, tzinfo=UTC).timestamp())

    def __call__(self, url, params=None, timeout=None):
        if "static/meta.json" in url:
            return FakeResponse({"last_run_initialisation_time": self.run_init})
        self.requests.append(params)
        if len(self.requests) in self.failing:
            return FakeResponse({}, 500)
        lats = [float(x) for x in params["latitude"].split(",")]
        lons = [float(x) for x in params["longitude"].split(",")]
        axis = [DAY_START + 3600 * h for h in range(N_HOURS)]
        out = []
        for lat, lon in zip(lats, lons, strict=True):
            v = None if (lat, lon) in self.null_points else grid_value(lat, lon)
            out.append({"hourly": {"time": axis, "pm2_5": [v] * N_HOURS}})
        return FakeResponse(out if len(out) > 1 else out[0])


@pytest.fixture
def grid_api(monkeypatch, sleeps):
    fake = FakeGridAPI()
    monkeypatch.setattr(cams.requests, "get", fake)
    FakeResponse.raise_for_status = lambda self: None
    return fake


def test_grid_batches_calls_and_throttle(grid_api, tmp_path, sleeps):
    result = cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path)
    sizes = [len(p["latitude"].split(",")) for p in grid_api.requests]
    assert sizes == [50] * 10 + [46]
    assert result.calls_used == GRID_POINTS == 546 and result.batches == 11
    assert (
        grid_api.requests[0]["domains"] == "cams_global" and grid_api.requests[0]["past_days"] == 1
    )
    # Throttled like the winds: 10 pauses of 50 calls at the configured rate.
    assert sum(sleeps) == pytest.approx(10 * 50 / config.CAMS_GRID_MAX_CALLS_PER_MIN * 60, rel=0.01)
    assert result.calls_used / (sum(sleeps) / 60) < 600


def test_grid_shape_window_and_values(grid_api, tmp_path):
    result = cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path)
    g = result.grid
    assert result.status.state == "ok" and result.missing_points == 0
    assert g.basis.shape == g.hourly.shape == (73, 21, 26)
    assert str(g.times[0]) == "2026-09-14T14:00:00" and str(g.times[-1]) == "2026-09-17T14:00:00"
    # Matched by request order: node (lat 3, lon 101) holds its own value.
    assert g.basis[0, 11, 6] == pytest.approx(grid_value(3.0, 101.0))
    assert g.basis[40, 0, 25] == pytest.approx(grid_value(-8.0, 120.0))


def test_grid_failed_batch_and_missing_points_degrade(grid_api, tmp_path):
    grid_api.failing = {3, 4}  # the third batch and its single retry
    grid_api.null_points = {(12.0, 120.0)}
    result = cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path)
    assert result.status.state == "degraded" and result.failed_batches == 1
    assert result.missing_points == 51
    assert np.isnan(result.grid.basis[:, 20, 25]).all()
    assert list(tmp_path.glob("*.npz")) == []  # incomplete data is not cached


def test_grid_all_failing_is_failed(grid_api, tmp_path):
    grid_api.failing = set(range(1, 40))
    result = cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path)
    assert result.grid is None and result.status.state == "failed"


def test_grid_cache_avoids_calls_within_a_cams_run(grid_api, tmp_path):
    first = cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path)
    n = len(grid_api.requests)
    second = cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path)
    assert second.from_cache and second.calls_used == 0 and len(grid_api.requests) == n
    assert np.array_equal(first.grid.basis, second.grid.basis)
    grid_api.run_init += 12 * 3600  # a new CAMS run: fetch again
    assert not cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path).from_cache


def test_grid_unexpected_exception_never_crashes(monkeypatch, sleeps, tmp_path):
    def boom(url, params=None, timeout=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(cams.requests, "get", boom)
    result = cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path)
    assert result.grid is None and result.status.state == "failed"


def test_grid_export_is_quantised_and_compact(grid_api, tmp_path):
    from hazier.model.trajectories import json_size

    grid_api.null_points = {(12.0, 120.0)}
    exported = cams.grid_for_export(cams.fetch_cams_grid(now=NOW, cache_dir=tmp_path))
    assert exported.hours == list(range(0, 73, 3))  # one frame every 3 h
    assert len(exported.values) == 25 * GRID_POINTS
    assert exported.averaging_hours == 24 and exported.quantum_ugm3 == 1.0
    # Row-major (frame, lat, lon): frame 0, lat index 11 (3 N), lon index 6 (101 E).
    assert exported.values[11 * 26 + 6] == round(grid_value(3.0, 101.0))
    assert all(isinstance(v, int) for v in exported.values if v is not None)
    assert exported.values[20 * 26 + 25] is None  # missing point
    assert json_size([exported]) < 60_000
    assert (
        cams.grid_for_export(cams.CamsGridResult(None, cams.SourceStatus("cams", "failed", None)))
        is None
    )
