import type L from "leaflet";

import type { ApiError, Home, TripPlan } from "./api/types";

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const CURRENT_MONTH = new Date().getMonth() + 1; // 1-12

export type View = "destinations" | "calendar" | "alerts" | "plan";

export type Units = "km" | "mi";

export interface State {
  months: Set<number>;
  view: View;
  home: Home | null;
  markers: L.CircleMarker[];
  campMarkers: L.CircleMarker[];
  landLayer: L.GeoJSON | null;
  trailLayer: L.GeoJSON | null;
  planRouteLayer: L.Polyline | null;
  planTrip: TripPlan | null;
  focused: { lat: number; lng: number } | null;
  units: Units;
}

export const state: State = {
  months: new Set([CURRENT_MONTH]),
  view: "destinations",
  home: null,
  markers: [],
  campMarkers: [],
  landLayer: null,
  trailLayer: null,
  planRouteLayer: null,
  planTrip: null,
  focused: null,
  units: ((): Units => {
    const stored = localStorage.getItem("foray-units");
    return stored === "km" || stored === "mi" ? stored : "mi";
  })(),
};

const KM_TO_MI = 0.621371;

export function dist(km: number): string {
  if (state.units === "mi") return `${Math.round(km * KM_TO_MI)} mi`;
  return `${Math.round(km)} km`;
}

export function distVal(km: number): number {
  if (state.units === "mi") return Math.round(km * KM_TO_MI);
  return Math.round(km);
}

export function qs<T extends HTMLElement = HTMLElement>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`missing element: ${selector}`);
  return element;
}

export const errorDetail = (error: unknown): string => (error as ApiError)?.detail ?? "error";

export const inatUrl = (taxonId: number): string => `https://www.inaturalist.org/taxa/${taxonId}`;

/** Escape text destined for an HTML string template (innerHTML / Leaflet popup strings). */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function setStatus(text: string): void {
  qs("#status").textContent = text;
}
