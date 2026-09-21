// Deep links that hand a location (or a whole planned route) off to the user's own maps app
// (issue #310). `geo:` is the only URI scheme that reliably triggers the OS "open with" chooser
// on Android/iOS - it drops a pin, and the user taps "Directions" one tap later in whichever app
// opens. There's no cross-app URI that starts turn-by-turn directly. `google.com/maps/dir` is
// the one exception worth building for: it's the only maps app URL that accepts a multi-stop
// route, so it's used for the whole-trip "Open in Google Maps" button instead of a per-app
// chooser (planner.ts).
//
// Every href here is built from numeric lat/lng plus a caller-supplied label that we
// URL-encode ourselves - never raw external text dropped into a URL - so this holds the same
// safety contract as the rest of `buildPopup`'s callers (see popup.ts).

import type { PopupLink } from "./popup";

function geoUri(lat: number, lng: number, label: string): string {
  const coords = `${lat.toFixed(6)},${lng.toFixed(6)}`;
  return `geo:${coords}?q=${coords}(${encodeURIComponent(label)})`;
}

/** A "Directions" popup link for a single point - see the module doc for why `geo:` over a
 * provider-specific URL. */
export function directionsLink(lat: number, lng: number, label: string): PopupLink {
  return { href: geoUri(lat, lng, label), text: "Directions" };
}

export interface RoutePoint {
  lat: number;
  lng: number;
}

// Google Maps' `dir/?api=1` URL silently drops waypoints past this count - see planner.ts's
// stop-count check before offering the button.
export const GOOGLE_MAPS_WAYPOINT_CAP = 9;

/** A Google Maps multi-stop driving route: start -> waypoints in order -> destination. The only
 * maps-app URL that accepts more than one stop (module doc); everyone else keeps using the GPX
 * export. Caller is responsible for the `GOOGLE_MAPS_WAYPOINT_CAP` check. */
export function googleMapsRouteUrl(
  origin: RoutePoint,
  destination: RoutePoint,
  waypoints: readonly RoutePoint[],
): string {
  const params = new URLSearchParams({
    api: "1",
    origin: `${origin.lat},${origin.lng}`,
    destination: `${destination.lat},${destination.lng}`,
    travelmode: "driving",
  });
  if (waypoints.length > 0) {
    params.set("waypoints", waypoints.map((point) => `${point.lat},${point.lng}`).join("|"));
  }
  return `https://www.google.com/maps/dir/?${params.toString()}`;
}
