// Hazier's smoke layer: a soft grey haze from the hourly smoke grid, drawn over the CAMS bands.
//
// Pure functions (no DOM) so they can be tested; layers.ts turns the result into a canvas.
//
// The grid is coarse (0.5 degrees) and one byte per cell on a log scale. Bytes are interpolated
// bilinearly (so in log space, which keeps a plume's edge soft rather than blocky), Web
// Mercator rows like the CAMS raster, and the opacity grows with the smoke index: faint below
// the arrival threshold, clear above it. Colour is a neutral grey, never a category colour, so
// the haze can not be mistaken for the CAMS air quality bands underneath.

import type { SmokeGrid } from "../data/types";

export type SmokeRaster = {
  width: number;
  height: number;
  data: Uint8ClampedArray<ArrayBuffer>; // RGBA, row 0 is the north edge
  bounds: [number, number, number, number]; // west, south, east, north
};

const HAZE: [number, number, number] = [214, 216, 224];
const MAX_ALPHA = 170;

/** The bytes of the grid (frame, lat, lon), decoded once. */
export function decodeSmokeGrid(grid: SmokeGrid): Uint8Array {
  const text = atob(grid.data);
  const out = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i);
  return out;
}

/** The smoke index a byte stands for (0 below the minimum). */
export function indexOfByte(q: number, grid: SmokeGrid): number {
  return q === 0 ? 0 : grid.min_index * 10 ** ((q - 1) / grid.steps_per_decade);
}

/**
 * Opacity (0 to 255) of a smoke index. Logarithmic: about 60 at a tenth of the threshold, 100
 * at the threshold, 140 at ten times it, capped at MAX_ALPHA.
 */
export function hazeAlpha(index: number, threshold: number): number {
  if (index <= 0) return 0;
  const t = Math.log10(index / threshold); // 0 at the threshold
  return Math.max(0, Math.min(MAX_ALPHA, 100 + 40 * t));
}

const mercatorY = (lat: number) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
const inverseMercatorY = (y: number) => (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * (180 / Math.PI);

/** The haze raster of one frame of the grid. */
export function rasteriseSmoke(
  grid: SmokeGrid,
  bytes: Uint8Array,
  frame: number,
  threshold: number,
  pixelsPerCell = 4,
): SmokeRaster {
  const ny = grid.lats.length;
  const nx = grid.lons.length;
  const half = grid.cell_deg / 2;
  const west = grid.lons[0] - half;
  const east = grid.lons[nx - 1] + half;
  const south = grid.lats[0] - half;
  const north = grid.lats[ny - 1] + half;
  const width = nx * pixelsPerCell;
  const height = ny * pixelsPerCell;
  const data = new Uint8ClampedArray(width * height * 4);
  const base = frame * ny * nx;
  const yTop = mercatorY(north);
  const yBottom = mercatorY(south);

  for (let py = 0; py < height; py++) {
    const lat = inverseMercatorY(yTop + (yBottom - yTop) * ((py + 0.5) / height));
    const fy = Math.min(Math.max((lat - grid.lats[0]) / grid.cell_deg, 0), ny - 1);
    const y0 = Math.min(Math.floor(fy), ny - 2);
    const wy = fy - y0;
    for (let px = 0; px < width; px++) {
      const lon = west + ((px + 0.5) / width) * (east - west);
      const fx = Math.min(Math.max((lon - grid.lons[0]) / grid.cell_deg, 0), nx - 1);
      const x0 = Math.min(Math.floor(fx), nx - 2);
      const wx = fx - x0;
      const i00 = base + y0 * nx + x0;
      const q =
        bytes[i00] * (1 - wy) * (1 - wx) +
        bytes[i00 + 1] * (1 - wy) * wx +
        bytes[i00 + nx] * wy * (1 - wx) +
        bytes[i00 + nx + 1] * wy * wx;
      if (q < 0.5) continue; // transparent
      const index = grid.min_index * 10 ** ((q - 1) / grid.steps_per_decade);
      const o = (py * width + px) * 4;
      data[o] = HAZE[0];
      data[o + 1] = HAZE[1];
      data[o + 2] = HAZE[2];
      // Bytes near 0 fade out smoothly instead of stopping at the minimum.
      data[o + 3] = Math.round(hazeAlpha(index, threshold) * Math.min(1, q / 8));
    }
  }
  return { width, height, data, bounds: [west, south, east, north] };
}
