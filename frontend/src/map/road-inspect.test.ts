import { describe, expect, it } from "vitest";

import {
  enrichedRoadPopupSpec,
  type NearbyTrail,
  pickNearbyTrail,
  pickRoadFeature,
  roadLineLayerIds,
  roadPopupSpec,
  shouldEnrichRoad,
} from "./road-inspect";

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

  it("drops the redundant prose line for an unnamed way - just the bare highway tag", () => {
    expect(roadPopupSpec({ kind_detail: "track" }, 0, 0).lines).toEqual(["highway=track"]);
    // a named way still spells the class out (the title carries the name, not the type)
    expect(roadPopupSpec({ name: "Davison Road", kind_detail: "track" }, 0, 0).lines).toEqual([
      "Forest / logging road (highway=track)",
    ]);
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

  it("title-cases an unmapped highway value for a named way", () => {
    const spec = roadPopupSpec({ name: "Busway 1", kind_detail: "busway" }, 0, 0);
    expect(spec.lines?.[0]).toBe("Busway road (highway=busway)");
  });
});

describe("shouldEnrichRoad", () => {
  it("enriches an unnamed track or foot/path kind", () => {
    expect(shouldEnrichRoad({ kind_detail: "track" })).toBe(true);
    expect(shouldEnrichRoad({ kind_detail: "path" })).toBe(true);
    expect(shouldEnrichRoad({ kind_detail: "footway" })).toBe(true);
    expect(shouldEnrichRoad({ kind: "path" })).toBe(true);
  });

  it("skips a named way or a class we don't ingest", () => {
    expect(shouldEnrichRoad({ name: "Davison Trail", kind_detail: "track" })).toBe(false);
    expect(shouldEnrichRoad({ "name:en": "X", kind_detail: "path" })).toBe(false);
    expect(shouldEnrichRoad({ kind: "minor_road", kind_detail: "residential" })).toBe(false);
    expect(shouldEnrichRoad({ kind_detail: "trunk" })).toBe(false);
  });
});

describe("pickNearbyTrail", () => {
  const row = (kind: string, distance_km: number): NearbyTrail => ({
    name: "x",
    kind,
    url: "u",
    distance_km,
  });

  it("takes the nearest row that is a way, skipping trailhead nodes and route relations", () => {
    const rows = [row("trailhead", 0.01), row("route", 0.02), row("road", 0.03), row("path", 0.04)];
    expect(pickNearbyTrail(rows)?.kind).toBe("road");
  });

  it("returns undefined when nothing is a road or path", () => {
    expect(pickNearbyTrail([row("trailhead", 0.01)])).toBeUndefined();
  });
});

describe("enrichedRoadPopupSpec", () => {
  const near = (over: Partial<NearbyTrail> = {}): NearbyTrail => ({
    name: "Lost Man Creek Road",
    kind: "road",
    url: "https://www.openstreetmap.org/way/12264948",
    distance_km: 0.02,
    attrs: { ref: "12N01", surface: "compacted", tracktype: "grade3" },
    ...over,
  });

  it("returns null when there is no hit or it is too far to be the clicked way", () => {
    expect(enrichedRoadPopupSpec({ kind_detail: "track" }, 0, 0, undefined)).toBeNull();
    expect(enrichedRoadPopupSpec({ kind_detail: "track" }, 0, 0, near({ distance_km: 0.4 }))).toBeNull();
  });

  it("uses the real name, road number, surface, grade and the actual OSM way link", () => {
    const spec = enrichedRoadPopupSpec({ kind_detail: "track" }, 41.3, -124.0, near())!;
    expect(spec.title).toBe("Lost Man Creek Road");
    expect(spec.lines).toEqual([
      "Forest / logging road (highway=track)",
      "Ref 12N01",
      "Surface: compacted",
      "Grade: grade 3",
    ]);
    expect(spec.link?.href).toBe("https://www.openstreetmap.org/way/12264948");
  });

  it("falls back to the road number as the title when the cached name is synthetic", () => {
    const spec = enrichedRoadPopupSpec({ kind_detail: "track" }, 0, 0, near({ name: "Forest road (OSM)" }))!;
    expect(spec.title).toBe("Ref 12N01");
  });

  it("notes a walk-in (vehicle-gated) forest road", () => {
    const spec = enrichedRoadPopupSpec(
      { kind_detail: "track" },
      0,
      0,
      near({ walk_in: true, attrs: { ref: "300", motor_vehicle: "no" } }),
    )!;
    expect(spec.lines).toContain("Walk-in (gated to vehicles)");
  });

  it("notes the foraging-record count when the way carries one", () => {
    const spec = enrichedRoadPopupSpec({ kind_detail: "track" }, 0, 0, near({ forage_obs: 23 }))!;
    expect(spec.lines).toContain("23 fungi records within ~500 m");
    // absent / zero -> no line
    const none = enrichedRoadPopupSpec({ kind_detail: "track" }, 0, 0, near({ forage_obs: 0 }))!;
    expect((none.lines ?? []).some((l) => l.includes("fungi records"))).toBe(false);
  });
});
