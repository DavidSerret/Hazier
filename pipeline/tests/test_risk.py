import numpy as np
import pytest
from helpers import cluster, hotspot, uniform

from hazier import config
from hazier.geo import great_circle_km
from hazier.model import risk
from hazier.model import trajectories as tj

ZONES = {z.id: z for z in config.ZONES}
KL = ZONES["klang_valley"]
MODERATE = risk.thresholds()["moderate"]


def forecast(clusters, field, zones=config.ZONES):
    """Release, advect with dispersion and accumulate: the risk part of run_model."""
    particles = tj.release_particles(clusters)
    acc = risk.ZoneAccumulator(zones, len(clusters))
    adv = tj.advect(field, particles, [acc])
    return {z.id: z for z in risk.zone_forecasts(acc, clusters, adv.hours)}


# Fire 293 km due south of Klang Valley, wind blowing north at 5 m/s (18 km/h): the plume
# reaches the zone edge (50 km before the centre) after about 13.5 h and the centre after 16 h.
SOUTH_OF_KL = cluster(0, KL.lat - 2.64, KL.lon, 1000.0, "Riau")
NORTHWARD = uniform(0.0, 5.0)


@pytest.fixture(scope="module")
def plume():
    return forecast([SOUTH_OF_KL], NORTHWARD)


# --- geometry and levels ---------------------------------------------------------------


def test_great_circle_km():
    assert great_circle_km(0.0, 100.0, 0.0, 101.0) == pytest.approx(111.195, abs=0.01)
    assert great_circle_km(1.0, 100.0, 2.0, 100.0) == pytest.approx(111.195, abs=0.01)
    assert great_circle_km(3.0, 101.0, 1.0, 104.0) == pytest.approx(
        great_circle_km(1.0, 104.0, 3.0, 101.0)
    )


def test_level_boundaries():
    th = risk.thresholds()
    assert risk.level_of(0.0) == "low"
    assert risk.level_of(th["moderate"] - 0.01) == "low"
    assert risk.level_of(th["moderate"]) == "moderate"
    assert risk.level_of(th["high"]) == "high"
    assert risk.level_of(th["very_high"] * 10) == "very_high"


# --- accumulation ----------------------------------------------------------------------


def test_accumulator_counts_particles_within_the_radius():
    # Due north of the centre: 0 km, 49 km and 51 km; clusters 0, 0, 1.
    dlat = np.degrees(np.array([0.0, 49.0, 51.0]) / 6371.0)
    particles = tj.Particles(
        cluster_index=np.array([0, 0, 1]),
        release_hour=np.zeros(3),
        lat0=KL.lat + dlat,
        lon0=np.full(3, KL.lon),
        weight=np.array([1.0, 2.0, 4.0]),
    )
    acc = risk.ZoneAccumulator((KL,), n_clusters=2)
    acc.start(particles, np.arange(3.0))
    acc.observe(1, np.arange(3), particles.lat0, particles.lon0)
    assert acc.load[0].tolist() == [0.0, 3.0, 0.0]
    assert acc.cluster_load[0].tolist() == [3.0, 0.0]


# --- synthetic plume: which zone is hit, when, and from where ---------------------------


def test_downwind_zone_is_hit_at_the_expected_time(plume):
    kl = plume["klang_valley"]
    assert kl.eta_hours is not None and 10 <= kl.eta_hours <= 17
    assert kl.risk_level in ("moderate", "high", "very_high")
    # Fires keep burning, so once the plume is established the zone stays smoky.
    assert all(p.index >= MODERATE for p in kl.risk_series if 20 <= p.hour <= 72)
    assert [s.region for s in kl.sources] == ["Riau"] and kl.sources[0].share == 1.0


def test_eta_range_brackets_the_eta(plume):
    kl = plume["klang_valley"]
    assert kl.eta_earliest_hours <= kl.eta_hours <= kl.eta_latest_hours
    assert kl.eta_latest_hours - kl.eta_earliest_hours <= 12  # a range, not the whole window


def test_zones_away_from_the_plume_stay_clean(plume):
    kuching = plume["kuching"]  # 960 km east of the plume
    assert kuching.risk_index == 0.0 and kuching.risk_level == "low"
    assert kuching.eta_hours is None and kuching.peak_hour is None
    assert kuching.eta_earliest_hours is None and kuching.eta_latest_hours is None
    assert kuching.sources == []
    assert plume["singapore"].risk_index < MODERATE  # 240 km east of the plume axis


def test_series_and_levels_are_consistent(plume):
    for z in plume.values():
        assert [p.hour for p in z.risk_series] == list(range(73))
        assert all(p.level == risk.level_of(p.index) for p in z.risk_series)
        assert z.risk_index == max(p.index for p in z.risk_series)
        assert z.risk_level == risk.level_of(z.risk_index)


def test_small_fire_gives_no_eta():
    weak = cluster(0, SOUTH_OF_KL.lat, SOUTH_OF_KL.lon, 30.0, "Riau")
    kl = forecast([weak], NORTHWARD)["klang_valley"]
    assert 0 < kl.risk_index < MODERATE  # smoke arrives, but not enough for "moderate"
    assert kl.eta_hours is None and kl.eta_earliest_hours is None and kl.eta_latest_hours is None
    assert kl.sources[0].region == "Riau"


def test_attribution_sums_to_100_percent_and_follows_frp():
    clusters = [
        cluster(0, SOUTH_OF_KL.lat, KL.lon - 0.05, 750.0, "Riau"),
        cluster(1, SOUTH_OF_KL.lat, KL.lon + 0.05, 250.0, "Jambi"),
    ]
    kl = forecast(clusters, NORTHWARD)["klang_valley"]
    assert sum(round(s.share * 1000) for s in kl.sources) == 1000
    assert sum(s.share for s in kl.sources) == pytest.approx(1.0, abs=1e-9)
    shares = {s.region: s.share for s in kl.sources}
    assert 0.65 <= shares["Riau"] <= 0.85 and shares["Riau"] > shares["Jambi"]


def test_forecast_is_reproducible():
    a = forecast([SOUTH_OF_KL], NORTHWARD)["klang_valley"]
    b = forecast([SOUTH_OF_KL], NORTHWARD)["klang_valley"]
    assert a.risk_series == b.risk_series and a.eta_hours == b.eta_hours


# --- pieces of the zone forecast -------------------------------------------------------


def test_source_shares_use_largest_remainder():
    clusters = [cluster(i, 0.0, 100.0, 1.0, r) for i, r in enumerate(["A", "B", "C", ""])]
    shares = risk.source_shares(np.array([1.0, 1.0, 1.0, 0.0]), clusters)
    assert [(s.region, s.share) for s in shares] == [("A", 0.334), ("B", 0.333), ("C", 0.333)]
    other = risk.source_shares(np.array([0.0, 0.0, 0.0, 2.0]), clusters)
    assert [(s.region, s.share) for s in other] == [("Other", 1.0)]
    assert risk.source_shares(np.zeros(4), clusters) == []


def test_eta_range_is_the_climb_around_moderate():
    hours = np.arange(73.0)
    ramp = np.clip((hours - 10) * MODERATE / 4, 0, None)  # reaches moderate at 14 h
    # half of moderate at 12 h, twice moderate at 18 h
    assert risk.eta_range(ramp, hours, MODERATE) == (12.0, 18.0)
    marginal = np.minimum(ramp, 1.5 * MODERATE)  # never reaches twice moderate
    assert risk.eta_range(marginal, hours, MODERATE) == (12.0, None)
    faint = np.minimum(ramp, 0.9 * MODERATE)  # never reaches moderate: no ETA, no range
    assert risk.eta_range(faint, hours, MODERATE) == (None, None)


# --- the whole chain -------------------------------------------------------------------


def test_run_model_end_to_end():
    # 20 hotspots of 50 MW in one cell near Pekanbaru: one 1000 MW cluster in Riau.
    hotspots = [hotspot(0.5 + 0.005 * i, 101.69, 50.0, i=i) for i in range(20)]
    run = risk.run_model(hotspots, NORTHWARD)
    assert [(c.region, c.frp_mw) for c in run.clusters] == [("Riau", 1000.0)]
    assert run.n_particles == 72 * config.PARTICLES_PER_RELEASE and run.n_tracers == 72
    zones = {z.id: z for z in run.zones}
    assert zones["klang_valley"].eta_hours is not None
    assert zones["klang_valley"].sources[0].region == "Riau"
    assert zones["kuching"].risk_index == 0.0
    assert run.selection.near_zone > 0 and len(run.selection.trajectories) == 72
    assert run.density.load.sum() > 0
