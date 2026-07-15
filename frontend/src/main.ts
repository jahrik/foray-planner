import "leaflet/dist/leaflet.css";
import "./style.css";

import { getJson, postJson } from "./api/client";
import type { Config, Home } from "./api/types";
import { loadCamps, loadLand, loadTrails } from "./layers";
import { initLocationAutocomplete } from "./location";
import { currentTheme, initMap, setMapClickHandler, setTiles, updateHome } from "./map";
import { runPlan } from "./plan";
import { setLocationLatLng, startRefresh } from "./refresh";
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
  const config = await getJson<Config>("/api/config");
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
    currentRefreshTarget = null;
    loadCamps();
    loadLand();
    loadTrails();
  };
  const cancelLayerRefresh = async (target: string) => {
    // Only cancel if the in-flight refresh is for this specific layer, so we
    // don't accidentally abort an unrelated mushroom refresh.
    if (currentRefreshTarget === target) {
      await fetch("/api/refresh", { method: "DELETE" });
      currentRefreshTarget = null;
    }
  };

  qs("#show-camps").onchange = (e) => {
    if ((e.target as HTMLInputElement).checked) { currentRefreshTarget = "camps"; ensureLayer("camps", "Fetching campgrounds…"); }
    else { cancelLayerRefresh("camps"); loadCamps(); }
  };
  qs("#show-dispersed").onchange = (e) => {
    if ((e.target as HTMLInputElement).checked) { currentRefreshTarget = "dispersed"; ensureLayer("dispersed", "Fetching dispersed camping…"); }
    else { cancelLayerRefresh("dispersed"); loadCamps(); }
  };
  qs("#free-camps").onchange = () => loadCamps();
  qs("#show-land").onchange = (e) => {
    if ((e.target as HTMLInputElement).checked) { currentRefreshTarget = "land"; ensureLayer("land", "Fetching public land…"); }
    else { cancelLayerRefresh("land"); loadLand(); }
  };
  qs("#show-trails").onchange = (e) => {
    if ((e.target as HTMLInputElement).checked) { currentRefreshTarget = "trails"; ensureLayer("trails", "Fetching trails…"); }
    else { cancelLayerRefresh("trails"); loadTrails(); }
  };
  initLocationAutocomplete();
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

// Auto-detect location on load so the destination flow needs no manual setup; the search box
// (initLocationAutocomplete) stays available as an override/plan-ahead path, and denial/error
// just leaves whatever location `/api/config` already gave us.
function initGeolocation(): void {
  if (!("geolocation" in navigator)) return;
  navigator.geolocation.getCurrentPosition(
    async (position) => {
      let response: { home: Home };
      try {
        response = await postJson<{ home: Home }>("/api/location", {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
        });
      } catch {
        return; // keep whatever location is already loaded
      }
      updateHome(response.home);
      loadLand();
      refreshCurrentView();
    },
    () => {
      // denied/unavailable - fall back silently to the already-loaded location
    },
    { timeout: 8000 },
  );
}

function initRadiusPresets(): void {
  qs("#radius-presets").querySelectorAll<HTMLButtonElement>("button[data-km]").forEach((button) => {
    button.onclick = async () => {
      if (!state.home) return;
      const radius_km = Number(button.dataset.km);
      let response: { home: Home };
      try {
        response = await postJson<{ home: Home }>("/api/location", {
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
