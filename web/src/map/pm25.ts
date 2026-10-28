// The CAMS PM2.5 colour layer: a raster of discrete Malaysian API category bands.
//
// Pure functions (no DOM) so they can be tested; layers.ts turns the result into a canvas.
//
// The grid holds the hourly value, like the main number of the zone cards. For a forecast hour
// the two nearest 3-hourly frames are mixed linearly, the nodes are interpolated bilinearly to
// a finer raster, and each pixel takes the colour of its category band: no blending between
// categories, so every colour on the map reads as exactly one category, with crisp edges
// where the air crosses a limit.

import type { Pm25Category, Pm25Grid } from "../data/types";
import { CATEGORY_COLOURS, CATEGORY_ORDER, type Rgb } from "../data/derive";

type Bounds = Partial<Record<Pm25Category, number | null>>;
export type Band = { upper: number; colour: Rgb; alpha: number };

// Used when forecast.json has no pm25_baseline (CAMS zones failed but the grid worked).
const FALLBACK_BOUNDS: Bounds = {
  good: 15,
  moderate: 35,
  unhealthy: 75,
  very_unhealthy: 250,
  hazardous: null,
};

// Opacity of each band (0 to 255): clean air barely tints the basemap, haze is clearly visible,
// and none of it hides the labels underneath.
const BAND_ALPHA: Record<Pm25Category, number> = {
  good: 34,
  moderate: 78,
  unhealthy: 125,
  very_unhealthy: 160,
  hazardous: 185,
};

/** The category bands from their upper bounds (the last one is open ended). */
export function bandsFrom(bounds: Bounds = FALLBACK_BOUNDS): Band[] {
  return CATEGORY_ORDER.map((category) => ({
    upper: bounds[category] ?? Infinity,
    colour: CATEGORY_COLOURS[category],
    alpha: BAND_ALPHA[category],
  }));
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

/** [r, g, b, a] (0 to 255) of a concentration in µg/m³: the colour of its band. */
export function colourAt(value: number, bands: Band[]): [number, number, number, number] {
  const band = bands.find((b) => value <= b.upper) ?? bands[bands.length - 1];
  return [...band.colour, band.alpha];
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

// Raster resolution: about this many pixels per degree, whatever the grid spacing (0.4 degrees
// from the ADS, 2 from the fallback). The GPU smooths the image further when it is drawn, and
// the per-hour images stay small enough to cache on a phone (about 190 x 150 pixels).
const PIXELS_PER_DEGREE = 8;

/** Pixels per grid cell for a grid: about PIXELS_PER_DEGREE per degree, at least 2. */
export function pixelsPerCellFor(grid: Pm25Grid): number {
  return Math.max(2, Math.round(PIXELS_PER_DEGREE * (grid.lons[1] - grid.lons[0])));
}

/**
 * The PM2.5 raster for a forecast hour. Each grid node is the centre of a cell of one grid
 * step, so the raster covers half a step beyond the outer nodes. Rows are spaced in Web
 * Mercator so the image lines up with the basemap. A pixel with no valid node around it is
 * transparent. The outer band (about a degree) fades to transparent, so the layer has no hard
 * edge where the data ends.
 */
export function rasterise(
  grid: Pm25Grid,
  hour: number,
  bands: Band[],
  pixelsPerCell = pixelsPerCellFor(grid),
): Raster {
  const ny = grid.lats.length;
  const nx = grid.lons.length;
  const dy = grid.lats[1] - grid.lats[0];
  const dx = grid.lons[1] - grid.lons[0];
  const fadePixels = Math.max(pixelsPerCell, PIXELS_PER_DEGREE);
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
      const [r, g, b, a] = colourAt(sum / weight, bands);
      const edge = Math.min(px + 0.5, width - px - 0.5, py + 0.5, height - py - 0.5);
      const fade = Math.min(edge / fadePixels, 1);
      const o = (py * width + px) * 4;
      data[o] = r;
      data[o + 1] = g;
      data[o + 2] = b;
      data[o + 3] = Math.round(a * fade * fade * (3 - 2 * fade)); // smoothstep
    }
  }
  return { width, height, data, bounds: [west, south, east, north] };
}
