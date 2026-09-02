import type L from "leaflet";
import { describe, expect, it, vi } from "vitest";

import { clearLayer, clearLayerList } from "./layer-lifecycle";

// The helpers only ever call `map.removeLayer`, so a one-method stub stands in for the map.
const stubMap = () =>
  ({ removeLayer: vi.fn() }) as unknown as L.Map & { removeLayer: ReturnType<typeof vi.fn> };
const layer = () => ({}) as L.Layer;

describe("clearLayerList", () => {
  it("removes every layer from the map and empties the array in place", () => {
    const map = stubMap();
    const a = layer();
    const b = layer();
    const layers = [a, b];
    clearLayerList(map, layers);
    expect(map.removeLayer.mock.calls).toEqual([[a], [b]]);
    expect(layers).toHaveLength(0);
  });

  it("is a no-op on an already-empty list", () => {
    const map = stubMap();
    const layers: L.Layer[] = [];
    clearLayerList(map, layers);
    expect(map.removeLayer).not.toHaveBeenCalled();
    expect(layers).toHaveLength(0);
  });
});

describe("clearLayer", () => {
  it("removes a present layer and returns null", () => {
    const map = stubMap();
    const only = layer();
    expect(clearLayer(map, only)).toBeNull();
    expect(map.removeLayer).toHaveBeenCalledWith(only);
  });

  it("is a no-op when the layer is already null", () => {
    const map = stubMap();
    expect(clearLayer(map, null)).toBeNull();
    expect(map.removeLayer).not.toHaveBeenCalled();
  });
});
