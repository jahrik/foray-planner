import { describe, expect, it } from "vitest";

import type { Stop, StopPin, TripPlan } from "../api/types";
import { legPoint, pinKindLabel, routeEnd, stopPoint } from "./plan-points";

function makeStop(regionId: string, overrides: Partial<Stop> = {}): Stop {
  return {
    order: 1,
    region_id: regionId,
    center_lat: 45,
    center_lng: -121,
    score_norm: 1,
    n_species: 1,
    recent_count: 0,
    species: [],
    progress_km: 0,
    drive_km_from_prev: 0,
    cumulative_drive_km: 0,
    camp: null,
    camp_is_free: false,
    trail: null,
    trail_distance_km: null,
    fire_nearby: [],
    ...overrides,
  };
}

function makeTrip(stops: Stop[], overrides: Partial<TripPlan> = {}): TripPlan {
  return {
    start_lat: 44,
    start_lng: -121,
    destination_lat: 48,
    destination_lng: -121,
    destination_name: null,
    auto_destination: false,
    corridor_km: 60,
    months: [10],
    n_stops: stops.length,
    total_drive_km: 0,
    stops,
    skipped_unreachable: 0,
    ...overrides,
  };
}

const PIN: StopPin = {
  kind: "trail",
  id: "osm:node/1",
  name: "Foo TH",
  feature_kind: "trailhead",
  lat: 45.5,
  lng: -121.5,
};
const CAMP = {
  id: "ridb:1",
  name: "Camp",
  kind: "campground",
  fee: null,
  free: null,
  center_lat: 45.1,
  center_lng: -121.1,
  distance_km: 1,
  source: "ridb",
  url: "u",
};

describe("stop points", () => {
  it("prefers the pin, then the camp, then the region centre for the navigable point", () => {
    expect(stopPoint(makeStop("a", { pin: PIN, camp: CAMP }))).toEqual({ lat: 45.5, lng: -121.5 });
    expect(stopPoint(makeStop("a", { camp: CAMP }))).toEqual({ lat: 45.1, lng: -121.1 });
    expect(stopPoint(makeStop("a"))).toEqual({ lat: 45, lng: -121 });
  });

  it("measures legs to the pin or the centre - never the camp", () => {
    expect(legPoint(makeStop("a", { pin: PIN, camp: CAMP }))).toEqual({ lat: 45.5, lng: -121.5 });
    expect(legPoint(makeStop("a", { camp: CAMP }))).toEqual({ lat: 45, lng: -121 });
  });

  it("labels the pinned feature by kind", () => {
    expect(pinKindLabel(PIN)).toBe("trailhead");
    expect(pinKindLabel({ ...PIN, feature_kind: "road" })).toBe("forest road");
    expect(pinKindLabel({ ...PIN, kind: "camp", feature_kind: "dispersed" })).toBe("campground");
  });
});

describe("routeEnd", () => {
  it("ends at the last stop when the server ran the trip to that picked region", () => {
    const trip = makeTrip([makeStop("a"), makeStop("b", { pin: PIN })], {
      auto_destination: true,
      destination_name: "b",
    });
    expect(routeEnd(trip)).toEqual({ point: { lat: 45.5, lng: -121.5 }, lastStopIsDestination: true });
  });

  it("keeps a typed destination as its own end point", () => {
    const trip = makeTrip([makeStop("a")]);
    expect(routeEnd(trip)).toEqual({ point: { lat: 48, lng: -121 }, lastStopIsDestination: false });
  });

  it("keeps an auto-picked destination that isn't the last stop", () => {
    const trip = makeTrip([makeStop("a")], { auto_destination: true, destination_name: "z" });
    expect(routeEnd(trip).lastStopIsDestination).toBe(false);
  });
});
