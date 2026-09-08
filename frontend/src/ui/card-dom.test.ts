import { beforeEach, describe, expect, it, vi } from "vitest";

import { buildResultCard, type ResultCardModel } from "./card-dom";

function model(over: Partial<ResultCardModel> = {}): ResultCardModel {
  return {
    rank: 0,
    titleText: "235 mi",
    whyHtml: "<strong>Boletus</strong> is at peak here.",
    metaHtml: "score <span class='num'>1.00</span>",
    scoreNorm: 1,
    fireHtml: "",
    renderChips: (showAll) => (showAll ? "<a>a</a><a>b</a><a>c</a>" : "<a>a</a><a>b</a>"),
    chipCount: 3,
    cappedChipCount: 2,
    ...over,
  };
}

const handlers = () => ({
  onSelect: vi.fn(),
  onDetails: vi.fn(),
  onPlan: vi.fn(),
  isPlanned: vi.fn(() => false),
});

describe("buildResultCard", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("renders rank + title, why line, score bar and meta", () => {
    const { card } = buildResultCard(model(), handlers());
    expect(card.querySelector(".num")?.textContent).toBe("#1 · 235 mi");
    expect(card.querySelector(".why")?.textContent).toContain("at peak here");
    expect(card.querySelector(".bar span")?.getAttribute("style")).toContain("100%");
    expect(card.classList.contains("hero")).toBe(true);
  });

  it("omits the score bar and why line when not provided", () => {
    const { card } = buildResultCard(model({ scoreNorm: null, whyHtml: "" }), handlers());
    expect(card.querySelector(".bar")).toBeNull();
    expect(card.querySelector(".why")).toBeNull();
  });

  it("shows a show-more button only when chipCount exceeds the cap, and toggles it", () => {
    const { card } = buildResultCard(model(), handlers());
    const button = card.querySelector<HTMLButtonElement>(".show-more")!;
    expect(button.textContent).toBe("Show all 3");
    expect(card.querySelectorAll(".chips a")).toHaveLength(2);
    button.click();
    expect(card.querySelectorAll(".chips a")).toHaveLength(3);
    expect(button.textContent).toBe("Show less");
  });

  it("has no show-more button when every chip is already shown", () => {
    const { card } = buildResultCard(model({ chipCount: 2 }), handlers());
    expect(card.querySelector(".show-more")).toBeNull();
  });

  it("routes the action buttons without also firing card select", () => {
    const spies = handlers();
    const { card } = buildResultCard(model(), spies);
    card.querySelector<HTMLButtonElement>('[data-act="details"]')!.click();
    card.querySelector<HTMLButtonElement>('[data-act="plan"]')!.click();
    expect(spies.onDetails).toHaveBeenCalledOnce();
    expect(spies.onPlan).toHaveBeenCalledOnce();
    expect(spies.onSelect).not.toHaveBeenCalled();
  });

  it("reflects planned state on the + Plan button", () => {
    const spies = handlers();
    spies.isPlanned.mockReturnValue(true);
    const { card } = buildResultCard(model(), spies);
    const plan = card.querySelector<HTMLButtonElement>('[data-act="plan"]')!;
    expect(plan.textContent).toBe("✓ In route");
    expect(plan.classList.contains("on")).toBe(true);
  });

  it("activates card select on a plain card click", () => {
    const spies = handlers();
    const { card } = buildResultCard(model(), spies);
    card.click();
    expect(spies.onSelect).toHaveBeenCalledOnce();
  });
});
