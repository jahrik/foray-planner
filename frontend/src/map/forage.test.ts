import { describe, expect, it } from "vitest";

import { FORAGE_HI_THRESHOLD, FORAGE_RAMP, FORAGE_TIER_LABELS, forageTier } from "./forage";

describe("forageTier", () => {
  it("maps a null / undefined count to the unknown tier", () => {
    expect(forageTier(null)).toBe(0);
    expect(forageTier(undefined)).toBe(0);
  });

  it("bands the count into few / some / many", () => {
    expect(forageTier(0)).toBe(1);
    expect(forageTier(11)).toBe(1);
    expect(forageTier(12)).toBe(2);
    expect(forageTier(39)).toBe(2);
    expect(forageTier(40)).toBe(3);
    expect(forageTier(500)).toBe(3);
  });

  it("has a ramp colour and label per non-zero tier, and a hi threshold at the top band", () => {
    for (const tier of [1, 2, 3] as const) {
      expect(FORAGE_RAMP[tier - 1]).toMatch(/^#[0-9a-f]{6}$/);
      expect(FORAGE_TIER_LABELS[tier - 1]).toContain("records nearby");
    }
    expect(forageTier(FORAGE_HI_THRESHOLD)).toBe(3);
  });
});
