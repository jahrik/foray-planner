// Our own trails vector tiles (issue #336 PR 1), proxied same-origin from the martin tile
// server (api/routes/tiles.py) - the single source for trails and forest roads on the map at
// every zoom: OSM paths/forest roads plus the USFS Trail_NFS and MVUM records, deduped
// server-side (`cache.prune_duplicate_cross_source_*`). basemap-roads.ts drops the Protomaps
// base theme's own copies of those ways, which used to be drawn alongside this layer from z12 -
// every road twice, and a colour switch between zoom levels.
//
// `promoteId` (PR 2) lets a click on these layers read its properties keyed by the trail's own
// id, the same id `/api/trails/network` takes - our own tile already carries the real
// name/kind/land_agency/forage_obs at every zoom.

import type {
  ExpressionSpecification,
  LayerSpecification,
  SourceSpecification,
} from "@maplibre/maplibre-gl-style-spec";

import { CASING_DELTA, PALETTE, PATH_STOPS, TRACK_STOPS, widthExpr } from "./basemap-roads";
import { directionsLink } from "./directions";
import type { PopupSpec } from "./popup";

export const TRAILS_SOURCE_ID = "foray-trails";
// The two clickable line layers (map inspect hit-tests these); casings and labels echo them.
const ROAD_LAYER_ID = "foray_trails_road";
const PATH_LAYER_ID = "foray_trails_path";
export const TRAILS_LINE_LAYER_IDS: readonly string[] = [ROAD_LAYER_ID, PATH_LAYER_ID];
// martin names the vector tile's source-layer after the published table (infra/martin-config.yaml).
const TRAILS_SOURCE_LAYER = "trails";

/** The tile properties a `foray_trails` feature carries (martin-config.yaml's `trails` table
 * properties) - read straight off a click hit (inspect.ts). `kind` is "path" | "route" | "road"
 * ("trailhead" rows are Points, not drawn by `trailsLayers` below). */
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

// Forest roads (`kind='road'` - OSM highway=track / service=forestry, and USFS MVUM) vs. walk-in
// trails (`path` / `route` - OSM and USFS Trail_NFS). Trailheads are Points, not drawn here.
const ROAD_FILTER: ExpressionSpecification = ["==", ["get", "kind"], "road"];
const PATH_FILTER: ExpressionSpecification = ["in", ["get", "kind"], ["literal", ["path", "route"]]];

// Solid at regional zoom (dashes on a sub-pixel line just read as noise), dashed once a road
// or trail is wide enough to carry the pattern - the paper-map convention for unpaved ways.
function dashes(dash: [number, number]): ExpressionSpecification {
  return ["step", ["zoom"], ["literal", [1, 0]], 12, ["literal", dash]] as ExpressionSpecification;
}

function line(
  id: string,
  filter: ExpressionSpecification,
  color: string,
  stops: ReadonlyArray<readonly [number, number]>,
  dash: [number, number],
): LayerSpecification {
  return {
    id,
    type: "line",
    source: TRAILS_SOURCE_ID,
    "source-layer": TRAILS_SOURCE_LAYER,
    filter,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": color,
      "line-width": widthExpr(stops),
      "line-dasharray": dashes(dash),
      "line-opacity": ["interpolate", ["linear"], ["zoom"], 8, 0.8, 12, 1],
    },
  } as LayerSpecification;
}

// The under-line that gives a class its edge over forest fill, water or a paved road beside it.
// Only from z12 - at regional zoom a casing just muddies a hairline.
function casing(
  id: string,
  filter: ExpressionSpecification,
  color: string,
  stops: ReadonlyArray<readonly [number, number]>,
): LayerSpecification {
  return {
    id,
    type: "line",
    source: TRAILS_SOURCE_ID,
    "source-layer": TRAILS_SOURCE_LAYER,
    minzoom: 12,
    filter,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": color, "line-width": widthExpr(stops, CASING_DELTA) },
  } as LayerSpecification;
}

// A line-placed label. Unnamed OSM ways carry a synthetic "Trail (OSM)" / "Forest road (OSM)"
// name (trails.py) that says nothing on the map, so those stay unlabelled.
function label(
  id: string,
  filter: ExpressionSpecification,
  minzoom: number,
  color: string,
  halo: string,
): LayerSpecification {
  return {
    id,
    type: "symbol",
    source: TRAILS_SOURCE_ID,
    "source-layer": TRAILS_SOURCE_LAYER,
    minzoom,
    filter: ["all", filter, ["has", "name"], ["!", ["in", "(OSM)", ["get", "name"]]]],
    layout: {
      "symbol-placement": "line",
      "text-font": ["Noto Sans Regular"],
      "text-field": ["get", "name"],
      "text-size": 11,
    },
    paint: { "text-color": color, "text-halo-color": halo, "text-halo-width": 1.25 },
  } as LayerSpecification;
}

/** The trail + forest-road layers, bottom to top: casings, lines, labels. The single source for
 * both classes at every zoom - basemap-roads.ts drops the base theme's own copies. */
export function trailsLayers(theme: "dark" | "light"): LayerSpecification[] {
  const palette = PALETTE[theme];
  return [
    casing("foray_trails_road_casing", ROAD_FILTER, palette.trackCasing, TRACK_STOPS),
    line(ROAD_LAYER_ID, ROAD_FILTER, palette.track, TRACK_STOPS, [3, 1.3]),
    casing("foray_trails_path_casing", PATH_FILTER, palette.pathCasing, PATH_STOPS),
    line(PATH_LAYER_ID, PATH_FILTER, palette.path, PATH_STOPS, [1.6, 1.4]),
    label("foray_trails_road_labels", ROAD_FILTER, 13, palette.trackLabel, palette.labelHalo),
    label("foray_trails_path_labels", PATH_FILTER, 14, palette.pathLabel, palette.labelHalo),
  ];
}

const KIND_LABEL: Record<string, string> = {
  path: "Trail",
  route: "Route",
  road: "Forest road",
};

/** Popup contents straight off the tile - name/kind/length/land unit the martin-config.yaml
 * `trails` table already carries, no `/api/trails/network` round-trip needed just to show what
 * was clicked (map.ts's click-to-inspect, same pattern as basemap-land.ts/basemap-fire.ts).
 * `lat`/`lng` (the click point) are optional so existing tests that don't care about the
 * Directions link don't need to pass them - map.ts's real caller always does (issue #310). */
export function trailPopupSpec(props: TrailTileProps, lat?: number, lng?: number): PopupSpec {
  const lines: string[] = [];
  const kindLabel = props.kind ? (KIND_LABEL[props.kind] ?? props.kind) : "Trail";
  const bits = [kindLabel];
  if (props.length_km != null) bits.push(`${props.length_km.toFixed(1)} km`);
  lines.push(bits.join(" · "));
  if (props.land_unit) lines.push(props.land_unit);
  else if (props.land_agency) lines.push(props.land_agency);
  const title = props.name ?? "Unnamed trail";
  const directions = lat != null && lng != null ? directionsLink(lat, lng, title) : undefined;
  return { title, lines, ...(directions ? { directions } : {}) };
}
