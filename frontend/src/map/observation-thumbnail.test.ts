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

  it("asks for at most 300 ids, nearest first", async () => {
    postJson.mockResolvedValue({ thumbnails: {} });
    const pins = Array.from({ length: 350 }, (_, index) => ({
      id: 1000 + index,
      lat: 47.0 + index / 1000,
      lng: -122.0,
    }));
    await prefetchThumbnails(pins.reverse(), center);
    const ids: number[] = postJson.mock.calls[0]![1].body.ids;
    expect(ids).toHaveLength(300);
    expect(ids[0]).toBe(1000);
  });
});
