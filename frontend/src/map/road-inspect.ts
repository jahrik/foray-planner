// Click-to-inspect for the vector basemap: turn a rendered Protomaps road feature into a popup
// of its OSM tags - a tap reads them straight off the tile, no API call. A click that instead
// lands on our own `foray_trails` layer (issue #336) skips this entirely and selects the trail
// (map.ts, same as clicking a Trails-tab trailhead) rather than popping up its tags - that layer
// replaced the old fallback `GET /api/trails` name lookup this module used to do here.
//
// Pure helpers only (no maplibre/leaflet imports) so they unit-test without a GL context;
// map.ts does the `queryRenderedFeatures` and opens the Leaflet popup.

import type { PopupSpec } from "./popup";

/** The Protomaps flat-schema fields we read off a `roads` feature. All optional - a minor
 * unnamed way carries almost none of them. */
export interface RoadProps {
  kind?: string; // protomaps class: highway / major_road / medium_road / minor_road / link / path / other
  kind_detail?: string; // raw OSM highway=* value: track / path / footway / service / residential / ...
  name?: string;
  "name:en"?: string;
  ref?: string; // route number, e.g. "FR 300"
  is_bridge?: boolean | number;
  is_tunnel?: boolean | number;
}

/** Line-layer ids drawn from the `roads` source-layer, minus the casing/label helpers (they
 * echo the same feature). Read off the live style so a protomaps-themes-base or basemap-roads
 * rename can't silently drop the hit test. */
export function roadLineLayerIds(
  layers: ReadonlyArray<{ id: string; type?: string; "source-layer"?: string }>,
): string[] {
  return layers
    .filter(
      (layer) =>
        layer["source-layer"] === "roads" &&
        layer.type === "line" &&
        !layer.id.includes("casing") &&
        !layer.id.includes("label"),
    )
    .map((layer) => layer.id);
}

/** Pick the most useful hit under the cursor: a named way wins over an unnamed one, otherwise
 * the topmost (queryRenderedFeatures returns front-to-back). `null` when nothing was hit. */
export function pickRoadFeature<T extends { properties?: RoadProps | null }>(
  features: ReadonlyArray<T>,
): T | null {
  if (features.length === 0) return null;
  const named = features.find((feature) => roadName(feature.properties ?? {}));
  return named ?? features[0]!;
}

function roadName(props: RoadProps): string | undefined {
  return props["name:en"] || props.name || undefined;
}

// Raw OSM highway=* value -> human label. Anything not listed falls back to a title-cased
// version of the value itself ("living_street" -> "Living street road").
const KIND_DETAIL_LABEL: Record<string, string> = {
  track: "Forest / logging road",
  path: "Path",
  footway: "Footpath",
  bridleway: "Bridleway",
  steps: "Steps",
  cycleway: "Cycleway",
  pedestrian: "Pedestrian street",
  service: "Service road",
  motorway: "Motorway",
  trunk: "Highway",
  primary: "Primary road",
  secondary: "Secondary road",
  tertiary: "Tertiary road",
  residential: "Residential road",
  unclassified: "Minor road",
  living_street: "Living street",
};

const TRAIL_KIND_DETAIL = new Set(["path", "footway", "bridleway", "steps", "cycleway"]);

function titleCase(value: string): string {
  return value.replace(/(^|[_\s-])([a-z])/g, (_match, sep: string, char: string) =>
    sep ? ` ${char.toUpperCase()}` : char.toUpperCase(),
  );
}

function typeLine(props: RoadProps): string {
  const detail = props.kind_detail;
  if (detail) {
    const label = KIND_DETAIL_LABEL[detail] ?? `${titleCase(detail)} road`;
    return `${label} (highway=${detail})`;
  }
  if (props.kind) return titleCase(props.kind.replace(/_/g, " "));
  return "Road";
}

function fallbackTitle(props: RoadProps): string {
  const detail = props.kind_detail ?? "";
  if (detail === "track") return "Forest road";
  if (TRAIL_KIND_DETAIL.has(detail) || props.kind === "path") return "Trail";
  return "Unnamed road";
}

function flagged(value: boolean | number | undefined): boolean {
  return value === true || value === 1;
}

function osmLink(lat: number, lng: number): NonNullable<PopupSpec["link"]> {
  return {
    href: `https://www.openstreetmap.org/#map=17/${lat.toFixed(5)}/${lng.toFixed(5)}`,
    text: "View on OpenStreetMap ↗",
  };
}

/**
 * Popup contents from the tile alone. `lat`/`lng` are the click point: Protomaps tiles carry
 * only normalized tags, not the OSM way id, so the link points at the location. For a named way
 * the first line spells out the class; for an unnamed one the title already says "Forest road" /
 * "Trail", so the line is just the bare `highway=` tag (no repeated prose). A way our own trails
 * layer covers never reaches this popup at all - map.ts's click handler checks that layer first.
 */
export function roadPopupSpec(props: RoadProps, lat: number, lng: number): PopupSpec {
  const name = roadName(props);
  const lines: string[] = [];
  if (name) lines.push(typeLine(props));
  else if (props.kind_detail) lines.push(`highway=${props.kind_detail}`);
  if (props.ref) lines.push(`Ref ${props.ref}`);
  if (flagged(props.is_bridge)) lines.push("Bridge");
  if (flagged(props.is_tunnel)) lines.push("Tunnel");
  return { title: name ?? fallbackTitle(props), lines, link: osmLink(lat, lng) };
}
