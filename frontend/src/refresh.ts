import { postJson } from "./api/client";
import type { Home } from "./api/types";
import { loadLand } from "./layers";
import { updateHome } from "./map";
import { errorDetail, qs, setStatus } from "./state";
import { runDestinations } from "./views";

// Tracks the in-flight refresh's SSE connection + its promise resolver, so cancelRefresh()
// can tear both down immediately instead of waiting for the server to report cancellation.
let activeSource: EventSource | null = null;
let activeResolve: ((succeeded: boolean) => void) | null = null;

function resetRefreshUI(): void {
  qs<HTMLButtonElement>("#refresh").disabled = false;
  qs<HTMLProgressElement>("#refresh-progress").style.display = "none";
}

// Kick off a data refresh and resolve once the server finishes (listens via SSE).
export async function startRefresh(message: string, target: string = "mushrooms"): Promise<boolean> {
  setStatus(message);
  qs<HTMLButtonElement>("#refresh").disabled = true;
  const progress = qs<HTMLProgressElement>("#refresh-progress");
  progress.style.display = "inline-block";
  progress.value = 0;

  let started: Response;
  try {
    started = await fetch(`/api/refresh?target=${target}`, { method: "POST" });
  } catch (error) {
    setStatus(errorDetail(error) || "refresh failed to start - no connection");
    resetRefreshUI();
    return false;
  }
  if (!started.ok) {
    let detail = `refresh failed to start (${started.status})`;
    try {
      const body = await started.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // body wasn't JSON; fall back to the status-code message above
    }
    setStatus(detail);
    resetRefreshUI();
    return false;
  }
  const body = await started.json();
  if (body?.status === "already running") {
    setStatus("Another refresh is running, showing progress…");
  }
  return new Promise((resolve) => {
    const source = new EventSource("/api/refresh/stream");
    activeSource = source;
    activeResolve = resolve;

    const finish = (succeeded: boolean) => {
      source.close();
      if (activeSource === source) activeSource = null;
      if (activeResolve === resolve) activeResolve = null;
      resolve(succeeded);
    };

    source.onmessage = (event) => {
      let data: { step?: string; progress?: number; error?: string; done?: boolean };
      try {
        data = JSON.parse(event.data);
      } catch (error) {
        console.error("SSE: malformed message", event.data, error);
        setStatus("Refresh error: malformed update from server");
        resetRefreshUI();
        finish(false);
        return;
      }
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
    };

    source.onerror = (err) => {
      console.error("SSE Error:", err);
      resetRefreshUI();
      finish(false);
    };
  });
}

// Cancel the in-flight refresh from the client side: tell the server to abort, then
// immediately close the local SSE connection and resolve startRefresh()'s pending promise
// rather than waiting for the server to notice and broadcast a cancellation.
export function cancelRefresh(): void {
  fetch("/api/refresh", { method: "DELETE" }).catch(() => {
    // best-effort - still tear down the client side below regardless
  });
  if (activeSource) {
    activeSource.close();
    activeSource = null;
  }
  resetRefreshUI();
  if (activeResolve) {
    const resolve = activeResolve;
    activeResolve = null;
    resolve(false);
  }
}

export async function setLocation(query: string): Promise<void> {
  setStatus("Finding location…");
  let response: { home: Home };
  try {
    response = await postJson<{ home: Home }>("/api/location", { query });
  } catch (error) {
    setStatus(errorDetail(error) || "location not found");
    return;
  }
  updateHome(response.home);
  loadLand();
  runDestinations();
}

// Map clicks (e.g. on a city label on the base tiles) carry only coordinates; reverse-geocode
// so the location name matches what the user actually clicked on, instead of showing raw
// lat/lng. Falls back to whatever name the backend derives if the reverse lookup fails.
export async function setLocationLatLng(lat: number, lng: number): Promise<void> {
  setStatus("Finding location…");
  let name: string | undefined;
  try {
    const params = new URLSearchParams({ lat: String(lat), lon: String(lng), format: "json" });
    const resp = await fetch(`https://nominatim.openstreetmap.org/reverse?${params}`);
    if (resp.ok) {
      const data = await resp.json();
      name = data?.display_name;
    }
  } catch {
    // fall back to the coordinate-based name the backend derives
  }
  let response: { home: Home };
  try {
    response = await postJson<{ home: Home }>("/api/location", { lat, lng, name });
  } catch (error) {
    setStatus(errorDetail(error) || "location not found");
    return;
  }
  updateHome(response.home);
  loadLand();
  runDestinations();
}
