import { deleteJson, openRefreshStream, postJson } from "./api/client";
import type { LocationResponse } from "./api/types";
import { loadLand } from "./layers";
import { updateHome } from "./map";
import { errorDetail, qs, setStatus } from "./state";
import { refreshCurrentView } from "./view-run";

// Tracks the in-flight refresh's SSE connection + its promise resolver, so cancelRefresh()
// can tear both down immediately instead of waiting for the server to report cancellation.
// At most one refresh is ever tracked here - a new startRefresh() call finishes (as
// cancelled) whatever the previous call was still tracking, so an old EventSource/promise
// can never be leaked or left dangling behind a newer one.
let activeSource: EventSource | null = null;
let activeResolve: ((succeeded: boolean) => void) | null = null;
// Set when cancelRefresh() is called before the SSE connection exists yet (i.e. while
// startRefresh() is still awaiting the initial POST) - checked right after that await so
// startRefresh() short-circuits instead of opening a stream for a refresh already cancelled.
let cancelRequested = false;

function resetRefreshUI(): void {
  qs<HTMLButtonElement>("#refresh").disabled = false;
  qs<HTMLProgressElement>("#refresh-progress").style.display = "none";
}

function finishActive(succeeded: boolean): void {
  if (activeSource) {
    activeSource.close();
    activeSource = null;
  }
  if (activeResolve) {
    const resolve = activeResolve;
    activeResolve = null;
    resolve(succeeded);
  }
}

// Kick off a data refresh and resolve once the server finishes (listens via SSE).
export async function startRefresh(message: string, target: string = "mushrooms"): Promise<boolean> {
  // A new refresh supersedes whatever the previous call was still tracking.
  finishActive(false);
  cancelRequested = false;

  setStatus(message);
  qs<HTMLButtonElement>("#refresh").disabled = true;
  const progress = qs<HTMLProgressElement>("#refresh-progress");
  progress.style.display = "inline-block";
  progress.value = 0;

  let body: { status?: string };
  try {
    body = await postJson("/api/refresh", { params: { query: { target } } });
  } catch (error) {
    setStatus(errorDetail(error) || "refresh failed to start - no connection");
    resetRefreshUI();
    return false;
  }
  if (cancelRequested) {
    // Cancelled while the POST was still in flight - don't open a stream for it.
    resetRefreshUI();
    return false;
  }
  if (body?.status === "already running") {
    setStatus("Another refresh is running, showing progress…");
  }
  return new Promise((resolve) => {
    // Hoisted so the stream callbacks below can call it; by the time an SSE event fires
    // `source` is assigned. The `=== source` guards keep a straggler event from a superseded
    // refresh from tearing down the current one.
    function finish(succeeded: boolean): void {
      if (activeSource === source) activeSource = null;
      if (activeResolve === resolve) activeResolve = null;
      source.close();
      resolve(succeeded);
    }

    const source = openRefreshStream(
      (data) => {
        if (data.step) {
          setStatus(data.step);
        }
        if (data.progress !== undefined) {
          progress.value = data.progress;
        }
        if (data.error) {
          setStatus("Refresh error: " + data.error);
          resetRefreshUI();
          finish(false);
        } else if (data.done) {
          setStatus("Data ready.");
          resetRefreshUI();
          finish(true);
        }
      },
      (raw, error) => {
        console.error("SSE: malformed message", raw, error);
        setStatus("Refresh error: malformed update from server");
        resetRefreshUI();
        finish(false);
      },
    );
    activeSource = source;
    activeResolve = resolve;

    source.onerror = (err) => {
      console.error("SSE Error:", err);
      resetRefreshUI();
      finish(false);
    };
  });
}

// Cancel the in-flight refresh from the client side: tell the server to abort, then
// immediately close the local SSE connection and resolve startRefresh()'s pending promise
// rather than waiting for the server to notice and broadcast a cancellation. If startRefresh()
// hasn't opened its EventSource yet (still awaiting the initial POST), cancelRequested makes
// it short-circuit as soon as that await resolves instead of opening a stream anyway.
export function cancelRefresh(): void {
  cancelRequested = true;
  deleteJson("/api/refresh").catch(() => {
    // best-effort - still tear down the client side below regardless
  });
  finishActive(false);
  resetRefreshUI();
}

export async function setLocation(query: string): Promise<void> {
  setStatus("Finding location…");
  let response: LocationResponse;
  try {
    response = await postJson("/api/location", { body: { query } });
  } catch (error) {
    setStatus(errorDetail(error) || "location not found");
    return;
  }
  updateHome(response.home);
  loadLand();
  refreshCurrentView();
}

// Map clicks (e.g. on a city label on the base tiles) carry only coordinates; the backend
// reverse-geocodes server-side (issue #145) so the location name matches what the user actually
// clicked on, instead of showing raw lat/lng - falls back to the coordinate string there if the
// reverse lookup fails.
export async function setLocationLatLng(lat: number, lng: number): Promise<void> {
  setStatus("Finding location…");
  let response: LocationResponse;
  try {
    response = await postJson("/api/location", { body: { lat, lng } });
  } catch (error) {
    setStatus(errorDetail(error) || "location not found");
    return;
  }
  updateHome(response.home);
  loadLand();
  refreshCurrentView();
}
