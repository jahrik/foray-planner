// The campground and trailhead markers drawn inside the selected destination's circle (issue
// #451). Both layers are always on for a selection: loadCamps / loadCircleTrailheads (layers.ts)
// fill them, the Layers pill's checkboxes only hide them. A circle can hold a hundred of each, so
// each layer is a marker-cluster group that folds into a count badge at low zoom and spreads into
// individual icons from `POI_UNCLUSTER_ZOOM` up.
//
// A camp marker is its camp-type icon (icons/camp-icons.ts) in ink on a disc whose colour says
// free / paid / OSM-reported - the same three colours the legend always used. A trailhead is the
// signpost the Details > Trails tab plots (pins.ts), so the two read as one thing where they
// overlap. Both sit in their own pane under the precise-observation pins, which stay on top.

import L from "leaflet";

import type { CampSite } from "../api/types";
import { CAMP_ICON_LABELS, campIconKey, campIconSvg, type CampIconKey } from "../icons/camp-icons";
import { CAMP_FREE, CAMP_OSM, CAMP_PAID } from "./map";
import { trailheadIcon } from "./pins";

const POI_PANE = "pois";
// Between the vector overlay pane (400) and the marker pane (600), where the precise pins live.
const POI_PANE_Z_INDEX = 590;
/** Zoom at which clustering stops and every icon is drawn on its own (the circle is ~9 at fit). */
export const POI_UNCLUSTER_ZOOM = 13;

let campCluster: L.MarkerClusterGroup | null = null;
let trailheadCluster: L.MarkerClusterGroup | null = null;

// What is on the map now, for the legend: which camp icons, and the three disc colours.
let loadedCampKeys = new Set<CampIconKey>();
let loadedCampColors = new Set<string>();
let trailheadCount = 0;

/** The disc colour for a campsite: OSM-reported sites teal, otherwise free gold / paid amber. */
export function campFill(site: Pick<CampSite, "kind" | "free">): string {
  if (site.kind === "reported") return CAMP_OSM;
  return site.free === true ? CAMP_FREE : CAMP_PAID;
}

/** A camp's accessible name: its name and what kind of camp the source says it is. */
export function campLabel(site: Pick<CampSite, "name" | "camp_type" | "pitch_count">): string {
  const parts = [site.name, CAMP_ICON_LABELS[campIconKey(site.camp_type)]];
  if (site.pitch_count > 1) parts.push(`${site.pitch_count} pitches`);
  return parts.join(", ");
}

/** The marker icon for a campsite: type icon on a free / paid / reported coloured disc, with a
 * count chip when the row stands for several OSM pitches. `active` is the Campgrounds tab's
 * selected row. */
export function campIcon(site: CampSite, active = false): L.DivIcon {
  const chip = site.pitch_count > 1 ? `<b>${site.pitch_count}</b>` : "";
  return L.divIcon({
    className: `camp-pin-icon${active ? " active" : ""}`,
    html: `<span style="background:${campFill(site)}">${campIconSvg(site.camp_type)}${chip}</span>`,
    iconSize: L.point(26, 26),
  });
}

/** A campsite marker (not yet on a map). Popups and tooltips are bound by the caller. */
export function createCampMarker(site: CampSite): L.Marker {
  const marker = L.marker([site.center_lat, site.center_lng], {
    icon: campIcon(site),
    pane: POI_PANE,
    bubblingMouseEvents: false,
  });
  // On every add: the cluster group re-creates the element as the marker leaves and re-enters a
  // cluster. Not `title` - its native tooltip would sit on top of the popup.
  marker.on("add", () => marker.getElement()?.setAttribute("aria-label", campLabel(site)));
  return marker;
}

function clusterIcon(kind: "camp" | "trailhead"): (cluster: L.MarkerCluster) => L.DivIcon {
  return (cluster) => {
    const count = cluster.getChildCount();
    const size = count < 10 ? 30 : count < 100 ? 36 : 42;
    const label = kind === "camp" ? "campgrounds" : "trailheads";
    return L.divIcon({
      html: `<div class="poi-cluster-badge ${kind}" style="width:${size}px;height:${size}px" aria-label="${count} ${label}"><b>${count}</b></div>`,
      className: "poi-cluster-icon",
      iconSize: L.point(size, size),
    });
  };
}

function createCluster(leafletMap: L.Map, kind: "camp" | "trailhead"): L.MarkerClusterGroup {
  return L.markerClusterGroup({
    iconCreateFunction: clusterIcon(kind),
    maxClusterRadius: 44,
    disableClusteringAtZoom: POI_UNCLUSTER_ZOOM,
    showCoverageOnHover: false,
    spiderfyOnMaxZoom: false,
    clusterPane: POI_PANE,
  }).addTo(leafletMap);
}

/** Create the pane and the two cluster groups. Called once from initMap. */
export function initPoiLayers(leafletMap: L.Map): void {
  leafletMap.createPane(POI_PANE).style.zIndex = String(POI_PANE_Z_INDEX);
  campCluster = createCluster(leafletMap, "camp");
  trailheadCluster = createCluster(leafletMap, "trailhead");
}

export function clearCampMarkers(): void {
  campCluster?.clearLayers();
  loadedCampKeys = new Set();
  loadedCampColors = new Set();
}

export function addCampMarkers(entries: { site: CampSite; marker: L.Marker }[]): void {
  campCluster?.addLayers(entries.map((entry) => entry.marker));
  for (const { site } of entries) {
    loadedCampKeys.add(campIconKey(site.camp_type));
    loadedCampColors.add(campFill(site));
  }
}

export function clearCircleTrailheads(): void {
  trailheadCluster?.clearLayers();
  trailheadCount = 0;
}

/** A trailhead marker for the circle layer: the Trails tab's signpost, in the POI pane. */
export function createTrailheadMarker(
  lat: number,
  lng: number,
  name: string,
  onSelect: () => void,
): L.Marker {
  // textContent, not a bare string: Leaflet renders a string tooltip as innerHTML and the name is
  // external OSM data.
  const tooltip = document.createElement("span");
  tooltip.textContent = name;
  const marker = L.marker([lat, lng], {
    icon: trailheadIcon(false),
    pane: POI_PANE,
    bubblingMouseEvents: false,
  }).bindTooltip(tooltip, { direction: "top", offset: [0, -22] });
  marker.on("click", onSelect);
  marker.on("add", () => marker.getElement()?.setAttribute("aria-label", `Trailhead: ${name}`));
  return marker;
}

export function addCircleTrailheads(markers: L.Marker[]): void {
  trailheadCluster?.addLayers(markers);
  trailheadCount += markers.length;
}

/** Legend rows for what the POI layers hold right now. `swatch` is the disc colour for a plain
 * dot entry, `iconKey` for an icon entry, `trailhead` for the signpost. */
export type PoiLegendEntry =
  | { label: string; swatch: string }
  | { label: string; iconKey: CampIconKey }
  | { label: string; trailhead: true };

export function poiLegendEntries(): PoiLegendEntry[] {
  const entries: PoiLegendEntry[] = [];
  const colors: [string, string][] = [
    [CAMP_FREE, "Free campground"],
    [CAMP_PAID, "Paid / unknown campground"],
    [CAMP_OSM, "Reported campsite (OSM)"],
  ];
  for (const [color, label] of colors) {
    if (loadedCampColors.has(color)) entries.push({ swatch: color, label });
  }
  for (const key of [
    "tent",
    "rv",
    "mixed",
    "backcountry",
    "group",
    "equestrian",
    "cabin",
    "generic",
  ] as const) {
    if (loadedCampKeys.has(key)) entries.push({ iconKey: key, label: CAMP_ICON_LABELS[key] });
  }
  if (trailheadCount > 0) entries.push({ trailhead: true, label: "Trailhead" });
  return entries;
}
