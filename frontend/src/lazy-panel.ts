// The fetch-once-per-card guard behind each destination-card detail tab (Calendar, Photos,
// Trails, Campgrounds). The first time a tab is opened its loader fires; a "loading" state
// (not a bare boolean) stops a second click starting a duplicate fetch while the first is
// still in flight, and a failed load resets to "idle" so the tab retries on its next open
// rather than staying permanently blank. See views.ts runDestinations.

/**
 * Wraps a `() => Promise<boolean>` loader (true = loaded, false = failed/retryable) so it runs
 * at most once on success. `open()` is safe to call on every tab click: it no-ops while a load
 * is in flight or already done, and re-arms itself if the load reported failure or threw.
 */
export function createLazyLoader(load: () => Promise<boolean>): { open: () => void } {
  let status: "idle" | "loading" | "loaded" = "idle";
  return {
    open() {
      if (status !== "idle") return;
      status = "loading";
      void (async () => {
        try {
          status = (await load()) ? "loaded" : "idle";
        } catch {
          status = "idle"; // a thrown/rejected load re-arms the tab rather than wedging it
        }
      })();
    },
  };
}
