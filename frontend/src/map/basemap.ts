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
import L from "leaflet";
import { addProtocol } from "maplibre-gl";
// MapLibre's own stylesheet positions the GL canvas/controls inside its container; without it
// the basemap can lay out wrong even mounted through the Leaflet plugin.
import "maplibre-gl/dist/maplibre-gl.css";
import "@maplibre/maplibre-gl-leaflet";
import { Protocol } from "pmtiles";
import layers from "protomaps-themes-base";
import type { StyleSpecification } from "@maplibre/maplibre-gl-style-spec";

const ASSETS = "https://protomaps.github.io/basemaps-assets";
const ATTRIBUTION =
  '<a href="https://protomaps.com">Protomaps</a> · © <a href="https://openstreetmap.org">OpenStreetMap</a>';

let protocolRegistered = false;
let glLayer: L.MaplibreGL | null = null;

function buildStyle(url: string, theme: "dark" | "light"): StyleSpecification {
  return {
    version: 8,
    glyphs: `${ASSETS}/fonts/{fontstack}/{range}.pbf`,
    sprite: `${ASSETS}/sprites/v4/${theme}`,
    sources: {
      protomaps: {
        type: "vector",
        url: `pmtiles://${url}`,
        attribution: ATTRIBUTION,
      },
    },
    layers: layers("protomaps", theme, "en"),
  } as StyleSpecification;
}

/** Mount the vector basemap on `map` and return the Leaflet layer. Registers the `pmtiles://`
 * protocol with MapLibre once per page. The layer lands in Leaflet's default `tilePane`
 * (z-index 200), below the `satellite` pane (350) and every overlay pane (400+). */
export function mountVectorBasemap(map: L.Map, url: string, theme: "dark" | "light"): L.MaplibreGL {
  if (!protocolRegistered) {
    addProtocol("pmtiles", new Protocol().tile);
    protocolRegistered = true;
  }
  glLayer = L.maplibreGL({ style: buildStyle(url, theme) }).addTo(map);
  return glLayer;
}

/** Swap the vector style for a theme change (light <-> dark). No-op if the basemap has not
 * been mounted yet (no basemap_url configured). */
export function setVectorBasemapTheme(url: string, theme: "dark" | "light"): void {
  glLayer?.getMaplibreMap().setStyle(buildStyle(url, theme));
}

export function hasVectorBasemap(): boolean {
  return glLayer !== null;
}
