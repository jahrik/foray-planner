# Runbook: `pg_repack` for `observations` after a bulk import

Issue #333 PR 1. A one-time table rewrite to reclaim bloat and pack rows at the new
`fillfactor = 90` (issue #333's migration 38) - **not** part of the normal deploy or any
automated job. Run manually, once, after a bulk historical import (the kind
`scripts/load_inat_bulk.py`-style loaders do) leaves `observations` with pages at the old
100% fillfactor and update/delete bloat `autovacuum` hasn't caught up on yet.

`fillfactor = 90` only changes how *future* writes lay out pages - it does nothing to rows
already on disk. `pg_repack` (not plain `VACUUM FULL`) rewrites the table online, without the
`ACCESS EXCLUSIVE` lock `VACUUM FULL`/`CLUSTER` would hold for the whole rewrite - unacceptable
on the live table backing production traffic.

## Prerequisites

- `pg_repack` extension available on the target Postgres (DO managed Postgres ships it -
  confirm with `SELECT * FROM pg_available_extensions WHERE name = 'pg_repack';`).
- A maintenance window is not strictly required (`pg_repack` is online), but run it off-peak
  anyway - it still generates a burst of WAL and I/O on a 1-vCPU box.
- `observations` needs a `REPLICA IDENTITY` Postgres can use, or a primary key (`id` already is
  one) - `pg_repack` uses that, not a full-table lock, to track concurrent changes during the
  rewrite.

## Steps

1. Confirm the extension is installed on the target database (one-time, per cluster):

   ```sql
   CREATE EXTENSION IF NOT EXISTS pg_repack;
   ```

2. Check current bloat first, so there's a before/after to compare:

   ```sql
   SELECT pg_size_pretty(pg_total_relation_size('observations'));
   ```

3. From a host with `pg_repack`'s client binary (not just the server extension) and network
   access to the cluster:

   ```sh
   pg_repack --table=observations --no-order --jobs=2 \
       --host=<cluster host> --port=<cluster port> --username=<app role> --dbname=<db>
   ```

   `--no-order` skips a `CLUSTER`-style reorder by an index (there's no single index that
   ordering would meaningfully help here); `--jobs=2` caps parallel workers well under the
   managed plan's backend cap (see `docs/runbooks/pg-production-tuning.md`) so this doesn't
   starve the API pool or the cron jobs running alongside it.

4. Re-check size, and spot-check a few hot queries (`docs/db-performance-baseline.md`'s
   `trails_near`/ranking shapes, run against `observations` where relevant) for a plan change.

5. `ANALYZE observations;` afterward - `pg_repack` doesn't always leave fresh enough stats for
   the planner on its own.

## Rollback

`pg_repack` builds the new table alongside the old one and swaps at the end - a failure mid-run
leaves the original table untouched and just cleans up its own temp objects. No manual rollback
step beyond re-running it.

## When to re-run

Only after another bulk import, or if `pg_stat_user_tables.n_dead_tup` for `observations`
climbs high enough that regular `autovacuum` (tuned per
`docs/runbooks/pg-production-tuning.md`) isn't keeping up - not on a recurring schedule.
