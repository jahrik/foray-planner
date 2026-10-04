// Foray's cartographic override for the Protomaps base theme's road layers. Runs on top of the
// contrast pass in `basemap-theme.ts`.
//
// Trails and forest roads come from exactly one source: our own `foray_trails` vector tiles
// (basemap-trails.ts - OSM paths/forest roads plus the USFS Trail_NFS and MVUM records, deduped
// server-side). The stock theme's `roads_other` layer draws OSM's own copy of those same ways
// (`highway=track` / path / footway / ...) from ~z12, which used to be re-styled here as an
// ochre forest-road layer and a green trail layer. With both drawn, every road appeared twice
// from z12 up (an MVUM line and an OSM line 10-100m apart) and the map switched sources as you
// zoomed - green tile lines at regional zoom, dashed basemap tracks once zoomed in. So this now
// drops those classes from the base theme and keeps only the remaining minor ways it lumps in.
// The colours and widths live here and basemap-trails.ts styles with them.

import type { ExpressionSpecification, LayerSpecification } from "@maplibre/maplibre-gl-style-spec";

// `kind_detail` values (raw OSM highway tag) the base theme draws that basemap-trails.ts's tile
// layer owns instead (with `track`) - dropped from the base so nothing is drawn twice.
const PATH_KIND_DETAIL = ["path", "footway", "bridleway", "steps", "cycleway"];

// The base theme layer we replace, plus its label layer we narrow.
const BASE_SURFACE_ID = "roads_other";
const BASE_MINOR_LABEL_ID = "roads_labels_minor";

export interface RoadPalette {
  track: string;
  trackCasing: string;
  trackLabel: string;
  path: string;
  pathCasing: string;
  pathLabel: string;
  labelHalo: string;
  misc: string;
}

// Track = ochre (a forest-service road on a paper quad), path = green. Bright enough to carry
// over the dark forest fill; each sits on a contrasting casing so it also reads over water,
// rock, or a paved road it runs beside. Exported so basemap-trails.ts's own tile layer colours
// the same classes the same way - one road network, not two palettes stitched at z12.
export const PALETTE: Record<"dark" | "light", RoadPalette> = {
  dark: {
    track: "#e0a458",
    trackCasing: "#1a1206",
    trackLabel: "#f0c078",
    path: "#8fd06f",
    pathCasing: "#11210b",
    pathLabel: "#abe08c",
    labelHalo: "#141414",
    misc: "#3a3a3a",
  },
  light: {
    track: "#b06a1e",
    trackCasing: "#fbf3e6",
    trackLabel: "#7d4f14",
    path: "#3f7a2c",
    pathCasing: "#f1f6ec",
    pathLabel: "#2f5f20",
    labelHalo: "#f7f7f4",
    misc: "#d0d0d0",
  },
};

// Line widths per class, from the trails tile source's minzoom (8) up. Stored as [zoom, width]
// stops so the casing can be built as its own top-level interpolate: MapLibre rejects a
// ["zoom"] expression nested inside anything but a top-level step/interpolate, so "line + a
// constant" is not an option for the casing width.
export const TRACK_STOPS: ReadonlyArray<readonly [number, number]> = [
  [8, 0.6],
  [12, 1.1],
  [14, 2.2],
  [16, 3.8],
  [18, 6.5],
  [20, 12],
];
export const PATH_STOPS: ReadonlyArray<readonly [number, number]> = [
  [8, 0.5],
  [12, 1],
  [14, 1.6],
  [17, 3.2],
  [20, 6.5],
];

// Build a zoom-interpolated width from stops, optionally widened by `delta` at every stop (the
// casing is a hair wider than the line it sits under - reads as an edge, not a second line).
export function widthExpr(
  stops: ReadonlyArray<readonly [number, number]>,
  delta = 0,
): ExpressionSpecification {
  const pairs = stops.flatMap(([zoom, width]) => [zoom, width + delta]);
  return ["interpolate", ["exponential", 1.5], ["zoom"], ...pairs] as ExpressionSpecification;
}

const pathWidth = widthExpr(PATH_STOPS);
export const CASING_DELTA = 1.5;

/**
 * Return a new layer array with the base theme's `roads_other` layer narrowed to the minor ways
 * basemap-trails.ts doesn't draw (forest roads and trails are dropped - the tile layer owns
 * them), and the base minor-road label layer narrowed so it no longer labels those ways either.
 *
 * Pure: does not mutate `base`.
 */
export function applyForayRoadStyle(
  base: LayerSpecification[],
  theme: "dark" | "light",
): LayerSpecification[] {
  const palette = PALETTE[theme];
  // Anything still matching the base `roads_other` filter but neither a track nor one of the
  // path kinds (mostly `kind_detail` absent) - keep it drawn so no way silently disappears.
  const misc = {
    id: "roads_foray_other",
    type: "line",
    source: "protomaps",
    "source-layer": "roads",
    minzoom: 13,
    filter: [
      "all",
      ["!", ["has", "is_tunnel"]],
      ["!", ["has", "is_bridge"]],
      ["in", ["get", "kind"], ["literal", ["other", "path"]]],
      ["!=", ["get", "kind_detail"], "pier"],
      ["!=", ["get", "kind_detail"], "track"],
      ["!", ["in", ["get", "kind_detail"], ["literal", PATH_KIND_DETAIL]]],
    ],
    paint: {
      "line-color": palette.misc,
      "line-dasharray": [3, 1],
      "line-width": pathWidth,
    },
  } as LayerSpecification;

  let inserted = false;
  const out: LayerSpecification[] = [];
  for (const layer of base) {
    if (layer.id === BASE_SURFACE_ID) {
      out.push(misc);
      inserted = true;
      continue;
    }
    if (layer.id === BASE_MINOR_LABEL_ID) {
      // The base layer labels minor_road + other + path from z15; drop other/path - the
      // trails tile layer labels its own lines.
      out.push({ ...layer, filter: ["==", ["get", "kind"], "minor_road"] } as LayerSpecification);
      continue;
    }
    out.push(layer);
  }
  // Guard against a future protomaps-themes-base renaming the anchor: append rather than
  // silently drop the misc layer.
  if (!inserted) out.push(misc);
  return out;
}
