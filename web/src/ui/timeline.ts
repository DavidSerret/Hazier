// The timeline at the bottom: play/pause, slider with day and "Now" ticks, MYT label, legend.

import { CATEGORY_COLOURS, CATEGORY_LABELS, CATEGORY_ORDER } from "../data/derive";
import { formatMYT } from "../data/time";
import { relativeLabel, timelineTicks } from "../data/timeline";
import type { Forecast } from "../data/types";
import { $, el, rgb } from "./dom";

export type TimelineHandlers = {
  /** The user touched the timeline (press, key, drag): playback should pause. */
  onInteract: () => void;
  onScrub: (hour: number) => void;
  onTogglePlay: () => void;
};

// Static markup, no data in it.
const PLAY_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
const PAUSE_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>';

export function showTimeline(
  forecast: Forecast,
  ageHours: number,
  handlers: TimelineHandlers,
): void {
  $("timeline").hidden = false;
  const slider = $<HTMLInputElement>("hour");
  slider.max = String(forecast.horizon_hours);
  slider.value = "0";
  slider.oninput = () => {
    handlers.onInteract();
    handlers.onScrub(Number(slider.value));
  };
  slider.onpointerdown = () => handlers.onInteract();
  slider.onkeydown = () => handlers.onInteract();
  $("play").onclick = () => handlers.onTogglePlay();
  setPlaying(false);
  renderTicks(forecast, ageHours);
}

function renderTicks(forecast: Forecast, ageHours: number): void {
  const box = $("ticks");
  box.replaceChildren();
  const horizon = forecast.horizon_hours;
  for (const tick of timelineTicks(forecast.generated_at, horizon, ageHours)) {
    const node = el("div", "absolute top-0 -translate-x-1/2 text-center");
    node.style.left = `${(tick.hour / horizon) * 100}%`;
    const mark = el(
      "div",
      tick.kind === "now"
        ? "mx-auto h-3 w-0.5 bg-sky-400"
        : tick.kind === "day"
          ? "mx-auto h-2.5 w-px bg-slate-400"
          : "mx-auto h-1.5 w-px bg-slate-600",
    );
    node.append(mark);
    if (tick.label) {
      node.append(
        el(
          "div",
          tick.kind === "now" ? "text-[11px] font-medium text-sky-300" : "text-[11px] text-slate-400",
          tick.label,
        ),
      );
    }
    box.append(node);
  }
}

/** Move the slider and the label to a forecast hour (whole hours on the slider). */
export function setTimelineHour(forecast: Forecast, hour: number, ageHours: number): void {
  const step = Math.round(hour);
  const slider = $<HTMLInputElement>("hour");
  if (slider.value !== String(step)) slider.value = String(step);
  const time = new Date(Date.parse(forecast.generated_at) + step * 3_600_000).toISOString();
  const rel = relativeLabel(step, ageHours, forecast.horizon_hours);
  $("hour-label").textContent = `${rel} · ${formatMYT(time)}`;
  slider.setAttribute("aria-valuetext", `${rel}, ${formatMYT(time)}`);
}

export function setPlaying(playing: boolean): void {
  const button = $<HTMLButtonElement>("play");
  button.innerHTML = playing ? PAUSE_ICON : PLAY_ICON;
  button.setAttribute("aria-label", playing ? "Pause" : "Play");
}

export function showLegend(): void {
  const list = $("legend");
  list.replaceChildren();
  for (const category of CATEGORY_ORDER) {
    const item = el("li", "flex items-center gap-1");
    const dot = el("span", "inline-block h-2.5 w-2.5 rounded-full");
    dot.style.background = rgb(CATEGORY_COLOURS[category]);
    item.append(dot, el("span", "", CATEGORY_LABELS[category]));
    list.append(item);
  }
}
