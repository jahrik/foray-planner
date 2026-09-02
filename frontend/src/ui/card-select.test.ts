import { beforeEach, describe, expect, it, vi } from "vitest";

const selectSize = vi.fn();
const deselectSize = vi.fn();
vi.mock("../map/map", () => ({
  selectSize: (marker: unknown) => selectSize(marker),
  deselectSize: (marker: unknown, weight: number) => deselectSize(marker, weight),
}));

const viewState = { view: "destinations" };
vi.mock("../state", () => ({
  get state() {
    return viewState;
  },
}));

import { createCardSelection, createRunGuard } from "./card-select";

beforeEach(() => {
  selectSize.mockClear();
  deselectSize.mockClear();
  viewState.view = "destinations";
  Element.prototype.scrollIntoView = vi.fn();
  document.body.innerHTML = "";
});

describe("createRunGuard", () => {
  it("reports the latest run as current and every earlier one as stale", () => {
    const guard = createRunGuard();
    const first = guard.begin();
    expect(first()).toBe(true);
    const second = guard.begin();
    expect(first()).toBe(false);
    expect(second()).toBe(true);
  });

  it("with a view name, goes stale once state.view changes", () => {
    const guard = createRunGuard("destinations");
    const isCurrent = guard.begin();
    expect(isCurrent()).toBe(true);
    viewState.view = "alerts";
    expect(isCurrent()).toBe(false);
  });

  it("without a view name, ignores state.view", () => {
    const isCurrent = createRunGuard().begin();
    viewState.view = "alerts";
    expect(isCurrent()).toBe(true);
  });
});

describe("createCardSelection", () => {
  function cards(): { container: HTMLElement; a: HTMLElement; b: HTMLElement } {
    document.body.innerHTML = `<div id="list">
      <div class="rank" id="a"></div><div class="rank" id="b"></div></div>`;
    const container = document.getElementById("list") as HTMLElement;
    return {
      container,
      a: document.getElementById("a") as HTMLElement,
      b: document.getElementById("b") as HTMLElement,
    };
  }

  it("moves .active to the selected card and scrolls it into view", () => {
    const { container, a, b } = cards();
    const selection = createCardSelection(container);
    const markerA = {};
    const markerB = {};

    selection.select(a, markerA as never, 0.5);
    expect(a.classList.contains("active")).toBe(true);
    expect(a.scrollIntoView).toHaveBeenCalledWith({ block: "nearest", behavior: "smooth" });
    expect(selectSize).toHaveBeenCalledWith(markerA);

    selection.select(b, markerB as never, 0.3);
    expect(a.classList.contains("active")).toBe(false);
    expect(b.classList.contains("active")).toBe(true);
    // previously selected marker reverts, using the weight it was selected with
    expect(deselectSize).toHaveBeenCalledWith(markerA, 0.5);
    expect(selectSize).toHaveBeenLastCalledWith(markerB);
  });

  it("re-selecting the same marker does not revert it", () => {
    const { container, a } = cards();
    const selection = createCardSelection(container);
    const marker = {};
    selection.select(a, marker as never, 0.5);
    selection.select(a, marker as never, 0.5);
    expect(deselectSize).not.toHaveBeenCalled();
  });

  it("selectInitial marks the card without scrolling or clearing siblings", () => {
    const { container, a, b } = cards();
    b.classList.add("active");
    const selection = createCardSelection(container);
    const marker = {};
    selection.selectInitial(a, marker as never, 0.9);
    expect(a.classList.contains("active")).toBe(true);
    expect(b.classList.contains("active")).toBe(true); // not cleared
    expect(a.scrollIntoView).not.toHaveBeenCalled();
    expect(selectSize).toHaveBeenCalledWith(marker);
  });
});
