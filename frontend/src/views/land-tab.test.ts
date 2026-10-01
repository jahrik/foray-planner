import { beforeEach, describe, expect, it, vi } from "vitest";

import type { LandParcel } from "../api/types";

const getJson = vi.fn();
vi.mock("../api/client", () => ({ getJson: (...args: unknown[]) => getJson(...args) }));
// destination-tabs' other loaders pull in Leaflet/map modules; the Public land tab uses none.
vi.mock("../map/layers", () => ({ selectTrailhead: vi.fn() }));
vi.mock("../map/pins", () => ({}));
vi.mock("../map/map", () => ({ HEAT_RGB: "0,0,0" }));
vi.mock("../map/destinations", () => ({ regionRadiusKm: () => 28 }));
vi.mock("../map/popup", () => ({ buildPopup: vi.fn() }));

const { loadLandInto, namesAgency } = await import("./destination-tabs");
const { clearShortlist, pinFor } = await import("./shortlist");

const REGION = { region_id: "r1", center_lat: 45, center_lng: -121 };
const FOREST: LandParcel = {
  id: "usfs:1",
  agency: "USFS",
  unit: "Six Rivers National Forest",
  url: "https://example.test/usfs",
  distance_km: 0,
};
const BLM: LandParcel = {
  id: "blm:1",
  agency: "BLM",
  unit: null,
  url: "http://insecure.test",
  distance_km: 4.2,
};

beforeEach(() => {
  getJson.mockReset();
  clearShortlist();
  document.body.innerHTML = `<div id="status"></div><div class="details-view"><div id="land"></div></div>`;
});

describe("loadLandInto", () => {
  it("lists parcels and pins the selected one as the stop", async () => {
    getJson.mockResolvedValueOnce([FOREST, BLM]);
    const container = document.getElementById("land")!;
    expect(await loadLandInto(REGION, container)).toBe(true);

    const chips = [...container.querySelectorAll<HTMLButtonElement>(".chip")];
    expect(chips.map((chip) => chip.textContent)).toEqual(["Six Rivers NF · USFS · inside", "BLM · 3 mi"]);
    expect(container.querySelector(".hint")?.textContent).toContain("Ownership only");

    const action = container.querySelector<HTMLButtonElement>(".pin-action")!;
    const source = container.querySelector<HTMLAnchorElement>("a.show-more")!;
    expect(action.hidden).toBe(true);
    chips[0]!.click();
    expect(source.hidden).toBe(false);
    expect(source.href).toBe("https://example.test/usfs");
    action.click();
    expect(pinFor("r1")).toEqual({ kind: "land", id: "usfs:1", name: "Six Rivers National Forest" });
    expect(chips[0]!.classList.contains("pinned")).toBe(true);

    chips[1]!.click(); // a non-https source link is never surfaced
    expect(source.hidden).toBe(true);
  });

  it("widens the search once when nothing is inside the destination", async () => {
    getJson.mockResolvedValueOnce([]).mockResolvedValueOnce([BLM]);
    const container = document.getElementById("land")!;
    await loadLandInto(REGION, container);
    expect(getJson.mock.calls.map((call) => call[1].query.radius_km)).toEqual([28, 84]);
    expect(container.querySelector(".hint")?.textContent).toContain("None inside the destination");
  });
});

describe("namesAgency", () => {
  it("spots a name that already spells out its agency", () => {
    expect(namesAgency("Bureau of Land Management", "BLM")).toBe(true);
    expect(namesAgency("BLM", "BLM")).toBe(true);
    expect(namesAgency("Olympic National Forest", "USFS")).toBe(false);
  });
});
