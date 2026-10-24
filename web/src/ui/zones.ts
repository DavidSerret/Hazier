// Desktop zone list: a plain list in the left panel to switch the selected zone.

import { CATEGORY_LABELS, NO_DATA_COLOUR, zoneColourAt, zonePointAt } from "../data/derive";
import type { Forecast } from "../data/types";
import { $, el, rgb } from "./dom";

export function renderZoneList(
  forecast: Forecast,
  selectedId: string | null,
  hour: number,
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
    const dot = el("span", "inline-block h-3 w-3 shrink-0 rounded-full");
    dot.style.background = rgb(point ? zoneColourAt(forecast, zone, hour) : NO_DATA_COLOUR);
    button.append(
      dot,
      el("span", "flex-1", zone.name),
      el("span", "text-xs text-slate-400", point ? CATEGORY_LABELS[point.category] : "no data"),
    );
    button.onclick = () => onSelect(zone.id);
    const item = el("li", "");
    item.append(button);
    list.append(item);
  }
  box.append(list);
  if (focused >= 0) buttons()[focused]?.focus({ preventScroll: true });
}
