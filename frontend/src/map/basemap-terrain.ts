// Terrain layers for the vector basemap: a DEM hillshade (always on) and contour lines +
// elevation labels (behind the Layers-pill "Contours" toggle, off by default). Runs on top of
// the contrast pass (`basemap-theme.ts`) and the road styling (`basemap-roads.ts`).
//
// Both are driven by Terrarium-encoded DEM raster tiles (`state.terrainUrl`, AWS Open Data's
// elevation-tiles-prod by default). The hillshade uses MapLibre's native `hillshade` layer;
// the contour lines are vector tiles generated in the browser by `maplibre-contour` from the
// same DEM (see `basemap.ts` for the `DemSource` wiring). 2D only - no MapLibre 3D `terrain`,
// which fights the Leaflet 2D pane sync in `@maplibre/maplibre-gl-leaflet`.

import type { LayerSpecification, SourceSpecification } from "@maplibre/maplibre-gl-style-spec";

export const DEM_SOURCE_ID = "terrain-dem";
export const CONTOUR_SOURCE_ID = "terrain-contour";
export const HILLSHADE_LAYER_ID = "terrain_hillshade";
export const CONTOUR_LINE_LAYER_ID = "terrain_contour";
export const CONTOUR_LABEL_LAYER_ID = "terrain_contour_label";
/** The contour layers the "Contours" toggle shows/hides. Hillshade is always on. */
export const CONTOUR_LAYER_IDS = [CONTOUR_LINE_LAYER_ID, CONTOUR_LABEL_LAYER_ID];

// The DEM composite is USGS 3DEP ~10 m over CONUS, native ~z13-15; MapLibre overzooms above
// this and `maplibre-contour` overzooms internally for contour extraction.
export const DEM_MAX_ZOOM = 15;

// Zoom -> [minor, major] contour interval in metres. maplibre-contour tags each line with
// `level` (0 = minor, 1 = a multiple of the major interval) and `ele` (metres). Coarser
// intervals when zoomed out so the lines stay readable; nothing below z12 (noise at regional
// zoom). A zoom with no entry inherits the next lower one's thresholds.
export const CONTOUR_THRESHOLDS: Record<number, [number, number]> = {
  11: [200, 1000],
  12: [100, 500],
  13: [50, 250],
  14: [20, 100],
  15: [10, 50],
};

interface TerrainPalette {
  shadow: string;
  highlight: string;
  accent: string;
  exaggeration: number;
  contour: string;
  contourLabel: string;
  contourHalo: string;
}

const PALETTE: Record<"dark" | "light", TerrainPalette> = {
  dark: {
    // Deepen valleys without washing out the D2 contrast pass - a shadow that reads as
    // "darker ground" rather than a grey film, barely any highlight.
    shadow: "#0a0d0c",
    highlight: "#3a4038",
    accent: "#12331f",
    exaggeration: 0.75,
    contour: "rgba(214, 170, 120, 0.42)",
    contourLabel: "#d8b482",
    contourHalo: "#141414",
  },
  light: {
    shadow: "#6b6459",
    highlight: "#fffdf7",
    accent: "#8f8674",
    exaggeration: 0.9,
    contour: "rgba(120, 92, 54, 0.4)",
    contourLabel: "#6b4c22",
    contourHalo: "#f7f7f4",
  },
};

/**
 * The `raster-dem` + contour vector sources. `demTilesUrl` is a `{z}/{x}/{y}` template (the
 * raw DEM URL, or maplibre-contour's shared-DEM protocol URL so hillshade and contours reuse
 * decoded tiles). `contourTilesUrl` comes from `DemSource.contourProtocolUrl(...)`.
 */
export function terrainSources(
  demTilesUrl: string,
  contourTilesUrl: string,
): Record<string, SourceSpecification> {
  return {
    [DEM_SOURCE_ID]: {
      type: "raster-dem",
      tiles: [demTilesUrl],
      encoding: "terrarium",
      tileSize: 256,
      maxzoom: DEM_MAX_ZOOM,
    },
    [CONTOUR_SOURCE_ID]: {
      type: "vector",
      tiles: [contourTilesUrl],
      maxzoom: DEM_MAX_ZOOM,
    },
  } as Record<string, SourceSpecification>;
}

// At regional zooms (looking for a mountain from far away) the DEM is heavily overzoomed and
// per-pixel relief washes out, so ramp exaggeration up well past the palette's base value the
// further out you are; it eases back down to the tuned base by the zoom where the DEM has real
// native resolution (DEM_MAX_ZOOM territory).
function exaggerationExpression(base: number) {
  return ["interpolate", ["linear"], ["zoom"], 6, base * 2.4, 9, base * 1.7, 13, base];
}

function hillshadeLayer(palette: TerrainPalette): LayerSpecification {
  return {
    id: HILLSHADE_LAYER_ID,
    type: "hillshade",
    source: DEM_SOURCE_ID,
    paint: {
      "hillshade-exaggeration": exaggerationExpression(palette.exaggeration),
      "hillshade-shadow-color": palette.shadow,
      "hillshade-highlight-color": palette.highlight,
      "hillshade-accent-color": palette.accent,
      "hillshade-illumination-direction": 315,
      "hillshade-method": "igor",
    },
  } as LayerSpecification;
}

function contourLineLayer(palette: TerrainPalette, visible: boolean): LayerSpecification {
  return {
    id: CONTOUR_LINE_LAYER_ID,
    type: "line",
    source: CONTOUR_SOURCE_ID,
    "source-layer": "contours",
    minzoom: 12,
    layout: { visibility: visible ? "visible" : "none", "line-join": "round" },
    paint: {
      "line-color": palette.contour,
      // major lines a touch heavier so the reader can count the interval
      "line-width": ["match", ["get", "level"], 1, 1.1, 0.55],
    },
  } as LayerSpecification;
}

function contourLabelLayer(palette: TerrainPalette, visible: boolean): LayerSpecification {
  return {
    id: CONTOUR_LABEL_LAYER_ID,
    type: "symbol",
    source: CONTOUR_SOURCE_ID,
    "source-layer": "contours",
    minzoom: 13,
    filter: ["==", ["get", "level"], 1],
    layout: {
      visibility: visible ? "visible" : "none",
      "symbol-placement": "line",
      "text-font": ["Noto Sans Regular"],
      "text-field": ["concat", ["to-string", ["get", "ele"]], " m"],
      "text-size": 10,
      "symbol-spacing": 320,
    },
    paint: {
      "text-color": palette.contourLabel,
      "text-halo-color": palette.contourHalo,
      "text-halo-width": 1,
    },
  } as LayerSpecification;
}

/**
 * Splice the hillshade + contour layers into a base layer list: hillshade just below the
 * first `water` layer (above the earth + landcover fills, under water, roads and labels) and
 * the contour line + label just below the first `roads` layer. `contoursVisible` bakes the
 * "Contours" toggle state into the layers so a theme swap (which rebuilds the whole style)
 * keeps them showing. Pure: does not mutate `base`.
 *
 * `includeHillshade` (default `true`) is `false` for the satellite basemap (issue #340): real
 * photographic shadows already show relief, so compositing synthetic hillshade on top would
 * just wash out the imagery's true colors. Contours stay on either way - they're still useful
 * elevation information over a photo.
 *
 * If an anchor is missing (a future protomaps-themes-base rename), the layer falls back to a
 * sane index near the bottom of the stack rather than being dropped.
 */
export function applyTerrainLayers(
  base: LayerSpecification[],
  theme: "dark" | "light",
  contoursVisible = false,
  includeHillshade = true,
): LayerSpecification[] {
  const palette = PALETTE[theme];
  const hillshade = includeHillshade ? hillshadeLayer(palette) : null;
  const contourLine = contourLineLayer(palette, contoursVisible);
  const contourLabel = contourLabelLayer(palette, contoursVisible);

  const hillshadeAnchor = hillshade ? anchorIndex(base, /^water/, 2) : -1;
  const contourAnchor = anchorIndex(base, /^roads/, base.length);

  const out: LayerSpecification[] = [];
  let hillshadeDone = !hillshade;
  let contoursDone = false;
  base.forEach((layer, index) => {
    if (hillshade && index === hillshadeAnchor) {
      out.push(hillshade);
      hillshadeDone = true;
    }
    if (index === contourAnchor) {
      out.push(contourLine, contourLabel);
      contoursDone = true;
    }
    out.push(layer);
  });
  if (hillshade && !hillshadeDone) out.push(hillshade);
  if (!contoursDone) out.push(contourLine, contourLabel);
  return out;
}

/** First index whose layer id matches `pattern`, or `fallback` clamped to [1, length]. */
function anchorIndex(layers: LayerSpecification[], pattern: RegExp, fallback: number): number {
  const found = layers.findIndex((layer) => pattern.test(layer.id));
  return found !== -1 ? found : Math.min(Math.max(fallback, 1), layers.length);
}
