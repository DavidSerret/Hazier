import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from hazier import config
from hazier.env import load_env
from hazier.sources import firms

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
KEY = "SECRETKEY123"

CSV_BY_SOURCE = {
    "VIIRS_SNPP_NRT": (FIXTURES / "firms_snpp.csv").read_text(),
    "VIIRS_NOAA20_NRT": (FIXTURES / "firms_noaa20.csv").read_text(),
    "VIIRS_NOAA21_NRT": (FIXTURES / "firms_noaa21.csv").read_text(),
}


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(firms.time, "sleep", sleeps.append)
    return sleeps


@pytest.fixture
def calls(monkeypatch):
    """Serve the fixture CSVs and record every requested URL. No network is touched."""
    urls = []

    def fake_get(url, timeout):
        urls.append(url)
        source = url.split("/")[-3]
        return FakeResponse(CSV_BY_SOURCE[source])

    monkeypatch.setattr(firms.requests, "get", fake_get)
    return urls


def patch_get(monkeypatch, handler):
    urls = []

    def fake_get(url, timeout):
        urls.append(url)
        return handler(url, len(urls))

    monkeypatch.setattr(firms.requests, "get", fake_get)
    return urls


# --- request ---------------------------------------------------------------------------


def test_request_url_uses_bbox_days_and_all_sources(calls):
    firms.fetch_hotspots(KEY, NOW)
    assert len(calls) == 3
    for url, (_, source) in zip(calls, config.FIRMS_SOURCES, strict=True):
        assert url.endswith(f"/{KEY}/{source}/95.0,-8.0,120.0,12.0/2")


# --- parsing and filtering -------------------------------------------------------------


def test_parse_counts_and_filters():
    hotspots, c = firms.parse_hotspots(CSV_BY_SOURCE["VIIRS_SNPP_NRT"], "S-NPP", NOW)
    assert (c.raw, c.invalid, c.too_old, c.low_confidence, c.kept) == (5, 0, 1, 1, 3)
    assert len(hotspots) == 3
    assert all(h.satellite == "S-NPP" for h in hotspots)
    assert all(h.confidence != "low" for h in hotspots)


def test_parse_fields():
    hotspots, _ = firms.parse_hotspots(CSV_BY_SOURCE["VIIRS_SNPP_NRT"], "S-NPP", NOW)
    h = next(h for h in hotspots if h.frp_mw == 45.0)
    assert (h.lat, h.lon) == (0.8, 101.9)
    assert h.confidence == "high"
    assert h.acquired_at == "2026-09-14T05:30:00Z"


def test_parse_pads_acq_time_without_leading_zero():
    hotspots, _ = firms.parse_hotspots(CSV_BY_SOURCE["VIIRS_NOAA21_NRT"], "NOAA-21", NOW)
    assert {h.acquired_at for h in hotspots} == {"2026-09-14T05:45:00Z"}


def test_parse_drops_invalid_rows():
    text = (
        "latitude,longitude,acq_date,acq_time,confidence,frp\n"
        "1.0,101.0,2026-09-14,0600,n,10.0\n"
        "bad,101.0,2026-09-14,0600,n,10.0\n"
        "1.0,101.0,2026-09-14,0600,n,\n"
        "1.0,101.0,2026-09-14,0600,x,10.0\n"
        "1.0,101.0,not-a-date,0600,n,10.0\n"
    )
    hotspots, c = firms.parse_hotspots(text, "S-NPP", NOW)
    assert len(hotspots) == 1
    assert (c.raw, c.invalid, c.kept) == (5, 4, 1)


def test_parse_header_only_csv_is_empty_not_an_error():
    header = CSV_BY_SOURCE["VIIRS_SNPP_NRT"].splitlines()[0] + "\n"
    hotspots, c = firms.parse_hotspots(header, "S-NPP", NOW)
    assert hotspots == [] and c.raw == 0


def test_parse_missing_columns_raises():
    with pytest.raises(firms.FirmsError):
        firms.parse_hotspots("latitude,longitude\n1,2\n", "S-NPP", NOW)


# --- deduplication ---------------------------------------------------------------------


def test_dedup_keeps_best_detection_of_same_fire(calls):
    result = firms.fetch_hotspots(KEY, NOW)
    assert result.duplicates_removed == 2
    # The nominal S-NPP detection loses to the high-confidence NOAA-20 one at the same spot.
    near_a = [h for h in result.hotspots if abs(h.lat - 0.512) < 0.01]
    assert [(h.satellite, h.confidence) for h in near_a] == [("NOAA-20", "high")]
    # The high-confidence S-NPP detection beats the nominal NOAA-21 one.
    near_b = [h for h in result.hotspots if abs(h.lat - 0.8) < 0.01]
    assert [(h.satellite, h.frp_mw) for h in near_b] == [("S-NPP", 45.0)]


def test_dedup_keeps_same_spot_at_distant_times(calls):
    result = firms.fetch_hotspots(KEY, NOW)
    same_spot = [h for h in result.hotspots if (h.lat, h.lon) == (0.1, 110.2)]
    assert len(same_spot) == 2  # 18:00 the day before and 06:00 today


def test_dedup_respects_distance_threshold():
    a = firms.Hotspot(0, 1.0, 101.0, 10.0, "nominal", "2026-09-14T06:00:00Z", "S-NPP")
    far = firms.Hotspot(0, 1.0 + 0.01, 101.0, 10.0, "nominal", "2026-09-14T06:00:00Z", "NOAA-20")
    assert len(firms.deduplicate([a, far])) == 2  # about 1.1 km apart


def test_dedup_ids_are_sequential_and_sorted_by_time(calls):
    result = firms.fetch_hotspots(KEY, NOW)
    assert [h.id for h in result.hotspots] == list(range(len(result.hotspots)))
    times = [h.acquired_at for h in result.hotspots]
    assert times == sorted(times)


def test_fetch_success_result(calls):
    result = firms.fetch_hotspots(KEY, NOW)
    assert len(result.hotspots) == 5
    assert result.status.ok and result.status.name == "firms"
    assert result.status.last_ok == "2026-09-14T12:00:00Z"
    assert result.failed_satellites == []
    assert set(result.counts) == {"S-NPP", "NOAA-20", "NOAA-21"}


# --- failure paths ---------------------------------------------------------------------


def test_missing_key_fails_without_requests(monkeypatch, calls):
    monkeypatch.delenv(firms.KEY_ENV, raising=False)
    result = firms.fetch_hotspots(None, NOW)
    assert result.hotspots == []
    assert not result.status.ok and result.status.last_ok is None
    assert calls == []


def test_server_error_retries_once_then_fails(monkeypatch, no_sleep):
    urls = patch_get(monkeypatch, lambda url, n: FakeResponse("oops", 503))
    result = firms.fetch_hotspots(KEY, NOW)
    assert len(urls) == 6  # 3 satellites x (1 try + 1 retry)
    assert no_sleep == [config.FIRMS_BACKOFF_S] * 3
    assert result.hotspots == []
    assert not result.status.ok
    assert result.failed_satellites == ["S-NPP", "NOAA-20", "NOAA-21"]


def test_timeout_then_success_is_retried(monkeypatch):
    def handler(url, n):
        if n == 1:
            raise requests.Timeout(f"timed out calling {url}")
        return FakeResponse(CSV_BY_SOURCE[url.split("/")[-3]])

    urls = patch_get(monkeypatch, handler)
    result = firms.fetch_hotspots(KEY, NOW)
    assert len(urls) == 4
    assert result.status.ok and result.failed_satellites == []
    assert len(result.hotspots) == 5


def test_invalid_key_response_is_not_retried(monkeypatch):
    urls = patch_get(monkeypatch, lambda url, n: FakeResponse("Invalid MAP_KEY."))
    result = firms.fetch_hotspots(KEY, NOW)
    assert len(urls) == 3
    assert result.hotspots == [] and not result.status.ok


def test_client_error_is_not_retried(monkeypatch):
    urls = patch_get(monkeypatch, lambda url, n: FakeResponse("", 403))
    result = firms.fetch_hotspots(KEY, NOW)
    assert len(urls) == 3 and not result.status.ok


def test_partial_failure_keeps_other_satellites(monkeypatch):
    def handler(url, n):
        source = url.split("/")[-3]
        if source == "VIIRS_NOAA20_NRT":
            return FakeResponse("", 500)
        return FakeResponse(CSV_BY_SOURCE[source])

    patch_get(monkeypatch, handler)
    result = firms.fetch_hotspots(KEY, NOW)
    assert result.status.state == "degraded"
    assert result.status.usable and not result.status.ok
    assert result.status.last_ok == "2026-09-14T12:00:00Z"
    assert result.status.detail == "no data from NOAA-20"
    assert result.failed_satellites == ["NOAA-20"]
    assert result.hotspots  # S-NPP and NOAA-21 data still returned
    assert all(h.satellite != "NOAA-20" for h in result.hotspots)


def test_malformed_csv_does_not_crash(monkeypatch):
    patch_get(monkeypatch, lambda url, n: FakeResponse("latitude,longitude\n1,2\n"))
    result = firms.fetch_hotspots(KEY, NOW)
    assert result.hotspots == [] and not result.status.ok


def test_unexpected_exception_does_not_crash(monkeypatch):
    def handler(url, n):
        raise RuntimeError(f"boom {url}")

    patch_get(monkeypatch, handler)
    result = firms.fetch_hotspots(KEY, NOW)
    assert result.hotspots == [] and not result.status.ok


def test_key_never_appears_in_logs(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def handler(url, n):
        # Real requests exceptions embed the full URL, key included.
        raise requests.ConnectionError(f"HTTPSConnectionPool failed for {url}")

    patch_get(monkeypatch, handler)
    firms.fetch_hotspots(KEY, NOW)
    assert caplog.text  # something was logged
    assert KEY not in caplog.text


def test_key_is_redacted_from_error_body(monkeypatch, caplog):
    patch_get(monkeypatch, lambda url, n: FakeResponse(f"Invalid MAP_KEY {KEY}."))
    firms.fetch_hotspots(KEY, NOW)
    assert KEY not in caplog.text


# --- env loader ------------------------------------------------------------------------


def test_load_env_sets_missing_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nFIRMS_TEST_A="abc"\nexport FIRMS_TEST_B=two\nFIRMS_TEST_C=new\n')
    monkeypatch.delenv("FIRMS_TEST_A", raising=False)
    monkeypatch.delenv("FIRMS_TEST_B", raising=False)
    monkeypatch.setenv("FIRMS_TEST_C", "existing")
    load_env(env)

    assert os.environ["FIRMS_TEST_A"] == "abc"
    assert os.environ["FIRMS_TEST_B"] == "two"
    assert os.environ["FIRMS_TEST_C"] == "existing"
    monkeypatch.delenv("FIRMS_TEST_A")
    monkeypatch.delenv("FIRMS_TEST_B")


def test_load_env_missing_file_is_fine(tmp_path):
    load_env(tmp_path / "nope.env")
