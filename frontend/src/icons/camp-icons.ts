// Camp-type icons (issue #451): one small silhouette per kind of camp a campsite row can be, drawn
// the same way as the genus icons - hand-made 24x24 `currentColor` SVGs in `./camps/`, bundled with
// the client (no runtime fetch), ink on a free/paid-coloured disc on the map. The backend derives
// `camp_type` from what the source reports (OSM tags, RIDB's site list or facility name) and sends
// it on every CampSite; the icon says what the source reports, nothing about legality or
// availability (AGENTS.md "No claims"). A row whose source says nothing - or says "pitch", a lone
// OSM pitch - gets the generic campfire.

import type { components } from "../api/schema";

export type CampType = NonNullable<components["schemas"]["CampSite"]["camp_type"]>;

export const CAMP_ICON_KEYS = [
  "tent",
  "rv",
  "mixed",
  "backcountry",
  "group",
  "equestrian",
  "cabin",
  "generic",
] as const;
export type CampIconKey = (typeof CAMP_ICON_KEYS)[number];

// Compile-time only: `never` unless the API grows a camp type this module has no art or label for.
type Unlisted = Exclude<CampType, CampIconKey | "pitch">;
const everyTypeListed: [Unlisted] extends [never] ? true : Unlisted = true;
void everyTypeListed;

export const CAMP_ICON_LABELS: Record<CampIconKey, string> = {
  tent: "Tent camping",
  rv: "RV camping",
  mixed: "Tent and RV camping",
  backcountry: "Hike-in / backcountry",
  group: "Group camping",
  equestrian: "Equestrian camping",
  cabin: "Cabin or yurt",
  generic: "Campground",
};

const SVG_FILES = import.meta.glob<string>("./camps/*.svg", {
  query: "?raw",
  import: "default",
  eager: true,
});

const markupByKey = new Map<string, string>(
  Object.entries(SVG_FILES).map(([path, markup]) => [path.replace(/^.*\/(.+)\.svg$/, "$1"), markup.trim()]),
);

/** The icon a camp type draws with: its own, or the generic campfire for an unstated type, a lone
 * pitch, or a type newer than this bundle. */
export function campIconKey(type: string | null | undefined): CampIconKey {
  return (CAMP_ICON_KEYS as readonly string[]).includes(type ?? "") ? (type as CampIconKey) : "generic";
}

/** The SVG markup for a camp type. Static bundled files, so safe to place via innerHTML. */
export function campIconSvg(type: string | null | undefined): string {
  return markupByKey.get(campIconKey(type)) ?? "";
}
