// Single owner for the three persisted UI preferences (theme, text size, distance units), each
// a plain localStorage key read/written in a couple of places before this module. `public/
// theme-init.js` still reads `foray-theme` directly - it runs as a classic script in <head>
// before any module loads (to set the theme attribute before first paint), so it can't import
// from here; keep the key name (`foray-theme`) in sync with STORAGE_KEYS.theme.

import type { Units } from "./state";

const STORAGE_KEYS = {
  theme: "foray-theme",
  textSize: "foray-text-size",
  units: "foray-units",
  months: "foray-months",
  manualHome: "foray-manual-home",
} as const;

type Theme = "dark" | "light";

// Default dark - matches theme-init.js and the pre-module <head> attribute.
export function getTheme(): Theme {
  return localStorage.getItem(STORAGE_KEYS.theme) === "light" ? "light" : "dark";
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEYS.theme, theme);
}

export function getLargeText(): boolean {
  return localStorage.getItem(STORAGE_KEYS.textSize) === "large";
}

export function setLargeText(large: boolean): void {
  localStorage.setItem(STORAGE_KEYS.textSize, large ? "large" : "normal");
}

// Default miles - the app's audience is largely US-based (public-land / iNat coverage).
export function getUnits(): Units {
  const stored = localStorage.getItem(STORAGE_KEYS.units);
  return stored === "km" || stored === "mi" ? stored : "mi";
}

export function setUnits(units: Units): void {
  localStorage.setItem(STORAGE_KEYS.units, units);
}

// Selected month filter (1-12). Persisted like the other pill state so a reload doesn't
// silently snap back to the current month. `null` = never set (caller defaults to the
// current month); an explicit empty array is kept (reads as "all months" via monthsParam).
export function getMonths(): number[] | null {
  const stored = localStorage.getItem(STORAGE_KEYS.months);
  if (stored === null) return null;
  return stored
    .split(",")
    .map((part) => Number(part))
    .filter((month) => Number.isInteger(month) && month >= 1 && month <= 12);
}

export function setMonths(months: Iterable<number>): void {
  localStorage.setItem(STORAGE_KEYS.months, [...months].sort((left, right) => left - right).join(","));
}

// The point the user last set by hand (search / map click), as "lat,lng" - geolocate.ts won't let
// a coarse device fix (an IP / cell-tower guess) silently replace it. Cleared when a device fix
// is applied. Compared against the server's saved home, so a home changed on another tab or by a
// device fix since then reads as not-manual.
export function getManualHome(): { lat: number; lng: number } | null {
  const stored = localStorage.getItem(STORAGE_KEYS.manualHome);
  if (!stored) return null;
  const [lat, lng] = stored.split(",").map(Number);
  return Number.isFinite(lat) && Number.isFinite(lng) ? { lat: lat as number, lng: lng as number } : null;
}

export function setManualHome(point: { lat: number; lng: number } | null): void {
  if (point) localStorage.setItem(STORAGE_KEYS.manualHome, `${point.lat},${point.lng}`);
  else localStorage.removeItem(STORAGE_KEYS.manualHome);
}
