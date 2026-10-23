// Data is UTC; the UI always shows Malaysia time (MYT, UTC+8).

const MYT = "Asia/Kuala_Lumpur";

const fmt = new Intl.DateTimeFormat("en-MY", {
  timeZone: MYT,
  weekday: "short",
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** Format a UTC ISO timestamp as Malaysia local time, for example "Mon, 14 Sept, 14:00 MYT". */
export function formatMYT(utcIso: string): string {
  return `${fmt.format(new Date(utcIso))} MYT`;
}

/** UTC ISO timestamp of `hours` after `generatedAt`. */
export function addHours(generatedAt: string, hours: number): string {
  return new Date(new Date(generatedAt).getTime() + hours * 3_600_000).toISOString();
}
