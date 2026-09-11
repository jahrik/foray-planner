# Postgres performance baseline

Issue #333 PR 1. **These are local-dev baselines, not production numbers** - captured against
`docker-compose.yml`'s single-container Postgres (`postgis/postgis:16-3.5`, no cluster tuning,
default resource limits) on a dev machine, with local dev's own data volume (not prod's
~1.9M-observation / ~1.2M-trail scale). They're a starting shape for "does this query use the
index we think it does", not a capacity number - re-run against prod (or a prod-sized snapshot)
before trusting any absolute timing.

## How to reproduce

```
just db
just psql "EXPLAIN (ANALYZE, BUFFERS) <query>"
```

or from a `psql` session against the local `foray` database (never a remote one -
`docs/runbooks/pg-production-tuning.md` covers anything that touches the managed prod cluster).

## Destination ranking (`scoring.ranking._rank_candidates`)

The double `GROUP BY` over `phenology`, scoped to the candidate-cell allowlist (a bounding box
over the home radius / corridor - a few hundred `region_id`s, not the whole table):

```sql
EXPLAIN (ANALYZE, BUFFERS)
WITH tot AS (
    SELECT region_id, taxon_id,
           (sum(center_lat * cnt) / sum(cnt))::double precision AS center_lat,
           (sum(center_lng * cnt) / sum(cnt))::double precision AS center_lng,
           sum(cnt)::bigint AS total_cnt
    FROM phenology
    WHERE region_id = ANY(ARRAY['47_-122', '47_-121'])
    GROUP BY region_id, taxon_id
)
SELECT * FROM tot;
```

Before issue #333's index change, `phenology` carried `(taxon_id, region_id)` - a poor match
for a query that filters `region_id = ANY(...)` first. After the change
(`scoring/regions.py`'s build-and-swap now creates `(region_id, taxon_id) INCLUDE (month,
cnt)`), the plan on a locally-seeded table shows an index-only scan on
`ix_phenology_taxon_region` feeding the `GROUP BY` directly, with `Heap Fetches: 0` after an
`ANALYZE phenology` (the build-and-swap already runs one right after creating the table).

## `trails_near` (`scoring.queries.trails_near`)

`trails_near`, `land_near`, `camps_near`, and `trail_land_units` are Python functions in
`scoring/queries.py` that build their SQL inline (not stored procedures) - the queries below
are the core `ST_DWithin` shape each one runs, pulled from its `sql: LiteralString` literal.

```sql
EXPLAIN (ANALYZE, BUFFERS)
WITH pt AS (SELECT ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography AS g)
SELECT t.id, ST_Distance(t.geom, pt.g) / 1000.0 AS dist_km
FROM trails t, pt
WHERE t.geom IS NOT NULL AND ST_DWithin(t.geom, pt.g, 25000)
ORDER BY t.geom <-> pt.g
LIMIT 20;
```

Expect an index scan on `ix_trails_geom_notnull` (issue #333's partial GiST) feeding the
`ST_DWithin` filter, not a sequential scan - watch for `Rows Removed by Filter` staying small
relative to the geography index's candidate set.

## `land_near` (`scoring.queries.land_near`)

```sql
EXPLAIN (ANALYZE, BUFFERS)
WITH pt AS (SELECT ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography AS g)
SELECT p.id FROM public_land p, pt
WHERE p.geom IS NOT NULL AND ST_DWithin(p.geom, pt.g, 25000);
```

Same shape as `trails_near`, over `ix_public_land_geom`.

## `camps_near` (`scoring.queries.camps_near`)

```sql
EXPLAIN (ANALYZE, BUFFERS)
WITH pt AS (SELECT ST_SetSRID(ST_MakePoint(-122.3, 47.6), 4326)::geography AS g)
SELECT c.id, ST_Distance(c.geom, pt.g) / 1000.0 AS dist_km
FROM campsites c, pt
WHERE c.geom IS NOT NULL AND ST_DWithin(c.geom, pt.g, 25000);
```

`campsites.geom`'s GiST index (not partial - campsites is a much smaller table, so the NULL
overhead a partial index avoids on `observations`/`trails` doesn't pay for itself there; see
the SP-GiST vs GiST A/B note in `docs/runbooks/pg-production-tuning.md`).

## `trail_land_units` (`scoring.queries.trail_land_units`)

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT t.id, land.agency, land.unit
FROM trails t
LEFT JOIN LATERAL (
    SELECT pl.agency, pl.unit FROM public_land pl
    WHERE pl.geom IS NOT NULL
      AND ST_DWithin(pl.geom, ST_SetSRID(ST_MakePoint(t.center_lng, t.center_lat), 4326)::geography, 0)
    ORDER BY ST_Area(pl.geom::geometry)
    LIMIT 1
) land ON true
WHERE t.id = ANY(ARRAY['<a real trail id from local data>']);
```

Point-in-polygon `LATERAL` join against `public_land` - watch for a GiST index scan
(`ix_public_land_geom`) on the land geometry rather than a seq scan.

## What this baseline doesn't cover

`pg_stat_statements`, `track_io_timing`, and `auto_explain`-shaped always-on query logging are
cluster config, not something a local `EXPLAIN` run can substitute for - see
`docs/runbooks/pg-production-tuning.md` for what to enable on the real managed cluster before
trusting a plan captured there.
