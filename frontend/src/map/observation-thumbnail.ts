// Lazy photo for a single precise observation's popup (issue #449). Fetched only when the popup
// opens, remembered per observation (including "no photo") so hovering back and forth costs
// nothing, and any failure just leaves the popup without a picture. Built from DOM nodes - the
// URL and attribution come from an external API.

import { getJson, postJson } from "../api/client";
import type { components } from "../api/schema";

type Thumbnail = components["schemas"]["ObservationThumbnail"];

const cache = new Map<number, Promise<Thumbnail | null>>();

export function fetchThumbnail(obsId: number): Promise<Thumbnail | null> {
  let pending = cache.get(obsId);
  if (!pending) {
    pending = getJson("/api/observations/{obs_id}/thumbnail", { path: { obs_id: obsId } }).catch(() => {
      cache.delete(obsId); // transient failure (503, offline): allow a retry on the next open
      return null;
    });
    cache.set(obsId, pending);
  }
  return pending;
}

/** Matches the server's per-request cap (`THUMBNAIL_BATCH_MAX`); a bigger batch is rejected. */
const BATCH_SIZE = 300;

/** The `limit` pins nearest `center`, nearest first - the ones a visitor is most likely to open. */
export function nearestFirst<Pin extends { id: number; lat: number; lng: number }>(
  pins: Pin[],
  center: { lat: number; lng: number },
  limit: number,
): Pin[] {
  const lngScale = Math.cos((center.lat * Math.PI) / 180);
  const distance = (pin: Pin): number =>
    (pin.lat - center.lat) ** 2 + ((pin.lng - center.lng) * lngScale) ** 2;
  return [...pins].sort((first, second) => distance(first) - distance(second)).slice(0, limit);
}

type Thumbnails = Record<string, Thumbnail | null>;

// Batches from every prefetch loop go through this chain, one at a time. Picking a destination
// right after another (or the auto-focus on load) starts a second loop while the first one's
// request is still running, and the server answers a lookup that overlaps another with nothing
// but what is cached - which would look like a failed batch and end the new loop immediately.
let lastBatch: Promise<unknown> = Promise.resolve();

/** POST one batch once the previous one has settled. Resolves null, without sending, when
 * `isCurrent` turned false while it waited its turn. */
function sendBatch(ids: number[], isCurrent: () => boolean): Promise<Thumbnails | null> {
  const request = lastBatch
    .catch(() => undefined)
    .then(async () => {
      if (!isCurrent()) return null;
      const { thumbnails } = await postJson("/api/observations/thumbnails", { body: { ids } });
      return thumbnails;
    });
  lastBatch = request.catch(() => undefined);
  return request;
}

/** Warm the popup photos for every pin in a destination's circle, so they are already known when
 * one is opened (the per-pin fetch is a 1+ s round trip to iNat on a cold cache). Pins go to the
 * server in batches of `BATCH_SIZE`, nearest the centre first and one batch at a time (a
 * destination can hold thousands, and the server only runs one lookup at once), so the pins a
 * visitor is most likely to open are ready first. Resolved ids seed the same per-observation memo
 * `fetchThumbnail` uses; anything the server could not resolve stays a normal on-demand lookup.
 * Best effort: it stops quietly on a failed batch, on a batch that resolved nothing (iNat is
 * unavailable or the server is busy - hammering it would not help), or once `isCurrent` says the
 * circle on screen has changed. */
export async function prefetchThumbnails(
  pins: { id: number; lat: number; lng: number }[],
  center: { lat: number; lng: number },
  isCurrent: () => boolean = () => true,
): Promise<void> {
  const ordered = nearestFirst(pins, center, pins.length).map((pin) => pin.id);
  for (let start = 0; start < ordered.length; start += BATCH_SIZE) {
    if (!isCurrent()) return;
    // Re-filter per batch: a popup opened meanwhile may already have looked its pin up.
    const wanted = ordered.slice(start, start + BATCH_SIZE).filter((obsId) => !cache.has(obsId));
    if (wanted.length === 0) continue;
    let thumbnails: Thumbnails | null;
    try {
      thumbnails = await sendBatch(wanted, isCurrent);
    } catch {
      return; // Leave the rest to the per-pin lookup.
    }
    if (thumbnails === null || Object.keys(thumbnails).length === 0) return;
    for (const [key, thumbnail] of Object.entries(thumbnails)) {
      const obsId = Number(key);
      if (!cache.has(obsId)) cache.set(obsId, Promise.resolve(thumbnail));
    }
  }
}

export function thumbnailFigure(thumbnail: Thumbnail): HTMLElement {
  const figure = document.createElement("figure");
  figure.className = "popup-thumb";
  const image = document.createElement("img");
  image.src = thumbnail.url;
  image.alt = "";
  image.loading = "lazy";
  const credit = document.createElement("figcaption");
  credit.textContent = thumbnail.attribution;
  figure.append(image, credit);
  return figure;
}

/** A slot that fills with the observation's photo once (and if) one is found. While the lookup is
 * pending it already holds the photo's height, so the popup opens at its final size and the map's
 * auto-pan (touch) clears the controls; a find with no photo collapses it. */
export function thumbnailSlot(obsId: number): HTMLElement {
  const slot = document.createElement("div");
  slot.className = "popup-thumb-slot is-pending";
  void fetchThumbnail(obsId).then((thumbnail) => {
    // Append before un-pending so the slot never matches :empty:not(.is-pending) mid-swap.
    if (thumbnail) slot.append(thumbnailFigure(thumbnail));
    slot.classList.remove("is-pending");
  });
  return slot;
}
