// The selected zone card: name, CAMS category, PM2.5, trend and smoke ETA; tap to expand for
// the smoke origin shares and the 72 h CAMS PM2.5 values.

import { CATEGORY_COLOURS, CATEGORY_LABELS, zonePointAt } from "../data/derive";
import { formatMYT } from "../data/time";
import { relativeLabel } from "../data/timeline";
import type { Forecast, ZoneForecast } from "../data/types";
import { etaText, originRows, TREND_TEXT } from "../data/zone";
import { $, el, rgb } from "./dom";

let expanded = false;

/** Wire the expand toggle once. */
export function initCard(): void {
  const toggle = $<HTMLButtonElement>("card-toggle");
  toggle.onclick = () => {
    expanded = !expanded;
    applyExpanded();
  };
}

function applyExpanded(): void {
  $("card-toggle").setAttribute("aria-expanded", String(expanded));
  $("card-detail").hidden = !expanded;
  $("card-chevron").textContent = expanded ? "▴" : "▾";
}

export type CardView = {
  forecast: Forecast;
  zone: ZoneForecast;
  hour: number; // whole forecast hour on the timeline
  ageHours: number;
  verified: boolean; // categories_verified
};

export function renderCard({ forecast, zone, hour, ageHours, verified }: CardView): void {
  $("card").hidden = false;
  applyExpanded();

  const point = zonePointAt(forecast, zone, hour);
  const summary = $("card-summary");
  summary.replaceChildren();

  const head = el("div", "flex items-baseline justify-between gap-2");
  head.append(
    el("span", "text-base font-semibold", zone.name),
    el("span", "text-[11px] text-slate-400", relativeLabel(hour, ageHours, forecast.horizon_hours)),
  );

  const level = el("div", "mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm");
  if (point) {
    const chip = el("span", "inline-flex items-center gap-1.5 font-medium");
    const dot = el("span", "inline-block h-3 w-3 rounded-full");
    dot.style.background = rgb(CATEGORY_COLOURS[point.category]);
    chip.append(dot, CATEGORY_LABELS[point.category]);
    if (!verified) chip.append(el("span", "text-[10px] font-normal text-amber-300/90", "provisional"));
    level.append(chip, el("span", "text-slate-300", `PM2.5 ${Math.round(point.basis_ugm3)} µg/m³`));
  } else {
    level.append(el("span", "text-slate-300", "No CAMS air quality data for this zone"));
  }
  if (zone.cams_trend) level.append(el("span", "text-slate-400", `Trend: ${TREND_TEXT[zone.cams_trend].toLowerCase()}`));

  summary.append(head, level, el("div", "mt-1 text-sm text-sky-200", etaText(zone, ageHours)));

  renderDetail(forecast, zone, hour);
}

function renderDetail(forecast: Forecast, zone: ZoneForecast, hour: number): void {
  const detail = $("card-detail");
  detail.replaceChildren();

  // Where the smoke comes from.
  detail.append(el("h3", "text-xs font-semibold uppercase tracking-wide text-slate-400", "Smoke origin"));
  const origins = originRows(zone.smoke_sources);
  if (origins.length === 0) {
    detail.append(
      el("p", "mt-1 text-sm text-slate-300", "No smoke from detected fires is expected to reach this zone."),
    );
  } else {
    const list = el("ul", "mt-1 space-y-1");
    for (const row of origins) {
      const item = el("li", "text-sm");
      const line = el("div", "flex justify-between gap-2");
      line.append(el("span", "", row.region), el("span", "tabular-nums text-slate-300", `${row.percent}%`));
      const track = el("div", "mt-0.5 h-1.5 rounded bg-white/10");
      const fill = el("div", "h-full rounded bg-sky-400");
      fill.style.width = `${row.percent}%`;
      track.append(fill);
      item.append(line, track);
      list.append(item);
    }
    detail.append(list);
  }

  // The 72 h CAMS values, one bar per CAMS point, the current hour outlined.
  detail.append(
    el("h3", "mt-3 text-xs font-semibold uppercase tracking-wide text-slate-400", "PM2.5 over 72 h"),
  );
  const points = zone.cams_pm25;
  if (points.length === 0) {
    detail.append(el("p", "mt-1 text-sm text-slate-300", "No CAMS data for this zone."));
    return;
  }
  const top = Math.max(35, ...points.map((p) => p.basis_ugm3));
  const current = zonePointAt(forecast, zone, hour);
  const bars = el("div", "mt-1 flex h-14 items-end gap-px");
  for (const p of points) {
    const bar = el("div", "min-w-px flex-1 rounded-t-sm");
    bar.style.height = `${Math.max(4, (p.basis_ugm3 / top) * 100)}%`;
    bar.style.background = rgb(CATEGORY_COLOURS[p.category]);
    bar.title = `${formatMYT(p.time)}: ${Math.round(p.basis_ugm3)} µg/m³, ${CATEGORY_LABELS[p.category]}`;
    if (p === current) bar.classList.add("outline", "outline-1", "outline-white");
    bars.append(bar);
  }
  const axis = el("div", "mt-0.5 flex justify-between text-[10px] text-slate-500");
  axis.append(
    el("span", "", formatMYT(points[0].time)),
    el("span", "", formatMYT(points[points.length - 1].time)),
  );
  detail.append(bars, axis);
  const peak = zone.cams_peak;
  if (peak) {
    detail.append(
      el(
        "p",
        "mt-1 text-sm text-slate-300",
        `Highest: ${Math.round(peak.basis_ugm3)} µg/m³ (${CATEGORY_LABELS[peak.category]}), around ${formatMYT(peak.time)}.`,
      ),
    );
  }
  detail.append(
    el(
      "p",
      "mt-1 text-[11px] leading-snug text-slate-500",
      `Values are CAMS ${forecast.pm25_baseline?.averaging_hours ?? 24} h means at the zone centre, one bar every ${forecast.pm25_baseline?.step_hours ?? 3} h. Trend compares the next 24 h with the last 24 h.`,
    ),
  );
}
