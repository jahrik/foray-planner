import { beforeEach, describe, expect, it } from "vitest";

import { collapsibleRankList } from "./views";

function card(id: string): HTMLElement {
  const el = document.createElement("div");
  el.className = "rank";
  el.id = id;
  return el;
}

beforeEach(() => {
  document.body.innerHTML = `<div id="panel"></div>`;
});

describe("collapsibleRankList", () => {
  it("keeps the top 3 in the hero column and the rest in the collapsed tail", () => {
    const panel = document.getElementById("panel")!;
    const list = collapsibleRankList(panel);
    for (let rank = 0; rank < 6; rank++) list.place(card(`c${rank}`), rank);
    list.finalize(6);

    const hero = [...list.container.children].filter((el) => el.classList.contains("rank"));
    expect(hero.map((el) => el.id)).toEqual(["c0", "c1", "c2"]);

    const more = document.getElementById("rank-list-more")!;
    expect([...more.children].map((el) => el.id)).toEqual(["c3", "c4", "c5"]);
    expect(more.hidden).toBe(true);
  });

  it("toggles the tail and labels the button with the hidden count", () => {
    const panel = document.getElementById("panel")!;
    const list = collapsibleRankList(panel);
    for (let rank = 0; rank < 10; rank++) list.place(card(`c${rank}`), rank);
    list.finalize(10);

    const button = document.getElementById("more-regions") as HTMLButtonElement;
    const more = document.getElementById("rank-list-more")!;
    expect(button.hidden).toBe(false);
    expect(button.textContent).toBe("Show 7 more regions");

    button.click();
    expect(more.hidden).toBe(false);
    expect(button.textContent).toBe("Show fewer");
    button.click();
    expect(more.hidden).toBe(true);
  });

  it("has no toggle when nothing spills past the hero count", () => {
    const panel = document.getElementById("panel")!;
    const list = collapsibleRankList(panel);
    for (let rank = 0; rank < 3; rank++) list.place(card(`c${rank}`), rank);
    list.finalize(3);
    expect((document.getElementById("more-regions") as HTMLButtonElement).hidden).toBe(true);
  });

  it("revealCard expands the tail for a card inside it, and is a no-op for a hero card", () => {
    const panel = document.getElementById("panel")!;
    const list = collapsibleRankList(panel);
    const cards = Array.from({ length: 6 }, (_, rank) => card(`c${rank}`));
    cards.forEach((el, rank) => list.place(el, rank));
    list.finalize(6);
    const more = document.getElementById("rank-list-more")!;

    list.revealCard(cards[0]!); // hero card
    expect(more.hidden).toBe(true);

    list.revealCard(cards[4]!); // in the tail
    expect(more.hidden).toBe(false);
    expect(document.getElementById("more-regions")?.textContent).toBe("Show fewer");
  });
});
