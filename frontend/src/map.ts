import L from "leaflet";

import type { Home } from "./api/types";
import { qs, state } from "./state";

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
};
export const LAND_DEFAULT = "#b5b5b5"; // any other agency
export const TRAIL = "#ff5555"; // bright red - the walking network (paths/routes) + trailhead dots
export const PLAN_STOP = "#ffd060"; // neon gold - planned-route stops and connecting line

// Theme-aware basemap: a dark CARTO raster under dark mode, standard OSM under light. The bright
// marker palette above reads well over both. Attribution stays per each provider's terms.
const TILES = {
  dark: {
    url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    attribution: "© OpenStreetMap © CARTO · observations © iNaturalist",
  },
  light: {
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: "© OpenStreetMap · observations © iNaturalist",
  },
} as const;
let tileLayer: L.TileLayer | null = null;

export let map: L.Map;
let homeMarker: L.CircleMarker;

export const currentTheme = (): "dark" | "light" =>
  document.documentElement.dataset.theme === "light" ? "light" : "dark";

export function setTiles(theme: "dark" | "light"): void {
  if (!map) return; // map not built yet; initMap lays the first tiles for the current theme
  if (tileLayer) tileLayer.remove();
  const tiles = TILES[theme];
  tileLayer = L.tileLayer(tiles.url, { attribution: tiles.attribution, maxZoom: 14 }).addTo(map);
}

export function initMap(home: Home): void {
  map = L.map("map").setView([home.lat, home.lng], 7);
  setTiles(currentTheme());
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

export function updateHome(home: Home): void {
  state.home = home;
  qs("#home-name").textContent = home.name;
  qs("#home-coords").textContent = `${home.lat.toFixed(3)}, ${home.lng.toFixed(3)}`;
  qs("#home-radius").textContent = String(Math.round(home.radius_km));
  if (homeMarker) {
    homeMarker.setLatLng([home.lat, home.lng]).bindPopup("Location: " + home.name);
    map.setView([home.lat, home.lng], 8);
  }
}

export function plot(
  lat: number,
  lng: number,
  weight: number,
  popup: string,
  live: boolean,
): L.CircleMarker {
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

export function clearMarkers(): void {
  state.markers.forEach((marker) => map.removeLayer(marker));
  state.markers = [];
  clearCamps();
  clearLand();
  clearTrails();
  clearPlanRoute();
  state.focused = null;
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

export function clearTrails(): void {
  if (state.trailLayer) {
    map.removeLayer(state.trailLayer);
    state.trailLayer = null;
  }
}

export function clearPlanRoute(): void {
  if (state.planRouteLayer) {
    map.removeLayer(state.planRouteLayer);
    state.planRouteLayer = null;
  }
}
