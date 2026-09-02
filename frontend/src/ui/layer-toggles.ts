// The camp / dispersed / public-land checkbox wiring in the header. Turning a layer on first
// runs an on-demand ingest for it (startRefresh) then plots it; turning it off cancels any
// in-flight ingest for that layer and re-plots without it. Split out of main.ts (issue #242
// Part 2d).

import { loadCamps, loadLand } from "../map/layers";
import { cancelRefresh, startRefresh } from "../refresh";
import { qs } from "../state";

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
}
