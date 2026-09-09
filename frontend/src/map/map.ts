import L from "leaflet";
import "leaflet.markercluster";

import type { CampSite, Home } from "../api/types";
import { clearLayer, clearLayerList } from "./layer-lifecycle";
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
export const FIRE_ACTIVE = "#ff3b1f"; // hot red - active wildfire perimeter/point (issue #227)
export const FIRE_SCAR = "#ff8c42"; // burnt orange - recent burn scar (dimmer for older years)

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

// Marker hierarchy (issue #301): the map used to draw ~200 near-identical circles. Now rank
// drives three tiers so the shortlist reads at a glance -
//   top 3   (rank 0-2)  : score-scaled circle with a translucent fill + a permanent rank numeral
//   next 7  (rank 3-9)   : ring only (no fill), fixed radius - a marker, not a region wash
//   the rest (rank 10+)  : a small dim moss dot, fixed size, no score scaling
const HERO_RANK_MAX = 2;
const PROMINENT_RANK_MAX = 9;
const DIM_DOT_RADIUS_M = 900; // fixed ground radius for the rank-11+ dots

// Mount the MapLibre GL vector basemap, or swap its style on a later theme change. Named
// setTiles() because ui-prefs.ts's theme toggle calls it after flipping data-theme. A no-op
// when the server sent no basemap_url - the map then has overlays but no base layer.
//
// The MapLibre GL stack (~280 kB gzip) lives in the code-split ./basemap chunk, loaded here so
// it fetches in parallel with the first render rather than bloating the entry bundle.
// `vectorMounting` guards the async gap so initMap() + an immediate theme toggle can't mount
// twice.
let vectorMounting = false;
export function setTiles(): void {
  if (!map || !state.basemapUrl) return;
  void applyVectorBasemap(currentTheme());
}

async function applyVectorBasemap(theme: "dark" | "light"): Promise<void> {
  const basemap = await import("./basemap");
  if (!map || !state.basemapUrl) return;
  if (basemap.hasVectorBasemap()) {
    if (tileTheme === theme) return; // already showing the right style
    basemap.setVectorBasemapTheme(state.basemapUrl, theme);
    tileTheme = theme;
    return;
  }
  if (vectorMounting) return;
  vectorMounting = true;
  basemap.mountVectorBasemap(map, state.basemapUrl, theme);
  map.attributionControl.addAttribution(VECTOR_ATTRIBUTION);
  tileTheme = theme;
  // Adding a layer / attribution rebuilds the attribution control's innerHTML (Leaflet
  // _update), wiping the ⓘ toggle - re-decorate so a live theme switch doesn't leave the
  // credits fully expanded.
  decorateAttribution(map);
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
// call rather than once at init.
function decorateAttribution(target: L.Map): void {
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
  map = L.map("map", { zoomControl: false }).setView([home.lat, home.lng], 7);
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
// geographic footprint (see selectSize/deselectSize below) without re-plotting. `regionId` rides
// along so selectSize can address this marker's satellite fill (showSatelliteOverlay) without
// widening its own signature - every caller already has the marker, not all of them the region.
const sizing = new WeakMap<
  L.Circle,
  {
    scoreRadius: number;
    trueRadius: number;
    weight: number;
    regionId: string;
    baseColor: string;
    restFillOpacity: number;
  }
>();

// The score-scaled fill a destination circle sits at when nothing is selected. Pulled out so
// selectSize/deselectSize and the "dim everything else" pass below all agree on one formula.
const scoreFillOpacity = (weight: number): number => 0.15 + 0.45 * weight;

// When a region is selected its circle grows to its true footprint and can blanket a big patch
// of map; in a dense area (Puget Sound) the other circles it overlaps used to keep compositing
// their fills into a near-opaque blob over it. So on select, every *other* destination circle
// drops to stroke-only (ring, no fill) - the rings still show where the other candidates are
// without burying the focused circle or the basemap under it. Restored on the next select/
// deselect. Scoped to plot()-drawn circles via the `sizing` map, so plan pins and the like are
// untouched.
function setOthersFill(selected: L.Circle, ringOnly: boolean): void {
  for (const marker of state.markers) {
    if (marker === selected) continue;
    const info = sizing.get(marker as L.Circle);
    if (!info) continue; // not a plot()-drawn destination circle (plan pin, etc.)
    marker.setStyle({ fillOpacity: ringOnly ? 0 : info.restFillOpacity });
  }
}

// No popup bound here - a bubble hovering over the marker you're trying to look at was jarring,
// and the same info (rank, distance, species) already lives on the matching card in the side
// panel. Callers wire the marker's click to highlight/scroll to that card instead.
//
// `rank` (0-indexed position in the ranked list) drives the marker hierarchy - see the tier
// constants up top. Colour: rust for a ranked destination, moss for the dim 11+ dots, flush
// green when the region has recent observations. Score is still carried by size + fill opacity
// within the top-10 tier. Uses L.circle (a geographic radius in meters, not L.circleMarker's
// fixed pixel radius) so selecting a region can snap it to its true cell_deg footprint
// (selectSize) and so the circle scales with zoom instead of reading as a screen-space blob.
export function plot(
  lat: number,
  lng: number,
  weight: number,
  live: boolean,
  regionId: string,
  rank: number,
): L.Circle {
  const palette = markerPalette();
  const trueRadius = ((state.cellDeg * KM_PER_DEG) / 2) * 1000;
  const isDim = rank > PROMINENT_RANK_MAX;
  const isHero = rank <= HERO_RANK_MAX;
  const baseColor = live ? palette.flush : isDim ? palette.moss : palette.rust;
  // Hero circles stay score-scaled (their size is part of the read); ranks 4-10 shrink to a
  // tighter ring that reads as a marker, not a region wash; 11+ are small solid dots.
  const scoreRadius = isDim
    ? DIM_DOT_RADIUS_M
    : isHero
      ? trueRadius * (0.35 + weight * 0.65)
      : trueRadius * 0.5;
  // Only the top 3 carry a fill - at this zoom the score-scaled cell circles overlap heavily,
  // so a translucent fill on every one of the top 10 composited into an unreadable blob. Ranks
  // 4-10 are rings only; 11+ are small solid dots (too small to blob).
  const restFillOpacity = isHero ? Math.max(0.35, scoreFillOpacity(weight)) : isDim ? 0.6 : 0;
  const marker = L.circle([lat, lng], {
    radius: scoreRadius,
    color: baseColor,
    fillColor: baseColor,
    fillOpacity: restFillOpacity,
    opacity: isDim ? 0.75 : isHero ? 0.95 : 0.9,
    weight: isDim ? 1 : isHero ? 2 : 2.5,
    bubblingMouseEvents: false,
  }).addTo(map);
  sizing.set(marker, { scoreRadius, trueRadius, weight, regionId, baseColor, restFillOpacity });
  state.markers.push(marker);
  if (isHero) {
    marker.bindTooltip(String(rank + 1), {
      permanent: true,
      direction: "center",
      className: "rank-numeral",
    });
  }
  return marker;
}

// Register a marker that plan.ts drew itself (start/destination/stop pins) into the same
// state.markers set plot() feeds, so clearMarkers() tears it down too. Keeps state.markers
// writable only from map.ts (issue #103).
export function addMarker(marker: L.CircleMarker): void {
  state.markers.push(marker);
}

// The focused destination drives loadCamps()/loadPreciseObservations() (layers.ts). map.ts owns
// it because clearMarkers() is what resets it to null (issue #103).
export function setFocused(lat: number, lng: number): void {
  state.focused = { lat, lng };
}

// Proxied and cached through our own API (sources/satellite.py, #293 follow-up) rather than the
// browser hitting Esri directly: a live export at full resolution takes 25-45s server-side, and
// `foray backfill-satellite` pre-fetches every known region so a selection is normally an
// instant cache hit instead of paying that render time in the browser. `regionId` addresses the
// same fixed grid cell the circle's true footprint (regionRadiusKm) already matches server-side.
const SATELLITE_ATTRIBUTION = "Imagery © Esri";

export function satelliteImageUrl(regionId: string): string {
  return `/api/destinations/${regionId}/satellite/image`;
}

export function satelliteLabelsUrl(regionId: string): string {
  return `/api/destinations/${regionId}/satellite/labels`;
}

let satelliteOverlay: L.ImageOverlay | null = null;
let satelliteLabelsOverlay: L.ImageOverlay | null = null;

// The aerial fill for the selected destination is opt-in now (issue #301): selecting a region
// no longer drops a satellite photo over it by default - the "Aerial" layer toggle does. When
// it's on and a region is already selected, flip the overlay straight on/off without needing a
// re-select. selectedRegionMarker is the circle selectSize() last grew to its true footprint.
let aerialEnabled = false;
let selectedRegionMarker: L.Circle | null = null;

export function setAerialEnabled(on: boolean): void {
  aerialEnabled = on;
  if (!map) return;
  const info = selectedRegionMarker && sizing.get(selectedRegionMarker);
  if (on && selectedRegionMarker && info) {
    showSatelliteOverlay(selectedRegionMarker, info.regionId);
  } else {
    clearSatelliteOverlay();
  }
}

// Fills the selected destination's true footprint with a satellite image plus its matching
// roads/labels overlay (so streets and city names stay readable, not just the bare photo),
// clipped to a circle in CSS (style.css's .sat-circle-overlay) rather than requested
// pre-clipped, so each is one plain rectangular image request. Both render in their own pane
// between the tiles and the vector overlay pane (see initMap) so the destination circle's
// ring/stroke and every other layer still draw on top - only the basemap underneath the
// selection is replaced, nothing else dims or hides. Fetched once - not re-requested on zoom
// (Leaflet re-scales the same raster onto `bounds` for free), so selecting a destination costs
// exactly one load, not a fresh reload/flash on every zoom step.
export function showSatelliteOverlay(marker: L.Circle, regionId: string): void {
  if (!map) return; // unit tests exercise selectSize()'s fill logic without a real map/initMap()
  clearSatelliteOverlay();
  const bounds = marker.getBounds();
  satelliteOverlay = L.imageOverlay(satelliteImageUrl(regionId), bounds, {
    className: "sat-circle-overlay",
    pane: "satellite",
    interactive: false,
  }).addTo(map);
  satelliteLabelsOverlay = L.imageOverlay(satelliteLabelsUrl(regionId), bounds, {
    className: "sat-circle-overlay",
    pane: "satellite",
    interactive: false,
  }).addTo(map);
  map.attributionControl.addAttribution(SATELLITE_ATTRIBUTION);
  decorateAttribution(map);
}

export function clearSatelliteOverlay(): void {
  // Checks both, not just satelliteOverlay - the two are always set/cleared together in normal
  // use, but gating on only one risks leaving the other (or the attribution) stale if that ever
  // stops being true (#293 Copilot review).
  if (!satelliteOverlay && !satelliteLabelsOverlay) return;
  satelliteOverlay = clearLayer(map, satelliteOverlay);
  satelliteLabelsOverlay = clearLayer(map, satelliteLabelsOverlay);
  map.attributionControl.removeAttribution(SATELLITE_ATTRIBUTION);
  decorateAttribution(map);
}

// Selecting a region (marker or card click) snaps its circle from the score-sized preview to
// its true real-world cell_deg footprint, computed from the same live config value as plot()
// (never hard-coded), so the user can see exactly how much ground that dot actually represents.
// Fill drops to fully transparent at this size - the circle's own vector fill draws in Leaflet's
// overlayPane, which sits *above* the "satellite" pane (see initMap/showSatelliteOverlay), so
// any nonzero fillOpacity here would tint the satellite imagery underneath with the circle's
// score hue instead of leaving it true-color. The satellite overlay (below, z-order-wise) fills
// the footprint with imagery; only the ring needs to stay drawn on top of it.
export function selectSize(marker: L.Circle): void {
  const info = sizing.get(marker);
  if (!info) return;
  marker.setRadius(info.trueRadius);
  marker.setStyle({ fillOpacity: 0, color: markerPalette().purple });
  setOthersFill(marker, true);
  selectedRegionMarker = marker;
  if (aerialEnabled) showSatelliteOverlay(marker, info.regionId);
}

// Reverts a previously selected marker back to its score-scaled preview size/opacity - called
// when a different region gets selected, so only one circle shows its true footprint at a time.
// The new selection's own selectSize() re-dims the rest; this just restores the one being
// dropped (and, when nothing new is selected, brings every circle's fill back). Size and fill
// both come from the marker's own `sizing` entry, so every restore path uses one source of
// truth (plot()'s weight), never a caller-passed value that could drift.
export function deselectSize(marker: L.Circle): void {
  const info = sizing.get(marker);
  if (!info) return;
  marker.setRadius(info.scoreRadius);
  marker.setStyle({ fillOpacity: info.restFillOpacity, color: info.baseColor });
  setOthersFill(marker, false);
  if (selectedRegionMarker === marker) selectedRegionMarker = null;
  clearSatelliteOverlay();
}

export function clearMarkers(): void {
  clearLayerList(map, state.markers);
  clearCamps();
  clearLand();
  clearFire();
  clearTrailheadMarkers();
  clearCardCampMarkers();
  clearSelectedTrail();
  clearPlanRoute();
  clearPrecise();
  clearSatelliteOverlay();
  selectedRegionMarker = null;
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
  clearLayerList(map, state.campMarkers);
}

export function addCampMarker(marker: L.CircleMarker): void {
  state.campMarkers.push(marker);
}

export function clearLand(): void {
  state.landLayer = clearLayer(map, state.landLayer);
}

export function setLandLayer(layer: L.GeoJSON): void {
  state.landLayer = layer;
}

export function clearFire(): void {
  state.fireLayer = clearLayer(map, state.fireLayer);
}

export function setFireLayer(layer: L.GeoJSON): void {
  state.fireLayer = layer;
}

// Signpost marker for a destination card's Trails tab trailhead list (views.ts) - only the
// currently open card's trailheads are on the map at once (plotTrailhead clears the previous
// set first), same "one destination's detail at a time" approach as camps/land. Clicking a
// marker selects that trailhead's real trail (layers.ts's selectTrailhead), same as clicking
// its matching list chip; setTrailheadActive keeps the two in visual sync. Drawn in the same
// TRAIL red as the selected trail line, with a dark keyline so it holds up on either basemap.
const TRAILHEAD_SVG = [
  `<svg viewBox="0 0 24 24" width="24" height="24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">`,
  `<path d="M12 3.5v18" stroke="${HOME_RING}" stroke-width="3.4" stroke-linecap="round"/>`,
  `<path d="M12 3.5v18" stroke="${TRAIL}" stroke-width="1.8" stroke-linecap="round"/>`,
  `<path d="M12 5.2h8l3 2.6-3 2.6h-8z" fill="${TRAIL}" stroke="${HOME_RING}" stroke-width="1.1" stroke-linejoin="round"/>`,
  `<path d="M12 12.4H5l-3 2.5 3 2.5h7z" fill="${TRAIL}" stroke="${HOME_RING}" stroke-width="1.1" stroke-linejoin="round"/>`,
  `</svg>`,
].join("");

function trailheadIcon(active: boolean): L.DivIcon {
  return L.divIcon({
    html: `<div class="trailhead-marker${active ? " active" : ""}">${TRAILHEAD_SVG}</div>`,
    className: "trailhead-icon",
    iconSize: [24, 24],
    iconAnchor: [12, 22],
  });
}

export function clearTrailheadMarkers(): void {
  clearLayerList(map, state.trailheadMarkers);
}

export function plotTrailhead(lat: number, lng: number, name: string, onSelect: () => void): L.Marker {
  // textContent, not a bare string: Leaflet renders a string tooltip as innerHTML and the name
  // is external OSM data (same guard as plotCardCamp).
  const tooltip = document.createElement("span");
  tooltip.textContent = name;
  const marker = L.marker([lat, lng], { icon: trailheadIcon(false), bubblingMouseEvents: false })
    .addTo(map)
    .bindTooltip(tooltip, { direction: "top", offset: [0, -22] });
  marker.on("click", onSelect);
  state.trailheadMarkers.push(marker);
  return marker;
}

export function setTrailheadActive(marker: L.Marker, active: boolean): void {
  marker.setIcon(trailheadIcon(active));
}

// Campground marker for a destination card's Campgrounds tab (views.ts) - same "one card's
// detail at a time" scoping as the Trails tab's trailhead markers, kept in a dedicated
// state.cardCampMarkers array rather than reusing state.campMarkers so this doesn't interact
// with the global #show-camps/#show-dispersed toggle's own marker set (loadCamps in layers.ts).
// Styled the same free/paid gold-vs-amber as that toggle's markers for visual consistency.
function cardCampStyle(site: CampSite, active: boolean): L.CircleMarkerOptions {
  return circleStyle({
    radius: active ? 8 : 6,
    fill: site.free === true ? CAMP_FREE : CAMP_PAID,
    stroke: HOME_RING,
    weight: active ? 2 : 1,
    fillOpacity: 0.9,
  });
}

export function clearCardCampMarkers(): void {
  clearLayerList(map, state.cardCampMarkers);
}

export function plotCardCamp(site: CampSite, onSelect: () => void): L.CircleMarker {
  const tooltip = document.createElement("span");
  tooltip.textContent = site.name;
  const marker = L.circleMarker([site.center_lat, site.center_lng], cardCampStyle(site, false))
    .addTo(map)
    .bindTooltip(tooltip, { direction: "top", offset: [0, -6] });
  marker.on("click", onSelect);
  state.cardCampMarkers.push(marker);
  return marker;
}

export function setCardCampActive(marker: L.CircleMarker, site: CampSite, active: boolean): void {
  marker.setStyle(cardCampStyle(site, active));
}

// Clears whichever trail is currently drawn from a destination card's Trails tab selection
// (layers.ts's selectTrailhead) - at most one at a time, see state.selectedTrailLayer.
export function clearSelectedTrail(): void {
  state.selectedTrailLayer = clearLayer(map, state.selectedTrailLayer);
}

export function setSelectedTrail(layer: L.Polyline): void {
  state.selectedTrailLayer = layer;
}

export function clearPlanRoute(): void {
  state.planRouteLayer = clearLayer(map, state.planRouteLayer);
}

export function setPlanRoute(layer: L.Polyline): void {
  state.planRouteLayer = layer;
}
