"use strict";

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const CURRENT_MONTH = new Date().getMonth() + 1; // 1-12
const state = { months: new Set([CURRENT_MONTH]), view: "destinations", home: null, markers: [] };
let map, homeMarker;

// Marker palette — deliberately non-green so it reads against the green OSM terrain.
const HEAT = "#e6398b";      // magenta — historical strength (destinations)
const HEAT_RGB = "230,57,139";
const LIVE = "#22c3e6";      // cyan — fresh / recently observed
const HOME_FILL = "#ffffff"; // white "you are here" dot
const HOME_RING = "#161a12";

const qs = (selector) => document.querySelector(selector);
const getJson = (path) =>
  fetch(path).then((response) =>
    response.ok ? response.json() : response.json().then((body) => Promise.reject(body)),
  );
const inatUrl = (taxonId) => `https://www.inaturalist.org/taxa/${taxonId}`;
const speciesChip = (hit, extraClass) =>
  `<a class="chip${extraClass ? " " + extraClass : ""}" href="${inatUrl(hit.taxon_id)}"
      target="_blank" rel="noopener" onclick="event.stopPropagation()"
   >${hit.common_name}${hit.label ? " · " + hit.label : ""}</a>`;

function initMonths() {
  const box = qs("#months");
  MONTHS.forEach((label, index) => {
    const month = index + 1;
    const button = document.createElement("button");
    button.textContent = label;
    if (state.months.has(month)) button.classList.add("on");
    button.onclick = () => {
      if (state.months.has(month)) { state.months.delete(month); button.classList.remove("on"); }
      else { state.months.add(month); button.classList.add("on"); }
    };
    box.appendChild(button);
  });
}

async function initSpecies() {
  const species = await getJson("/api/species");
  const select = qs("#species");
  species.forEach((entry) => {
    const option = document.createElement("option");
    option.value = entry.taxon_id;
    option.textContent = entry.common_name;
    option.title = "View on iNaturalist";
    select.appendChild(option);
  });
}

function selectedSpecies() {
  const chosen = [...qs("#species").selectedOptions].map((option) => option.value);
  return chosen.length ? chosen.join(",") : "all";
}

function monthsParam() {
  const ordered = [...state.months].sort((left, right) => left - right);
  return ordered.length ? ordered.join(",") : "1,2,3,4,5,6,7,8,9,10,11,12";
}

function initMap() {
  map = L.map("map").setView([state.home.lat, state.home.lng], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap · observations © iNaturalist",
    maxZoom: 14,
  }).addTo(map);
  homeMarker = L.circleMarker([state.home.lat, state.home.lng], {
    radius: 7, color: HOME_RING, weight: 3, fillColor: HOME_FILL, fillOpacity: 1,
  }).addTo(map).bindPopup("Location: " + state.home.name);
}

function clearMarkers() {
  state.markers.forEach((marker) => map.removeLayer(marker));
  state.markers = [];
}

function plot(lat, lng, weight, popup, live) {
  const marker = L.circleMarker([lat, lng], {
    radius: 6 + 14 * weight,
    color: live ? LIVE : HEAT,
    fillColor: live ? LIVE : HEAT,
    fillOpacity: 0.6,
    weight: 1.5,
  }).addTo(map).bindPopup(popup);
  state.markers.push(marker);
  return marker;
}

async function runDestinations() {
  setStatus("Ranking…");
  clearMarkers();
  let regions;
  try {
    regions = await getJson(`/api/destinations?months=${monthsParam()}&species=${selectedSpecies()}`);
  } catch (error) { return setStatus(error.detail || "error"); }
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML =
      "<p class='hint'>No regions in range for those months. Try widening months or running Refresh.</p>";
    return setStatus("");
  }
  panel.innerHTML = "";
  regions.forEach((region, rank) => {
    const marker = plot(region.center_lat, region.center_lng, region.score_norm,
      `<b>#${rank + 1}</b> ${region.distance_km} km<br>${region.species.map((hit) => hit.common_name).join(", ")}`,
      region.recent_count > 0);
    const card = document.createElement("div");
    card.className = "rank";
    card.innerHTML = `
      <h3><span>#${rank + 1} · ${region.distance_km} km</span><span>${region.n_species} spp</span></h3>
      <div class="bar"><span style="width:${(region.score_norm * 100).toFixed(0)}%"></span></div>
      <div class="meta">score ${region.score_norm.toFixed(2)}${region.recent_count ? ` · ${region.recent_count} seen recently` : ""}</div>
      <div class="chips">${region.species.slice(0, 6).map((hit) =>
        speciesChip({ ...hit, label: (hit.w_pheno * 100).toFixed(0) + "%" })).join("")}</div>`;
    card.onclick = () => {
      map.setView([region.center_lat, region.center_lng], 9);
      marker.openPopup();
      loadCalendar(region.region_id);
    };
    panel.appendChild(card);
  });
  setStatus(`${regions.length} regions`);
}

async function loadCalendar(regionId) {
  const calendar = await getJson(`/api/calendar?region_id=${regionId}&species=${selectedSpecies()}`);
  const peak = Math.max(1, ...Object.values(calendar).map((bucket) => bucket.total));
  let rows = "";
  for (let month = 1; month <= 12; month++) {
    const bucket = calendar[month];
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

async function runAlerts() {
  setStatus("Checking recent activity…");
  clearMarkers();
  const regions = await getJson(`/api/alerts?species=${selectedSpecies()}`);
  const panel = qs("#panel");
  if (!regions.length) {
    panel.innerHTML = "<p class='hint'>No target species observed in the trailing window yet.</p>";
    return setStatus("");
  }
  panel.innerHTML = "<h3 style='margin-top:0'>Fruiting now / recently</h3>";
  regions.forEach((region) => {
    plot(region.center_lat, region.center_lng, Math.min(1, region.total / 10),
      `${region.distance_km} km · ${region.total} recent`, true);
    const card = document.createElement("div");
    card.className = "rank";
    card.innerHTML = `<h3><span>${region.distance_km} km</span><span>${region.total} recent</span></h3>
      <div class="chips">${region.species.map((hit) =>
        speciesChip({ ...hit, label: hit.count + " · " + hit.last_seen }, "live")).join("")}</div>`;
    card.onclick = () => map.setView([region.center_lat, region.center_lng], 9);
    panel.appendChild(card);
  });
  setStatus(`${regions.length} active regions`);
}

function setStatus(text) { qs("#status").textContent = text; }

function initTabs() {
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((other) => other.classList.remove("active"));
      button.classList.add("active");
      state.view = button.dataset.view;
      if (state.view === "destinations") runDestinations();
      else if (state.view === "alerts") runAlerts();
      else qs("#panel").innerHTML =
        "<p class='hint'>Click a ranked destination to see its 12-month calendar.</p>";
    };
  });
}

function updateHome(home) {
  state.home = home;
  qs("#home-name").textContent = home.name;
  qs("#home-coords").textContent = `${home.lat.toFixed(3)}, ${home.lng.toFixed(3)}`;
  if (homeMarker) {
    homeMarker.setLatLng([home.lat, home.lng]).bindPopup("Location: " + home.name);
    map.setView([home.lat, home.lng], 8);
  }
}

// Kick off a data refresh and resolve once the server finishes (polls /api/config).
async function startRefresh(message) {
  setStatus(message);
  qs("#refresh").disabled = true;
  await fetch("/api/refresh", { method: "POST" });
  return new Promise((resolve) => {
    const timer = setInterval(async () => {
      const config = await getJson("/api/config");
      if (!config.refreshing) {
        clearInterval(timer);
        qs("#refresh").disabled = false;
        if (config.last_error) setStatus("Refresh error: " + config.last_error);
        else setStatus("Data ready.");
        resolve(!config.last_error);
      }
    }, 2000);
  });
}

async function setLocation(query) {
  setStatus("Finding location…");
  let response;
  try {
    response = await postJson("/api/location", { query });
  } catch (error) { return setStatus(error.detail || "location not found"); }
  updateHome(response.home);
  const succeeded = await startRefresh(
    `Fetching iNaturalist data around ${response.home.name}… (a few minutes)`,
  );
  if (succeeded) runDestinations();
}

// POST helper returning parsed JSON or rejecting with the error body.
async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) return Promise.reject(await response.json());
  return response.json();
}

async function main() {
  const config = await getJson("/api/config");
  state.home = config.home;
  initMonths();
  await initSpecies();
  initMap();
  initTabs();
  qs("#run").onclick = runDestinations;
  qs("#refresh").onclick = () =>
    startRefresh("Refreshing from iNaturalist…").then((succeeded) => succeeded && runDestinations());
  qs("#locform").onsubmit = (event) => {
    event.preventDefault();
    const query = qs("#loc").value.trim();
    if (query) setLocation(query);
  };
  // If a refresh is already running (e.g. page reload mid-fetch), reflect it.
  if (config.refreshing) {
    startRefresh("Fetching data…").then((succeeded) => succeeded && runDestinations());
  }
}

main();
