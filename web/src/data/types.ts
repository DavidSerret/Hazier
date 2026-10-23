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

export interface Trajectory {
  id: number;
  hotspot_id: number;
  source_region: string;
  path: [number, number][];
  hours: number[];
}

export interface SourceShare {
  region: string;
  share: number;
}

export interface RiskPoint {
  hour: number;
  index: number;
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
  risk_index: number;
  risk_level: RiskLevel;
  eta_hours: number | null;
  sources: SourceShare[];
  risk_series: RiskPoint[];
  cams_pm25: Pm25Point[];
}

export interface SourceStatus {
  name: SourceName;
  ok: boolean;
  last_ok: string | null;
}

export interface Forecast {
  generated_at: string;
  horizon_hours: number;
  hotspots: Hotspot[];
  trajectories: Trajectory[];
  zones: ZoneForecast[];
  source_status: SourceStatus[];
  schema_version: number;
}
