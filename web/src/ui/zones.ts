// Desktop zone list: hourly PM2.5, category, trend and smoke arrival, to switch the selected zone.

import {
  CATEGORY_LABELS,
  NO_DATA_COLOUR,
  smokeAloft,
  textOn,
  zoneColourAt,
  zonePointAt,
} from "../data/derive";
import type { Forecast, Trend, ZoneForecast } from "../data/types";
import { $, el, rgb } from "./dom";

const TREND_ARROW: Record<Trend, string> = { improving: "↓", stable: "→", worsening: "↑" };
const TREND_WORD: Record<Trend, string> = {
  improving: "getting better",
  stable: "steady",
  worsening: "getting worse",
};

/** Short smoke arrival for the list, counted from now. */
export function shortEta(zone: ZoneForecast, ageHours: number): string {
  const eta = zone.smoke_eta_hours;
  if (eta === null) return "no smoke";
  const left = eta - Math.max(0, ageHours);
  return left <= 0 ? "smoke now" : `smoke in ${Math.max(1, Math.round(left))} h`;
}

export function renderZoneList(
  forecast: Forecast,
  selectedId: string | null,
  hour: number,
  ageHours: number,
  onSelect: (zoneId: string) => void,
): void {
  const box = $("zones");
  // The list is rebuilt on every hour step: keep keyboard focus on the same row.
  const buttons = () => [...box.querySelectorAll("button")];
  const focused = buttons().indexOf(document.activeElement as HTMLButtonElement);
  box.replaceChildren();
  box.append(el("h2", "mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400", "Zones"));
  const list = el("ul", "space-y-0.5");
  for (const zone of forecast.zones) {
    const point = zonePointAt(forecast, zone, hour);
    const selected = zone.id === selectedId;
    const button = el(
      "button",
      `flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-white/10 ${selected ? "bg-white/15" : ""}`,
    );
    button.type = "button";
    if (selected) button.setAttribute("aria-current", "true");
    const colour = point ? zoneColourAt(forecast, zone, hour) : NO_DATA_COLOUR;
    const badge = el(
      "span",
      "inline-flex h-7 w-9 shrink-0 items-center justify-center rounded-md text-xs font-bold tabular-nums",
      point ? String(Math.round(point.value_ugm3)) : "–",
    );
    badge.style.background = rgb(colour);
    badge.style.color = rgb(textOn(colour));
    if (smokeAloft(forecast, zone, hour)) badge.style.boxShadow = "0 0 0 2px rgb(214 216 224)";

    const text = el("span", "min-w-0 flex-1");
    text.append(el("span", "block truncate", zone.name));
    const sub = point
      ? `${CATEGORY_LABELS[point.hourly_category]}${zone.cams_trend ? ` ${TREND_ARROW[zone.cams_trend]}` : ""}`
      : "no data";
    text.append(el("span", "block text-xs text-slate-400", sub));
    if (zone.cams_trend) text.title = `Next 24 h: ${TREND_WORD[zone.cams_trend]}`;

    button.append(badge, text, el("span", "shrink-0 text-xs text-sky-200/90", shortEta(zone, ageHours)));
    button.onclick = () => onSelect(zone.id);
    const item = el("li", "");
    item.append(button);
    list.append(item);
  }
  box.append(list);
  if (focused >= 0) buttons()[focused]?.focus({ preventScroll: true });
}
