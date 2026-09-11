// The camp / dispersed / public-land checkbox wiring in the header. Turning a layer on first
// runs an on-demand ingest for it (startRefresh) then plots it; turning it off cancels any
// in-flight ingest for that layer and re-plots without it. Split out of main.ts (issue #242
// Part 2d).

import { loadCamps, loadFire, loadLand } from "../map/layers";
import { setAerialEnabled, setContoursEnabled, setSatelliteBasemapEnabled } from "../map/map";
import { cancelRefresh, startRefresh } from "../refresh";
import { qs, state } from "../state";

export function initLayerToggles(): void {
  let currentRefreshTarget: string | null = null;

  const ensureLayer = async (target: string, msg: string) => {
    // startRefresh will instantly skip if the backend detects it's already ingested
    await startRefresh(msg, target);
    // If the user toggled a different layer (or cancelled) while this await was in flight,
    // currentRefreshTarget has already moved on - don't clobber its state or reload out of order.
    if (currentRefreshTarget !== target) return;
    currentRefreshTarget = null;
    loadCamps();
    loadLand();
  };
  const cancelLayerRefresh = (target: string) => {
    // Only cancel if the in-flight refresh is for this specific layer, so we
    // don't accidentally abort an unrelated mushroom refresh.
    if (currentRefreshTarget === target) {
      cancelRefresh();
      currentRefreshTarget = null;
    }
  };

  const wireLayerToggle = (id: string, target: string, msg: string, loader: () => void) => {
    qs(id).onchange = (e) => {
      if ((e.target as HTMLInputElement).checked) {
        currentRefreshTarget = target;
        ensureLayer(target, msg);
      } else {
        cancelLayerRefresh(target);
        loader();
      }
    };
  };

  wireLayerToggle("#show-camps", "camps", "Fetching campgrounds…", loadCamps);
  wireLayerToggle("#show-dispersed", "dispersed", "Fetching dispersed camping…", loadCamps);
  qs("#free-camps").onchange = () => loadCamps();
  wireLayerToggle("#show-land-blm", "land", "Fetching public land…", loadLand);
  wireLayerToggle("#show-land-usfs", "land", "Fetching public land…", loadLand);
  wireLayerToggle("#show-land-tribal", "land", "Fetching public land…", loadLand);
  // Fire data is refreshed server-side on its own cadence (issue #227), not on-demand per
  // toggle - so this just fetches + plots what's cached, no startRefresh round-trip.
  qs("#show-fire").onchange = () => loadFire();
  // Aerial imagery for the selected destination - no fetch here, map.ts shows/hides the cached
  // overlay for whatever region is currently selected (sources/satellite.py serves it).
  qs("#show-aerial").onchange = (e) => setAerialEnabled((e.target as HTMLInputElement).checked);
  // Contour lines off the DEM tiles - no fetch here either, map.ts flips the layer visibility
  // on the vector basemap (the hillshade underneath it is always on). Hidden outright when
  // there is no terrain configured, so it never shows in the Layers pill's on-count.
  const contours = qs("#show-contours") as HTMLInputElement;
  contours.onchange = (e) => setContoursEnabled((e.target as HTMLInputElement).checked);
  const contoursRow = contours.closest("label");
  if (contoursRow) contoursRow.hidden = !state.terrainUrl;
  // The checkbox defaults to checked (index.html), but basemap.ts's `contoursVisible` module
  // flag defaults to false and only ever changes via this same setContoursEnabled call - so
  // without this, the first-ever style build bakes the contour layers hidden while the
  // checkbox/Layers-pill count both say "on" (Copilot review, PR #341). Sync it once on init;
  // a no-op once the box starts unchecked by user choice on a later load.
  setContoursEnabled(contours.checked);
  // The full-map satellite basemap toggle (issue #340) - a different feature from "Aerial
  // imagery" above (that's the per-destination photo fill from #293). No fetch here either -
  // map.ts swaps the whole MapLibre style. Hidden when no satellite tile URL is configured,
  // same gating shape as Contours.
  const satelliteBasemap = qs("#show-satellite-basemap");
  satelliteBasemap.onchange = (e) => setSatelliteBasemapEnabled((e.target as HTMLInputElement).checked);
  const satelliteBasemapRow = satelliteBasemap.closest("label");
  // Needs both: no vector basemap means no roads/labels to draw over the imagery, and
  // setSatelliteBasemapEnabled itself requires basemapUrl too (map.ts) - gating on only
  // satelliteTilesUrl left the row visible (and then self-unchecking on click) for a deployment
  // with no vector basemap but the satellite proxy's non-empty default URL (Copilot review, PR
  // #341).
  if (satelliteBasemapRow) satelliteBasemapRow.hidden = !state.basemapUrl || !state.satelliteTilesUrl;
}
