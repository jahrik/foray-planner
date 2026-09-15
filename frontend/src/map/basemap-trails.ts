// Our own trails vector tiles (issue #336 PR 1), proxied same-origin from the martin tile
// server (api/routes/tiles.py). Protomaps' base tiles carry no track/path geometry below ~z13
// (basemap-roads.ts), so below that zoom this is the only trail/forest-road geometry on the
// map at all - and even above z13 it carries the national USFS/OSM coverage the base tiles
// don't (issue #335).
//
// First PR stood up the source + a plain line layer so the plumbing was provably working end to
// end. `promoteId` (PR 2) lets a click on this layer read its properties keyed by the trail's
// own id (road-inspect.ts), the same id `/api/trails/network` takes - no more falling back to a
// live `GET /api/trails` name lookup (the old #325 fallback) since our own tile already carries
// the real name/kind/land_agency/forage_obs at every zoom.

import type { LayerSpecification, SourceSpecification } from "@maplibre/maplibre-gl-style-spec";

export const TRAILS_SOURCE_ID = "foray-trails";
export const TRAILS_LAYER_ID = "foray_trails";
// martin names the vector tile's source-layer after the published table (infra/martin-config.yaml).
const TRAILS_SOURCE_LAYER = "trails";

/** The tile properties a `foray_trails` feature carries (martin-config.yaml's `trails` table
 * properties) - read straight off a click hit (map.ts's click-to-select, road-inspect.ts's
 * neighbour for Protomaps roads). `kind` is "path" | "route" ("trailhead" rows are filtered out
 * of this layer, see `trailsLayer` below - they're Points, with no click-to-select story yet). */
export interface TrailTileProps {
  id?: string;
  name?: string;
  kind?: string;
  source?: string;
  length_km?: number | null;
  land_agency?: string | null;
  land_unit?: string | null;
  forage_obs?: number | null;
}

// Same path green as basemap-roads.ts's PALETTE, so this reads as one continuous trail network
// rather than two differently-colored layers stitched at z13.
const LINE_COLOR: Record<"dark" | "light", string> = {
  dark: "#8fd06f",
  light: "#3f7a2c",
};

/** The trails MVT source, proxied same-origin (see api/routes/tiles.py). `promoteId` maps the
 * MVT's numeric feature id to the `id` property (trails.id is text, e.g. "osm:way/42" - martin's
 * MVT feature id column must be integer, so martin-config.yaml carries it as a plain property
 * instead) - lets `queryRenderedFeatures` hits carry a stable `feature.id` for click-to-select
 * without a second lookup. */
export function trailsSource(tilesUrl: string): Record<string, SourceSpecification> {
  return {
    [TRAILS_SOURCE_ID]: {
      type: "vector",
      tiles: [tilesUrl],
      minzoom: 8,
      maxzoom: 14,
      promoteId: "id",
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
