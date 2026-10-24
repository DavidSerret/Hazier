# Hazier

Haze forecaster for Malaysia and the southern ASEAN region. It predicts whether, when and from where transboundary smoke (mainly from fires in Sumatra and Borneo) will reach each zone, with a clear, public-friendly interface.

Hazier does not replace the CAMS (Copernicus) PM2.5 forecast: the air quality level it shows is the CAMS PM2.5 and its Malaysian API category. What Hazier adds is source attribution (which provinces the smoke comes from), a readable arrival time ("smoke reaches Klang Valley in about 30 h") and the smoke paths.

Status: the pipeline works end to end and writes `forecast.json`; the web app is next.

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

Full pipeline (needs a free FIRMS MAP_KEY from https://firms.modaps.eosdis.nasa.gov/api/map_key/):

```
cd pipeline
copy .env.example .env        # then put your key in .env
.venv\Scripts\python.exe -m hazier
```

It writes `pipeline/output/site/forecast.json` (git-ignored), appends one line to `pipeline/output/history/runs.csv` (git-ignored; on GitHub the history lives on a separate `data` branch) and prints a one-screen summary. Without cache a run takes about 5 minutes and 1,100 Open-Meteo calls; winds and the CAMS grid are cached per model run, so repeated local runs are fast. Use `--no-history` for test runs you do not want recorded. Exit codes: 0 forecast written, 1 no usable forecast (FIRMS or winds unavailable, the previous file is kept), 2 forecast failed validation (the previous file is kept).

Debug commands for single steps (from `pipeline/`):

```
.venv\Scripts\python.exe -m hazier.sources.firms          # FIRMS hotspots per satellite
.venv\Scripts\python.exe -m hazier.sources.winds          # 850 hPa winds, cached per model run
.venv\Scripts\python.exe -m hazier.model.smoke            # full model: zone table (CAMS level, smoke ETA, origin), saves output/trajectories.png
.venv\Scripts\python.exe -m hazier.sources.cams           # CAMS PM2.5 for the zones and the map grid (--no-grid for zones only)
```

Web (from `web/`):

```
npm install
npm run dev        # http://localhost:5173, loads public/forecast.sample.json
npm test           # unit tests: loading, validation, staleness, map raster
npm run build      # typecheck and production build into dist/
```

The dev server shows the sample forecast (made by the real model on mock data, marked "Sample data" on screen). To try the production build with a real forecast, run the pipeline, then copy `pipeline/output/site/forecast.json` into `web/dist/` (the workflow does the same) and run `npm run preview`. Add `?forecast=<url>` to any address to load another file. A forecast older than 9 hours is flagged as stale on screen.

## Limitations

Trajectories cannot resolve neighbourhoods. Phase 1 gives one forecast per zone (circles of about 50 km around each city), not street-level detail.

## Credits

- Active fires: NASA FIRMS
- Air quality baseline: CAMS (Copernicus Atmosphere Monitoring Service) via Open-Meteo
- Weather: Open-Meteo
- Basemap: CARTO, OpenStreetMap contributors
