// Device geolocation: auto-detects the user's position on load (mushroom hunters travel
// constantly, so the saved home is often stale) and on the search bar's "Use my location" button.
//
// Two passes run side by side, because no single Geolocation API request is both fast and
// accurate:
//   - a coarse `getCurrentPosition` (enableHighAccuracy: false) - a network/Wi-Fi fix that
//     usually lands in a few hundred ms, fast enough to feed the first paint;
//   - a high-accuracy `watchPosition` (enableHighAccuracy: true) - turns on the GPS on a phone,
//     whose cold fix can take 10-30 s+, then converges over successive readings.
// Every reading goes through `isBetterFix`, so a converging GPS only replaces the current fix when
// it is meaningfully tighter (or the user has actually moved). The watch stops once a reading is
// within TARGET_ACCURACY_M, after WATCH_MAX_MS, or when the user sets a location by hand.
// `maximumAge: 0` on both means a fresh fix - never a position the OS cached earlier.
//
// A fix that lands after the user has started interacting must not yank the page out from under
// them (re-centring the map, re-rendering the list over an open Details view): `fixAction` decides
// whether a fix is applied in full, saved quietly, or only offered via the locate button.

import type { Home } from "./api/types";
import { postLocation } from "./location-writes";

export interface Fix {
  lat: number;
  lng: number;
  accuracyM: number;
}

// Stop refining once a reading is this tight - consumer GPS rarely does better under trees.
export const TARGET_ACCURACY_M = 25;
// Give the GPS this long to converge before settling for the best fix so far.
export const WATCH_MAX_MS = 60_000;
// The coarse pass is the fast path - if the network fix takes longer than this, let the GPS
// watch carry on alone.
export const COARSE_TIMEOUT_MS = 8_000;
// A fix within this distance of the current home is saved without re-centring the map or
// re-running the open view: destinations are ~26 km H3 cells and distances display rounded to
// whole km/mi, so a smaller move can't visibly change the result.
export const QUIET_MOVE_KM = 1;
// A fix looser than this (an IP / cell-tower guess - a desktop without GPS or a Wi-Fi location
// service can be 100 km+ off) never replaces a home the user set by hand; it is only offered.
export const TRUSTED_ACCURACY_M = 1_000;
// A replacement reading must shrink the uncertainty by at least this factor to count as better.
const ACCURACY_IMPROVEMENT = 0.75;

const EARTH_RADIUS_KM = 6371.0088;

export function haversineKm(from: { lat: number; lng: number }, to: { lat: number; lng: number }): number {
  const toRad = (degrees: number) => (degrees * Math.PI) / 180;
  const deltaLat = toRad(to.lat - from.lat);
  const deltaLng = toRad(to.lng - from.lng);
  const chord =
    Math.sin(deltaLat / 2) ** 2 +
    Math.cos(toRad(from.lat)) * Math.cos(toRad(to.lat)) * Math.sin(deltaLng / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(chord)));
}

/** Whether `next` should replace `best`: it is the first reading, it is meaningfully more
 * accurate, or it sits outside both readings' uncertainty (the device actually moved). */
export function isBetterFix(best: Fix | null, next: Fix): boolean {
  if (!best) return true;
  if (next.accuracyM < best.accuracyM * ACCURACY_IMPROVEMENT) return true;
  return haversineKm(best, next) * 1000 > best.accuracyM + next.accuracyM;
}

/** What to do with an accepted fix:
 * - `ignore`: a coarse fix whose uncertainty already covers a hand-set home - nothing to say;
 * - `quiet`: within QUIET_MOVE_KM of the current home - save it and update the location line,
 *   but leave the map view and the open panel alone;
 * - `apply`: the user hasn't touched anything yet (or asked for this fix) - save it, re-centre,
 *   and re-run the open view;
 * - `offer`: the user is mid-task and the fix is a real move - don't save it yet, light up the
 *   locate button so one tap applies it. */
export type FixAction = "ignore" | "quiet" | "apply" | "offer";

export function fixAction(
  home: { lat: number; lng: number } | null,
  fix: Fix,
  engaged: boolean,
  homeIsManual = false,
): FixAction {
  if (homeIsManual && fix.accuracyM > TRUSTED_ACCURACY_M) {
    // A hand-set home is more precise than any coarse guess - never overwrite it, at most offer.
    return home && haversineKm(home, fix) * 1000 <= fix.accuracyM ? "ignore" : "offer";
  }
  if (home && haversineKm(home, fix) <= QUIET_MOVE_KM) return "quiet";
  return engaged ? "offer" : "apply";
}

export interface LocatorDeps {
  geolocation: Geolocation;
  /** The current home, read fresh at decision time. */
  currentHome: () => Home | null;
  /** Whether the current home was set by hand (search / map click) rather than a device fix. */
  homeIsManual: () => boolean;
  /** Persist a fix as this device's home (server reverse-geocodes the name). */
  save: (fix: Fix) => Promise<Home>;
  /** Show a saved home: `recenter` re-centres the map and re-runs the open view. */
  show: (home: Home, fix: Fix, recenter: boolean) => void;
  /** A fix is waiting for the user to accept it (null clears the offer). `saved`: the fix is
   * already the saved home (the user started interacting while it was being saved), so accepting
   * only re-centres and refreshes results. */
  offer: (fix: Fix | null, home: Home | null, saved?: boolean) => void;
  status: (text: string) => void;
  setTimer?: (callback: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

/** One locate session's outcome: whether any fix was applied with `recenter` (main()'s
 * first-paint race uses it to skip its own initial render). */
export interface Session {
  /** Resolves on the first fix that was shown with recenter, or false once that can no longer
   * happen (session ended without one, or it was only saved quietly / offered). */
  firstApplied: Promise<boolean>;
}

interface SaveJob {
  fix: Fix;
  recenter: boolean;
  // The user asked for this fix (locate button / accepted offer) - applied even mid-task.
  userRequested: boolean;
  generation: number;
}

export class Locator {
  private engaged = false;
  private best: Fix | null = null;
  private pending: Fix | null = null;
  private watchId: number | null = null;
  private timer: unknown = null;
  private generation = 0;
  private gotFix = false;
  private finished = false;
  // Saves are serialized latest-wins: a converging GPS can produce several better readings a
  // second, and each save is a server round-trip with a (throttled) reverse geocode.
  private saving: Promise<void> = Promise.resolve();
  private queued: SaveJob | null = null;
  private resolveFirst: ((applied: boolean) => void) | null = null;
  private explicit = false;

  constructor(private readonly deps: LocatorDeps) {}

  /** The user has started interacting with the page - later fixes stop re-centring things. */
  markEngaged(): void {
    this.engaged = true;
  }

  /** Begin a locate session. `explicit` (the locate button) applies the first fix even though
   * the user is, by definition, interacting. */
  start({ explicit = false }: { explicit?: boolean } = {}): Session {
    this.stop();
    this.generation += 1;
    const generation = this.generation;
    this.best = null;
    this.gotFix = false;
    this.finished = false;
    this.explicit = explicit;
    // The click / key that started an explicit session marked the page engaged; reset it so a
    // GPS refinement after the first (coarse) fix still applies. Any further input re-marks it.
    if (explicit) this.engaged = false;
    const firstApplied = new Promise<boolean>((resolve) => {
      this.resolveFirst = resolve;
    });
    const geolocation = this.deps.geolocation;
    const onPosition = (position: GeolocationPosition) => {
      // `finished`: clearWatch can't recall callbacks already queued, and the coarse
      // getCurrentPosition can't be cancelled at all - neither may override the settled fix.
      if (generation !== this.generation || this.finished) return;
      this.receive({
        lat: position.coords.latitude,
        lng: position.coords.longitude,
        accuracyM: position.coords.accuracy,
      });
    };
    geolocation.getCurrentPosition(onPosition, (error) => this.onError(generation, error), {
      enableHighAccuracy: false,
      timeout: COARSE_TIMEOUT_MS,
      maximumAge: 0,
    });
    this.watchId = geolocation.watchPosition(onPosition, (error) => this.onError(generation, error), {
      enableHighAccuracy: true,
      maximumAge: 0,
    });
    const setTimer = this.deps.setTimer ?? ((callback, ms) => setTimeout(callback, ms));
    this.timer = setTimer(() => {
      if (generation === this.generation) this.finish();
    }, WATCH_MAX_MS);
    return { firstApplied };
  }

  /** The user set a location by hand (search / map click) - stop, and drop any pending offer,
   * so a GPS reading arriving afterwards can't overwrite their choice. */
  cancel(): void {
    this.generation += 1;
    this.stop();
    this.queued = null;
    this.pending = null;
    this.deps.offer(null, null);
    this.settleFirst(false);
  }

  /** Apply the fix the locate button is offering. Returns false when nothing is pending. */
  acceptPending(): boolean {
    const fix = this.pending;
    if (!fix) return false;
    this.pending = null;
    this.deps.offer(null, null);
    this.enqueueSave(fix, true, true);
    return true;
  }

  get active(): boolean {
    return this.watchId !== null;
  }

  private receive(fix: Fix): void {
    if (!isBetterFix(this.best, fix)) return;
    this.best = fix;
    this.gotFix = true;
    // An explicit request (the locate button) always applies its first fix - the user asked to
    // be centred on it, however close it is to home. After that, a refinement mustn't re-centre
    // a map the user has since panned.
    const userRequested = this.explicit;
    const action: FixAction = userRequested
      ? "apply"
      : fixAction(this.deps.currentHome(), fix, this.engaged, this.deps.homeIsManual());
    this.explicit = false;
    if (action === "offer") {
      this.pending = fix;
      this.deps.offer(fix, this.deps.currentHome());
      this.settleFirst(false);
    } else {
      // A newer, better fix supersedes any older offer (e.g. a GPS fix confirming a hand-set
      // home withdraws the IP guess offered before it) - including one it says to ignore.
      this.withdrawOffer();
      if (action === "ignore") this.settleFirst(false);
      else this.enqueueSave(fix, action === "apply", userRequested);
      if (action === "quiet") this.settleFirst(false);
    }
    if (fix.accuracyM <= TARGET_ACCURACY_M) this.finish();
  }

  private withdrawOffer(): void {
    if (!this.pending) return;
    this.pending = null;
    this.deps.offer(null, null);
  }

  private enqueueSave(fix: Fix, recenter: boolean, userRequested: boolean): void {
    const wasIdle = this.queued === null;
    // Keep a pending recenter even if a quiet refinement replaces it - the user still expects
    // the map to move to the (now more precise) spot.
    this.queued = {
      fix,
      recenter: recenter || (this.queued?.recenter ?? false),
      userRequested: userRequested || (this.queued?.userRequested ?? false),
      generation: this.generation,
    };
    if (!wasIdle) return;
    this.saving = this.saving.then(() => this.runSave());
  }

  private async runSave(): Promise<void> {
    const job = this.queued;
    this.queued = null;
    if (!job || job.generation !== this.generation) return;
    // Engagement is re-checked when the save actually runs (and again once it lands): the
    // decision to re-centre was made when the reading arrived, and the user may have started
    // panning / reading Details since. Not yet persisted -> just offer it.
    const intrudes = () => job.recenter && !job.userRequested && this.engaged;
    if (intrudes()) {
      this.pending = job.fix;
      this.deps.offer(job.fix, this.deps.currentHome());
      this.settleFirst(false);
      return;
    }
    let home: Home;
    try {
      home = await this.deps.save(job.fix);
    } catch {
      if (job.recenter) {
        this.deps.status("couldn't save your location - tap 📍 to try again");
        this.settleFirst(false);
      }
      return; // keep whatever location is already loaded
    }
    if (job.generation !== this.generation) return; // a manual location won the race
    if (intrudes()) {
      // Already saved server-side, so show it - but leave the map and open panel alone and let
      // one tap re-centre and refresh the results.
      this.deps.show(home, job.fix, false);
      this.pending = job.fix;
      this.deps.offer(job.fix, home, true);
      this.settleFirst(false);
      return;
    }
    this.deps.show(home, job.fix, job.recenter);
    if (job.recenter) this.settleFirst(true);
  }

  private onError(generation: number, error: GeolocationPositionError): void {
    if (generation !== this.generation || this.finished) return;
    if (error.code === error.PERMISSION_DENIED) {
      this.deps.status(
        `couldn't detect location (${error.message}) - set it manually via search or map click`,
      );
      this.gotFix = true; // reported - finish() mustn't overwrite it with the generic message
      this.finish();
    }
    // TIMEOUT / POSITION_UNAVAILABLE on one pass: the other may still deliver; finish() reports
    // if neither did.
  }

  private finish(): void {
    if (this.finished) return;
    this.finished = true;
    this.stop();
    if (!this.gotFix) {
      this.deps.status("couldn't detect location - set it manually via search or map click");
    }
    // Any save still in flight is already chained on `saving`; once it lands, nothing else can
    // apply this session (a no-op if it already resolved true).
    void this.saving.then(() => this.settleFirst(false));
  }

  private stop(): void {
    if (this.watchId !== null) {
      this.deps.geolocation.clearWatch(this.watchId);
      this.watchId = null;
    }
    if (this.timer !== null) {
      (this.deps.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>)))(
        this.timer,
      );
      this.timer = null;
    }
  }

  private settleFirst(applied: boolean): void {
    if (!this.resolveFirst) return;
    this.resolveFirst(applied);
    this.resolveFirst = null;
  }
}

/** The default server save: POST the fix; the server reverse-geocodes the place name. */
export async function saveFix(fix: Fix): Promise<Home> {
  const response = await postLocation({ lat: fix.lat, lng: fix.lng });
  return response.home;
}
