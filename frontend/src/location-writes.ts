// The single ordered queue for saving the home location, so a slow write can never overwrite a later one.

import { postJson } from "./api/client";
import type { components } from "./api/schema";
import type { LocationResponse } from "./api/types";

type LocationBody = components["schemas"]["LocationBody"];

// Every `POST /api/location` - device fixes (geolocate.ts), search / map click (refresh.ts), the
// radius presets (main.ts) - goes through this one queue, so writes reach the server in the
// order they were made. Without it a slow device-fix save (the server reverse-geocodes it, which
// can take seconds) could commit *after* a later map click or radius change and silently
// overwrite it server-side - the client's own stale-response guards only protect the UI, not the
// saved row. A queued write still runs after one ahead of it fails.
//
// `body` may be a function, called when the write actually runs: a radius change reads the
// current home then, so it can't re-send coordinates a device fix queued ahead of it replaced.
let tail: Promise<unknown> = Promise.resolve();

export function postLocation(body: LocationBody | (() => LocationBody)): Promise<LocationResponse> {
  const send = () => postJson("/api/location", { body: typeof body === "function" ? body() : body });
  const write = tail.then(send, send);
  tail = write.catch(() => undefined);
  return write;
}
