import { MapLibreOverlay } from "@deck.gl/maplibre";
import { Map, setWorkerUrl, type IControl } from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import { DEFAULT_ZONE_ID, zoneSummary } from "./data/derive";
import { forecastUrl, loadForecast, ForecastError } from "./data/load";
import type { Forecast } from "./data/types";
import { buildLayers, Pm25Images } from "./map/layers";
import { visibleLabels, type LabelBox } from "./map/labels";
import {
  setHourLabel,
  setZoneReadout,
  showBanners,
  showControl,
  showError,
  showFooter,
  showLegend,
  showLoading,
} from "./ui";

// MapLibre 6 looks for its worker next to the main module. Vite bundles that module into the
// app chunk, so the sibling file would be missing in production. Point to the emitted asset.
setWorkerUrl(workerUrl);

// Free, keyless dark basemap (CARTO Dark Matter). Credited in the footer.
const DARK_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

// The whole domain (95 to 120 E, 8 S to 12 N), with room for the controls at the bottom.
const map = new Map({
  container: "map",
  style: DARK_STYLE,
  bounds: [
    [95, -8],
    [120, 12],
  ],
  fitBoundsOptions: { padding: { top: 56, bottom: 190, left: 8, right: 8 } },
  attributionControl: false, // credited in our own footer
  dragRotate: false,
  pitchWithRotate: false,
  touchPitch: false,
});

const overlay = new MapLibreOverlay({
  layers: [],
  onHover: ({ object }) => {
    map.getCanvas().style.cursor = object ? "pointer" : "";
  },
});
map.addControl(overlay as unknown as IControl);
map.on("moveend", () => {
  updateLabels();
  schedule();
});

let forecast: Forecast | null = null;
let images: Pm25Images | null = null;
let hour = 0;
let selectedZoneId: string | null = null;
let queued = false;
let visibleNames: Set<string> | null = null;
const query = new URLSearchParams(location.search);
const hiddenLayers = query.has("perf") ? (query.get("hide") ?? "").split(",").filter(Boolean) : [];

function draw(): void {
  if (!forecast || !images) return;
  const started = performance.now();
  let layers = buildLayers({ forecast, hour, selectedZoneId }, images, selectZone, visibleNames);
  // Benchmark aid (?perf&hide=pm25,fires): switch layers off by id prefix to see what costs most.
  if (hiddenLayers.length) layers = layers.filter((l) => !hiddenLayers.some((h) => l.id.startsWith(h)));
  overlay.setProps({ layers });
  perf?.record(performance.now() - started);
}

/** Hide zone names that would overlap on screen; the selected zone's name always wins. */
function updateLabels(): void {
  if (!forecast) return;
  const boxes: LabelBox[] = forecast.zones.map((z) => {
    // The same anchor as the layer: the southern edge of the zone circle.
    const p = map.project([z.lon, z.lat - z.radius_km / 110.57]);
    return { id: z.id, x: p.x, y: p.y + 4, text: z.name, priority: z.id === selectedZoneId ? 10 : 0 };
  });
  visibleNames = visibleLabels(boxes);
}

/** Redraw at most once per frame, however fast the control fires. */
function schedule(): void {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    draw();
  });
}

function updateReadout(): void {
  if (!forecast) return;
  setHourLabel(forecast, hour);
  const zone = forecast.zones.find((z) => z.id === selectedZoneId);
  setZoneReadout(zone ? zoneSummary(forecast, zone, hour) : "Tap a zone to select it.");
}

function setHour(next: number): void {
  hour = next;
  updateReadout();
  schedule();
}

function selectZone(id: string): void {
  selectedZoneId = id;
  updateLabels();
  updateReadout();
  schedule();
}

async function start(): Promise<void> {
  showLoading();
  const url = forecastUrl(location.search);
  try {
    forecast = await loadForecast(url);
  } catch (error) {
    const message =
      error instanceof ForecastError ? error.message : "Something went wrong loading the forecast.";
    showError(message, () => void start());
    return;
  }
  const isSample = url.includes("sample");
  images = new Pm25Images(forecast);
  hour = 0;
  selectedZoneId = (
    forecast.zones.find((z) => z.id === DEFAULT_ZONE_ID) ?? forecast.zones[0]
  ).id;
  showBanners(forecast, isSample);
  showFooter(forecast);
  showLegend(forecast, forecast.pm25_baseline?.categories_verified ?? true);
  showControl(forecast, setHour);
  updateReadout();
  updateLabels();
  draw();
}

// ?perf adds a benchmark hook, in its own chunk so the normal page does not carry it.
type Perf = { record: (ms: number) => void };
let perf: Perf | null = null;
if (query.has("perf")) {
  void import("./perf").then((m) => {
    perf = m.installPerf({
      setHour: (h) => setHour(h),
      draw,
      selectedZone: () => selectedZoneId,
      zonePixel: (id) => {
        const zone = forecast?.zones.find((z) => z.id === id);
        if (!zone) return null;
        const p = map.project([zone.lon, zone.lat]);
        return [p.x, p.y];
      },
    });
  });
}

showFooter(null);
void start();
