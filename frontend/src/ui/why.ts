// The one-sentence plain-language "why" that leads each destination card (issue #301
// redesign). Synthesised entirely from the /api/destinations payload - no extra fetch - so
// the answer-first shortlist stays a single request.

import type { RegionScore } from "../api/types";
import { escapeHtml } from "../format";
import { dist, displayName, rainLabel } from "../state";

// Where the selected months sit in the top genus's local season (backend pheno_trend).
const TREND_PHRASE: Record<string, string> = {
  peak: "is at peak here",
  building: "is coming into season here",
  "past-peak": "is past peak here",
  off: "is barely in season here",
};

// A 7-day total at or above this reads as "it has rained" (~0.2 in); below it, "dry".
const RAINED_MM = 5;

/** HTML for a card's "why" sentence. Empty string when the region has no species (shouldn't
 * happen for a ranked region, but the card builder is defensive). The top genus is
 * ``region.species[0]`` - the API sorts species by in-window count. */
export function whySentence(region: RegionScore): string {
  const top = region.species[0];
  if (!top) return "";

  const genus = `<strong>${escapeHtml(displayName(top))}</strong>`;
  const trend = TREND_PHRASE[region.pheno_trend ?? ""] ?? "is in season here";
  const count = top.month_count;
  const records = `${count} record${count === 1 ? "" : "s"} in your months`;

  let rain = "";
  if (region.precip_recent_7d_mm != null) {
    rain =
      region.precip_recent_7d_mm >= RAINED_MM
        ? ` Rained ${rainLabel(region.precip_recent_7d_mm)} in the last week.`
        : " Dry the last week.";
  }

  let fire = "";
  const fires = region.fire_nearby ?? [];
  const active = fires.find((entry) => entry.status === "active");
  const scar = fires.find((entry) => entry.status === "historical");
  if (active) fire = ` Active fire ${dist(active.distance_km)} away, check access.`;
  else if (scar) fire = ` Burn scar ${dist(scar.distance_km)} away, morel ground.`;

  return `${genus} ${trend}: ${records}.${rain}${fire}`;
}
