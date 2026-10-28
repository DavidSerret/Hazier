// Mirrors pipeline/hazier/schema.py field by field. Change both together.
//
// All timestamps are ISO 8601 strings in UTC. Display them in Malaysia time (MYT, UTC+8)
// with the helpers in ./time.ts.
// Coordinates are [lon, lat] pairs. "hours" and "hour" are offsets from generated_at.

// 2: smoke_model.wind_source, wind_run and wind_grid_deg; pm25_grid.source.
// 3: hourly CAMS points with hourly_category; zone smoke_index; trajectory release_hour;
//    smoke_grid; pm25_grid holds hourly values.
export const SCHEMA_VERSION = 3;

// Malaysian API categories, used for CAMS PM2.5 concentrations: the air quality shown to users.
export type Pm25Category =
  | "good"
  | "moderate"
  | "unhealthy"
  | "very_unhealthy"
  | "hazardous";

// CAMS over the next 24 h compared with the last 24 h, for the zone card.
export type Trend = "improving" | "stable" | "worsening";

export type SourceName = "firms" | "winds" | "cams" | "apims";

export interface Hotspot {
  id: number;
  lat: number;
  lon: number;
  frp_mw: number;
  confidence: string;
  acquired_at: string;
  satellite: string;
  region: string;
}

export interface SourceCluster {
  id: number;
  lat: number;
  lon: number;
  frp_mw: number;
  hotspot_count: number;
  latest_at: string;
  region: string;
  first_at: string; // earliest detection in the 48 h lookback; "" when unknown
}

export interface Trajectory {
  id: number;
  cluster_id: number;
  weight: number;
  path: [number, number][];
  hours: number[];
  source_region: string;
  // Hour of the release (negative before t0, when the path starts at 0). The smoke left on
  // the line at hour h is weight * exp(-(h - release_hour) / removal_lifetime_hours).
  release_hour: number;
}

export interface SourceShare {
  region: string;
  share: number; // 0 to 1 in steps of 0.001; the shares of a zone sum to exactly 1
}

// Whether Hazier expects smoke from the detected fires and whether CAMS expects elevated PM2.5,
// over the window. For verification, not for display.
export type AgreementFlag = "both_elevated" | "hazier_only" | "cams_only" | "both_low";

export interface Pm25Point {
  time: string;
  value_ugm3: number; // CAMS hourly value at the zone centre
  basis_ugm3: number; // trailing mean the category is based on
  category: Pm25Category; // of basis_ugm3: the official-style value
  hourly_category: Pm25Category; // the same limits on the hourly value: the main number
}

// One zone: air quality from CAMS; smoke timing and origin from Hazier. The level shown to
// users is the CAMS category; Hazier publishes no level of its own.
export interface ZoneForecast {
  id: string;
  name: string;
  lat: number;
  lon: number;
  radius_km: number;
  // 0: smoke already there at t0; null: not within the horizon. Earliest and latest are null
  // with the ETA; latest alone null means "possibly after 72 h".
  smoke_eta_hours: number | null;
  smoke_eta_earliest_hours: number | null;
  smoke_eta_latest_hours: number | null;
  smoke_sources: SourceShare[];
  cams_now: Pm25Point | null; // at t0: the level to show on the card
  cams_peak: Pm25Point | null; // the hour of the highest category basis in the window
  cams_trend: Trend | null;
  cams_pm25: Pm25Point[]; // empty when CAMS has no data for the zone
  agreement: AgreementFlag | null;
  // Hazier's smoke index over the zone at forecast hours 0 .. 72. At or above
  // smoke_model.arrival_threshold the smoke is over the zone at 850 hPa ("smoke aloft").
  smoke_index: number[];
}

// ok: all data arrived. degraded: usable but incomplete, show a warning.
// failed: no usable data, the last valid forecast is being served.
export type SourceState = "ok" | "degraded" | "failed";

export interface SourceStatus {
  name: SourceName;
  state: SourceState;
  last_ok: string | null;
  detail: string | null;
}

// Credit that must be shown wherever the data is shown.
export interface Attribution {
  name: string;
  text: string;
  url: string;
}

// How to read cams_pm25: where it comes from and how the categories were derived.
export interface Pm25Baseline {
  source: string;
  resolution_km: number; // about 45: one value stands for a cell much larger than a zone
  step_hours: number; // spacing of the points in cams_pm25
  averaging_hours: number; // trailing mean behind each category
  averaging_verified: boolean; // whether that averaging period is confirmed as the DOE's
  // False until the Malaysian API breakpoints are checked against the DOE document:
  // the web must label the categories as provisional.
  categories_verified: boolean;
  category_upper_bounds: Record<Pm25Category, number | null>; // ug/m3, null = open ended
  cams_elevated_category: Pm25Category;
}

// CAMS PM2.5 on a regular grid for the map colour layer. values is row-major (frame, lat, lon),
// latitude south to north, longitude west to east; concentration = value * quantum_ugm3, null
// = missing. Hourly values since schema 3 (averaging_hours 1).
export interface Pm25Grid {
  lats: number[];
  lons: number[];
  hours: number[];
  averaging_hours: number;
  quantum_ugm3: number;
  values: (number | null)[];
  source: string; // for example "CAMS global via the Copernicus Atmosphere Data Store (0.4 degrees)"
}

// Hazier's smoke per forecast hour on a coarse grid. data is base64 of one byte per (hour, lat,
// lon), row-major, latitude south to north; a byte q > 0 stands for a smoke index of
// min_index * 10 ** ((q - 1) / steps_per_decade), 0 for less than min_index. Same units as a
// zone's smoke index. Smoke at 850 hPa, not a ground concentration.
export interface SmokeGrid {
  lats: number[]; // cell centres, ascending
  lons: number[];
  hours: number[];
  cell_deg: number;
  min_index: number;
  steps_per_decade: number;
  data: string;
}

// How Hazier's smoke timing and attribution were made. Not an air quality measure.
export interface SmokeModel {
  wind_model: string;
  level_hpa: number;
  lookback_hours: number;
  past_hours: number;
  removal_lifetime_hours: number;
  arrival_threshold: number; // internal smoke index units, provisional
  wind_source: string; // "ecmwf_open_data", or "open_meteo" (the fallback)
  wind_run: string | null; // UTC start of the model run of the forecast part
  wind_grid_deg: number;
}

export interface Forecast {
  generated_at: string;
  horizon_hours: number;
  hotspots: Hotspot[];
  clusters: SourceCluster[];
  trajectories: Trajectory[];
  zones: ZoneForecast[];
  smoke_model: SmokeModel;
  source_status: SourceStatus[];
  attributions: Attribution[];
  pm25_baseline: Pm25Baseline | null; // null when the CAMS step produced nothing
  pm25_grid: Pm25Grid | null; // null when the CAMS grid could not be fetched
  smoke_grid: SmokeGrid | null; // Hazier's smoke per hour for the map
  schema_version: number;
}
