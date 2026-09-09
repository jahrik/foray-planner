// Foray's cartographic override for the Protomaps base theme's road layers. Runs on top of the
// contrast pass in `basemap-theme.ts`.
//
// The stock theme lumps every unpaved way - logging roads, ATV tracks, hiking trails, game
// paths - into one dim grey dashed layer (`roads_other`). For a forager the emphasis is wanted
// the other way round: the drive-in forest roads and the walk-in trails are the whole point of
// the map. Protomaps carries the original OSM `highway=*` value in `kind_detail`, so this
// splits `roads_other` into a bright-ochre cased "forest roads" layer (`highway=track`) and a
// bright-green cased "trails" layer (footway/path/bridleway/steps/cycleway), each with its own
// line-following label. Protomaps' vector tiles carry no track/path geometry below ~z13, so
// these only appear once you zoom into an area - there is no data to style at regional zoom.

import type {
  ExpressionSpecification,
  FilterSpecification,
  LayerSpecification,
} from "@maplibre/maplibre-gl-style-spec";

// `kind_detail` values (raw OSM highway tag) that count as a walk-in trail rather than a
// drive-in forest road. `track` is handled on its own as the forest-road class.
const PATH_KIND_DETAIL = ["path", "footway", "bridleway", "steps", "cycleway"];

// The base theme layer we replace, plus its label layer we narrow.
const BASE_SURFACE_ID = "roads_other";
const BASE_MINOR_LABEL_ID = "roads_labels_minor";

interface RoadPalette {
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
// rock, or a paved road it runs beside.
const PALETTE: Record<"dark" | "light", RoadPalette> = {
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

// Protomaps tiles carry no track/path geometry below ~z13 (checked against the CONUS archive),
// so these only render once zoomed into an area - widths ramp up fast from there. Stored as
// [zoom, width] stops so the casing can be built as its own top-level interpolate: MapLibre
// rejects a ["zoom"] expression nested inside anything but a top-level step/interpolate, so
// "line + a constant" is not an option for the casing width.
const TRACK_STOPS: ReadonlyArray<readonly [number, number]> = [
  [12, 0.9],
  [14, 2.2],
  [16, 3.8],
  [18, 6.5],
  [20, 12],
];
const PATH_STOPS: ReadonlyArray<readonly [number, number]> = [
  [13, 0.9],
  [15, 2],
  [17, 3.2],
  [20, 6.5],
];

// Build a zoom-interpolated width from stops, optionally widened by `delta` at every stop (the
// casing is a hair wider than the line it sits under - reads as an edge, not a second line).
function widthExpr(stops: ReadonlyArray<readonly [number, number]>, delta = 0): ExpressionSpecification {
  const pairs = stops.flatMap(([zoom, width]) => [zoom, width + delta]);
  return ["interpolate", ["exponential", 1.5], ["zoom"], ...pairs] as ExpressionSpecification;
}

const trackWidth = widthExpr(TRACK_STOPS);
const pathWidth = widthExpr(PATH_STOPS);
const CASING_DELTA = 1.5;

// The under-line that gives a class its edge. Same filter/zoom as the main line, drawn first.
function classCasing(
  id: string,
  filter: FilterSpecification,
  minzoom: number,
  color: string,
  stops: ReadonlyArray<readonly [number, number]>,
): LayerSpecification {
  return {
    id,
    type: "line",
    source: "protomaps",
    "source-layer": "roads",
    minzoom,
    filter,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": color, "line-width": widthExpr(stops, CASING_DELTA) },
  } as LayerSpecification;
}

// A line-placed label for one of the foray road classes. Uses a plain name coalesce rather
// than the base theme's full localization cascade - the archive is US-only.
function classLabel(
  id: string,
  filter: FilterSpecification,
  minzoom: number,
  color: string,
  halo: string,
): LayerSpecification {
  return {
    id,
    type: "symbol",
    source: "protomaps",
    "source-layer": "roads",
    minzoom,
    filter,
    layout: {
      "symbol-placement": "line",
      "text-font": ["Noto Sans Regular"],
      "text-field": ["coalesce", ["get", "name:en"], ["get", "name"]],
      "text-size": 11,
    },
    paint: {
      "text-color": color,
      "text-halo-color": halo,
      "text-halo-width": 1.25,
    },
  } as LayerSpecification;
}

/**
 * Return a new layer array with the base theme's single `roads_other` layer replaced by
 * distinct forest-road and trail layers (each cased, each with a label), and the base
 * minor-road label layer narrowed so it no longer double-labels those ways.
 *
 * Pure: does not mutate `base`.
 */
export function applyForayRoadStyle(
  base: LayerSpecification[],
  theme: "dark" | "light",
): LayerSpecification[] {
  const palette = PALETTE[theme];
  // `*Filter` matches the class anywhere (used by the label layers); `*Surface` additionally
  // excludes tunnels/bridges (the line + casing layers - the base theme draws those variants).
  const trackFilter: ExpressionSpecification = ["==", ["get", "kind_detail"], "track"];
  const pathFilter: ExpressionSpecification = ["in", ["get", "kind_detail"], ["literal", PATH_KIND_DETAIL]];
  const surface = (kindFilter: ExpressionSpecification): ExpressionSpecification => [
    "all",
    ["!", ["has", "is_tunnel"]],
    ["!", ["has", "is_bridge"]],
    kindFilter,
  ];
  const trackSurface = surface(trackFilter);
  const pathSurface = surface(pathFilter);

  const trackCasing = classCasing(
    "roads_foray_track_casing",
    trackSurface,
    12,
    palette.trackCasing,
    TRACK_STOPS,
  );
  const trackLine = {
    id: "roads_foray_track",
    type: "line",
    source: "protomaps",
    "source-layer": "roads",
    minzoom: 12,
    filter: trackSurface,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": palette.track,
      "line-dasharray": [3, 1.3],
      "line-width": trackWidth,
    },
  } as LayerSpecification;

  const pathCasing = classCasing("roads_foray_path_casing", pathSurface, 13, palette.pathCasing, PATH_STOPS);
  const pathLine = {
    id: "roads_foray_path",
    type: "line",
    source: "protomaps",
    "source-layer": "roads",
    minzoom: 13,
    filter: pathSurface,
    layout: { "line-cap": "round" },
    paint: {
      "line-color": palette.path,
      "line-dasharray": [1.6, 1.4],
      "line-width": pathWidth,
    },
  } as LayerSpecification;

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

  const trackLabels = classLabel(
    "roads_foray_track_labels",
    trackFilter,
    13,
    palette.trackLabel,
    palette.labelHalo,
  );
  const pathLabels = classLabel(
    "roads_foray_path_labels",
    pathFilter,
    14,
    palette.pathLabel,
    palette.labelHalo,
  );

  let insertedLines = false;
  let insertedLabels = false;
  const out: LayerSpecification[] = [];
  for (const layer of base) {
    if (layer.id === BASE_SURFACE_ID) {
      out.push(trackCasing, trackLine, pathCasing, pathLine, misc);
      insertedLines = true;
      continue;
    }
    if (layer.id === BASE_MINOR_LABEL_ID) {
      // The base layer labels minor_road + other + path from z15; drop other/path so it
      // stops competing with the two foray label layers, then add ours right after it.
      out.push({ ...layer, filter: ["==", ["get", "kind"], "minor_road"] } as LayerSpecification);
      out.push(trackLabels, pathLabels);
      insertedLabels = true;
      continue;
    }
    out.push(layer);
  }
  // Guard against a future protomaps-themes-base renaming either anchor: append rather than
  // silently drop the foray layers. Lines before labels keeps them under the label symbols.
  if (!insertedLines) out.push(trackCasing, trackLine, pathCasing, pathLine, misc);
  if (!insertedLabels) out.push(trackLabels, pathLabels);
  return out;
}
