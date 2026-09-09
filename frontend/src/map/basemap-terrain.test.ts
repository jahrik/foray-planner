import {
  validateStyleMin,
  type LayerSpecification,
  type SourceSpecification,
  type StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import { applyForayRoadStyle } from "./basemap-roads";
import {
  applyTerrainLayers,
  CONTOUR_LABEL_LAYER_ID,
  CONTOUR_LINE_LAYER_ID,
  HILLSHADE_LAYER_ID,
  terrainSources,
} from "./basemap-terrain";
import { themedBaseLayers } from "./basemap-theme";

// A trimmed stand-in for the protomaps-themes-base output - the anchors applyTerrainLayers
// keys off of, plus an unrelated layer that must pass through untouched.
function baseLayers(): LayerSpecification[] {
  return [
    { id: "background", type: "background", paint: { "background-color": "#111" } },
    {
      id: "earth",
      type: "fill",
      source: "protomaps",
      "source-layer": "earth",
      paint: { "fill-color": "#222" },
    },
    {
      id: "landcover",
      type: "fill",
      source: "protomaps",
      "source-layer": "landcover",
      paint: { "fill-color": "#232" },
    },
    {
      id: "water",
      type: "fill",
      source: "protomaps",
      "source-layer": "water",
      paint: { "fill-color": "#134" },
    },
    {
      id: "roads_minor",
      type: "line",
      source: "protomaps",
      "source-layer": "roads",
      paint: { "line-color": "#3d3d3d" },
    },
    {
      id: "places_locality",
      type: "symbol",
      source: "protomaps",
      "source-layer": "places",
      layout: { "text-field": ["get", "name"] },
    },
  ] as LayerSpecification[];
}

function ids(layers: LayerSpecification[]): string[] {
  return layers.map((layer) => layer.id);
}
function byId(layers: LayerSpecification[], id: string): LayerSpecification | undefined {
  return layers.find((layer) => layer.id === id);
}

describe("terrainSources", () => {
  it("builds a terrarium raster-dem source and a contour vector source", () => {
    const sources = terrainSources("dem-shared://x/{z}/{x}/{y}", "dem-contour://x?thresholds=1");
    const dem = sources["terrain-dem"] as SourceSpecification & Record<string, unknown>;
    const contour = sources["terrain-contour"] as SourceSpecification & Record<string, unknown>;

    expect(dem.type).toBe("raster-dem");
    expect(dem.encoding).toBe("terrarium");
    expect(dem.tiles).toEqual(["dem-shared://x/{z}/{x}/{y}"]);
    expect(contour.type).toBe("vector");
    expect(contour.tiles).toEqual(["dem-contour://x?thresholds=1"]);
  });
});

describe("applyTerrainLayers", () => {
  it("splices hillshade below water and contours below the first road layer", () => {
    const out = ids(applyTerrainLayers(baseLayers(), "dark"));

    expect(out.indexOf("earth")).toBeLessThan(out.indexOf(HILLSHADE_LAYER_ID));
    expect(out.indexOf(HILLSHADE_LAYER_ID)).toBeLessThan(out.indexOf("water"));
    expect(out.indexOf(CONTOUR_LINE_LAYER_ID)).toBeLessThan(out.indexOf("roads_minor"));
    expect(out.indexOf(CONTOUR_LINE_LAYER_ID)).toBeLessThan(out.indexOf(CONTOUR_LABEL_LAYER_ID));
    expect(out.indexOf(CONTOUR_LABEL_LAYER_ID)).toBeLessThan(out.indexOf("roads_minor"));
  });

  it("keeps unrelated layers and their order", () => {
    const out = ids(applyTerrainLayers(baseLayers(), "dark"));
    expect(out[0]).toBe("background");
    expect(out).toContain("places_locality");
    expect(out.indexOf("water")).toBeLessThan(out.indexOf("places_locality"));
  });

  it("hides the contour layers unless contoursVisible is set", () => {
    const hidden = applyTerrainLayers(baseLayers(), "dark", false);
    const shown = applyTerrainLayers(baseLayers(), "dark", true);
    const vis = (layers: LayerSpecification[], id: string) =>
      (byId(layers, id) as { layout?: Record<string, unknown> }).layout?.visibility;

    expect(vis(hidden, CONTOUR_LINE_LAYER_ID)).toBe("none");
    expect(vis(hidden, CONTOUR_LABEL_LAYER_ID)).toBe("none");
    expect(vis(shown, CONTOUR_LINE_LAYER_ID)).toBe("visible");
    expect(vis(shown, CONTOUR_LABEL_LAYER_ID)).toBe("visible");
  });

  it("colours the hillshade + contours per theme", () => {
    const shadow = (theme: "dark" | "light") =>
      (
        byId(applyTerrainLayers(baseLayers(), theme), HILLSHADE_LAYER_ID) as {
          paint: Record<string, unknown>;
        }
      ).paint["hillshade-shadow-color"];
    expect(shadow("dark")).not.toBe(shadow("light"));
  });

  it("does not mutate the input array or its layers", () => {
    const input = baseLayers();
    const snapshot = JSON.stringify(input);
    applyTerrainLayers(input, "dark", true);
    expect(JSON.stringify(input)).toBe(snapshot);
  });

  it("still adds the terrain layers when the base anchors are missing", () => {
    const out = ids(
      applyTerrainLayers([{ id: "background", type: "background" }] as LayerSpecification[], "dark"),
    );
    expect(out).toEqual(
      expect.arrayContaining([HILLSHADE_LAYER_ID, CONTOUR_LINE_LAYER_ID, CONTOUR_LABEL_LAYER_ID]),
    );
  });

  it.each(["dark", "light"] as const)(
    "produces a spec-valid style over the real %s base theme + road styling",
    (theme) => {
      const style: StyleSpecification = {
        version: 8,
        glyphs: "https://example.com/fonts/{fontstack}/{range}.pbf",
        sources: {
          protomaps: { type: "vector", url: "pmtiles://us.pmtiles" },
          ...terrainSources("https://dem.example/{z}/{x}/{y}.png", "https://contour.example/{z}/{x}/{y}"),
        },
        layers: applyTerrainLayers(applyForayRoadStyle(themedBaseLayers(theme), theme), theme, true),
      };
      const errors = validateStyleMin(style).filter(
        (error) => error.severity !== "warning" && !/glyphs/.test(error.message),
      );
      expect(errors).toEqual([]);
    },
  );
});
