// Per-trail foraging density (issue A4c): how many research-grade fungi observations hug a
// trail line (`Trail.forage_obs`, a genus-agnostic count the backend refreshes in rotation).
// More records -> a hotter colour on the selected-trail line + a matching legend entry, and a
// plain count on the Trails-tab row. It tracks foot traffic as much as fungal productivity (a
// roadside nature trail out-counts a remote productive forest road), so it's presented as a
// hint - "records nearby" - never as a score. Pure module, no Leaflet, so it stays unit-testable.

export const FORAGE_RAMP = ["#9a9a9a", "#ffb24d", "#ff4d4d"] as const; // tier 1 / 2 / 3
export const FORAGE_TIER_LABELS = [
  "few records nearby",
  "some records nearby",
  "many records nearby",
] as const;
// forage_obs cutoffs between tier 1|2 and 2|3, and the "give the row a visual lift" threshold
// (== the tier-3 break). Set from the cached counts (2026-10): across counted paths and routes the
// median is 1 record within ~500 m, the 75th percentile 5 and the 90th 25, so 5+ is the busiest
// quarter. The first guess of 12 / 40 sat above the 90th percentile and almost nothing lit up.
const FORAGE_TIER_BREAKS = [2, 5] as const;
export const FORAGE_HI_THRESHOLD = FORAGE_TIER_BREAKS[1];

/** 0 = unknown (forage_obs null), else 1 (few) / 2 (some) / 3 (many) records within ~500 m. */
export function forageTier(obs: number | null | undefined): 0 | 1 | 2 | 3 {
  if (obs == null) return 0;
  if (obs < FORAGE_TIER_BREAKS[0]) return 1;
  if (obs < FORAGE_TIER_BREAKS[1]) return 2;
  return 3;
}

/**
 * The record count a listed row needs to get the list highlight: the top quarter of this list's
 * counts, but never below the absolute floor (`FORAGE_HI_THRESHOLD`). The absolute tiers colour the
 * line; the highlight is for telling rows apart, and in a dense area every row clears the floor, so
 * the floor alone would light them all.
 */
export function highlightCutoff(counts: ReadonlyArray<number | null | undefined>): number {
  const known = counts.filter((count): count is number => count != null && count > 0).sort((a, b) => a - b);
  if (known.length === 0) return FORAGE_HI_THRESHOLD;
  const upperQuarter = known[Math.floor((known.length - 1) * 0.75)] ?? FORAGE_HI_THRESHOLD;
  return Math.max(FORAGE_HI_THRESHOLD, upperQuarter);
}
