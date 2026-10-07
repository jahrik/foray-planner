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
const PREFETCH_MAX = 300;

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

/** Warm the popup photos for a destination's pins in one batched request, so most are already
 * known when one is opened (the per-pin fetch is a 1+ s round trip to iNat on a cold cache).
 * Resolved ids seed the same per-observation memo `fetchThumbnail` uses; anything the server
 * could not resolve stays a normal on-demand lookup. Best effort - a failure changes nothing. */
export async function prefetchThumbnails(
  pins: { id: number; lat: number; lng: number }[],
  center: { lat: number; lng: number },
): Promise<void> {
  const wanted = nearestFirst(
    pins.filter((pin) => !cache.has(pin.id)),
    center,
    PREFETCH_MAX,
  ).map((pin) => pin.id);
  if (wanted.length === 0) return;
  try {
    const { thumbnails } = await postJson("/api/observations/thumbnails", { body: { ids: wanted } });
    for (const [key, thumbnail] of Object.entries(thumbnails)) {
      const obsId = Number(key);
      if (!cache.has(obsId)) cache.set(obsId, Promise.resolve(thumbnail));
    }
  } catch {
    // Leave the pins to the per-pin lookup.
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
