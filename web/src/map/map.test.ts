import { describe, expect, it } from "vitest";
import { CATEGORY_COLOURS } from "../data/derive";
import type { Pm25Grid } from "../data/types";
import { bandsFrom, colourAt, gridAtHour, pixelsPerCellFor, rasterise } from "./pm25";

const bands = bandsFrom();

/** A 3 x 3 grid at 1 degree, 2 frames 3 h apart. value(frame, y, x) given by fn. */
function grid(fn: (f: number, y: number, x: number) => number | null): Pm25Grid {
  const values: (number | null)[] = [];
  for (let f = 0; f < 2; f++)
    for (let y = 0; y < 3; y++) for (let x = 0; x < 3; x++) values.push(fn(f, y, x));
  return {
    lats: [0, 1, 2],
    lons: [100, 101, 102],
    hours: [0, 3],
    averaging_hours: 24,
    quantum_ugm3: 1,
    values,
    source: "test",
  };
}

describe("category bands", () => {
  it("gives every value the colour of its band, with no blending", () => {
    expect(colourAt(10, bands).slice(0, 3)).toEqual(CATEGORY_COLOURS.good);
    expect(colourAt(15, bands).slice(0, 3)).toEqual(CATEGORY_COLOURS.good); // upper bound inclusive
    expect(colourAt(15.1, bands).slice(0, 3)).toEqual(CATEGORY_COLOURS.moderate);
    expect(colourAt(40, bands).slice(0, 3)).toEqual(CATEGORY_COLOURS.unhealthy);
    expect(colourAt(900, bands).slice(0, 3)).toEqual(CATEGORY_COLOURS.hazardous);
  });
  it("is more opaque for dirtier air, never fully opaque", () => {
    const alphas = [0, 20, 50, 100, 300].map((v) => colourAt(v, bands)[3]);
    expect(alphas).toEqual([...alphas].sort((a, b) => a - b));
    expect(new Set(alphas).size).toBe(5);
    expect(Math.max(...alphas)).toBeLessThan(255);
  });
  it("uses the bounds of the forecast when it has them", () => {
    const custom = bandsFrom({ good: 10, moderate: 20, unhealthy: 40, very_unhealthy: 100, hazardous: null });
    expect(custom.map((b) => b.upper)).toEqual([10, 20, 40, 100, Infinity]);
    expect(colourAt(12, custom).slice(0, 3)).toEqual(CATEGORY_COLOURS.moderate);
  });
});

describe("gridAtHour", () => {
  const g = grid((f, y, x) => (f === 0 ? 10 : 40) + y + x);
  it("returns the frames at their hours and mixes linearly between them", () => {
    expect(gridAtHour(g, 0)[4]).toBe(12); // frame 0, centre node: 10 + 1 + 1
    expect(gridAtHour(g, 3)[4]).toBe(42);
    expect(gridAtHour(g, 1.5)[4]).toBeCloseTo(27);
  });
  it("clamps outside the frames", () => {
    expect(gridAtHour(g, -5)[0]).toBe(10);
    expect(gridAtHour(g, 99)[0]).toBe(40);
  });
  it("uses the other frame when one is missing, NaN when both are", () => {
    const holes = grid((f, y, x) => (y === 0 && x === 0 ? (f === 0 ? null : 7) : 5));
    expect(gridAtHour(holes, 1)[0]).toBe(7);
    const none = grid((_f, y, x) => (y === 0 && x === 0 ? null : 5));
    expect(gridAtHour(none, 1)[0]).toBeNaN();
  });
});

describe("rasterise", () => {
  const flat = grid(() => 25);
  const r = rasterise(flat, 0, bands, 4);

  it("covers half a grid step beyond the outer nodes", () => {
    expect(r.bounds).toEqual([99.5, -0.5, 102.5, 2.5]);
    expect([r.width, r.height]).toEqual([12, 12]);
    expect(r.data).toHaveLength(12 * 12 * 4);
  });
  it("paints a uniform grid with one colour", () => {
    const rgb = (i: number) => [...r.data.slice(i, i + 3)];
    expect(rgb(0)).toEqual(CATEGORY_COLOURS.moderate);
    for (let i = 0; i < r.data.length; i += 4) expect(rgb(i)).toEqual(CATEGORY_COLOURS.moderate);
  });
  it("puts north at the top and west at the left", () => {
    // Dirty air in the north-east node (lat 2, lon 102), clean elsewhere.
    const g = grid((_f, y, x) => (y === 2 && x === 2 ? 300 : 5));
    const out = rasterise(g, 0, bands, 4);
    const red = (px: number, py: number) => out.data[(py * out.width + px) * 4]; // red channel
    expect(red(out.width - 1, 0)).toBeGreaterThan(150); // north-east corner: hazardous red
    expect(red(0, out.height - 1)).toBeLessThan(60); // south-west corner: good blue
  });
  it("interpolates bilinearly between nodes", () => {
    const g = grid((_f, _y, x) => x * 50); // 0, 50, 100 from west to east
    const out = rasterise(g, 0, bands, 8);
    for (let px = 0; px < out.width; px++) {
      // Node x sits at the centre of cell x, i.e. at pixel (x + 0.5) * 8 on this raster.
      const fx = Math.min(Math.max((px + 0.5) / 8 - 0.5, 0), 2);
      const expected = colourAt(50 * fx, bands);
      const o = (12 * out.width + px) * 4;
      expect([...out.data.slice(o, o + 3)]).toEqual(expected.slice(0, 3));
      const inside = px >= 8 && px < out.width - 8; // beyond the faded outer cell
      if (inside) expect(out.data[o + 3]).toBe(expected[3]);
      else expect(out.data[o + 3]).toBeLessThan(expected[3] + 1);
    }
  });
  it("fades the outer band (about a degree, 8 pixels) to transparent", () => {
    const wide = rasterise(flat, 0, bands, 8); // 24 pixels across
    const edge = (px: number) => wide.data[(12 * wide.width + px) * 4 + 3];
    expect(edge(0)).toBeLessThan(edge(2));
    expect(edge(2)).toBeLessThan(edge(4));
    expect(edge(4)).toBeLessThanOrEqual(edge(8));
    expect(edge(0)).toBeLessThan(5);
    expect(edge(12)).toBe(colourAt(25, bands)[3]); // more than 8 pixels in: untouched
  });

  it("uses about 8 pixels per degree whatever the grid spacing", () => {
    const fine = { ...flat, lats: [0, 0.4, 0.8], lons: [100, 100.4, 100.8] };
    expect(pixelsPerCellFor(fine)).toBe(3);
    expect(pixelsPerCellFor(flat)).toBe(8);
    expect(pixelsPerCellFor({ ...flat, lons: [100, 102, 104] })).toBe(16);
  });
  it("fills a gap from its neighbours but never invents data at the edge", () => {
    const southRowMissing = grid((_f, y, _x) => (y === 0 ? null : 25));
    const out = rasterise(southRowMissing, 0, bands, 4);
    const alphaAt = (py: number) => out.data[(py * out.width + 6) * 4 + 3];
    expect(alphaAt(8)).toBeGreaterThan(0); // between rows 0 and 1: filled from row 1
    expect(alphaAt(out.height - 1)).toBe(0); // the outer half cell of the missing row: empty
    const empty = rasterise(grid(() => null), 0, bands, 4);
    expect(empty.data.every((b) => b === 0)).toBe(true);
  });
});
