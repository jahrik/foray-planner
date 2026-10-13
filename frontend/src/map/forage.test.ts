import { describe, expect, it } from "vitest";

import { FORAGE_HI_THRESHOLD, FORAGE_RAMP, FORAGE_TIER_LABELS, forageTier, highlightCutoff } from "./forage";

describe("forageTier", () => {
  it("maps a null / undefined count to the unknown tier", () => {
    expect(forageTier(null)).toBe(0);
    expect(forageTier(undefined)).toBe(0);
  });

  it("bands the count into few / some / many", () => {
    expect(forageTier(0)).toBe(1);
    expect(forageTier(1)).toBe(1);
    expect(forageTier(2)).toBe(2);
    expect(forageTier(4)).toBe(2);
    expect(forageTier(5)).toBe(3);
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

describe("highlightCutoff", () => {
  it("falls back to the absolute floor with no known counts", () => {
    expect(highlightCutoff([])).toBe(FORAGE_HI_THRESHOLD);
    expect(highlightCutoff([null, undefined, 0])).toBe(FORAGE_HI_THRESHOLD);
  });

  it("never drops below the floor, so a quiet area does not light up one-record rows", () => {
    expect(highlightCutoff([1, 1, 2, 3, 4])).toBe(FORAGE_HI_THRESHOLD);
  });

  it("rises to the top quarter in a dense area so the highlight still separates rows", () => {
    const dense = [94, 107, 159, 166, 172, 328, 383, 399, 419, 593, 1256, 2030];
    const cutoff = highlightCutoff(dense);
    expect(cutoff).toBeGreaterThan(FORAGE_HI_THRESHOLD);
    const lit = dense.filter((count) => count >= cutoff).length;
    expect(lit).toBe(Math.ceil(dense.length / 4));
    expect(highlightCutoff([300, 400, 500, 600])).toBe(600);
  });
});
