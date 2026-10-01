// Hover list for a precise-observation cluster badge: the observations folded into the badge,
// newest first, each linking to its iNaturalist page. Built from DOM nodes (textContent, never
// innerHTML) for the same reason as popup.ts - names/dates come from an external API. `obs.uri`
// is server-constructed (a fixed iNaturalist observation URL). No state - see
// cluster-popup.test.ts.

import type { PreciseObservation } from "../api/types";
import { displayName } from "../state";

/** Rows shown before the list collapses into a "+N more" line - a dense cluster can hold
 * hundreds of pins, and the hover card should stay glanceable. */
export const CLUSTER_LIST_LIMIT = 15;

// ISO dates (YYYY-MM-DD) sort lexically; undated observations go last.
function newestFirst(left: PreciseObservation, right: PreciseObservation): number {
  return (right.observed_on ?? "").localeCompare(left.observed_on ?? "");
}

/** `onZoom`, when given, adds a "Zoom in" button - the touch path, where tapping the badge
 * opens this list instead of zooming, so the list has to offer the zoom itself. */
export function buildClusterList(
  observations: PreciseObservation[],
  { limit = CLUSTER_LIST_LIMIT, onZoom }: { limit?: number; onZoom?: () => void } = {},
): HTMLElement {
  const root = document.createElement("div");
  root.className = "cluster-list";

  const heading = document.createElement("b");
  heading.textContent = `${observations.length} observation${observations.length === 1 ? "" : "s"}`;
  root.append(heading);

  const list = document.createElement("ul");
  const sorted = [...observations].sort(newestFirst);
  for (const obs of sorted.slice(0, limit)) {
    const item = document.createElement("li");
    const name = displayName(obs);
    if (obs.uri) {
      const anchor = document.createElement("a");
      anchor.href = obs.uri;
      anchor.target = "_blank";
      anchor.rel = "noopener";
      anchor.textContent = name;
      item.append(anchor);
    } else {
      item.append(document.createTextNode(name));
    }
    if (obs.observed_on) {
      const date = document.createElement("span");
      date.className = "cluster-list-date";
      date.textContent = obs.observed_on;
      item.append(date);
    }
    list.append(item);
  }
  root.append(list);

  const hidden = sorted.length - limit;
  if (hidden > 0) {
    const more = document.createElement("div");
    more.className = "cluster-list-more";
    more.textContent = `+${hidden} more - zoom in to see them`;
    root.append(more);
  }
  if (onZoom) {
    const zoom = document.createElement("button");
    zoom.type = "button";
    zoom.className = "cluster-list-zoom";
    zoom.textContent = "Zoom in";
    zoom.addEventListener("click", onZoom);
    root.append(zoom);
  }
  return root;
}
