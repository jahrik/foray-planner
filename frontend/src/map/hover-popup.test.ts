import type L from "leaflet";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createHoverPopup, HOVER_POPUP_CLOSE_MS, type HoverPopupTarget } from "./hover-popup";

// A stand-in for L.Popup: openOn attaches its element to the document, map.closePopup detaches it.
function setup() {
  const element = document.createElement("div");
  let content: HTMLElement | null = null;
  const popup = {
    setLatLng: vi.fn(() => popup),
    setContent: vi.fn((next: HTMLElement) => {
      content = next;
      element.replaceChildren(next);
      return popup;
    }),
    openOn: vi.fn(() => {
      document.body.append(element);
      return popup;
    }),
    getElement: () => (element.isConnected ? element : undefined),
  };
  const map = { closePopup: vi.fn(() => element.remove()) };
  const view = createHoverPopup(map as unknown as L.Map, popup as unknown as L.Popup);
  return { view, map, element, content: () => content };
}

function target(): HoverPopupTarget & { anchor: HTMLElement } {
  const anchor = document.createElement("div");
  anchor.tabIndex = 0;
  document.body.append(anchor);
  const content = document.createElement("div");
  const link = document.createElement("a");
  link.href = "https://www.inaturalist.org/observations/1";
  content.append(link);
  return { latLng: [45, -121], anchor, content };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  document.body.replaceChildren();
});

describe("createHoverPopup", () => {
  it("opens with the target's content and remembers its anchor", () => {
    const { view, element, content } = setup();
    const pin = target();
    view.open(pin);
    expect(element.isConnected).toBe(true);
    expect(content()).toBe(pin.content);
    expect(view.anchor()).toBe(pin.anchor);
  });

  it("closes only after the delay", () => {
    const { view, map } = setup();
    view.open(target());
    view.scheduleClose();
    vi.advanceTimersByTime(HOVER_POPUP_CLOSE_MS - 1);
    expect(map.closePopup).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(map.closePopup).toHaveBeenCalledOnce();
  });

  it("stays open while the pointer is over the popup, and closes after it leaves", () => {
    const { view, map, element } = setup();
    view.open(target());
    view.scheduleClose(); // pointer left the marker...
    element.dispatchEvent(new MouseEvent("mouseenter")); // ...and reached the popup
    vi.advanceTimersByTime(HOVER_POPUP_CLOSE_MS * 2);
    expect(map.closePopup).not.toHaveBeenCalled();
    element.dispatchEvent(new MouseEvent("mouseleave"));
    vi.advanceTimersByTime(HOVER_POPUP_CLOSE_MS);
    expect(map.closePopup).toHaveBeenCalledOnce();
  });

  it("re-opening cancels a pending close (pointer moved from one pin to the next)", () => {
    const { view, map } = setup();
    view.open(target());
    view.scheduleClose();
    const next = target();
    view.open(next);
    vi.advanceTimersByTime(HOVER_POPUP_CLOSE_MS * 2);
    expect(map.closePopup).not.toHaveBeenCalled();
    expect(view.anchor()).toBe(next.anchor);
  });

  it("wires the popup element once across re-opens", () => {
    const { view, element } = setup();
    const listen = vi.spyOn(element, "addEventListener");
    view.open(target());
    view.open(target());
    expect(listen.mock.calls.filter(([type]) => type === "keydown")).toHaveLength(1);
  });

  it("Escape closes and returns focus to the anchor without flagging a reopen afterwards", () => {
    const { view, map, element } = setup();
    const pin = target();
    view.open(pin);
    let returningDuringFocus: boolean | null = null;
    pin.anchor.addEventListener("focus", () => (returningDuringFocus = view.returningFocus()));
    element.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(map.closePopup).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(pin.anchor);
    expect(returningDuringFocus).toBe(true);
    expect(view.returningFocus()).toBe(false);
  });

  it("focus leaving the popup closes it, unless it moves back to the anchor", () => {
    const { view, map, element } = setup();
    const pin = target();
    view.open(pin);
    element.dispatchEvent(new FocusEvent("focusout", { relatedTarget: pin.anchor }));
    vi.advanceTimersByTime(HOVER_POPUP_CLOSE_MS);
    expect(map.closePopup).not.toHaveBeenCalled();
    element.dispatchEvent(new FocusEvent("focusout", { relatedTarget: document.body }));
    vi.advanceTimersByTime(HOVER_POPUP_CLOSE_MS);
    expect(map.closePopup).toHaveBeenCalledOnce();
  });

  it("contains() is true only for nodes inside the open popup", () => {
    const { view } = setup();
    const pin = target();
    view.open(pin);
    expect(view.contains(pin.content.querySelector("a"))).toBe(true);
    expect(view.contains(pin.anchor)).toBe(false);
    expect(view.contains(null)).toBe(false);
  });
});
