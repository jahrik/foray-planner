import { describe, expect, it } from "vitest";

import type { RegionScore } from "../api/types";
import { sortRegions } from "./sort";

function region(over: Partial<RegionScore>): RegionScore {
  return {
    region_id: "0_0",
    center_lat: 0,
    center_lng: 0,
    distance_km: 100,
    score: 1,
    score_norm: 0.5,
    n_species: 1,
    recent_count: 0,
    species: [],
    ...over,
  } as RegionScore;
}

const a = region({ region_id: "a", distance_km: 300, score_norm: 0.9, recent_count: 1 });
const b = region({ region_id: "b", distance_km: 50, score_norm: 0.6, recent_count: 8 });
const c = region({ region_id: "c", distance_km: 150, score_norm: 0.8, recent_count: 8 });

describe("sortRegions", () => {
  it("leaves the server order untouched for 'best'", () => {
    expect(sortRegions([a, b, c], "best").map((r) => r.region_id)).toEqual(["a", "b", "c"]);
  });

  it("orders by distance for 'nearest'", () => {
    expect(sortRegions([a, b, c], "nearest").map((r) => r.region_id)).toEqual(["b", "c", "a"]);
  });

  it("floats recent regions for 'active', breaking ties on score", () => {
    expect(sortRegions([a, b, c], "active").map((r) => r.region_id)).toEqual(["c", "b", "a"]);
  });

  it("never mutates the input array", () => {
    const input = [a, b, c];
    sortRegions(input, "nearest");
    expect(input.map((r) => r.region_id)).toEqual(["a", "b", "c"]);
  });
});
