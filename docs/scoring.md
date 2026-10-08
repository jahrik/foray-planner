# How destinations are ranked

This page explains, precisely, how Foray Planner turns iNaturalist records into a ranked list of
destinations, a "why" sentence, the Active-now list, a trail list and a road trip. The code is in
[`src/foray/scoring/`](../src/foray/scoring).

> **What the score is, and is not.** It is a summary of *past iNaturalist observations* for the
> months and genera you chose, nudged by access and fire. It says where records cluster in time
> and space. It is not a prediction of what is fruiting today, and it makes no identification,
> edibility or safety claim. Observers photograph what is easy to reach, so popular trails and
> parks are over-represented.

Contents: [the three primitives](#the-three-primitives) |
[the score](#the-score) | [adjustments](#adjustments-fire-and-access) |
[season trend](#season-trend-peak-building-past-peak-off) |
[sorts](#sorts) | [Active now](#active-now) | [trails](#trail-relevance) |
[camps](#camps) | [trip planner](#trip-planner) | [caching](#caching) |
[tuning](#changing-a-weight)

---

## The three primitives

Everything is built from the `phenology` table: a count of research-grade observations for every
(region, genus, month). A *region* is an H3 hexagon, see
[architecture.md](architecture.md#regions-and-the-h3-grid).

| Primitive | Definition | Reads as |
|---|---|---|
| `w_pheno` | for one genus in one region: records in your chosen months / that genus's records in all twelve months | "Is it in season here?" 0 to 1 |
| abundance | `log1p(month_count)` | "Does it show up reliably?" Log-scaled so a 5,000-record genus does not drown a 50-record one |
| recency | records in the trailing `recent_weeks` (default 4) for the selected genera | "Is something being found right now?" |

## The score

For each region inside the search radius:

```
base    = sum over genera of  w_pheno x log1p(month_count)
diverse = base x (1 + 0.1 x (n_genera - 1))
raw     = diverse x (1 + log1p(recent_count))
```

- Only genera with at least one record **in your months** count.
- `n_genera` is how many such genera the region has, so a place with many choices in season
  outranks one with a single big genus, by 10% per extra genus.
- `recent_count` multiplies the whole thing, so a region with live activity beats an otherwise
  equal one.
- `score_norm` is `raw` divided by the best region's `raw`, so the top card is always 1.0 and every
  other card is a fraction of it. The bar on each card draws `score_norm`.

**Worked example.** Region A has two genera in your months. Genus 1: 40 of its 100 records fall in
those months (`w_pheno` 0.4). Genus 2: 10 of 20 (0.5). It has 12 records in the last four weeks.

```
genus 1     0.4 x log1p(40) = 1.485
genus 2     0.5 x log1p(10) = 1.199
base                         2.684
diverse     2.684 x 1.1    = 2.953
raw         2.953 x (1 + log1p(12)) = 10.53
```

Region B has a single genus with 100 of its 150 records in season and nothing recent:
`0.667 x log1p(100) = 3.08`. A wins, because it has two genera in season and live activity, even
though B's one genus is individually stronger.

**Selecting genera** restricts every sum to those genera. With none selected the filter is off and
every fungus counts. The Genera pill is therefore the biggest lever on what the list means.

**Which months?** The Months pill defaults to the current month. Choosing several months widens
`month_count`, so a long window flattens the seasonality that makes the score informative.

## Adjustments: fire and access

After the base ranking, two multiplicative adjusters run in this order. Each re-normalizes
`score_norm` and re-sorts. Their constants are at the top of
[`scoring/ranking.py`](../src/foray/scoring/ranking.py).

### Wildfire (`_apply_fire`)

| Condition | Effect |
|---|---|
| An **active** fire perimeter or point within **25 km** of the region centre | score x **0.35** (you cannot forage in an active fire area) |
| A **burn scar** within **30 km**, fire in the last 1 to 2 years, severity low, moderate or unknown, **and the device explicitly selected *Morchella*** | score x **1.6** (scar is year 1 or younger) or x **1.25** (year 2) |

The morel boost is opt-in by design: with "All genera" selected it never applies, because burn
scars help one genus and an unscoped boost would mislead. High-severity scars are excluded as poor
producers. Nearby fires are attached to the region as `fire_nearby` so cards can warn, and the
card shows the incident link rather than asserting a closure.

### Access (`_apply_access`)

"Park, walk, find" is the premise, so reachability nudges the score:

| Condition | Effect |
|---|---|
| A trailhead within **3 km** | score x **1.08** |
| The nearest trailhead **and** the nearest campground are both known to be farther than **15 km** | score x **0.85** |
| Nothing cached within **45 km** | **No change.** `null` means "unknown", not "remote": an unmapped area is never penalized |

Distances are exposed as `trailhead_km`, `camp_km` and `camp_is_free`, which feed the "why"
sentence. This stays a live query rather than a materialized column on purpose: trail and camp
refreshes do not touch observations, so a materialized copy could go stale indefinitely.

## Season trend: peak, building, past-peak, off

`phenology_trend` (`scoring/trend.py`) labels where your months sit in the **top genus's** local
season, from that genus's month-by-month histogram for the region. It is informational and **is
not an input to the score**; the card's "why" sentence is its only consumer.

1. Take the selected month in which the genus is recorded most (`primary`) and compare it with the
   genus's best month of the year.
2. Below **15%** of the best month: `off`.
3. At or above **75%**: `peak`.
4. Between: compare the month after `primary` with the month before it (wrapping December to
   January). More activity ahead than behind: `building`; otherwise `past-peak`.

## Sorts

The Sort pill reorders or replaces the list:

| Sort | Behaviour |
|---|---|
| **Best overall** | The server order above. |
| **Nearest** | Re-sorted client-side by `distance_km` (great-circle from home). |
| **Active now** | Replaces the list with `/api/alerts` (below); months are ignored. |

## Active now

`GET /api/alerts` answers "where have people found these *recently*?" It groups research-grade
observations from the trailing window (`weeks`, default 4) by region and genus, with the count,
the latest date, the iNaturalist link and whether the point is obscured. Regions are ordered by
total recent records. It does no historical averaging and no fire or access adjustment; it adds
the recent-rain readout and fire warnings for each region. Pins use the same window
(`/api/observations/precise?weeks=`), so the map matches the list.

## Trail relevance

`GET /api/trails?sort=relevance` (the Details view's default) ranks trails by how likely they are
to be worth walking, not how close they are. Constants are in
[`scoring/queries.py`](../src/foray/scoring/queries.py).

For a path or route:

```
relevance = length_km
          + 3.0      if it is, or leads to, a named hiking route
          + 2.5 x log1p(target-genus records within 500 m of the line)
```

A trailhead takes its length and route flag from the trails it connects to (the `connects` list
computed at ingest by snapping each trailhead onto trail lines within 35 m), because "Trailhead
(OSM) - 16 mi" says nothing; the trail it opens does.

For a **forest road** (`kind = road`), records along the road dominate and length is damped, since
an old logging road is worth walking because mushrooms fruit along it, not because it is long:

```
relevance = log1p(length_km)  +  6.0 x log1p(records within 500 m)  +  3.0 if a route
          + 2.0 if the road is walk-in
```

*Walk-in* means gated to motor vehicles but not to people (`motor_vehicle=no`, a gate or bollard,
and so on, unless `foot` is denied). It is treated as a positive signal: less picked, easy to
reach on foot. The target-genus term only applies when genera are selected; otherwise it would count
every fungus everywhere. `forage_obs` (records within about 500 m, any genus) is stored on each
trail by `backfill-forage` and shown as the "N nearby" figure; it is a hint, not part of the sort.

Other sorts: `nearest` (distance to the line) and `longest` (the length above). The unnamed OSM
connector stubs under 0.5 km that clutter lists are dropped by `significant_only`, and rows that
share a name collapse to the best one (`distinct_names`). Unnamed trailheads never make the list
(they are still on the map).

## Camps

`GET /api/camps` orders **free first, then nearest**. `free` is `true` only where the source states
no fee (RIDB with no positive fee amount, or an OSM `fee=no` tag); an unpriced site is `null`
(unknown), never assumed free. OSM tent pitches inside a campground fold into it, and an OSM
campground within 150 m with a similar name folds into its RIDB twin. Whether camping is *allowed*
anywhere is not asserted: each row carries its source and a link.

## Trip planner

`plan_route` builds a start-to-destination trip in two phases. It is **straight-line**: legs are
great-circle distances and the "corridor" is a buffer around the great-circle chord, not a road
route.

1. **Destination.** If none is given, rank radially from the start (within the home radius), drop
   anything closer than 50 km, and take the best.
2. **Stops.** Rank regions within `corridor_km` of the line (progress along the line replaces
   distance), attach each region's nearest camp (free first) and nearest trail, drop any without a
   free camp when `require_free_camp`, keep the best `max_stops`, then **sort by progress along the
   line**, not nearest-neighbour. A leg longer than `max_drive_km` skips that one stop (counted in
   `skipped_unreachable`), not every later stop, because a wide corridor can zigzag.

**Waypoints** (the "+ Plan" shortlist) are different: they are the *whole* itinerary, never
dropped for score, camp or leg length, and nothing is auto-filled around them. The corridor widens
to contain every pick, and with no explicit destination the trip runs to the farthest pick.
**Pins** attach a stop to a specific cached campground, trail or parcel: legs are measured to the
pin, and a public-land parcel resolves to its *entrance* seen from the region (the nearest in-parcel
point of a cached forest road or trailhead, else a path, else the nearest boundary point).
Only federal and state land managers can be pinned.

## Caching

Ranked lists are cached in-process for 600 s, keyed on every input and cleared by every phenology
rebuild and every write to trails, camps, land or fire. See
[architecture.md](architecture.md#request-path). The cache is a module-level dict, correct only
because there is one server process.

## Changing a weight

Weights are module constants (`FIRE_*`, `BURN_SCAR_*`, `ACCESS_*`, and the relevance weights),
deliberately conservative and multiplicative so each can be tuned without disturbing the others.
When you change one:

1. Update the constant and this page together.
2. Run `just test tests/scoring` (the ranking tests use fixtures and a hermetic database).
3. Look at real output on the map for a few places before shipping, as the existing weights were:
   compare the top five before and after for a region you know.
4. `rank_cache` clears itself on deploy (it is in-process), so no manual invalidation is needed.

A new adjuster should follow the existing shape: take the ranked list, change `score`, record
whatever the card needs to explain it, re-normalize `score_norm`, re-sort, and treat "no data" as
"leave alone".
