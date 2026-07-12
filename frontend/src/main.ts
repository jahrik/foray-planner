import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./style.css";

import { getJson, postJson } from "./api/client";
import type {
  AlertRegion,
  ApiError,
  Calendar,
  CampSite,
  Config,
  Home,
  RegionScore,
  Species,
} from "./api/types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const CURRENT_MONTH = new Date().getMonth() + 1; // 1-12

type View = "destinations" | "calendar" | "alerts";

interface State {
  months: Set<number>;
  view: View;
  home: Home | null;
  markers: L.CircleMarker[];
  campMarkers: L.CircleMarker[];
  focused: { lat: number; lng: number } | null;
}

const state: State = {
  months: new Set([CURRENT_MONTH]),
  view: "destinations",
  home: null,
  markers: [],
  campMarkers: [],
  focused: null,
};
let map: L.Map;
let homeMarker: L.CircleMarker;

// Marker palette — deliberately non-green so it reads against the green OSM terrain.
const HEAT = "#e6398b"; // magenta — historical strength (destinations)
const HEAT_RGB = "230,57,139";
const LIVE = "#22c3e6"; // cyan — fresh / recently observed
const HOME_FILL = "#ffffff"; // white "you are here" dot
const HOME_RING = "#161a12";
const CAMP_FREE = "#ffd24d"; // gold — free / no-fee campground
const CAMP_PAID = "#f5a623"; // amber — fee or unknown-cost campground

function qs<T extends HTMLElement = HTMLElement>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`missing element: ${selector}`);
  return element;
}

const errorDetail = (error: unknown): string =>
  (error as ApiError)?.detail ?? "error";

const inatUrl = (taxonId: number): string => `https://www.inaturalist.org/taxa/${taxonId}`;

interface ChipData {
  taxon_id: number;
  common_name: string;
  label?: string;
}

const speciesChip = (hit: ChipData, extraClass?: string): string =>
  `<a class="chip${extraClass ? " " + extraClass : ""}" href="${inatUrl(hit.taxon_id)}"
      target="_blank" rel="noopener" onclick="event.stopPropagation()"
   >${hit.common_name}${hit.label ? " · " + hit.label : ""}</a>`;

function initMonths(): void {
  const box = qs("#months");
  MONTHS.forEach((label, index) => {
    const month = index + 1;
    const button = document.createElement("button");
    button.textContent = label;
    if (state.months.has(month)) button.classList.add("on");
    button.onclick = () => {
      if (state.months.has(month)) {
        state.months.delete(month);
        button.classList.remove("on");
      } else {
        state.months.add(month);
        button.classList.add("on");
      }
    };
    box.appendChild(button);
  });
}

async function initSpecies(): Promise<void> {
  const species = await getJson<Species[]>("/api/species");
  const select = qs<HTMLSelectElement>("#species");
  species.forEach((entry) => {
    const option = document.createElement("option");
    option.value = String(entry.taxon_id);
    option.textContent = entry.common_name;
    option.title = "View on iNaturalist";
    select.appendChild(option);
  });
}

function selectedSpecies(): string {
  const chosen = [...qs<HTMLSelectElement>("#species").selectedOptions].map((option) => option.value);
  return chosen.length ? chosen.join(",") : "all";
}

function monthsParam(): string {
  const ordered = [...state.months].sort((left, right) => left - right);
  return ordered.length ? ordered.join(",") : "1,2,3,4,5,6,7,8,9,10,11,12";
}

function initMap(home: Home): void {
  map = L.map("map").setView([home.lat, home.lng], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap · observations © iNaturalist",
    maxZoom: 14,
  }).addTo(map);
  homeMarker = L.circleMarker([home.lat, home.lng], {
    radius: 7,
    color: HOME_RING,
    weight: 3,
    fillColor: HOME_FILL,
    fillOpacity: 1,
  })
    .addTo(map)
    .bindPopup("Location: " + home.name);
}

function clearMarkers(): void {
  state.markers.forEach((marker) => map.removeLayer(marker));
  state.markers = [];
  clearCamps();
  state.focused = null;
}

function clearCamps(): void {
  state.campMarkers.forEach((marker) => map.removeLayer(marker));
  state.campMarkers = [];
}

const campsOn = (): boolean => qs<HTMLInputElement>("#show-camps").checked;
const freeOnly = (): boolean => qs<HTMLInputElement>("#free-camps").checked;

// Fetch + plot campgrounds near the focused region. No-op (just clears) when the toggle
// is off. Failures degrade quietly to a status line rather than throwing.
async function loadCamps(): Promise<void> {
  clearCamps();
  if (!campsOn() || !state.focused) return;
  const { lat, lng } = state.focused;
  let sites: CampSite[];
  try {
    sites = await getJson<CampSite[]>(
      `/api/camps?lat=${lat}&lng=${lng}&free_only=${freeOnly()}`,
    );
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  sites.forEach((site) => {
    const isFree = site.free === true;
    const cost = isFree ? "free" : site.fee ? site.fee : "cost unknown";
    const marker = L.circleMarker([site.center_lat, site.center_lng], {
      radius: 5,
      color: HOME_RING,
      weight: 1,
      fillColor: isFree ? CAMP_FREE : CAMP_PAID,
      fillOpacity: 0.9,
    })
      .addTo(map)
      .bindPopup(campPopup(site, cost));
    state.campMarkers.push(marker);
  });
}

// Build the campground popup from DOM nodes rather than an HTML string: `site.name` and the
// fee text come from an external API, so `textContent` escapes them instead of injecting raw
// HTML. `site.url` is server-constructed (recreation.gov + facility id), so it's a safe href.
function campPopup(site: CampSite, cost: string): HTMLElement {
  const root = document.createElement("div");
  const title = document.createElement("b");
  title.textContent = site.name;
  const link = document.createElement("a");
  link.href = site.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Recreation.gov ↗";
  root.append(
    title,
    document.createElement("br"),
    document.createTextNode(`${site.distance_km} km · ${cost}`),
    document.createElement("br"),
    link,
  );
  return root;
}

function focusRegion(lat: number, lng: number): void {
  state.focused = { lat, lng };
  loadCamps();
}

function plot(lat: number, lng: number, weight: number, popup: string, live: boolean): L.CircleMarker {
  const marker = L.circleMarker([lat, lng], {
    radius: 6 + 14 * weight,
    color: live ? LIVE : HEAT,
    fillColor: live ? LIVE : HEAT,
    fillOpacity: 0.6,
    weight: 1.5,
  })
    .addTo(map)
    .bindPopup(popup);
  state.markers.push(marker);
  return marker;
}

async function runDestinations(): Promise<void> {
  setStatus("Ranking…");
  clearMarkers();
  let regions: RegionScore[];
  try {
    regions = await getJson<RegionScore[]>(
      `/api/destinations?months=${monthsParam()}&species=${selectedSpecies()}`,
    );
  } catch (error) {
    setStatus(errorDetail(error));
    return;
  }
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML =
      "<p class='hint'>No regions in range for those months. Try widening months or running Refresh.</p>";
    setStatus("");
    return;
  }
  panel.innerHTML = "";
  regions.forEach((region, rank) => {
    const marker = plot(
      region.center_lat,
      region.center_lng,
      region.score_norm,
      `<b>#${rank + 1}</b> ${region.distance_km} km<br>${region.species.map((hit) => hit.common_name).join(", ")}`,
      region.recent_count > 0,
    );
    const card = document.createElement("div");
    card.className = "rank";
    card.innerHTML = `
      <h3><span>#${rank + 1} · ${region.distance_km} km</span><span>${region.n_species} spp</span></h3>
      <div class="bar"><span style="width:${(region.score_norm * 100).toFixed(0)}%"></span></div>
      <div class="meta">score ${region.score_norm.toFixed(2)}${region.recent_count ? ` · ${region.recent_count} seen recently` : ""}</div>
      <div class="chips">${region.species
        .slice(0, 6)
        .map((hit) => speciesChip({ ...hit, label: (hit.w_pheno * 100).toFixed(0) + "%" }))
        .join("")}</div>`;
    card.onclick = () => {
      map.setView([region.center_lat, region.center_lng], 9);
      marker.openPopup();
      focusRegion(region.center_lat, region.center_lng);
      loadCalendar(region.region_id);
    };
    panel.appendChild(card);
  });
  setStatus(`${regions.length} regions`);
}

async function loadCalendar(regionId: string): Promise<void> {
  const calendar = await getJson<Calendar>(
    `/api/calendar?region_id=${regionId}&species=${selectedSpecies()}`,
  );
  const peak = Math.max(1, ...Object.values(calendar).map((bucket) => bucket.total));
  let rows = "";
  for (let month = 1; month <= 12; month++) {
    const bucket = calendar[month];
    if (!bucket) continue;
    const fraction = bucket.total / peak;
    const background = `rgba(${HEAT_RGB},${fraction.toFixed(2)})`;
    const speciesText = Object.entries(bucket.species)
      .map(([name, count]) => `${name}: ${count}`)
      .join(", ");
    rows += `<tr><td>${MONTHS[month - 1]}</td>
      <td class="heat" style="background:${background}">${bucket.total || ""}</td>
      <td class="meta">${speciesText}</td></tr>`;
  }
  qs("#panel").innerHTML = `<h3 style="margin-top:0">Calendar · region ${regionId}</h3>
    <table class="cal"><tr><th>Month</th><th>Obs</th><th>Species</th></tr>${rows}</table>`;
  setStatus("");
}

async function runAlerts(): Promise<void> {
  setStatus("Checking recent activity…");
  clearMarkers();
  const regions = await getJson<AlertRegion[]>(`/api/alerts?species=${selectedSpecies()}`);
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML = "<p class='hint'>No target species observed in the trailing window yet.</p>";
    setStatus("");
    return;
  }
  panel.innerHTML = "<h3 style='margin-top:0'>Fruiting now / recently</h3>";
  regions.forEach((region) => {
    plot(
      region.center_lat,
      region.center_lng,
      Math.min(1, region.total / 10),
      `${region.distance_km} km · ${region.total} recent`,
      true,
    );
    const card = document.createElement("div");
    card.className = "rank";
    card.innerHTML = `<h3><span>${region.distance_km} km</span><span>${region.total} recent</span></h3>
      <div class="chips">${region.species
        .map((hit) => speciesChip({ ...hit, label: hit.count + " · " + hit.last_seen }, "live"))
        .join("")}</div>`;
    card.onclick = () => {
      map.setView([region.center_lat, region.center_lng], 9);
      focusRegion(region.center_lat, region.center_lng);
    };
    panel.appendChild(card);
  });
  setStatus(`${regions.length} active regions`);
}

function setStatus(text: string): void {
  qs("#status").textContent = text;
}

function initTabs(): void {
  document.querySelectorAll<HTMLButtonElement>(".tabs button").forEach((button) => {
    button.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((other) => other.classList.remove("active"));
      button.classList.add("active");
      state.view = (button.dataset.view as View) ?? "destinations";
      if (state.view === "destinations") runDestinations();
      else if (state.view === "alerts") runAlerts();
      else
        qs("#panel").innerHTML =
          "<p class='hint'>Click a ranked destination to see its 12-month calendar.</p>";
    };
  });
}

function updateHome(home: Home): void {
  state.home = home;
  qs("#home-name").textContent = home.name;
  qs("#home-coords").textContent = `${home.lat.toFixed(3)}, ${home.lng.toFixed(3)}`;
  qs("#home-radius").textContent = String(Math.round(home.radius_km));
  if (homeMarker) {
    homeMarker.setLatLng([home.lat, home.lng]).bindPopup("Location: " + home.name);
    map.setView([home.lat, home.lng], 8);
  }
}

// Kick off a data refresh and resolve once the server finishes (polls /api/config).
async function startRefresh(message: string): Promise<boolean> {
  setStatus(message);
  qs<HTMLButtonElement>("#refresh").disabled = true;
  await fetch("/api/refresh", { method: "POST" });
  return new Promise((resolve) => {
    const timer = setInterval(async () => {
      const config = await getJson<Config>("/api/config");
      if (!config.refreshing) {
        clearInterval(timer);
        qs<HTMLButtonElement>("#refresh").disabled = false;
        if (config.last_error) setStatus("Refresh error: " + config.last_error);
        else setStatus("Data ready.");
        resolve(!config.last_error);
      }
    }, 2000);
  });
}

async function setLocation(query: string): Promise<void> {
  setStatus("Finding location…");
  let response: { home: Home };
  try {
    response = await postJson<{ home: Home }>("/api/location", { query });
  } catch (error) {
    setStatus(errorDetail(error) || "location not found");
    return;
  }
  updateHome(response.home);
  const succeeded = await startRefresh(
    `Fetching iNaturalist data around ${response.home.name}… (a few minutes)`,
  );
  if (succeeded) runDestinations();
}

async function main(): Promise<void> {
  const config = await getJson<Config>("/api/config");
  state.home = config.home;
  initMonths();
  await initSpecies();
  initMap(config.home);
  updateHome(config.home);
  initTabs();
  qs("#run").onclick = runDestinations;
  qs("#show-camps").onchange = () => loadCamps();
  qs("#free-camps").onchange = () => loadCamps();
  qs("#refresh").onclick = () =>
    startRefresh("Refreshing from iNaturalist…").then((succeeded) => {
      if (succeeded) runDestinations();
    });
  qs<HTMLFormElement>("#locform").onsubmit = (event) => {
    event.preventDefault();
    const query = qs<HTMLInputElement>("#loc").value.trim();
    if (query) setLocation(query);
  };
  // If a refresh is already running (e.g. page reload mid-fetch), reflect it.
  if (config.refreshing) {
    startRefresh("Fetching data…").then((succeeded) => {
      if (succeeded) runDestinations();
    });
  }
}

main();
