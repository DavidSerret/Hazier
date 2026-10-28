// The fire popup: region, FRP and first detection of a tapped fire cluster.

import { formatMYT } from "../data/time";
import type { SourceCluster } from "../data/types";
import { $, el } from "./dom";

let shownAt = 0;

export function showFire(cluster: SourceCluster, x: number, y: number): void {
  const box = $("fire-popup");
  box.replaceChildren(
    el("div", "font-semibold", `Fire cluster, ${cluster.region || "unknown region"}`),
    el("div", "", `FRP ${Math.round(cluster.frp_mw).toLocaleString("en-MY")} MW (sum of ${cluster.hotspot_count} detections, last 48 h)`),
    el("div", "", cluster.first_at ? `First detected ${formatMYT(cluster.first_at)}` : "First detection unknown"),
    el("div", "", `Latest ${formatMYT(cluster.latest_at)}`),
  );
  box.hidden = false;
  // Keep it on screen: to the right of the tap, or to the left near the right edge.
  const width = box.offsetWidth;
  const left = x + 12 + width > window.innerWidth ? x - 12 - width : x + 12;
  box.style.left = `${Math.max(8, left)}px`;
  box.style.top = `${Math.max(8, y - box.offsetHeight / 2)}px`;
  shownAt = performance.now();
}

/** Hide, unless the popup was opened by the very click that is being handled now. */
export function hideFire(fromMapClick = false): void {
  if (fromMapClick && performance.now() - shownAt < 300) return;
  $("fire-popup").hidden = true;
}
