import "leaflet/dist/leaflet.css";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
import "./style.css";

import { getJson, postJson } from "./api/client";
import type { Home } from "./api/types";
import { initGenusSelection } from "./genera";
import { loadCamps, loadLand, loadTrails } from "./layers";
import { initLocationAutocomplete, initPlaceAutocomplete } from "./location";
import { currentTheme, initMap, map, setMapClickHandler, setTiles, updateHome } from "./map";
import { runPlan } from "./plan";
import { cancelRefresh, setLocationLatLng, startRefresh } from "./refresh";
import { errorDetail, qs, setStatus, state, type Units, type View } from "./state";
import { initMonths, runAlerts, runDestinations } from "./views";

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

// Re-runs whichever view is currently open - used after a data refresh finishes so the new
// data actually shows up without the user having to switch tabs back and forth to force it.
function refreshCurrentView(): void {
  if (state.view === "destinations") runDestinations();
  else if (state.view === "alerts") runAlerts();
  else if (state.view === "plan") runPlan();
}

function initTabs(): void {
  document.querySelectorAll<HTMLButtonElement>(".tabs button").forEach((button) => {
    button.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((other) => other.classList.remove("active"));
      button.classList.add("active");
      state.view = (button.dataset.view as View) ?? "destinations";

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

      if (state.view === "destinations") runDestinations();
      else if (state.view === "alerts") runAlerts();
      else if (state.view === "plan") runPlan();
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

function initTheme(): void {
  const toggle = qs<HTMLButtonElement>("#theme-toggle");
  const apply = (theme: "dark" | "light"): void => {
    document.documentElement.dataset.theme = theme;
    toggle.textContent = theme === "dark" ? "🌙" : "☀️";
    toggle.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    toggle.setAttribute("aria-pressed", String(theme === "dark"));
    setTiles(theme); // no-op until the map exists; initMap lays the first tiles
  };
  apply(currentTheme()); // the inline <head> script already set the attribute (default dark)
  toggle.onclick = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    localStorage.setItem("foray-theme", next);
    apply(next);
  };
}

// Persisted like theme/units - toggles a root data attribute that style.css uses to bump up
// font sizes across the panel/cards/map controls for readability on a phone.
function initTextSize(): void {
  const toggle = qs<HTMLButtonElement>("#text-size-toggle");
  const apply = (large: boolean): void => {
    document.documentElement.dataset.textSize = large ? "large" : "normal";
    toggle.setAttribute("aria-pressed", String(large));
    toggle.title = large ? "Switch to normal text size" : "Switch to larger text";
  };
  apply(localStorage.getItem("foray-text-size") === "large");
  toggle.onclick = () => {
    const next = document.documentElement.dataset.textSize !== "large";
    localStorage.setItem("foray-text-size", next ? "large" : "normal");
    apply(next);
  };
}

function initUnits(): void {
  const toggle = qs<HTMLButtonElement>("#units-toggle");
  const apply = (units: Units): void => {
    state.units = units;
    toggle.textContent = units;
    toggle.title = units === "mi" ? "Switch to kilometers" : "Switch to miles";
    toggle.setAttribute("aria-pressed", String(units === "mi"));
    if (state.home) updateHome(state.home);
  };
  apply(state.units);
  toggle.onclick = () => {
    const next: Units = state.units === "mi" ? "km" : "mi";
    localStorage.setItem("foray-units", next);
    apply(next);
  };
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
  setMapClickHandler(setLocationLatLng);
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
    const succeeded = await startRefresh("Refreshing mushroom data…", "mushrooms");
    if (succeeded) refreshCurrentView();
  };

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
    loadTrails();
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
  wireLayerToggle("#show-trails", "trails", "Fetching trails…", loadTrails);
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
      loadLand();
      refreshCurrentView();
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
        let name: string | undefined;
        try {
          const params = new URLSearchParams({ lat: String(lat), lon: String(lng), format: "json" });
          const resp = await fetch(`https://nominatim.openstreetmap.org/reverse?${params}`);
          if (resp.ok) name = (await resp.json())?.display_name;
        } catch {
          // fall back to the coordinate-based name the backend derives
        }
        // /api/location's `name` field is capped at 200 chars server-side; Nominatim's
        // display_name is often longer (full address chain), so an unguarded post would 422 and
        // leave the location stale - the opposite of the point of this auto-refresh.
        if (name && name.length > 200) name = undefined;

        try {
          const response = await postJson("/api/location", { lat, lng, name: name ?? null });
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
  qs("#radius-presets").querySelectorAll<HTMLButtonElement>("button[data-km]").forEach((button) => {
    button.onclick = async () => {
      if (!state.home) return;
      const radius_km = Number(button.dataset.km);
      let response: { home: Home };
      try {
        response = await postJson("/api/location", {
          lat: state.home.lat,
          lng: state.home.lng,
          name: state.home.name,
          radius_km,
        });
      } catch (error) {
        setStatus(errorDetail(error));
        return;
      }
      updateHome(response.home);
      loadLand();
      refreshCurrentView();
    };
  });
}

main();
