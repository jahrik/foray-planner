import {
  validateStyleMin,
  type SourceSpecification,
  type StyleSpecification,
} from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import { FIRE_LAYER_IDS, FIRE_SOURCE_ID, fireLayers, firePopupSpec, fireSource } from "./basemap-fire";

describe("fireSource", () => {
  it("builds a vector source pointed at the given tiles URL", () => {
    const sources = fireSource("/api/tiles/fire/{z}/{x}/{y}.pbf");
    const source = sources[FIRE_SOURCE_ID] as SourceSpecification & Record<string, unknown>;

    expect(source.type).toBe("vector");
    expect(source.tiles).toEqual(["/api/tiles/fire/{z}/{x}/{y}.pbf"]);
  });
});

describe("fireLayers", () => {
  it("shows/hides both layers together", () => {
    const hidden = fireLayers(false) as { layout: { visibility: string } }[];
    expect(hidden.map((layer) => layer.layout.visibility)).toEqual(["none", "none"]);
    const shown = fireLayers(true) as { layout: { visibility: string } }[];
    expect(shown.map((layer) => layer.layout.visibility)).toEqual(["visible", "visible"]);
  });

  it("splits fill (polygons) from circle (points) by MVT geometry type", () => {
    const [fill, point] = fireLayers(true) as { id: string; type: string; filter: unknown }[];
    expect([fill!.id, point!.id]).toEqual(FIRE_LAYER_IDS);
    expect(fill!.type).toBe("fill");
    expect(fill!.filter).toEqual(["==", ["geometry-type"], "Polygon"]);
    expect(point!.type).toBe("circle");
    expect(point!.filter).toEqual(["==", ["geometry-type"], "Point"]);
  });

  it.each([true, false])("produces a spec-valid style (visible=%s)", (visible) => {
    const style: StyleSpecification = {
      version: 8,
      glyphs: "https://example.com/fonts/{fontstack}/{range}.pbf",
      sources: fireSource("https://example.com/tiles/{z}/{x}/{y}.pbf"),
      layers: fireLayers(visible),
    };
    const errors = validateStyleMin(style).filter(
      (error) => error.severity !== "warning" && !/glyphs/.test(error.message),
    );
    expect(errors).toEqual([]);
  });
});

describe("firePopupSpec", () => {
  it("describes an active fire with containment", () => {
    const spec = firePopupSpec({
      name: "Ridge Fire",
      status: "active",
      percent_contained: 42.6,
      gis_acres: 1234,
    });
    expect(spec.title).toBe("Ridge Fire");
    expect(spec.lines?.[0]).toBe("Active fire · 43% contained · 1,234 ac");
  });

  it("describes a burn scar with its year and severity", () => {
    const spec = firePopupSpec({
      name: "Old Fire",
      status: "historical",
      fire_year: 2021,
      dominant_severity: "high",
    });
    expect(spec.lines?.[0]).toBe("2021 burn scar · high severity");
  });

  it("links to the incident page only when the tile carries one", () => {
    expect(firePopupSpec({ name: "X", status: "active" }).link).toBeUndefined();
    expect(firePopupSpec({ name: "X", status: "active", incident_url: "https://example.com" }).link).toEqual({
      href: "https://example.com",
      text: "Incident info ↗",
    });
  });
});
