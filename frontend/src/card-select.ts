// Two small primitives shared by the card-list views (Destinations rank list, Alerts list) and
// the trail-draw animation. No DOM template, no network - see card-select.test.ts.
import type L from "leaflet";

import { deselectSize, selectSize } from "./map";
import { state } from "./state";

/**
 * Stale-response guard for an async render that can be re-entered while a previous run of
 * itself is still in flight - startup's head-start timer vs geolocation resolving,
 * tab switches, refreshCurrentView(). `begin()` claims the newest token and returns
 * `isCurrent()`, which reports `false` once a newer run has begun or - when `view` is given -
 * once the user has switched away from that view. See views.ts runDestinations/runAlerts and
 * layers.ts animateTrail.
 */
export function createRunGuard(view?: string): { begin: () => () => boolean } {
  let latest = 0;
  return {
    begin() {
      const token = (latest += 1);
      return () => token === latest && (view === undefined || state.view === view);
    },
  };
}

/**
 * Single-selection manager for a list of region cards paired with map markers. Selecting a
 * card clears `.active` from its siblings in `container`, marks and scrolls the chosen card
 * into view, and reverts the previously selected marker to its score-scaled preview size while
 * growing the new one to its true cell footprint (map.ts selectSize/deselectSize).
 * `selectInitial` is the no-scroll, no-sibling-clear variant for the top result auto-selected
 * on first render.
 */
export function createCardSelection(container: HTMLElement): {
  select: (card: HTMLElement, marker: L.Circle, weight: number) => void;
  selectInitial: (card: HTMLElement, marker: L.Circle, weight: number) => void;
} {
  let selected: { marker: L.Circle; weight: number } | null = null;

  const grow = (marker: L.Circle, weight: number): void => {
    if (selected && selected.marker !== marker) deselectSize(selected.marker, selected.weight);
    selectSize(marker);
    selected = { marker, weight };
  };

  return {
    select(card, marker, weight) {
      container.querySelectorAll(".rank").forEach((el) => el.classList.remove("active"));
      card.classList.add("active");
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      grow(marker, weight);
    },
    selectInitial(card, marker, weight) {
      card.classList.add("active");
      grow(marker, weight);
    },
  };
}
