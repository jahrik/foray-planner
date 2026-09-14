import {
  validateStyleMin,
  type SourceSpecification,
  type StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import { TRAILS_SOURCE_ID, trailsLayer, trailsSource } from "./basemap-trails";

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
