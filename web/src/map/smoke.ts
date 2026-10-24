// Smoke particles: where each display trajectory is at a forecast hour.

import type { Trajectory } from "../data/types";

export type Head = { position: [number, number]; weight: number };

/**
 * Position [lon, lat] along a path at a forecast hour, interpolated linearly between the two
 * surrounding points. Null before the path starts and after it ends: the puff has not been
 * released yet, or has left the domain.
 */
export function positionAt(
  path: [number, number][],
  hours: number[],
  hour: number,
): [number, number] | null {
  const n = hours.length;
  if (n === 0 || hour < hours[0] || hour > hours[n - 1]) return null;
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (hours[mid] <= hour) lo = mid;
    else hi = mid;
  }
  if (n === 1) return path[0];
  const span = hours[hi] - hours[lo];
  const t = span > 0 ? (hour - hours[lo]) / span : 0;
  return [
    path[lo][0] + (path[hi][0] - path[lo][0]) * t,
    path[lo][1] + (path[hi][1] - path[lo][1]) * t,
  ];
}

/** The particles in the air at a forecast hour. */
export function headsAt(trajectories: Trajectory[], hour: number): Head[] {
  const heads: Head[] = [];
  for (const t of trajectories) {
    const position = positionAt(t.path, t.hours, hour);
    if (position) heads.push({ position, weight: t.weight });
  }
  return heads;
}
