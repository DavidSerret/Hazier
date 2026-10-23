"""The whole pipeline with every source mocked: no network."""

import csv
import json
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
from helpers import hotspot, uniform

from hazier import config, export
from hazier.schema import Forecast, SourceStatus, validate_forecast
from hazier.sources.cams import CamsGrid, CamsGridResult, CamsResult, CamsSeries
from hazier.sources.firms import FirmsResult
from hazier.sources.winds import WindsResult, grid_axes

NOW = datetime(2026, 9, 14, 14, 44, tzinfo=UTC)  # t0 14:00, the T0 of the test fields
KL = next(z for z in config.ZONES if z.id == "klang_valley")


def ok(name):
    return SourceStatus(name, "ok", "2026-09-14T14:44:00Z")


def failed(name, detail="down"):
    return SourceStatus(name, "failed", None, detail)


def fake_firms():
    # A 1000 MW cluster 293 km south of Klang Valley, first seen 30 h before t0.
    return FirmsResult(
        [hotspot(KL.lat - 2.64 + 0.005 * i, KL.lon, 50.0, "2026-09-13T08:00:00Z", i) for i in range(20)],
        ok("firms"),
    )  # fmt: skip


def fake_winds():
    field = uniform(0.0, 5.0, past_hours=48)  # northward 5 m/s, from t0 - 48 h to t0 + 72 h
    return WindsResult(field, ok("winds"), calls_used=546)


def fake_cams():
    times = np.datetime64("2026-09-14T14:00:00", "s") + np.arange(73).astype("timedelta64[h]")
    series = {
        z.id: CamsSeries(np.full(73, 40.0 + i), np.full(73, 38.0 + i))
        for i, z in enumerate(config.ZONES)
    }
    return CamsResult(times, series, ok("cams"), [], calls_used=8)


def fake_grid():
    lats, lons = grid_axes()
    times = np.datetime64("2026-09-14T14:00:00", "s") + np.arange(73).astype("timedelta64[h]")
    values = np.full((73, len(lats), len(lons)), 25.0)
    return CamsGridResult(CamsGrid(times, lats, lons, values, values), ok("cams"), calls_used=546)


@pytest.fixture
def sources(monkeypatch):
    """Every source mocked; a test can replace one by assigning to the returned dict."""
    results = {
        "firms": fake_firms(),
        "winds": fake_winds(),
        "cams": fake_cams(),
        "grid": fake_grid(),
    }
    monkeypatch.setattr(export, "load_env", lambda: None)
    monkeypatch.setattr(export, "fetch_hotspots", lambda now: results["firms"])
    monkeypatch.setattr(export, "fetch_winds", lambda now: results["winds"])
    monkeypatch.setattr(export, "fetch_cams", lambda now: results["cams"])
    monkeypatch.setattr(export, "fetch_cams_grid", lambda now: results["grid"])
    return results


def run(tmp_path, **kwargs):
    return export.run_pipeline(
        now=NOW,
        output_dir=tmp_path / "site",
        history_file=tmp_path / "history" / "runs.csv",
        **kwargs,
    )


def load(tmp_path):
    return json.loads((tmp_path / "site" / "forecast.json").read_text(encoding="utf-8"))


# --- full run --------------------------------------------------------------------------


def test_full_run_writes_a_valid_forecast(sources, tmp_path):
    report = run(tmp_path)
    assert report.exit_code == export.EXIT_OK and report.outcome == "written"
    data = load(tmp_path)
    assert data["generated_at"] == "2026-09-14T14:00:00Z" and data["schema_version"] == 1
    assert [z["id"] for z in data["zones"]] == [z.id for z in config.ZONES]
    kl = data["zones"][0]
    assert kl["smoke_eta_hours"] == 0  # smoke from 30 h before t0 is already there
    assert kl["smoke_sources"][0]["region"] == "Riau"
    assert kl["cams_now"]["basis_ugm3"] == 38.0 and kl["cams_now"]["category"] == "unhealthy"
    assert data["pm25_grid"]["values"][0] == 25 and len(data["pm25_grid"]["values"]) == 25 * 546
    assert [s["name"] for s in data["source_status"]] == ["firms", "winds", "cams"]
    assert {a["name"] for a in data["attributions"]} == {"CAMS", "Open-Meteo"}
    assert data["smoke_model"]["past_hours"] == 48
    assert report.size_bytes == len((tmp_path / "site" / "forecast.json").read_bytes())
    assert 0 < report.gzip_bytes < report.size_bytes
    assert report.calls == {"winds": 546, "CAMS zones": 8, "CAMS grid": 546}


def test_written_file_passes_the_schema(sources, tmp_path):
    run(tmp_path)
    data = load(tmp_path)
    assert data["trajectories"] and all(t["hours"][0] >= 0 for t in data["trajectories"])
    assert len(data["hotspots"]) == 20 and data["hotspots"][0]["region"] == "Riau"


def test_summary_is_one_screen(sources, tmp_path):
    text = export.format_summary(run(tmp_path))
    assert "forecast written (exit 0)" in text and "Klang Valley" in text
    assert "Open-Meteo 1,100 this run" in text and "validated" in text
    assert len(text.splitlines()) <= 25


# --- partial failure -------------------------------------------------------------------


def test_cams_failure_still_writes_the_smoke_forecast(sources, tmp_path):
    sources["cams"] = CamsResult(None, {}, failed("cams", "HTTP 503"), [], calls_used=8)
    sources["grid"] = CamsGridResult(None, failed("cams", "HTTP 503"), calls_used=546)
    report = run(tmp_path)
    assert report.exit_code == export.EXIT_OK
    data = load(tmp_path)
    cams = next(s for s in data["source_status"] if s["name"] == "cams")
    assert cams["state"] == "failed" and "zones: HTTP 503" in cams["detail"]
    assert data["pm25_grid"] is None and data["pm25_baseline"] is None
    kl = data["zones"][0]
    assert kl["cams_now"] is None and kl["agreement"] is None
    assert kl["smoke_eta_hours"] == 0  # Hazier's own part is untouched


def test_grid_failure_alone_degrades_cams(sources, tmp_path):
    sources["grid"] = CamsGridResult(None, failed("cams", "HTTP 500"))
    run(tmp_path)
    data = load(tmp_path)
    cams = next(s for s in data["source_status"] if s["name"] == "cams")
    assert cams["state"] == "degraded" and cams["detail"] == "map layer: HTTP 500"
    assert data["pm25_grid"] is None and data["zones"][0]["cams_now"] is not None


def test_degraded_firms_is_flagged_and_used(sources, tmp_path):
    sources["firms"] = replace(
        sources["firms"], status=SourceStatus("firms", "degraded", "x", "no data from NOAA-20")
    )
    assert run(tmp_path).exit_code == export.EXIT_OK
    firms = load(tmp_path)["source_status"][0]
    assert firms["state"] == "degraded" and firms["detail"] == "no data from NOAA-20"


# --- total failure ---------------------------------------------------------------------


@pytest.mark.parametrize("broken", ["firms", "winds"])
def test_total_failure_keeps_the_previous_forecast(sources, tmp_path, broken):
    previous = tmp_path / "site" / "forecast.json"
    previous.parent.mkdir(parents=True)
    previous.write_text('{"previous": true}', encoding="utf-8")
    if broken == "firms":
        sources["firms"] = FirmsResult([], failed("firms", "FIRMS_MAP_KEY is not set"))
    else:
        sources["winds"] = WindsResult(None, failed("winds", "HTTP 503"), calls_used=592)
    report = run(tmp_path)
    assert report.exit_code == export.EXIT_NO_FORECAST and report.outcome == "no_forecast"
    assert previous.read_text(encoding="utf-8") == '{"previous": true}'  # untouched
    assert "NO FORECAST, previous file kept" in export.format_summary(report)
    rows = list(csv.DictReader((tmp_path / "history" / "runs.csv").open(encoding="utf-8")))
    assert rows[-1]["outcome"] == "no_forecast" and rows[-1][broken] == "failed"


def test_invalid_forecast_is_not_written(sources, tmp_path, monkeypatch):
    monkeypatch.setattr(export, "validate_forecast", lambda fc: ["zones: duplicate ids"])
    report = run(tmp_path)
    assert report.exit_code == export.EXIT_INVALID and report.errors == ["zones: duplicate ids"]
    assert not (tmp_path / "site" / "forecast.json").exists()


def test_model_crash_never_escapes(sources, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(export, "run_model", boom)
    report = run(tmp_path)
    assert report.exit_code == export.EXIT_NO_FORECAST and "RuntimeError" in report.errors[0]


def test_main_returns_the_exit_code(sources, tmp_path):
    code = export.main(["--output", str(tmp_path / "site"), "--history", str(tmp_path / "h.csv")])
    assert code == 0 and (tmp_path / "site" / "forecast.json").is_file()


# --- history ---------------------------------------------------------------------------


def test_history_appends_one_line_per_run(sources, tmp_path):
    run(tmp_path)
    run(tmp_path)
    path = tmp_path / "history" / "runs.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # header + 2 runs
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert list(rows[0]) == export.history_header()
    row = rows[0]
    assert (row["outcome"], row["firms"], row["winds"], row["cams"]) == (
        "written",
        "ok",
        "ok",
        "ok",
    )
    assert row["klang_valley_pm25"] == "38" and row["klang_valley_eta"] == "0"
    assert row["klang_valley_source"] == "Riau" and row["kuching_eta"] == ""
    assert len(lines[1].encode()) < 400  # small: about 1 KB a day at 4 runs


def test_history_without_writing(sources, tmp_path):
    run(tmp_path, write_history=False)
    assert not (tmp_path / "history" / "runs.csv").exists()


def test_history_columns_change_starts_a_new_file(sources, tmp_path):
    path = tmp_path / "history" / "runs.csv"
    path.parent.mkdir(parents=True)
    path.write_text("run_at,old_column\n2026-09-01T00:00:00Z,x\n", encoding="utf-8")
    run(tmp_path)
    assert (tmp_path / "history" / "runs-until-2026-09-14.csv").is_file()
    assert path.read_text(encoding="utf-8").splitlines()[0].startswith("run_at,t0,outcome")


def test_last_ok_comes_from_the_history(sources, tmp_path):
    run(tmp_path)  # everything ok at 14:44
    sources["cams"] = CamsResult(None, {}, failed("cams"), [])
    sources["grid"] = CamsGridResult(None, failed("cams"))
    later = export.run_pipeline(
        now=datetime(2026, 9, 14, 20, 44, tzinfo=UTC),
        output_dir=tmp_path / "site",
        history_file=tmp_path / "history" / "runs.csv",
    )
    cams = later.statuses["cams"]
    assert cams.state == "failed" and cams.last_ok == "2026-09-14T14:44:00Z"


# --- schema validation -----------------------------------------------------------------


def minimal_forecast():
    from hazier.schema import SmokeModel

    return Forecast(
        "2026-09-14T14:00:00Z", 72, [], [], [], [], SmokeModel("m", 850, 48, 48, 72.0, 10.0)
    )


def test_validator_accepts_a_minimal_forecast():
    assert validate_forecast(minimal_forecast()) == []


def test_validator_catches_type_and_invariant_errors():
    fc = minimal_forecast()
    assert validate_forecast(replace(fc, horizon_hours="72")) == [
        "forecast.horizon_hours: expected an integer"
    ]
    assert "not UTC ISO 8601" in validate_forecast(replace(fc, generated_at="2026-09-14 14:00"))[0]
    nan_model = replace(fc.smoke_model, removal_lifetime_hours=float("nan"))
    assert "not finite" in validate_forecast(replace(fc, smoke_model=nan_model))[0]


def test_validator_on_the_real_pipeline_output(sources, tmp_path, monkeypatch):
    captured = {}
    real = export.validate_forecast

    def spy(fc):
        captured["fc"] = fc
        return real(fc)

    monkeypatch.setattr(export, "validate_forecast", spy)
    run(tmp_path)
    fc = captured["fc"]
    assert real(fc) == []
    broken = replace(
        fc, trajectories=[replace(fc.trajectories[0], hours=fc.trajectories[0].hours[:-1])]
    )
    assert any("path and hours differ" in e for e in real(broken))
