import { describe, expect, it } from "vitest";

import { directionsHref, directionsLink, GOOGLE_MAPS_WAYPOINT_CAP, googleMapsRouteUrl } from "./directions";

const ANDROID_UA = "Mozilla/5.0 (Linux; Android 14)";
const IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)";
const DESKTOP_UA = "Mozilla/5.0 (X11; Linux x86_64)";

describe("directionsHref", () => {
  it("builds a geo: URI with a pin label, URL-encoded, on Android", () => {
    const href = directionsHref(41.3, -124.02, "Six Rivers Trailhead", ANDROID_UA);
    expect(href).toBe("geo:41.300000,-124.020000?q=41.300000,-124.020000(Six%20Rivers%20Trailhead)");
  });

  it("builds a geo: URI on iOS too", () => {
    const href = directionsHref(41.3, -124.02, "Trailhead", IPHONE_UA);
    expect(href.startsWith("geo:")).toBe(true);
  });

  it("falls back to a Google Maps web destination link on desktop, where geo: has no handler", () => {
    const href = directionsHref(41.3, -124.02, "Trailhead", DESKTOP_UA);
    expect(href).toBe("https://www.google.com/maps/dir/?api=1&destination=41.300000,-124.020000");
  });

  it("encodes characters that would otherwise break the geo: query string", () => {
    const href = directionsHref(0, 0, "A & B (Trail)", ANDROID_UA);
    expect(href).toContain(encodeURIComponent("A & B (Trail)"));
  });
});

describe("directionsLink", () => {
  it("always labels the link 'Directions'", () => {
    expect(directionsLink(0, 0, "Anywhere").text).toBe("Directions");
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
