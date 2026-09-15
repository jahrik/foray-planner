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
import type { Map as MaplibreMap } from "maplibre-gl";
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
import { fireLayers, FIRE_LAYER_IDS, fireSource } from "./basemap-fire";
import { landFilter, landLayers, LAND_LAYER_IDS, landSource } from "./basemap-land";
import { applyForayRoadStyle } from "./basemap-roads";
import { roadsAndLabelsOnly, satelliteImageryLayer, satelliteSource } from "./basemap-satellite";
import { trailsLayer, trailsSource } from "./basemap-trails";
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

/** The same-origin tile/archive URLs a style build needs - grouped into one object once a
 * fifth and sixth ({@link landUrl}/{@link fireUrl}, issue #336 PR 2) joined the original four,
 * since threading that many near-identical strings positionally through every call site here
 * and in map.ts had become its own source of mix-up risk. Any entry may be `""` to disable that
 * layer/source entirely - see each field's own Settings.* counterpart in config.py. */
export interface TileUrls {
  basemapUrl: string;
  terrainUrl: string;
  satelliteUrl: string;
  trailsUrl: string;
  landUrl: string;
  fireUrl: string;
}

let protocolRegistered = false;
let glLayer: L.MaplibreGL | null = null;

// One DemSource for the page: it registers the maplibre-contour protocols once and caches
// decoded DEM tiles across the hillshade and the contour lines. `contoursVisible` is tracked
// here so a theme swap (which rebuilds the whole style) can re-bake the toggle state.
let demSource: InstanceType<typeof mlcontour.DemSource> | null = null;
let contoursVisible = false;
// The Layers-pill "Satellite basemap" toggle (issue #340) - swaps the whole style between the
// vector map and real Esri imagery. Tracked here (not just passed as an argument) so a theme
// change alone re-bakes whichever mode is currently active, the same way `contoursVisible` does.
let satelliteBasemapEnabled = false;
// Public-land / fire layer toggles (issue #336 PR 2) - same "track it here so a theme swap
// re-bakes the current state" reasoning as contoursVisible/satelliteBasemapEnabled. Land carries
// one flag per agency toggle (map.ts's #show-land-blm/usfs/tribal); fire is a single on/off.
let landAgencies: readonly string[] = [];
let fireVisible = false;

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

// `urls.satelliteUrl` is only used when `satelliteBasemapEnabled` is true (the Layers-pill
// toggle) - buildStyle is the one place that branches on that flag, so every other call site
// (mount, theme change, contour toggle, the satellite toggle itself) stays a plain "rebuild and
// setStyle" with no satellite-specific logic of its own.
function buildStyle(urls: TileUrls, theme: "dark" | "light"): StyleSpecification {
  const useSatellite = satelliteBasemapEnabled && !!urls.satelliteUrl;
  const sources: StyleSpecification["sources"] = {
    protomaps: {
      type: "vector",
      url: `pmtiles://${urls.basemapUrl}`,
      attribution: ATTRIBUTION,
    },
  };
  const vectorLayers = applyForayRoadStyle(themedBaseLayers(theme), theme);
  let layers: StyleSpecification["layers"];
  if (useSatellite) {
    Object.assign(sources, satelliteSource(urls.satelliteUrl));
    layers = [satelliteImageryLayer(), ...roadsAndLabelsOnly(vectorLayers)];
  } else {
    layers = vectorLayers;
  }

  if (urls.terrainUrl) {
    const source = ensureDemSource(urls.terrainUrl);
    Object.assign(sources, terrainSources(source.sharedDemProtocolUrl, contourTilesUrl(source)));
    layers = applyTerrainLayers(layers, theme, contoursVisible, !useSatellite);
  }

  // Land/fire before trails: trails is the layer users click most (road-inspect.ts), and MapLibre
  // hit-tests top-down, so it should sit above the land/fire fills rather than under them.
  if (urls.landUrl) {
    Object.assign(sources, landSource(urls.landUrl));
    layers = [...layers, ...landLayers(landAgencies)];
  }
  if (urls.fireUrl) {
    Object.assign(sources, fireSource(urls.fireUrl));
    layers = [...layers, ...fireLayers(fireVisible)];
  }
  if (urls.trailsUrl) {
    Object.assign(sources, trailsSource(urls.trailsUrl));
    layers = [...layers, trailsLayer(theme)];
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
export function mountVectorBasemap(map: L.Map, urls: TileUrls, theme: "dark" | "light"): L.MaplibreGL {
  if (!protocolRegistered) {
    addProtocol("pmtiles", new Protocol().tile);
    protocolRegistered = true;
  }
  glLayer = L.maplibreGL({ style: buildStyle(urls, theme) }).addTo(map);
  return glLayer;
}

/** Swap the style for a theme change (light <-> dark). No-op if the basemap has not been
 * mounted yet (no basemap_url configured). Keeps whichever of vector/satellite mode is
 * currently active - see `satelliteBasemapEnabled`. */
export function setVectorBasemapTheme(urls: TileUrls, theme: "dark" | "light"): void {
  glLayer?.getMaplibreMap().setStyle(buildStyle(urls, theme));
}

/** The Layers-pill "Satellite basemap" toggle: swap the whole style between the vector map and
 * real Esri imagery with our own roads/boundaries/labels over it. No-op if the basemap has not
 * been mounted yet. */
export function setSatelliteBasemapMode(urls: TileUrls, theme: "dark" | "light", on: boolean): void {
  satelliteBasemapEnabled = on;
  glLayer?.getMaplibreMap().setStyle(buildStyle(urls, theme));
}

/** The Layers-pill land-agency toggles (#show-land-blm/usfs/tribal): live `setLayoutProperty` +
 * `setFilter` on the already-mounted land layers, no full style rebuild (unlike the satellite/
 * contour toggles above, which change what else is in the style). `agencies` tracked module-side
 * so a later theme swap's rebuild keeps the current selection. No-op if land tiles are disabled
 * (layers never got added to the style) or the basemap isn't mounted yet. */
export function setLandLayerState(agencies: readonly string[]): void {
  landAgencies = agencies;
  const gl = glLayer?.getMaplibreMap();
  if (!gl) return;
  const visibility = agencies.length > 0 ? "visible" : "none";
  const filter = landFilter(agencies);
  for (const id of LAND_LAYER_IDS) {
    if (!gl.getLayer(id)) continue;
    gl.setLayoutProperty(id, "visibility", visibility);
    gl.setFilter(id, filter);
  }
}

/** The Layers-pill "Fire" toggle (#show-fire) - same live-update reasoning as
 * `setLandLayerState`, minus the per-agency filter (fire has one toggle for both layers). */
export function setFireLayerState(visible: boolean): void {
  fireVisible = visible;
  const gl = glLayer?.getMaplibreMap();
  if (!gl) return;
  const visibility = visible ? "visible" : "none";
  for (const id of FIRE_LAYER_IDS) {
    if (gl.getLayer(id)) gl.setLayoutProperty(id, "visibility", visibility);
  }
}

export function hasVectorBasemap(): boolean {
  return glLayer !== null;
}

/** The underlying MapLibre GL map, for read-only queries like `queryRenderedFeatures`
 * (click-to-inspect in map.ts). `null` until the vector basemap is mounted. */
export function getGlMap(): MaplibreMap | null {
  return glLayer?.getMaplibreMap() ?? null;
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
