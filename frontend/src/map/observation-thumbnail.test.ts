import { beforeEach, describe, expect, it, vi } from "vitest";

const { getJson, postJson } = vi.hoisted(() => ({ getJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../api/client", () => ({ getJson, postJson }));

import { fetchThumbnail, nearestFirst, prefetchThumbnails, thumbnailFigure } from "./observation-thumbnail";

describe("thumbnailFigure", () => {
  it("sets the photo and its attribution as plain text", () => {
    const figure = thumbnailFigure({
      url: "https://inaturalist-open-data.s3.amazonaws.com/photos/1/small.jpg",
      attribution: "(c) <b>someone</b>, CC BY",
      license_code: "cc-by",
    });
    expect(figure.querySelector("img")?.getAttribute("src")).toContain("/small.jpg");
    expect(figure.querySelector("figcaption")?.textContent).toBe("(c) <b>someone</b>, CC BY");
    expect(figure.querySelector("b")).toBeNull();
  });
});

describe("nearestFirst", () => {
  const pins = [
    { id: 1, lat: 47.5, lng: -122.3 },
    { id: 2, lat: 47.01, lng: -122.0 },
    { id: 3, lat: 47.0, lng: -122.0 },
  ];
  it("orders by distance from the centre and applies the limit", () => {
    const nearest = nearestFirst(pins, { lat: 47.0, lng: -122.0 }, 2);
    expect(nearest.map((pin) => pin.id)).toEqual([3, 2]);
  });
  it("does not mutate its input", () => {
    nearestFirst(pins, { lat: 47.0, lng: -122.0 }, 3);
    expect(pins.map((pin) => pin.id)).toEqual([1, 2, 3]);
  });
});

describe("prefetchThumbnails", () => {
  const center = { lat: 47.0, lng: -122.0 };
  const photo = { url: "https://x/small.jpg", attribution: "(c) a", license_code: "cc-by" };

  beforeEach(() => {
    getJson.mockReset();
    postJson.mockReset();
  });

  it("seeds the per-pin memo so opening a popup makes no request", async () => {
    postJson.mockResolvedValue({ thumbnails: { "101": photo, "102": null } });
    await prefetchThumbnails(
      [
        { id: 101, lat: 47.0, lng: -122.0 },
        { id: 102, lat: 47.1, lng: -122.0 },
      ],
      center,
    );
    expect(postJson).toHaveBeenCalledWith("/api/observations/thumbnails", { body: { ids: [101, 102] } });
    expect(await fetchThumbnail(101)).toEqual(photo);
    expect(await fetchThumbnail(102)).toBeNull();
    expect(getJson).not.toHaveBeenCalled();
  });

  it("leaves unresolved ids to the per-pin lookup", async () => {
    postJson.mockResolvedValue({ thumbnails: {} });
    getJson.mockResolvedValue(photo);
    await prefetchThumbnails([{ id: 201, lat: 47.0, lng: -122.0 }], center);
    expect(await fetchThumbnail(201)).toEqual(photo);
    expect(getJson).toHaveBeenCalledTimes(1);
  });

  it("swallows a failed batch", async () => {
    postJson.mockRejectedValue(new Error("offline"));
    await expect(prefetchThumbnails([{ id: 301, lat: 47.0, lng: -122.0 }], center)).resolves.toBeUndefined();
  });

  it("skips ids already known", async () => {
    postJson.mockResolvedValue({ thumbnails: { "401": photo } });
    await prefetchThumbnails([{ id: 401, lat: 47.0, lng: -122.0 }], center); // seeds 401 itself
    postJson.mockClear();
    await prefetchThumbnails([{ id: 401, lat: 47.0, lng: -122.0 }], center);
    expect(postJson).not.toHaveBeenCalled();
  });

  it("sends every pin in batches of 300, nearest first, one batch at a time", async () => {
    let inFlight = 0;
    let overlapped = false;
    postJson.mockImplementation(async (_path: string, init: { body: { ids: number[] } }) => {
      inFlight += 1;
      overlapped ||= inFlight > 1;
      await Promise.resolve();
      inFlight -= 1;
      return { thumbnails: Object.fromEntries(init.body.ids.map((obsId) => [String(obsId), null])) };
    });
    const pins = Array.from({ length: 650 }, (_, index) => ({
      id: 1000 + index,
      lat: 47.0 + index / 1000,
      lng: -122.0,
    }));
    await prefetchThumbnails([...pins].reverse(), center);
    const batches: number[][] = postJson.mock.calls.map((call) => call[1].body.ids);
    expect(batches.map((ids) => ids.length)).toEqual([300, 300, 50]);
    expect(batches.flat()).toEqual(pins.map((pin) => pin.id)); // all of them, nearest first
    expect(overlapped).toBe(false);
  });

  it("stops when the circle on screen changes", async () => {
    postJson.mockImplementation(async (_path: string, init: { body: { ids: number[] } }) => ({
      thumbnails: Object.fromEntries(init.body.ids.map((obsId) => [String(obsId), null])),
    }));
    const pins = Array.from({ length: 650 }, (_, index) => ({
      id: 5000 + index,
      lat: 47.0 + index / 1000,
      lng: -122.0,
    }));
    await prefetchThumbnails(pins, center, () => postJson.mock.calls.length < 1);
    expect(postJson).toHaveBeenCalledTimes(1);
  });

  it("stops after a batch the server could not resolve", async () => {
    postJson.mockResolvedValue({ thumbnails: {} });
    const pins = Array.from({ length: 650 }, (_, index) => ({
      id: 9000 + index,
      lat: 47.0 + index / 1000,
      lng: -122.0,
    }));
    await prefetchThumbnails(pins, center);
    expect(postJson).toHaveBeenCalledTimes(1);
  });
});

describe("prefetchThumbnails across overlapping loops", () => {
  const center = { lat: 47.0, lng: -122.0 };

  it("runs a second circle's batches only after the first one's request settles", async () => {
    let inFlight = 0;
    let overlapped = false;
    postJson.mockImplementation(async (_path: string, init: { body: { ids: number[] } }) => {
      inFlight += 1;
      overlapped ||= inFlight > 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      inFlight -= 1;
      return { thumbnails: Object.fromEntries(init.body.ids.map((obsId) => [String(obsId), null])) };
    });
    const circle = (firstId: number) =>
      Array.from({ length: 400 }, (_, index) => ({
        id: firstId + index,
        lat: 47.0 + index / 1000,
        lng: -122.0,
      }));
    let firstCurrent = true;
    const first = prefetchThumbnails(circle(20000), center, () => firstCurrent);
    firstCurrent = false; // the visitor picks another destination while the first batch is in flight
    const second = prefetchThumbnails(circle(30000), center);
    await Promise.all([first, second]);
    expect(overlapped).toBe(false);
    const sent: number[] = postJson.mock.calls.flatMap((call) => call[1].body.ids);
    expect(sent.filter((obsId) => obsId < 30000)).toEqual([]); // the stale loop never got to send
    expect(sent).toHaveLength(400); // the second loop covered its whole circle
  });
});
