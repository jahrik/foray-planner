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

// Destinations rank automatically now (no manual trigger needed); calendar has no re-runnable
// action either. Alerts/plan still depend on inputs the user might change after the initial
// run, so they keep a manual re-run button. Called both on tab switches and once on initial
// load, since the page starts on the destinations tab and the button's static HTML label
// ("Rank destinations") would otherwise sit there as a stale, non-functional no-op until the
// user switched tabs at least once.
function updateRunButton(): void {
  const runBtn = qs<HTMLButtonElement>("#run");
  if (state.view === "destinations") {
    runBtn.style.display = "none";
  } else {
    runBtn.style.display = "";
    if (state.view === "alerts") runBtn.textContent = "Check alerts";
    else if (state.view === "plan") runBtn.textContent = "Plan route";
  }
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

      updateRunButton();

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
  initTheme();
  initUnits();
  initFiltersToggle();
  initMonths();
  initMap(config.home);
  setMapClickHandler(setLocationLatLng);
  updateHome(config.home);
  loadLand();
  initTabs();
  updateRunButton();
  initRadiusPresets();
  qs("#run").onclick = () => {
    if (state.view === "alerts") runAlerts();
    else if (state.view === "plan") runPlan();
  };
  qs("#refresh").onclick = () => startRefresh("Refreshing mushroom data…", "mushrooms");

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
      if (succeeded) runDestinations();
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
      if (state.view === "destinations") runDestinations();
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
      if (state.view === "destinations") runDestinations();
    };
  });
}

main();
