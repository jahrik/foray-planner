// The route shortlist (issue #301): "+ Plan" on a result card adds that region to an ordered
// list of waypoints; a floating action bar appears once the list is non-empty and, when tapped,
// opens the plan form seeded with those regions as required stops (GET /api/plan ?waypoints=).
// State only - no DOM template here; main.ts wires the action bar's button, views.ts re-renders
// cards' plan buttons through card-dom's syncPlan.

import { qs } from "../state";

const ids: string[] = [];
let onChange: () => void = () => {};

export function setShortlistChangeHook(hook: () => void): void {
  onChange = hook;
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
  else ids.splice(at, 1);
  renderActionBar();
  onChange();
}

export function clearShortlist(): void {
  ids.length = 0;
  renderActionBar();
  onChange();
}

// Shows/hides #route-bar and keeps its count label current. Called on every list change.
export function renderActionBar(): void {
  const bar = document.getElementById("route-bar");
  if (!bar) return;
  bar.hidden = ids.length === 0;
  const label = bar.querySelector<HTMLElement>(".route-bar-count");
  if (label) label.textContent = ids.length === 1 ? "1 spot" : `${ids.length} spots`;
}

export function initRouteBar(onPlan: () => void): void {
  const bar = qs("#route-bar");
  qs<HTMLButtonElement>(".route-bar-go", bar).onclick = onPlan;
  qs<HTMLButtonElement>(".route-bar-clear", bar).onclick = () => clearShortlist();
  renderActionBar();
}
