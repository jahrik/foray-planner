import L from "leaflet";

import type { CampSite } from "../api/types";
import { state } from "../state";
import { clearLayerList } from "./layer-lifecycle";
import { CAMP_FREE, CAMP_PAID, HOME_RING, map, TRAIL } from "./map";
import { circleStyle } from "./markers";

// Global camp-layer markers (#show-camps/#show-dispersed toggle, layers.ts's loadCamps) - kept
// separate from the per-card cardCampMarkers below so the two don't interact.
export function clearCamps(): void {
  clearLayerList(map, state.campMarkers);
}

export function addCampMarker(marker: L.CircleMarker): void {
  state.campMarkers.push(marker);
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
