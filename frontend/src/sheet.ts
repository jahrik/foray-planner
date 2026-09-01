import { map } from "./map";
import { qs } from "./state";

// Mobile bottom sheet (issue #229) - a Google/Apple Maps-style draggable sheet over a
// full-screen map. Vanilla TS, Pointer Events, hand-rolled snap: there's no animation lib in
// this repo. Only active at the mobile breakpoint; on desktop #sheet is `display: contents`
// and this module is inert (see style.css).

export type Detent = "collapsed" | "half" | "full";

// Sheet's top edge as a fraction of viewport height, per detent. Bigger fraction = the sheet
// sits lower and covers less; `collapsed` leaves only the handle + tab bar peeking.
const DETENT_TOP: Record<Detent, number> = { collapsed: 0.82, half: 0.52, full: 0.08 };
const ORDER: Detent[] = ["collapsed", "half", "full"];

// Past this drag distance a pointerup is treated as a drag (snap to nearest), not a tap.
const DRAG_SLOP_PX = 6;
// Past this pointer speed (px/ms) at release, velocity picks the detent instead of raw position.
const FLICK_VELOCITY = 0.6;

const MOBILE_MQ = "(max-width: 780px)";

let sheetEl: HTMLElement;
let handleEl: HTMLButtonElement;
let panelEl: HTMLElement;
let mainEl: HTMLElement;

let enabled = false;
let current: Detent = "collapsed";

// One history entry is pushed while the sheet is open past `collapsed`, so Android's Back /
// the browser back button collapses the sheet instead of leaving the page.
let ownsHistoryEntry = false;

// Drag state. A pointerdown only *arms* a drag; it becomes a real sheet drag (grabbing pointer
// capture, suppressing the click) once the finger moves past the slop - "header" arms on any
// direction, "panel" only converts on a downward move from the list's scroll-top so upward
// moves still scroll the list. A tap that never converts falls through to the handle's click.
let armed: null | "header" | "panel" = null;
let dragging = false;
let didDrag = false;
let startPointerY = 0;
let startTop = 0;
let lastPointerY = 0;
let lastPointerT = 0;
let velocity = 0;
let renderedTop = 0;

const viewportH = (): number => window.innerHeight;
const detentTopPx = (detent: Detent): number => DETENT_TOP[detent] * viewportH();

export const currentDetent = (): Detent => current;

// Whether the mobile sheet is active (mobile breakpoint). Desktop callers use this to keep
// sheet-specific map moves from firing.
export const sheetEnabled = (): boolean => enabled;

// Animate/jump the sheet to a detent. `fromPopstate` marks a change already reflected in
// history (a Back press) so we don't try to unwind the history entry again.
export function snapTo(detent: Detent, opts: { fromPopstate?: boolean } = {}): void {
  if (!enabled) return;
  const previous = current;
  current = detent;
  setTop(detentTopPx(detent));
  sheetEl.dataset.detent = detent;
  handleEl.setAttribute("aria-expanded", String(detent !== "collapsed"));
  syncHistory(previous, detent, opts.fromPopstate ?? false);
}

// Collapse the sheet if it's open; returns whether it did (so a map tap that collapsed the
// sheet can swallow that tap instead of also dropping a location pin).
export function collapseIfOpen(): boolean {
  if (enabled && current !== "collapsed") {
    snapTo("collapsed");
    return true;
  }
  return false;
}

// Center the map on a point, offset upward on mobile so it lands in the visible strip above
// the sheet rather than behind it. On desktop this is a plain setView.
export function focusOnMap(lat: number, lng: number, zoom: number): void {
  map.setView([lat, lng], zoom);
  if (!enabled) return;
  const size = map.getSize();
  // Put the target ~1/3 of the way down the map area that stays visible above the sheet.
  const visibleH = size.y * DETENT_TOP[current];
  map.panBy([0, size.y / 2 - visibleH / 3], { animate: false });
}

function setTop(top: number): void {
  renderedTop = top;
  sheetEl.style.transform = `translateY(${top}px)`;
}

const atIndex = (index: number): Detent =>
  ORDER[Math.max(0, Math.min(ORDER.length - 1, index))] as Detent;

function nearestDetent(top: number): Detent {
  let best = 0;
  ORDER.forEach((detent, index) => {
    if (Math.abs(detentTopPx(detent) - top) < Math.abs(detentTopPx(atIndex(best)) - top)) best = index;
  });
  if (velocity > FLICK_VELOCITY) return atIndex(best - 1); // flicked down -> lower detent
  if (velocity < -FLICK_VELOCITY) return atIndex(best + 1); // flicked up -> higher detent
  return atIndex(best);
}

function beginDrag(event: PointerEvent): void {
  armed = null;
  dragging = true;
  didDrag = true;
  sheetEl.style.transition = "none";
  sheetEl.setPointerCapture(event.pointerId);
}

function onPointerDown(event: PointerEvent): void {
  if (!enabled) return;
  if (event.pointerType === "mouse" && event.button !== 0) return;
  didDrag = false;
  const fromHeader = (event.target as HTMLElement).closest("#sheet-handle, .tabs, #sheet-summary");
  if (!fromHeader && panelEl.scrollTop > 0) return; // scrolled list keeps the drag
  armed = fromHeader ? "header" : "panel";
  startPointerY = lastPointerY = event.clientY;
  startTop = renderedTop;
  lastPointerT = event.timeStamp;
  velocity = 0;
}

function onPointerMove(event: PointerEvent): void {
  if (armed) {
    const dy = event.clientY - startPointerY;
    if (armed === "header" ? Math.abs(dy) > DRAG_SLOP_PX : dy > DRAG_SLOP_PX) {
      beginDrag(event);
    } else if (armed === "panel" && dy < -DRAG_SLOP_PX) {
      armed = null; // upward from scroll-top -> let the list scroll
      return;
    } else {
      return;
    }
  }
  if (!dragging) return;
  const delta = event.clientY - startPointerY;
  const dt = event.timeStamp - lastPointerT;
  if (dt > 0) velocity = (event.clientY - lastPointerY) / dt;
  lastPointerY = event.clientY;
  lastPointerT = event.timeStamp;
  const clamped = Math.max(detentTopPx("full"), Math.min(detentTopPx("collapsed"), startTop + delta));
  setTop(clamped);
  event.preventDefault();
}

function onPointerUp(event: PointerEvent): void {
  armed = null;
  if (!dragging) return;
  dragging = false;
  if (sheetEl.hasPointerCapture(event.pointerId)) sheetEl.releasePointerCapture(event.pointerId);
  sheetEl.style.transition = "";
  snapTo(nearestDetent(renderedTop));
}

function syncHistory(previous: Detent, next: Detent, fromPopstate: boolean): void {
  if (fromPopstate) return;
  const wasOpen = previous !== "collapsed";
  const isOpen = next !== "collapsed";
  if (isOpen && !wasOpen && !ownsHistoryEntry) {
    history.pushState({ foraySheet: true }, "");
    ownsHistoryEntry = true;
  } else if (!isOpen && wasOpen && ownsHistoryEntry) {
    ownsHistoryEntry = false;
    history.back(); // pops our entry; the popstate handler sees the sheet already collapsed
  }
}

function onPopstate(): void {
  if (!enabled) return;
  ownsHistoryEntry = false;
  if (current !== "collapsed") snapTo("collapsed", { fromPopstate: true });
}

function enable(): void {
  if (enabled) return;
  enabled = true;
  if (panelEl.parentElement !== sheetEl) sheetEl.appendChild(panelEl);
  document.body.dataset.sheet = "on";
  current = "collapsed";
  sheetEl.dataset.detent = "collapsed";
  sheetEl.style.transition = "none";
  setTop(detentTopPx("collapsed"));
  // Re-enable the snap transition after the initial position has painted.
  requestAnimationFrame(() => {
    sheetEl.style.transition = "";
    map.invalidateSize();
  });
}

function disable(): void {
  if (!enabled) return;
  enabled = false;
  delete document.body.dataset.sheet;
  sheetEl.style.transform = "";
  sheetEl.style.transition = "";
  if (panelEl.parentElement !== mainEl) mainEl.appendChild(panelEl); // back to the desktop grid
  ownsHistoryEntry = false;
  requestAnimationFrame(() => map.invalidateSize());
}

export function initSheet(): void {
  sheetEl = qs("#sheet");
  handleEl = qs<HTMLButtonElement>("#sheet-handle");
  panelEl = qs("#panel");
  mainEl = qs("main");

  const mq = window.matchMedia(MOBILE_MQ);
  const applyMode = (): void => (mq.matches ? enable() : disable());
  mq.addEventListener("change", applyMode);

  sheetEl.addEventListener("pointerdown", onPointerDown);
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onPointerUp);
  window.addEventListener("popstate", onPopstate);

  handleEl.addEventListener("click", () => {
    if (didDrag) return; // a real drag already snapped; don't also toggle
    snapTo(current === "collapsed" ? "half" : "collapsed");
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") collapseIfOpen();
  });

  // A map drag or a tap on the map returns the sheet to its peek.
  map.on("dragstart", () => collapseIfOpen());

  applyMode();
}
