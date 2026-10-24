import itertools
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from gribfiles import axis, write_grib

from hazier import config
from hazier.schema import SourceStatus
from hazier.sources import ecmwf, winds
from hazier.sources.winds import WindsResult

NOW = datetime(2026, 9, 14, 10, 44, tzinfo=UTC)
T0 = datetime(2026, 9, 14, 10, tzinfo=UTC)
REF = datetime(2026, 9, 10, tzinfo=UTC)
LATS, LONS = axis(-8, 12, 0.25), axis(95, 120, 0.25)


def hours_since_ref(t: datetime) -> float:
    return (t - REF) / timedelta(hours=1)


def u_true(t: datetime) -> np.ndarray:
    """Linear in time, distinct per latitude: hourly interpolation must reproduce it exactly."""
    return np.broadcast_to(0.01 * hours_since_ref(t) + LATS[:, None], (len(LATS), len(LONS)))


def v_true() -> np.ndarray:
    return np.broadcast_to(LONS[None, :] - 100.0, (len(LATS), len(LONS)))


class FakeRetriever:
    """Stands in for the ECMWF download: writes u and v for the requested run and steps."""

    def __init__(self):
        self.calls: list[tuple[datetime, list[int]]] = []
        self.missing_runs: set[datetime] = set()
        self.drop_steps: dict[datetime, set[int]] = {}

    def __call__(self, run, steps, target):
        self.calls.append((run, list(steps)))
        if run in self.missing_runs:
            raise ecmwf.EcmwfError("HTTPError: 404 Not Found")
        messages = []
        for step in steps:
            if step in self.drop_steps.get(run, set()):
                continue
            valid = run + timedelta(hours=step)
            for name, values in (("u", u_true(valid)), ("v", v_true())):
                messages.append({"name": name, "run": run, "step": step, "lats": LATS,
                                 "lons": LONS, "values": values})  # fmt: skip
        write_grib(target, messages)


@pytest.fixture
def fake():
    return FakeRetriever()


def fetch(fake, tmp_path, **kwargs):
    return ecmwf.fetch_ecmwf_winds(now=NOW, cache_dir=tmp_path, retrieve=fake, **kwargs)


# --- Planning ------------------------------------------------------------------------------


def test_slice_times_cover_the_window_every_3_hours():
    times = ecmwf.slice_times(T0)
    assert times[0] == datetime(2026, 9, 12, 9, tzinfo=UTC)  # floor of t0 - 48 h to 3 h
    assert times[-1] == datetime(2026, 9, 17, 12, tzinfo=UTC)  # first slice at or after t0 + 72
    assert all(b - a == timedelta(hours=3) for a, b in itertools.pairwise(times))


def test_forecast_candidates_respect_the_publication_delay():
    runs = ecmwf.forecast_candidates(NOW)  # 10:44: the 06 UTC run is too young
    assert runs[0] == datetime(2026, 9, 14, 0, tzinfo=UTC)
    assert runs[1] == datetime(2026, 9, 13, 18, tzinfo=UTC)
    assert len(runs) == config.ECMWF_FORECAST_RUN_TRIES


def test_forecast_steps_and_run_length():
    times = ecmwf.slice_times(T0)
    run = datetime(2026, 9, 14, 0, tzinfo=UTC)
    steps = ecmwf.forecast_steps(run, times)
    assert steps[0] == 0 and steps[-1] == 84 and steps == list(range(0, 85, 3))
    # A 06 UTC run goes to 90 h only: from a day earlier it does not reach t0 + 72 h.
    assert ecmwf.forecast_steps(datetime(2026, 9, 13, 6, tzinfo=UTC), times) is None


def test_past_options_prefer_the_shortest_step():
    t = datetime(2026, 9, 13, 9, tzinfo=UTC)
    options = ecmwf.past_options(t)
    assert options[0] == (datetime(2026, 9, 13, 6, tzinfo=UTC), 3)
    assert options[1] == (datetime(2026, 9, 13, 0, tzinfo=UTC), 9)
    assert all(step <= config.ECMWF_PAST_MAX_STEP_HOURS for _, step in options)


def test_to_hourly_is_linear_and_bridges_only_short_gaps():
    base = datetime(2026, 9, 14, tzinfo=UTC)
    slices = [base + timedelta(hours=3 * k) for k in range(6)]  # 00 to 15
    u = np.array([[[float(k)]] for k in range(6)])
    present = np.array([True, True, False, True, False, False])  # 06 missing; 12, 15 missing
    targets = [base + timedelta(hours=h) for h in range(16)]
    out, _ = ecmwf.to_hourly(slices, u, u, present, targets)
    assert out[1, 0, 0] == pytest.approx(1 / 3)  # 01:00 between 00 (0) and 03 (1)
    assert out[7, 0, 0] == pytest.approx(2 + 1 / 3)  # 07:00 bridged over the missing 06 slice
    assert out[9, 0, 0] == 3  # 09 present
    assert np.isnan(out[10:, 0, 0]).all()  # after the last present slice


def test_to_hourly_does_not_bridge_long_gaps():
    base = datetime(2026, 9, 14, tzinfo=UTC)
    slices = [base + timedelta(hours=3 * k) for k in range(4)]
    u = np.zeros((4, 1, 1))
    present = np.array([True, False, False, True])  # 9 h apart: more than 6
    out, _ = ecmwf.to_hourly(slices, u, u, present, [base + timedelta(hours=h) for h in range(10)])
    assert out[0, 0, 0] == 0 and out[9, 0, 0] == 0 and np.isnan(out[1:9, 0, 0]).all()


# --- Fetch ---------------------------------------------------------------------------------


def test_field_shape_times_and_values(fake, tmp_path):
    result = fetch(fake, tmp_path)
    f = result.field
    assert result.status.state == "ok" and result.source == ecmwf.SOURCE
    assert f.u.shape == (121, 81, 101) and f.t0_index == 48
    assert f.t0 == np.datetime64("2026-09-14T10:00:00") and f.source == ecmwf.SOURCE
    assert f.run_init == datetime(2026, 9, 14, 0, tzinfo=UTC) and f.model == "ecmwf_ifs025"
    assert np.allclose(np.diff(f.lats), 0.25) and f.lons[0] == 95 and f.lons[-1] == 120
    for k in (0, 1, 47, 48, 49, 120):  # hours between 3-hourly slices are exact (linear data)
        t = T0 + timedelta(hours=k - 48)
        assert np.allclose(f.u[k], u_true(t), atol=0.02)
    assert np.allclose(f.v[60], v_true(), atol=0.02)
    assert result.missing_hours == 0 and not np.isnan(f.u).any()


def test_requests_one_download_per_run(fake, tmp_path):
    fetch(fake, tmp_path)
    runs = [run for run, _ in fake.calls]
    assert runs[0] == datetime(2026, 9, 14, 0, tzinfo=UTC)  # the forecast run, all its steps
    assert fake.calls[0][1] == list(range(0, 85, 3))
    assert len(runs) == len(set(runs))  # each earlier run asked once, for its short steps
    assert all(steps in ([0, 3], [3], [0]) for _, steps in fake.calls[1:])


def test_unpublished_newest_run_falls_back_to_the_previous(fake, tmp_path):
    fake.missing_runs.add(datetime(2026, 9, 14, 0, tzinfo=UTC))
    result = fetch(fake, tmp_path)
    assert result.status.state == "ok"
    assert result.field.run_init == datetime(2026, 9, 13, 18, tzinfo=UTC)


def test_incomplete_run_is_not_used_for_the_forecast(fake, tmp_path):
    fake.drop_steps[datetime(2026, 9, 14, 0, tzinfo=UTC)] = {42}
    result = fetch(fake, tmp_path)
    assert result.field.run_init == datetime(2026, 9, 13, 18, tzinfo=UTC)


def test_one_missing_past_run_is_covered_by_an_older_run(fake, tmp_path):
    fake.missing_runs.add(datetime(2026, 9, 13, 6, tzinfo=UTC))
    result = fetch(fake, tmp_path)
    assert result.status.state == "ok" and result.missing_hours == 0


def test_a_long_past_gap_degrades_but_keeps_the_forecast(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ECMWF_PAST_MAX_STEP_HOURS", 3)  # no longer steps to fill in
    for hour in (0, 6):
        fake.missing_runs.add(datetime(2026, 9, 13, hour, tzinfo=UTC))
    result = fetch(fake, tmp_path)
    assert result.status.state == "degraded" and "past winds" in result.status.detail
    assert result.missing_hours > 0 and result.field is not None
    assert not np.isnan(result.field.u[48:]).any()  # the forecast part is complete


def test_no_complete_run_fails(fake, tmp_path):
    for run in ecmwf.forecast_candidates(NOW):
        fake.missing_runs.add(run)
    result = fetch(fake, tmp_path)
    assert result.field is None and result.status.state == "failed"
    assert "no complete recent run" in result.status.detail


def test_unexpected_errors_never_crash(tmp_path):
    def boom(run, steps, target):
        raise RuntimeError("boom")

    result = ecmwf.fetch_ecmwf_winds(now=NOW, cache_dir=tmp_path, retrieve=boom)
    assert result.status.state == "failed"


def test_cache_avoids_downloads_on_the_next_run(fake, tmp_path):
    fetch(fake, tmp_path)
    n = len(fake.calls)
    second = fetch(fake, tmp_path)
    assert len(fake.calls) == n and second.from_cache
    assert np.array_equal(second.field.u, fetch(fake, tmp_path).field.u)


def test_half_degree_grid(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ECMWF_GRID_STEP_DEG", 0.5)
    f = fetch(fake, tmp_path).field
    assert f.u.shape == (121, 41, 51) and np.allclose(np.diff(f.lons), 0.5)


# --- The fallback --------------------------------------------------------------------------


def test_fetch_winds_uses_ecmwf_when_it_works(monkeypatch):
    primary = WindsResult(object(), SourceStatus("winds", "ok", "x"), source=ecmwf.SOURCE)
    monkeypatch.setattr(ecmwf, "fetch_ecmwf_winds", lambda **kw: primary)
    monkeypatch.setattr(winds, "fetch_openmeteo_winds", lambda **kw: pytest.fail("no fallback"))
    assert winds.fetch_winds(now=NOW) is primary


def test_fetch_winds_falls_back_to_open_meteo(monkeypatch):
    failed = WindsResult(None, SourceStatus("winds", "failed", None, "ECMWF open data: down"))
    backup = WindsResult(object(), SourceStatus("winds", "ok", "x"), calls_used=546)
    monkeypatch.setattr(ecmwf, "fetch_ecmwf_winds", lambda **kw: failed)
    monkeypatch.setattr(winds, "fetch_openmeteo_winds", lambda **kw: backup)
    result = winds.fetch_winds(now=NOW)
    assert result is backup and result.source == "open_meteo"
    assert "ECMWF open data: down, Open-Meteo used" in result.status.detail
    assert result.status.state == "ok" and "fallback" in result.note
