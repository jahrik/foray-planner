import L from "leaflet";
import { beforeEach, describe, expect, it, vi } from "vitest";

// map.ts pulls in Leaflet (and the markercluster side-effect import) for real map wiring we
// don't exercise here - stub both so we can unit-test the select/deselect fill logic.
class FakeCircle {
  radius: number;
  style: { fillOpacity: number };
  constructor(_latlng: unknown, options: { radius: number; fillOpacity: number }) {
    this.radius = options.radius;
    this.style = { fillOpacity: options.fillOpacity };
  }
  addTo(): this {
    return this;
  }
  setRadius(radius: number): this {
    this.radius = radius;
    return this;
  }
  setStyle(style: { fillOpacity?: number }): this {
    Object.assign(this.style, style);
    return this;
  }
}

vi.mock("leaflet.markercluster", () => ({}));
// Only `circle` needs stubbing (real map wiring we don't exercise here) - CRS.EPSG3857.project
// is pure math with no DOM/network, so keep the real implementation for the satelliteImageUrl/
// satelliteLabelsUrl tests below to project against.
vi.mock("leaflet", async (importOriginal) => {
  const actual = await importOriginal<{ default: typeof L }>();
  return {
    default: {
      ...actual.default,
      circle: (latlng: unknown, options: { radius: number; fillOpacity: number }) =>
        new FakeCircle(latlng, options),
    },
  };
});

const fakeState: { markers: unknown[]; cellDeg: number } = { markers: [], cellDeg: 0.5 };
vi.mock("../state", () => ({
  get state() {
    return fakeState;
  },
  dist: (n: number) => `${n}`,
  qs: () => null,
}));

import { deselectSize, plot, satelliteImageUrl, satelliteLabelsUrl, selectSize } from "./map";

beforeEach(() => {
  fakeState.markers = [];
});

describe("selectSize / deselectSize fill management", () => {
  const fill = (circle: unknown): number => (circle as FakeCircle).style.fillOpacity;

  it("drops every other destination circle to stroke-only on select, restores on deselect", () => {
    const strong = plot(47.6, -122.3, 0.8, false);
    const weak = plot(47.7, -122.4, 0.2, false);

    selectSize(strong);
    expect(fill(strong)).toBeCloseTo(0.08); // the focused circle itself
    expect(fill(weak)).toBe(0); // ring only - no fill to composite into a blob

    deselectSize(strong);
    expect(fill(weak)).toBeCloseTo(0.15 + 0.45 * 0.2); // back to its score-scaled fill
  });

  it("re-dims the rest when selection moves to another circle", () => {
    const a = plot(47.6, -122.3, 0.5, false);
    const b = plot(47.7, -122.4, 0.5, false);

    selectSize(a);
    // card-select's grow(): deselect the old, then select the new
    deselectSize(a);
    selectSize(b);

    expect(fill(b)).toBeCloseTo(0.08);
    expect(fill(a)).toBe(0);
  });

  it("leaves markers it never plotted (plan pins, etc.) untouched", () => {
    const plotted = plot(47.6, -122.3, 0.5, false);
    const foreign = { style: { fillOpacity: 0.9 }, setStyle: vi.fn() };
    fakeState.markers.push(foreign);

    selectSize(plotted);
    expect(foreign.setStyle).not.toHaveBeenCalled();
  });
});

// A bbox in EPSG:3857 meters, matching what Leaflet's own imageOverlay projects `bounds`
// through to position the image - see the comment on satelliteBboxParams in map.ts for why
// requesting anything else (e.g. plain lat/lng degrees) mismatches and distorts the image.
const sw3857 = L.CRS.EPSG3857.project(L.latLng(47.5, -122.4));
const ne3857 = L.CRS.EPSG3857.project(L.latLng(47.6, -122.3));
const expectedBbox = `${sw3857.x},${sw3857.y},${ne3857.x},${ne3857.y}`;

describe("satelliteImageUrl", () => {
  it("requests a square jpg sized to the circle's bounding box, projected into EPSG:3857 meters", () => {
    const bounds = {
      getSouthWest: () => L.latLng(47.5, -122.4),
      getNorthEast: () => L.latLng(47.6, -122.3),
    } as unknown as L.LatLngBounds;

    const url = new URL(satelliteImageUrl(bounds, 640));
    expect(url.origin + url.pathname).toBe(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export",
    );
    expect(url.searchParams.get("bbox")).toBe(expectedBbox);
    expect(url.searchParams.get("bboxSR")).toBe("3857");
    expect(url.searchParams.get("imageSR")).toBe("3857");
    expect(url.searchParams.get("size")).toBe("640,640");
    expect(url.searchParams.get("format")).toBe("jpg");
  });
});

describe("satelliteLabelsUrl", () => {
  it("requests a transparent png from the boundaries/places reference service with the same bbox", () => {
    const bounds = {
      getSouthWest: () => L.latLng(47.5, -122.4),
      getNorthEast: () => L.latLng(47.6, -122.3),
    } as unknown as L.LatLngBounds;

    const url = new URL(satelliteLabelsUrl(bounds, 640));
    expect(url.origin + url.pathname).toBe(
      "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/export",
    );
    expect(url.searchParams.get("bbox")).toBe(expectedBbox);
    expect(url.searchParams.get("format")).toBe("png32");
    expect(url.searchParams.get("transparent")).toBe("true");
  });
});
