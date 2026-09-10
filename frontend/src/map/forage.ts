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
// (== the tier-3 break). Rough and tunable; picked from typical PNW/CA counts where a
// well-visited trail sees dozens of records and a backcountry line sees a handful.
const FORAGE_TIER_BREAKS = [12, 40] as const;
export const FORAGE_HI_THRESHOLD = FORAGE_TIER_BREAKS[1];

/** 0 = unknown (forage_obs null), else 1 (few) / 2 (some) / 3 (many) records within ~500 m. */
export function forageTier(obs: number | null | undefined): 0 | 1 | 2 | 3 {
  if (obs == null) return 0;
  if (obs < FORAGE_TIER_BREAKS[0]) return 1;
  if (obs < FORAGE_TIER_BREAKS[1]) return 2;
  return 3;
}
