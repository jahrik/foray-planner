// Click-to-inspect for the vector basemap: turn a rendered road/trail feature into a popup of
// its OSM tags. The Protomaps `roads` source-layer already carries normalized tags for every
// way we draw, so a tap reads them straight off the tile - no API call, and it can stand in
// for the `/api/trails/network` Overpass round-trip.
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
 * "Trail", so the line is just the bare `highway=` tag (no repeated prose). `enrichedRoadPopupSpec`
 * replaces this once `/api/trails` comes back with the real name.
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

/** A row from `GET /api/trails` - only the fields the enriched popup reads. */
export interface NearbyTrail {
  name: string;
  kind: string; // road | path | route | trailhead
  url: string;
  distance_km: number;
  length_km?: number | null;
  walk_in?: boolean;
  attrs?: Record<string, string> | null;
}

// Synthetic names `sources/trails.py` writes when OSM had neither a name nor a ref.
const SYNTHETIC_TRAIL_NAMES = new Set(["Forest road (OSM)", "Trail (OSM)", "Hiking route (OSM)"]);
// How close a `/api/trails` hit must be to the click to trust it as the way that was clicked.
// Our cached geometry is thinned to 60 points, so allow a little slack.
const ENRICH_MAX_KM = 0.1;

/**
 * Whether to look the clicked way up in `/api/trails` for its real name: only when the tile gave
 * us none (a named way already shows what we'd fetch) and only for the classes workstream A
 * ingests - a track or a foot/path kind. Protomaps normalizes `highway=track` to `path` at some
 * zooms, so both map here and the lookup itself is not constrained by `kind` (see `pickNearbyTrail`).
 */
export function shouldEnrichRoad(props: RoadProps): boolean {
  if (roadName(props)) return false;
  const detail = props.kind_detail;
  if (detail === "track") return true;
  if (detail && TRAIL_KIND_DETAIL.has(detail)) return true;
  return !detail && props.kind === "path";
}

/** The nearest `/api/trails` row that is an actual way (a road or path, not a trailhead node or
 * a route relation), or `undefined`. Rows come back nearest-first. */
export function pickNearbyTrail(rows: readonly NearbyTrail[]): NearbyTrail | undefined {
  return rows.find((row) => row.kind === "road" || row.kind === "path");
}

/**
 * Fold a nearby `/api/trails` row into the popup: the way's real name / road number, surface,
 * grade, walk-in status, and a link to the actual OSM way. Returns `null` when the nearest row
 * is too far to be the clicked way (so the caller keeps the tile-only popup).
 */
export function enrichedRoadPopupSpec(
  props: RoadProps,
  lat: number,
  lng: number,
  trail: NearbyTrail | undefined,
): PopupSpec | null {
  if (!trail || trail.distance_km > ENRICH_MAX_KM) return null;
  const attrs = trail.attrs ?? {};
  const realName = trail.name && !SYNTHETIC_TRAIL_NAMES.has(trail.name) ? trail.name : undefined;

  const lines = [typeLine(props)];
  if (attrs.ref) lines.push(`Ref ${attrs.ref}`);
  if (attrs.surface) lines.push(`Surface: ${attrs.surface.replace(/_/g, " ")}`);
  if (attrs.tracktype) lines.push(`Grade: ${attrs.tracktype.replace(/^grade/, "grade ")}`);
  if (trail.walk_in) lines.push("Walk-in (gated to vehicles)");
  else if (["no", "private"].includes(attrs.motor_vehicle ?? attrs.access ?? ""))
    lines.push("Access restricted");
  if (flagged(props.is_bridge)) lines.push("Bridge");
  if (flagged(props.is_tunnel)) lines.push("Tunnel");

  return {
    title: realName ?? (attrs.ref ? `Ref ${attrs.ref}` : fallbackTitle(props)),
    lines,
    link: trail.url ? { href: trail.url, text: "View on OpenStreetMap ↗" } : osmLink(lat, lng),
  };
}
