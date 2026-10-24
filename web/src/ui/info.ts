// The info panel behind the "i" button: how to read the map, limitations, credits.

import { formatMYT } from "../data/time";
import type { Forecast } from "../data/types";
import { $, el, link } from "./dom";

/** Toggle behaviour, wired once. */
export function initInfo(): void {
  const button = $<HTMLButtonElement>("info-button");
  const panel = $("info");
  const set = (open: boolean) => {
    panel.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
  };
  button.onclick = () => set(Boolean(panel.hidden));
  $("info-close").onclick = () => {
    set(false);
    button.focus();
  };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) set(false);
  });
}

/** Fill the panel. Without a forecast (not loaded yet) it still carries the basemap credits. */
export function showInfo(forecast: Forecast | null): void {
  const body = $("info-body");
  body.replaceChildren();
  const section = (title: string, ...parts: (string | Node)[]) => {
    body.append(el("h3", "mt-3 text-xs font-semibold uppercase tracking-wide text-slate-400 first:mt-0", title));
    const p = el("p", "mt-1");
    p.append(...parts);
    body.append(p);
  };

  if (forecast) {
    const verified = forecast.pm25_baseline?.categories_verified ?? true;
    section(
      "This forecast",
      `Made ${formatMYT(forecast.generated_at)}. Updated every 6 hours. Times are Malaysia time (MYT).`,
    );
    section(
      "How to read the map",
      "Colours show PM2.5 from the CAMS model, as a 24 h mean, in the Malaysian air quality categories. " +
        "Glowing points are detected fires, sized by their intensity. White streaks are Hazier's estimate of where the smoke from those fires travels, " +
        "and the arrival time on the zone card comes from them.",
    );
    if (!verified) {
      section(
        "Provisional categories",
        "The Malaysian API category limits used here are not yet verified against the official source, so a category name may change once they are checked.",
      );
    }
  }
  section(
    "Limitations",
    "CAMS is a coarse model (cells of about 45 km), so it shows regional haze, not your street. " +
      "Smoke arrival times are estimates from winds at one height and can be off by several hours. " +
      "Each zone is an area of about 50 km radius: the forecast does not tell neighbourhoods apart.",
  );

  const credits: (string | Node)[] = [
    "Basemap © ",
    link("CARTO", "https://carto.com/attributions"),
    ", © ",
    link("OpenStreetMap contributors", "https://www.openstreetmap.org/copyright"),
    ". Fires: ",
    link("NASA FIRMS", "https://firms.modaps.eosdis.nasa.gov/"),
    ". ",
  ];
  for (const a of forecast?.attributions ?? []) credits.push(link(a.name, a.url), `: ${a.text} `);
  section("Credits", ...credits);
}
