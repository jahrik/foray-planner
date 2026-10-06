import L from "leaflet";

import type { CampSite } from "../api/types";
import { state } from "../state";
import { clearLayerList } from "./layer-lifecycle";
import { HOME_RING, map, TRAIL } from "./map";
import { campIcon, campLabel } from "./poi-layers";

// Signpost marker for a destination card's Trails tab trailhead list (views.ts) - drawn over the
// always-on circle layer's identical signposts (poi-layers.ts) so a selected row can light up. Only the
// currently open card's trailheads are on the map at once (plotTrailhead clears the previous
// set first), same "one destination's detail at a time" approach as camps/land. Clicking a
// marker selects that trailhead's real trail (layers.ts's selectTrailhead), same as clicking
// its matching list chip; setTrailheadActive keeps the two in visual sync. Drawn in the same
// TRAIL red as the selected trail line, with a dark keyline so it holds up on either basemap.
//
// Built lazily inside trailheadIcon(), not as a module-level const: map.ts and pins.ts import
// each other (map.ts needs pins.ts's clear* functions; pins.ts needs map.ts's HOME_RING/TRAIL),
// and a top-level template literal reading those bindings at pins.ts's own module-eval time can
// run before map.ts's `const HOME_RING = ...` has executed, hitting the TDZ and throwing
// "Cannot access 'HOME_RING' before initialization" on load (Copilot review, PR #376).
export function trailheadSvg(): string {
  return [
    `<svg viewBox="0 0 24 24" width="24" height="24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">`,
    `<path d="M12 3.5v18" stroke="${HOME_RING}" stroke-width="3.4" stroke-linecap="round"/>`,
    `<path d="M12 3.5v18" stroke="${TRAIL}" stroke-width="1.8" stroke-linecap="round"/>`,
    `<path d="M12 5.2h8l3 2.6-3 2.6h-8z" fill="${TRAIL}" stroke="${HOME_RING}" stroke-width="1.1" stroke-linejoin="round"/>`,
    `<path d="M12 12.4H5l-3 2.5 3 2.5h7z" fill="${TRAIL}" stroke="${HOME_RING}" stroke-width="1.1" stroke-linejoin="round"/>`,
    `</svg>`,
  ].join("");
}

export function trailheadIcon(active: boolean): L.DivIcon {
  return L.divIcon({
    html: `<div class="trailhead-marker${active ? " active" : ""}">${trailheadSvg()}</div>`,
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
