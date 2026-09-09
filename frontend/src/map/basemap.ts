// The map's base layer: a self-hosted Protomaps PMTiles archive rendered by MapLibre GL,
// mounted inside the Leaflet map via @maplibre/maplibre-gl-leaflet. Activated when the server
// sends a `basemap_url` in /api/config; there is no raster fallback.
//
// Why vector: a raster base bakes every road and label into pixels we can only draw over. A
// vector base lets us style forest roads / trails / highways distinctly, declutter labels, and
// drop the maxZoom ceiling; dark mode is a real style, not a CSS invert() hack.
//
// PMTiles is a single-file archive of Mapbox Vector Tiles addressed by z/x/y over HTTP range
// requests, so the whole US basemap is one static object behind a CDN with no tile server.
// Glyphs and sprites come from Protomaps' small static assets site (a few MB, not the tiles);
// self-hosting those alongside the archive is a later step.
import type { StyleSpecification } from "@maplibre/maplibre-gl-style-spec";
import L from "leaflet";
import { addProtocol, setWorkerUrl } from "maplibre-gl";
// v6 ships the GL web worker as a sibling ESM module whose URL MapLibre derives from
// import.meta.url - a bundler can't rewrite that, so under Vite we must point MapLibre at the
// worker chunk once before the first map or it 404s and the style never loads (a blank base,
// no console error). ?worker&url routes the file through Vite's worker pipeline so the emitted
// chunk keeps its maplibre-gl-shared.mjs sibling; a plain ?url would drop it.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
// MapLibre's own stylesheet positions the GL canvas/controls inside its container; without it
// the basemap can lay out wrong even mounted through the Leaflet plugin.
import "maplibre-gl/dist/maplibre-gl.css";
import "@maplibre/maplibre-gl-leaflet";
// maplibre-contour generates contour vector tiles in the browser from the same DEM raster
// tiles the hillshade uses. Its worker is embedded as a blob: URL at import time (no bundler
// resolution needed), covered by the `worker-src blob:` the backend CSP already sets.
import mlcontour from "maplibre-contour";
import { Protocol } from "pmtiles";
import { applyForayRoadStyle } from "./basemap-roads";
import {
  applyTerrainLayers,
  CONTOUR_LAYER_IDS,
  CONTOUR_THRESHOLDS,
  DEM_MAX_ZOOM,
  terrainSources,
} from "./basemap-terrain";
import { themedBaseLayers } from "./basemap-theme";

setWorkerUrl(workerUrl);

const ASSETS = "https://protomaps.github.io/basemaps-assets";
const ATTRIBUTION =
  '<a href="https://protomaps.com">Protomaps</a> · © <a href="https://openstreetmap.org">OpenStreetMap</a>';

let protocolRegistered = false;
let glLayer: L.MaplibreGL | null = null;

// One DemSource for the page: it registers the maplibre-contour protocols once and caches
// decoded DEM tiles across the hillshade and the contour lines. `contoursVisible` is tracked
// here so a theme swap (which rebuilds the whole style) can re-bake the toggle state.
let demSource: InstanceType<typeof mlcontour.DemSource> | null = null;
let contoursVisible = false;

/** The URL pattern the contour vector source pulls from - encodes the per-zoom thresholds. */
function contourTilesUrl(source: InstanceType<typeof mlcontour.DemSource>): string {
  return source.contourProtocolUrl({
    thresholds: CONTOUR_THRESHOLDS,
    // major lines get level 1; keep contours off the tile edges
    contourLayer: "contours",
    elevationKey: "ele",
    levelKey: "level",
    overzoom: 1,
  });
}

/** Create the DemSource + register its protocols once, for a given DEM tile URL template. */
function ensureDemSource(terrainUrl: string): InstanceType<typeof mlcontour.DemSource> {
  if (!demSource) {
    demSource = new mlcontour.DemSource({
      url: terrainUrl,
      encoding: "terrarium",
      maxzoom: DEM_MAX_ZOOM,
      worker: true,
    });
    demSource.setupMaplibre({ addProtocol });
  }
  return demSource;
}

function buildStyle(url: string, terrainUrl: string, theme: "dark" | "light"): StyleSpecification {
  const sources: StyleSpecification["sources"] = {
    protomaps: {
      type: "vector",
      url: `pmtiles://${url}`,
      attribution: ATTRIBUTION,
    },
  };
  let layers = applyForayRoadStyle(themedBaseLayers(theme), theme);

  if (terrainUrl) {
    const source = ensureDemSource(terrainUrl);
    Object.assign(sources, terrainSources(source.sharedDemProtocolUrl, contourTilesUrl(source)));
    layers = applyTerrainLayers(layers, theme, contoursVisible);
  }

  return {
    version: 8,
    glyphs: `${ASSETS}/fonts/{fontstack}/{range}.pbf`,
    sprite: `${ASSETS}/sprites/v4/${theme}`,
    sources,
    layers,
  } as StyleSpecification;
}

/** Mount the vector basemap on `map` and return the Leaflet layer. Registers the `pmtiles://`
 * protocol with MapLibre once per page. The layer lands in Leaflet's default `tilePane`
 * (z-index 200), below the `satellite` pane (350) and every overlay pane (400+). */
export function mountVectorBasemap(
  map: L.Map,
  url: string,
  terrainUrl: string,
  theme: "dark" | "light",
): L.MaplibreGL {
  if (!protocolRegistered) {
    addProtocol("pmtiles", new Protocol().tile);
    protocolRegistered = true;
  }
  glLayer = L.maplibreGL({ style: buildStyle(url, terrainUrl, theme) }).addTo(map);
  return glLayer;
}

/** Swap the vector style for a theme change (light <-> dark). No-op if the basemap has not
 * been mounted yet (no basemap_url configured). */
export function setVectorBasemapTheme(url: string, terrainUrl: string, theme: "dark" | "light"): void {
  glLayer?.getMaplibreMap().setStyle(buildStyle(url, terrainUrl, theme));
}

export function hasVectorBasemap(): boolean {
  return glLayer !== null;
}

/** Show or hide the contour lines + elevation labels (the Layers-pill "Contours" toggle).
 * No-op until the basemap is mounted. */
export function setContoursVisible(visible: boolean): void {
  contoursVisible = visible;
  const gl = glLayer?.getMaplibreMap();
  if (!gl) return;
  const value = visible ? "visible" : "none";
  for (const id of CONTOUR_LAYER_IDS) {
    if (gl.getLayer(id)) gl.setLayoutProperty(id, "visibility", value);
  }
}
