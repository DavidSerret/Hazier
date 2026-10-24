import { MapLibreOverlay } from "@deck.gl/maplibre";
import { Map, setWorkerUrl, type IControl, type LngLatBoundsLike } from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import { DEFAULT_ZONE_ID } from "./data/derive";
import { forecastUrl, loadForecast, ageHours, ForecastError } from "./data/load";
import { advance, hasNow, nowHour } from "./data/timeline";
import type { Forecast } from "./data/types";
import { buildLayers, Pm25Images, ZONE_MARKER_RADIUS, ZONE_NAME_GAP } from "./map/layers";
import { visibleLabels, type LabelBox } from "./map/labels";
import { showBanners, showError, showLoaded, showLoading } from "./ui/banners";
import { initCard, renderCard } from "./ui/card";
import { $ } from "./ui/dom";
import { initInfo, showInfo } from "./ui/info";
import { setPlaying, setTimelineHour, showLegend, showTimeline } from "./ui/timeline";
import { renderZoneList } from "./ui/zones";

// MapLibre 6 looks for its worker next to the main module. Vite bundles that module into the
// app chunk, so the sibling file would be missing in production. Point to the emitted asset.
setWorkerUrl(workerUrl);

// Free, keyless dark basemap (CARTO Dark Matter). Credited in the info panel.
const DARK_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

// The whole domain: 95 to 120 E, 8 S to 12 N.
const DOMAIN: LngLatBoundsLike = [
  [95, -8],
  [120, 12],
];

const map = new Map({
  container: "map",
  style: DARK_STYLE,
  bounds: DOMAIN,
  fitBoundsOptions: { padding: 8 },
  attributionControl: false, // credited in the info panel
  dragRotate: false,
  pitchWithRotate: false,
  touchPitch: false,
  // Arrow keys belong to the timeline, not to panning the map.
  keyboard: false,
});

const overlay = new MapLibreOverlay({
  layers: [],
  // A finger is wider than a 20 px marker: accept taps this close to a zone marker.
  pickingRadius: 14,
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
let age = 0; // hours since the forecast was made (0 for the sample)
let hour = 0; // forecast hour on the timeline; fractional while playing
let playing = false;
let lastFrame = 0;
let lastStep = -1;
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
    const p = map.project([z.lon, z.lat]);
    return {
      id: z.id,
      x: p.x,
      y: p.y + ZONE_MARKER_RADIUS + ZONE_NAME_GAP,
      text: z.name,
      priority: z.id === selectedZoneId ? 10 : 0,
    };
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

/** Update the text parts of the UI. The card and list only change when the whole hour does. */
function syncUi(force = false): void {
  if (!forecast) return;
  const step = Math.round(hour);
  if (!force && step === lastStep) return;
  lastStep = step;
  setTimelineHour(forecast, step, age);
  const zone = forecast.zones.find((z) => z.id === selectedZoneId);
  if (zone) {
    renderCard({
      forecast,
      zone,
      hour: step,
      ageHours: age,
      verified: forecast.pm25_baseline?.categories_verified ?? true,
    });
  }
  renderZoneList(forecast, selectedZoneId, step, selectZone);
}

function setHour(next: number): void {
  hour = next;
  syncUi();
  schedule();
}

function selectZone(id: string): void {
  selectedZoneId = id;
  updateLabels();
  syncUi(true);
  schedule();
}

// Playback. Time moves smoothly (the trails glide between hours); the colour layer, the zone
// markers and the card step once per forecast hour.
function playFrame(now: number): void {
  if (!playing || !forecast) return;
  const dt = Math.min(0.1, (now - lastFrame) / 1000); // a hidden tab must not jump ahead
  lastFrame = now;
  hour = advance(hour, dt, forecast.horizon_hours);
  syncUi();
  draw();
  requestAnimationFrame(playFrame);
}

function setPlayState(next: boolean): void {
  if (next === playing) return;
  playing = next;
  setPlaying(playing);
  if (playing) {
    lastFrame = performance.now();
    requestAnimationFrame(playFrame);
  } else if (forecast) {
    hour = Math.round(hour); // settle on a whole hour, like the slider
    syncUi();
    schedule();
  }
}

/** The user took over the timeline: stop playing. */
const pause = () => setPlayState(false);

function nudge(delta: number): void {
  if (!forecast) return;
  pause();
  setHour(Math.min(forecast.horizon_hours, Math.max(0, Math.round(hour) + delta)));
}

// Keyboard: arrows step the timeline an hour, space plays and pauses. The slider handles its
// own arrow keys natively; buttons handle their own space.
document.addEventListener("keydown", (e) => {
  if (!forecast || e.altKey || e.ctrlKey || e.metaKey) return;
  const target = e.target as HTMLElement;
  if (target.id === "hour" && e.key.startsWith("Arrow")) return;
  if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
    e.preventDefault();
    nudge(-1);
  } else if (e.key === "ArrowRight" || e.key === "ArrowUp") {
    e.preventDefault();
    nudge(1);
  } else if (e.key === " " && !["BUTTON", "A", "INPUT"].includes(target.tagName)) {
    e.preventDefault();
    setPlayState(!playing);
  }
});

/** Fit the domain into the part of the screen the panels leave free. */
function fitDomain(): void {
  const desktop = matchMedia("(min-width: 768px)").matches;
  const panel = $("panel").getBoundingClientRect();
  // On a phone only the card is reserved at the top: warnings may cover the map, they are brief.
  const card = $("card").getBoundingClientRect();
  const timeline = $("timeline").getBoundingClientRect();
  map.fitBounds(DOMAIN, {
    padding: {
      top: desktop ? 16 : card.bottom + 8,
      bottom: timeline.height + 8,
      left: desktop ? panel.width + 8 : 8,
      right: 8,
    },
    duration: 0,
  });
}

// The left panel and the info sheet end above the timeline, whatever its height.
new ResizeObserver(([entry]) => {
  document.documentElement.style.setProperty("--timeline-h", `${entry.target.getBoundingClientRect().height}px`);
}).observe($("timeline"));

async function start(): Promise<void> {
  showLoading();
  setPlayState(false);
  const url = forecastUrl(location.search);
  try {
    forecast = await loadForecast(url);
  } catch (error) {
    forecast = null;
    const message =
      error instanceof ForecastError ? error.message : "Something went wrong loading the forecast.";
    showError(message, () => void start());
    return;
  }
  const isSample = url.includes("sample");
  const horizon = forecast.horizon_hours;
  // The sample is old by definition; treat it as made now so its card reads like a live one.
  age = isSample ? 0 : Math.max(0, ageHours(forecast));
  images = new Pm25Images(forecast);
  // Open on the current time. A forecast older than its horizon has no "now": open on its start.
  hour = hasNow(age, horizon) ? Math.round(nowHour(age, horizon)) : 0;
  lastStep = -1;
  selectedZoneId = (
    forecast.zones.find((z) => z.id === DEFAULT_ZONE_ID) ?? forecast.zones[0]
  ).id;

  showLoaded();
  showBanners(forecast, isSample);
  showInfo(forecast);
  showLegend();
  showTimeline(forecast, age, {
    onInteract: pause,
    onScrub: (h) => setHour(h),
    onTogglePlay: () => setPlayState(!playing),
  });
  syncUi(true);
  fitDomain();
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

initCard();
initInfo();
showInfo(null);
void start();
