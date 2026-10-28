import math
from dataclasses import fields

import numpy as np
import pytest
from helpers import cluster, hotspot, uniform

from hazier import config
from hazier.geo import great_circle_km
from hazier.model import smoke as sm
from hazier.model import trajectories as tj
from hazier.schema import ZoneForecast

ZONES = {z.id: z for z in config.ZONES}
KL = ZONES["klang_valley"]
ARRIVAL = config.SMOKE_ARRIVAL_THRESHOLD


def smoke(clusters, field, zones=config.ZONES, lifetime_hours=None):
    """Release, advect with dispersion and accumulate: the smoke part of run_model."""
    particles = tj.release_particles(clusters)
    acc = sm.ZoneAccumulator(zones, len(clusters), lifetime_hours)
    tj.advect(field, particles, [acc])
    return {s.zone.id: s for s in sm.zone_smoke(acc, clusters)}


# Fire 293 km due south of Klang Valley, wind blowing north at 5 m/s (18 km/h): the plume
# reaches the zone edge (50 km before the centre) after about 13.5 h and the centre after 16 h.
SOUTH_OF_KL = cluster(0, KL.lat - 2.64, KL.lon, 1000.0, "Riau")
NORTHWARD = uniform(0.0, 5.0)


@pytest.fixture(scope="module")
def plume():
    return smoke([SOUTH_OF_KL], NORTHWARD)


def test_great_circle_km():
    assert great_circle_km(0.0, 100.0, 0.0, 101.0) == pytest.approx(111.195, abs=0.01)
    assert great_circle_km(1.0, 100.0, 2.0, 100.0) == pytest.approx(111.195, abs=0.01)
    assert great_circle_km(3.0, 101.0, 1.0, 104.0) == pytest.approx(
        great_circle_km(1.0, 104.0, 3.0, 101.0)
    )


# --- accumulation and removal ----------------------------------------------------------


def particles_north_of_kl(release_hours):
    # Due north of the centre: 0 km, 49 km and 51 km; clusters 0, 0, 1.
    dlat = np.degrees(np.array([0.0, 49.0, 51.0]) / 6371.0)
    return tj.Particles(
        cluster_index=np.array([0, 0, 1]),
        release_hour=np.asarray(release_hours, dtype=float),
        lat0=KL.lat + dlat,
        lon0=np.full(3, KL.lon),
        weight=np.array([1.0, 2.0, 4.0]),
    )


def test_accumulator_counts_particles_within_the_radius():
    particles = particles_north_of_kl([1.0, 1.0, 1.0])  # observed at their release: age 0
    acc = sm.ZoneAccumulator((KL,), n_clusters=2)
    acc.start(particles, np.arange(3.0))
    acc.observe(1, np.arange(3), particles.lat0, particles.lon0)
    assert acc.load[0].tolist() == [0.0, 3.0, 0.0]
    assert acc.cluster_load[0].tolist() == [3.0, 0.0]


def test_accumulator_applies_removal_by_age():
    particles = particles_north_of_kl([-24.0, -24.0, -24.0])  # released a day before t0
    acc = sm.ZoneAccumulator((KL,), n_clusters=2, lifetime_hours=72.0)
    hours = np.arange(-24.0, 3.0)
    acc.start(particles, hours)
    acc.observe(26, np.arange(3), particles.lat0, particles.lon0)  # hour 2: age 26 h
    assert acc.hours.tolist() == [0.0, 1.0, 2.0]
    assert acc.load[0, 2] == pytest.approx(3.0 * math.exp(-26 / 72))
    acc.observe(10, np.arange(3), particles.lat0, particles.lon0)  # hour -14: before t0
    assert acc.load[0, :2].tolist() == [0.0, 0.0]  # the past is not counted


def test_shorter_lifetime_gives_less_smoke_and_the_same_origin(plume):
    fast = smoke([SOUTH_OF_KL], NORTHWARD, lifetime_hours=48.0)["klang_valley"]
    slow = smoke([SOUTH_OF_KL], NORTHWARD, lifetime_hours=120.0)["klang_valley"]
    mid = plume["klang_valley"]
    assert fast.peak < mid.peak < slow.peak
    # About 16 h of travel: the ratio follows exp(-t / lifetime) to within dispersion noise.
    assert fast.peak / slow.peak == pytest.approx(math.exp(-16 / 48 + 16 / 120), rel=0.15)
    assert [s.region for s in fast.sources] == [s.region for s in slow.sources] == ["Riau"]


# --- synthetic plume: which zone is reached, when, and from where -----------------------


def test_downwind_zone_is_reached_at_the_expected_time(plume):
    kl = plume["klang_valley"]
    assert kl.eta is not None and 10 <= kl.eta <= 17
    # Fires keep burning, so once the plume is established the zone stays smoky.
    assert (kl.series[(kl.hours >= 20)] >= ARRIVAL).all()
    assert [s.region for s in kl.sources] == ["Riau"] and kl.sources[0].share == 1.0


def test_eta_range_brackets_the_eta(plume):
    kl = plume["klang_valley"]
    assert kl.eta_earliest <= kl.eta <= kl.eta_latest
    assert kl.eta_latest - kl.eta_earliest <= 12  # a range, not the whole window


def test_zones_away_from_the_plume_stay_clean(plume):
    kuching = plume["kuching"]  # 960 km east of the plume
    assert kuching.peak == 0.0 and kuching.peak_hour is None
    assert kuching.eta is None and kuching.eta_earliest is None and kuching.eta_latest is None
    assert kuching.sources == []
    assert plume["singapore"].peak < ARRIVAL  # 240 km east of the plume axis


def test_series_shape(plume):
    for s in plume.values():
        assert s.hours.tolist() == list(range(73)) and s.series.shape == (73,)
        assert s.peak == s.series.max()


def test_small_fire_gives_no_eta():
    weak = cluster(0, SOUTH_OF_KL.lat, SOUTH_OF_KL.lon, 30.0, "Riau")
    kl = smoke([weak], NORTHWARD)["klang_valley"]
    assert 0 < kl.peak < ARRIVAL  # smoke arrives, but too little to count
    assert kl.eta is None and kl.eta_earliest is None and kl.eta_latest is None
    assert kl.sources[0].region == "Riau"


def test_attribution_sums_to_100_percent_and_follows_frp():
    clusters = [
        cluster(0, SOUTH_OF_KL.lat, KL.lon - 0.05, 750.0, "Riau"),
        cluster(1, SOUTH_OF_KL.lat, KL.lon + 0.05, 250.0, "Jambi"),
    ]
    kl = smoke(clusters, NORTHWARD)["klang_valley"]
    assert sum(round(s.share * 1000) for s in kl.sources) == 1000
    assert sum(s.share for s in kl.sources) == pytest.approx(1.0, abs=1e-9)
    shares = {s.region: s.share for s in kl.sources}
    assert 0.65 <= shares["Riau"] <= 0.85 and shares["Riau"] > shares["Jambi"]


def test_smoke_is_reproducible():
    a = smoke([SOUTH_OF_KL], NORTHWARD)["klang_valley"]
    b = smoke([SOUTH_OF_KL], NORTHWARD)["klang_valley"]
    assert np.array_equal(a.series, b.series) and a.eta == b.eta


# --- pieces ----------------------------------------------------------------------------


def test_source_shares_use_largest_remainder():
    clusters = [cluster(i, 0.0, 100.0, 1.0, r) for i, r in enumerate(["A", "B", "C", ""])]
    shares = sm.source_shares(np.array([1.0, 1.0, 1.0, 0.0]), clusters)
    assert [(s.region, s.share) for s in shares] == [("A", 0.334), ("B", 0.333), ("C", 0.333)]
    other = sm.source_shares(np.array([0.0, 0.0, 0.0, 2.0]), clusters)
    assert [(s.region, s.share) for s in other] == [("Other", 1.0)]
    assert sm.source_shares(np.zeros(4), clusters) == []


def test_eta_range_is_the_climb_around_the_threshold():
    hours = np.arange(73.0)
    ramp = np.clip((hours - 10) * ARRIVAL / 4, 0, None)  # reaches the threshold at 14 h
    # half of it at 12 h, twice it at 18 h
    assert sm.eta_range(ramp, hours, ARRIVAL) == (12.0, 18.0)
    marginal = np.minimum(ramp, 1.5 * ARRIVAL)  # never reaches twice the threshold
    assert sm.eta_range(marginal, hours, ARRIVAL) == (12.0, None)
    faint = np.minimum(ramp, 0.9 * ARRIVAL)  # never reaches it: no ETA, no range
    assert sm.eta_range(faint, hours, ARRIVAL) == (None, None)


def test_published_zone_has_no_smoke_level():
    names = {f.name for f in fields(ZoneForecast)}
    assert not {"risk_level", "risk_index", "risk_series"} & names
    assert {"smoke_eta_hours", "smoke_sources", "cams_now", "cams_trend"} <= names


def test_fmt_eta():
    assert sm.fmt_eta(None, None, None) == "-"
    assert sm.fmt_eta(0.0, 0.0, 0.0) == "already at t0"
    assert sm.fmt_eta(14.0, 12.0, 18.0) == "14 h (12 to 18)"
    assert sm.fmt_eta(7.0, 5.0, None) == "7 h (5 to >72)"


# --- the whole chain -------------------------------------------------------------------


def test_run_model_end_to_end():
    # 20 hotspots of 50 MW in one cell near Pekanbaru: one 1000 MW cluster in Riau.
    hotspots = [hotspot(0.5 + 0.005 * i, 101.69, 50.0, i=i) for i in range(20)]
    run = sm.run_model(hotspots, NORTHWARD, extra_lifetimes=(48.0, 120.0))
    assert [(c.region, c.frp_mw) for c in run.clusters] == [("Riau", 1000.0)]
    assert run.n_particles == 72 * config.PARTICLES_PER_RELEASE and run.n_tracers == 72
    zones = {z.id: z for z in run.zones}
    assert zones["klang_valley"].smoke_eta_hours is not None
    assert zones["klang_valley"].smoke_sources[0].region == "Riau"
    assert zones["kuching"].smoke_eta_hours is None and zones["kuching"].smoke_sources == []
    assert zones["klang_valley"].cams_now is None  # filled later by the CAMS step
    assert run.selection.clusters_shown == 1
    assert len(run.selection.trajectories) == min(72, config.TRAJ_EXPORT_MAX)
    assert run.hourly.load.shape[0] == 73 and run.hourly.load.sum() > 0
    assert len(zones["klang_valley"].smoke_index) == 73 and run.static.dropped == []
    assert run.density.load.sum() > 0
    # Sensitivity runs share the advection: same zones, ordered by lifetime.
    peaks = [s[0].peak for s in (run.sensitivity[48.0], run.smoke, run.sensitivity[120.0])]
    assert peaks == sorted(peaks) and peaks[0] > 0


def test_smoke_model_metadata():
    meta = sm.smoke_model_metadata(uniform(0.0, 5.0, past_hours=48))
    assert meta.removal_lifetime_hours == 72.0 and meta.arrival_threshold == ARRIVAL
    assert meta.past_hours == 48 and meta.lookback_hours == 48 and meta.level_hpa == 850
