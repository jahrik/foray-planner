"""Postgres cache: schema, idempotent upserts, and the ingest log.

Split (issue #373) from one 2550-line ``cache.py`` into a package by domain, mirroring how
``foray.sources`` and ``foray.scoring`` are already organized. Every name below is re-exported
unchanged so existing call sites (``from foray import cache; cache.upsert_observations(...)``,
``from foray.cache import connect``) keep working with no changes - only this package's own
internal layout changed, not its public interface.

See the individual modules for what lives where: ``core`` (schema/migrations/connection/generic
upsert primitives), ``genera`` (fungi genus catalog), ``observations`` (observations table CRUD +
per-observation elevation/precip enrichment), ``campsites``, ``land_trails`` (public land + trail
writes, including the trail<->land spatial join), ``fire_cache``, ``ingest_log`` (coverage
tracking + job-run bookkeeping), ``region_cache`` (place-name/satellite-image cache + the
phenology-rebuild trigger), ``backfill_queue``, ``precip_cache`` (the ``precip_daily``/
``precipitation`` layer tables), ``device_prefs`` (per-device location/genera preferences).
"""

from __future__ import annotations

from foray.cache.backfill_queue import (
    backfill_queue_depth,
    dequeue_backfill_batch,
    job_run_drain_rate,
    refresh_backfill_queue,
)
from foray.cache.campsites import (
    prune_campsites_missing_from,
    prune_campsites_outside_bounds,
    prune_campsites_outside_radius,
    upsert_campsites,
)
from foray.cache.core import (
    SCHEMA,
    SCHEMA_VERSION,
    _invalidate_rank_cache,
    _schema_is_current,
    apply_schema,
    connect,
    connection,
    copy_insert_ignore,
    copy_upsert,
    upsert_rows,
)
from foray.cache.device_prefs import (
    add_genus,
    delete_location,
    list_selected_genera,
    load_genera,
    load_location,
    remove_genus,
    save_location,
)
from foray.cache.fire_cache import apply_fire_severity, replace_fire_lane, upsert_fire_perimeters
from foray.cache.genera import genus_taxon_ids, known_genus_taxon_ids, search_fungi_genera, upsert_fungi_genera
from foray.cache.ingest_log import (
    forget_ingest,
    is_area_covered,
    is_ingested,
    latest_ingest_at,
    latest_job_run,
    latest_obs_date,
    latest_obs_date_by_place,
    latest_successful_job_run,
    record_ingest,
    record_job_run,
)
from foray.cache.land_trails import (
    _assign_trail_land,
    backfill_trail_land,
    prune_duplicate_route_paths,
    prune_trails_missing_from,
    upsert_public_land,
    upsert_trails,
)
from foray.cache.observations import (
    delete_observations,
    insert_observations_if_missing,
    mark_revalidated,
    observation_count,
    observation_ids_for_genus,
    observation_taxon_ids,
    observations_missing_elevation,
    observations_missing_precip,
    set_observation_elevations,
    set_observation_precip,
    stale_observation_ids,
    suspect_genus_taxon_ids,
    upsert_observations,
)
from foray.cache.precip_cache import (
    cached_precip,
    region_precip,
    stale_precip_region_ids,
    upsert_precip_days,
    upsert_region_precip,
)
from foray.cache.region_cache import (
    load_region_place,
    load_region_places,
    load_region_satellite,
    maybe_rebuild_phenology,
    save_region_place,
    save_region_satellite,
)

__all__ = [
    "SCHEMA",
    "SCHEMA_VERSION",
    "_assign_trail_land",
    "_invalidate_rank_cache",
    "_schema_is_current",
    "add_genus",
    "apply_fire_severity",
    "apply_schema",
    "backfill_queue_depth",
    "backfill_trail_land",
    "cached_precip",
    "connect",
    "connection",
    "copy_insert_ignore",
    "copy_upsert",
    "delete_location",
    "delete_observations",
    "dequeue_backfill_batch",
    "forget_ingest",
    "genus_taxon_ids",
    "insert_observations_if_missing",
    "is_area_covered",
    "is_ingested",
    "job_run_drain_rate",
    "known_genus_taxon_ids",
    "latest_ingest_at",
    "latest_job_run",
    "latest_obs_date",
    "latest_obs_date_by_place",
    "latest_successful_job_run",
    "list_selected_genera",
    "load_genera",
    "load_location",
    "load_region_place",
    "load_region_places",
    "load_region_satellite",
    "mark_revalidated",
    "maybe_rebuild_phenology",
    "observation_count",
    "observation_ids_for_genus",
    "observation_taxon_ids",
    "observations_missing_elevation",
    "observations_missing_precip",
    "prune_campsites_missing_from",
    "prune_campsites_outside_bounds",
    "prune_campsites_outside_radius",
    "prune_duplicate_route_paths",
    "prune_trails_missing_from",
    "record_ingest",
    "record_job_run",
    "refresh_backfill_queue",
    "region_precip",
    "remove_genus",
    "replace_fire_lane",
    "save_location",
    "save_region_place",
    "save_region_satellite",
    "search_fungi_genera",
    "set_observation_elevations",
    "set_observation_precip",
    "stale_observation_ids",
    "stale_precip_region_ids",
    "suspect_genus_taxon_ids",
    "upsert_campsites",
    "upsert_fire_perimeters",
    "upsert_fungi_genera",
    "upsert_observations",
    "upsert_precip_days",
    "upsert_public_land",
    "upsert_region_precip",
    "upsert_rows",
    "upsert_trails",
]
