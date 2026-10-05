// Lazy photo for a single precise observation's popup (issue #449). Fetched only when the popup
// opens, remembered per observation (including "no photo") so hovering back and forth costs
// nothing, and any failure just leaves the popup without a picture. Built from DOM nodes - the
// URL and attribution come from an external API.

import { getJson } from "../api/client";
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

/** An empty slot that fills with the observation's photo once (and if) one is found. */
export function thumbnailSlot(obsId: number): HTMLElement {
  const slot = document.createElement("div");
  slot.className = "popup-thumb-slot";
  void fetchThumbnail(obsId).then((thumbnail) => {
    if (thumbnail) slot.append(thumbnailFigure(thumbnail));
  });
  return slot;
}
