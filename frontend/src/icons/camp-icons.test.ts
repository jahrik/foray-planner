import { describe, expect, it } from "vitest";

import { CAMP_ICON_KEYS, CAMP_ICON_LABELS, campIconKey, campIconSvg } from "./camp-icons";

describe("camp icons", () => {
  it("has art and a label for every icon key", () => {
    for (const key of CAMP_ICON_KEYS) {
      expect(campIconSvg(key)).toMatch(/^<svg[\s\S]*<\/svg>$/);
      expect(CAMP_ICON_LABELS[key]).not.toBe("");
    }
  });

  it("draws every distinct key with distinct art", () => {
    const art = new Set(CAMP_ICON_KEYS.map((key) => campIconSvg(key)));
    expect(art.size).toBe(CAMP_ICON_KEYS.length);
  });

  it("falls back to the generic campfire for an unstated, pitch or unknown type", () => {
    expect(campIconKey(null)).toBe("generic");
    expect(campIconKey(undefined)).toBe("generic");
    expect(campIconKey("pitch")).toBe("generic");
    expect(campIconKey("treehouse")).toBe("generic");
    expect(campIconSvg("pitch")).toBe(campIconSvg("generic"));
  });

  it("keeps a known type", () => {
    expect(campIconKey("rv")).toBe("rv");
    expect(campIconKey("equestrian")).toBe("equestrian");
  });
});
