// Contrast pass over the Protomaps base themes.
//
// The stock `dark` theme sits in a ~15% luminance band - earth #1f1f1f, water #31353f (barely
// off the background), forest a near-black green, every road a dark grey a hair above the land.
// It reads as "a dark surface", not "a map you navigate a forest with". This lifts the handful
// of things a forager reads off the base - land vs water vs forest, the road hierarchy, place
// and peak labels - while keeping it a genuinely dark map. The stock `light` theme is already
// legible, so it passes through untouched.

import type { LayerSpecification } from "@maplibre/maplibre-gl-style-spec";
import { layersWithPartialCustomTheme, type Theme } from "protomaps-themes-base";

// protomaps-themes-base replaces (does not merge) the nested `landcover` object, turning any
// key we omit into `null` and breaking that fill - so the full set is repeated here.
const DARK_LANDCOVER = {
  grassland: "rgba(33, 46, 34, 1)",
  barren: "rgba(42, 42, 39, 1)",
  urban_area: "rgba(32, 32, 33, 1)",
  farmland: "rgba(36, 43, 35, 1)",
  glacier: "rgba(48, 48, 50, 1)",
  scrub: "rgba(44, 48, 35, 1)",
  forest: "rgba(30, 51, 40, 1)",
};

// Only the keys that change. Everything else falls through to `namedTheme("dark")`.
const DARK_OVERRIDES: Partial<Theme> = {
  background: "#20232a",
  earth: "#26262a",
  // Water: a real navy so lakes / rivers / coastline register at a glance.
  water: "#1c3b57",
  waterway_label: "#7196b4",
  ocean_label: "#7196b4",
  // Vegetation: actual dark greens / olive rather than near-black.
  wood_a: "#1e3328",
  wood_b: "#1e3328",
  park_a: "#1f3a2e",
  park_b: "#22402f",
  scrub_a: "#2c3023",
  scrub_b: "#2c3023",
  landcover: DARK_LANDCOVER,
  // Roads: lift the whole hierarchy clear of the earth, warm the majors so a route stands out.
  minor_a: "#565660",
  minor_b: "#4b4b53",
  minor_service: "#454548",
  other: "#4a4a4a",
  link: "#82827a",
  major: "#82827a",
  major_casing_early: "#131313",
  major_casing_late: "#131313",
  highway: "#a39a86",
  highway_casing_early: "#131313",
  highway_casing_late: "#131313",
  bridges_minor: "#4b4b53",
  bridges_major: "#82827a",
  bridges_highway: "#a39a86",
  // Labels: readable, near-black halos so they hold over the lighter greens.
  roads_label_minor: "#9a9aa2",
  roads_label_minor_halo: "#16181c",
  roads_label_major: "#bdb8b0",
  roads_label_major_halo: "#16181c",
  city_label: "#c4c4c9",
  city_label_halo: "#161616",
  state_label: "#5c5c64",
  subplace_label: "#8a8a92",
  peak_label: "#b3a897",
  boundaries: "#74809a",
};

/**
 * Base-theme layer list for the given mode. `dark` gets the contrast pass above; `light` is the
 * stock theme. The road-class overrides in `basemap-roads.ts` run on top of this.
 */
export function themedBaseLayers(mode: "dark" | "light"): LayerSpecification[] {
  if (mode === "light") {
    return layersWithPartialCustomTheme("protomaps", "light", {}, "en");
  }
  return layersWithPartialCustomTheme("protomaps", "dark", DARK_OVERRIDES, "en");
}
