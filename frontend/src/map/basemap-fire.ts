// Wildfire/burn-scar vector tiles (issue #336 PR 2), proxied same-origin from martin
// (api/routes/tiles.py) and read straight off the `fire` source-layer (infra/martin-config.yaml
// publishes `fire_perimeters`). Replaces the old `GET /api/fire` fetch + Leaflet `L.GeoJSON`
// layer (layers.ts's `loadFire`). Geometry is mixed - a perimeter/scar is a Polygon, a WFIGS
// location with none yet is a Point (`is_point`) - split here on MVT's own `$type` rather than
// that flag, since a fill layer can't render a Point and a circle layer can't render a Polygon.

import type {
  ExpressionSpecification,
  LayerSpecification,
  SourceSpecification,
} from "@maplibre/maplibre-gl-style-spec";

import type { PopupSpec } from "./popup";

export const FIRE_SOURCE_ID = "foray-fire";
const FIRE_FILL_LAYER_ID = "foray_fire_fill";
const FIRE_LINE_LAYER_ID = "foray_fire_line";
const FIRE_POINT_LAYER_ID = "foray_fire_point";
const FIRE_SOURCE_LAYER = "fire";
export const FIRE_LAYER_IDS = [FIRE_FILL_LAYER_ID, FIRE_LINE_LAYER_ID, FIRE_POINT_LAYER_ID];

// Same palette as the old Leaflet layer (map.ts's FIRE_ACTIVE/FIRE_SCAR).
export const FIRE_ACTIVE = "#ff3b1f"; // hot red - active wildfire perimeter/point
export const FIRE_SCAR = "#ff8c42"; // burnt orange - recent burn scar (dimmer for older years)

/** The fire MVT source - nationwide, like land (no viewport-scoped bbox param). */
export function fireSource(tilesUrl: string): Record<string, SourceSpecification> {
  return {
    [FIRE_SOURCE_ID]: {
      type: "vector",
      tiles: [tilesUrl],
      minzoom: 0,
      maxzoom: 12,
    },
  } as Record<string, SourceSpecification>;
}

function fireColorExpression(): ExpressionSpecification {
  return [
    "case",
    ["==", ["get", "status"], "active"],
    FIRE_ACTIVE,
    FIRE_SCAR,
  ] as unknown as ExpressionSpecification;
}

// Mirrors layers.ts's old `scarOpacity`: active fires always read hot; a scar dims with age,
// falling back to the oldest (dimmest) bucket when `fire_year` is missing. `currentYear` is
// baked in at layer-build time (a style rebuild already happens on every toggle change - see
// basemap.ts - so this never goes stale for longer than a page load).
function scarOpacityExpression(currentYear: number): ExpressionSpecification {
  const age: ExpressionSpecification = [
    "-",
    currentYear,
    ["coalesce", ["get", "fire_year"], currentYear - 3],
  ] as unknown as ExpressionSpecification;
  return [
    "case",
    ["==", ["get", "status"], "active"],
    0.25,
    ["<=", age, 1],
    0.35,
    ["==", age, 2],
    0.22,
    0.12,
  ] as unknown as ExpressionSpecification;
}

// A visible perimeter/scar boundary, not just a translucent fill - matches the old Leaflet
// layer's `color`/`weight` outline (Copilot review, PR #369: a fill-only replacement dropped
// that boundary cue). An active perimeter draws a touch heavier than a scar, same as before.
function fireLineWidthExpression(): ExpressionSpecification {
  return ["case", ["==", ["get", "status"], "active"], 2, 1] as unknown as ExpressionSpecification;
}

/** Fill + outline (polygons: perimeters + burn scars) + circle (points: a WFIGS location with
 * no perimeter yet) layers, shown/hidden for `visible` right from the build - same "one place
 * decides, basemap.ts's initial build and setFireLayerState's live update both call it" reasoning
 * as `landLayers`. Fire has one toggle for all three layers, unlike land's per-agency split. */
export function fireLayers(visible: boolean, currentYear = new Date().getFullYear()): LayerSpecification[] {
  const color = fireColorExpression();
  const visibility = visible ? "visible" : "none";
  const polygonFilter = ["==", ["geometry-type"], "Polygon"] as const;
  return [
    {
      id: FIRE_FILL_LAYER_ID,
      type: "fill",
      source: FIRE_SOURCE_ID,
      "source-layer": FIRE_SOURCE_LAYER,
      filter: polygonFilter,
      layout: { visibility },
      paint: { "fill-color": color, "fill-opacity": scarOpacityExpression(currentYear) },
    },
    {
      id: FIRE_LINE_LAYER_ID,
      type: "line",
      source: FIRE_SOURCE_ID,
      "source-layer": FIRE_SOURCE_LAYER,
      filter: polygonFilter,
      layout: { visibility },
      paint: { "line-color": color, "line-width": fireLineWidthExpression(), "line-opacity": 0.9 },
    },
    {
      id: FIRE_POINT_LAYER_ID,
      type: "circle",
      source: FIRE_SOURCE_ID,
      "source-layer": FIRE_SOURCE_LAYER,
      filter: ["==", ["geometry-type"], "Point"],
      layout: { visibility },
      paint: {
        "circle-radius": 6,
        "circle-color": color,
        "circle-opacity": 0.9,
        "circle-stroke-width": 1,
        "circle-stroke-color": "#0c0d09",
      },
    },
  ] as LayerSpecification[];
}

/** The tile properties a `foray_fire_fill`/`foray_fire_point` feature carries
 * (martin-config.yaml's `fire` table properties) - read straight off a click hit. */
export interface FireProps {
  id?: string;
  name?: string;
  status?: string; // "active" | "historical"
  fire_year?: number | null;
  percent_contained?: number | null;
  gis_acres?: number | null;
  dominant_severity?: string | null;
  incident_url?: string | null;
}

// `props.name` comes from an external service (buildPopup sets it via textContent); the
// incident url is server-constructed (InciWeb / NIFC, see sources/fire.py).
export function firePopupSpec(props: FireProps): PopupSpec {
  const bits: string[] = [];
  if (props.status === "active") {
    bits.push("Active fire");
    if (props.percent_contained != null) bits.push(`${Math.round(props.percent_contained)}% contained`);
  } else {
    bits.push(props.fire_year ? `${props.fire_year} burn scar` : "Burn scar");
    if (props.dominant_severity) bits.push(`${props.dominant_severity} severity`);
  }
  if (props.gis_acres != null) bits.push(`${Math.round(props.gis_acres).toLocaleString()} ac`);
  return {
    title: props.name ?? "Fire",
    lines: [bits.join(" · "), "Informational only - check official sources before travel"],
    ...(props.incident_url ? { link: { href: props.incident_url, text: "Incident info ↗" } } : {}),
  };
}
