// The CAMS PM2.5 colour layer: a smooth raster made from the 1 degree grid.
//
// Pure functions (no DOM) so they can be tested; layers.ts turns the result into a canvas.
//
// The grid holds the trailing 24 h mean, the same basis as the zone categories, so the map and
// the cards colour the same air the same way. For a forecast hour the two nearest 3-hourly
// frames are mixed linearly, then the 21 x 26 nodes are interpolated bilinearly to a finer
// raster. Colour comes from a ramp that sits exactly on each category colour at the middle of
// the category's band and blends linearly in between, so the map is smooth but every colour
// is recognisably a Malaysian API category.

import type { Pm25Category, Pm25Grid } from "../data/types";
import { CATEGORY_COLOURS, CATEGORY_ORDER, type Rgb } from "../data/derive";

type Bounds = Partial<Record<Pm25Category, number | null>>;
export type RampStop = { value: number; colour: Rgb };

// Used when forecast.json has no pm25_baseline (CAMS zones failed but the grid worked).
const FALLBACK_BOUNDS: Bounds = {
  good: 15,
  moderate: 35,
  unhealthy: 75,
  very_unhealthy: 250,
  hazardous: null,
};

/** Ramp stops at the middle of each category band (the open last band gets 1.2 x its start). */
export function rampStops(bounds: Bounds = FALLBACK_BOUNDS): RampStop[] {
  const stops: RampStop[] = [];
  let lower = 0;
  for (const category of CATEGORY_ORDER) {
    const upper = bounds[category];
    const centre = upper == null ? lower * 1.2 : (lower + upper) / 2;
    stops.push({ value: centre, colour: CATEGORY_COLOURS[category] });
    if (upper != null) lower = upper;
  }
  stops[0] = { value: 0, colour: stops[0].colour }; // clean air starts at 0 with the first colour
  return stops;
}

// Opacity by concentration (0 to 255): clean air barely tints the basemap, haze is clearly
// visible, and none of it hides the labels underneath.
const ALPHA_STOPS: [number, number][] = [
  [0, 22],
  [15, 58],
  [35, 120],
  [75, 165],
  [250, 190],
];

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

function alphaAt(value: number): number {
  const last = ALPHA_STOPS[ALPHA_STOPS.length - 1];
  if (value >= last[0]) return last[1];
  for (let i = 1; i < ALPHA_STOPS.length; i++) {
    const [v1, a1] = ALPHA_STOPS[i];
    if (value <= v1) {
      const [v0, a0] = ALPHA_STOPS[i - 1];
      return lerp(a0, a1, (value - v0) / (v1 - v0));
    }
  }
  return last[1];
}

/** [r, g, b, a] (0 to 255) of a concentration in µg/m³. */
export function colourAt(value: number, stops: RampStop[]): [number, number, number, number] {
  const a = Math.round(alphaAt(value));
  if (value <= stops[0].value) return [...stops[0].colour, a];
  const last = stops[stops.length - 1];
  if (value >= last.value) return [...last.colour, a];
  for (let i = 1; i < stops.length; i++) {
    if (value <= stops[i].value) {
      const s0 = stops[i - 1];
      const s1 = stops[i];
      const t = (value - s0.value) / (s1.value - s0.value);
      return [
        Math.round(lerp(s0.colour[0], s1.colour[0], t)),
        Math.round(lerp(s0.colour[1], s1.colour[1], t)),
        Math.round(lerp(s0.colour[2], s1.colour[2], t)),
        a,
      ];
    }
  }
  return [...last.colour, a];
}

/** The grid at a forecast hour, in µg/m³ row-major (lat, lon); NaN where missing. */
export function gridAtHour(grid: Pm25Grid, hour: number): Float32Array {
  const ny = grid.lats.length;
  const nx = grid.lons.length;
  const frames = grid.hours.length;
  const clamped = Math.min(Math.max(hour, grid.hours[0]), grid.hours[frames - 1]);
  let i1 = grid.hours.findIndex((h) => h >= clamped);
  if (i1 < 0) i1 = frames - 1;
  const i0 = Math.max(i1 - 1, 0);
  const span = grid.hours[i1] - grid.hours[i0];
  const t = span > 0 ? (clamped - grid.hours[i0]) / span : 0;
  const out = new Float32Array(ny * nx);
  const size = ny * nx;
  for (let k = 0; k < size; k++) {
    const v0 = grid.values[i0 * size + k];
    const v1 = grid.values[i1 * size + k];
    if (v0 == null && v1 == null) out[k] = NaN;
    else if (v0 == null) out[k] = (v1 as number) * grid.quantum_ugm3;
    else if (v1 == null) out[k] = v0 * grid.quantum_ugm3;
    else out[k] = lerp(v0, v1, t) * grid.quantum_ugm3;
  }
  return out;
}

const mercatorY = (latDeg: number) =>
  Math.log(Math.tan(Math.PI / 4 + (latDeg * Math.PI) / 360));
const inverseMercatorY = (y: number) => (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * (180 / Math.PI);

export type Raster = {
  width: number;
  height: number;
  data: Uint8ClampedArray<ArrayBuffer>; // RGBA, row 0 is the north edge
  bounds: [number, number, number, number]; // west, south, east, north (degrees)
};

/**
 * The PM2.5 raster for a forecast hour. Each grid node is the centre of a cell of one grid
 * step, so the raster covers half a step beyond the outer nodes. Rows are spaced in Web
 * Mercator so the image lines up with the basemap. A pixel with no valid node around it is
 * transparent. The outer cell fades to transparent, so the layer has no hard edge where the
 * data ends.
 */
export function rasterise(
  grid: Pm25Grid,
  hour: number,
  stops: RampStop[],
  pixelsPerCell = 8,
): Raster {
  const ny = grid.lats.length;
  const nx = grid.lons.length;
  const dy = grid.lats[1] - grid.lats[0];
  const dx = grid.lons[1] - grid.lons[0];
  const west = grid.lons[0] - dx / 2;
  const east = grid.lons[nx - 1] + dx / 2;
  const south = grid.lats[0] - dy / 2;
  const north = grid.lats[ny - 1] + dy / 2;
  const width = nx * pixelsPerCell;
  const height = ny * pixelsPerCell;
  const values = gridAtHour(grid, hour);
  const data = new Uint8ClampedArray(width * height * 4);
  const yTop = mercatorY(north);
  const yBottom = mercatorY(south);

  for (let py = 0; py < height; py++) {
    const lat = inverseMercatorY(lerp(yTop, yBottom, (py + 0.5) / height));
    const fy = Math.min(Math.max((lat - grid.lats[0]) / dy, 0), ny - 1);
    const y0 = Math.min(Math.floor(fy), ny - 2);
    const wy = fy - y0;
    for (let px = 0; px < width; px++) {
      const lon = west + ((px + 0.5) / width) * (east - west);
      const fx = Math.min(Math.max((lon - grid.lons[0]) / dx, 0), nx - 1);
      const x0 = Math.min(Math.floor(fx), nx - 2);
      const wx = fx - x0;
      let sum = 0;
      let weight = 0;
      for (let j = 0; j < 4; j++) {
        const cy = y0 + (j >> 1);
        const cx = x0 + (j & 1);
        const w = (j >> 1 ? wy : 1 - wy) * (j & 1 ? wx : 1 - wx);
        const v = values[cy * nx + cx];
        if (w > 0 && !Number.isNaN(v)) {
          sum += v * w;
          weight += w;
        }
      }
      if (weight === 0) continue; // stays transparent
      const [r, g, b, a] = colourAt(sum / weight, stops);
      const edge = Math.min(px + 0.5, width - px - 0.5, py + 0.5, height - py - 0.5);
      const fade = Math.min(edge / pixelsPerCell, 1);
      const o = (py * width + px) * 4;
      data[o] = r;
      data[o + 1] = g;
      data[o + 2] = b;
      data[o + 3] = Math.round(a * fade * fade * (3 - 2 * fade)); // smoothstep
    }
  }
  return { width, height, data, bounds: [west, south, east, north] };
}
