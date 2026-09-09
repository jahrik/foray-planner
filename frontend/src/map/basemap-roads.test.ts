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
  it("replaces roads_other with distinct forest-road and trail layers", () => {
    const out = applyForayRoadStyle(baseLayers(), "dark");

    expect(byId(out, "roads_other")).toBeUndefined();
    expect(byId(out, "roads_foray_track")).toBeDefined();
    expect(byId(out, "roads_foray_path")).toBeDefined();
    expect(byId(out, "roads_foray_other")).toBeDefined();
  });

  it("keeps unrelated layers and their order", () => {
    const out = applyForayRoadStyle(baseLayers(), "dark");
    const ids = out.map((layer) => layer.id);

    expect(ids[0]).toBe("earth");
    expect(ids.indexOf("roads_foray_track")).toBeLessThan(ids.indexOf("roads_minor"));
    expect(ids).toContain("roads_labels_minor");
  });

  it("colours the classes per theme", () => {
    const dark = applyForayRoadStyle(baseLayers(), "dark");
    const light = applyForayRoadStyle(baseLayers(), "light");

    const trackPaint = (layers: LayerSpecification[]) =>
      (byId(layers, "roads_foray_track") as { paint: Record<string, unknown> }).paint["line-color"];

    expect(trackPaint(dark)).toBe("#e0a458");
    expect(trackPaint(light)).toBe("#b06a1e");
    expect(trackPaint(dark)).not.toBe(trackPaint(light));
  });

  it("draws each class on a contrasting casing under the line", () => {
    const out = applyForayRoadStyle(baseLayers(), "dark");
    const ids = out.map((layer) => layer.id);

    expect(ids.indexOf("roads_foray_track_casing")).toBeLessThan(ids.indexOf("roads_foray_track"));
    expect(ids.indexOf("roads_foray_path_casing")).toBeLessThan(ids.indexOf("roads_foray_path"));
  });

  it("narrows the base minor-road label filter and adds class labels after it", () => {
    const out = applyForayRoadStyle(baseLayers(), "dark");
    const ids = out.map((layer) => layer.id);

    expect((byId(out, "roads_labels_minor") as { filter: unknown }).filter).toEqual([
      "==",
      ["get", "kind"],
      "minor_road",
    ]);
    expect(ids.indexOf("roads_foray_track_labels")).toBeGreaterThan(ids.indexOf("roads_labels_minor"));
    expect(ids.indexOf("roads_foray_path_labels")).toBeGreaterThan(ids.indexOf("roads_labels_minor"));
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

  it("still adds the foray layers when the base anchors are missing", () => {
    const out = applyForayRoadStyle([{ id: "earth", type: "background" }] as LayerSpecification[], "dark");
    const ids = out.map((layer) => layer.id);

    expect(ids).toEqual(
      expect.arrayContaining([
        "roads_foray_track",
        "roads_foray_path",
        "roads_foray_track_labels",
        "roads_foray_path_labels",
      ]),
    );
  });
});
