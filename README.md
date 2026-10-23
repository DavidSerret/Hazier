# Hazier

Haze forecaster for Malaysia and the southern ASEAN region. It predicts whether, when and from where transboundary smoke (mainly from fires in Sumatra and Borneo) will reach each zone, with a clear, public-friendly interface.

Hazier does not replace the CAMS (Copernicus) PM2.5 forecast: the air quality level it shows is the CAMS PM2.5 and its Malaysian API category. What Hazier adds is source attribution (which provinces the smoke comes from), a readable arrival time ("smoke reaches Klang Valley in about 30 h") and the smoke paths.

Status: early scaffold, no forecasting logic yet.

## How it works

1. A Python pipeline downloads active fire hotspots (NASA FIRMS, VIIRS) and 850 hPa winds (Open-Meteo).
2. It carries the smoke of each fire with the winds, from its first detection to 72 h ahead, and works out when it reaches each zone and where it comes from. It also fetches CAMS PM2.5 for the zones and the map.
3. It writes a single `forecast.json`. The web app reads only this file.
4. GitHub Actions runs the pipeline every 6 hours and publishes the site to GitHub Pages.

Times in the data are UTC. The web app displays Malaysia time (MYT).

## Stack

- Pipeline: Python 3.11+, pandas, numpy (`pipeline/`)
- Web: Vite, TypeScript, MapLibre GL JS, deck.gl, Tailwind CSS (`web/`)
- Automation: GitHub Actions and GitHub Pages

## Run locally

Pipeline tests:

```
cd pipeline
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Real FIRMS fetch (needs a free MAP_KEY from https://firms.modaps.eosdis.nasa.gov/api/map_key/):

```
cd pipeline
copy .env.example .env        # then put your key in .env
.venv\Scripts\python.exe -m hazier.sources.firms
```

Other debug commands (from `pipeline/`):

```
.venv\Scripts\python.exe -m hazier.sources.winds          # 850 hPa winds, cached per model run
.venv\Scripts\python.exe -m hazier.model.risk             # full model: zone table (CAMS level, smoke ETA, origin), saves output/trajectories.png
.venv\Scripts\python.exe -m hazier.sources.cams           # CAMS PM2.5 for the zones and the map grid (--no-grid for zones only)
```

Web (uses `public/forecast.sample.json` until the pipeline produces real data):

```
cd web
npm install
npm run dev
```

## Limitations

Trajectories cannot resolve neighbourhoods. Phase 1 gives one forecast per zone (circles of about 50 km around each city), not street-level detail.

## Credits

- Active fires: NASA FIRMS
- Air quality baseline: CAMS (Copernicus Atmosphere Monitoring Service) via Open-Meteo
- Weather: Open-Meteo
- Basemap: CARTO, OpenStreetMap contributors
