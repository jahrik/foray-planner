import type { RegionScore } from "../api/types";
import type { Sort } from "../state";

// Reorders the /api/destinations payload for the active sort (issue #301). "best" keeps the
// server's scored order; "active" floats regions with recent observations (by recent count,
// then score); "nearest" is straight distance. Always returns a new array - never mutates the
// cached payload, which the km/mi repaint path reuses.
export function sortRegions(regions: RegionScore[], sort: Sort): RegionScore[] {
  const ordered = [...regions];
  if (sort === "nearest") {
    ordered.sort((left, right) => left.distance_km - right.distance_km);
  } else if (sort === "active") {
    ordered.sort(
      (left, right) => right.recent_count - left.recent_count || right.score_norm - left.score_norm,
    );
  }
  return ordered;
}

export const SORT_LABEL: Record<Sort, string> = {
  best: "Best overall",
  active: "Active now",
  nearest: "Nearest",
};
