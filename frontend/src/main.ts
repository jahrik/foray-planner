// Entry point. Loads /api/config, initializes preferences, the map, the mobile sheet, every control and the
// filter pills (last, since it moves their DOM), starts geolocation without blocking first paint, then runs
// the first destinations load. See docs/frontend.md for how the modules fit together.

import "leaflet/dist/leaflet.css";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
import "@fontsource-variable/fraunces";
import "@fontsource-variable/ibm-plex-sans";
import "./tokens.css";
import "./style.css";

import { getJson } from "./api/client";
import { postLocation } from "./location-writes";
import type { LocationResponse } from "./api/types";
import { initTaxaSelection } from "./taxa";
import { initLayerToggles } from "./ui/layer-toggles";
import { loadFire, loadLand } from "./map/layers";
import { initLocationAutocomplete, initPlaceAutocomplete } from "./location";
import { initAutoLocate, trackEngagement } from "./locate-ui";
import { initMap, map, setMapClickHandler, updateHome } from "./map/map";
import { runPlan } from "./views/plan";
import { setLocationLatLng, startRefresh } from "./refresh";
import { collapseIfOpen, currentDetent, initSheet, snapTo } from "./map/sheet";
import { errorDetail, onScopeChange, qs, setStatus, state } from "./state";
import { initTextSize, initTheme, initUnits } from "./ui/ui-prefs";
import { initPills, syncPillsForView } from "./ui/pills";
import { OPEN_EVENT } from "./ui/pill";
import { refreshCurrentView } from "./views/view-run";
import { initMonths, runDestinations } from "./views/views";
import { initRouteBar, renderActionBar, setShortlistChangeHook } from "./views/shortlist";

// Wires a plan-tab Start/Destination field: unlike the header's home search (which persists the
// choice via /api/location), a selected suggestion here just fills the input with resolved
// "lat, lng" text and re-runs the plan - the field itself is the only state, read fresh by
// runPlan() on every request.
function initPlanPlaceField(inputId: string, listId: string, formId: string): void {
  const input = qs<HTMLInputElement>(`#${inputId}`);
  initPlaceAutocomplete(
    input,
    qs<HTMLUListElement>(`#${listId}`),
    qs<HTMLFormElement>(`#${formId}`),
    (resolved) => {
      input.value = resolved;
      runPlan();
    },
    { clearInputOnSelect: false },
  );
}

// Plan route is no longer a tab (issue #301) - it is a mode entered from the shortlist action
// bar and left with the #plan-back button. enterPlan/exitPlan swap state.view, toggle the
// plan-route query form (#plan-row) and the back button, re-sync the pills (Sort/Months only
// apply to the Destinations flow), and run the matching view.
function setPlanMode(on: boolean): void {
  state.view = on ? "plan" : "destinations";
  const planRow = document.getElementById("plan-row");
  if (planRow) planRow.style.display = on ? "flex" : "none";
  const back = document.getElementById("plan-back");
  if (back) back.hidden = !on;
  renderActionBar(); // hide the shortlist bar inside Plan mode, restore it on the way out
  syncPillsForView(state.view);
  // run*() only replaces #panel once its fetch resolves - clear it now so the previous mode's
  // content doesn't linger, looking interactive, for a beat after the switch.
  qs("#panel").innerHTML = "<p class='hint'>Loading…</p>";
  refreshCurrentView();
}

function initPlanNav(): void {
  qs<HTMLButtonElement>("#plan-back").onclick = () => setPlanMode(false);
}

// The utility overflow menu behind the "⋮" button in the search bar (issue #297): units,
// theme, text size, help text and the source link. The toggles themselves are wired in
// ui/ui-prefs.ts - this only opens/closes the menu (outside-click + Escape + toggle again),
// the same pattern the old header help popover used.
function initOverflowMenu(): void {
  const toggle = qs<HTMLButtonElement>("#overflow-toggle");
  const menu = qs("#overflow-menu");
  const close = () => {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  };
  toggle.onclick = (event) => {
    event.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    // Opening the menu closes any open pill popover (and vice versa, below) so only one
    // floating layer is ever up - matches the pills' own mutual-exclusion.
    if (open) document.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: menu }));
  };
  // Close on any click outside the menu itself. A plain `document` "close" listener plus
  // `menu.onclick = stopPropagation` used to trap the menu open on mobile: the menu overlays
  // the pill row, so a tap meant for a pill lands on the menu, gets its propagation stopped,
  // and nothing closes. Now that tap closes the menu (the pill then takes a second tap, the
  // standard dropdown behaviour).
  document.addEventListener("click", (event) => {
    if (menu.hidden) return;
    const target = event.target as Node;
    if (menu.contains(target) || toggle.contains(target)) return;
    close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
  document.addEventListener(OPEN_EVENT, (event) => {
    if ((event as CustomEvent).detail !== menu) close();
  });
}

// Desktop: the results dock is a left-anchored slide-out over the map, dismissible so the map
// returns to full width; #dock-reopen brings it back (issue #297). Inert on mobile, where the
// bottom sheet (#229) owns show/hide via its detents and both buttons are hidden by CSS.
function initDock(): void {
  const dock = qs("#dock");
  const closeButton = qs<HTMLButtonElement>("#dock-close");
  const reopenButton = qs<HTMLButtonElement>("#dock-reopen");
  const setClosed = (closed: boolean) => {
    dock.classList.toggle("closed", closed);
    reopenButton.hidden = !closed;
  };
  closeButton.onclick = () => setClosed(true);
  reopenButton.onclick = () => setClosed(false);
}

async function main(): Promise<void> {
  trackEngagement(); // before the first await - see locate-ui.ts
  const config = await getJson("/api/config");
  state.home = config.home;
  state.regionRadiusKm = config.region_radius_km;
  state.basemapUrl = config.basemap_url ?? "";
  state.terrainUrl = config.terrain_url ?? "";
  state.satelliteTilesUrl = config.satellite_tiles_url ?? "";
  state.trailsTilesUrl = config.trails_tiles_url ?? "";
  state.landTilesUrl = config.land_tiles_url ?? "";
  state.fireTilesUrl = config.fire_tiles_url ?? "";
  state.recentWeeks = config.recent_weeks;
  initTheme();
  initUnits();
  initTextSize();
  initOverflowMenu();
  initDock();
  initMonths();
  initMap(config.home);
  initSheet();
  // On mobile a tap on the map first collapses an open sheet (and is swallowed); only a tap
  // with the sheet already at its peek sets the location.
  setMapClickHandler((lat, lng) => {
    if (collapseIfOpen()) return;
    setLocationLatLng(lat, lng);
  });
  updateHome(config.home);
  // Leaflet measures #map's box once at construction and never re-measures on its own. The
  // mobile media query gives #map an explicit height, but the browser may not have finished
  // laying that out in the same tick initMap() ran in - invalidateSize() after the next frame
  // makes sure Leaflet's cached size matches reality before the user ever interacts with it.
  requestAnimationFrame(() => map.invalidateSize());
  // resize fires repeatedly during a drag/orientation-change, not once - coalesce into a
  // single invalidateSize() per frame instead of one per event, cancelling any pending frame
  // so only the latest resize in a burst actually triggers a recalculation.
  let resizeFrame: number | null = null;
  window.addEventListener("resize", () => {
    if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      map.invalidateSize();
      resizeFrame = null;
    });
  });
  loadLand();
  loadFire();
  initPlanNav();
  initRadiusPresets();
  initSort();
  // "Plan a route" on the shortlist action bar enters the Plan mode; runPlan() reads the
  // shortlisted region ids itself (views/plan.ts) and threads them as waypoints.
  initRouteBar(() => setPlanMode(true));
  // Clearing (or toggling) the shortlist has to repaint the rendered cards' "+ Plan / ✓ In
  // route" state - cheap re-render from the cached payload, no refetch.
  setShortlistChangeHook(() => {
    if (state.view === "destinations") void runDestinations({ reuseCache: true });
  });
  // 'change' (not 'input') so a re-run only fires on blur/enter/stepper-click, not every
  // keystroke while typing a number.
  qs("#plan-stops").addEventListener("change", () => runPlan());
  qs("#plan-drive").addEventListener("change", () => runPlan());
  qs("#plan-free-camp").addEventListener("change", () => runPlan());
  initPlanPlaceField("plan-start", "plan-start-suggestions", "plan-start-form");
  initPlanPlaceField("plan-destination", "plan-destination-suggestions", "plan-destination-form");
  qs("#refresh").onclick = async () => {
    if (currentDetent() === "full") snapTo("half");
    const succeeded = await startRefresh("Refreshing mushroom data…", "mushrooms");
    if (succeeded) refreshCurrentView();
  };
  initLayerToggles();
  initLocationAutocomplete();
  initTaxaSelection(() => {
    refreshCurrentView();
    onScopeChange();
  });
  // Build the filter-pill row last: it moves the radius / months / taxon / layer control DOM
  // into each pill's popover, so every module that wires those controls has run first.
  initPills();

  // Kick geolocation off immediately, but don't let it block the initial paint. If a fix
  // (already-granted permission, no browser prompt) is applied within the head-start window, the
  // very first plot already reflects the real location - no visible re-plot. If it's slower, we
  // paint with the saved/default home now; geolocate.ts decides what a later fix may do (see
  // locate-ui.ts).
  const geoSession = initAutoLocate();
  let geoApplied = false;
  const geoPromise = (geoSession?.firstApplied ?? Promise.resolve(false)).then((applied) => {
    geoApplied = applied;
  });

  // If a refresh is already running (e.g. page reload mid-fetch), reflect it. Only one that
  // rebuilds phenology blocks reads; a layer-only refresh (land, camps, ...) still lets the
  // destination list load underneath its progress bar.
  if (config.refreshing) {
    startRefresh("Fetching data…").then((succeeded) => {
      if (succeeded) refreshCurrentView();
    });
  }
  // A phenology rebuild 409s reads until it finishes - startRefresh's .then above re-runs the
  // view then, so only load now when nothing is blocking.
  if (!(config.refreshing && config.rebuilding_phenology)) {
    await Promise.race([geoPromise, sleep(GEOLOCATION_HEAD_START_MS)]);
    if (!geoApplied && state.view === "destinations") runDestinations();
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// How long the first paint waits on geolocation before giving up and plotting the stale/saved
// home instead. Long enough that an already-granted permission (no browser prompt, typically a
// few hundred ms for the coarse network fix) resolves in time and the very first plot is the
// accurate one; short enough that a slow fix (first-time permission prompt, cold GPS) doesn't
// stall the initial paint.
const GEOLOCATION_HEAD_START_MS = 600;

// The Destinations sort control (issue #301) - a pill popover of Best overall / Active now /
// Nearest. Client-side reorder of the fetched payload, so it repaints from cache with no
// network round-trip; onScopeChange keeps the pill label in sync.
function initSort(): void {
  qs("#sort-options")
    .querySelectorAll<HTMLButtonElement>("button[data-sort]")
    .forEach((button) => {
      button.onclick = () => {
        state.sort = (button.dataset.sort as typeof state.sort) ?? "best";
        onScopeChange();
        if (state.view === "destinations") void runDestinations({ reuseCache: true });
      };
    });
}

function initRadiusPresets(): void {
  qs("#radius-presets")
    .querySelectorAll<HTMLButtonElement>("button[data-km]")
    .forEach((button) => {
      button.onclick = async () => {
        if (!state.home) return;
        const fallbackHome = state.home;
        const radius_km = Number(button.dataset.km);
        let response: LocationResponse;
        try {
          // Read home when the write runs, not now - see location-writes.ts.
          response = await postLocation(() => {
            const home = state.home ?? fallbackHome;
            return { lat: home.lat, lng: home.lng, name: home.name, radius_km };
          });
        } catch (error) {
          setStatus(errorDetail(error));
          return;
        }
        updateHome(response.home);
        refreshCurrentView(); // before loadLand() - see refresh.ts setLocation
        loadLand();
        loadFire();
      };
    });
}

main();
