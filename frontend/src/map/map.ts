import L from "leaflet";
import "leaflet.markercluster";

import type { CampSite, Home } from "../api/types";
import { clearLayer, clearLayerList } from "./layer-lifecycle";
import { circleStyle } from "./markers";
import { dist, qs, state } from "../state";

// Marker palette - bright/neon so it pops on the dark basemap (the default), while still
// reading over the lighter OSM terrain in light mode. Deliberately non-green vs the terrain.
export const HEAT = "#ff2d9b"; // hot magenta - historical strength (destinations)
export const HEAT_RGB = "255,45,155";
export const LIVE = "#22e0ff"; // electric cyan - fresh / recently observed
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
export const PRECISE = "#c792ea"; // bright lavender - known-precise (non-obscured) observation pin
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

// A single standard OSM tile source for both themes - dark mode inverts it via CSS
// (`invert() hue-rotate()` in style.css) instead of swapping in a separate dark tileset.
// The CARTO dark_all raster this used to load renders minor labels (peaks, lakes, wilderness
// boundaries) in very low-contrast gray by design, and no CSS brightness/contrast filter could
// fix that without also crushing the rest of the tile. Inverting OSM's normal high-contrast
// dark-on-light labels turns them into equally high-contrast light-on-dark, so everything from
// city names down to trail/forest labels stays legible.
const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION =
  "© OpenStreetMap · observations © iNaturalist · elevation &amp; weather © Open-Meteo";
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

export function setTiles(): void {
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
  setTiles();
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
  { scoreRadius: number; trueRadius: number; weight: number; regionId: string }
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
    marker.setStyle({ fillOpacity: ringOnly ? 0 : scoreFillOpacity(info.weight) });
  }
}

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
export function plot(lat: number, lng: number, weight: number, live: boolean, regionId: string): L.Circle {
  const trueRadius = ((state.cellDeg * KM_PER_DEG) / 2) * 1000;
  const scoreRadius = trueRadius * (0.3 + weight);
  const marker = L.circle([lat, lng], {
    radius: scoreRadius,
    color: live ? LIVE : HEAT,
    fillColor: live ? LIVE : HEAT,
    fillOpacity: scoreFillOpacity(weight),
    opacity: 0.4 + 0.5 * weight,
    weight: 1.5,
    bubblingMouseEvents: false,
  }).addTo(map);
  sizing.set(marker, { scoreRadius, trueRadius, weight, regionId });
  state.markers.push(marker);
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
}

export function clearSatelliteOverlay(): void {
  // Checks both, not just satelliteOverlay - the two are always set/cleared together in normal
  // use, but gating on only one risks leaving the other (or the attribution) stale if that ever
  // stops being true (#293 Copilot review).
  if (!satelliteOverlay && !satelliteLabelsOverlay) return;
  satelliteOverlay = clearLayer(map, satelliteOverlay);
  satelliteLabelsOverlay = clearLayer(map, satelliteLabelsOverlay);
  map.attributionControl.removeAttribution(SATELLITE_ATTRIBUTION);
}

// Selecting a region (marker or card click) snaps its circle from the score-sized preview to
// its true real-world cell_deg footprint, computed from the same live config value as plot()
// (never hard-coded), so the user can see exactly how much ground that dot actually represents.
// Fill goes very transparent at this size so the map underneath - which the circle now likely
// covers a large part of - stays readable. The satellite overlay (above) fills that same
// footprint with imagery so "the map underneath" is actually worth looking at.
export function selectSize(marker: L.Circle): void {
  const info = sizing.get(marker);
  if (!info) return;
  marker.setRadius(info.trueRadius);
  marker.setStyle({ fillOpacity: 0.08 });
  setOthersFill(marker, true);
  showSatelliteOverlay(marker, info.regionId);
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
  marker.setStyle({ fillOpacity: scoreFillOpacity(info.weight) });
  setOthersFill(marker, false);
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

// Hiking-boot marker for a destination card's Trails tab trailhead list (views.ts) - only the
// currently open card's trailheads are on the map at once (plotTrailhead clears the previous
// set first), same "one destination's detail at a time" approach as camps/land. Clicking a
// marker selects that trailhead's real trail (layers.ts's selectTrailhead), same as clicking
// its matching list chip; setTrailheadActive keeps the two in visual sync.
function trailheadIcon(active: boolean): L.DivIcon {
  return L.divIcon({
    html: `<div class="trailhead-marker${active ? " active" : ""}">🥾</div>`,
    className: "trailhead-icon",
    iconSize: [22, 22],
    iconAnchor: [11, 20],
  });
}

export function clearTrailheadMarkers(): void {
  clearLayerList(map, state.trailheadMarkers);
}

export function plotTrailhead(lat: number, lng: number, name: string, onSelect: () => void): L.Marker {
  const marker = L.marker([lat, lng], { icon: trailheadIcon(false), bubblingMouseEvents: false })
    .addTo(map)
    .bindTooltip(name, { direction: "top", offset: [0, -18] });
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
