# Runbook: production Postgres tuning (DO managed cluster)

Issue #333 PR 1. **Everything in this document is a live action against the managed
production DigitalOcean Postgres cluster - none of it is automated, none of it runs from CI or
a cron job, and none of it was executed to write this doc.** It's a checklist for the
maintainer to run by hand, in the DO control panel / `doctl` / a `psql` session against the
real cluster, on their own schedule. No hostnames, IPs, or credentials are recorded here - the
maintainer has those.

The repo-safe parts of #333 PR 1 (migrations, the `scoring/regions.py` index shape, local
`EXPLAIN` baselines, the `pg_repack` runbook) are implemented in the app repo already; see
`docs/db-performance-baseline.md` and `docs/runbooks/pg-repack-observations.md`.

## 1. Cluster-configurable knobs (DO control panel / `doctl databases update` / Terraform
   `database_postgresql_config`)

Check the live value before changing anything - `effective_cache_size` in particular may
already be auto-sized from the plan's RAM.

- `pg_stat_statements` on, `pg_stat_statements.track = all` (or `top`, if `all`'s overhead is
  too high on the 1-vCPU plan - measure both)
- `log_min_duration_statement` - start around `500ms` on a plan this small; DO's
  `auto_explain` equivalent isn't available (no `shared_preload_libraries` control), so this is
  the closest thing to always-on slow-query logging
- `track_io_timing` on - lets `EXPLAIN (ANALYZE, BUFFERS)` show real I/O timing, not just
  buffer counts
- `jit` - test on vs. off. This workload is many small PostGIS queries, not a few huge
  analytical ones; JIT compilation overhead can lose more than it saves at that shape. Compare
  `docs/db-performance-baseline.md`'s query shapes with each setting before deciding
- `max_parallel_workers_per_gather` - `0` on the current 1-vCPU plan (no spare core to
  parallelize into); revisit only for the nightly phenology rebuild if a bigger plan or a
  maintenance-window exception measurably helps there
- `work_mem`, `shared_buffers_percentage` - size relative to the plan's RAM; increase carefully,
  `work_mem` is per-sort/per-hash *per connection*, and the pool concurrency here (API +
  cron) multiplies it fast on a small instance
- `autovacuum_vacuum_scale_factor` / `autovacuum_vacuum_cost_limit` /
  `autovacuum_analyze_scale_factor` - cluster-wide defaults; see step 4 for the per-table
  overrides that matter more here

## 2. Per-role / per-session settings (not on DO's cluster-config surface)

```sql
-- Check the live value first - may already be reasonable for the plan's storage.
SHOW effective_cache_size;

ALTER ROLE <app_role> SET random_page_cost = 1.1;   -- SSD-backed managed storage; 4 is the
                                                      -- spinning-disk-era default and pushes
                                                      -- the planner away from index scans it
                                                      -- should be taking
ALTER ROLE <app_role> SET effective_cache_size = '<measured value>';
ALTER ROLE <app_role> SET effective_io_concurrency = 200;  -- SSD-backed; higher than the
                                                             -- spinning-disk default of 1
```

`maintenance_work_mem` is bumped per-session, not per-role, in the index/vacuum jobs
themselves (e.g. `SET maintenance_work_mem = '512MB'` at the top of a manual `pg_repack` or
reindex session) - a persistent per-role bump would apply it to every connection including
short-lived API queries that don't need it.

## 3. `statement_timeout` on the API role

```sql
ALTER ROLE <api_role> SET statement_timeout = '5s';
```

Protects the shared ~22-backend cap from one runaway query (a bad `ST_Intersects` over an
unindexed geometry, a corridor query with a huge `max-drive-km`) holding a connection open
indefinitely. Cron/migration roles need a longer or no timeout - a bulk `refresh --all` or
`pg_repack` run legitimately takes minutes; don't apply this to those roles.

## 4. Per-table autovacuum tuning

`observations` (resync ~2000 rows/hr) and `trails` (forage backfill ~20k rows/6h) take steady
UPDATE traffic that a cluster-wide `autovacuum_vacuum_scale_factor` (a % of table size) doesn't
suit well once a table is large - a 2% threshold on a multi-million-row table is a lot of dead
tuples before autovacuum fires.

```sql
ALTER TABLE observations SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_cost_limit = 2000
);
ALTER TABLE trails SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_cost_limit = 2000
);
```

Watch `pg_stat_user_tables` (`n_dead_tup`, `last_autovacuum`) for a week after changing this -
tune the cost limit up further if autovacuum still can't keep ahead of the write rate, or back
off if it starts competing too hard with foreground query I/O.

## 5. Drop unused indexes

Needs a live query first - this can't be determined from the app repo alone:

```sql
SELECT schemaname, relname, indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid))
FROM pg_stat_user_indexes
WHERE idx_scan = 0
ORDER BY pg_relation_size(indexrelid) DESC;
```

Cross-check any `idx_scan = 0` hit against how long the cluster has been up since the last
`pg_stat` reset (a young cluster or a recent failover legitimately shows 0 for everything) and
against the app's own known-rare paths (e.g. a filter only the trip planner's corridor mode
uses) before dropping anything.

## 6. SP-GiST vs GiST A/B (pure-point tables)

`observations` and `campsites` are pure `Point` geography, where SP-GiST (a different index
structure, better suited to point data with no polygon/linestring mix) can outperform GiST -
`trails`/`public_land`/`fire_perimeters` stay GiST regardless (SP-GiST doesn't support
`LineString`/`Polygon`/mixed `Geometry` the way this schema's `geom` columns are typed there).

```sql
-- Build alongside the existing GiST index (don't drop it until the comparison is done):
CREATE INDEX CONCURRENTLY ix_observations_geom_spgist ON observations
    USING SPGIST (geom) WHERE geom IS NOT NULL;
```

Compare `EXPLAIN (ANALYZE, BUFFERS)` on the destination-ranking / `trails_near`-style point
queries against both indexes (`SET enable_indexscan = ...` tricks, or drop one temporarily in a
transaction you roll back, to force the planner's choice for the comparison). Keep whichever
wins on the real prod data distribution; drop the loser. This is real experimentation against
live data shape, not something to decide from the app repo.

## 7. DO managed PgBouncer

- Transaction mode for the API pool (`psycopg_pool` in `api/app.py` already uses short-lived
  checkouts per request with no session-level state carried across requests - transaction mode
  is safe there once confirmed: check `api/deps.py`'s pool code doesn't lean on any
  session-scoped `SET` surviving across requests. It doesn't today.)
- Session mode for cron/migration connections - `cache.apply_schema`'s advisory lock
  (`pg_advisory_lock`/`pg_advisory_unlock`) and any `SET work_mem`/`SET maintenance_work_mem`
  for a specific job need a session, not just a transaction, or they silently apply to the
  wrong connection under transaction pooling.
- Once PgBouncer is in front, raise the API pool's `max_size` (`api/app.py`, currently 10,
  sized to the direct 22-backend cluster cap shared with 6 cron jobs) - PgBouncer, not Postgres
  itself, becomes the connection multiplexer, so the ceiling that mattered before moves.
  Revisit alongside moving cron to a night window (#332 PR 2) rather than in isolation.

## 8. PG14 -> PG17 + PostGIS >= 3.4 upgrade

A maintenance-window operation, not a PR - DO's managed upgrade path (snapshot + upgrade +
verify, or a logical-replication cutover for near-zero downtime, depending on what DO offers
for this cluster size). Confirm PostGIS >= 3.4 is available on PG17 in DO's managed offering
before scheduling.

**This gates issue #337 (H3):** `h3`/`h3_postgis` extensions are Standard-Edition, PG 15-17
only (not the current PG 14, not PG 18) - #337 can't start until this upgrade lands.

Checklist for the window:
- [ ] Confirm PostGIS >= 3.4 available on target PG version in DO's managed offering
- [ ] Take a manual snapshot/backup immediately before, independent of DO's automated backups
- [ ] Run the app's full `_MIGRATIONS` chain against a restored copy on the new PG version
      first (the CI job added in `.github/workflows/ci.yml` for issue #332 does this against a
      fresh container - re-run the same idea against a prod-shaped restore, not just fresh)
- [ ] Schedule the window; `www/maintenance.on` (see `docs/deployment.md`) during the cutover
- [ ] Re-verify `docs/db-performance-baseline.md`'s query shapes post-upgrade (planner behavior
      can shift across major versions)
