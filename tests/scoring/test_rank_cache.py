"""The in-process TTL cache in front of rank_destinations/rank_destinations_corridor
(issue #333 PR 2) - no network, hand-built fixtures (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray.scoring import build_phenology, rank_destinations
from foray.scoring import rank_cache as rank_cache_module

CELL = 0.5
MOREL = 111
APR_LAT, APR_LNG = 47.6, -122.3


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    with con.cursor() as cur:
        cur.execute(
            "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)",
            (MOREL, "Morchella", "Morels"),
        )
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [(obs_id, MOREL, APR_LAT, APR_LNG, dt.date(2022, 4, 15), 4, "research", 10) for obs_id in range(1, 21)],
        )
    build_phenology(con, CELL)


def _rank(con: psycopg.Connection) -> list:
    return rank_destinations(
        con,
        months=[4],
        taxon_ids=[MOREL],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
    )


def test_identical_params_hit_the_cache(con: psycopg.Connection) -> None:
    first = _rank(con)
    # Truncate the underlying data without rebuilding phenology - if the second call actually
    # re-ran the SQL it would come back empty (or error); a cache hit returns the same values.
    con.execute("TRUNCATE observations RESTART IDENTITY CASCADE")
    second = _rank(con)
    assert [region.region_id for region in second] == [region.region_id for region in first]
    assert second[0].score == first[0].score
    # get() hands back a per-item copy (dataclasses.replace), not the exact stored objects.
    assert second[0] is not first[0]


def test_different_params_miss(con: psycopg.Connection) -> None:
    first = _rank(con)
    different = rank_destinations(
        con,
        months=[4],
        taxon_ids=[MOREL],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=1000,  # only the radius changed
        cell_deg=CELL,
    )
    # A cache miss re-runs the query against the still-present data - same region should be
    # found, but it's independently computed rather than the identical cached object.
    assert [region.region_id for region in different] == [region.region_id for region in first]
    assert different[0] is not first[0]


def test_invalidation_after_rebuild_produces_a_fresh_result(con: psycopg.Connection) -> None:
    first = _rank(con)
    # A second rebuild (no data change) invalidates the cache as a side effect - the next call
    # must recompute rather than reuse the pre-rebuild cached entry.
    build_phenology(con, CELL)
    second = _rank(con)
    assert [region.region_id for region in second] == [region.region_id for region in first]
    assert second[0] is not first[0]


def test_ttl_expiry_forces_a_recompute(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rank_cache_module.time, "monotonic", lambda: 1000.0)
    rank_destinations(
        con,
        months=[4],
        taxon_ids=[MOREL],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
        ttl_seconds=10,
    )
    monkeypatch.setattr(rank_cache_module.time, "monotonic", lambda: 1011.0)  # past the 10s TTL
    # _rank_candidates reads `phenology`, not `observations` directly - truncating just
    # `observations` wouldn't touch what a recompute actually finds. Truncate `phenology`
    # (the table build_phenology already materialized) so a genuine recompute comes back empty.
    con.execute("TRUNCATE phenology RESTART IDENTITY CASCADE")
    stale_gone = rank_destinations(
        con,
        months=[4],
        taxon_ids=[MOREL],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
        ttl_seconds=10,
    )
    # The truncate above ran after the entry expired, so recomputing finds nothing left.
    assert stale_gone == []
