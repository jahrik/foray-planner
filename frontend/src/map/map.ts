import L from "leaflet";
import "leaflet.markercluster";

import type { Home } from "../api/types";
import { FIRE_ACTIVE, FIRE_SCAR } from "./basemap-fire";
import { LAND_COLORS, LAND_DEFAULT } from "./basemap-land";
import { FORAGE_RAMP, FORAGE_TIER_LABELS } from "./forage";
import { clearCardCampMarkers, clearCamps, clearTrailheadMarkers } from "./pins";
import { clearLayer, clearLayerList } from "./layer-lifecycle";
import { clearSatelliteOverlay, resetSelection } from "./destinations";
import { inspectRoadAt } from "./inspect";
import { circleStyle } from "./markers";
import { dist, onScopeChange, qs, state } from "../state";

// Marker palette. Destination + recency markers now read their colour from tokens.css at
// runtime (markerPalette below) so they track the active theme and stay on the spore-print
// palette (issue #301). HEAT_RGB is the rust used for the calendar heat cells
// (destination-tabs.ts); the rest of the constants below are fixed-hue overlay markers
// (camps, land, fire, trails, plan pins) that keep their own distinct colours.
export const HEAT_RGB = "122,67,38"; // --rust, for the phenology calendar heat cells
export const HOME_FILL = "#ffffff"; // white "you are here" dot
export const HOME_RING = "#0c0d09";
export const CAMP_FREE = "#ffe14d"; // neon gold - free / no-fee campground
export const CAMP_PAID = "#ff9e2e"; // bright amber - fee or unknown-cost campground
export const CAMP_OSM = "#1fe6d0"; // neon teal - OSM dispersed-camping layer (reported sites)
// Bright red - a destination card's selected trail (layers.ts's selectTrailhead), drawn solid
// when its geometry comes from real OSM topology, dashed when it's the nearest-cached fallback.
export const TRAIL = "#ff5555";
// Teal - a selected *walk-in* forest road (gated to vehicles, open on foot). Reads as distinct
// from the red drive-in trail line; matching legend entry + Trails-tab chip (issue A4b).
export const TRAIL_WALKIN = "#1fb6a6";
export const PLAN_STOP = "#ffd060"; // neon gold - planned-route stops and connecting line

// The persistent "you are here" dot: a white fill with a dark ring. Shared by the base-map home
// marker (initMap) and the plan-route start marker (plan.ts runPlan) so the two stay identical.
export const HOME_DOT_STYLE = circleStyle({
  radius: 7,
  fill: HOME_FILL,
  stroke: HOME_RING,
  weight: 3,
  fillOpacity: 1,
});

// The basemap is a self-hosted Protomaps PMTiles archive rendered by MapLibre GL, mounted
// inside Leaflet via ./basemap. Its light/dark styles are real cartography (no CSS invert()
// hack), swapped on theme change. There is no raster fallback: the server must hand the
// frontend a `basemap_url` in /api/config (FORAY_BASEMAP_URL) or the map has no base layer.
const DATA_ATTRIBUTION = "observations © iNaturalist · elevation &amp; weather © Open-Meteo";
const VECTOR_ATTRIBUTION = `© OpenStreetMap · © Protomaps · ${DATA_ATTRIBUTION}`;
// Shown only when a terrain layer is active (hillshade/contours from the DEM tiles).
const TERRAIN_ATTRIBUTION =
  'terrain <a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">© Tilezen / Mapzen, USGS, NASA</a>';
let tileTheme: "dark" | "light" | null = null;

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

// Marker colours come from tokens.css so they follow the theme toggle. cssVar falls back to
// the light-theme literal when the stylesheet is not in the document yet (unit tests, the
// brief window before style.css loads).
function cssVar(name: string, fallback: string): string {
  if (typeof getComputedStyle !== "function") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

type MarkerPalette = { rust: string; flush: string; purple: string; moss: string; spore: string };
let paletteCache: MarkerPalette | null = null;
let paletteCacheTheme: "dark" | "light" | null = null;

// Cached per theme - plot() calls this once per region, and getComputedStyle on each call
// would be hundreds of style reads per render (Copilot review). The toggle in ui-prefs.ts
// swaps data-theme then calls setTiles(), so currentTheme() flipping is the invalidation cue.
export function markerPalette(): MarkerPalette {
  const theme = currentTheme();
  if (paletteCache && paletteCacheTheme === theme) return paletteCache;
  paletteCache = {
    rust: cssVar("--rust", "#7a4326"),
    flush: cssVar("--flush", "#5f7d3e"),
    purple: cssVar("--purple", "#4a3a4d"),
    moss: cssVar("--moss", "#4c5d43"),
    spore: cssVar("--spore", "#b06a82"),
  };
  paletteCacheTheme = theme;
  return paletteCache;
}

// Mount the MapLibre GL vector basemap, or swap its style on a later theme change. Named
// setTiles() because ui-prefs.ts's theme toggle calls it after flipping data-theme. A no-op
// when the server sent no basemap_url - the map then has overlays but no base layer.
//
// The MapLibre GL stack (~280 kB gzip) lives in the code-split ./basemap chunk, loaded here so
// it fetches in parallel with the first render rather than bloating the entry bundle.
// `vectorMounting` guards the async gap so initMap() + an immediate theme toggle can't mount
// twice; it's cleared again if the mount throws so a later call can retry.
let vectorMounting = false;
// Held after the first load so the synchronous map-click handler can reach getGlMap() for
// click-to-inspect without another dynamic import.
let basemapModule: typeof import("./basemap") | null = null;

// The inspect module needs the live GL map handle without another dynamic import of its own.
export function getBasemapModule(): typeof import("./basemap") | null {
  return basemapModule;
}

// Grouped once here (see basemap.ts's `TileUrls` doc) rather than re-spelled at each call site.
function tileUrls(): import("./basemap").TileUrls {
  return {
    basemapUrl: state.basemapUrl,
    terrainUrl: state.terrainUrl,
    satelliteUrl: state.satelliteTilesUrl,
    trailsUrl: state.trailsTilesUrl,
    landUrl: state.landTilesUrl,
    fireUrl: state.fireTilesUrl,
  };
}

export function setTiles(): void {
  if (!map || !state.basemapUrl) return;
  // Detached on purpose (the GL stack loads async), so swallow-and-log any rejection - a failed
  // basemap chunk load or mount shouldn't surface as an unhandled promise rejection.
  applyVectorBasemap(currentTheme()).catch((error) => {
    console.error("vector basemap failed to load", error);
  });
}

async function applyVectorBasemap(theme: "dark" | "light"): Promise<void> {
  const basemap = await import("./basemap");
  basemapModule = basemap;
  if (!map || !state.basemapUrl) return;
  if (basemap.hasVectorBasemap()) {
    if (tileTheme === theme) return; // already showing the right style
    basemap.setVectorBasemapTheme(tileUrls(), theme);
    tileTheme = theme;
    return;
  }
  if (vectorMounting) return;
  vectorMounting = true;
  try {
    basemap.mountVectorBasemap(map, tileUrls(), theme);
  } catch (error) {
    vectorMounting = false; // let a later setTiles() retry
    throw error;
  }
  map.attributionControl.addAttribution(VECTOR_ATTRIBUTION);
  if (state.terrainUrl) map.attributionControl.addAttribution(TERRAIN_ATTRIBUTION);
  tileTheme = theme;
  // Adding a layer / attribution rebuilds the attribution control's innerHTML (Leaflet
  // _update), wiping the ⓘ toggle - re-decorate so a live theme switch doesn't leave the
  // credits fully expanded.
  decorateAttribution(map);
  // maplibre-gl-leaflet folds the GL style's own source attribution into the Leaflet control
  // on the GL 'load' event - async, after this runs - and that _update wipes the ⓘ toggle
  // again, leaving the full credit slab expanded (covers the map on mobile). Re-collapse it
  // once the style is in.
  const gl = basemap.getGlMap();
  if (gl) {
    if (gl.loaded()) decorateAttribution(map);
    else gl.once("load", () => decorateAttribution(map));
  }
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
// clear, so it always mirrors what's on the map. Trails have no toggle, but a walk-in (gated)
// forest road drawn from the Trails tab gets a legend entry while its distinct teal line is
// shown (issue A4b, state.selectedTrailWalkIn).
export function renderLegend(): void {
  const el = qs("#legend");
  const camps = (document.getElementById("show-camps") as HTMLInputElement | null)?.checked;
  const dispersed = (document.getElementById("show-dispersed") as HTMLInputElement | null)?.checked;
  const blm = (document.getElementById("show-land-blm") as HTMLInputElement | null)?.checked;
  const usfs = (document.getElementById("show-land-usfs") as HTMLInputElement | null)?.checked;
  const tribal = (document.getElementById("show-land-tribal") as HTMLInputElement | null)?.checked;
  const palette = markerPalette();
  const entries: [string, string][] = [
    [palette.rust, "Top destination"],
    [palette.flush, "Seen in the last few weeks"],
    [palette.moss, "Other region in range"],
    [palette.purple, "Selected"],
    [palette.spore, "Precise observation (verified location)"],
  ];
  if (camps) {
    entries.push([CAMP_FREE, "Free campground"], [CAMP_PAID, "Paid / unknown campground"]);
  }
  if (dispersed) entries.push([CAMP_OSM, "Reported campsite (OSM)"]);
  if ((document.getElementById("show-fire") as HTMLInputElement | null)?.checked) {
    entries.push([FIRE_ACTIVE, "Active wildfire"], [FIRE_SCAR, "Recent burn scar"]);
  }
  if (blm) entries.push([LAND_COLORS.BLM ?? LAND_DEFAULT, "BLM land"]);
  if (usfs) entries.push([LAND_COLORS.USFS ?? LAND_DEFAULT, "USFS land"]);
  if (tribal) entries.push([LAND_COLORS.Tribal ?? LAND_DEFAULT, "Tribal land"]);
  if (state.selectedTrailWalkIn) entries.push([TRAIL_WALKIN, "Walk-in forest road (gated)"]);
  if (state.selectedTrailForage > 0) {
    const idx = state.selectedTrailForage - 1; // 0..2 into the tier-1/2/3 ramp
    entries.push([FORAGE_RAMP[idx] ?? FORAGE_RAMP[2], `Selected trail: ${FORAGE_TIER_LABELS[idx] ?? ""}`]);
  }
  el.innerHTML = entries
    .map(([color, label]) => `<span class="legend-item"><i style="background:${color}"></i>${label}</span>`)
    .join("");
}

// Cluster badge styling: a spore-pink disc (same --spore hue as an individual pin) with a dark
// ring and the count in the middle - readable on both the dark and light basemap, and visually
// reads as "more precise pins" rather than borrowing the plugin's default blue/yellow/orange
// severity gradient, which has no meaning here.
function preciseClusterIcon(cluster: L.MarkerCluster): L.DivIcon {
  const count = cluster.getChildCount();
  const size = count < 10 ? 30 : count < 100 ? 36 : 42;
  return L.divIcon({
    html: `<div style="
      width:${size}px;height:${size}px;line-height:${size}px;
      background:${markerPalette().spore};border:2px solid ${HOME_RING};border-radius:50%;
      text-align:center;font-weight:600;color:${HOME_RING};
    ">${count}</div>`,
    className: "precise-cluster-icon",
    iconSize: L.point(size, size),
  });
}

// The attribution lists four providers (OSM + iNaturalist + Open-Meteo + Esri) and rendered as a
// large opaque slab floating over the map - worst on mobile, where it sat mid-map above the
// bottom sheet and never moved with it. Collapse the credit text behind a real "ⓘ" toggle button
// (focusable, Enter/Space for free, aria-expanded); it also reveals on hover/focus for a mouse.
// The credit links are display:none while collapsed so they stay out of the tab order (#300
// Copilot review).
let attributionOpen = false;

// Leaflet rebuilds the attribution container's innerHTML on every _update (each addAttribution /
// removeAttribution / layer add), which wipes the button + wrapper, so re-run this after any such
// call rather than once at init. Exported: destinations.ts's satellite-overlay functions also
// add/remove an attribution entry and need to re-decorate the same way.
export function decorateAttribution(target: L.Map): void {
  const container = target.attributionControl.getContainer();
  if (!container || container.querySelector(".attrib-toggle")) return;

  const list = document.createElement("span");
  list.className = "attrib-list";
  while (container.firstChild) list.appendChild(container.firstChild);

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "attrib-toggle";
  toggle.setAttribute("aria-label", "Map data credits");

  const apply = (): void => {
    container.classList.toggle("attrib-open", attributionOpen);
    toggle.setAttribute("aria-expanded", String(attributionOpen));
    toggle.textContent = attributionOpen ? "×" : "ⓘ"; // × / ⓘ
  };
  L.DomEvent.on(toggle, "click", (ev) => {
    L.DomEvent.stop(ev);
    attributionOpen = !attributionOpen;
    apply();
  });

  container.append(toggle, list);
  apply();
}

export function initMap(home: Home): void {
  // Zoom control bottom-right (issue #297): the Google-Maps-style slide-out dock and the
  // floating search bar / pill row all sit over the top-left, so the default top-left zoom
  // buttons would be covered.
  // maxZoom must be set on the map itself: the vector basemap layer (L.maplibreGL) carries no
  // zoom range, and without one Leaflet's getBoundsZoom throws "Map has no maxZoom specified"
  // the first time anything fits bounds with padding (destination framing, plan route), which
  // aborts marker/overlay rendering. The old raster tileLayer used to supply maxZoom: 14; the
  // PMTiles archive is z0-15 and MapLibre overzooms cleanly past that.
  map = L.map("map", { zoomControl: false, minZoom: 2, maxZoom: 19 }).setView([home.lat, home.lng], 7);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  setTiles();
  decorateAttribution(map);
  // Sits above the basemap tiles but below the vector overlay pane (circles, trails, markers -
  // default z-index 400) so the selected destination's ring and every other layer still draw on
  // top of the satellite image, not under it (see showSatelliteOverlay).
  map.createPane("satellite");
  map.getPane("satellite")!.style.zIndex = "350";
  preciseCluster = L.markerClusterGroup({
    iconCreateFunction: preciseClusterIcon,
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
  }).addTo(map);
  renderLegend();
  homeMarker = L.circleMarker([home.lat, home.lng], HOME_DOT_STYLE)
    .addTo(map)
    .bindPopup("Location: " + home.name);

  // Clicking a city (or anywhere else) on the base map sets it as home, the same as searching
  // for it. Markers/polygons set `bubblingMouseEvents: false` so clicking one (to open its
  // popup) doesn't also fire this and stomp the location.
  map.on("click", (e: L.LeafletMouseEvent) => {
    if (inspectRoadAt(e.latlng)) return; // hit a road/trail on the vector base - show its tags, don't move home
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
  onScopeChange();
}

export function clearMarkers(): void {
  clearLayerList(map, state.markers);
  clearCamps();
  clearTrailheadMarkers();
  clearCardCampMarkers();
  clearSelectedTrail();
  clearPlanRoute();
  clearPrecise();
  clearSatelliteOverlay();
  resetSelection();
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

// Public-land agency toggles (#show-land-blm/usfs/tribal, layers.ts's loadLand) - live
// setLayoutProperty/setFilter on the vector layers, no fetch (issue #336 PR 2). Same lazy
// `import("./basemap")` pattern as setContoursEnabled/setSatelliteBasemapEnabled below.
export function setLandVisibility(agencies: readonly string[]): void {
  void import("./basemap").then((basemap) => basemap.setLandLayerState(agencies));
}

// The Fire toggle (#show-fire, layers.ts's loadFire) - same reasoning as setLandVisibility.
export function setFireVisibility(visible: boolean): void {
  void import("./basemap").then((basemap) => basemap.setFireLayerState(visible));
}

// The Layers-pill "Contours" toggle. The hillshade is always on with the vector basemap; only
// the contour lines + elevation labels flip. Deferred like the basemap import so the GL stack
// stays out of the entry bundle. The toggle is hidden when there is no terrain (initLayerToggles),
// but guard here too so a stale checked box can't leave the Layers pill counting a layer that
// can never render.
export function setContoursEnabled(on: boolean): void {
  if (!state.basemapUrl || !state.terrainUrl) {
    const box = qs("#show-contours") as HTMLInputElement;
    if (box.checked) {
      box.checked = false;
      box.dispatchEvent(new Event("change"));
    }
    return;
  }
  void import("./basemap").then((basemap) => basemap.setContoursVisible(on));
}

// The Layers-pill "Satellite basemap" toggle (issue #340): swaps the whole vector style for
// real Esri imagery with our own roads/boundaries/labels drawn over it (basemap-satellite.ts)
// instead of the vector map's land/water fills - a different feature from the per-destination
// aerial photo fill (destinations.ts's setAerialEnabled), which stays independently available.
// Hidden when there is no basemap or no satellite tile URL configured, same guard shape as
// setContoursEnabled.
const SATELLITE_ATTRIBUTION = "Imagery © Esri";

export function setSatelliteBasemapEnabled(on: boolean): void {
  if (!state.basemapUrl || !state.satelliteTilesUrl) {
    const box = qs("#show-satellite-basemap") as HTMLInputElement;
    if (box.checked) {
      box.checked = false;
      box.dispatchEvent(new Event("change"));
    }
    return;
  }
  void import("./basemap").then((basemap) => {
    basemap.setSatelliteBasemapMode(tileUrls(), currentTheme(), on);
    if (!map) return;
    if (on) {
      map.attributionControl.addAttribution(SATELLITE_ATTRIBUTION);
    } else {
      map.attributionControl.removeAttribution(SATELLITE_ATTRIBUTION);
    }
    decorateAttribution(map);
  });
}

// Clears whichever trail is currently drawn from a destination card's Trails tab selection
// (layers.ts's selectTrailhead) - at most one at a time, see state.selectedTrailLayer.
export function clearSelectedTrail(): void {
  state.selectedTrailLayer = clearLayer(map, state.selectedTrailLayer);
  const hadEntry = state.selectedTrailWalkIn || state.selectedTrailForage > 0;
  state.selectedTrailWalkIn = false;
  state.selectedTrailForage = 0;
  if (hadEntry) renderLegend(); // drop the walk-in / foraging-density entries we added
}

export function setSelectedTrail(layer: L.Polyline, walkIn = false, forage: 0 | 1 | 2 | 3 = 0): void {
  state.selectedTrailLayer = layer;
  state.selectedTrailWalkIn = walkIn;
  state.selectedTrailForage = forage;
}

export function clearPlanRoute(): void {
  state.planRouteLayer = clearLayer(map, state.planRouteLayer);
}

export function setPlanRoute(layer: L.Polyline): void {
  state.planRouteLayer = layer;
}
