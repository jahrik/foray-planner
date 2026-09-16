import L from "leaflet";
import type { Map as MaplibreMap } from "maplibre-gl";

import { FIRE_LAYER_IDS, firePopupSpec, type FireProps } from "./basemap-fire";
import { LAND_LAYER_IDS, landPopupSpec, type LandProps } from "./basemap-land";
import { TRAILS_LAYER_ID, trailPopupSpec, type TrailTileProps } from "./basemap-trails";
import { getBasemapModule, map } from "./map";
import { buildPopup } from "./popup";
import { pickRoadFeature, roadLineLayerIds, roadPopupSpec, type RoadProps } from "./road-inspect";

// A few px of slop around the click point so a thin forest-road line is still an easy tap
// target - but a fixed screen-space slop covers a huge ground area at low zoom, so it can catch
// a road/trail the user has no way of seeing (issue #377). Ramp it from 0 below the zoom where
// our own track/path layers even start rendering (basemap-roads.ts's minzoom 12/13) up to the
// full 5px once they're on screen, so a low-zoom click only inspects something actually visible
// under the cursor.
const SLOP_MIN_ZOOM = 9;
const SLOP_MAX_ZOOM = 14;
const SLOP_MAX_PX = 5;

export function hitSlop(zoom: number): number {
  if (zoom <= SLOP_MIN_ZOOM) return 0;
  if (zoom >= SLOP_MAX_ZOOM) return SLOP_MAX_PX;
  return (SLOP_MAX_PX * (zoom - SLOP_MIN_ZOOM)) / (SLOP_MAX_ZOOM - SLOP_MIN_ZOOM);
}

function hitBox(gl: MaplibreMap, latlng: L.LatLng): [[number, number], [number, number]] {
  const point = gl.project([latlng.lng, latlng.lat]);
  const slop = hitSlop(gl.getZoom());
  return [
    [point.x - slop, point.y - slop],
    [point.x + slop, point.y + slop],
  ];
}

// A hit on our own trails layer just pops up what the tile carries (name/kind/length/land
// unit), same as the land/fire helper below - it used to select + draw the trail outright
// (issue #336 PR 2), but that's the destination card's Trails-tab gesture (layers.ts's
// `selectTrailhead`); a plain map click should show what was clicked, not act on it.
function tryInspectTrailAt(gl: MaplibreMap, latlng: L.LatLng): boolean {
  if (!gl.getLayer(TRAILS_LAYER_ID)) return false;
  const [hit] = gl.queryRenderedFeatures(hitBox(gl, latlng), { layers: [TRAILS_LAYER_ID] });
  if (!hit) return false;
  L.popup()
    .setLatLng(latlng)
    .setContent(buildPopup(trailPopupSpec((hit.properties ?? {}) as TrailTileProps)))
    .openOn(map);
  return true;
}

// Land/fire (issue #336 PR 2): a hit just pops up what the tile carries - no click-to-select
// story for these, unlike trails. `LAND_LAYER_IDS`/`FIRE_LAYER_IDS` are hidden (`visibility:
// "none"`) whenever their Layers-pill toggle is off, and `queryRenderedFeatures` never returns
// features from a hidden layer, so no extra toggle check is needed here.
function tryInspectLandOrFireAt(gl: MaplibreMap, latlng: L.LatLng): boolean {
  const box = hitBox(gl, latlng);
  const landLayers = LAND_LAYER_IDS.filter((id) => gl.getLayer(id));
  if (landLayers.length > 0) {
    const [hit] = gl.queryRenderedFeatures(box, { layers: landLayers });
    if (hit) {
      L.popup()
        .setLatLng(latlng)
        .setContent(buildPopup(landPopupSpec((hit.properties ?? {}) as LandProps)))
        .openOn(map);
      return true;
    }
  }
  const fireLayers = FIRE_LAYER_IDS.filter((id) => gl.getLayer(id));
  if (fireLayers.length > 0) {
    const [hit] = gl.queryRenderedFeatures(box, { layers: fireLayers });
    if (hit) {
      L.popup()
        .setLatLng(latlng)
        .setContent(buildPopup(firePopupSpec((hit.properties ?? {}) as FireProps)))
        .openOn(map);
      return true;
    }
  }
  return false;
}

// Click-to-inspect: if the tap landed on a rendered trail/land/fire/road feature, act on it (see
// the three helpers above) and report the hit so the caller skips the set-home behaviour. A
// miss, or no vector basemap, returns false and the click falls through.
//
// Exported because the destination-region circles set `bubblingMouseEvents: false` (their own
// click selects the region and must not also stomp the home location), so a click on a feature
// that runs under a hero circle's translucent fill never reaches the map handler above - the
// circle's own handler (views.ts) calls this first so a road line still wins.
export function inspectRoadAt(latlng: L.LatLng): boolean {
  const gl = getBasemapModule()?.getGlMap();
  if (!gl) return false;
  if (tryInspectTrailAt(gl, latlng)) return true;
  if (tryInspectLandOrFireAt(gl, latlng)) return true;
  const layerIds = roadLineLayerIds(gl.getStyle().layers);
  if (layerIds.length === 0) return false;
  const feature = pickRoadFeature(gl.queryRenderedFeatures(hitBox(gl, latlng), { layers: layerIds }));
  if (!feature) return false;
  const props = (feature.properties ?? {}) as RoadProps;
  L.popup()
    .setLatLng(latlng)
    .setContent(buildPopup(roadPopupSpec(props, latlng.lat, latlng.lng)))
    .openOn(map);
  return true;
}
