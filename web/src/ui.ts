// Small DOM helpers for the temporary part A interface: banners, hour control, legend, footer.
// All text goes through textContent, never innerHTML: some of it comes from the JSON.

import {
  CATEGORY_COLOURS,
  CATEGORY_LABELS,
  CATEGORY_ORDER,
  SOURCE_LABELS,
  sourceWarnings,
} from "./data/derive";
import { ageHours, STALE_AFTER_HOURS } from "./data/load";
import { formatMYT } from "./data/time";
import type { Forecast } from "./data/types";

const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} is missing from index.html`);
  return el as T;
};

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

type Tone = "warn" | "bad" | "info";
const TONES: Record<Tone, string> = {
  warn: "border-amber-400/50 bg-amber-950/85 text-amber-100",
  bad: "border-red-400/50 bg-red-950/85 text-red-100",
  info: "border-sky-400/40 bg-slate-900/85 text-sky-100",
};

export function showBanners(forecast: Forecast, isSample: boolean, now = new Date()): void {
  const box = $("banners");
  box.replaceChildren();
  const add = (tone: Tone, text: string) =>
    box.append(
      el(
        "div",
        `max-w-[min(92vw,34rem)] rounded-md border px-2.5 py-1.5 text-xs shadow ${TONES[tone]}`,
        text,
      ),
    );

  if (isSample) {
    add("info", "Sample data, not a live forecast.");
  } else if (ageHours(forecast, now) > STALE_AFTER_HOURS) {
    const hours = Math.floor(ageHours(forecast, now));
    add(
      "warn",
      `This forecast is ${hours} h old (made ${formatMYT(forecast.generated_at)}). ` +
        "The latest update did not arrive, so it may be out of date.",
    );
  }
  for (const s of sourceWarnings(forecast)) {
    const name = SOURCE_LABELS[s.name] ?? s.name;
    const what = s.state === "failed" ? "unavailable" : "partly unavailable";
    add(s.state === "failed" ? "bad" : "warn", `${name} ${what}${s.detail ? `: ${s.detail}` : ""}.`);
  }
}

export function showError(message: string, onRetry: () => void): void {
  $("loading").hidden = true;
  $("hour-control").hidden = true;
  $("error-message").textContent = message;
  const panel = $("error");
  panel.hidden = false;
  const button = $<HTMLButtonElement>("error-retry");
  button.onclick = () => {
    panel.hidden = true;
    $("loading").hidden = false;
    onRetry();
  };
}

export function showLoading(): void {
  $("loading").hidden = false;
  $("error").hidden = true;
}

export function showControl(forecast: Forecast, onHour: (hour: number) => void): HTMLInputElement {
  $("loading").hidden = true;
  $("hour-control").hidden = false;
  const slider = $<HTMLInputElement>("hour");
  slider.max = String(forecast.horizon_hours);
  slider.value = "0";
  slider.oninput = () => onHour(Number(slider.value));
  return slider;
}

export function setHourLabel(forecast: Forecast, hour: number): void {
  const time = new Date(Date.parse(forecast.generated_at) + hour * 3_600_000).toISOString();
  $("hour-label").textContent = `${hour === 0 ? "Now" : `+${hour} h`} · ${formatMYT(time)}`;
}

export function setZoneReadout(text: string): void {
  $("zone-readout").textContent = text;
}

export function showLegend(forecast: Forecast, verified: boolean): void {
  const list = $("legend");
  list.replaceChildren();
  for (const category of CATEGORY_ORDER) {
    const [r, g, b] = CATEGORY_COLOURS[category];
    const item = el("li", "flex items-center gap-1");
    const dot = el("span", "inline-block h-2.5 w-2.5 rounded-full");
    dot.style.background = `rgb(${r}, ${g}, ${b})`;
    item.append(dot, el("span", "", CATEGORY_LABELS[category]));
    list.append(item);
  }
  const bounds = forecast.pm25_baseline?.category_upper_bounds;
  const note = verified
    ? "PM2.5 as a 24 h mean, from CAMS."
    : "PM2.5 as a 24 h mean, from CAMS. Category limits are provisional.";
  list.append(el("li", "basis-full text-slate-400", bounds ? note : "PM2.5 from CAMS."));
}

function link(label: string, href: string): HTMLAnchorElement {
  const a = el("a", "underline decoration-slate-600 underline-offset-2 hover:text-slate-200", label);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

/** Credits for every source, and the provisional note while the categories are unverified. */
export function showFooter(forecast: Forecast | null): void {
  const footer = $("footer");
  footer.replaceChildren();
  const line = (...parts: (string | Node)[]) => {
    const p = el("p", "");
    p.append(...parts);
    footer.append(p);
  };
  if (forecast) {
    const verified = forecast.pm25_baseline?.categories_verified ?? true;
    line(
      `Forecast made ${formatMYT(forecast.generated_at)}. Updated every 6 hours. `,
      verified
        ? ""
        : "Air quality categories (Malaysian API) are provisional: their limits are not yet verified against the official source.",
    );
  }
  const credits: (string | Node)[] = [
    "Basemap © ",
    link("CARTO", "https://carto.com/attributions"),
    ", © ",
    link("OpenStreetMap contributors", "https://www.openstreetmap.org/copyright"),
    ". Fires: ",
    link("NASA FIRMS", "https://firms.modaps.eosdis.nasa.gov/"),
    ". ",
  ];
  for (const a of forecast?.attributions ?? []) {
    credits.push(link(a.name, a.url), `: ${a.text} `);
  }
  line(...credits);
}
