// The three persisted-preference toggles in the header (theme, text size, distance units).
// Each reads its stored value through prefs.ts, applies it to the DOM / app state, and writes
// the new value back on click. Split out of main.ts (issue #242 Part 2d).

import { currentTheme, setTiles, updateHome } from "./map";
import { getLargeText, setLargeText, setTheme, setUnits } from "./prefs";
import { qs, state, type Units } from "./state";

export function initTheme(): void {
  const toggle = qs<HTMLButtonElement>("#theme-toggle");
  const apply = (theme: "dark" | "light"): void => {
    document.documentElement.dataset.theme = theme;
    toggle.textContent = theme === "dark" ? "🌙" : "☀️";
    toggle.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    toggle.setAttribute("aria-pressed", String(theme === "dark"));
    setTiles(); // no-op until the map exists; initMap lays the first tiles
  };
  apply(currentTheme()); // the inline <head> script already set the attribute (default dark)
  toggle.onclick = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    setTheme(next);
    apply(next);
  };
}

// Persisted like theme/units - toggles a root data attribute that style.css uses to bump up
// font sizes across the panel/cards/map controls for readability on a phone.
export function initTextSize(): void {
  const toggle = qs<HTMLButtonElement>("#text-size-toggle");
  const apply = (large: boolean): void => {
    document.documentElement.dataset.textSize = large ? "large" : "normal";
    toggle.setAttribute("aria-pressed", String(large));
    toggle.title = large ? "Switch to normal text size" : "Switch to larger text";
  };
  apply(getLargeText());
  toggle.onclick = () => {
    const next = document.documentElement.dataset.textSize !== "large";
    setLargeText(next);
    apply(next);
  };
}

export function initUnits(): void {
  const toggle = qs<HTMLButtonElement>("#units-toggle");
  const apply = (units: Units): void => {
    state.units = units;
    toggle.textContent = units;
    toggle.title = units === "mi" ? "Switch to kilometers" : "Switch to miles";
    toggle.setAttribute("aria-pressed", String(units === "mi"));
    if (state.home) updateHome(state.home);
  };
  apply(state.units);
  toggle.onclick = () => {
    const next: Units = state.units === "mi" ? "km" : "mi";
    setUnits(next);
    apply(next);
  };
}
