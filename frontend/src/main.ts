import "leaflet/dist/leaflet.css";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
import "./style.css";

import { getJson, postJson } from "./api/client";
import type { Home, LocationResponse } from "./api/types";
import { initGenusSelection } from "./genera";
import { initLayerToggles } from "./ui/layer-toggles";
import { loadLand } from "./map/layers";
import { initLocationAutocomplete, initPlaceAutocomplete } from "./location";
import { initMap, map, setMapClickHandler, updateHome } from "./map/map";
import { runPlan } from "./views/plan";
import { setLocationLatLng, startRefresh } from "./refresh";
import { collapseIfOpen, currentDetent, initSheet, snapTo } from "./map/sheet";
import { errorDetail, qs, setStatus, state } from "./state";
import { initTextSize, initTheme, initUnits } from "./ui/ui-prefs";
import { refreshCurrentView } from "./views/view-run";
import { initMonths, runDestinations } from "./views/views";

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

function initTabs(): void {
  document.querySelectorAll<HTMLButtonElement>(".tabs button").forEach((button) => {
    button.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((other) => other.classList.remove("active"));
      button.classList.add("active");
      state.view = (button.dataset.view as typeof state.view) ?? "destinations";

      // Show plan controls only while on the Plan tab.
      const planRow = document.getElementById("plan-row");
      if (planRow) planRow.style.display = state.view === "plan" ? "flex" : "none";

      // Alerts (Fruiting now) has no months param - it's a fixed trailing-weeks window, not
      // a month picker (see /api/alerts) - so the filter is irrelevant, not just redundant.
      const monthsField = document.getElementById("months-field");
      if (monthsField) monthsField.style.display = state.view === "alerts" ? "none" : "flex";

      // Each run*() only replaces #panel's content once its fetch resolves, so without this
      // the previous tab's cards stay on screen (and interactive) for a beat after switching -
      // easy to mistake for the new tab's data since nothing visibly changed yet.
      qs("#panel").innerHTML = "<p class='hint'>Loading…</p>";

      refreshCurrentView();
    };
  });
}

// Mobile-only toggle (hidden by CSS on desktop, where the filters row is always visible).
function initFiltersToggle(): void {
  const toggle = qs<HTMLButtonElement>("#filters-toggle");
  const row = qs("#filters-row");
  toggle.onclick = () => {
    const open = row.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
    // Keep the filters row (top of the screen) clear of a fully-raised sheet.
    if (open && currentDetent() === "full") snapTo("half");
    // Opening/closing the filters row changes how much vertical space main (and #map) get on
    // mobile - resync Leaflet's cached container size once the reflow settles, same reason as
    // the resize listener in main().
    requestAnimationFrame(() => map.invalidateSize());
  };
}

// Small popover explaining the core flow for a first-time visitor - closes on outside click,
// Escape, or toggling it again, same pattern as the mobile filters disclosure.
function initHelp(): void {
  const toggle = qs<HTMLButtonElement>("#help-toggle");
  const popover = qs("#help-popover");
  const close = () => {
    popover.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  };
  toggle.onclick = (e) => {
    e.stopPropagation();
    const open = popover.hidden;
    popover.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
  };
  popover.onclick = (e) => e.stopPropagation();
  document.addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

async function main(): Promise<void> {
  const config = await getJson("/api/config");
  state.home = config.home;
  state.cellDeg = config.cell_deg;
  initTheme();
  initUnits();
  initTextSize();
  initHelp();
  initFiltersToggle();
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
  initTabs();
  initRadiusPresets();
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
  initGenusSelection(refreshCurrentView);

  // Kick geolocation off immediately, but don't let it block the initial paint. If a home
  // (already-granted permission, no browser prompt) resolves within the head-start window, the
  // side effects below run *before* the race settles, so the very first plot already reflects
  // the real location - no visible re-plot. If it's slower, we fall through and paint with the
  // saved/default home now; the .then() below still fires whenever the fix eventually lands.
  let geoApplied = false;
  const geoPromise = geolocateHome().then((home) => {
    if (home) {
      geoApplied = true;
      updateHome(home);
      refreshCurrentView(); // before loadLand() - see refresh.ts setLocation
      loadLand();
    }
    return home;
  });

  // If a refresh is already running (e.g. page reload mid-fetch), reflect it.
  if (config.refreshing) {
    startRefresh("Fetching data…").then((succeeded) => {
      if (succeeded) refreshCurrentView();
    });
  } else {
    await Promise.race([geoPromise, sleep(GEOLOCATION_HEAD_START_MS)]);
    if (!geoApplied && state.view === "destinations") runDestinations();
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// How long the first paint waits on geolocation before giving up and plotting the stale/saved
// home instead. Long enough that an already-granted permission (no browser prompt, typically a
// few hundred ms) resolves in time and the very first plot is the accurate one; short enough
// that a slow fix (first-time permission prompt, weak GPS) doesn't stall the initial paint.
const GEOLOCATION_HEAD_START_MS = 600;

// Auto-detect location on load so users without a fixed home base (e.g. living in a van) get
// a current fix each time they open the app, without needing to remember to set it manually.
// maximumAge: 0 forces a fresh GPS fix rather than whatever cached position the OS/browser last
// resolved - the earlier bug here was a stale cached fix silently masquerading as current. The
// search box (initLocationAutocomplete) and map click stay available as manual overrides.
// Denial/error surfaces a status message instead of failing silently, since a stale location is
// otherwise easy to miss.
//
// Resolves to the updated Home once geolocation succeeds, or null if it's unsupported, denied,
// or fails - never rejects. Applying the result (updateHome/loadLand/refreshCurrentView) is left
// to the caller; main() races this against GEOLOCATION_HEAD_START_MS so a fast resolution can
// feed the very first plot instead of forcing a second, visibly different-looking one right after it.
function geolocateHome(): Promise<Home | null> {
  if (!("geolocation" in navigator)) return Promise.resolve(null);
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        const { latitude: lat, longitude: lng } = position.coords;
        // Reverse geocoding happens server-side now (issue #145) - no direct browser->Nominatim
        // call, no client-side 200-char guard to duplicate.
        try {
          const response = await postJson("/api/location", { body: { lat, lng } });
          resolve(response.home);
        } catch {
          resolve(null); // keep whatever location is already loaded
        }
      },
      (error) => {
        setStatus(`couldn't detect location (${error.message}) - set it manually via search or map click`);
        resolve(null);
      },
      { timeout: 8000, maximumAge: 0 },
    );
  });
}

function initRadiusPresets(): void {
  qs("#radius-presets")
    .querySelectorAll<HTMLButtonElement>("button[data-km]")
    .forEach((button) => {
      button.onclick = async () => {
        if (!state.home) return;
        const radius_km = Number(button.dataset.km);
        let response: LocationResponse;
        try {
          response = await postJson("/api/location", {
            body: {
              lat: state.home.lat,
              lng: state.home.lng,
              name: state.home.name,
              radius_km,
            },
          });
        } catch (error) {
          setStatus(errorDetail(error));
          return;
        }
        updateHome(response.home);
        refreshCurrentView(); // before loadLand() - see refresh.ts setLocation
        loadLand();
      };
    });
}

main();
