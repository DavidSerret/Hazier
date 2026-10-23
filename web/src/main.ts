import { Map, setWorkerUrl } from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";

// MapLibre 6 looks for its worker next to the main module. Vite bundles that module into the
// app chunk, so the sibling file would be missing in production. Point to the emitted asset.
setWorkerUrl(workerUrl);

// Free, keyless dark basemap (CARTO Dark Matter). Attribution is shown by the map control.
const DARK_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

// Centre of the domain (95 to 120 E, 8 S to 12 N).
const CENTER: [number, number] = [107.5, 2];

new Map({
  container: "map",
  style: DARK_STYLE,
  center: CENTER,
  zoom: 4.6,
  attributionControl: { compact: true },
});
