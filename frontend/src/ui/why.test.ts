import { describe, expect, it } from "vitest";

import type { RegionScore } from "../api/types";
import { whySentence } from "./why";

function region(over: Partial<RegionScore> = {}): RegionScore {
  return {
    region_id: "1_2",
    center_lat: 45,
    center_lng: -122,
    distance_km: 100,
    score: 1,
    score_norm: 1,
    n_species: 3,
    recent_count: 4,
    species: [
      {
        taxon_id: 1,
        name: "Boletus",
        common_name: "Porcini",
        month_count: 38,
        total_count: 100,
        w_pheno: 0.38,
      },
    ],
    ...over,
  } as RegionScore;
}

describe("whySentence", () => {
  it("leads with the top genus and its trend phrase", () => {
    const html = whySentence(region({ pheno_trend: "peak" }));
    expect(html).toContain("<strong>Boletus (Porcini)</strong>");
    expect(html).toContain("is at peak here");
    expect(html).toContain("38 records in your months");
  });

  it("falls back to a neutral phrase when trend is null", () => {
    expect(whySentence(region({ pheno_trend: null }))).toContain("is in season here");
  });

  it("reports recent rain over the threshold and dryness under it", () => {
    expect(whySentence(region({ precip_recent_7d_mm: 20 }))).toMatch(/Rained .* in the last week\./);
    expect(whySentence(region({ precip_recent_7d_mm: 1 }))).toContain("Dry the last week.");
    expect(whySentence(region({ precip_recent_7d_mm: null }))).not.toContain("week");
  });

  it("adds a burn-scar clause, and an active fire takes precedence", () => {
    const scar = whySentence(
      region({
        fire_nearby: [{ status: "historical", name: "X", fire_year: 2024, distance_km: 12 }] as never,
      }),
    );
    expect(scar).toContain("burn scar");
    const both = whySentence(
      region({
        fire_nearby: [
          { status: "historical", name: "X", fire_year: 2024, distance_km: 12 },
          { status: "active", name: "Y", fire_year: 2026, distance_km: 8 },
        ] as never,
      }),
    );
    expect(both).toContain("check access");
    expect(both).not.toContain("burn scar");
  });

  it("uses one singular 'record' at a count of 1", () => {
    const one = region();
    one.species[0]!.month_count = 1;
    expect(whySentence(one)).toContain("1 record in your months");
  });
});
