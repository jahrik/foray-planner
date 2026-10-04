import { describe, expect, it, vi } from "vitest";

import type { Home } from "./api/types";
import {
  type Fix,
  fixAction,
  haversineKm,
  isBetterFix,
  Locator,
  type LocatorDeps,
  QUIET_MOVE_KM,
  TRUSTED_ACCURACY_M,
  WATCH_MAX_MS,
} from "./geolocate";

const SEATTLE = { lat: 47.6062, lng: -122.3321 };
const TACOMA = { lat: 47.2529, lng: -122.4443 };

describe("haversineKm", () => {
  it("measures Seattle -> Tacoma at ~40 km", () => {
    expect(haversineKm(SEATTLE, TACOMA)).toBeCloseTo(40.2, 0);
  });
  it("is zero for the same point", () => {
    expect(haversineKm(SEATTLE, SEATTLE)).toBe(0);
  });
});

describe("isBetterFix", () => {
  const coarse: Fix = { ...SEATTLE, accuracyM: 2000 };
  it("accepts the first reading", () => {
    expect(isBetterFix(null, coarse)).toBe(true);
  });
  it("accepts a much tighter reading at the same spot", () => {
    expect(isBetterFix(coarse, { ...SEATTLE, accuracyM: 30 })).toBe(true);
  });
  it("rejects a barely-tighter reading inside the old uncertainty", () => {
    expect(isBetterFix(coarse, { lat: SEATTLE.lat + 0.001, lng: SEATTLE.lng, accuracyM: 1800 })).toBe(false);
  });
  it("rejects a looser reading at the same spot", () => {
    expect(isBetterFix({ ...SEATTLE, accuracyM: 20 }, coarse)).toBe(false);
  });
  it("accepts a reading outside both uncertainties (the device moved)", () => {
    expect(isBetterFix({ ...SEATTLE, accuracyM: 20 }, { ...TACOMA, accuracyM: 50 })).toBe(true);
  });
});

describe("fixAction", () => {
  const near: Fix = { lat: SEATTLE.lat + 0.005, lng: SEATTLE.lng, accuracyM: 20 }; // ~0.55 km
  const far: Fix = { ...TACOMA, accuracyM: 20 };
  it("saves a near fix quietly, engaged or not", () => {
    expect(haversineKm(SEATTLE, near)).toBeLessThan(QUIET_MOVE_KM);
    expect(fixAction(SEATTLE, near, false)).toBe("quiet");
    expect(fixAction(SEATTLE, near, true)).toBe("quiet");
  });
  it("applies a far fix before the user has interacted", () => {
    expect(fixAction(SEATTLE, far, false)).toBe("apply");
  });
  it("only offers a far fix once the user is mid-task", () => {
    expect(fixAction(SEATTLE, far, true)).toBe("offer");
  });
  describe("against a hand-set home", () => {
    const COOS_BAY = { lat: 43.3868, lng: -124.2032 };
    const eugeneGuess: Fix = { lat: 43.9419, lng: -122.8472, accuracyM: 25_000 }; // IP-based, ~125 km off
    it("only offers a coarse guess that doesn't cover it, even before interaction", () => {
      expect(eugeneGuess.accuracyM).toBeGreaterThan(TRUSTED_ACCURACY_M);
      expect(fixAction(COOS_BAY, eugeneGuess, false, true)).toBe("offer");
    });
    it("ignores a coarse guess whose uncertainty already covers it", () => {
      expect(
        fixAction(COOS_BAY, { ...COOS_BAY, lat: COOS_BAY.lat + 0.05, accuracyM: 25_000 }, false, true),
      ).toBe("ignore");
    });
    it("still lets a trusted (GPS / Wi-Fi) fix replace it", () => {
      expect(fixAction(COOS_BAY, { ...TACOMA, accuracyM: 30 }, false, true)).toBe("apply");
      expect(fixAction(COOS_BAY, { ...COOS_BAY, accuracyM: 10 }, false, true)).toBe("quiet");
    });
    it("treats the same coarse guess normally when home wasn't set by hand", () => {
      expect(fixAction(COOS_BAY, eugeneGuess, false, false)).toBe("apply");
    });
  });
  it("applies when there's no home yet", () => {
    expect(fixAction(null, far, false)).toBe("apply");
  });
});

// A scriptable stand-in for navigator.geolocation: tests push positions / errors into whichever
// pass (coarse getCurrentPosition, high-accuracy watchPosition) they like.
interface Registration {
  success: PositionCallback;
  error: PositionErrorCallback | null | undefined;
  options: PositionOptions | undefined;
}

class FakeGeolocation {
  coarse: Registration | null = null;
  watch: Registration | null = null;
  cleared: number[] = [];

  getCurrentPosition(
    success: PositionCallback,
    error?: PositionErrorCallback | null,
    options?: PositionOptions,
  ) {
    this.coarse = { success, error, options };
  }
  watchPosition(success: PositionCallback, error?: PositionErrorCallback | null, options?: PositionOptions) {
    this.watch = { success, error, options };
    return 7;
  }
  clearWatch(id: number) {
    this.cleared.push(id);
  }
}

function position(fix: Fix): GeolocationPosition {
  return {
    coords: { latitude: fix.lat, longitude: fix.lng, accuracy: fix.accuracyM },
    timestamp: Date.now(),
  } as GeolocationPosition;
}

function deniedError(): GeolocationPositionError {
  return { code: 1, message: "User denied Geolocation", PERMISSION_DENIED: 1 } as GeolocationPositionError;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function setup(home: Home = { name: "Seattle", ...SEATTLE, radius_km: 150 }, manual = false) {
  const geolocation = new FakeGeolocation();
  let currentHome: Home | null = home;
  const timers: (() => void)[] = [];
  const show = vi.fn((saved: Home) => {
    currentHome = saved;
  });
  const deps: LocatorDeps = {
    geolocation: geolocation as unknown as Geolocation,
    currentHome: () => currentHome,
    homeIsManual: () => manual,
    save: vi.fn(async (fix: Fix) => ({
      name: `${fix.lat},${fix.lng}`,
      lat: fix.lat,
      lng: fix.lng,
      radius_km: 150,
    })),
    show,
    offer: vi.fn(),
    status: vi.fn(),
    setTimer: (callback) => timers.push(callback),
    clearTimer: () => undefined,
  };
  const locator = new Locator(deps);
  return { geolocation, deps, locator, timers, show };
}

describe("Locator", () => {
  it("runs a fast coarse pass and a fresh high-accuracy watch side by side", () => {
    const { geolocation, locator } = setup();
    locator.start();
    expect(geolocation.coarse?.options).toMatchObject({ enableHighAccuracy: false, maximumAge: 0 });
    expect(geolocation.watch?.options).toMatchObject({ enableHighAccuracy: true, maximumAge: 0 });
  });

  it("applies the first far fix with recenter before the user has interacted", async () => {
    const { geolocation, locator, show } = setup();
    const session = locator.start();
    geolocation.coarse?.success(position({ ...TACOMA, accuracyM: 1500 }));
    expect(await session.firstApplied).toBe(true);
    expect(show).toHaveBeenCalledWith(expect.objectContaining({ lat: TACOMA.lat }), expect.anything(), true);
  });

  it("saves a converging GPS refinement quietly and stops at the target accuracy", async () => {
    const { geolocation, locator, show } = setup();
    locator.start();
    geolocation.coarse?.success(position({ ...TACOMA, accuracyM: 1500 }));
    await flush();
    geolocation.watch?.success(position({ lat: TACOMA.lat + 0.002, lng: TACOMA.lng, accuracyM: 10 }));
    await flush();
    expect(show).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({ accuracyM: 10 }),
      false,
    );
    expect(geolocation.cleared).toEqual([7]);
    expect(locator.active).toBe(false);
  });

  it("offers - not applies - a far fix once the user is engaged, and applies it on accept", async () => {
    const { geolocation, deps, locator, show } = setup();
    locator.start();
    locator.markEngaged();
    geolocation.watch?.success(position({ ...TACOMA, accuracyM: 15 }));
    await flush();
    expect(show).not.toHaveBeenCalled();
    expect(deps.save).not.toHaveBeenCalled();
    expect(deps.offer).toHaveBeenLastCalledWith(
      expect.objectContaining({ lat: TACOMA.lat }),
      expect.anything(),
    );
    expect(locator.acceptPending()).toBe(true);
    await flush();
    expect(show).toHaveBeenCalledWith(expect.objectContaining({ lat: TACOMA.lat }), expect.anything(), true);
    expect(locator.acceptPending()).toBe(false);
  });

  it("doesn't let an IP-grade guess on load overwrite a hand-set home, but keeps waiting for GPS", async () => {
    const { geolocation, deps, locator, show } = setup(
      { name: "Coos Bay", lat: 43.3868, lng: -124.2032, radius_km: 150 },
      true,
    );
    const session = locator.start();
    geolocation.coarse?.success(position({ lat: 43.9419, lng: -122.8472, accuracyM: 25_000 }));
    expect(await session.firstApplied).toBe(false);
    expect(deps.save).not.toHaveBeenCalled();
    expect(deps.offer).toHaveBeenLastCalledWith(
      expect.objectContaining({ accuracyM: 25_000 }),
      expect.anything(),
    );
    expect(locator.active).toBe(true);
    // A real GPS fix at the hand-set spot is trusted: saved quietly, and the stale offer withdrawn.
    geolocation.watch?.success(position({ lat: 43.3869, lng: -124.2031, accuracyM: 8 }));
    await flush();
    expect(show).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ accuracyM: 8 }), false);
    expect(deps.offer).toHaveBeenLastCalledWith(null, null);
    expect(locator.acceptPending()).toBe(false);
  });

  it("an explicit request applies its first fix even when engaged and close to home", async () => {
    const { geolocation, locator, show } = setup();
    locator.markEngaged();
    const session = locator.start({ explicit: true });
    geolocation.coarse?.success(position({ lat: SEATTLE.lat + 0.001, lng: SEATTLE.lng, accuracyM: 900 }));
    expect(await session.firstApplied).toBe(true);
    expect(show).toHaveBeenCalledWith(expect.anything(), expect.anything(), true);
  });

  it("cancel() stops the watch and drops a fix that lands afterwards", async () => {
    const { geolocation, deps, locator } = setup();
    const session = locator.start();
    locator.cancel();
    expect(geolocation.cleared).toEqual([7]);
    geolocation.watch?.success(position({ ...TACOMA, accuracyM: 10 }));
    await flush();
    expect(deps.save).not.toHaveBeenCalled();
    expect(await session.firstApplied).toBe(false);
  });

  it("cancel() during an in-flight save keeps the manual location", async () => {
    const { geolocation, deps, locator, show } = setup();
    let release: (home: Home) => void = () => undefined;
    deps.save = vi.fn(() => new Promise<Home>((resolve) => (release = resolve)));
    locator.start();
    geolocation.coarse?.success(position({ ...TACOMA, accuracyM: 1500 }));
    await flush();
    locator.cancel();
    release({ name: "Tacoma", ...TACOMA, radius_km: 150 });
    await flush();
    expect(show).not.toHaveBeenCalled();
  });

  it("reports a denied permission once, without the generic fallback overwriting it", async () => {
    const { geolocation, deps, locator } = setup();
    const session = locator.start();
    geolocation.coarse?.error?.(deniedError());
    geolocation.watch?.error?.(deniedError());
    expect(deps.status).toHaveBeenCalledTimes(1);
    expect(deps.status).toHaveBeenCalledWith(expect.stringContaining("User denied Geolocation"));
    expect(await session.firstApplied).toBe(false);
  });

  it("gives up after the watch window with no fix and says so", async () => {
    const { deps, locator, timers } = setup();
    const session = locator.start();
    expect(timers).toHaveLength(1);
    timers[0]?.();
    expect(deps.status).toHaveBeenCalledWith(expect.stringContaining("couldn't detect location"));
    expect(await session.firstApplied).toBe(false);
    expect(WATCH_MAX_MS).toBeGreaterThanOrEqual(30_000);
  });
});
