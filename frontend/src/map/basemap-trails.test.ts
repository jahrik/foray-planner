import {
  validateStyleMin,
  type SourceSpecification,
  type StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import { TRAILS_SOURCE_ID, trailPopupSpec, trailsLayer, trailsSource } from "./basemap-trails";

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

describe("trailsLayer", () => {
  it("reads from the trails source-layer and excludes trailhead points", () => {
    const layer = trailsLayer("dark") as {
      type: string;
      source: string;
      "source-layer": string;
      filter: unknown;
    };

    expect(layer.type).toBe("line");
    expect(layer.source).toBe(TRAILS_SOURCE_ID);
    expect(layer["source-layer"]).toBe("trails");
    expect(layer.filter).toEqual(["!=", ["get", "kind"], "trailhead"]);
  });

  it("colours the line differently per theme", () => {
    const dark = trailsLayer("dark").paint as Record<string, unknown>;
    const light = trailsLayer("light").paint as Record<string, unknown>;
    expect(dark["line-color"]).not.toBe(light["line-color"]);
  });

  it.each(["dark", "light"] as const)("produces a spec-valid style layer (%s)", (theme) => {
    const style: StyleSpecification = {
      version: 8,
      glyphs: "https://example.com/fonts/{fontstack}/{range}.pbf",
      sources: trailsSource("https://example.com/tiles/{z}/{x}/{y}.pbf"),
      layers: [trailsLayer(theme)],
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

  it("adds a directions link labeled with the trail's title when a click point is given", () => {
    const spec = trailPopupSpec({ name: "Moorman Pond Trail", kind: "path" }, 41.3, -124.02);
    expect(spec.directions?.href).toBe(
      "geo:41.300000,-124.020000?q=41.300000,-124.020000(Moorman%20Pond%20Trail)",
    );
  });
});
