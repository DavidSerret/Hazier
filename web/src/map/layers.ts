// The deck.gl layers, rebuilt from the forecast, the forecast hour, the zoom and the selection.
//
// From the bottom: CAMS PM2.5 category bands, Hazier's smoke haze (grey), a few smoke flow
// lines, fire clusters, zone markers with their hourly PM2.5 number, a ring where Hazier has
// smoke over a zone, and the zone names. Zones and fires are pickable.

import type { Layer } from "@deck.gl/core";
import { TripsLayer } from "@deck.gl/geo-layers";
import { BitmapLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { smokeAloft, textOn, zoneColourAt, zonePointAt } from "../data/derive";
import type { Forecast, SourceCluster, Trajectory, ZoneForecast } from "../data/types";
import { bandsFrom, rasterise, type Band } from "./pm25";
import { decodeSmokeGrid, rasteriseSmoke } from "./smokegrid";

export const ZONE_NAME_SIZE = 12;
export const ZONE_NAME_SIZE_SELECTED = 14;
// Zone markers have a fixed size on screen, whatever the zoom: a 50 km circle would cover the
// city at high zoom. They are big enough to hold a three-digit number.
export const ZONE_MARKER_RADIUS = 14;
export const ZONE_MARKER_RADIUS_SELECTED = 16;
const SMOKE_RING_RADIUS = 21;
export const ZONE_NAME_OFFSET = SMOKE_RING_RADIUS + 3; // names hang below the smoke ring
const SMOKE_RGB: [number, number, number] = [214, 216, 224];

export type MapState = {
  forecast: Forecast;
  hour: number; // may be fractional while playing; data layers use the nearest whole hour
  selectedZoneId: string | null;
  zoom: number;
};

export type Picks = {
  onSelectZone: (zoneId: string) => void;
  onPickFire: (cluster: SourceCluster, x: number, y: number) => void;
};

// The flow lines show only their recent part, fading behind the moving head.
const TRAIL_HOURS = 6;

function toCanvas(width: number, height: number, data: Uint8ClampedArray<ArrayBuffer>) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  canvas.getContext("2d")?.putImageData(new ImageData(data, width, height), 0, 0);
  return canvas;
}

/** Renders and remembers the map images: CAMS bands and smoke haze, one per forecast hour. */
export class MapImages {
  private pm25 = new Map<number, HTMLCanvasElement>();
  private smoke = new Map<number, HTMLCanvasElement>();
  private bands: Band[];
  private smokeBytes: Uint8Array | null;
  pm25Bounds: [number, number, number, number] | null = null;
  smokeBounds: [number, number, number, number] | null = null;

  constructor(private forecast: Forecast) {
    this.bands = bandsFrom(forecast.pm25_baseline?.category_upper_bounds);
    this.smokeBytes = forecast.smoke_grid ? decodeSmokeGrid(forecast.smoke_grid) : null;
  }

  pm25At(hour: number): HTMLCanvasElement | null {
    const grid = this.forecast.pm25_grid;
    if (!grid) return null;
    const hit = this.pm25.get(hour);
    if (hit) return hit;
    const raster = rasterise(grid, hour, this.bands);
    this.pm25Bounds = raster.bounds;
    const canvas = toCanvas(raster.width, raster.height, raster.data);
    this.pm25.set(hour, canvas);
    if (this.pm25.size > 80) this.pm25.delete(this.pm25.keys().next().value as number);
    return canvas;
  }

  /** The haze of one whole forecast hour (frame index = hour). */
  smokeAt(hour: number): HTMLCanvasElement | null {
    const grid = this.forecast.smoke_grid;
    if (!grid || !this.smokeBytes || hour < 0 || hour >= grid.hours.length) return null;
    const hit = this.smoke.get(hour);
    if (hit) return hit;
    const threshold = this.forecast.smoke_model.arrival_threshold;
    const raster = rasteriseSmoke(grid, this.smokeBytes, hour, threshold);
    this.smokeBounds = raster.bounds;
    const canvas = toCanvas(raster.width, raster.height, raster.data);
    this.smoke.set(hour, canvas);
    if (this.smoke.size > 80) this.smoke.delete(this.smoke.keys().next().value as number);
    return canvas;
  }
}

/** Smoke left on a flow line at an hour, relative to the heaviest line (0 to 1). */
export function lineStrength(t: Trajectory, hour: number, lifetime: number, maxWeight: number): number {
  const age = Math.max(0, hour - t.release_hour);
  return maxWeight > 0 ? (t.weight * Math.exp(-age / lifetime)) / maxWeight : 0;
}

/** Fires shrink and dim as the map zooms in, so they never hide the city underneath. */
export function fireScale(zoom: number): { size: number; alpha: number } {
  const t = Math.min(1, Math.max(0, (zoom - 5) / 4)); // 0 at zoom 5 or less, 1 at zoom 9
  return { size: 1 - 0.55 * t, alpha: 1 - 0.6 * t };
}

function clusterLayers(clusters: SourceCluster[], zoom: number, picks: Picks): Layer[] {
  const top = Math.max(1, ...clusters.map((c) => c.frp_mw));
  const { size, alpha } = fireScale(zoom);
  // Core radius in pixels grows with the square root of FRP: the area follows the intensity.
  const core = (c: SourceCluster) => (1.5 + 6 * Math.sqrt(c.frp_mw / top)) * size;
  const base = {
    data: clusters,
    getPosition: (c: SourceCluster) => [c.lon, c.lat] as [number, number],
    radiusUnits: "pixels" as const,
    stroked: false,
    updateTriggers: { getRadius: [size], getFillColor: [alpha] },
  };
  return [
    new ScatterplotLayer<SourceCluster>({
      ...base,
      id: "fires-glow",
      getRadius: (c) => core(c) * 2.4,
      getFillColor: [255, 120, 30, Math.round(45 * alpha)],
    }),
    new ScatterplotLayer<SourceCluster>({
      ...base,
      id: "fires-core",
      getRadius: core,
      getFillColor: [255, 200, 110, Math.round(235 * alpha)],
      pickable: true,
      onClick: ({ object, x, y }) => {
        if (object) picks.onPickFire(object, x, y);
        return true;
      },
    }),
  ];
}

export function buildLayers(
  state: MapState,
  images: MapImages,
  picks: Picks,
  visibleNames: Set<string> | null = null,
): Layer[] {
  const { forecast, hour, selectedZoneId, zoom } = state;
  const step = Math.round(hour);
  const layers: Layer[] = [];
  const noDepth = { depthCompare: "always" as const };

  const pm25 = images.pm25At(step);
  if (pm25 && images.pm25Bounds) {
    layers.push(
      new BitmapLayer({ id: "pm25", image: pm25, bounds: images.pm25Bounds, parameters: noDepth }),
    );
  }

  // Smoke haze: the two whole hours around the current time, cross-faded, so it moves smoothly
  // while playing without rasterising every frame.
  const h0 = Math.floor(hour);
  const f = hour - h0;
  for (const [h, opacity, id] of [
    [h0, 1 - f, "smoke-haze-a"],
    [h0 + 1, f, "smoke-haze-b"],
  ] as const) {
    if (opacity <= 0.01) continue;
    const image = images.smokeAt(h);
    if (image && images.smokeBounds) {
      layers.push(
        new BitmapLayer({ id, image, bounds: images.smokeBounds, opacity, parameters: noDepth }),
      );
    }
  }

  const lifetime = forecast.smoke_model.removal_lifetime_hours;
  const maxWeight = Math.max(0, ...forecast.trajectories.map((t) => t.weight));
  layers.push(
    new TripsLayer<Trajectory>({
      id: "smoke-lines",
      data: forecast.trajectories,
      getPath: (t) => t.path,
      getTimestamps: (t) => t.hours,
      // Opacity follows the smoke the line carries now: weight after removal.
      getColor: (t) => [...SMOKE_RGB, Math.round(30 + 170 * lineStrength(t, hour, lifetime, maxWeight))],
      widthUnits: "pixels",
      getWidth: 1.6,
      capRounded: true,
      jointRounded: true,
      trailLength: TRAIL_HOURS,
      currentTime: hour,
      fadeTrail: true,
      updateTriggers: { getColor: [step] },
      parameters: noDepth,
    }),
  );

  layers.push(...clusterLayers(forecast.clusters, zoom, picks));

  const radius = (z: ZoneForecast) =>
    z.id === selectedZoneId ? ZONE_MARKER_RADIUS_SELECTED : ZONE_MARKER_RADIUS;
  const aloft = forecast.zones.filter((z) => smokeAloft(forecast, z, step));
  layers.push(
    new ScatterplotLayer<ZoneForecast>({
      id: "zone-smoke-ring",
      data: aloft,
      getPosition: (z) => [z.lon, z.lat],
      getRadius: SMOKE_RING_RADIUS,
      radiusUnits: "pixels",
      filled: false,
      stroked: true,
      getLineColor: [...SMOKE_RGB, 230],
      getLineWidth: 3,
      lineWidthUnits: "pixels",
      parameters: noDepth,
    }),
    new ScatterplotLayer<ZoneForecast>({
      id: "zones",
      data: forecast.zones,
      getPosition: (z) => [z.lon, z.lat],
      getRadius: radius,
      radiusUnits: "pixels",
      stroked: true,
      getFillColor: (z) => [...zoneColourAt(forecast, z, step), 245],
      getLineColor: (z) => (z.id === selectedZoneId ? [255, 255, 255, 255] : [12, 14, 18, 230]),
      getLineWidth: (z) => (z.id === selectedZoneId ? 3 : 1.5),
      lineWidthUnits: "pixels",
      pickable: true,
      onClick: ({ object }) => {
        if (object) picks.onSelectZone(object.id);
        return true;
      },
      updateTriggers: {
        getFillColor: [step],
        getRadius: [selectedZoneId],
        getLineColor: [selectedZoneId],
        getLineWidth: [selectedZoneId],
      },
      parameters: noDepth,
    }),
    new TextLayer<ZoneForecast>({
      id: "zone-numbers",
      data: forecast.zones,
      getPosition: (z) => [z.lon, z.lat],
      getText: (z) => {
        const p = zonePointAt(forecast, z, step);
        return p ? String(Math.round(p.value_ugm3)) : "–";
      },
      getSize: 12,
      getColor: (z) => [...textOn(zoneColourAt(forecast, z, step)), 255],
      getTextAnchor: "middle",
      getAlignmentBaseline: "center",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
      fontWeight: 700,
      characterSet: "0123456789–",
      updateTriggers: { getText: [step], getColor: [step] },
      parameters: noDepth,
    }),
    new TextLayer<ZoneForecast>({
      id: "zone-names",
      data: visibleNames ? forecast.zones.filter((z) => visibleNames.has(z.id)) : forecast.zones,
      getPosition: (z) => [z.lon, z.lat],
      getText: (z) => z.name,
      getSize: (z) => (z.id === selectedZoneId ? ZONE_NAME_SIZE_SELECTED : ZONE_NAME_SIZE),
      getColor: [255, 255, 255, 255],
      getPixelOffset: [0, ZONE_NAME_OFFSET],
      getTextAnchor: "middle",
      getAlignmentBaseline: "top",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
      fontWeight: 600,
      fontSettings: { sdf: true },
      outlineWidth: 3,
      outlineColor: [10, 12, 16, 235],
      updateTriggers: { getSize: [selectedZoneId] },
      parameters: noDepth,
    }),
  );
  return layers;
}
