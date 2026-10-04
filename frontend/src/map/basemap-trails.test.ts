import {
  validateStyleMin,
  type SourceSpecification,
  type StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import { PALETTE } from "./basemap-roads";
import {
  TRAILS_LINE_LAYER_IDS,
  TRAILS_SOURCE_ID,
  trailPopupSpec,
  trailsLayers,
  trailsSource,
} from "./basemap-trails";

describe("trailsSource", () => {
  it("builds a vector source pointed at the given tiles URL", () => {
    const sources = trailsSource("/api/tiles/trails/{z}/{x}/{y}.pbf");
    const source = sources[TRAILS_SOURCE_ID] as SourceSpecification & Record<string, unknown>;

    expect(source.type).toBe("vector");
    expect(source.tiles).toEqual(["/api/tiles/trails/{z}/{x}/{y}.pbf"]);
    expect(source.minzoom).toBe(8);
    expect(source.maxzoom).toBe(14);
  });
});

describe("trailsLayers", () => {
  type Line = {
    id: string;
    type: string;
    source: string;
    "source-layer": string;
    filter: unknown;
    paint: Record<string, unknown>;
  };
  const byId = (theme: "dark" | "light", id: string) =>
    trailsLayers(theme).find((layer) => layer.id === id) as unknown as Line;

  it("draws forest roads and trails as their own clickable layers off the trails source", () => {
    const ids = trailsLayers("dark").map((layer) => layer.id);
    expect(ids).toEqual(expect.arrayContaining([...TRAILS_LINE_LAYER_IDS]));
    for (const id of TRAILS_LINE_LAYER_IDS) {
      const layer = byId("dark", id);
      expect(layer.type).toBe("line");
      expect(layer.source).toBe(TRAILS_SOURCE_ID);
      expect(layer["source-layer"]).toBe("trails");
    }
    expect(byId("dark", "foray_trails_road").filter).toEqual(["==", ["get", "kind"], "road"]);
    expect(byId("dark", "foray_trails_path").filter).toEqual([
      "in",
      ["get", "kind"],
      ["literal", ["path", "route"]],
    ]);
  });

  it.each(["dark", "light"] as const)("colours roads ochre and trails green at every zoom (%s)", (theme) => {
    expect(byId(theme, "foray_trails_road").paint["line-color"]).toBe(PALETTE[theme].track);
    expect(byId(theme, "foray_trails_path").paint["line-color"]).toBe(PALETTE[theme].path);
  });

  it("draws each class's casing under its line, and labels on top", () => {
    const ids = trailsLayers("dark").map((layer) => layer.id);
    expect(ids.indexOf("foray_trails_road_casing")).toBeLessThan(ids.indexOf("foray_trails_road"));
    expect(ids.indexOf("foray_trails_path_casing")).toBeLessThan(ids.indexOf("foray_trails_path"));
    expect(ids.indexOf("foray_trails_road_labels")).toBeGreaterThan(ids.indexOf("foray_trails_path"));
  });

  it("leaves the synthetic '(OSM)' fallback names unlabelled", () => {
    const labels = trailsLayers("dark").find(
      (layer) => layer.id === "foray_trails_path_labels",
    ) as unknown as {
      filter: unknown;
    };
    expect(JSON.stringify(labels.filter)).toContain("(OSM)");
  });

  it.each(["dark", "light"] as const)("produces a spec-valid style (%s)", (theme) => {
    const style: StyleSpecification = {
      version: 8,
      glyphs: "https://example.com/fonts/{fontstack}/{range}.pbf",
      sources: trailsSource("https://example.com/tiles/{z}/{x}/{y}.pbf"),
      layers: trailsLayers(theme),
    };
    const errors = validateStyleMin(style).filter(
      (error) => error.severity !== "warning" && !/glyphs/.test(error.message),
    );
    expect(errors).toEqual([]);
  });
});

describe("trailPopupSpec", () => {
  it("labels a path with its length", () => {
    const spec = trailPopupSpec({ name: "Moorman Pond Trail", kind: "path", length_km: 0.6 });
    expect(spec.title).toBe("Moorman Pond Trail");
    expect(spec.lines?.[0]).toBe("Trail · 0.6 km");
  });

  it("labels a route distinctly from a plain path", () => {
    const spec = trailPopupSpec({ name: "Pacific Crest Trail", kind: "route", length_km: 12 });
    expect(spec.lines?.[0]).toBe("Route · 12.0 km");
  });

  it("labels a forest road distinctly from a trail", () => {
    const spec = trailPopupSpec({ name: "FR 1506016", kind: "road", length_km: 0.1 });
    expect(spec.lines?.[0]).toBe("Forest road · 0.1 km");
  });

  it("omits the length when the tile doesn't carry one", () => {
    const spec = trailPopupSpec({ name: "X", kind: "path" });
    expect(spec.lines?.[0]).toBe("Trail");
  });

  it("falls back to a generic title and kind when the tile is unnamed/untyped", () => {
    const spec = trailPopupSpec({});
    expect(spec.title).toBe("Unnamed trail");
    expect(spec.lines?.[0]).toBe("Trail");
  });

  it("prefers the land unit over the bare agency when both are present", () => {
    const spec = trailPopupSpec({
      name: "X",
      kind: "path",
      land_unit: "Six Rivers National Forest",
      land_agency: "USFS",
    });
    expect(spec.lines?.[1]).toBe("Six Rivers National Forest");
  });

  it("falls back to the agency when no land unit is carried", () => {
    const spec = trailPopupSpec({ name: "X", kind: "path", land_agency: "BLM" });
    expect(spec.lines?.[1]).toBe("BLM");
  });

  it("omits the second line when neither land unit nor agency is carried", () => {
    const spec = trailPopupSpec({ name: "X", kind: "path" });
    expect(spec.lines).toHaveLength(1);
  });

  it("omits the directions link when no click point is given", () => {
    expect(trailPopupSpec({ name: "X", kind: "path" }).directions).toBeUndefined();
  });

  it("adds a directions link to the click point, labeled 'Directions'", () => {
    const spec = trailPopupSpec({ name: "Moorman Pond Trail", kind: "path" }, 41.3, -124.02);
    expect(spec.directions?.text).toBe("Directions");
    expect(spec.directions?.href).toContain("41.300000,-124.020000");
  });
});
