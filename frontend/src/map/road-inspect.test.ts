import { describe, expect, it } from "vitest";

import { pickRoadFeature, roadLineLayerIds, roadPopupSpec } from "./road-inspect";

describe("roadLineLayerIds", () => {
  const style = [
    { id: "background", type: "background" },
    { id: "water", type: "fill", "source-layer": "water" },
    { id: "roads_minor_casing", type: "line", "source-layer": "roads" },
    { id: "roads_minor", type: "line", "source-layer": "roads" },
    { id: "roads_foray_track", type: "line", "source-layer": "roads" },
    { id: "roads_labels_minor", type: "symbol", "source-layer": "roads" },
    { id: "roads_foray_track_labels", type: "line", "source-layer": "roads" },
    { id: "terrain_hillshade", type: "hillshade" },
  ];

  it("keeps only line layers off the roads source-layer, minus casing + label helpers", () => {
    expect(roadLineLayerIds(style)).toEqual(["roads_minor", "roads_foray_track"]);
  });

  it("returns empty when the style has no roads layers yet", () => {
    expect(roadLineLayerIds([{ id: "background", type: "background" }])).toEqual([]);
  });
});

describe("pickRoadFeature", () => {
  it("returns null for no hits", () => {
    expect(pickRoadFeature([])).toBeNull();
  });

  it("prefers a named way over an unnamed one even if the unnamed is on top", () => {
    const hits = [
      { properties: { kind_detail: "path" } },
      { properties: { name: "James Irvine Trail", kind_detail: "path" } },
    ];
    expect(pickRoadFeature(hits)).toBe(hits[1]);
  });

  it("falls back to the topmost hit when nothing is named", () => {
    const hits = [{ properties: { kind_detail: "track" } }, { properties: { kind_detail: "path" } }];
    expect(pickRoadFeature(hits)).toBe(hits[0]);
  });

  it("tolerates null properties", () => {
    const hits = [{ properties: null }];
    expect(pickRoadFeature(hits)).toBe(hits[0]);
  });
});

describe("roadPopupSpec", () => {
  it("titles a named forest road and labels the raw highway tag", () => {
    const spec = roadPopupSpec({ name: "FR 300", kind_detail: "track", ref: "300" }, 41.3, -124.02);
    expect(spec.title).toBe("FR 300");
    expect(spec.lines).toEqual(["Forest / logging road (highway=track)", "Ref 300"]);
  });

  it("uses name:en ahead of name", () => {
    const spec = roadPopupSpec({ name: "Chemin", "name:en": "Coastal Trail", kind_detail: "path" }, 0, 0);
    expect(spec.title).toBe("Coastal Trail");
  });

  it("gives an unnamed path a friendly fallback title", () => {
    expect(roadPopupSpec({ kind_detail: "path" }, 0, 0).title).toBe("Trail");
    expect(roadPopupSpec({ kind_detail: "track" }, 0, 0).title).toBe("Forest road");
    expect(roadPopupSpec({ kind: "minor_road" }, 0, 0).title).toBe("Unnamed road");
  });

  it("flags bridges and tunnels (bool or 1)", () => {
    expect(roadPopupSpec({ kind_detail: "path", is_bridge: true }, 0, 0).lines).toContain("Bridge");
    expect(roadPopupSpec({ kind_detail: "path", is_tunnel: 1 }, 0, 0).lines).toContain("Tunnel");
  });

  it("links to the click location on OSM (tiles drop the way id), new tab", () => {
    const spec = roadPopupSpec({ kind_detail: "track" }, 41.30512, -124.02311);
    expect(spec.link?.href).toBe("https://www.openstreetmap.org/#map=17/41.30512/-124.02311");
    expect(spec.link?.text).toMatch(/OpenStreetMap/);
  });

  it("title-cases an unmapped highway value", () => {
    const spec = roadPopupSpec({ kind_detail: "busway" }, 0, 0);
    expect(spec.lines?.[0]).toBe("Busway road (highway=busway)");
  });
});
