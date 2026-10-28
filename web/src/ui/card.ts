// The selected zone card: the hourly PM2.5 (main number), the 24 h mean category (official
// style), trend, smoke arrival and "smoke aloft"; tap to expand for the smoke origin shares and
// the 72 h hourly CAMS values.

import { CATEGORY_COLOURS, CATEGORY_LABELS, smokeAloft, zonePointAt } from "../data/derive";
import { formatMYT } from "../data/time";
import { relativeLabel } from "../data/timeline";
import type { Forecast, Pm25Category, ZoneForecast } from "../data/types";
import { etaText, originsToShow, TREND_TEXT } from "../data/zone";
import { $, el, rgb } from "./dom";

let expanded = false;

const ALOFT_NOTE =
  "Hazier's smoke travels at about 1.5 km (850 hPa). Smoke aloft means a plume is over the zone " +
  "at that height; it may or may not reach the ground. The PM2.5 number is CAMS, near the ground.";

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

function chip(category: Pm25Category, verified: boolean, small = false): HTMLElement {
  const out = el("span", `inline-flex items-center gap-1.5 ${small ? "" : "font-medium"}`);
  const dot = el("span", `inline-block rounded-full ${small ? "h-2 w-2" : "h-3 w-3"}`);
  dot.style.background = rgb(CATEGORY_COLOURS[category]);
  out.append(dot, CATEGORY_LABELS[category]);
  if (!verified && !small) out.append(el("span", "text-[10px] font-normal text-amber-300/90", "provisional"));
  return out;
}

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
  summary.append(head);

  if (point) {
    // Main number: the hourly PM2.5, which follows the air as it changes.
    const main = el("div", "mt-1 flex flex-wrap items-baseline gap-x-2");
    main.append(
      el("span", "text-3xl font-bold tabular-nums leading-none", String(Math.round(point.value_ugm3))),
      el("span", "text-sm text-slate-300", "µg/m³ PM2.5, hourly"),
      chip(point.hourly_category, verified),
    );
    // Secondary: the 24 h mean category, the basis of the official API, which lags behind.
    const official = el("div", "mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-slate-400");
    official.append(
      `24 h mean (official style): ${Math.round(point.basis_ugm3)} µg/m³,`,
      chip(point.category, verified, true),
    );
    summary.append(main, official);
  } else {
    summary.append(el("div", "mt-1 text-sm text-slate-300", "No CAMS air quality data for this zone"));
  }

  const smokeLine = el("div", "mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm");
  smokeLine.append(el("span", "text-sky-200", etaText(zone, ageHours)));
  if (smokeAloft(forecast, zone, hour)) {
    const badge = el(
      "span",
      "rounded-full border border-slate-300/70 px-2 py-0.5 text-[11px] font-medium text-slate-100",
      "Smoke aloft",
    );
    badge.title = ALOFT_NOTE;
    smokeLine.append(badge);
  }
  summary.append(smokeLine);
  if (zone.cams_trend) {
    summary.append(
      el("div", "mt-0.5 text-xs text-slate-400", `Next 24 h: ${TREND_TEXT[zone.cams_trend].toLowerCase()}`),
    );
  }

  renderDetail(forecast, zone, hour);
}

function renderDetail(forecast: Forecast, zone: ZoneForecast, hour: number): void {
  const detail = $("card-detail");
  detail.replaceChildren();
  const heading = (text: string, first = false) =>
    el("h3", `${first ? "" : "mt-3 "}text-xs font-semibold uppercase tracking-wide text-slate-400`, text);

  // Where the smoke comes from. Without an arrival (no ETA) the shares only describe trace
  // amounts below the arrival level: they are not shown, so no "source" is suggested.
  detail.append(heading("Smoke origin", true));
  const origins = originsToShow(zone);
  if (origins.length === 0) {
    detail.append(
      el(
        "p",
        "mt-1 text-sm text-slate-300",
        zone.smoke_sources.length
          ? "No smoke arrival expected. Only trace amounts, below the arrival level, may reach this zone."
          : "No smoke from detected fires is expected to reach this zone.",
      ),
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

  // The 72 h hourly CAMS values, the current hour outlined, and the hours of smoke aloft.
  detail.append(heading("PM2.5 over 72 h (hourly)"));
  const points = zone.cams_pm25;
  if (points.length === 0) {
    detail.append(el("p", "mt-1 text-sm text-slate-300", "No CAMS data for this zone."));
    return;
  }
  const top = Math.max(35, ...points.map((p) => p.value_ugm3));
  const current = zonePointAt(forecast, zone, hour);
  const bars = el("div", "mt-1 flex h-14 items-end gap-px");
  for (const p of points) {
    const bar = el("div", "min-w-px flex-1 rounded-t-sm");
    bar.style.height = `${Math.max(4, (p.value_ugm3 / top) * 100)}%`;
    bar.style.background = rgb(CATEGORY_COLOURS[p.hourly_category]);
    bar.title = `${formatMYT(p.time)}: ${Math.round(p.value_ugm3)} µg/m³ (24 h mean ${Math.round(p.basis_ugm3)})`;
    if (p === current) bar.classList.add("outline", "outline-1", "outline-white");
    bars.append(bar);
  }
  // Smoke aloft strip: one cell per forecast hour, grey where Hazier has smoke over the zone.
  const strip = el("div", "mt-0.5 flex h-1.5 gap-px");
  const threshold = forecast.smoke_model.arrival_threshold;
  for (let h = 0; h < points.length; h++) {
    const cell = el("div", "min-w-px flex-1 rounded-sm");
    cell.style.background =
      (zone.smoke_index[h] ?? 0) >= threshold ? "rgb(214 216 224)" : "rgb(255 255 255 / 0.06)";
    strip.append(cell);
  }
  const axis = el("div", "mt-0.5 flex justify-between text-[10px] text-slate-500");
  axis.append(el("span", "", formatMYT(points[0].time)), el("span", "", formatMYT(points[points.length - 1].time)));
  detail.append(bars, strip, axis);

  const highest = points.reduce((a, b) => (b.value_ugm3 > a.value_ugm3 ? b : a));
  detail.append(
    el(
      "p",
      "mt-1 text-sm text-slate-300",
      `Highest hour: ${Math.round(highest.value_ugm3)} µg/m³ (${CATEGORY_LABELS[highest.hourly_category]}), around ${formatMYT(highest.time)}.`,
    ),
    el(
      "p",
      "mt-1 text-[11px] leading-snug text-slate-500",
      "Bars: CAMS hourly PM2.5 at the zone centre, coloured with the Malaysian API limits (which " +
        "officially apply to the 24 h mean, so the hourly colour is an indication). Grey strip: " +
        `hours with smoke aloft. ${ALOFT_NOTE}`,
    ),
  );
}
