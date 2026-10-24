import sys
import types
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from gribfiles import axis, write_grib

from hazier import config
from hazier.schema import SourceStatus
from hazier.sources import ads, cams
from hazier.sources.cams import CamsGridResult

NOW = datetime(2026, 10, 24, 10, 30, tzinfo=UTC)
T0 = datetime(2026, 10, 24, 10, tzinfo=UTC)
RUN = datetime(2026, 10, 24, 0, tzinfo=UTC)
LATS, LONS = axis(-8, 12, 0.4), axis(95.2, 120, 0.4)


def pm25_true(valid: datetime) -> np.ndarray:
    """ug/m3, linear in time and distinct per longitude."""
    h = (valid - T0) / timedelta(hours=1)
    return np.broadcast_to(20.0 + 0.1 * h + (LONS[None, :] - 95), (len(LATS), len(LONS)))


class FakeRequester:
    """Stands in for the ADS: writes the cartesian product of the request as GRIB (kg/m3)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.fail_runs: set[datetime] = set()

    def __call__(self, request, target, deadline):
        self.requests.append(request)
        older, newer = request["date"][0].split("/")
        hour = int(request["time"][0][:2])
        runs = [datetime.fromisoformat(d).replace(hour=hour, tzinfo=UTC) for d in (older, newer)]
        if runs[1] in self.fail_runs:
            raise ads.AdsError("request failed: the data is not available yet")
        messages = []
        for run in runs:
            for lead in request["leadtime_hour"]:
                valid = run + timedelta(hours=int(lead))
                messages.append({"name": "pm2p5", "run": run, "step": int(lead), "lats": LATS,
                                 "lons": LONS, "values": pm25_true(valid) * 1e-9})  # fmt: skip
        write_grib(target, messages)


@pytest.fixture
def fake():
    return FakeRequester()


def fetch(fake, tmp_path, now=NOW, **kwargs):
    return ads.fetch_ads_grid(now=now, cache_dir=tmp_path, requester=fake, **kwargs)


# --- Planning ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "run"),
    [
        (datetime(2026, 10, 24, 10, 30, tzinfo=UTC), datetime(2026, 10, 24, 0, tzinfo=UTC)),
        (datetime(2026, 10, 24, 9, 59, tzinfo=UTC), datetime(2026, 10, 23, 12, tzinfo=UTC)),
        (datetime(2026, 10, 24, 22, 0, tzinfo=UTC), datetime(2026, 10, 24, 12, tzinfo=UTC)),
    ],
)
def test_newest_run_is_the_newest_guaranteed_online(now, run):
    assert ads.newest_run(now) == run


def test_hours_before_the_run_come_from_the_run_a_day_earlier():
    assert ads.source_of(datetime(2026, 10, 23, 23, tzinfo=UTC), RUN) == (
        datetime(2026, 10, 23, 0, tzinfo=UTC),
        23,
    )
    assert ads.source_of(datetime(2026, 10, 24, 5, tzinfo=UTC), RUN) == (RUN, 5)


def test_leads_are_hourly_then_3_hourly():
    assert ads.leads_for(90) == [90]
    assert ads.leads_for(91) == [90, 93] and ads.leads_for(92) == [90, 93]
    assert ads.leads_for(93) == [93]


def test_plan_and_request():
    p = ads.plan(T0, RUN)
    assert len(p.hours) == 23 + 73 and p.hours[0] == datetime(2026, 10, 23, 11, tzinfo=UTC)
    assert p.leads == list(range(83))  # 0 to t0 + 72 h; the older run reuses 11 to 23
    r = p.request()
    assert r["date"] == ["2026-10-23/2026-10-24"] and r["time"] == ["00:00"]
    assert r["area"] == [12.0, 95.0, -8.0, 120.0] and r["data_format"] == "grib"
    assert r["variable"] == ["particulate_matter_2.5um"] and r["type"] == ["forecast"]


def test_plan_beyond_the_last_lead_is_impossible():
    assert ads.plan(T0, RUN - timedelta(hours=60)) is None


def test_assemble_interpolates_the_3_hourly_leads():
    t0 = datetime(2026, 10, 24, 20, tzinfo=UTC)
    p = ads.plan(t0, RUN)  # the last hour is lead 92: between 90 and 93
    assert 91 not in p.leads and {90, 93} <= set(p.leads)
    grids = {(r, lead): np.full((1, 1), float(lead)) for r in (RUN - timedelta(days=1), RUN)
             for lead in p.leads}  # fmt: skip
    out = ads.assemble(p, grids)
    assert out[-1, 0, 0] == pytest.approx(92.0) and out[-2, 0, 0] == pytest.approx(91.0)


# --- Fetch ---------------------------------------------------------------------------------


def test_grid_values_and_basis(fake, tmp_path):
    result = fetch(fake, tmp_path)
    g = result.grid
    assert result.status.state == "ok" and result.source == "ads" and result.missing_points == 0
    assert g.hourly.shape == (73, 51, 63) and g.lons[0] == 95.2 and g.lats[0] == -8
    assert g.times[0] == np.datetime64("2026-10-24T10:00:00")
    assert np.allclose(g.hourly[0], pm25_true(T0), rtol=1e-3)
    # The 24 h mean at t0 of a series rising 0.1 per hour is the value 11.5 h earlier.
    assert np.allclose(g.basis[0], pm25_true(T0 - timedelta(hours=11.5)), rtol=1e-3)
    assert result.run_init == RUN and "0.4 deg" in result.note


def test_failed_newest_run_tries_the_one_before(fake, tmp_path):
    fake.fail_runs.add(RUN)
    result = fetch(fake, tmp_path)
    assert result.status.state == "ok" and result.run_init == RUN - timedelta(hours=12)
    assert len(fake.requests) == 2


def test_every_run_failing_fails_with_the_reasons(fake, tmp_path):
    fake.fail_runs |= {RUN, RUN - timedelta(hours=12)}
    result = fetch(fake, tmp_path)
    assert result.grid is None and result.status.state == "failed"
    assert "not available yet" in result.status.detail


def test_cache_avoids_a_second_request(fake, tmp_path):
    fetch(fake, tmp_path)
    second = fetch(fake, tmp_path)
    assert len(fake.requests) == 1 and second.from_cache


def test_missing_key_is_reported(monkeypatch, tmp_path):
    monkeypatch.delenv(config.ADS_KEY_ENV, raising=False)
    monkeypatch.setattr(ads, "load_env", lambda: None)
    result = ads.fetch_ads_grid(now=NOW, cache_dir=tmp_path)
    assert result.status.state == "failed" and "ADS_API_KEY is not set" in result.status.detail


class FakeRemote:
    def __init__(self, states):
        self.states = list(states)
        self.deleted = False
        self.downloaded = None

    @property
    def status(self):
        return self.states[0]

    @property
    def results_ready(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if state == "failed":
            raise RuntimeError("The job failed: data not available")
        return state == "successful"

    def delete(self):
        self.deleted = True

    def download(self, target):
        self.downloaded = target


def fake_cdsapi(monkeypatch, remote, clock):
    module = types.ModuleType("cdsapi")

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["url"] == config.ADS_URL and kwargs["key"] == "secret-key"
            assert kwargs["wait_until_complete"] is False

        def retrieve(self, name, request):
            assert name == config.ADS_DATASET
            return remote

    module.Client = Client
    monkeypatch.setitem(sys.modules, "cdsapi", module)
    monkeypatch.setenv(config.ADS_KEY_ENV, "secret-key")
    monkeypatch.setattr(ads.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ads.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))


def test_request_polls_until_ready_then_downloads(monkeypatch, tmp_path):
    clock = [0.0]
    remote = FakeRemote(["accepted", "running", "running", "successful"])
    fake_cdsapi(monkeypatch, remote, clock)
    ads.request_ads({}, tmp_path / "x.grib", deadline=600)
    assert remote.downloaded == str(tmp_path / "x.grib")


def test_request_gives_up_at_the_deadline_and_cancels(monkeypatch, tmp_path):
    clock = [0.0]
    remote = FakeRemote(["accepted"])
    fake_cdsapi(monkeypatch, remote, clock)
    with pytest.raises(ads.AdsError, match="still accepted in the ADS queue"):
        ads.request_ads({}, tmp_path / "x.grib", deadline=120)
    assert remote.deleted and clock[0] <= 120


def test_request_failure_never_leaks_the_key(monkeypatch, tmp_path):
    clock = [0.0]
    remote = FakeRemote(["failed"])
    fake_cdsapi(monkeypatch, remote, clock)
    with pytest.raises(ads.AdsError) as err:
        ads.request_ads({}, tmp_path / "x.grib", deadline=120)
    assert "secret-key" not in str(err.value) and "data not available" in str(err.value)


# --- The fallback --------------------------------------------------------------------------


def test_cams_grid_falls_back_to_open_meteo_at_2_degrees(monkeypatch):
    failed = CamsGridResult(None, SourceStatus("cams", "failed", None, "ADS: queue timeout"))
    backup = CamsGridResult(object(), SourceStatus("cams", "ok", "x"), calls_used=143)
    seen = {}
    monkeypatch.setattr(ads, "fetch_ads_grid", lambda **kw: failed)

    def openmeteo(**kw):
        seen.update(kw)
        return backup

    monkeypatch.setattr(cams, "fetch_openmeteo_grid", openmeteo)
    result = cams.fetch_cams_grid(now=NOW)
    assert result is backup and result.source == "open_meteo"
    assert "ADS: queue timeout, Open-Meteo used" in result.status.detail
    assert "2 deg" in result.note and "step_deg" not in seen  # the 2 degree default


def test_open_meteo_fallback_grid_is_2_degrees():
    from hazier.sources.winds import grid_axes

    lats, lons = grid_axes(config.CAMS_FALLBACK_GRID_STEP_DEG)
    assert len(lats) * len(lons) == 143


def test_cams_grid_uses_the_ads_when_it_works(monkeypatch):
    primary = CamsGridResult(object(), SourceStatus("cams", "ok", "x"), source="ads")
    monkeypatch.setattr(ads, "fetch_ads_grid", lambda **kw: primary)
    monkeypatch.setattr(cams, "fetch_openmeteo_grid", lambda **kw: pytest.fail("no fallback"))
    assert cams.fetch_cams_grid(now=NOW) is primary
