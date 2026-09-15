# Local-dev Postgres image (issue #337): postgis/postgis:17-3.5 plus the h3 extension.
#
# DO managed Postgres exposes h3/h3_postgis on its Standard-Edition plan for PG 15-17 (see
# TODO.md's Postgres-performance reference) - prod just runs `CREATE EXTENSION h3` against the
# managed cluster, no image build needed there. The bare postgis/postgis image dev used before
# this doesn't carry h3 at all (`pg_available_extensions` confirmed empty), so dev needs this
# thin layer to match what prod can already do. `postgresql-17-h3` comes from the same
# apt.postgresql.org PGDG repo the base image already trusts (`/etc/apt/sources.list.d/`) - no
# new key or repo to add.
FROM docker.io/postgis/postgis:17-3.5

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-h3 \
    && rm -rf /var/lib/apt/lists/*
