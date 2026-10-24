import { describe, expect, it } from "vitest";
import type { Forecast, Pm25Point, ZoneForecast } from "../data/types";
import { buildLayers, type Pm25Images } from "./layers";

const point = (time: string, category: Pm25Point["category"]): Pm25Point => ({
  time,
  value_ugm3: 10,
  basis_ugm3: 10,
  category,
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
  cams_pm25: [point("2026-09-14T06:00:00Z", "good"), point("2026-09-14T09:00:00Z", "unhealthy")],
  agreement: null,
};

const forecast = {
  generated_at: "2026-09-14T06:00:00Z",
  horizon_hours: 72,
  zones: [zone],
  clusters: [],
  trajectories: [],
} as unknown as Forecast;

const noImages = { get: () => null, bounds: null } as unknown as Pm25Images;

function layer(id: string, hour = 0) {
  const layers = buildLayers({ forecast, hour, selectedZoneId: "z" }, noImages, () => {});
  const found = layers.find((l) => l.id === id);
  if (!found) throw new Error(`no layer ${id}`);
  return found.props as unknown as Record<string, unknown>;
}

describe("zone markers", () => {
  it("are sized in screen pixels, not metres", () => {
    const props = layer("zones");
    expect(props.radiusUnits).toBe("pixels");
    expect(props.radiusMinPixels ?? 0).toBe(0);
  });

  it("make the selected zone a little bigger", () => {
    const getRadius = layer("zones").getRadius as (z: ZoneForecast) => number;
    const other = { ...zone, id: "other" };
    expect(getRadius(zone)).toBeGreaterThan(getRadius(other));
  });

  it("follow the whole hour while the trails use the exact time", () => {
    // Hour 1.4 is drawn as hour 1 for colours; the trails get the exact 1.4.
    expect((layer("zones", 1.4).updateTriggers as { getFillColor: number[] }).getFillColor).toEqual([1]);
    expect(layer("smoke-trails", 1.4).currentTime).toBe(1.4);
  });
});
