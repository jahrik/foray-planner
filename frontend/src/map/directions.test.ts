import { describe, expect, it } from "vitest";

import { directionsLink, GOOGLE_MAPS_WAYPOINT_CAP, googleMapsRouteUrl } from "./directions";

describe("directionsLink", () => {
  it("builds a geo: URI with a pin label, URL-encoded", () => {
    const link = directionsLink(41.3, -124.02, "Six Rivers Trailhead");
    expect(link.href).toBe("geo:41.300000,-124.020000?q=41.300000,-124.020000(Six%20Rivers%20Trailhead)");
    expect(link.text).toBe("Directions");
  });

  it("encodes characters that would otherwise break the query string", () => {
    const link = directionsLink(0, 0, "A & B (Trail)");
    expect(link.href).toContain(encodeURIComponent("A & B (Trail)"));
    expect(link.href).not.toContain("&B");
  });
});

describe("googleMapsRouteUrl", () => {
  it("builds an origin/destination/waypoints driving route", () => {
    const url = googleMapsRouteUrl({ lat: 40, lng: -122 }, { lat: 42, lng: -123 }, [
      { lat: 41, lng: -122.5 },
    ]);
    const parsed = new URL(url);
    expect(parsed.origin + parsed.pathname).toBe("https://www.google.com/maps/dir/");
    expect(parsed.searchParams.get("api")).toBe("1");
    expect(parsed.searchParams.get("origin")).toBe("40,-122");
    expect(parsed.searchParams.get("destination")).toBe("42,-123");
    expect(parsed.searchParams.get("waypoints")).toBe("41,-122.5");
    expect(parsed.searchParams.get("travelmode")).toBe("driving");
  });

  it("omits the waypoints param entirely when there are none", () => {
    const url = googleMapsRouteUrl({ lat: 40, lng: -122 }, { lat: 42, lng: -123 }, []);
    expect(new URL(url).searchParams.has("waypoints")).toBe(false);
  });

  it("joins multiple waypoints with a pipe, in order", () => {
    const url = googleMapsRouteUrl({ lat: 0, lng: 0 }, { lat: 1, lng: 1 }, [
      { lat: 0.3, lng: 0.3 },
      { lat: 0.6, lng: 0.6 },
    ]);
    expect(new URL(url).searchParams.get("waypoints")).toBe("0.3,0.3|0.6,0.6");
  });

  it("exports a waypoint cap for callers to check before building the button", () => {
    expect(GOOGLE_MAPS_WAYPOINT_CAP).toBe(9);
  });
});
