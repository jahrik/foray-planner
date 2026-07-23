import "leaflet/dist/leaflet.css";
import "./style.css";

import { getJson, postJson } from "./api/client";
import type { Home } from "./api/types";
import { initGenusSelection } from "./genera";
import { loadCamps, loadLand, loadTrails } from "./layers";
import { initLocationAutocomplete } from "./location";
import { currentTheme, initMap, setMapClickHandler, setTiles, updateHome } from "./map";
import { runPlan } from "./plan";
import { cancelRefresh, setLocationLatLng, startRefresh } from "./refresh";
import { errorDetail, qs, setStatus, state, type Units, type View } from "./state";
import { initMonths, runAlerts, runDestinations } from "./views";

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
  loadLand();
  initTabs();
  initRadiusPresets();
  // 'change' (not 'input') so a re-run only fires on blur/enter/stepper-click, not every
  // keystroke while typing a number.
  qs("#plan-stops").addEventListener("change", () => runPlan());
  qs("#plan-drive").addEventListener("change", () => runPlan());
  qs("#plan-free-camp").addEventListener("change", () => runPlan());
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
  wireLayerToggle("#show-land", "land", "Fetching public land…", loadLand);
  wireLayerToggle("#show-trails", "trails", "Fetching trails…", loadTrails);
  initLocationAutocomplete();
  initGenusSelection(refreshCurrentView);
  // If a refresh is already running (e.g. page reload mid-fetch), reflect it.
  if (config.refreshing) {
    startRefresh("Fetching data…").then((succeeded) => {
      if (succeeded) refreshCurrentView();
    });
  } else if (state.view === "destinations") {
    runDestinations();
  }
  initGeolocation();
}

// Auto-detect location on load so users without a fixed home base (e.g. living in a van) get
// a current fix each time they open the app, without needing to remember to set it manually.
// maximumAge: 0 forces a fresh GPS fix rather than whatever cached position the OS/browser last
// resolved - the earlier bug here was a stale cached fix silently masquerading as current. The
// search box (initLocationAutocomplete) and map click stay available as manual overrides.
// Denial/error surfaces a status message instead of failing silently, since a stale location is
// otherwise easy to miss.
function initGeolocation(): void {
  if (!("geolocation" in navigator)) return;
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

      let response: { home: Home };
      try {
        response = await postJson("/api/location", { lat, lng, name: name ?? null });
      } catch {
        return; // keep whatever location is already loaded
      }
      updateHome(response.home);
      loadLand();
      refreshCurrentView();
    },
    (error) => {
      setStatus(`couldn't detect location (${error.message}) - set it manually via search or map click`);
    },
    { timeout: 8000, maximumAge: 0 },
  );
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
