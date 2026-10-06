import { describe, expect, it } from "vitest";

import type { CampSite } from "../api/types";
import { campFill, campIcon, campLabel, poiLegendEntries } from "./poi-layers";

const SITE: CampSite = {
  id: "ridb:1",
  name: "Woods Lake Campground",
  kind: "campground",
  fee: null,
  free: null,
  center_lat: 44,
  center_lng: -122,
  distance_km: 2,
  source: "ridb",
  url: "https://example.com",
  camp_type: "tent",
  pitch_count: 0,
};

describe("camp markers", () => {
  it("colours free gold, paid or unknown amber, and OSM-reported teal", () => {
    expect(campFill({ kind: "campground", free: true })).toBe("#ffe14d");
    expect(campFill({ kind: "campground", free: null })).toBe("#ff9e2e");
    expect(campFill({ kind: "campground", free: false })).toBe("#ff9e2e");
    expect(campFill({ kind: "reported", free: true })).toBe("#1fe6d0");
  });

  it("names the camp type in the accessible label, and the pitches it stands for", () => {
    expect(campLabel(SITE)).toBe("Woods Lake Campground, Tent camping");
    expect(campLabel({ ...SITE, camp_type: null })).toBe("Woods Lake Campground, Campground");
    expect(campLabel({ ...SITE, camp_type: "pitch", pitch_count: 12 })).toBe(
      "Woods Lake Campground, Campground, 12 pitches",
    );
  });

  it("draws the type icon on the free / paid disc", () => {
    const html = String(campIcon({ ...SITE, free: true, camp_type: "rv" }).options.html);
    expect(html).toContain("background:#ffe14d");
    expect(html).toContain("<svg");
    expect(html).not.toContain("<b>");
  });

  it("adds a count chip only for a row standing for several pitches", () => {
    expect(String(campIcon({ ...SITE, pitch_count: 1 }).options.html)).not.toContain("<b>");
    expect(String(campIcon({ ...SITE, pitch_count: 12 }).options.html)).toContain("<b>12</b>");
  });

  it("marks the selected row", () => {
    expect(campIcon(SITE, true).options.className).toContain("active");
    expect(campIcon(SITE).options.className).not.toContain("active");
  });

  it("has no legend rows before anything is loaded", () => {
    expect(poiLegendEntries()).toEqual([]);
  });
});
