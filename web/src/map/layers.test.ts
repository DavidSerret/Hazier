import { describe, expect, it } from "vitest";
import type { Forecast, Pm25Point, Trajectory, ZoneForecast } from "../data/types";
import { buildLayers, fireScale, lineStrength, type MapImages } from "./layers";

const point = (time: string, value: number, category: Pm25Point["category"]): Pm25Point => ({
  time,
  value_ugm3: value,
  basis_ugm3: 40,
  category: "unhealthy",
  hourly_category: category,
});

const zone: ZoneForecast = {
  id: "z",
  name: "Zone",
  lat: 3,
  lon: 101,
  radius_km: 50,
  smoke_eta_hours: null,
  smoke_eta_earliest_hours: null,
  smoke_eta_latest_hours: null,
  smoke_sources: [],
  cams_now: null,
  cams_peak: null,
  cams_trend: null,
  cams_pm25: [point("2026-09-14T06:00:00Z", 8, "good"), point("2026-09-14T07:00:00Z", 60, "unhealthy")],
  agreement: null,
  smoke_index: [0, 25],
};

const forecast = {
  generated_at: "2026-09-14T06:00:00Z",
  horizon_hours: 72,
  zones: [zone],
  clusters: [],
  trajectories: [],
  smoke_model: { arrival_threshold: 10, removal_lifetime_hours: 72 },
} as unknown as Forecast;

const noImages = { pm25At: () => null, smokeAt: () => null } as unknown as MapImages;
const picks = { onSelectZone: () => {}, onPickFire: () => {} };

function layer(id: string, hour = 0, zoom = 5) {
  const layers = buildLayers({ forecast, hour, selectedZoneId: "z", zoom }, noImages, picks);
  const found = layers.find((l) => l.id === id);
  if (!found) throw new Error(`no layer ${id}`);
  return found.props as unknown as Record<string, unknown>;
}

describe("zone markers", () => {
  it("are sized in screen pixels, not metres", () => {
    expect(layer("zones").radiusUnits).toBe("pixels");
  });

  it("make the selected zone a little bigger", () => {
    const getRadius = layer("zones").getRadius as (z: ZoneForecast) => number;
    expect(getRadius(zone)).toBeGreaterThan(getRadius({ ...zone, id: "other" }));
  });

  it("show the hourly PM2.5 number of the hour", () => {
    const getText = layer("zone-numbers", 0).getText as (z: ZoneForecast) => string;
    expect(getText(zone)).toBe("8");
    const later = layer("zone-numbers", 1).getText as (z: ZoneForecast) => string;
    expect(later(zone)).toBe("60");
  });

  it("follow the whole hour while the flow lines use the exact time", () => {
    expect((layer("zones", 1.4).updateTriggers as { getFillColor: number[] }).getFillColor).toEqual([1]);
    expect(layer("smoke-lines", 1.4).currentTime).toBe(1.4);
  });

  it("get the smoke ring only at hours with smoke aloft", () => {
    expect((layer("zone-smoke-ring", 0).data as ZoneForecast[]).length).toBe(0);
    expect((layer("zone-smoke-ring", 1).data as ZoneForecast[]).map((z) => z.id)).toEqual(["z"]);
  });
});

describe("flow lines and fires", () => {
  const line = (weight: number, release: number) => ({ weight, release_hour: release }) as Trajectory;

  it("fade with the smoke the line carries", () => {
    expect(lineStrength(line(10, 0), 0, 72, 10)).toBe(1);
    expect(lineStrength(line(5, 0), 0, 72, 10)).toBe(0.5);
    expect(lineStrength(line(10, 0), 72, 72, 10)).toBeCloseTo(Math.exp(-1));
    expect(lineStrength(line(10, -24), 0, 72, 10)).toBeCloseTo(Math.exp(-24 / 72));
  });

  it("fires shrink and dim as the map zooms in", () => {
    expect(fireScale(4)).toEqual({ size: 1, alpha: 1 });
    const close = fireScale(10);
    expect(close.size).toBeLessThan(0.5) ;
    expect(close.alpha).toBeLessThan(0.5);
  });
});
