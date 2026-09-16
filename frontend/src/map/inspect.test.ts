import { describe, expect, it } from "vitest";

import { hitSlop } from "./inspect";

describe("hitSlop", () => {
  it("is zero at and below the low-zoom cutoff", () => {
    expect(hitSlop(2)).toBe(0);
    expect(hitSlop(9)).toBe(0);
  });

  it("is the full 5px at and above the high-zoom cutoff", () => {
    expect(hitSlop(14)).toBe(5);
    expect(hitSlop(19)).toBe(5);
  });

  it("ramps linearly in between", () => {
    expect(hitSlop(11.5)).toBeCloseTo(2.5);
  });
});
