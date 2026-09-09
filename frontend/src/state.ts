import type L from "leaflet";

import type { ApiError, Home, TripPlan } from "./api/types";
import { getMonths, getUnits } from "./prefs";

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const CURRENT_MONTH = new Date().getMonth() + 1; // 1-12

export type View = "destinations" | "plan";

export type Units = "km" | "mi";

// Destinations sort (issue #301): "best" is the scored ranking (default); "active" floats
// regions with recent observations; "nearest" is straight distance. Same cards, same map -
// just the order, applied client-side to the fetched payload.
export type Sort = "best" | "active" | "nearest";

// One flat `state` object, but the shape is grouped by concern so the three kinds of field
// stay legible: live Leaflet layer handles (map.ts owns their lifecycle), the user's scoping
// inputs, and view/display preferences. Kept flat rather than nested so every call site stays
// `state.foo` (issue #301 - the split is documentation, not a 20-file rename).

/** Leaflet layer/marker handles - written only from map.ts (issue #103). */
interface MapState {
  markers: L.CircleMarker[];
  campMarkers: L.CircleMarker[];
  landLayer: L.GeoJSON | null;
  fireLayer: L.GeoJSON | null;
  trailheadMarkers: L.Marker[];
  cardCampMarkers: L.CircleMarker[];
  selectedTrailLayer: L.Polyline | null;
  planRouteLayer: L.Polyline | null;
  focused: { lat: number; lng: number } | null;
}

/** What the user is asking about: location, months, and (server-supplied) grid resolution. */
interface ScopeState {
  home: Home | null;
  months: Set<number>;
  cellDeg: number;
  // URL of the Protomaps PMTiles vector basemap, from /api/config. Empty -> the map has no
  // base layer (there is no raster fallback). Set once on load, before initMap.
  basemapUrl: string;
  // Terrarium DEM tile URL template ({z}/{x}/{y}), from /api/config - drives the hillshade +
  // contours. Empty -> no terrain layer. Set once on load, before initMap.
  terrainUrl: string;
}

/** How results are shown: which view, sort order, unit system, and the last plan payload. */
interface UiState {
  view: View;
  sort: Sort;
  units: Units;
  planTrip: TripPlan | null;
}

export type State = MapState & ScopeState & UiState;

export const state: State = {
  months: new Set(getMonths() ?? [CURRENT_MONTH]),
  view: "destinations",
  sort: "best",
  home: null,
  markers: [],
  campMarkers: [],
  landLayer: null,
  fireLayer: null,
  trailheadMarkers: [],
  cardCampMarkers: [],
  selectedTrailLayer: null,
  planRouteLayer: null,
  planTrip: null,
  focused: null,
  cellDeg: 0.25, // overwritten from /api/config once it loads; matches the backend default
  basemapUrl: "", // overwritten from /api/config; empty means no base layer
  terrainUrl: "", // overwritten from /api/config; empty means no hillshade/contours
  units: getUnits(),
};

const KM_TO_MI = 0.621371;

export function dist(km: number): string {
  if (state.units === "mi") return `${Math.round(km * KM_TO_MI)} mi`;
  return `${Math.round(km)} km`;
}

const M_TO_FT = 3.28084;

/** Ground elevation label (issue #36), in the same unit system as distances: feet when the
 * user is on miles, metres when on kilometres. Input is always metres (Open-Meteo's unit). */
export function elevationLabel(metres: number): string {
  if (state.units === "mi") return `${Math.round(metres * M_TO_FT).toLocaleString()} ft`;
  return `${Math.round(metres).toLocaleString()} m`;
}

const MM_TO_IN = 0.0393701;

/** Rainfall label (issue #226): inches when the user is on miles, millimetres on kilometres.
 * Input is always millimetres (Open-Meteo's unit). Data is informational only (Open-Meteo). */
export function rainLabel(mm: number): string {
  if (state.units === "mi") return `${(mm * MM_TO_IN).toFixed(mm * MM_TO_IN < 1 ? 2 : 1)} in`;
  return `${mm < 10 ? mm.toFixed(1) : Math.round(mm).toString()} mm`;
}

/** Rain readout for a destination/alert card's meta line (issue #226). Shows the recent 7-day
 * total inline (the "go now" signal) with the fuller breakdown - other recent windows and the
 * per-observation antecedent means - in the hover title. Empty string when no rain data yet. */
export function rainMeta(r: {
  precip_recent_7d_mm?: number | null;
  precip_recent_14d_mm?: number | null;
  precip_recent_30d_mm?: number | null;
  precip_obs_7d_mm?: number | null;
  precip_obs_30d_mm?: number | null;
}): string {
  if (r.precip_recent_7d_mm == null && r.precip_obs_7d_mm == null) return "";
  const parts: string[] = [];
  if (r.precip_recent_7d_mm != null) parts.push(`recent 7d ${rainLabel(r.precip_recent_7d_mm)}`);
  if (r.precip_recent_14d_mm != null) parts.push(`14d ${rainLabel(r.precip_recent_14d_mm)}`);
  if (r.precip_recent_30d_mm != null) parts.push(`30d ${rainLabel(r.precip_recent_30d_mm)}`);
  if (r.precip_obs_7d_mm != null) parts.push(`observed-here 7d mean ${rainLabel(r.precip_obs_7d_mm)}`);
  if (r.precip_obs_30d_mm != null) parts.push(`30d mean ${rainLabel(r.precip_obs_30d_mm)}`);
  const inline =
    r.precip_recent_7d_mm != null ? rainLabel(r.precip_recent_7d_mm) : rainLabel(r.precip_obs_7d_mm ?? 0);
  const title = `Rain (Open-Meteo, informational): ${parts.join(" · ")}`;
  return ` · <span class="num" title="${title}">rain ${inline}</span>`;
}

/** The card-annotation fields of a `fire_nearby` entry (issue #227) - a structural subset so
 * both the generated schema type and the `./api/types` alias satisfy it. */
export interface FireBadge {
  status: string;
  name: string;
  fire_year: number | null;
  distance_km: number;
}

/** Fire annotation line for a destination / alert / plan-stop card (issue #227). Active fires
 * render as a warning; low/moderate/unknown-severity year-1/2 burn scars as a morel
 * opportunity. Informational only - never asserts a closure. Empty string when nothing nearby. */
export function fireBadges(fires: readonly FireBadge[] | undefined): string {
  const active = (fires ?? []).filter((fire) => fire.status === "active");
  const scars = (fires ?? []).filter((fire) => fire.status === "historical");
  const rows: string[] = [];
  const nearestActive = active[0];
  if (nearestActive) {
    rows.push(
      `<div class="fire-warn">⚠ Active fire ${dist(nearestActive.distance_km)} away` +
        (active.length > 1 ? ` (+${active.length - 1} more)` : "") +
        ` · safety/access, verify officially</div>`,
    );
  }
  const nearestScar = scars[0];
  if (nearestScar) {
    const year = nearestScar.fire_year ? ` (${nearestScar.fire_year})` : "";
    rows.push(
      `<div class="fire-scar">🔥 Burn scar${year} ${dist(nearestScar.distance_km)} away · burn-morel opportunity</div>`,
    );
  }
  return rows.join("");
}

// Presentation hook fired whenever a scoping input that a filter pill's label reflects changes
// (home/radius, units, months, genera, layers). ui/pills.ts registers refreshPills() here so
// the pill row stays in sync without map.ts / ui-prefs.ts importing the pill module (which
// would cycle through map/sheet). No-op until initPills() runs.
export let onScopeChange: () => void = () => {};
export function setScopeChangeHook(hook: () => void): void {
  onScopeChange = hook;
}

export function qs<T extends HTMLElement = HTMLElement>(selector: string, root: ParentNode = document): T {
  const element = root.querySelector<T>(selector);
  if (!element) throw new Error(`missing element: ${selector}`);
  return element;
}

const isApiError = (error: unknown): error is ApiError & { detail: string } =>
  typeof error === "object" && error !== null && typeof (error as ApiError).detail === "string";

export const errorDetail = (error: unknown): string => {
  if (!isApiError(error)) {
    console.error(error);
    return "error";
  }
  return error.detail;
};

export const inatUrl = (taxonId: number): string => `https://www.inaturalist.org/taxa/${taxonId}`;

/** Scientific name, with the common name parenthesized when the genus has one on iNat (same
 * format as genera.ts's search results) - most of the ~6,018-genus catalog lacks an English
 * common name, so the scientific name is always the primary, reliable label. */
export const displayName = (entry: { name: string; common_name?: string | null }): string =>
  entry.common_name ? `${entry.name} (${entry.common_name})` : entry.name;

// Re-exported so the many `from "./state"` importers keep working; the implementation
// (and escapeXml / feeLabel) now lives in the pure-helper module.
export { escapeHtml } from "./format";

export function setStatus(text: string): void {
  qs("#status").textContent = text;
  // Mirror into the mobile bottom sheet's peek summary (issue #229), when present.
  const summary = document.getElementById("sheet-summary");
  if (summary) summary.textContent = text;
}

/** No months toggled reads the same as all 12 toggled - there's no actual restriction either
 * way, so callers that need to distinguish the two (e.g. phenology-% wording) check
 * `state.months.size` themselves rather than trying to infer it from this string. */
export function monthsParam(): string {
  const ordered = [...state.months].sort((left, right) => left - right);
  return ordered.length ? ordered.join(",") : "1,2,3,4,5,6,7,8,9,10,11,12";
}
