# Methodology

## Overview

## Data sources

## Wind field

Hazier moves smoke with the wind at a single pressure level, 850 hPa, about 1.5 km above sea level.

- **Source:** Open-Meteo forecast API, model `ecmwf_ifs025` (ECMWF IFS at 0.25 degrees). The model is named explicitly, never "best match", so a run is reproducible, and the model run time is recorded with the data.
- **Grid:** 1 degree over 95 to 120 E and 8 S to 12 N, which is 21 x 26 = 546 points. Each point takes the value of the nearest 0.25 degree model cell.
- **Time:** hourly, from the current hour to 72 h ahead. ECMWF publishes pressure-level fields every 3 h and Open-Meteo interpolates them to hourly.
- **Components:** the API gives the wind speed and the direction the wind blows from (meteorological convention). With that direction θ measured clockwise from north:

  ```
  u = -speed * sin(θ)    eastward component, m/s
  v = -speed * cos(θ)    northward component, m/s
  ```

  A wind from the north (θ = 0) blows towards the south: u = 0, v = -speed.

**Why 850 hPa.** Smoke from the peat and vegetation fires of Sumatra and Borneo is mostly injected low, within the boundary layer (roughly the lowest 1 to 2 km), and travels long distances near its top. A single representative level is the simplest defensible choice for phase 1. Its limits are listed under [Limitations](#limitations).

## Fire sources

### Clustering

VIIRS sees a fire as many 375 m pixels, and the region can produce more than 15,000 detections in 48 h. Tracking each pixel would be slow and would let large fires dominate simply by covering more pixels. Hotspots are therefore grouped by deterministic grid binning: a 0.25 degree grid (about 28 km) aligned to the corner of the domain, one cluster per occupied cell. Each cluster has:

- **Release point:** the FRP-weighted centroid of its hotspots, so smoke starts where the burning is strongest.
- **Strength S:** the summed fire radiative power (FRP) of its detections over the last 48 h.
- The number of hotspots and the time of the most recent detection.

Summed FRP mixes intensity and persistence, because a fire seen on several satellite overpasses counts each time. It is used as a proxy for emission strength (below), not as a physical emission rate.

Clusters with a summed FRP below 25 MW are dropped as weak or spurious sources. On 23 October 2026 this removed 420 of 830 clusters but only 2.2% of the total FRP: many weak, isolated detections and very little smoke.

### Source regions

Each cluster is assigned to the province, state or country its centroid falls in, using Natural Earth admin-1 outlines (public domain), reduced to the countries around the domain, clipped and simplified to about 1 km (`pipeline/hazier/data/regions.geojson`, 158 KB; see the README next to it). Indonesian provinces and Malaysian states keep their English names; other countries are one region each. A cluster whose centroid falls just off the coast (a fire on the shore) takes the nearest region within 25 km; anything farther out is "Other". In this edition of Natural Earth, North Kalimantan is still part of East Kalimantan.

### Particle release

Fires emit continuously. Each cluster releases 20 particles every hour, at 0, 1, ..., 71 h; nothing is released at 72 h because it would have no time to move. Hourly releases keep consecutive puffs about 20 km apart, well inside a 50 km zone, so a zone's smoke does not flicker between puffs.

Under the persistent-fire assumption each cluster emits at a constant rate proportional to its strength, taken as S / 48 h. Each release carries one hour of that emission, split equally among its particles:

```
weight = (S / 48 h) * 1 h / 20
```

The unit, an **emission unit**, is the smoke emitted in one hour by a source averaging 1 MW of detected FRP per hour of observation. The weights are absolute, not shares of the day's total: a zone's smoke index must grow with the size of the fires, otherwise a single small fire on a quiet day would look like an episode.

## Trajectory model

### Equations of motion

Each particle moves with the wind. In longitude λ and latitude φ (in radians), on a sphere of radius R = 6,371 km:

```
dφ/dt = v / R
dλ/dt = u / (R cos φ)
```

Moving v metres north changes the latitude by v / R radians. A circle of latitude has radius R cos φ, so moving u metres east changes the longitude by u / (R cos φ) radians. At the equator, 10 m/s is about 0.32 degrees per hour. The domain stays within 12 degrees of the equator, so cos φ is at least 0.97 and the division is always well behaved.

### Integration

Trajectories run forward to the end of the 72 h horizon with a time step Δt = 1 h, using Heun's method, a second-order Runge-Kutta (RK2) predictor-corrector:

```
predictor:  x* = x + Δt f(x, t)
corrector:  x' = x + Δt/2 [f(x, t) + f(x*, t + Δt)]
```

where x is the position (λ, φ) and f is the right-hand side of the equations above. Heun and the midpoint method are both second order. Heun is used because it is the scheme of HYSPLIT, the reference trajectory model, so results are comparable. And with Δt = 1 h it reads the wind only at whole hours, the times of the data, while the midpoint method would need winds at half hours that only exist by interpolation.

The wind at a particle is interpolated bilinearly in space between the four surrounding grid points and linearly in time between the two surrounding hours.

### Dispersion

A 1 degree wind field misses the eddies that spread a plume sideways. They are represented as horizontal diffusion with a constant diffusivity K, implemented as a random walk: after each Heun step, every particle receives independent random displacements in x and y, each drawn from a normal distribution with standard deviation

```
σ = sqrt(2 K Δt)
```

so the spread of a puff grows as sqrt(2 K t), the solution of the diffusion equation. The default is **K = 2 x 10⁴ m²/s**. Richardson's law, in which diffusivity grows as the 4/3 power of the eddy size (K ≈ 0.1 ε^(1/3) L^(4/3)), gives about that value for eddies the size of the grid (L ≈ 100 km) with a free-troposphere dissipation rate ε ≈ 10⁻⁴ m²/s³. It is an order-of-magnitude choice: a puff spreads to about 60 km after 24 h and 100 km after 72 h, comparable to the zone size and the grid spacing. It should be tuned against observations in phase 2.

All random numbers come from one generator with a fixed seed, so identical inputs always give identical forecasts. With K = 0 the model is fully deterministic, which is how the integrator itself is tested.

### Stopping rules

- **Leaving the domain.** If the predictor, the corrected or the dispersed position falls outside the wind grid, the particle stops at its last position inside. On 23 October 2026, 15,161 of 590,400 particles (2.6%) left the domain.
- **Missing wind.** In a degraded run some grid points have no wind (NaN). A particle that needs one of them stops and is counted. It is never moved with an invented wind.

Otherwise a particle runs to the end of the 72 h horizon.

### Memory and what is kept

With 410 clusters, 72 hourly releases and 20 particles per release, a run moves 590,400 particles. Storing every position (72 steps) would take hundreds of MB, so it is never done. Only the current position of each particle is kept, and observers gather what later steps need while the particles move: the smoke inside each zone per hour and per cluster (risk step), and a 0.1 degree grid of smoke summed over the forecast (maps).

Full paths are kept only for the **display tracers**: one undispersed particle per release (29,520 on 23 October 2026), whose smooth paths show the axis of each puff on the map, where a random-walk path would zigzag.

On 23 October 2026 the whole model (clustering, regions, 590,400 dispersed particles, tracers, risk) took about 24 s on a laptop, with a peak memory of 180 MB.

### Checking the integrator

The automated tests check the model against known answers:

- **Uniform wind.** Pure northward and pure eastward motion match the exact solution to 1e-9 degrees. A diagonal wind keeps a constant compass heading, so the exact path is a rhumb line. The model matches it to about 0.25 m after 72 h and 1,000 km, and that small residual shrinks 4 times when Δt is halved, as it should.
- **Convergence.** With dispersion off, on a rotating wind field that changes in time, halving Δt divides the error by 4.00, the signature of a second-order method. With Δt = 1 h the error after 72 h and more than one full turn is about 3.3 km, far below the uncertainty of the winds themselves.
- **Dispersion.** In still air, the spread of 4,000 particles after 24 h and 72 h matches sqrt(2 K t) within 5%.
- **Synthetic plumes.** A fire due south of a zone under a steady northward wind reaches it when expected, zones away from the plume stay clean, and attribution adds up to 100%.

### Output for the web

`forecast.json` gets at most 300 display trajectories, so the web stays light. Showing only the strongest clusters would hide what matters most to a reader, a small fire whose smoke heads for their city, so they are chosen in two passes:

1. **Near-zone lines first:** tracers passing within 50 km of a zone's edge. Zones take turns, so a zone reached only by a small fire still gets its lines, and within each zone the clusters that bring it the most smoke come first. This pass takes at most 60% of the cap.
2. **Regional context:** the rest of the cap is a random sample of the other tracers, weighted by their FRP.

Coordinates are rounded to 0.01 degrees (about 1.1 km). On 23 October 2026 this gave 300 trajectories, 180 of them near zones, for 265 KB, plus 53 KB for the 410 clusters, before compression.

Lines show plume axes only. A city can receive smoke from the dispersed edge of a plume whose axis passes 100 km away; that smoke counts in its risk and its source breakdown, but no line is drawn to it. On 23 October 2026 most of Singapore's smoke came from Jambi through dispersion, while most lines near Singapore came from a West Sumatra cluster whose axis crossed the city. The source breakdown, not the lines, is the reference.

## Risk index

At each forecast hour, the **smoke index** of a zone is the summed weight of the particles within 50 km of the zone centre (great-circle distance). It measures the FRP-weighted amount of smoke inside the zone, in emission units.

Rule of thumb: a cluster of strength S whose plume covers a zone for τ hours adds about

```
(S / 48) * τ * f
```

where f is the fraction of its particles that fall inside the zone. Dispersion lowers f with distance, so distant fires count less, and continuous emission keeps the index up for as long as the plume stays over the zone.

The index is mapped to four levels of our own. They are **not** Malaysian API categories, which apply to measured PM2.5 only.

| Level | Smoke index from |
|---|---|
| low | 0 |
| moderate | 10 |
| high | 50 |
| very high | 200 |

**These thresholds are provisional and uncalibrated.** They were set by judgement on the October 2026 data, so that a zone downwind of a large fire complex reads high and a brush with the plume of a few small fires reads low. Nothing yet ties them to measured PM2.5. They will be calibrated in phase 2 against the APIMS stations and CAMS; until then the levels should be read as relative.

On 23 October 2026 (t0 16:00 UTC), Kuching reached high (peak index 103, from West Kalimantan), Singapore and Johor Bahru reached moderate (29 and 21, mostly from Jambi), and the other zones stayed low.

## Time of arrival (ETA)

The **ETA** of a zone is the first forecast hour at which its smoke index reaches "moderate". It is empty when that never happens within 72 h.

Smoke does not arrive all at once: dispersion spreads the arrival times of the particles, so the index climbs over several hours as more of them enter the zone. The **range** shown with the ETA is the time that climb takes around the moderate level, from the first hour the index reaches half the moderate threshold to the first hour it reaches twice it. A sharp front gives a narrow range, a slow build-up or a zone on the edge of a plume a wide one. If the index never reaches twice the threshold the range stays open ("possibly after 72 h"). Because the thresholds are uncalibrated, the same range also shows how far the ETA would move if the true threshold were half or double. On 23 October 2026: Singapore 25 h (20 to 29 h), Johor Bahru 28 h (25 to 33 h), Kuching 23 h (21 to 26 h).

Two alternatives were rejected. Percentiles of individual particle arrival times mostly reflect the release schedule, because emission is continuous and particles keep arriving until the end of the forecast. An ensemble with one particle per release in each member treats each hourly release as a single undiluted point, which inflates the index at the edge of a plume and gave far too early ETAs in tests.

The range covers dispersion and threshold sensitivity within one wind forecast. It does not include wind forecast error, which can be much larger (see [Wind model uncertainty](#wind-model-uncertainty)), so the true uncertainty is wider.

## Source attribution

For each zone, the smoke inside it is summed over the whole forecast and split by the source region of the clusters it came from. Shares are rounded to 0.1% with the largest-remainder method, so they always add up to exactly 100%.

## CAMS PM2.5 baseline

## Verification

## Limitations

### Physical assumptions of the trajectory model

- **One level, no vertical motion.** All smoke moves with the 850 hPa wind. Real smoke spreads over a range of heights, and the wind changes with height (wind shear), so smoke near the ground or higher up can travel in another direction. Vertical motion is ignored: smoke rising in convection or trapped near the ground when the boundary layer collapses at night. Sea breezes and other local circulations are not captured by an 850 hPa wind on a 1 degree grid.
- **No deposition or washout.** Smoke is never removed. In reality rain washes particles out of the air and they also settle over time, so the model overestimates how much smoke reaches distant places, especially in wet weather. Rain washout is planned for phase 2.
- **Simple dispersion.** Horizontal spread uses one constant diffusivity everywhere and at all times. Real mixing varies with weather, time of day and height; the value is an order-of-magnitude estimate, not a fit to observations.
- **Persistent fires.** Fires seen in the last 48 h are assumed to keep burning at the same strength for the next 72 h. New fires are not anticipated, and fires put out by rain keep emitting in the model.
- **No smoke already in the air at t0.** Only smoke emitted from t0 on is tracked. Smoke emitted earlier and already drifting is missing, so the first hours of the forecast tend to underestimate. CAMS, which carries the smoke already present, is the baseline for that.
- **FRP as a proxy.** Summed FRP is a relative indicator of fire strength. Smouldering peat fires release a lot of smoke for little radiative power, so their contribution is probably underestimated compared with flaming surface fires.
- **Not every hotspot is a fire.** Gas flares and other industrial heat sources are also detected. On 23 October 2026 a persistent cluster at 10.4 N 108.2 E lay offshore of southern Vietnam, on an oil field, and was almost certainly a gas flare. Offshore clusters are labelled "Other" but still release smoke in the model.
- **Resolution.** A 1 degree wind grid and 28 km clusters cannot resolve neighbourhoods. Phase 1 gives one forecast per zone.

### Limitations of the risk index

- **Uncalibrated levels.** The smoke index is a relative measure of FRP-weighted smoke, not a concentration. The level thresholds are judgement calls until phase 2 verification.
- **Zone as a circle.** Smoke counts when it is within 50 km of the zone centre, at 850 hPa. Smoke passing overhead without mixing down to the ground is counted the same as smoke at street level.
- **Attribution by outline.** Regions follow simplified Natural Earth outlines (North Kalimantan inside East Kalimantan), and clusters near a border are assigned by their centroid only.
- **ETA range.** It reflects dispersion and threshold sensitivity, not wind forecast error.

### Wind model uncertainty

Near the equator the 850 hPa winds are often weak and variable, and global models disagree. On 23 October 2026 at 00:00 UTC, at 0.5 N 101.5 E (central Riau), three models agreed on the direction (wind from about 140 degrees) but not the speed: ECMWF IFS 6.8 m/s, ICON 5.3 m/s and GFS 2.3 m/s. A factor of three in speed is a factor of three in travel time. Over the 290 km between central Riau and Klang Valley, for example, that is roughly 12 h against 35 h. Over Kuala Lumpur the same morning (01:00 to 05:00 UTC), ECMWF had less than 1 m/s while ICON had about 3 m/s.

The arrival times Hazier shows come from a single model (ECMWF IFS) and should be read as indicative. A small multi-model or ensemble forecast is planned for phase 2 to show this uncertainty.
