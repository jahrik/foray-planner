import L from "leaflet";
import "leaflet.markercluster";

import type { Home } from "./api/types";
import { dist, qs, state } from "./state";

// Marker palette - bright/neon so it pops on the dark basemap (the default), while still
// reading over the lighter OSM terrain in light mode. Deliberately non-green vs the terrain.
export const HEAT = "#ff2d9b"; // hot magenta - historical strength (destinations)
export const HEAT_RGB = "255,45,155";
export const LIVE = "#22e0ff"; // electric cyan - fresh / recently observed
export const HOME_FILL = "#ffffff"; // white "you are here" dot
export const HOME_RING = "#0c0d09";
export const CAMP_FREE = "#ffe14d"; // neon gold - free / no-fee campground
export const CAMP_PAID = "#ff9e2e"; // bright amber - fee or unknown-cost campground
export const CAMP_OSM = "#1fe6d0"; // neon teal - OSM dispersed layer (solid = reported, dashed = proxy)
// Public-land ownership fill - non-green so it reads over the terrain, one hue per agency.
export const LAND_COLORS: Record<string, string> = {
  BLM: "#e8974a", // bright ochre
  USFS: "#a693ff", // bright violet
  Tribal: "#4d79ff", // bright blue - sovereign nation land, visually distinct from BLM/USFS
};
export const LAND_DEFAULT = "#b5b5b5"; // any other agency
// Bright red - a destination card's selected trail (layers.ts's selectTrailhead), drawn solid
// when its geometry comes from real OSM topology, dashed when it's the nearest-cached fallback.
export const TRAIL = "#ff5555";
export const PLAN_STOP = "#ffd060"; // neon gold - planned-route stops and connecting line
export const PRECISE = "#c792ea"; // bright lavender - known-precise (non-obscured) observation pin

// A single standard OSM tile source for both themes - dark mode inverts it via CSS
// (`invert() hue-rotate()` in style.css) instead of swapping in a separate dark tileset.
// The CARTO dark_all raster this used to load renders minor labels (peaks, lakes, wilderness
// boundaries) in very low-contrast gray by design, and no CSS brightness/contrast filter could
// fix that without also crushing the rest of the tile. Inverting OSM's normal high-contrast
// dark-on-light labels turns them into equally high-contrast light-on-dark, so everything from
// city names down to trail/forest labels stays legible.
const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION = "© OpenStreetMap · observations © iNaturalist";
let tileLayer: L.TileLayer | null = null;

export let map: L.Map;
let homeMarker: L.CircleMarker;
// Precise-observation pins cluster (issue #161): a dense area can easily clear a thousand
// individual points, which would bury the map as flat markers - clustering collapses nearby
// pins into a count badge that splits apart on zoom, same value as a heatmap without losing
// per-observation click-through. Created once in initMap() and reused (clearLayers() on
// reload) rather than recreated per fetch, since MarkerClusterGroup itself owns the spatial
// index that makes re-clustering on zoom cheap.
let preciseCluster: L.MarkerClusterGroup;

export const currentTheme = (): "dark" | "light" =>
  document.documentElement.dataset.theme === "light" ? "light" : "dark";

export function setTiles(_theme: "dark" | "light"): void {
  if (!map) return; // map not built yet; initMap lays the first tiles for the current theme
  if (tileLayer) return; // same tile source for both themes now; the CSS filter handles dark mode
  tileLayer = L.tileLayer(TILE_URL, { attribution: TILE_ATTRIBUTION, maxZoom: 14 }).addTo(map);
}

// A plain DOM block below the map (not a Leaflet map-overlay control) - on small screens an
// on-map legend ate half the visible map, so this renders as a normal document element instead.
// Each entry is its own block-level span (not <br>-joined) so the mobile flex-wrap layout can
// wrap entries cleanly instead of fighting <br>'s line-break semantics.
//
// Destination markers (historical/recently-observed) and precise observations (verified-location
// pins for whichever destination is focused - no layer toggle, see layers.ts's
// loadPreciseObservations) are on the map by default, so they're always in the legend - camp/
// land entries only appear once their layer is actually toggled on, instead of explaining
// markers that aren't there yet. Called from layers.ts after every camps/land/precise load or
// clear, so it always mirrors what's on the map. Trails have no toggle (a destination card's
// Trails tab draws its own selected-trail line on demand, see selectTrailhead) so they're never
// in this legend.
export function renderLegend(): void {
  const el = qs("#legend");
  const camps = (document.getElementById("show-camps") as HTMLInputElement | null)?.checked;
  const dispersed = (document.getElementById("show-dispersed") as HTMLInputElement | null)?.checked;
  const blm = (document.getElementById("show-land-blm") as HTMLInputElement | null)?.checked;
  const usfs = (document.getElementById("show-land-usfs") as HTMLInputElement | null)?.checked;
  const tribal = (document.getElementById("show-land-tribal") as HTMLInputElement | null)?.checked;
  const entries: [string, string][] = [
    [HEAT, "Destination (historical)"],
    [LIVE, "Recently observed"],
    [PRECISE, "Precise observation (verified location)"],
  ];
  if (camps) {
    entries.push([CAMP_FREE, "Free campground"], [CAMP_PAID, "Paid / unknown campground"]);
  }
  if (dispersed) entries.push([CAMP_OSM, "Reported campsite (OSM)"]);
  if (blm) entries.push([LAND_COLORS.BLM ?? LAND_DEFAULT, "BLM land"]);
  if (usfs) entries.push([LAND_COLORS.USFS ?? LAND_DEFAULT, "USFS land"]);
  if (tribal) entries.push([LAND_COLORS.Tribal ?? LAND_DEFAULT, "Tribal land"]);
  el.innerHTML = entries
    .map(([color, label]) => `<span class="legend-item"><i style="background:${color}"></i>${label}</span>`)
    .join("");
}

// Cluster badge styling: a lavender disc (same hue as an individual pin) with a dark ring and
// the count in the middle - readable on both the dark and light basemap, and visually reads as
// "more precise pins" rather than borrowing the plugin's default blue/yellow/orange severity
// gradient, which has no meaning here.
function preciseClusterIcon(cluster: L.MarkerCluster): L.DivIcon {
  const count = cluster.getChildCount();
  const size = count < 10 ? 30 : count < 100 ? 36 : 42;
  return L.divIcon({
    html: `<div style="
      width:${size}px;height:${size}px;line-height:${size}px;
      background:${PRECISE};border:2px solid ${HOME_RING};border-radius:50%;
      text-align:center;font-weight:600;color:${HOME_RING};
    ">${count}</div>`,
    className: "precise-cluster-icon",
    iconSize: L.point(size, size),
  });
}

export function initMap(home: Home): void {
  map = L.map("map").setView([home.lat, home.lng], 7);
  setTiles(currentTheme());
  preciseCluster = L.markerClusterGroup({
    iconCreateFunction: preciseClusterIcon,
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
  }).addTo(map);
  renderLegend();
  homeMarker = L.circleMarker([home.lat, home.lng], {
    radius: 7,
    color: HOME_RING,
    weight: 3,
    fillColor: HOME_FILL,
    fillOpacity: 1,
    bubblingMouseEvents: false,
  })
    .addTo(map)
    .bindPopup("Location: " + home.name);

  // Clicking a city (or anywhere else) on the base map sets it as home, the same as searching
  // for it. Markers/polygons set `bubblingMouseEvents: false` so clicking one (to open its
  // popup) doesn't also fire this and stomp the location.
  map.on("click", (e: L.LeafletMouseEvent) => {
    onMapClick?.(e.latlng.lat, e.latlng.lng);
  });
}

let onMapClick: ((lat: number, lng: number) => void) | null = null;

export function setMapClickHandler(handler: (lat: number, lng: number) => void): void {
  onMapClick = handler;
}

export function updateHome(home: Home): void {
  state.home = home;
  qs("#home-name").textContent = home.name;
  qs("#home-coords").textContent = `${home.lat.toFixed(3)}, ${home.lng.toFixed(3)}`;
  qs("#home-radius").textContent = dist(home.radius_km);
  if (homeMarker) {
    homeMarker.setLatLng([home.lat, home.lng]).bindPopup("Location: " + home.name);
    map.setView([home.lat, home.lng], 8);
  }
}

// Matches the same 111 km/degree approximation used backend-side (camps.py, land.py,
// scoring.py) to convert a region's cell_deg grid width into meters.
const KM_PER_DEG = 111.0;

// Same footprint plot() uses for a region's true (not score-scaled) circle - see selectSize.
// Exported so layers.ts can scope the precise-observations fetch to exactly the ground a
// selected destination bubble represents, instead of the whole search radius (issue #161
// follow-up: a radius-wide fetch put a cluster badge on every destination on the map at once,
// visually burying the destination bubbles they were competing with).
export const regionRadiusKm = (): number => (state.cellDeg * KM_PER_DEG) / 2;

// Per-marker sizing so a selected region can snap between its score size and its true
// geographic footprint (see selectSize/deselectSize below) without re-plotting.
const sizing = new WeakMap<L.Circle, { scoreRadius: number; trueRadius: number }>();

// No popup bound here - a bubble hovering over the marker you're trying to look at was jarring,
// and the same info (rank, distance, species) already lives on the matching card in the side
// panel. Callers wire the marker's click to highlight/scroll to that card instead.
//
// Hue distinguishes category (magenta = historical destination, cyan = recently observed);
// score is carried by size and fill opacity within that hue - a faint, small circle is a weak
// match, a bold, larger one is a strong one. Uses L.circle (a geographic radius in meters, not
// L.circleMarker's fixed pixel radius) so selecting a region can snap it to its true cell_deg
// footprint (selectSize) - see the comment there for why - and so at any size the circle still
// scales correctly with zoom instead of reading as an arbitrary screen-space blob.
export function plot(lat: number, lng: number, weight: number, live: boolean): L.Circle {
  const trueRadius = ((state.cellDeg * KM_PER_DEG) / 2) * 1000;
  const scoreRadius = trueRadius * (0.3 + weight);
  const marker = L.circle([lat, lng], {
    radius: scoreRadius,
    color: live ? LIVE : HEAT,
    fillColor: live ? LIVE : HEAT,
    fillOpacity: 0.15 + 0.45 * weight,
    opacity: 0.4 + 0.5 * weight,
    weight: 1.5,
    bubblingMouseEvents: false,
  }).addTo(map);
  sizing.set(marker, { scoreRadius, trueRadius });
  state.markers.push(marker);
  return marker;
}

// Selecting a region (marker or card click) snaps its circle from the score-sized preview to
// its true real-world cell_deg footprint, computed from the same live config value as plot()
// (never hard-coded), so the user can see exactly how much ground that dot actually represents.
// Fill goes very transparent at this size so the map underneath - which the circle now likely
// covers a large part of - stays readable.
export function selectSize(marker: L.Circle): void {
  const info = sizing.get(marker);
  if (!info) return;
  marker.setRadius(info.trueRadius);
  marker.setStyle({ fillOpacity: 0.08 });
}

// Reverts a previously selected marker back to its score-scaled preview size/opacity - called
// when a different region gets selected, so only one circle shows its true footprint at a time.
export function deselectSize(marker: L.Circle, weight: number): void {
  const info = sizing.get(marker);
  if (!info) return;
  marker.setRadius(info.scoreRadius);
  marker.setStyle({ fillOpacity: 0.15 + 0.45 * weight });
}

export function clearMarkers(): void {
  state.markers.forEach((marker) => map.removeLayer(marker));
  state.markers = [];
  clearCamps();
  clearLand();
  clearSelectedTrail();
  clearPlanRoute();
  clearPrecise();
  state.focused = null;
}

export function clearPrecise(): void {
  preciseCluster.clearLayers();
}

// Adds a precise-observation pin into the cluster group (see preciseCluster above) instead of
// directly onto the map - the cluster group itself decides whether it renders standalone or
// folded into a nearby cluster badge at the current zoom.
export function addPreciseMarker(marker: L.CircleMarker): void {
  preciseCluster.addLayer(marker);
}

export function clearCamps(): void {
  state.campMarkers.forEach((marker) => map.removeLayer(marker));
  state.campMarkers = [];
}

export function clearLand(): void {
  if (state.landLayer) {
    map.removeLayer(state.landLayer);
    state.landLayer = null;
  }
}

// Clears whichever trail is currently drawn from a destination card's Trails tab selection
// (layers.ts's selectTrailhead) - at most one at a time, see state.selectedTrailLayer.
export function clearSelectedTrail(): void {
  if (state.selectedTrailLayer) {
    map.removeLayer(state.selectedTrailLayer);
    state.selectedTrailLayer = null;
  }
}

export function clearPlanRoute(): void {
  if (state.planRouteLayer) {
    map.removeLayer(state.planRouteLayer);
    state.planRouteLayer = null;
  }
}
