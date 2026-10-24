// Warnings (sample data, stale forecast, degraded or failed sources) and the load error panel.

import { SOURCE_LABELS, sourceWarnings } from "../data/derive";
import { ageHours, STALE_AFTER_HOURS } from "../data/load";
import { formatMYT } from "../data/time";
import type { Forecast } from "../data/types";
import { $, el } from "./dom";

type Tone = "warn" | "bad" | "info";
const TONES: Record<Tone, string> = {
  warn: "border-amber-400/50 bg-amber-950/90 text-amber-100",
  bad: "border-red-400/50 bg-red-950/90 text-red-100",
  info: "border-sky-400/40 bg-slate-900/90 text-sky-100",
};

export function showBanners(forecast: Forecast, isSample: boolean, now = new Date()): void {
  const box = $("banners");
  box.replaceChildren();
  const add = (tone: Tone, text: string) =>
    box.append(
      el("div", `pointer-events-auto rounded-md border px-2.5 py-1.5 text-xs shadow ${TONES[tone]}`, text),
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

export function showLoading(): void {
  $("loading").hidden = false;
  $("error").hidden = true;
}

export function showLoaded(): void {
  $("loading").hidden = true;
  $("error").hidden = true;
}

export function showError(message: string, onRetry: () => void): void {
  $("loading").hidden = true;
  $("timeline").hidden = true;
  $("card").hidden = true;
  $("banners").replaceChildren();
  $("zones").replaceChildren();
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
