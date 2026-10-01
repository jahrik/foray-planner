import { beforeEach, describe, expect, it } from "vitest";

import { state } from "../state";
import { clearShortlist, inShortlist, pinFor, setShortlistChangeHook } from "./shortlist";
import { createPinAction, markPinnable, syncPinnedChips } from "./stop-pin";

const TRAILHEAD = { kind: "trail" as const, id: "osm:node/1", name: "Foo TH" };
const CAMP = { kind: "camp" as const, id: "ridb:7", name: "Lost Lake" };

function chip(ref: typeof TRAILHEAD | typeof CAMP): HTMLButtonElement {
  const button = document.createElement("button");
  markPinnable(button, ref);
  return button;
}

beforeEach(() => {
  state.view = "destinations";
  setShortlistChangeHook(() => {});
  clearShortlist();
  document.body.innerHTML = `<div id="status"></div><div class="details-view"></div>`;
});

describe("createPinAction", () => {
  it("stays hidden until a row is selected", () => {
    const action = createPinAction("r1");
    expect(action.element.hidden).toBe(true);
    action.select(TRAILHEAD);
    expect(action.element.hidden).toBe(false);
    expect(action.element.textContent).toBe("📍 Use Foo TH as trip stop");
    expect(action.element.getAttribute("aria-pressed")).toBe("false");
  });

  it("pins on click, shortlisting the region, and unpins on a second click", () => {
    const view = document.querySelector<HTMLElement>(".details-view")!;
    const trailChip = chip(TRAILHEAD);
    const action = createPinAction("r1");
    view.append(trailChip, action.element);

    action.select(TRAILHEAD);
    action.element.click();
    expect(pinFor("r1")).toEqual(TRAILHEAD);
    expect(inShortlist("r1")).toBe(true);
    expect(action.element.textContent).toBe("✓ Trip stop: Foo TH");
    expect(action.element.getAttribute("aria-pressed")).toBe("true");
    expect(trailChip.classList.contains("pinned")).toBe(true);

    action.element.click();
    expect(pinFor("r1")).toBeUndefined();
    expect(inShortlist("r1")).toBe(true); // unpinning keeps the region in the route
    expect(action.element.getAttribute("aria-pressed")).toBe("false");
    expect(trailChip.classList.contains("pinned")).toBe(false);
  });

  it("re-pinning from another tab moves the mark across the whole Details view", () => {
    const view = document.querySelector<HTMLElement>(".details-view")!;
    const trailChip = chip(TRAILHEAD);
    const campChip = chip(CAMP);
    const trailAction = createPinAction("r1");
    const campAction = createPinAction("r1");
    view.append(trailChip, trailAction.element, campChip, campAction.element);

    trailAction.select(TRAILHEAD);
    trailAction.element.click();
    campAction.select(CAMP);
    expect(campAction.element.getAttribute("aria-pressed")).toBe("false");
    campAction.element.click();

    expect(pinFor("r1")).toEqual(CAMP);
    expect(campChip.classList.contains("pinned")).toBe(true);
    expect(trailChip.classList.contains("pinned")).toBe(false);
  });
});

describe("syncPinnedChips", () => {
  it("marks only the chip matching the region's pin", () => {
    const root = document.createElement("div");
    const trailChip = chip(TRAILHEAD);
    const campChip = chip(CAMP);
    root.append(trailChip, campChip);
    syncPinnedChips(root, "r1");
    expect(root.querySelectorAll(".pinned")).toHaveLength(0);
  });
});
