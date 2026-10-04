import {
  validateStyleMin,
  type LayerSpecification,
  type StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import { applyForayRoadStyle } from "./basemap-roads";
import { themedBaseLayers } from "./basemap-theme";

// A trimmed stand-in for the protomaps-themes-base output - just the anchors the override
// keys off of, plus an unrelated layer that must pass through untouched.
function baseLayers(): LayerSpecification[] {
  return [
    { id: "earth", type: "background", paint: { "background-color": "#111" } },
    {
      id: "roads_other",
      type: "line",
      source: "protomaps",
      "source-layer": "roads",
      filter: ["all", ["!has", "is_tunnel"], ["in", "kind", "other", "path"]],
      paint: { "line-color": "#333333" },
    },
    {
      id: "roads_minor",
      type: "line",
      source: "protomaps",
      "source-layer": "roads",
      paint: { "line-color": "#3d3d3d" },
    },
    {
      id: "roads_labels_minor",
      type: "symbol",
      source: "protomaps",
      "source-layer": "roads",
      minzoom: 15,
      filter: ["in", "kind", "minor_road", "other", "path"],
      layout: { "text-field": ["get", "name"] },
    },
  ] as LayerSpecification[];
}

function byId(layers: LayerSpecification[], id: string): LayerSpecification | undefined {
  return layers.find((layer) => layer.id === id);
}

describe("applyForayRoadStyle", () => {
  it("drops the base theme's forest-road and trail lines, keeping only the misc ways", () => {
    const out = applyForayRoadStyle(baseLayers(), "dark");

    expect(byId(out, "roads_other")).toBeUndefined();
    const misc = byId(out, "roads_foray_other") as { filter: unknown };
    expect(misc).toBeDefined();
    expect(JSON.stringify(misc.filter)).toContain('["!=",["get","kind_detail"],"track"]');
    // The trails tile layer (basemap-trails.ts) is the only source for these now.
    expect(out.some((layer) => /roads_foray_(track|path)/.test(layer.id))).toBe(false);
  });

  it("keeps unrelated layers and their order", () => {
    const out = applyForayRoadStyle(baseLayers(), "dark");
    const ids = out.map((layer) => layer.id);

    expect(ids[0]).toBe("earth");
    expect(ids.indexOf("roads_foray_other")).toBeLessThan(ids.indexOf("roads_minor"));
    expect(ids).toContain("roads_labels_minor");
  });

  it("narrows the base minor-road label filter so it no longer labels trails or tracks", () => {
    const out = applyForayRoadStyle(baseLayers(), "dark");

    expect((byId(out, "roads_labels_minor") as { filter: unknown }).filter).toEqual([
      "==",
      ["get", "kind"],
      "minor_road",
    ]);
  });

  it("does not mutate the input array or its layers", () => {
    const input = baseLayers();
    const snapshot = JSON.stringify(input);

    applyForayRoadStyle(input, "dark");

    expect(JSON.stringify(input)).toBe(snapshot);
  });

  it.each(["dark", "light"] as const)("produces a spec-valid style over the real %s base theme", (theme) => {
    // Catches malformed paint/layout expressions - e.g. a ["zoom"] nested somewhere other
    // than a top-level step/interpolate, which validates fine in isolation but MapLibre
    // rejects at load and silently drops the layer.
    const style: StyleSpecification = {
      version: 8,
      glyphs: "https://example.com/fonts/{fontstack}/{range}.pbf",
      sources: { protomaps: { type: "vector", url: "pmtiles://us.pmtiles" } },
      layers: applyForayRoadStyle(themedBaseLayers(theme), theme),
    };
    const errors = validateStyleMin(style).filter(
      (error) => error.severity !== "warning" && !/glyphs/.test(error.message),
    );
    expect(errors).toEqual([]);
  });

  it("still adds the misc layer when the base anchor is missing", () => {
    const out = applyForayRoadStyle([{ id: "earth", type: "background" }] as LayerSpecification[], "dark");

    expect(out.map((layer) => layer.id)).toEqual(["earth", "roads_foray_other"]);
  });
});
