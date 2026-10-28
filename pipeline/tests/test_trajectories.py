import json
import math
from dataclasses import asdict, replace

import numpy as np
import pytest
from helpers import T0, R, cluster, hotspot, make_field, particles_at, uniform

from hazier import config
from hazier.model import trajectories as tj


def km_between(lat1, lon1, lat2, lon2):
    dx = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return R / 1000 * math.hypot(dx, math.radians(lat2 - lat1))


# --- clustering ------------------------------------------------------------------------


def test_hotspots_in_one_cell_form_one_cluster():
    hs = [
        hotspot(0.51, 101.52, 10.0, "2026-09-14T05:00:00Z"),
        hotspot(0.55, 101.58, 30.0, "2026-09-14T06:30:00Z"),
    ]
    (c,) = tj.cluster_hotspots(hs, min_frp_mw=0).clusters
    assert c.frp_mw == 40.0 and c.hotspot_count == 2
    assert c.latest_at == "2026-09-14T06:30:00Z"
    # FRP-weighted centroid: pulled towards the stronger detection
    assert c.lat == pytest.approx((0.51 * 10 + 0.55 * 30) / 40)
    assert c.lon == pytest.approx((101.52 * 10 + 101.58 * 30) / 40)


def test_cell_edge_splits_clusters():
    # 0.5 N is a cell edge: -8 + 34 * 0.25
    hs = [hotspot(0.49, 101.6, 30.0), hotspot(0.51, 101.6, 30.0)]
    assert len(tj.cluster_hotspots(hs, min_frp_mw=0).clusters) == 2


def test_clusters_ranked_by_frp_and_weak_ones_dropped():
    hs = [
        hotspot(0.6, 101.6, 20.0),
        hotspot(2.0, 99.0, 100.0),
        hotspot(0.10, 110.20, 5.0),  # these two share a cell: 9 MW in total, below 15
        hotspot(0.12, 110.21, 4.0),
    ]
    res = tj.cluster_hotspots(hs, min_frp_mw=15.0)
    assert [c.frp_mw for c in res.clusters] == [100.0, 20.0]
    assert [c.id for c in res.clusters] == [0, 1]
    assert res.cells == 3 and res.dropped == 1
    assert res.dropped_frp_share == pytest.approx(9 / 129)


def test_clustering_is_order_independent():
    rng = np.random.default_rng(1)
    hs = [
        hotspot(float(rng.uniform(-2, 3)), float(rng.uniform(100, 112)), float(rng.uniform(1, 80)))
        for _ in range(400)
    ]
    a = tj.cluster_hotspots(hs).clusters
    b = tj.cluster_hotspots(list(reversed(hs))).clusters
    assert [(c.id, c.hotspot_count) for c in a] == [(c.id, c.hotspot_count) for c in b]
    for ca, cb in zip(a, b, strict=True):
        assert (ca.lat, ca.lon, ca.frp_mw) == pytest.approx((cb.lat, cb.lon, cb.frp_mw))


def test_clustering_empty_input():
    res = tj.cluster_hotspots([])
    assert res.clusters == [] and res.cells == 0 and res.dropped == 0


# --- release ---------------------------------------------------------------------------


def test_default_release_is_hourly_with_20_particles():
    p = tj.release_particles([cluster(0, 0.5, 101.5, 480.0)])
    assert len(p) == 72 * 20
    assert np.unique(p.release_hour).tolist() == list(range(72))
    assert np.bincount(p.release_hour.astype(int)).tolist() == [20] * 72


def test_release_order_and_positions():
    clusters = [cluster(0, 0.5, 101.5, 480.0), cluster(1, 0.1, 110.2, 96.0)]
    p = tj.release_particles(clusters, particles_per_release=3, interval_hours=6)
    assert len(p) == 2 * 12 * 3
    assert p.release_hour[:36:3].tolist() == list(range(0, 72, 6))
    assert p.release_hour[:6].tolist() == [0, 0, 0, 6, 6, 6]
    assert (p.cluster_index[:36] == 0).all() and (p.cluster_index[36:] == 1).all()
    assert (p.lat0[:36] == 0.5).all() and (p.lon0[36:] == 110.2).all()


def test_release_weights_are_absolute_emission_units():
    clusters = [cluster(0, 0.5, 101.5, 480.0), cluster(1, 0.1, 110.2, 96.0)]
    p = tj.release_particles(clusters, particles_per_release=4, interval_hours=1)
    # S / 48 h of emission per hour, split among the 4 particles of a release
    assert p.weight[0] == pytest.approx(480 / 48 / 4)
    assert p.weight[-1] == pytest.approx(96 / 48 / 4)
    # The total carried by a cluster depends on its FRP only, not on how it is split.
    coarse = tj.release_particles(clusters, particles_per_release=10, interval_hours=6)
    for parts in (p, coarse):
        assert parts.weight[parts.cluster_index == 0].sum() == pytest.approx(480 / 48 * 72)


def test_no_clusters_gives_no_particles():
    p = tj.release_particles([])
    assert len(p) == 0
    trajs = tj.trace(uniform(1.0, 1.0), p)
    assert trajs.lat.shape == (0, 73) and trajs.fate_counts()["horizon"] == 0


# --- unit conversion -------------------------------------------------------------------


def test_degrees_per_hour():
    dlon, dlat = tj.degrees_per_hour(0.0, 10.0, 0.0)
    assert dlat == pytest.approx(math.degrees(10 * 3600 / R)) and dlon == 0.0
    assert dlat == pytest.approx(0.3237, abs=1e-4)  # 10 m/s is about 0.32 degrees per hour
    # At 60 degrees a degree of longitude is half as long, so the same wind covers twice as many.
    dlon60, _ = tj.degrees_per_hour(10.0, 0.0, 60.0)
    assert dlon60 == pytest.approx(2 * math.degrees(10 * 3600 / R))


# --- uniform wind: exact analytic displacement (no dispersion) -------------------------


def test_uniform_northward_wind_is_exact():
    tr = tj.trace(uniform(0.0, 3.0), particles_at([(-5.0, 105.0)]))
    expected = -5.0 + np.degrees(3.0 * tr.hours * 3600 / R)
    assert np.allclose(tr.lat[0], expected, atol=1e-9, rtol=0)
    assert np.allclose(tr.lon[0], 105.0, atol=1e-12, rtol=0)
    assert tr.fate[0] == tj.Fate.HORIZON and tr.end_step[0] == 72


def test_uniform_eastward_wind_is_exact():
    tr = tj.trace(uniform(5.0, 0.0), particles_at([(2.0, 100.0)]))
    expected = 100.0 + np.degrees(5.0 * tr.hours * 3600 / (R * math.cos(math.radians(2.0))))
    assert np.allclose(tr.lon[0], expected, atol=1e-9, rtol=0)
    assert np.allclose(tr.lat[0], 2.0, atol=1e-12, rtol=0)


def test_uniform_wind_follows_the_rhumb_line():
    # A constant (u, v) keeps a constant compass heading: the path is a rhumb line.
    #   phi(t) = phi0 + v t / R
    #   lambda(t) = lambda0 + (u / v) [ln tan(pi/4 + phi/2) - ln tan(pi/4 + phi0/2)]
    # Latitude is exact; longitude is not, because d(lambda)/dt = u / (R cos(phi)) changes
    # along the path. The residual must be second-order truncation error: about 0.25 m after
    # 72 h and 1,000 km at dt = 1 h, divided by 4 each time dt is halved.
    u, v, lat0, lon0 = 4.0, 3.0, -4.0, 100.0
    field, start = uniform(u, v), particles_at([(lat0, lon0)])
    phi0 = math.radians(lat0)

    def merc(p):
        return np.log(np.tan(np.pi / 4 + p / 2))

    lon_errors = []
    for dt in (1.0, 0.5):
        tr = tj.trace(field, start, dt_hours=dt)
        phi = phi0 + v * tr.hours * 3600 / R
        lam = math.radians(lon0) + (u / v) * (merc(phi) - merc(phi0))
        assert np.allclose(tr.lat[0], np.degrees(phi), atol=1e-9, rtol=0)
        assert np.allclose(tr.lon[0], np.degrees(lam), atol=1e-5, rtol=0)  # about 1 m
        assert tr.fate[0] == tj.Fate.HORIZON
        lon_errors.append(abs(tr.lon[0, -1] - np.degrees(lam[-1])))
    assert 3.8 < lon_errors[0] / lon_errors[1] < 4.2


# --- convergence on a curved field (no dispersion) -------------------------------------

CENTRE_LAT, CENTRE_LON = 2.0, 108.0


def rotation(lat, lon, hour):
    """Rotation around (2 N, 108 E) that strengthens linearly in time.

    Linear in lat, lon and time, so bilinear / linear interpolation reproduces it exactly and
    the test measures the integrator alone. A particle 3 degrees from the centre turns more
    than one full circle in 72 h.
    """
    k = 2.5 * (1 + 0.3 * hour / 72)  # m/s per degree of distance from the centre
    return -k * (lat - CENTRE_LAT), k * (lon - CENTRE_LON)


def rk4_reference(lat, lon, dt=1 / 256, hours=72):
    """Independent reference: classic RK4 on the analytic field with a tiny step."""

    def f(la, lo, t):
        u, v = rotation(la, lo, t)
        c = 3600 / R * 180 / math.pi
        return u * c / math.cos(math.radians(la)), v * c

    t = 0.0
    for _ in range(round(hours / dt)):
        k1 = f(lat, lon, t)
        k2 = f(lat + dt / 2 * k1[1], lon + dt / 2 * k1[0], t + dt / 2)
        k3 = f(lat + dt / 2 * k2[1], lon + dt / 2 * k2[0], t + dt / 2)
        k4 = f(lat + dt * k3[1], lon + dt * k3[0], t + dt)
        lon += dt / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0])
        lat += dt / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1])
        t += dt
    return lat, lon


def test_heun_is_second_order():
    field = make_field(rotation)
    start = (2.0, 111.0)
    ref_lat, ref_lon = rk4_reference(*start)
    errors = []
    for dt in (1.0, 0.5, 0.25):
        tr = tj.trace(field, particles_at([start]), dt_hours=dt)
        assert tr.fate[0] == tj.Fate.HORIZON
        errors.append(km_between(tr.lat[0, -1], tr.lon[0, -1], ref_lat, ref_lon))

    # The path really is curved: it ends far from where a straight line would take it.
    angle = math.atan2(ref_lat - CENTRE_LAT, ref_lon - CENTRE_LON)
    assert abs(angle) > 0.3
    # Second order: halving dt divides the error by about 2^2 = 4.
    assert 3.5 < errors[0] / errors[1] < 4.5
    assert 3.5 < errors[1] / errors[2] < 4.5
    assert errors[0] < 5.0  # km after 72 h with the operational 1 h step


# --- dispersion ------------------------------------------------------------------------


def test_dispersion_spread_matches_diffusion_theory():
    # In still air particles only diffuse: after t, the spread per axis is sqrt(2 K t).
    k_h, n = 2.0e4, 4000
    tr = tj.trace(uniform(0.0, 0.0), particles_at([(0.0, 107.0)] * n), diffusivity_m2s=k_h)
    for hours in (24, 72):
        expected_km = math.sqrt(2 * k_h * hours * 3600) / 1000  # 59 km and 102 km
        dy_km = np.radians(tr.lat[:, hours]) * R / 1000
        dx_km = np.radians(tr.lon[:, hours] - 107.0) * R / 1000
        assert dy_km.std() == pytest.approx(expected_km, rel=0.05)
        assert dx_km.std() == pytest.approx(expected_km, rel=0.05)
        assert abs(dy_km.mean()) < 4 and abs(dx_km.mean()) < 4


def test_dispersion_is_reproducible_with_a_seed():
    field, parts = uniform(3.0, 2.0), particles_at([(0.0, 105.0)] * 50)
    a = tj.trace(field, parts, diffusivity_m2s=2e4, seed=7)
    b = tj.trace(field, parts, diffusivity_m2s=2e4, seed=7)
    c = tj.trace(field, parts, diffusivity_m2s=2e4, seed=8)
    assert np.array_equal(a.lat, b.lat, equal_nan=True)
    assert not np.array_equal(a.lat, c.lat, equal_nan=True)


def test_without_dispersion_particles_stay_together():
    tr = tj.trace(uniform(3.0, 2.0), particles_at([(0.0, 105.0)] * 5))
    assert (tr.lat == tr.lat[0]).all() and (tr.lon == tr.lon[0]).all()


def test_advect_disperses_by_default():
    recorder = tj.PathRecorder()
    tj.advect(uniform(3.0, 2.0), particles_at([(0.0, 105.0)] * 20), [recorder])
    assert recorder.lat[:, -1].std() > 0.1  # degrees: config dispersion is on


# --- observers -------------------------------------------------------------------------


class Counter:
    def start(self, particles, hours):
        self.positions = 0
        self.finite = True

    def observe(self, k, idx, lat, lon):
        self.positions += idx.size
        self.finite &= bool(np.isfinite(lat).all() and np.isfinite(lon).all())


def test_observers_see_every_position_once():
    parts = tj.release_particles([cluster(0, 0.0, 117.0, 100.0)], particles_per_release=3)
    counter = Counter()
    adv = tj.advect(uniform(6.0, 1.0), parts, [counter])  # some particles leave the domain
    assert adv.fate_counts()["left_domain"] > 0
    assert counter.positions == int((adv.end_step - adv.release_step + 1).sum())
    assert counter.finite


def test_density_grid_conserves_weight_hours():
    parts = tj.release_particles([cluster(0, 0.0, 105.0, 480.0)], particles_per_release=5)
    density = tj.DensityGrid(cell_deg=0.5, lifetime_hours=float("inf"))  # no removal
    adv = tj.advect(uniform(2.0, 1.0), parts, [density])
    expected = float((parts.weight * (adv.end_step - adv.release_step + 1)).sum())
    assert density.load.sum() == pytest.approx(expected)


def test_density_grid_applies_removal():
    parts = tj.release_particles([cluster(0, 0.0, 105.0, 480.0)], particles_per_release=5)
    density = tj.DensityGrid(cell_deg=0.5, lifetime_hours=72.0)
    adv = tj.advect(uniform(2.0, 1.0), parts, [density])
    expected = 0.0
    for p in range(len(parts)):
        ages = adv.hours[adv.release_step[p] : adv.end_step[p] + 1] - parts.release_hour[p]
        expected += parts.weight[p] * np.exp(-ages / 72.0).sum()
    assert density.load.sum() == pytest.approx(expected)


def test_removal_factor():
    assert tj.removal_factor(0.0) == 1.0
    assert tj.removal_factor(72.0, 72.0) == pytest.approx(math.exp(-1))
    assert tj.removal_factor(config.REMOVAL_LIFETIME_HOURS) == pytest.approx(math.exp(-1))
    assert tj.removal_factor(500.0, float("inf")) == 1.0


# --- integration bookkeeping -----------------------------------------------------------


def test_time_axis():
    tr = tj.trace(uniform(1.0, 1.0), particles_at([(0.0, 105.0)]))
    assert tr.hours.tolist() == list(range(73))
    assert tr.times[0] == T0 and tr.times[-1] == T0 + np.timedelta64(72, "h")


def test_late_release_starts_at_its_hour():
    tr = tj.trace(uniform(2.0, 2.0), particles_at([(0.0, 105.0)], release_hours=[6]))
    assert np.isnan(tr.lat[0, :6]).all()
    assert (tr.lat[0, 6], tr.lon[0, 6]) == (0.0, 105.0)
    assert tr.release_step[0] == 6 and tr.end_step[0] == 72
    assert np.isfinite(tr.lat[0, 6:]).all()


def test_vectorised_matches_one_by_one():
    field = make_field(rotation)
    points, releases = [(2.0, 111.0), (0.0, 104.0), (4.0, 108.5)], [0, 6, 12]
    together = tj.trace(field, particles_at(points, releases))
    for i in range(3):
        alone = tj.trace(field, particles_at([points[i]], [releases[i]]))
        assert np.array_equal(together.lat[i], alone.lat[0], equal_nan=True)
        assert np.array_equal(together.lon[i], alone.lon[0], equal_nan=True)


def test_invalid_step_release_or_diffusivity_raises():
    field, one = uniform(1.0, 1.0), particles_at([(0.0, 105.0)])
    with pytest.raises(ValueError):
        tj.trace(field, one, dt_hours=5)
    with pytest.raises(ValueError):
        tj.trace(field, particles_at([(0.0, 105.0)], [1.5]))
    with pytest.raises(ValueError):
        tj.trace(field, one, diffusivity_m2s=-1.0)


# --- domain exit -----------------------------------------------------------------------


def test_particle_leaving_the_domain_stops_inside_it():
    tr = tj.trace(uniform(10.0, 0.0), particles_at([(0.0, 117.0)]))
    assert tr.fate[0] == tj.Fate.LEFT_DOMAIN
    end = tr.end_step[0]
    assert end == 9  # 3 degrees at about 0.32 degrees per hour
    kept = tr.lon[0, : end + 1]
    assert np.isfinite(kept).all() and (kept <= config.LON_MAX).all()
    assert np.isnan(tr.lon[0, end + 1 :]).all()
    step = tj.degrees_per_hour(10.0, 0.0, 0.0)[0]
    assert tr.lon[0, end] + step > config.LON_MAX  # the next step would have crossed the edge


# --- missing winds ---------------------------------------------------------------------


def test_missing_wind_stops_the_particle_cleanly():
    def fn(lat, lon, hour):
        return np.where(lon >= 110, np.nan, 5.0), np.zeros(lat.shape)

    tr = tj.trace(make_field(fn), particles_at([(0.0, 104.0), (0.0, 111.0), (5.0, 96.0)]))
    # Heads east into the cell next to the missing column and stops there.
    assert tr.fate[0] == tj.Fate.MISSING_WIND and 0 < tr.end_step[0] < 72
    assert tr.lon[0, tr.end_step[0]] < 110
    # Released inside the missing area: stops at once, its path is the release point only.
    assert tr.fate[1] == tj.Fate.MISSING_WIND and tr.end_step[1] == 0
    # Never gets near the gap: unaffected.
    assert tr.fate[2] == tj.Fate.HORIZON
    assert tr.fate_counts() == {"horizon": 1, "left_domain": 0, "missing_wind": 2}
    for p in range(3):
        valid = slice(tr.release_step[p], tr.end_step[p] + 1)
        assert np.isfinite(tr.lat[p, valid]).all() and np.isfinite(tr.lon[p, valid]).all()
        assert np.isnan(tr.lat[p, tr.end_step[p] + 1 :]).all()


def test_missing_wind_later_in_time():
    def fn(lat, lon, hour):
        return np.where(hour >= 30, np.nan, 3.0), np.zeros(lat.shape)

    tr = tj.trace(make_field(fn), particles_at([(0.0, 100.0)]))
    assert tr.fate[0] == tj.Fate.MISSING_WIND
    assert tr.end_step[0] == 29  # the step from 29 h to 30 h needs the missing 30 h wind
    assert np.isfinite(tr.lon[0, :30]).all()


def test_dispersed_particles_never_hold_nan():
    def fn(lat, lon, hour):
        return np.where(lon >= 108, np.nan, 4.0), np.full(lat.shape, 1.0)

    parts = tj.release_particles([cluster(0, 0.0, 104.0, 100.0)], particles_per_release=10)
    counter = Counter()
    adv = tj.advect(make_field(fn), parts, [counter], diffusivity_m2s=2e4)
    assert adv.fate_counts()["missing_wind"] > 0
    assert counter.finite


# --- export selection ------------------------------------------------------------------


def tracers_for(clusters, field):
    return tj.trace(field, tj.release_particles(clusters, particles_per_release=1))


def test_allocate_lines_is_proportional_and_sums_to_the_cap():
    assert tj.allocate_lines(np.array([700.0, 200.0, 100.0]), 10).tolist() == [7, 2, 1]
    assert tj.allocate_lines(np.array([1000.0, 10.0]), 10).tolist() == [10, 0]
    assert tj.allocate_lines(np.array([0.0, 0.0]), 10).tolist() == [0, 0]
    lines = tj.allocate_lines(np.array([5.0, 3.0, 2.0, 1.0]), 7)
    assert lines.sum() == 7 and list(lines) == sorted(lines, reverse=True)


def test_lines_follow_smoke_amount_not_proximity():
    # A huge fire far from every zone and a small one just south of Singapore, wind northward.
    # The small fire carries 0.3% of the smoke: it gets no line, however close to the city.
    clusters = [cluster(0, -6.0, 115.0, 10000.0, "Big"), cluster(1, 0.5, 103.82, 30.0, "Small")]
    sel = tj.select_for_export(
        tracers_for(clusters, uniform(0.0, 3.0)), clusters, max_trajectories=20
    )
    ids = [t.cluster_id for t in sel.trajectories]
    assert ids.count(0) == 20 and ids.count(1) == 0 and sel.clusters_shown == 1


def test_lines_are_shared_in_proportion():
    clusters = [cluster(0, 0.6, 110.36, 3000.0, "K"), cluster(1, 0.5, 103.82, 1000.0, "S")]
    sel = tj.select_for_export(
        tracers_for(clusters, uniform(0.0, 3.0)), clusters, max_trajectories=8
    )
    ids = [t.cluster_id for t in sel.trajectories]
    assert ids.count(0) == 6 and ids.count(1) == 2


def test_lines_are_spread_over_the_release_times():
    clusters = [cluster(0, 0.5, 101.5, 480.0)]
    sel = tj.select_for_export(
        tracers_for(clusters, uniform(1.0, 2.0)), clusters, max_trajectories=5
    )
    assert [t.release_hour for t in sel.trajectories] == [0, 18, 36, 53, 71]


def test_selection_respects_the_cap_and_skips_particles_that_never_moved():
    def fn(lat, lon, hour):
        return np.where(lon > 101, np.nan, -1.0), np.zeros(lat.shape)

    clusters = [cluster(0, 0.5, 101.5, 300.0), cluster(1, 0.5, 99.0, 100.0)]
    tracers = tracers_for(clusters, make_field(fn))
    sel = tj.select_for_export(tracers, clusters, max_trajectories=200)
    assert {t.cluster_id for t in sel.trajectories} == {1}  # cluster 0 is stuck in the gap
    assert len(sel.trajectories) == 72  # every release of cluster 1, no more exist
    small = tj.select_for_export(tracers, clusters, max_trajectories=7)
    assert len(small.trajectories) == 7


def test_selection_is_deterministic():
    clusters = [cluster(0, 0.5, 103.0, 300.0), cluster(1, -1.0, 104.0, 100.0)]
    tracers = tracers_for(clusters, uniform(1.0, 3.0))

    def pick():
        sel = tj.select_for_export(tracers, clusters, max_trajectories=30)
        return [(t.cluster_id, t.hours[0]) for t in sel.trajectories]

    assert pick() == pick()


def test_selection_output_format():
    clusters = [cluster(0, 0.5, 101.5, 480.0, "Riau")]
    tracers = tracers_for(clusters, uniform(1.234567, 3.21))
    sel = tj.select_for_export(tracers, clusters, max_trajectories=100)
    t = next(t for t in sel.trajectories if t.hours[0] == 6)
    assert t.cluster_id == 0 and t.source_region == "Riau" and t.hours[-1] == 72
    assert t.release_hour == 6
    assert all(isinstance(h, int) for h in t.hours)
    assert len(t.path) == len(t.hours) == 67
    assert t.path[0] == (101.5, 0.5)
    assert all(round(x, 2) == x for point in t.path for x in point)
    assert t.weight == pytest.approx(480 / 48)  # one tracer stands for a whole hourly release
    assert [x.id for x in sel.trajectories] == list(range(len(sel.trajectories)))
    json.dumps([asdict(x) for x in sel.trajectories], allow_nan=False)  # valid JSON, no NaN


def test_release_hour_before_t0_is_kept():
    # First seen 10 h before t0 and recorded from t0: the path starts at hour 0, the release at -10.
    field = uniform(0.0, 3.0, past_hours=24)
    clusters = [replace(cluster(0, 0.5, 101.5, 480.0), first_at="2026-09-14T04:00:00Z")]
    parts = tj.release_particles(
        clusters, particles_per_release=1, t0=field.t0, earliest_hour=-24.0
    )
    tracers = tj.trace(field, parts, min_hour=0.0)
    sel = tj.select_for_export(tracers, clusters, max_trajectories=2)
    first = sel.trajectories[0]
    assert first.release_hour == -10 and first.hours[0] == 0


def test_hourly_density_conserves_the_smoke_in_the_domain():
    parts = particles_at([(0.0, 105.0), (2.0, 110.0), (-3.0, 100.0)])
    density = tj.HourlyDensity(cell_deg=0.5, lifetime_hours=float("inf"))
    tj.advect(uniform(1.0, 1.0), parts, [density], diffusivity_m2s=0.0)
    assert density.load.shape == (73, 40, 50)
    assert np.allclose(density.load.sum(axis=(1, 2)), 1.0)  # three particles of 1/3 each
    # At t0 the first particle sits in the cell around (0 N, 105 E).
    row, col = int((0.0 + 8) / 0.5), int((105.0 - 95) / 0.5)
    assert density.load[0, row, col] == pytest.approx(1 / 3)


def test_json_size_and_cluster_rounding():
    clusters = [cluster(0, 0.5, 101.5, 480.0)]
    sel = tj.select_for_export(tracers_for(clusters, uniform(1.0, 2.0)), clusters)
    text = json.dumps([asdict(t) for t in sel.trajectories], separators=(",", ":"))
    assert tj.json_size(sel.trajectories) == len(text.encode())
    (c,) = tj.clusters_for_export([cluster(0, 0.123456, 101.987654, 123.456)])
    assert (c.lat, c.lon, c.frp_mw) == (0.123, 101.988, 123.5)
