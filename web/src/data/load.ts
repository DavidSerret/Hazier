// Loading and minimal validation of forecast.json.
//
// The web only reads this file. A forecast is accepted when the fields the map needs are
// present and consistent; anything else gives a ForecastError with a message fit to show.

import { SCHEMA_VERSION, type Forecast } from "./types";

// A run is every 6 h, so a forecast older than one missed run plus margin is stale.
export const STALE_AFTER_HOURS = 9;

export class ForecastError extends Error {}

/** Where to load from: ?forecast=<url> if given, the sample in dev, forecast.json in production. */
export function forecastUrl(search = "", dev: boolean = import.meta.env.DEV): string {
  const override = new URLSearchParams(search).get("forecast");
  if (override) return override;
  return `${import.meta.env.BASE_URL}${dev ? "forecast.sample.json" : "forecast.json"}`;
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

function fail(what: string): never {
  throw new ForecastError(`The forecast file is not valid: ${what}.`);
}

/** Throws ForecastError unless the data has the minimum the map needs. */
export function validateForecast(data: unknown): Forecast {
  if (!isObject(data)) fail("it is not a JSON object");
  if (data.schema_version !== SCHEMA_VERSION) {
    throw new ForecastError(
      `The forecast uses format version ${String(data.schema_version)} but this page expects ` +
        `version ${SCHEMA_VERSION}. Reload the page; if this persists the site is being updated.`,
    );
  }
  if (typeof data.generated_at !== "string" || !Number.isFinite(Date.parse(data.generated_at))) {
    fail("generated_at is missing or not a date");
  }
  if (!isNum(data.horizon_hours) || data.horizon_hours <= 0) fail("horizon_hours is missing");
  for (const key of ["zones", "clusters", "trajectories", "source_status", "attributions"]) {
    if (!Array.isArray(data[key])) fail(`${key} is missing`);
  }
  if (!isObject(data.smoke_model)) fail("smoke_model is missing");

  const zones = data.zones as unknown[];
  if (zones.length === 0) fail("there are no zones");
  zones.forEach((z, i) => {
    if (!isObject(z) || typeof z.id !== "string" || typeof z.name !== "string") {
      fail(`zones[${i}] has no id or name`);
    }
    for (const k of ["lat", "lon", "radius_km"]) {
      if (!isNum(z[k])) fail(`zones[${i}].${k} is missing`);
    }
    if (!Array.isArray(z.cams_pm25) || !Array.isArray(z.smoke_sources)) {
      fail(`zones[${i}] has no CAMS series or smoke sources`);
    }
  });

  (data.clusters as unknown[]).forEach((c, i) => {
    if (!isObject(c) || !isNum(c.lat) || !isNum(c.lon) || !isNum(c.frp_mw)) {
      fail(`clusters[${i}] is incomplete`);
    }
  });
  (data.trajectories as unknown[]).forEach((t, i) => {
    if (!isObject(t) || !Array.isArray(t.path) || !Array.isArray(t.hours)) {
      fail(`trajectories[${i}] has no path or hours`);
    }
    if (t.path.length !== t.hours.length) fail(`trajectories[${i}]: path and hours differ`);
  });

  const grid = data.pm25_grid;
  if (grid != null) {
    if (!isObject(grid) || !Array.isArray(grid.lats) || !Array.isArray(grid.lons)) {
      fail("pm25_grid has no lats or lons");
    }
    const { lats, lons, hours, values } = grid as Record<string, unknown[]>;
    if (!Array.isArray(hours) || !Array.isArray(values)) fail("pm25_grid has no hours or values");
    if (values.length !== hours.length * lats.length * lons.length) {
      fail("pm25_grid values do not match hours x latitudes x longitudes");
    }
    if (lats.length < 2 || lons.length < 2 || hours.length < 1) fail("pm25_grid is too small");
  }
  return data as unknown as Forecast;
}

/** Hours between generated_at (t0, the start of the forecast) and now. */
export function ageHours(forecast: Forecast, now: Date = new Date()): number {
  return (now.getTime() - Date.parse(forecast.generated_at)) / 3_600_000;
}

/**
 * Whether the forecast is stale. generated_at is the hour the run was made in (t0), and a run
 * takes a few minutes, so the age is slightly pessimistic; the margin in STALE_AFTER_HOURS
 * covers that.
 */
export function isStale(forecast: Forecast, now: Date = new Date()): boolean {
  return ageHours(forecast, now) > STALE_AFTER_HOURS;
}

export async function loadForecast(
  url: string,
  fetchFn: typeof fetch = fetch,
): Promise<Forecast> {
  let response: Response;
  try {
    // no-cache: always revalidate, the file changes every 6 hours under the same URL.
    response = await fetchFn(url, { cache: "no-cache" });
  } catch {
    throw new ForecastError(
      "Could not reach the forecast. Check your connection and try again.",
    );
  }
  if (!response.ok) {
    throw new ForecastError(
      response.status === 404
        ? "No forecast has been published yet."
        : `The forecast could not be loaded (HTTP ${response.status}).`,
    );
  }
  // A host that answers a missing file with its HTML page (a single page app fallback, a CDN
  // error page) still says 200: that is "nothing published", not a damaged forecast.
  if ((response.headers.get("content-type") ?? "").includes("text/html")) {
    throw new ForecastError("No forecast has been published yet.");
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new ForecastError("The forecast file is damaged (it is not valid JSON).");
  }
  return validateForecast(data);
}
