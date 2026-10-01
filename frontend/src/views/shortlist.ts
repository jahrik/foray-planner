// The route shortlist (issue #301): "+ Plan" on a result card adds that region to an ordered
// list of waypoints; a floating action bar appears once the list is non-empty and, when tapped,
// opens the plan form seeded with those regions as required stops (GET /api/plan ?waypoints=).
// State only - no DOM template here; main.ts wires the action bar's button, views.ts re-renders
// cards' plan buttons through card-dom's syncPlan.
//
// Each shortlisted region can also carry a pin (issue #311): the specific campground or trail
// the user picked in its Details view as the stop's exact point. Pins are sent alongside the
// waypoints as `pin=<region>:<kind>:<id>`; the server resolves the coordinates itself.

import { qs, state } from "../state";

export interface PinRef {
  kind: "camp" | "trail";
  id: string;
  name: string;
}

const ids: string[] = [];
const pins = new Map<string, PinRef>();
// Fired when the shortlist changes in a way that isn't already reflected on a card - i.e. a
// bulk clear. A single toggle comes from one card's own "+ Plan" button, which re-syncs itself
// (card-dom syncPlan), so it doesn't need the hook.
let onBulkChange: () => void = () => {};

export function setShortlistChangeHook(hook: () => void): void {
  onBulkChange = hook;
}

export function inShortlist(regionId: string): boolean {
  return ids.includes(regionId);
}

export function shortlistIds(): string[] {
  return [...ids];
}

export function toggleShortlist(regionId: string): void {
  const at = ids.indexOf(regionId);
  if (at === -1) ids.push(regionId);
  else {
    ids.splice(at, 1);
    pins.delete(regionId);
  }
  renderActionBar();
}

export function pinFor(regionId: string): PinRef | undefined {
  return pins.get(regionId);
}

/** Pin a feature as `regionId`'s stop point - shortlisting the region if it isn't yet - or
 * unpin it with `null` (the region stays shortlisted, back on its centroid / nearest camp). */
export function setPin(regionId: string, pin: PinRef | null): void {
  if (pin === null) {
    pins.delete(regionId);
    return;
  }
  if (!ids.includes(regionId)) ids.push(regionId);
  pins.set(regionId, pin);
  renderActionBar();
}

/** The `/api/plan` `pin` query values for the shortlisted regions that carry one. */
export function pinParams(): string[] {
  return ids.flatMap((regionId) => {
    const pin = pins.get(regionId);
    return pin ? [`${regionId}:${pin.kind}:${pin.id}`] : [];
  });
}

export function clearShortlist(): void {
  if (ids.length === 0) return;
  ids.length = 0;
  pins.clear();
  renderActionBar();
  onBulkChange();
}

// Keeps #route-bar in sync: visible only on the Destinations flow (redundant inside Plan mode),
// its label reflecting the shortlist. Empty list still shows the bar - "Plan a route" then means
// auto-pick. Called on every list change and on every view switch.
export function renderActionBar(): void {
  const bar = document.getElementById("route-bar");
  if (!bar) return;
  bar.hidden = state.view !== "destinations";
  const empty = ids.length === 0;
  const label = bar.querySelector<HTMLElement>(".route-bar-count");
  if (label) {
    label.textContent = empty
      ? "Plan a road trip"
      : ids.length === 1
        ? "1 spot picked"
        : `${ids.length} spots picked`;
  }
  const clear = bar.querySelector<HTMLButtonElement>(".route-bar-clear");
  if (clear) clear.hidden = empty;
}

export function initRouteBar(onPlan: () => void): void {
  const bar = qs("#route-bar");
  qs<HTMLButtonElement>(".route-bar-go", bar).onclick = onPlan;
  qs<HTMLButtonElement>(".route-bar-clear", bar).onclick = () => clearShortlist();
  renderActionBar();
}
