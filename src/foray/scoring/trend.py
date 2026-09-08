"""Per-region phenology trend for the top genus (issue #301 redesign).

The destination cards want a plain-language "why" sentence - "Boletus is at peak
here" reads very differently from "Boletus is past peak here". ``RegionScore``
already carries ``w_pheno`` (the share of a genus's sightings that fall in the
selected months) but not *where in the season* those months sit, so this derives
a coarse ``peak`` / ``building`` / ``past-peak`` / ``off`` label from the genus's
month-by-month observation histogram for that region.

Deliberately coarse - it is an informational cue, not an input to the score.
"""

from __future__ import annotations

# A window month whose count is below this share of the genus's best month reads
# as "not really the season here" regardless of which side of the peak it sits.
OFF_FRACTION = 0.15
# At or above this share of the best month, call it peak rather than a shoulder.
PEAK_FRACTION = 0.75


def phenology_trend(monthly: dict[int, int], months: list[int]) -> str | None:
    """Classify where ``months`` sits in a genus's season for one region.

    ``monthly`` maps calendar month (1-12) -> observation count for the genus in
    that region; ``months`` is the user's selected window. Returns ``None`` when
    there is nothing to classify (no histogram, or no selected months).
    """
    if not monthly or not months:
        return None
    year_max = max(monthly.values())
    if year_max <= 0:
        return None

    # The selected month the genus shows up in most - the window's own peak.
    primary = max(months, key=lambda month: monthly.get(month, 0))
    primary_count = monthly.get(primary, 0)
    if primary_count < OFF_FRACTION * year_max:
        return "off"
    if primary_count >= PEAK_FRACTION * year_max:
        return "peak"

    # A shoulder month: rising into the season vs. falling out of it. Compare the
    # month just after the window's peak with the month just before (calendar
    # wrap-around) - more activity ahead than behind means still building.
    ahead = monthly.get(primary % 12 + 1, 0)
    behind = monthly.get((primary - 2) % 12 + 1, 0)
    return "building" if ahead >= behind else "past-peak"
