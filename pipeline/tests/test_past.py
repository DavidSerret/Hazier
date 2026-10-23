"""Smoke emitted before t0: release from the first detection, past winds, carried up to t0."""

from dataclasses import replace

import numpy as np
import pytest
from helpers import T0, cluster, hotspot, particles_at, uniform

from hazier import config
from hazier.model import smoke as sm
from hazier.model import trajectories as tj

KL = next(z for z in config.ZONES if z.id == "klang_valley")
KUCHING = next(z for z in config.ZONES if z.id == "kuching")
ARRIVAL = config.SMOKE_ARRIVAL_THRESHOLD


def iso(hours_from_t0):
    t = T0 + np.timedelta64(round(hours_from_t0 * 3600), "s")
    return f"{t}Z"


def seen(i, lat, lon, frp, first_hours, region="Riau"):
    """A cluster first detected first_hours from T0 (negative: before t0)."""
    c = cluster(i, lat, lon, frp, region)
    return replace(c, first_at=iso(first_hours))


# --- clustering and release schedule ---------------------------------------------------


def test_clusters_record_their_earliest_detection():
    hs = [
        hotspot(0.51, 101.52, 10.0, "2026-09-13T18:20:00Z"),
        hotspot(0.55, 101.58, 30.0, "2026-09-14T06:30:00Z"),
    ]
    (c,) = tj.cluster_hotspots(hs, min_frp_mw=0).clusters
    assert c.first_at == "2026-09-13T18:20:00Z" and c.latest_at == "2026-09-14T06:30:00Z"


def test_release_starts_at_the_first_detection():
    # Seen 5.4 h before t0: the first whole hour at or after the detection is -5.
    p = tj.release_particles([seen(0, 0.5, 101.5, 480.0, -5.4)], particles_per_release=2, t0=T0)
    hours = np.unique(p.release_hour)
    assert hours[0] == -5 and hours[-1] == 71 and len(hours) == 77
    assert len(p) == 77 * 2
    # Same rate before and after t0.
    assert np.all(p.weight == pytest.approx(480 / 48 / 2))


def test_release_start_rules():
    clusters = [
        seen(0, 0.5, 101.5, 100.0, -60.0),  # older than the field: clipped to its start
        seen(1, 0.5, 101.5, 100.0, 0.5),  # seen between t0 and now: starts at t0
        cluster(2, 0.5, 101.5, 100.0),  # no first_at: starts at t0
    ]
    p = tj.release_particles(clusters, particles_per_release=1, t0=T0, earliest_hour=-48.0)
    first = [p.release_hour[p.cluster_index == i].min() for i in range(3)]
    assert first == [-48.0, 0.0, 0.0]
    # Without t0 every cluster starts at t0, as before.
    p0 = tj.release_particles(clusters, particles_per_release=1)
    assert p0.release_hour.min() == 0.0


# --- advection over a field that starts before t0 --------------------------------------


def test_time_axis_runs_from_the_past_to_the_horizon():
    field = uniform(0.0, 3.0, past_hours=24)
    adv = tj.advect(field, particles_at([(0.0, 105.0)], release_hours=[-24]), diffusivity_m2s=0)
    assert adv.hours[0] == -24 and adv.hours[-1] == 72 and adv.t0_index == 24
    assert adv.forecast_hours.tolist() == list(range(73))
    assert adv.times[adv.t0_index] == T0
    assert adv.release_step[0] == 0 and adv.end_step[0] == 96


def test_particle_released_before_t0_is_downwind_at_t0():
    # 3 m/s northward for 24 h before t0: 259 km, about 2.33 degrees of latitude.
    field = uniform(0.0, 3.0, past_hours=24)
    tr = tj.trace(field, particles_at([(0.0, 105.0)], release_hours=[-24]))
    at_t0 = tr.lat[0, 24]
    assert at_t0 == pytest.approx(np.degrees(3.0 * 24 * 3600 / 6_371_000.0), abs=1e-9)


def test_release_before_the_field_starts_raises():
    with pytest.raises(ValueError):
        tj.advect(uniform(0.0, 3.0), particles_at([(0.0, 105.0)], release_hours=[-1]))


def test_recording_from_t0_only():
    field = uniform(10.0, 0.0, past_hours=24)
    parts = particles_at([(0.0, 104.0), (0.0, 117.0), (0.0, 104.0)], release_hours=[-10, -24, 5])
    tr = tj.trace(field, parts, min_hour=0.0, dtype=np.float32)
    assert tr.hours.tolist() == list(range(73)) and tr.lat.dtype == np.float32
    # Released at -10: recorded from t0, already 10 h downwind.
    assert tr.release_step[0] == 0
    assert tr.lon[0, 0] == pytest.approx(
        104.0 + 10 * tj.degrees_per_hour(10.0, 0.0, 0.0)[0], abs=1e-4
    )
    # Released at -24 near the east edge: left the domain before t0, nothing recorded.
    assert tr.end_step[1] == -1 < tr.release_step[1]
    assert np.isnan(tr.lat[1]).all()
    # Released at +5: recorded from its release.
    assert tr.release_step[2] == 5 and tr.lon[2, 5] == pytest.approx(104.0)


def test_observers_ignore_the_past():
    field = uniform(0.0, 3.0, past_hours=24)
    parts = tj.release_particles(
        [seen(0, KL.lat - 2.64, KL.lon, 500.0, -24)], particles_per_release=5, t0=T0,
        earliest_hour=-24.0,
    )  # fmt: skip
    # No removal here, so the grid must hold exactly weight x forecast hours.
    density = tj.DensityGrid(cell_deg=0.5, lifetime_hours=float("inf"))
    acc = sm.ZoneAccumulator((KL,), 1)
    adv = tj.advect(field, parts, [density, acc])
    k0 = adv.t0_index
    forecast_steps = np.clip(adv.end_step - np.maximum(adv.release_step, k0) + 1, 0, None)
    assert density.load.sum() == pytest.approx(float((parts.weight * forecast_steps).sum()))
    assert acc.hours.tolist() == list(range(73)) and acc.load.shape == (1, 73)


# --- batching --------------------------------------------------------------------------


def test_batching_does_not_change_deterministic_results():
    field = uniform(2.0, 3.0, past_hours=12)
    parts = tj.release_particles(
        [seen(0, 0.0, 104.0, 100.0, -12), seen(1, -2.0, 110.0, 50.0, -3)],
        particles_per_release=3, t0=T0, earliest_hour=-12.0,
    )  # fmt: skip
    one = tj.PathRecorder()
    many = tj.PathRecorder()
    a = tj.advect(field, parts, [one], diffusivity_m2s=0, batch_size=10_000)
    b = tj.advect(field, parts, [many], diffusivity_m2s=0, batch_size=7)
    assert np.array_equal(one.lat, many.lat, equal_nan=True)
    assert np.array_equal(a.end_step, b.end_step) and np.array_equal(a.fate, b.fate)


def test_batched_dispersion_is_reproducible():
    field = uniform(2.0, 3.0)
    parts = particles_at([(0.0, 104.0)] * 40)
    runs = []
    for _ in range(2):
        rec = tj.PathRecorder()
        tj.advect(field, parts, [rec], batch_size=16)
        runs.append(rec.lat)
    assert np.array_equal(runs[0], runs[1], equal_nan=True)
    assert np.nanstd(runs[0][:, -1]) > 0.1  # dispersion was on


def test_invalid_batch_size_raises():
    with pytest.raises(ValueError):
        tj.advect(uniform(1.0, 1.0), particles_at([(0.0, 105.0)]), batch_size=0)


# --- the point of it all: smoke already in the air at t0 -------------------------------

# Fire 293 km due south of Klang Valley, wind north at 5 m/s (18 km/h): smoke needs about 14 h
# to reach the zone. Seen 24 h before t0, its smoke is over the zone at t0 already.
SOUTH_OF_KL = (KL.lat - 2.64, KL.lon)


def zone_series(clusters, field, t0=T0):
    particles = tj.release_particles(clusters, t0=t0, earliest_hour=float(field.hours[0]))
    acc = sm.ZoneAccumulator(config.ZONES, len(clusters))
    tj.advect(field, particles, [acc])
    return {z.zone.id: z for z in sm.zone_smoke(acc, clusters)}


def test_smoke_released_before_t0_is_over_the_zone_at_t0():
    field = uniform(0.0, 5.0, past_hours=48)
    fire = [seen(0, *SOUTH_OF_KL, 1000.0, -24)]
    kl = zone_series(fire, field)["klang_valley"]
    assert kl.series[0] >= ARRIVAL  # smoky from the first forecast hour
    assert kl.eta == 0.0
    assert kl.sources[0].region == "Riau"
    assert zone_series(fire, field)["kuching"].peak == 0.0  # still clean


def test_without_the_past_the_same_fire_arrives_late():
    field = uniform(0.0, 5.0, past_hours=48)
    fire = [seen(0, *SOUTH_OF_KL, 1000.0, -24)]
    kl = zone_series(fire, field, t0=None)["klang_valley"]  # releases from t0 only
    assert kl.series[0] == 0.0
    assert kl.eta is not None and 10 <= kl.eta <= 17


def test_a_fire_first_seen_recently_has_no_smoke_at_t0():
    field = uniform(0.0, 5.0, past_hours=48)
    fire = [seen(0, *SOUTH_OF_KL, 1000.0, -2)]  # seen 2 h ago: smoke still 260 km away
    kl = zone_series(fire, field)["klang_valley"]
    assert kl.series[0] == 0.0
    assert kl.eta is not None and kl.eta <= 15  # 2 h earlier than from t0


def test_run_model_carries_past_smoke():
    field = uniform(0.0, 5.0, past_hours=48)
    hotspots = [hotspot(SOUTH_OF_KL[0] + 0.005 * i, SOUTH_OF_KL[1], 50.0, iso(-30), i)
                for i in range(20)]  # fmt: skip
    run = sm.run_model(hotspots, field)
    assert run.clusters[0].first_at == iso(-30)
    assert run.n_past_particles == 30 * config.PARTICLES_PER_RELEASE
    assert 0 < run.n_in_air_at_t0 <= run.n_past_particles
    smoke = {s.zone.id: s for s in run.smoke}
    assert smoke["klang_valley"].series[0] >= ARRIVAL
    assert smoke["kuching"].peak == 0.0
    zones = {z.id: z for z in run.zones}
    assert zones["klang_valley"].smoke_eta_hours == 0.0
    # Display trajectories start at t0 at the latest, whatever their release time.
    assert all(t.hours[0] >= 0 for t in run.selection.trajectories)
    assert any(t.hours[0] == 0 and t.path[0][1] > SOUTH_OF_KL[0] + 0.5
               for t in run.selection.trajectories)  # fmt: skip
