import type L from "leaflet";

// The two layer teardown idioms map.ts repeats for every layer group it owns. Both take just
// the `removeLayer` surface of the map so they stay trivially testable with a stub (see
// layer-lifecycle.test.ts).

type LayerRemover = Pick<L.Map, "removeLayer">;

/** Pull every layer in `layers` off the map and empty the array in place. Backs `clearCamps`,
 * `clearTrailheadMarkers`, `clearCardCampMarkers`, and the marker sweep in `clearMarkers`. */
export function clearLayerList(map: LayerRemover, layers: L.Layer[]): void {
  for (const layer of layers) map.removeLayer(layer);
  layers.length = 0;
}

/** Pull a single optional layer off the map if it's present; returns `null` for the caller to
 * store back. Backs `clearLand`, `clearSelectedTrail`, `clearPlanRoute`. */
export function clearLayer(map: LayerRemover, layer: L.Layer | null): null {
  if (layer) map.removeLayer(layer);
  return null;
}
