// Values derived from a Forecast for display: category colours, a zone's state at a forecast
// hour, source warnings.

import type {
  Forecast,
  Pm25Category,
  Pm25Point,
  SourceStatus,
  ZoneForecast,
} from "./types";

export const DEFAULT_ZONE_ID = "klang_valley";

export type Rgb = [number, number, number];

// Malaysian API colours (blue, green, yellow, orange, red), as in the debug plots.
export const CATEGORY_COLOURS: Record<Pm25Category, Rgb> = {
  good: [44, 127, 184],
  moderate: [65, 171, 93],
  unhealthy: [254, 196, 79],
  very_unhealthy: [230, 85, 13],
  hazardous: [165, 15, 21],
};

export const CATEGORY_LABELS: Record<Pm25Category, string> = {
  good: "Good",
  moderate: "Moderate",
  unhealthy: "Unhealthy",
  very_unhealthy: "Very unhealthy",
  hazardous: "Hazardous",
};

export const CATEGORY_ORDER: Pm25Category[] = [
  "good",
  "moderate",
  "unhealthy",
  "very_unhealthy",
  "hazardous",
];

export const NO_DATA_COLOUR: Rgb = [120, 126, 138];

/** Hours since generated_at (t0) of a timestamp. */
export function hoursSinceT0(forecast: Forecast, isoTime: string): number {
  return (Date.parse(isoTime) - Date.parse(forecast.generated_at)) / 3_600_000;
}

/** The CAMS point of a zone closest to a forecast hour, or null when the zone has none. */
export function zonePointAt(
  forecast: Forecast,
  zone: ZoneForecast,
  hour: number,
): Pm25Point | null {
  let best: Pm25Point | null = null;
  let bestGap = Infinity;
  for (const p of zone.cams_pm25) {
    const gap = Math.abs(hoursSinceT0(forecast, p.time) - hour);
    if (gap < bestGap) {
      best = p;
      bestGap = gap;
    }
  }
  return best;
}

/** Colour of a zone at an hour: the category of its hourly PM2.5, the main number. */
export function zoneColourAt(forecast: Forecast, zone: ZoneForecast, hour: number): Rgb {
  const point = zonePointAt(forecast, zone, hour);
  return point ? CATEGORY_COLOURS[point.hourly_category] : NO_DATA_COLOUR;
}

/** Hazier smoke index over a zone at a forecast hour (0 outside the series). */
export function smokeIndexAt(zone: ZoneForecast, hour: number): number {
  return zone.smoke_index?.[Math.round(hour)] ?? 0;
}

/** Whether Hazier has smoke over the zone at that hour: at 850 hPa, so "smoke aloft". */
export function smokeAloft(forecast: Forecast, zone: ZoneForecast, hour: number): boolean {
  return smokeIndexAt(zone, hour) >= forecast.smoke_model.arrival_threshold;
}

/** Dark or white text, whichever reads better on a colour (relative luminance). */
export function textOn([r, g, b]: Rgb): Rgb {
  const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  return lum > 0.55 ? [17, 20, 26] : [255, 255, 255];
}

/** Sources that need a warning: everything that is not ok. Failed ones first. */
export function sourceWarnings(forecast: Forecast): SourceStatus[] {
  const rank = { failed: 0, degraded: 1, ok: 2 } as const;
  return forecast.source_status
    .filter((s) => s.state !== "ok")
    .sort((a, b) => rank[a.state] - rank[b.state]);
}

export const SOURCE_LABELS: Record<string, string> = {
  firms: "Fire detections (NASA FIRMS)",
  winds: "Winds (Open-Meteo)",
  cams: "PM2.5 (CAMS)",
  apims: "Station readings (APIMS)",
};

/** One line for the zone readout: CAMS category and value at a forecast hour. */
export function zoneSummary(forecast: Forecast, zone: ZoneForecast, hour: number): string {
  const p = zonePointAt(forecast, zone, hour);
  if (!p) return `${zone.name}: no CAMS data`;
  return `${zone.name}: ${CATEGORY_LABELS[p.category]}, ${Math.round(p.basis_ugm3)} µg/m³`;
}
