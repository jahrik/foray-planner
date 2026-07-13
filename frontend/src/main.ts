import "leaflet/dist/leaflet.css";
import "./style.css";

import { getJson } from "./api/client";
import type { Config } from "./api/types";
import { loadCamps, loadLand, loadTrails } from "./layers";
import { initLocationAutocomplete } from "./location";
import { clearPlanRoute, currentTheme, initMap, setTiles, updateHome } from "./map";
import { runPlan } from "./plan";
import { startRefresh } from "./refresh";
import { qs, state, type Units, type View } from "./state";
import { initMonths, initSpecies, runAlerts, runDestinations } from "./views";

function initTabs(): void {
  document.querySelectorAll<HTMLButtonElement>(".tabs button").forEach((button) => {
    button.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((other) => other.classList.remove("active"));
      button.classList.add("active");
      state.view = (button.dataset.view as View) ?? "destinations";

      // Show plan controls only while on the Plan tab.
      const planRow = document.getElementById("plan-row");
      if (planRow) planRow.style.display = state.view === "plan" ? "flex" : "none";

      // The run button is only meaningful on destinations / alerts / plan.
      // Hide it on the calendar tab (no re-runnable action there).
      const runBtn = qs<HTMLButtonElement>("#run");
      if (state.view === "calendar") {
        runBtn.style.display = "none";
      } else {
        runBtn.style.display = "";
        if (state.view === "destinations") runBtn.textContent = "Rank destinations";
        else if (state.view === "alerts") runBtn.textContent = "Check alerts";
        else if (state.view === "plan") runBtn.textContent = "Plan route";
      }

      if (state.view === "destinations") runDestinations();
      else if (state.view === "alerts") runAlerts();
      else if (state.view === "plan") runPlan();
      else {
        clearPlanRoute();
        qs("#panel").innerHTML =
          "<p class='hint'>Click a ranked destination to see its 12-month calendar.</p>";
      }
    };
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
  initMonths();
  await initSpecies();
  initMap(config.home);
  updateHome(config.home);
  initTabs();
  qs("#run").onclick = () => {
    if (state.view === "destinations") runDestinations();
    else if (state.view === "alerts") runAlerts();
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
  }
}

main();
