import { describe, expect, it } from "vitest";

import type { PreciseObservation } from "../api/types";
import { buildClusterList } from "./cluster-popup";

function obs(overrides: Partial<PreciseObservation> = {}): PreciseObservation {
  return {
    id: 1,
    taxon_id: 47348,
    name: "Morchella",
    common_name: null,
    icon: "morchella",
    lat: 46,
    lng: -121,
    observed_on: "2026-05-01",
    uri: "https://www.inaturalist.org/observations/1",
    ...overrides,
  };
}

describe("buildClusterList", () => {
  it("heads the list with the count, pluralised", () => {
    expect(buildClusterList([obs()]).querySelector("b")?.textContent).toBe("1 observation");
    expect(buildClusterList([obs(), obs({ id: 2 })]).querySelector("b")?.textContent).toBe("2 observations");
  });

  it("puts the genus icon before each row's name", () => {
    const link = buildClusterList([obs({ icon: "morchella" })]).querySelector("li a")!;
    expect(link.firstElementChild?.className).toBe("genus-icon");
    expect(link.querySelector("svg")).not.toBeNull();
    expect(link.textContent).toBe("Morchella");
  });

  it("lists newest first with undated observations last", () => {
    const root = buildClusterList([
      obs({ id: 1, name: "Old", observed_on: "2024-04-01" }),
      obs({ id: 2, name: "Undated", observed_on: null }),
      obs({ id: 3, name: "New", observed_on: "2026-05-10" }),
    ]);
    const names = [...root.querySelectorAll("li")].map((item) => item.firstChild?.textContent);
    expect(names).toEqual(["New", "Old", "Undated"]);
  });

  it("links each name to its iNaturalist page by id, never the cached uri", () => {
    const root = buildClusterList([obs({ id: 7, uri: "javascript:alert(1)" }), obs({ id: 8, uri: null })]);
    const anchors = root.querySelectorAll("a");
    expect([...anchors].map((anchor) => anchor.href)).toEqual([
      "https://www.inaturalist.org/observations/7",
      "https://www.inaturalist.org/observations/8",
    ]);
    expect(anchors[0]?.target).toBe("_blank");
    expect(anchors[0]?.rel).toBe("noopener");
  });

  it("shows the common name and date", () => {
    const item = buildClusterList([obs({ common_name: "morels" })]).querySelector("li");
    expect(item?.textContent).toBe("Morchella (morels)2026-05-01");
  });

  it("caps the rows and summarises the rest", () => {
    const many = Array.from({ length: 7 }, (_, index) => obs({ id: index }));
    const root = buildClusterList(many, { limit: 5 });
    expect(root.querySelectorAll("li")).toHaveLength(5);
    expect(root.querySelector(".cluster-list-more")?.textContent).toBe("+2 more - zoom in to see them");
  });

  it("adds a Zoom in button only when a zoom handler is given", () => {
    expect(buildClusterList([obs()]).querySelector("button")).toBeNull();
    let zoomed = false;
    const button = buildClusterList([obs()], { onZoom: () => (zoomed = true) }).querySelector("button");
    expect(button?.textContent).toBe("Zoom in");
    button?.click();
    expect(zoomed).toBe(true);
  });

  it("sets external text via textContent (no markup injection)", () => {
    const root = buildClusterList([obs({ name: "<img src=x onerror=alert(1)>" })]);
    expect(root.querySelector("img")).toBeNull();
  });
});
