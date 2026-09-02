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
