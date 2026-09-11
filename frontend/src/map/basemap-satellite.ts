// The full-viewport satellite basemap (issue #340): a real Esri World Imagery raster in place
// of the vector map's land/water fills, with our *own* vector roads/boundaries/labels drawn on
// top - not a second, separately-styled label set from Esri. That reuses the exact theming
// `basemap-theme.ts` + `basemap-roads.ts` already produce for the vector map instead of
// maintaining a second look, and stays legible in both light and dark mode for free.

import type { LayerSpecification, SourceSpecification } from "@maplibre/maplibre-gl-style-spec";

export const SATELLITE_SOURCE_ID = "satellite-imagery";
const SATELLITE_LAYER_ID = "satellite_imagery";

/** The Esri imagery raster source, proxied same-origin (see api/routes/tiles.py). */
export function satelliteSource(tilesUrl: string): Record<string, SourceSpecification> {
  return {
    [SATELLITE_SOURCE_ID]: {
      type: "raster",
      tiles: [tilesUrl],
      tileSize: 256,
    },
  } as Record<string, SourceSpecification>;
}

export function satelliteImageryLayer(): LayerSpecification {
  return {
    id: SATELLITE_LAYER_ID,
    type: "raster",
    source: SATELLITE_SOURCE_ID,
  } as LayerSpecification;
}

/**
 * Filter a themed vector layer list (`applyForayRoadStyle(themedBaseLayers(theme), theme)`,
 * built exactly once, the same call `buildStyle` already makes) down to the layers that make
 * sense drawn over a photo instead of the vector map's own land fill: roads, boundaries, and
 * every label. Drops `background` and `fill` layers (earth, landcover, water, parks) - those
 * would just paint flat color over the imagery, hiding it. Pure: does not mutate `layers`.
 */
export function roadsAndLabelsOnly(layers: LayerSpecification[]): LayerSpecification[] {
  return layers.filter((layer) => layer.type !== "background" && layer.type !== "fill");
}
