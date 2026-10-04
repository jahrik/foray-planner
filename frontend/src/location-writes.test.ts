import { beforeEach, describe, expect, it, vi } from "vitest";

const postJson = vi.fn();
vi.mock("./api/client", () => ({ postJson: (...args: unknown[]) => postJson(...args) }));

const { postLocation } = await import("./location-writes");

const home = (name: string) => ({ home: { name, lat: 1, lng: 2, radius_km: 150 } });

beforeEach(() => postJson.mockReset());

describe("postLocation", () => {
  it("sends writes one at a time, in call order", async () => {
    let releaseFirst: (value: unknown) => void = () => undefined;
    postJson.mockImplementationOnce(() => new Promise((resolve) => (releaseFirst = resolve)));
    postJson.mockImplementationOnce(async () => home("second"));
    const first = postLocation({ lat: 1, lng: 2 });
    const second = postLocation({ query: "Coos Bay" });
    await Promise.resolve();
    expect(postJson).toHaveBeenCalledTimes(1); // the second waits for the first to land
    releaseFirst(home("first"));
    await expect(first).resolves.toEqual(home("first"));
    await expect(second).resolves.toEqual(home("second"));
    expect(postJson.mock.calls.map((call) => call[1].body)).toEqual([
      { lat: 1, lng: 2 },
      { query: "Coos Bay" },
    ]);
  });

  it("builds a lazy body only when its turn comes", async () => {
    let releaseFirst: (value: unknown) => void = () => undefined;
    postJson.mockImplementationOnce(() => new Promise((resolve) => (releaseFirst = resolve)));
    postJson.mockImplementationOnce(async () => home("radius"));
    let current = { lat: 1, lng: 2 };
    void postLocation({ lat: 5, lng: 6 });
    const radius = postLocation(() => ({ ...current, radius_km: 50 }));
    await Promise.resolve(); // let the first write start
    current = { lat: 5, lng: 6 }; // the first write's result became home meanwhile
    releaseFirst(home("device"));
    await radius;
    expect(postJson.mock.calls[1]?.[1].body).toEqual({ lat: 5, lng: 6, radius_km: 50 });
  });

  it("still runs a queued write after one ahead of it fails", async () => {
    postJson.mockImplementationOnce(async () => {
      throw new Error("502");
    });
    postJson.mockImplementationOnce(async () => home("after"));
    const failed = postLocation({ lat: 1, lng: 2 });
    const after = postLocation({ lat: 3, lng: 4 });
    await expect(failed).rejects.toThrow("502");
    await expect(after).resolves.toEqual(home("after"));
  });
});
