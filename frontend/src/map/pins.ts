// Markers for the Details view's Trails and Campgrounds tabs (trailhead signposts and camp icons) that light
// up when their list row is selected.

import L from "leaflet";

import type { CampSite } from "../api/types";
import { state } from "../state";
import { clearLayerList } from "./layer-lifecycle";
import { map } from "./map";
import { campIcon, campLabel } from "./poi-layers";

// Trailhead marker for a destination card's Trails tab trailhead list (views.ts) - drawn over the
// always-on circle layer's identical markers (poi-layers.ts) so a selected row can light up. Only the
// currently open card's trailheads are on the map at once (plotTrailhead clears the previous
// set first), same "one destination's detail at a time" approach as camps/land. Clicking a
// marker selects that trailhead's real trail (layers.ts's selectTrailhead), same as clicking
// its matching list chip; setTrailheadActive keeps the two in visual sync. A signpost in ink on a
// TRAIL-red disc with the same ring as the camp and observation pins, so every pin on the map
// reads at the same visibility.
//
// The disc colour is set in CSS (`.trailhead-icon span`, mirroring map.ts TRAIL), not here, so this
// module needn't read map.ts bindings at eval time (map.ts and pins.ts import each other; a
// top-level read can hit the TDZ - Copilot review, PR #376).
export function trailheadSvg(): string {
  return [
    `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" fill="currentColor" stroke="currentColor">`,
    `<path d="M12 4v16" stroke-width="2" stroke-linecap="round" fill="none"/>`,
    `<path d="M12 5.5h7l2.5 2.3-2.5 2.3h-7z" stroke-width="0.8" stroke-linejoin="round"/>`,
    `<path d="M12 11.9H5l-2.5 2.3L5 16.5h7z" stroke-width="0.8" stroke-linejoin="round"/>`,
    `</svg>`,
  ].join("");
}

export function trailheadIcon(active: boolean): L.DivIcon {
  return L.divIcon({
    html: `<span>${trailheadSvg()}</span>`,
    className: `trailhead-icon${active ? " active" : ""}`,
    iconSize: L.point(26, 26),
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
    .bindTooltip(tooltip, { direction: "top", offset: [0, -14] });
  marker.on("click", onSelect);
  state.trailheadMarkers.push(marker);
  return marker;
}

export function setTrailheadActive(marker: L.Marker, active: boolean): void {
  marker.setIcon(trailheadIcon(active));
}

// Campground marker for a destination card's Campgrounds tab (views.ts) - same "one card's
// detail at a time" scoping as the Trails tab's trailhead markers, kept in a dedicated
// state.cardCampMarkers array. Drawn with the same camp-type icon as the always-on circle layer
// (poi-layers.ts) so it sits over its twin without a visible seam; `active` (its list chip is
// selected) is the only difference.
export function clearCardCampMarkers(): void {
  clearLayerList(map, state.cardCampMarkers);
}

export function plotCardCamp(site: CampSite, onSelect: () => void): L.Marker {
  const tooltip = document.createElement("span");
  tooltip.textContent = site.name;
  const marker = L.marker([site.center_lat, site.center_lng], {
    icon: campIcon(site),
    bubblingMouseEvents: false,
  })
    .addTo(map)
    .bindTooltip(tooltip, { direction: "top", offset: [0, -10] });
  marker.on("click", onSelect);
  // `addTo` has already fired `add`, so label the element now; the listener covers later re-adds.
  const label = (): void => marker.getElement()?.setAttribute("aria-label", campLabel(site));
  label();
  marker.on("add", label);
  state.cardCampMarkers.push(marker);
  return marker;
}

export function setCardCampActive(marker: L.Marker, site: CampSite, active: boolean): void {
  marker.setIcon(campIcon(site, active));
  marker.getElement()?.setAttribute("aria-label", campLabel(site)); // setIcon builds a new element
}
