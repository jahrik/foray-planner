import { postJson } from "./api/client";
import type { Home } from "./api/types";
import { updateHome } from "./map";
import { errorDetail, qs, setStatus } from "./state";
import { runDestinations } from "./views";

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
    qs<HTMLButtonElement>("#refresh").disabled = false;
    progress.style.display = "none";
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
    qs<HTMLButtonElement>("#refresh").disabled = false;
    progress.style.display = "none";
    return false;
  }
  const body = await started.json();
  if (body?.status === "already running") {
    setStatus("Another refresh is running, showing progress…");
  }
  return new Promise((resolve) => {
    const source = new EventSource("/api/refresh/stream");

    source.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.step) {
        setStatus(data.step);
      }
      if (data.progress !== undefined) {
        progress.value = data.progress;
      }

      if (data.error) {
        setStatus("Refresh error: " + data.error);
        qs<HTMLButtonElement>("#refresh").disabled = false;
        progress.style.display = "none";
        source.close();
        resolve(false);
      } else if (data.done) {
        setStatus("Data ready.");
        qs<HTMLButtonElement>("#refresh").disabled = false;
        progress.style.display = "none";
        source.close();
        resolve(true);
      }
    };

    source.onerror = (err) => {
      console.error("SSE Error:", err);
      source.close();
      // Only un-disable if we haven't already finished.
      qs<HTMLButtonElement>("#refresh").disabled = false;
      progress.style.display = "none";
      resolve(false);
    };
  });
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
  runDestinations();
}
