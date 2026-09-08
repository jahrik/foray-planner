import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearShortlist,
  inShortlist,
  renderActionBar,
  setShortlistChangeHook,
  shortlistIds,
  toggleShortlist,
} from "./shortlist";

beforeEach(() => {
  clearShortlist();
  document.body.innerHTML = `
    <div id="route-bar" hidden>
      <span class="route-bar-count"></span>
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

  it("shows the action bar with a pluralised count once the list is non-empty", () => {
    const bar = document.getElementById("route-bar")!;
    renderActionBar();
    expect(bar.hidden).toBe(true);
    toggleShortlist("2_3");
    expect(bar.hidden).toBe(false);
    expect(bar.querySelector(".route-bar-count")?.textContent).toBe("1 spot");
    toggleShortlist("5_5");
    expect(bar.querySelector(".route-bar-count")?.textContent).toBe("2 spots");
  });
});
