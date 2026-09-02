import type L from "leaflet";

// Shared style factory for the circle / CircleMarker layers we draw. Every one of them sets
// `bubblingMouseEvents: false` so a click on the marker (to open its popup or select its card)
// doesn't also reach the map's click handler, which would treat it as "set home here".
// `circleStyle` bakes that in and defaults the stroke colour to the fill colour (the common
// case); pass `stroke` when they differ. No DOM, no Leaflet import at runtime - see markers.test.ts.

export interface CircleStyleInput {
  radius: number;
  /** Fill colour, and the stroke colour too unless `stroke` is given. */
  fill: string;
  stroke?: string;
  weight?: number;
  fillOpacity?: number;
  opacity?: number;
  /** e.g. "3 3" for a dashed ring; omitted or undefined means a solid ring. */
  dashArray?: string | undefined;
}

export function circleStyle(input: CircleStyleInput): L.CircleMarkerOptions {
  const style: L.CircleMarkerOptions = {
    radius: input.radius,
    color: input.stroke ?? input.fill,
    fillColor: input.fill,
    bubblingMouseEvents: false,
  };
  if (input.weight !== undefined) style.weight = input.weight;
  if (input.fillOpacity !== undefined) style.fillOpacity = input.fillOpacity;
  if (input.opacity !== undefined) style.opacity = input.opacity;
  if (input.dashArray !== undefined) style.dashArray = input.dashArray;
  return style;
}
