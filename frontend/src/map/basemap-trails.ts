// Our own trails vector tiles (issue #336 PR 1), proxied same-origin from the martin tile
// server (api/routes/tiles.py). Protomaps' base tiles carry no track/path geometry below ~z13
// (basemap-roads.ts), so below that zoom this is the only trail/forest-road geometry on the
// map at all - and even above z13 it carries the national USFS/OSM coverage the base tiles
// don't (issue #335).
//
// First PR: stand up the source + a plain line layer so the plumbing is provably working end to
// end. Styling parity with basemap-roads.ts's track/path split, promoteId click-to-select, and
// retiring the GeoJSON `/api/trails` fallback lookup are issue #336 PR 2 - this layer is
// additive and doesn't touch anything that reads from `/api/trails` today.

import type { LayerSpecification, SourceSpecification } from "@maplibre/maplibre-gl-style-spec";

export const TRAILS_SOURCE_ID = "foray-trails";
const TRAILS_LAYER_ID = "foray_trails";
// martin names the vector tile's source-layer after the published table (infra/martin-config.yaml).
const TRAILS_SOURCE_LAYER = "trails";

// Same path green as basemap-roads.ts's PALETTE, so this reads as one continuous trail network
// rather than two differently-colored layers stitched at z13.
const LINE_COLOR: Record<"dark" | "light", string> = {
  dark: "#8fd06f",
  light: "#3f7a2c",
};

/** The trails MVT source, proxied same-origin (see api/routes/tiles.py). */
export function trailsSource(tilesUrl: string): Record<string, SourceSpecification> {
  return {
    [TRAILS_SOURCE_ID]: {
      type: "vector",
      tiles: [tilesUrl],
      minzoom: 8,
      maxzoom: 14,
    },
  } as Record<string, SourceSpecification>;
}

/** `kind` is "path" | "route" | "trailhead" (trails.py) - trailhead rows are Points, not lines;
 * skipped here since there's no click-to-select or icon for them yet (PR 2). */
export function trailsLayer(theme: "dark" | "light"): LayerSpecification {
  return {
    id: TRAILS_LAYER_ID,
    type: "line",
    source: TRAILS_SOURCE_ID,
    "source-layer": TRAILS_SOURCE_LAYER,
    filter: ["!=", ["get", "kind"], "trailhead"],
    paint: {
      "line-color": LINE_COLOR[theme],
      "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.5, 14, 1.5],
      "line-opacity": 0.8,
    },
  } as LayerSpecification;
}
