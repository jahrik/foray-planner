import L from "leaflet";
import "leaflet.markercluster";

import type { Home, PreciseObservation } from "../api/types";
import { FIRE_ACTIVE, FIRE_SCAR } from "./basemap-fire";
import { LAND_COLORS, LAND_DEFAULT } from "./basemap-land";
import { FORAGE_RAMP, FORAGE_TIER_LABELS } from "./forage";
import { clearCardCampMarkers, clearCamps, clearTrailheadMarkers } from "./pins";
import { clearLayer, clearLayerList } from "./layer-lifecycle";
import { clearSatelliteOverlay, resetSelection } from "./destinations";
import { inspectRoadAt } from "./inspect";
import { buildClusterList } from "./cluster-popup";
import { type ClusterVote, voteGenus } from "./cluster-vote";
import { genusIconSvg } from "../icons/genus-icons";
import { escapeHtml } from "../format";
import { createHoverPopup, type HoverPopup } from "./hover-popup";
import { circleStyle } from "./markers";
import { accuracyLabel, dist, onScopeChange, qs, state } from "../state";

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
// Each credit follows its provider's own wording (docs/data-sources.md, "Licences and attribution"):
// Open-Meteo requires a link reading "Weather data by Open-Meteo.com"; elevation is Copernicus DEM
// (served via Open-Meteo and from the AWS mirror).
const DATA_ATTRIBUTION =
  'observations © <a href="https://www.inaturalist.org">iNaturalist</a> contributors · ' +
  '<a href="https://open-meteo.com/">Weather data by Open-Meteo.com</a> · elevation: Copernicus DEM';
const VECTOR_ATTRIBUTION = `© OpenStreetMap · © Protomaps · ${DATA_ATTRIBUTION}`;
// Shown only when a terrain layer is active (hillshade/contours from the DEM tiles).
const TERRAIN_ATTRIBUTION =
  'terrain <a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">Tilezen (USGS 3DEP and others)</a>';
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
// Each precise pin's source observation and popup card, so a cluster's observation list
// (wirePrecisePopup) can read back what's folded into it from getAllChildMarkers(), and a single
// pin's hover can build its card.
const preciseObservations = new WeakMap<L.Layer, { obs: PreciseObservation; content: () => HTMLElement }>();

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
    spore: cssVar("--spore", "#c9961a"),
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
// Sentinel for the legend's precise-observation entry, drawn as a mini genus pin, not a colour.
const PRECISE_SWATCH = "precise-pin";

export function renderLegend(): void {
  const el = qs("#legend");
  const camps = (document.getElementById("show-camps") as HTMLInputElement | null)?.checked;
  const dispersed = (document.getElementById("show-dispersed") as HTMLInputElement | null)?.checked;
  const blm = (document.getElementById("show-land-blm") as HTMLInputElement | null)?.checked;
  const usfs = (document.getElementById("show-land-usfs") as HTMLInputElement | null)?.checked;
  const tribal = (document.getElementById("show-land-tribal") as HTMLInputElement | null)?.checked;
  const otherLand = (document.getElementById("show-land-other") as HTMLInputElement | null)?.checked;
  const palette = markerPalette();
  const entries: [string, string][] = [
    [palette.rust, "Top destination"],
    [palette.flush, "Seen in the last few weeks"],
    [palette.moss, "Other region in range"],
    [palette.purple, "Selected"],
    [PRECISE_SWATCH, "Precise observation: icon shows its genus or shape"],
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
  if (otherLand) entries.push([LAND_DEFAULT, "State & other federal land"]);
  if (state.selectedTrailWalkIn) entries.push([TRAIL_WALKIN, "Walk-in forest road (gated)"]);
  if (state.selectedTrailForage > 0) {
    const idx = state.selectedTrailForage - 1; // 0..2 into the tier-1/2/3 ramp
    entries.push([FORAGE_RAMP[idx] ?? FORAGE_RAMP[2], `Selected trail: ${FORAGE_TIER_LABELS[idx] ?? ""}`]);
  }
  el.innerHTML = entries
    .map(([color, label]) =>
      color === PRECISE_SWATCH
        ? `<span class="legend-item"><i class="legend-pin" aria-hidden="true">${genusIconSvg("gilled")}</i>${label}</span>`
        : `<span class="legend-item"><i style="background:${color}"></i>${label}</span>`,
    )
    .join("");
}

// Cluster badge (issue #449): the icon of the cluster's most common genus (cluster-vote.ts) on the
// ochre --spore disc with an ink ring, and the pin count in a corner chip (style.css
// .precise-cluster-badge). Child clusters re-vote as they split on zoom. The vote is cached per
// cluster and only redone when its child count changes, since iconCreateFunction runs for every
// cluster on every re-cluster and a destination can hold 2,000+ pins.
const clusterVotes = new WeakMap<L.MarkerCluster, { childCount: number; vote: ClusterVote | null }>();
// Pins per genus across everything loaded: the vote's tie-break (rebuilt by addPreciseMarker /
// clearPrecise).
const loadedGenusCounts = new Map<number, number>();

function clusterVote(cluster: L.MarkerCluster): ClusterVote | null {
  const childCount = cluster.getChildCount();
  const cached = clusterVotes.get(cluster);
  if (cached && cached.childCount === childCount) return cached.vote;
  const vote = voteGenus(clusterObservations(cluster), loadedGenusCounts);
  clusterVotes.set(cluster, { childCount, vote });
  return vote;
}

function preciseClusterIcon(cluster: L.MarkerCluster): L.DivIcon {
  const count = cluster.getChildCount();
  const size = count < 10 ? 32 : count < 100 ? 38 : 44;
  const vote = clusterVote(cluster);
  const label = vote ? `${count} observations, most ${vote.name}` : `${count} observations`;
  return L.divIcon({
    html: `<div class="precise-cluster-badge" style="width:${size}px;height:${size}px" aria-label="${escapeHtml(label)}">${genusIconSvg(vote?.icon)}<b>${count}</b></div>`,
    className: "precise-cluster-icon",
    iconSize: L.point(size, size),
  });
}

// A single precise pin (issues #447, #449): the genus icon in ink on a 22 px --spore disc with an
// ink ring (style.css .precise-pin-icon), centred in a 24 px hit box so the hover popup is easy
// to trigger. A divIcon rather than a circleMarker so the pin is a focusable element for the
// keyboard path. One DivIcon per icon key, shared by every pin of that genus or shape.
const pinIcons = new Map<string, L.DivIcon>();

export function precisePinIcon(icon: PreciseObservation["icon"]): L.DivIcon {
  let pin = pinIcons.get(icon);
  if (!pin) {
    pin = L.divIcon({
      className: "precise-pin-icon",
      html: `<span>${genusIconSvg(icon)}</span>`,
      iconSize: L.point(24, 24),
    });
    pinIcons.set(icon, pin);
  }
  return pin;
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
  const hover = hoverCapable();
  preciseCluster = L.markerClusterGroup({
    iconCreateFunction: preciseClusterIcon,
    maxClusterRadius: 40,
    spiderfyOnMaxZoom: true,
    // Touch: a tap opens the observation list (which has its own Zoom in button) - see
    // wirePrecisePopup.
    zoomToBoundsOnClick: hover,
  }).addTo(map);
  wirePrecisePopup(preciseCluster, hover);
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

/** Show `home` on the location line + map. `recenter: false` moves the home marker without
 * re-centring the view (a background GPS refinement shouldn't undo the user's pan/zoom).
 * `accuracyM` is the device-fix uncertainty; left undefined, it's kept when the coordinates are
 * unchanged (a radius change) and cleared otherwise (a searched / clicked location). */
export function updateHome(
  home: Home,
  options: { recenter?: boolean; accuracyM?: number | null } = {},
): void {
  const { recenter = true } = options;
  const moved = !state.home || state.home.lat !== home.lat || state.home.lng !== home.lng;
  if (options.accuracyM !== undefined) state.homeAccuracyM = options.accuracyM;
  else if (moved) state.homeAccuracyM = null;
  state.home = home;
  renderHomeLine();
  if (homeMarker) {
    homeMarker.setLatLng([home.lat, home.lng]).bindPopup("Location: " + home.name);
    if (recenter) map.setView([home.lat, home.lng], 8);
  }
  onScopeChange();
}

/** The search bar's "name (lat, lng ±acc) · radius" line - also re-run on a units change. */
export function renderHomeLine(): void {
  const home = state.home;
  if (!home) return;
  qs("#home-name").textContent = home.name;
  const accuracy = state.homeAccuracyM === null ? "" : ` ${accuracyLabel(state.homeAccuracyM)}`;
  qs("#home-coords").textContent = `${home.lat.toFixed(4)}, ${home.lng.toFixed(4)}${accuracy}`;
  qs("#home-radius").textContent = dist(home.radius_km);
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
  pendingPrecise.length = 0;
  closePrecisePopup();
  preciseCluster.clearLayers();
  loadedGenusCounts.clear();
}

// Adds a precise-observation pin into the cluster group (see preciseCluster above) instead of
// directly onto the map - the cluster group itself decides whether it renders standalone or
// folded into a nearby cluster badge at the current zoom.
export function addPreciseMarker(
  marker: L.Marker,
  obs: PreciseObservation,
  content: () => HTMLElement,
): void {
  preciseObservations.set(marker, { obs, content });
  loadedGenusCounts.set(obs.taxon_id, (loadedGenusCounts.get(obs.taxon_id) ?? 0) + 1);
  pendingPrecise.push(marker);
}

// Pins are added in one `addLayers` after every `addPreciseMarker`, so the genus counts that break
// vote ties are final before any cluster icon is built, and clustering runs once instead of
// re-voting a growing cluster per pin.
const pendingPrecise: L.Marker[] = [];
export function commitPreciseMarkers(): void {
  preciseCluster.addLayers(pendingPrecise.splice(0));
}

// The precise-observation popup (hover-popup.ts): a cluster badge lists the observations folded
// into it (cluster-popup.ts), a single pin shows its own observation card. With a mouse, hovering
// either opens it and clicking a badge still zooms to its bounds (the plugin default); the popup
// stays open while the pointer moves onto it and closes a beat after the pointer leaves both.
// Touch screens have no hover, so a tap opens it instead - on a badge that replaces the zoom, and
// the list carries a "Zoom in" button for the drill-down (see initMap's zoomToBoundsOnClick).
// Keyboard: focusing a badge or pin opens it, Tab moves into it, Escape closes it back to the
// marker (Enter on a badge still zooms). It closes on any zoom since the clusters re-form, and on
// clearPrecise() since its pins are about to be replaced.
let precisePopup: HoverPopup | null = null;

function closePrecisePopup(): void {
  precisePopup?.close();
}

const hoverCapable = (): boolean => window.matchMedia?.("(hover: hover)").matches ?? true;

function clusterObservations(cluster: L.MarkerCluster): PreciseObservation[] {
  return cluster
    .getAllChildMarkers()
    .map((marker) => preciseObservations.get(marker)?.obs)
    .filter((obs): obs is PreciseObservation => obs !== undefined);
}

// The cluster badge or single pin whose icon is `element`. Cluster icons are rebuilt on every
// re-cluster, so there's no stable element -> cluster handle to keep; the public getVisibleParent
// finds the badge a pin is folded into.
function preciseMarkerFor(group: L.MarkerClusterGroup, element: Element): L.Marker | null {
  for (const layer of group.getLayers()) {
    const marker = layer as L.Marker;
    if (marker.getElement() === element) return marker;
    const parent = group.getVisibleParent(marker) as L.Marker | null;
    if (parent && parent !== marker && parent.getElement() === element) return parent;
  }
  return null;
}

function wirePrecisePopup(group: L.MarkerClusterGroup, hover: boolean): void {
  const popup = L.popup({
    className: "cluster-popup",
    closeButton: !hover,
    autoPan: !hover,
    maxWidth: 320,
    offset: L.point(0, -12),
  });
  const popupView = createHoverPopup(map, popup);
  precisePopup = popupView;
  // Touch on a phone layout (the same query as sheet.ts): pan the map so the card clears the
  // floating search bar and filter pills (about 190 px tall) instead of opening underneath them.
  const clearControls = (): void => {
    popup.options.autoPanPaddingTopLeft = window.matchMedia?.("(max-width: 780px)").matches
      ? L.point(10, 190)
      : L.point(5, 5);
  };
  const openCluster = (cluster: L.MarkerCluster): void => {
    const observations = clusterObservations(cluster);
    if (!observations.length) return;
    const options = hover ? {} : { onZoom: () => cluster.zoomToBounds({ padding: [20, 20] }) };
    clearControls();
    popupView.open({
      latLng: cluster.getLatLng(),
      anchor: (cluster as unknown as L.Marker).getElement() ?? null,
      content: buildClusterList(observations, options),
    });
  };
  const openPin = (marker: L.Marker): void => {
    const entry = preciseObservations.get(marker);
    if (!entry) return;
    clearControls();
    popupView.open({
      latLng: marker.getLatLng(),
      anchor: marker.getElement() ?? null,
      content: entry.content(),
    });
  };
  const open = (marker: L.Marker): void => {
    if (marker instanceof L.MarkerCluster) openCluster(marker);
    else openPin(marker);
  };

  if (hover) {
    group.on("clustermouseover", (event: L.LeafletEvent) => openCluster(event.layer as L.MarkerCluster));
    group.on("clustermouseout mouseout", popupView.scheduleClose);
    group.on("mouseover", (event: L.LeafletEvent) => openPin(event.layer as L.Marker));
  } else {
    group.on("clusterclick", (event: L.LeafletEvent) => openCluster(event.layer as L.MarkerCluster));
  }
  // A click on a pin opens its card on touch, and keeps it open on a mouse (it's already showing).
  group.on("click", (event: L.LeafletEvent) => openPin(event.layer as L.Marker));

  // Keyboard path, delegated on the map container since badges are recreated on re-cluster.
  const container = map.getContainer();
  const iconOf = (target: EventTarget | null): HTMLElement | null =>
    target instanceof HTMLElement &&
    (target.classList.contains("precise-cluster-icon") || target.classList.contains("precise-pin-icon"))
      ? target
      : null;
  L.DomEvent.on(container, "focusin", (event) => {
    const focused = popupView.returningFocus() ? null : iconOf(event.target);
    const marker = focused && preciseMarkerFor(group, focused);
    if (marker) open(marker);
  });
  L.DomEvent.on(container, "focusout", (event) => {
    if (iconOf(event.target) && !popupView.contains((event as FocusEvent).relatedTarget))
      popupView.scheduleClose();
  });
  L.DomEvent.on(container, "keydown", (event) => {
    const keyEvent = event as KeyboardEvent;
    const icon = iconOf(keyEvent.target);
    if (keyEvent.key !== "Tab" || keyEvent.shiftKey || !icon || popupView.anchor() !== icon) return;
    const first = map.getContainer().querySelector<HTMLElement>(".cluster-popup a, .cluster-popup button");
    if (!first) return;
    keyEvent.preventDefault();
    first.focus();
  });

  map.on("zoomstart", closePrecisePopup);
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
const SATELLITE_ATTRIBUTION = "Imagery: Esri, Vantor, Earthstar Geographics, and the GIS User Community";

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
