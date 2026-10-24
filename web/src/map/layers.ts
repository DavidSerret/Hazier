// The deck.gl layers, rebuilt from the forecast, the forecast hour and the selected zone.
//
// From the bottom: CAMS PM2.5 colour layer, faint smoke paths, animated smoke trails and
// particles, fire clusters with a glow, zones and their names. Only the zones are pickable.

import type { Layer } from "@deck.gl/core";
import { TripsLayer } from "@deck.gl/geo-layers";
import { BitmapLayer, PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { zoneColourAt } from "../data/derive";
import type { Forecast, SourceCluster, ZoneForecast } from "../data/types";
import { colourAt, rampStops, rasterise, type RampStop } from "./pm25";
import { headsAt } from "./smoke";

export const ZONE_NAME_SIZE = 12;
export const ZONE_NAME_SIZE_SELECTED = 14;

export type MapState = {
  forecast: Forecast;
  hour: number;
  selectedZoneId: string | null;
};

const KM_PER_DEG_LAT = 110.57;
const TRAIL_HOURS = 10;

/** Renders and remembers the PM2.5 canvases, one per forecast hour. */
export class Pm25Images {
  private cache = new Map<number, HTMLCanvasElement>();
  private stops: RampStop[];
  bounds: [number, number, number, number] | null = null;

  constructor(private forecast: Forecast) {
    this.stops = rampStops(forecast.pm25_baseline?.category_upper_bounds);
  }

  get(hour: number): HTMLCanvasElement | null {
    const grid = this.forecast.pm25_grid;
    if (!grid) return null;
    const hit = this.cache.get(hour);
    if (hit) return hit;
    const raster = rasterise(grid, hour, this.stops);
    this.bounds = raster.bounds;
    const canvas = document.createElement("canvas");
    canvas.width = raster.width;
    canvas.height = raster.height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.putImageData(new ImageData(raster.data, raster.width, raster.height), 0, 0);
    this.cache.set(hour, canvas);
    if (this.cache.size > 80) this.cache.delete(this.cache.keys().next().value as number);
    return canvas;
  }

  /** The legend gradient: colours for 0 to max µg/m³. */
  legendColours(max = 120, steps = 24): string[] {
    return Array.from({ length: steps }, (_, i) => {
      const [r, g, b] = colourAt((i / (steps - 1)) * max, this.stops);
      return `rgb(${r}, ${g}, ${b})`;
    });
  }
}

const maxFrp = (clusters: SourceCluster[]) => Math.max(1, ...clusters.map((c) => c.frp_mw));

function clusterLayers(clusters: SourceCluster[]): Layer[] {
  const top = maxFrp(clusters);
  // Core radius in pixels grows with the square root of FRP: the area follows the intensity.
  const core = (c: SourceCluster) => 2 + 9 * Math.sqrt(c.frp_mw / top);
  const base = {
    data: clusters,
    getPosition: (c: SourceCluster) => [c.lon, c.lat] as [number, number],
    radiusUnits: "pixels" as const,
    stroked: false,
  };
  return [
    new ScatterplotLayer<SourceCluster>({
      ...base,
      id: "fires-glow-outer",
      getRadius: (c) => core(c) * 3.2,
      getFillColor: [255, 90, 20, 28],
    }),
    new ScatterplotLayer<SourceCluster>({
      ...base,
      id: "fires-glow-inner",
      getRadius: (c) => core(c) * 1.8,
      getFillColor: [255, 140, 30, 70],
    }),
    new ScatterplotLayer<SourceCluster>({
      ...base,
      id: "fires-core",
      getRadius: core,
      getFillColor: [255, 214, 120, 255],
    }),
  ];
}

export function buildLayers(
  state: MapState,
  images: Pm25Images,
  onSelect: (zoneId: string) => void,
  visibleNames: Set<string> | null = null,
): Layer[] {
  const { forecast, hour, selectedZoneId } = state;
  const layers: Layer[] = [];

  const image = images.get(hour);
  if (image && images.bounds) {
    layers.push(
      new BitmapLayer({
        id: "pm25",
        image,
        bounds: images.bounds,
        opacity: 1,
        parameters: { depthCompare: "always" },
      }),
    );
  }

  const trajectories = forecast.trajectories;
  layers.push(
    new PathLayer({
      id: "smoke-paths",
      data: trajectories,
      getPath: (t) => t.path,
      getColor: [200, 196, 255, 34],
      widthUnits: "pixels",
      getWidth: 1,
      parameters: { depthCompare: "always" },
    }),
    new TripsLayer({
      id: "smoke-trails",
      data: trajectories,
      getPath: (t) => t.path,
      getTimestamps: (t) => t.hours,
      getColor: [232, 228, 255, 235],
      widthUnits: "pixels",
      getWidth: 2.5,
      capRounded: true,
      jointRounded: true,
      trailLength: TRAIL_HOURS,
      currentTime: hour,
      fadeTrail: true,
      parameters: { depthCompare: "always" },
    }),
    new ScatterplotLayer({
      id: "smoke-heads",
      data: headsAt(trajectories, hour),
      getPosition: (d) => d.position,
      getRadius: 2.2,
      radiusUnits: "pixels",
      getFillColor: [255, 255, 255, 215],
      parameters: { depthCompare: "always" },
    }),
  );

  layers.push(...clusterLayers(forecast.clusters));

  layers.push(
    new ScatterplotLayer<ZoneForecast>({
      id: "zones",
      data: forecast.zones,
      getPosition: (z) => [z.lon, z.lat],
      getRadius: (z) => z.radius_km * 1000,
      radiusUnits: "meters",
      radiusMinPixels: 11,
      stroked: true,
      getFillColor: (z) => [...zoneColourAt(forecast, z, hour), 200],
      getLineColor: (z) => (z.id === selectedZoneId ? [255, 255, 255, 255] : [12, 14, 18, 230]),
      getLineWidth: (z) => (z.id === selectedZoneId ? 3.5 : 1.5),
      lineWidthUnits: "pixels",
      pickable: true,
      onClick: ({ object }) => {
        if (object) onSelect(object.id);
      },
      updateTriggers: {
        getFillColor: [hour],
        getLineColor: [selectedZoneId],
        getLineWidth: [selectedZoneId],
      },
      parameters: { depthCompare: "always" },
    }),
    new TextLayer<ZoneForecast>({
      id: "zone-names",
      data: visibleNames ? forecast.zones.filter((z) => visibleNames.has(z.id)) : forecast.zones,
      // At the southern edge of the circle, so the name follows the circle when zooming.
      getPosition: (z) => [z.lon, z.lat - z.radius_km / KM_PER_DEG_LAT],
      getText: (z) => z.name,
      getSize: (z) => (z.id === selectedZoneId ? ZONE_NAME_SIZE_SELECTED : ZONE_NAME_SIZE),
      getColor: [255, 255, 255, 255],
      getPixelOffset: [0, 4],
      getTextAnchor: "middle",
      getAlignmentBaseline: "top",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
      fontWeight: 600,
      fontSettings: { sdf: true },
      outlineWidth: 3,
      outlineColor: [10, 12, 16, 235],
      updateTriggers: { getSize: [selectedZoneId] },
      parameters: { depthCompare: "always" },
    }),
  );
  return layers;
}
