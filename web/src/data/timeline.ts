// Timeline logic: tick marks in Malaysia time, the "now" position, playback stepping.

const MYT_OFFSET_HOURS = 8; // Malaysia has no daylight saving time

const weekday = new Intl.DateTimeFormat("en-MY", { timeZone: "Asia/Kuala_Lumpur", weekday: "short" });

/** Forecast hours advanced per second of playback: 72 h in 24 s. */
export const PLAY_HOURS_PER_SECOND = 3;

/** Closest a day label may be to the "now" label, in forecast hours, so the text does not overlap. */
const MIN_LABEL_GAP_HOURS = 9;

export type Tick = {
  hour: number;
  kind: "now" | "day" | "minor";
  label: string; // empty for minor ticks
};

/** The current time as a forecast hour: how old the forecast is, kept inside the slider. */
export function nowHour(ageHours: number, horizon: number): number {
  return Math.min(horizon, Math.max(0, ageHours));
}

/** Whether "now" falls inside the forecast. A forecast older than its horizon has no "now" on it. */
export const hasNow = (ageHours: number, horizon: number): boolean => ageHours <= horizon;

/**
 * Ticks for the slider: "Now" (when it is inside the forecast), a labelled tick at each midnight
 * in Malaysia time, and an unlabelled tick every 6 h. Day labels that would crowd "Now" are
 * left out.
 */
export function timelineTicks(generatedAt: string, horizon: number, ageHours: number): Tick[] {
  const withNow = hasNow(ageHours, horizon);
  const now = nowHour(ageHours, horizon);
  const t0 = Date.parse(generatedAt);
  const mytClockHours = (t0 / 3_600_000 + MYT_OFFSET_HOURS) % 24;
  const firstMidnight = (24 - mytClockHours) % 24;
  const ticks: Tick[] = withNow ? [{ hour: now, kind: "now", label: "Now" }] : [];
  const nearNow = (h: number, gap: number) => withNow && Math.abs(h - now) < gap;
  const midnights = new Set<number>();
  for (let h = firstMidnight; h <= horizon + 1e-9; h += 24) {
    midnights.add(h);
    if (nearNow(h, MIN_LABEL_GAP_HOURS)) continue;
    ticks.push({ hour: h, kind: "day", label: weekday.format(new Date(t0 + h * 3_600_000)) });
  }
  for (let h = 0; h <= horizon; h += 6) {
    const clash = [...midnights].some((m) => Math.abs(m - h) < 0.5) || nearNow(h, 0.5);
    if (!clash) ticks.push({ hour: h, kind: "minor", label: "" });
  }
  return ticks;
}

/** Playback: move the forecast hour on by dt seconds, wrapping back to 0 after the horizon. */
export function advance(
  hour: number,
  dtSeconds: number,
  horizon: number,
  hoursPerSecond = PLAY_HOURS_PER_SECOND,
): number {
  const next = hour + dtSeconds * hoursPerSecond;
  return next > horizon ? next - horizon : next;
}

/**
 * Text for the slider's position relative to now: "Now", "In 18 h" or "6 h ago". A forecast
 * older than its horizon has no "now" on it, so the offset from the forecast start is given.
 */
export function relativeLabel(hour: number, ageHours: number, horizon: number): string {
  if (!hasNow(ageHours, horizon)) return Math.round(hour) === 0 ? "Forecast start" : `+${Math.round(hour)} h`;
  const delta = Math.round(hour) - Math.round(nowHour(ageHours, horizon));
  if (delta === 0) return "Now";
  return delta > 0 ? `In ${delta} h` : `${-delta} h ago`;
}
