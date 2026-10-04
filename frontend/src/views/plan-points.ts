// Where a planned trip's stops actually are (issue #311) - pure helpers shared by the plan
// render, the GPX export and the Google Maps route so all three agree on every point.

import type { Stop, StopPin, TripPlan } from "../api/types";
import type { RoutePoint } from "../map/directions";

/** A stop's navigable point: the feature the user pinned (issue #311), else its camp when one's
 * in range, else the region center - so the GPX export and the Google Maps route always point
 * at the same places. */
export function stopPoint(stop: Stop): RoutePoint {
  if (stop.pin) return { lat: stop.pin.lat, lng: stop.pin.lng };
  return stop.camp
    ? { lat: stop.camp.center_lat, lng: stop.camp.center_lng }
    : { lat: stop.center_lat, lng: stop.center_lng };
}

/** The point the server measured a stop's legs to: its pin, else the region center. */
export function legPoint(stop: Stop): RoutePoint {
  return stop.pin ? { lat: stop.pin.lat, lng: stop.pin.lng } : { lat: stop.center_lat, lng: stop.center_lng };
}

/** Where the trip ends. With picked spots and no typed destination, the server runs the trip to
 * the farthest pick (and a ranking-picked trip to its chosen region) - that region is also the
 * last stop, so it's the destination rather than a second, duplicate point after it (which
 * Google Maps would show as an extra leg). Keyed on the region id alone, not `auto_destination`:
 * a hand-picked farthest stop isn't "auto-picked" but still ends the trip. A typed destination's
 * name is a place name, never an H3 region id, so it can't match. */
export function routeEnd(trip: TripPlan): { point: RoutePoint; lastStopIsDestination: boolean } {
  const last = trip.stops.at(-1);
  if (last && trip.destination_name === last.region_id) {
    return { point: stopPoint(last), lastStopIsDestination: true };
  }
  return { point: { lat: trip.destination_lat, lng: trip.destination_lng }, lastStopIsDestination: false };
}

const PIN_KIND_LABELS: Record<string, string> = {
  trailhead: "trailhead",
  path: "trail",
  route: "hiking route",
  road: "forest road",
};

// How a land parcel's entrance was found (queries._land_entrance) - always "public land", since
// the entrance is a point we derived, not the feature the user picked.
const LAND_ENTRANCE_LABELS: Record<string, string> = {
  road: "public land · where a forest road enters",
  trailhead: "public land · at a trailhead inside",
  path: "public land · where a trail enters",
  edge: "public land · nearest boundary point",
};

export function pinKindLabel(pin: StopPin): string {
  if (pin.kind === "camp") return "campground";
  if (pin.kind === "land") return LAND_ENTRANCE_LABELS[pin.feature_kind] ?? "public land";
  return PIN_KIND_LABELS[pin.feature_kind] ?? "trail";
}

/** Drop a point identical to the one before it. Neighbouring stops can resolve to the same
 * nearest campground, and Google Maps would otherwise route a zero-length leg between them. */
export function withoutRepeats(points: readonly RoutePoint[]): RoutePoint[] {
  return points.filter(
    (point, index) =>
      index === 0 || point.lat !== points[index - 1]!.lat || point.lng !== points[index - 1]!.lng,
  );
}
