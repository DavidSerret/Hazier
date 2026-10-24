// Plain-words text for the zone card: smoke ETA, trend and origin shares.

import type { SourceShare, Trend, ZoneForecast } from "./types";

/**
 * The smoke ETA in plain words, counted from now. The pipeline counts hours from generated_at
 * (t0), and a forecast can be a few hours old, so the age is taken off: an ETA of 12 h on a
 * forecast made 5 h ago is 7 h from now.
 */
export function etaText(zone: ZoneForecast, ageHours = 0): string {
  const eta = zone.smoke_eta_hours;
  if (eta === null) return "No smoke expected in the next 72 h";
  const age = Math.max(0, ageHours);
  if (eta - age <= 0) return "Smoke already present";
  const lo = Math.max(1, Math.round((zone.smoke_eta_earliest_hours ?? eta) - age));
  const latest = zone.smoke_eta_latest_hours;
  if (latest === null) return `Smoke expected in about ${lo} h, possibly later`;
  const hi = Math.round(latest - age);
  return hi > lo ? `Smoke expected in ${lo} to ${hi} h` : `Smoke expected in about ${lo} h`;
}

export const TREND_TEXT: Record<Trend, string> = {
  improving: "Getting better",
  stable: "Steady",
  worsening: "Getting worse",
};

export type OriginRow = { region: string; percent: number };

/**
 * Origin shares as whole percentages that add up to 100 (largest remainder rounding). Shares
 * under 1 % are folded into one "Other" row so the list stays short.
 */
export function originRows(sources: SourceShare[]): OriginRow[] {
  const big = sources.filter((s) => s.share >= 0.01);
  if (big.length === 0) return [];
  const rest = sources.filter((s) => s.share < 0.01).reduce((sum, s) => sum + s.share, 0);
  const rows = big.map((s) => ({ region: s.region, exact: s.share * 100 }));
  if (rest > 0) rows.push({ region: "Other", exact: rest * 100 });
  const total = rows.reduce((sum, r) => sum + r.exact, 0);
  const scaled = rows.map((r) => ({ region: r.region, exact: (r.exact / total) * 100 }));
  const floors = scaled.map((r) => Math.floor(r.exact));
  let left = 100 - floors.reduce((sum, f) => sum + f, 0);
  const byRemainder = scaled
    .map((r, i) => ({ i, rem: r.exact - floors[i] }))
    .sort((a, b) => b.rem - a.rem);
  for (const { i } of byRemainder) {
    if (left <= 0) break;
    floors[i] += 1;
    left -= 1;
  }
  return scaled.map((r, i) => ({ region: r.region, percent: floors[i] }));
}
