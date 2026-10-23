// Mirrors pipeline/hazier/schema.py field by field. Change both together.
//
// All timestamps are ISO 8601 strings in UTC. Display them in Malaysia time (MYT, UTC+8)
// with the helpers in ./time.ts.
// Coordinates are [lon, lat] pairs. "hours" and "hour" are offsets from generated_at.

export const SCHEMA_VERSION = 1;

// Our own trajectory risk levels.
export type RiskLevel = "low" | "moderate" | "high" | "very_high";

// Malaysian API categories, used only for CAMS PM2.5 concentrations.
export type Pm25Category =
  | "good"
  | "moderate"
  | "unhealthy"
  | "very_unhealthy"
  | "hazardous";

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
}

export interface Trajectory {
  id: number;
  cluster_id: number;
  weight: number;
  path: [number, number][];
  hours: number[];
  source_region: string;
}

export interface SourceShare {
  region: string;
  share: number;
}

export interface RiskPoint {
  hour: number;
  index: number; // smoke index, >= 0 and unbounded
  level: RiskLevel;
}

export interface Pm25Point {
  time: string;
  value_ugm3: number;
  category: Pm25Category;
}

export interface ZoneForecast {
  id: string;
  name: string;
  lat: number;
  lon: number;
  radius_km: number;
  risk_index: number; // peak smoke index
  risk_level: RiskLevel;
  peak_hour: number | null;
  eta_hours: number | null;
  // Both null when eta_hours is null; eta_latest_hours null alone means "possibly after 72 h".
  eta_earliest_hours: number | null;
  eta_latest_hours: number | null;
  sources: SourceShare[];
  risk_series: RiskPoint[];
  cams_pm25: Pm25Point[];
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

export interface Forecast {
  generated_at: string;
  horizon_hours: number;
  hotspots: Hotspot[];
  clusters: SourceCluster[];
  trajectories: Trajectory[];
  zones: ZoneForecast[];
  risk_thresholds: Record<RiskLevel, number>; // lower bound of the smoke index per level
  source_status: SourceStatus[];
  schema_version: number;
}
