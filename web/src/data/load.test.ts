import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { sourceWarnings, zoneColourAt, zonePointAt } from "./derive";
import {
  ForecastError,
  STALE_AFTER_HOURS,
  ageHours,
  forecastUrl,
  isStale,
  loadForecast,
  validateForecast,
} from "./load";
import type { Forecast } from "./types";

const sampleText = readFileSync(
  fileURLToPath(new URL("../../public/forecast.sample.json", import.meta.url)),
  "utf-8",
);
const sample = (): Record<string, any> => JSON.parse(sampleText);
const T0 = Date.parse("2026-09-14T06:00:00Z"); // generated_at of the sample

const response = (body: string, status = 200) =>
  Promise.resolve(new Response(body, { status })) as Promise<Response>;

describe("forecastUrl", () => {
  it("uses the sample in dev and forecast.json in production", () => {
    expect(forecastUrl("", true)).toBe("/forecast.sample.json");
    expect(forecastUrl("", false)).toBe("/forecast.json");
  });
  it("lets ?forecast= override", () => {
    expect(forecastUrl("?forecast=/other.json", false)).toBe("/other.json");
  });
});

describe("validateForecast", () => {
  it("accepts the sample", () => {
    const f = validateForecast(sample());
    expect(f.zones).toHaveLength(8);
    expect(f.pm25_grid?.values.length).toBe(25 * 21 * 26);
  });

  it("accepts a forecast without the CAMS map layer", () => {
    expect(validateForecast({ ...sample(), pm25_grid: null }).pm25_grid).toBeNull();
  });

  it.each([
    ["not an object", [] as unknown, /not a JSON object/],
    ["no generated_at", { ...sample(), generated_at: undefined }, /generated_at/],
    ["bad date", { ...sample(), generated_at: "yesterday" }, /generated_at/],
    ["no zones", { ...sample(), zones: [] }, /no zones/],
    ["missing list", { ...sample(), trajectories: undefined }, /trajectories is missing/],
    ["no smoke model", { ...sample(), smoke_model: null }, /smoke_model/],
  ])("rejects %s", (_name, data, message) => {
    expect(() => validateForecast(data)).toThrow(ForecastError);
    expect(() => validateForecast(data)).toThrow(message);
  });

  it("rejects an incompatible format version with a useful message", () => {
    expect(() => validateForecast({ ...sample(), schema_version: 1 })).toThrow(/version 1/);
  });

  it("rejects a zone without coordinates", () => {
    const data = sample();
    delete data.zones[3].lat;
    expect(() => validateForecast(data)).toThrow(/zones\[3\]\.lat/);
  });

  it("rejects a trajectory whose path and hours differ", () => {
    const data = sample();
    data.trajectories[2].hours.pop();
    expect(() => validateForecast(data)).toThrow(/trajectories\[2\]/);
  });

  it("rejects a grid whose size does not match", () => {
    const data = sample();
    data.pm25_grid.values.pop();
    expect(() => validateForecast(data)).toThrow(/pm25_grid values/);
  });
});

describe("staleness", () => {
  const forecast = validateForecast(sample());
  it("is fresh up to 9 h and stale after", () => {
    expect(STALE_AFTER_HOURS).toBe(9);
    expect(ageHours(forecast, new Date(T0 + 3 * 3_600_000))).toBeCloseTo(3);
    expect(isStale(forecast, new Date(T0 + 9 * 3_600_000))).toBe(false);
    expect(isStale(forecast, new Date(T0 + 9.01 * 3_600_000))).toBe(true);
  });
});

describe("loadForecast", () => {
  it("returns the validated forecast", async () => {
    const f = await loadForecast("/x.json", () => response(sampleText));
    expect(f.generated_at).toBe("2026-09-14T06:00:00Z");
  });
  it("explains a missing file, a server error, a network error and broken JSON", async () => {
    await expect(loadForecast("/x", () => response("", 404))).rejects.toThrow(/No forecast/);
    await expect(loadForecast("/x", () => response("", 503))).rejects.toThrow(/HTTP 503/);
    await expect(loadForecast("/x", () => Promise.reject(new TypeError("offline")))).rejects.toThrow(
      /connection/,
    );
    await expect(loadForecast("/x", () => response("{not json"))).rejects.toThrow(/not valid JSON/);
  });
  it("treats an HTML page served with status 200 as nothing published", async () => {
    const html = () =>
      Promise.resolve(
        new Response("<!doctype html>", { status: 200, headers: { "content-type": "text/html" } }),
      );
    await expect(loadForecast("/x", html)).rejects.toThrow(/No forecast has been published/);
  });
  it("reports a valid JSON file with the wrong shape", async () => {
    await expect(loadForecast("/x", () => response('{"zones": 1}'))).rejects.toThrow(ForecastError);
  });
});

describe("derived values", () => {
  const forecast = validateForecast(sample()) as Forecast;
  it("lists failed sources before degraded ones", () => {
    expect(sourceWarnings(forecast).map((s) => [s.name, s.state])).toEqual([
      ["apims", "failed"],
      ["firms", "degraded"],
    ]);
  });
  it("picks the CAMS point nearest to the forecast hour", () => {
    const zone = forecast.zones[0];
    expect(zonePointAt(forecast, zone, 0)?.time).toBe(zone.cams_pm25[0].time);
    expect(zonePointAt(forecast, zone, 4)?.time).toBe(zone.cams_pm25[1].time); // 3 h is closer
    expect(zonePointAt(forecast, zone, 5)?.time).toBe(zone.cams_pm25[2].time); // 6 h is closer
  });
  it("greys out a zone without CAMS data", () => {
    const zone = { ...forecast.zones[0], cams_pm25: [] };
    expect(zonePointAt(forecast, zone, 0)).toBeNull();
    expect(zoneColourAt(forecast, zone, 0)).toEqual([120, 126, 138]);
  });
});
