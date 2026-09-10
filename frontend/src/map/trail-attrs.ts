// Pure readers over the OSM detail tags a trail/road row carries (`Trail.attrs`). Kept leaflet-free
// so the banding logic is unit-testable - `layers.ts` (which imports leaflet, and so can't load
// under jsdom) and `road-inspect.ts` both pull from here.

import type { Trail } from "../api/types";

type Attrs = Trail["attrs"];

// Ungraded / rough tread: the selected-trail line draws dashed for these so a graded road and a
// washed-out two-track don't look alike on the map. `tracktype` grade4/5 is the primary signal;
// `surface` / `smoothness` back it up when tracktype is absent (common on `highway=path`).
const ROUGH_SURFACE = new Set(["ground", "dirt", "earth", "mud", "grass", "sand", "rock", "pebblestone"]);
const ROUGH_SMOOTHNESS = new Set(["bad", "very_bad", "horrible", "very_horrible", "impassable"]);

export function isRoughSurface(attrs: Attrs): boolean {
  if (!attrs) return false;
  const tracktype = attrs.tracktype;
  if (tracktype === "grade4" || tracktype === "grade5") return true;
  if (tracktype === "grade1" || tracktype === "grade2") return false; // graded - explicitly smooth
  return ROUGH_SURFACE.has(attrs.surface ?? "") || ROUGH_SMOOTHNESS.has(attrs.smoothness ?? "");
}

// "Grade 3" from `tracktype=grade3`, or null when the way carries no tracktype. grade1 is a
// maintained surface, grade5 is barely a track - the card shows the number, the map dashes 4-5
// (see `isRoughSurface`).
export function gradeLabel(attrs: Attrs): string | null {
  const match = /^grade([1-5])$/.exec(attrs?.tracktype ?? "");
  return match ? `grade ${match[1]}` : null;
}

// A time-limited restriction on the way - a snow gate, a winter closure, a fire-season gate. OSM
// carries these as `access:conditional` ("no @ (Nov-May)") or a bare `seasonal=yes|winter`. We
// don't parse the opening-hours grammar into an open/closed decision (that needs the viewer's
// date and a full parser); we just tell the forager the road isn't open year-round and hand them
// the raw condition. `foot:conditional` wins when present since walking is what matters here.
export function seasonalNote(attrs: Attrs): string | null {
  if (!attrs) return null;
  const condition =
    attrs["foot:conditional"] ?? attrs["motor_vehicle:conditional"] ?? attrs["access:conditional"];
  if (condition) return condition.trim();
  const seasonal = attrs.seasonal;
  if (seasonal === "yes") return "not open year-round";
  if (seasonal && seasonal !== "no") return `${seasonal} only`; // seasonal=winter, seasonal=summer, ...
  return null;
}
