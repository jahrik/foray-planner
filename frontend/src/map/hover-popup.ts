import type L from "leaflet";

// One popup shared by the precise-observation cluster badges and the single pins (issue #447).
// With a mouse it opens on hover, stays open while the pointer is over it (its rows are
// iNaturalist / directions links, so it has to be reachable), and closes a beat after the
// pointer leaves both the marker and the popup. Keyboard: focus inside the popup keeps it open,
// leaving it closes it, and Escape closes it and hands focus back to the marker that opened it.
// The DOM wiring is plain addEventListener on the popup element - no Leaflet import at runtime,
// see hover-popup.test.ts.

export const HOVER_POPUP_CLOSE_MS = 250;

export interface HoverPopupTarget {
  latLng: L.LatLngExpression;
  /** The marker element that opened the popup - where Escape returns focus. */
  anchor: HTMLElement | null;
  content: HTMLElement;
}

export interface HoverPopup {
  open(target: HoverPopupTarget): void;
  scheduleClose(): void;
  close(): void;
  /** True if `target` is inside the open popup. */
  contains(target: EventTarget | null): boolean;
  /** The marker element of the open popup, if any. */
  anchor(): HTMLElement | null;
  /** True while Escape is handing focus back to the anchor, so its focusin doesn't reopen. */
  returningFocus(): boolean;
}

export function createHoverPopup(
  map: Pick<L.Map, "closePopup">,
  popup: L.Popup,
  closeMs: number = HOVER_POPUP_CLOSE_MS,
): HoverPopup {
  let anchor: HTMLElement | null = null;
  let closeTimer: number | undefined;
  let returning = false;

  const cancelClose = (): void => window.clearTimeout(closeTimer);
  const close = (): void => {
    cancelClose();
    map.closePopup(popup);
  };
  const scheduleClose = (): void => {
    cancelClose();
    closeTimer = window.setTimeout(() => map.closePopup(popup), closeMs);
  };
  const contains = (target: EventTarget | null): boolean =>
    target instanceof Node && !!popup.getElement()?.contains(target);

  const wire = (element: HTMLElement): void => {
    if (element.dataset.hoverWired) return;
    element.dataset.hoverWired = "1";
    element.addEventListener("mouseenter", cancelClose);
    element.addEventListener("mouseleave", scheduleClose);
    element.addEventListener("focusin", cancelClose);
    element.addEventListener("focusout", (event) => {
      const next = event.relatedTarget;
      if (!contains(next) && next !== anchor) scheduleClose();
    });
    element.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      close();
      returning = true;
      anchor?.focus();
      returning = false;
    });
  };

  return {
    open(target) {
      cancelClose();
      anchor = target.anchor;
      popup
        .setLatLng(target.latLng)
        .setContent(target.content)
        .openOn(map as L.Map);
      const element = popup.getElement();
      if (element) wire(element);
    },
    scheduleClose,
    close,
    contains,
    anchor: () => anchor,
    returningFocus: () => returning,
  };
}
