import { describe, expect, it } from "vitest";
import type { SmokeGrid } from "../data/types";
import { decodeSmokeGrid, hazeAlpha, indexOfByte, rasteriseSmoke } from "./smokegrid";

/** 2 frames of a 3 x 3 grid at 0.5 degrees; frame 1 has smoke in the centre cell. */
function grid(centre: number): SmokeGrid {
  const bytes = new Uint8Array(18);
  bytes[9 + 4] = centre;
  return {
    lats: [0.25, 0.75, 1.25],
    lons: [100.25, 100.75, 101.25],
    hours: [0, 1],
    cell_deg: 0.5,
    min_index: 1,
    steps_per_decade: 40,
    data: btoa(String.fromCharCode(...bytes)),
  };
}

describe("smoke grid", () => {
  it("decodes the base64 bytes", () => {
    const bytes = decodeSmokeGrid(grid(41));
    expect(bytes.length).toBe(18);
    expect(bytes[13]).toBe(41);
  });

  it("maps bytes to the smoke index on a log scale", () => {
    const g = grid(0);
    expect(indexOfByte(0, g)).toBe(0);
    expect(indexOfByte(1, g)).toBe(1); // the minimum
    expect(indexOfByte(41, g)).toBeCloseTo(10); // 40 steps: one factor of ten
  });

  it("is faint below the threshold and clearer above it, never opaque", () => {
    expect(hazeAlpha(0, 10)).toBe(0);
    expect(hazeAlpha(1, 10)).toBeLessThan(hazeAlpha(10, 10));
    expect(hazeAlpha(10, 10)).toBe(100);
    expect(hazeAlpha(1e6, 10)).toBeLessThan(255);
  });

  it("draws grey haze only where there is smoke", () => {
    const g = grid(81); // an index of 100
    const bytes = decodeSmokeGrid(g);
    const empty = rasteriseSmoke(g, bytes, 0, 10);
    expect(empty.data.some((v, i) => i % 4 === 3 && v > 0)).toBe(false);
    const r = rasteriseSmoke(g, bytes, 1, 10);
    const centre = ((r.height / 2) * r.width + r.width / 2) * 4;
    expect(r.data[centre + 3]).toBeGreaterThan(50);
    expect(r.data[centre]).toBe(r.data[centre + 1] - 2); // neutral grey, not a category colour
    expect(r.bounds).toEqual([100, 0, 101.5, 1.5]);
  });
});
