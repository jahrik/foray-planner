import L from "leaflet";

import { state } from "../state";
import { clearLayer } from "./layer-lifecycle";
import { decorateAttribution, map, markerPalette } from "./map";

// Marker hierarchy (issue #301): the map used to draw ~200 near-identical circles. Now rank
// drives three tiers so the shortlist reads at a glance -
//   top 3   (rank 0-2)  : score-scaled circle with a translucent fill + a permanent rank numeral
//   next 7  (rank 3-9)   : ring only (no fill), fixed radius - a marker, not a region wash
//   the rest (rank 10+)  : a small dim moss dot, fixed size, no score scaling
const HERO_RANK_MAX = 2;
const PROMINENT_RANK_MAX = 9;
const DIM_DOT_RADIUS_M = 900; // fixed ground radius for the rank-11+ dots

// Same footprint plot() uses for a region's true (not score-scaled) circle - see selectSize.
// Exported so layers.ts can scope the precise-observations fetch to exactly the ground a
// selected destination bubble represents, instead of the whole search radius (issue #161
// follow-up: a radius-wide fetch put a cluster badge on every destination on the map at once,
// visually burying the destination bubbles they were competing with).
//
// issue #337: the server now sends this radius precomputed (an H3 cell's real-world size is
// the same everywhere, unlike the old cell_deg degree grid, which needed a 111 km/deg
// conversion done here that also silently assumed the equator - no distortion math belongs in
// the frontend at all any more).
export const regionRadiusKm = (): number => state.regionRadiusKm;

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
// fixed pixel radius) so selecting a region can snap it to its true H3-cell footprint
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
  const trueRadius = state.regionRadiusKm * 1000;
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
// its true real-world H3-cell footprint, computed from the same live config value as plot()
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

// Torn down by clearMarkers() (map.ts) - resets selection state that clearing the circle
// layers themselves doesn't touch.
export function resetSelection(): void {
  selectedRegionMarker = null;
}
