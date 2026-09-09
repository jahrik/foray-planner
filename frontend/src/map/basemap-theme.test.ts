import type { LayerSpecification } from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import { themedBaseLayers } from "./basemap-theme";

function paintColor(layers: LayerSpecification[], id: string): unknown {
  const layer = byId(layers, id) as { paint?: Record<string, unknown> } | undefined;
  return layer?.paint?.["fill-color"] ?? layer?.paint?.["line-color"];
}

function byId(layers: LayerSpecification[], id: string): LayerSpecification | undefined {
  return layers.find((layer) => layer.id === id);
}

describe("themedBaseLayers", () => {
  it("returns a non-trivial layer list for both modes", () => {
    expect(themedBaseLayers("dark").length).toBeGreaterThan(30);
    expect(themedBaseLayers("light").length).toBeGreaterThan(30);
  });

  it("lifts dark-mode water off the near-grey stock value", () => {
    // Stock dark water is #31353f - barely off the background. The contrast pass gives it a
    // real navy so the override is doing something visible.
    expect(paintColor(themedBaseLayers("dark"), "water")).toBe("#1c3b57");
  });

  it("keeps every landcover kind coloured (nested override is a full replace, not a merge)", () => {
    const landcover = byId(themedBaseLayers("dark"), "landcover") as
      { paint: { "fill-color": unknown[] } } | undefined;
    const match = landcover?.paint["fill-color"] ?? [];
    // ["match", ["get","kind"], "grassland", <color>, ..., <fallback>] - no entry may be null.
    expect(match.length).toBeGreaterThan(3);
    expect(match.slice(2)).not.toContain(null);
  });

  it("leaves light mode as the stock theme", () => {
    // Stock light water is the bright cyan #80deea; we don't touch light mode.
    expect(paintColor(themedBaseLayers("light"), "water")).toBe("#80deea");
  });
});
