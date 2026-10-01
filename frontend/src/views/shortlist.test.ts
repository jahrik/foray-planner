import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearShortlist,
  inShortlist,
  pinFor,
  pinParams,
  renderActionBar,
  setPin,
  setShortlistChangeHook,
  shortlistIds,
  toggleShortlist,
} from "./shortlist";
import { state } from "../state";

beforeEach(() => {
  state.view = "destinations";
  clearShortlist();
  document.body.innerHTML = `
    <div id="route-bar" hidden>
      <span class="route-bar-count"></span>
      <button class="route-bar-clear" hidden></button>
    </div>`;
  setShortlistChangeHook(() => {});
});

describe("shortlist", () => {
  it("adds and removes region ids, preserving insertion order", () => {
    toggleShortlist("2_3");
    toggleShortlist("5_5");
    toggleShortlist("1_1");
    expect(shortlistIds()).toEqual(["2_3", "5_5", "1_1"]);
    toggleShortlist("5_5");
    expect(shortlistIds()).toEqual(["2_3", "1_1"]);
    expect(inShortlist("5_5")).toBe(false);
  });

  it("fires the bulk-change hook only on a non-empty clear, not on toggles", () => {
    const hook = vi.fn();
    setShortlistChangeHook(hook);
    toggleShortlist("2_3");
    toggleShortlist("5_5");
    expect(hook).not.toHaveBeenCalled();
    clearShortlist();
    expect(hook).toHaveBeenCalledTimes(1);
    clearShortlist(); // already empty - no-op
    expect(hook).toHaveBeenCalledTimes(1);
  });

  it("shows the bar on the Destinations flow and updates its label + Clear button", () => {
    const bar = document.getElementById("route-bar")!;
    const clear = bar.querySelector<HTMLElement>(".route-bar-clear")!;
    renderActionBar();
    expect(bar.hidden).toBe(false); // empty list still shows the bar (auto-pick)
    expect(bar.querySelector(".route-bar-count")?.textContent).toBe("Plan a road trip");
    expect(clear.hidden).toBe(true);
    toggleShortlist("2_3");
    expect(bar.querySelector(".route-bar-count")?.textContent).toBe("1 spot picked");
    expect(clear.hidden).toBe(false);
    toggleShortlist("5_5");
    expect(bar.querySelector(".route-bar-count")?.textContent).toBe("2 spots picked");
  });

  it("hides the bar off the Destinations flow", () => {
    const bar = document.getElementById("route-bar")!;
    state.view = "plan";
    renderActionBar();
    expect(bar.hidden).toBe(true);
  });

  it("pinning shortlists the region and rides along as a pin query value", () => {
    const camp = { kind: "camp" as const, id: "ridb:7", name: "Lost Lake" };
    toggleShortlist("aaa");
    setPin("bbb", camp);
    expect(shortlistIds()).toEqual(["aaa", "bbb"]);
    expect(pinFor("bbb")).toEqual(camp);
    expect(pinParams()).toEqual(["bbb:camp:ridb:7"]);
    // Re-pinning replaces; unpinning keeps the region in the route.
    setPin("bbb", { kind: "trail", id: "osm:node/9", name: "Foo TH" });
    expect(pinParams()).toEqual(["bbb:trail:osm:node/9"]);
    setPin("bbb", null);
    expect(pinParams()).toEqual([]);
    expect(inShortlist("bbb")).toBe(true);
  });

  it("drops a region's pin when the region leaves the shortlist", () => {
    setPin("bbb", { kind: "camp", id: "ridb:7", name: "Lost Lake" });
    toggleShortlist("bbb");
    expect(pinFor("bbb")).toBeUndefined();
    toggleShortlist("bbb");
    expect(pinParams()).toEqual([]);
    setPin("ccc", { kind: "camp", id: "ridb:8", name: "X" });
    clearShortlist();
    expect(pinFor("ccc")).toBeUndefined();
  });
});
