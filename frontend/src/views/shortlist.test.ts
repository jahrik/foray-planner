import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearShortlist,
  inShortlist,
  renderActionBar,
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

  it("fires the change hook on every mutation", () => {
    const hook = vi.fn();
    setShortlistChangeHook(hook);
    toggleShortlist("2_3");
    toggleShortlist("2_3");
    clearShortlist();
    expect(hook).toHaveBeenCalledTimes(3);
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
});
