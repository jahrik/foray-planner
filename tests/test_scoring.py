"""Scoring tests on hand-built fixtures - no network (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray.scoring import (
    alerts,
    build_phenology,
    haversine_km,
    place_calendar,
    rank_destinations,
)

CELL = 0.5

# Two well-separated regions:
#   APR region ~ (47.6, -122.3): morels in April.
#   OCT region ~ (44.0, -121.0): chanterelles in October.
MOREL, CHANTERELLE = 111, 222
APR_LAT, APR_LNG = 47.6, -122.3
OCT_LAT, OCT_LNG = 44.0, -121.0


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)",
            [
                (MOREL, "Morchella", "Morels"),
                (CHANTERELLE, "Cantharellus", "Chanterelles"),
            ],
        )
    rows: list[tuple] = []
    obs_id = 1
    # 20 morel obs in the APR region, all in April.
    for _ in range(20):
        rows.append((obs_id, MOREL, APR_LAT, APR_LNG, dt.date(2022, 4, 15), 4, 2022, "research", 10))
        obs_id += 1
    # 30 chanterelle obs in the OCT region, all in October.
    for _ in range(30):
        rows.append((obs_id, CHANTERELLE, OCT_LAT, OCT_LNG, dt.date(2022, 10, 15), 10, 2022, "research", 10))
        obs_id += 1
    # A couple stray off-season morels in the OCT region (noise).
    for _ in range(2):
        rows.append((obs_id, MOREL, OCT_LAT, OCT_LNG, dt.date(2022, 7, 1), 7, 2022, "research", 10))
        obs_id += 1
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, year,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    build_phenology(con, CELL)


def test_april_ranks_morel_region_first(con: psycopg.Connection) -> None:
    ranked = rank_destinations(
        con,
        months=[4],
        taxon_ids=[MOREL, CHANTERELLE],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
    )
    assert ranked, "expected at least one region"
    top = ranked[0]
    assert abs(top.center_lat - APR_LAT) < CELL
    assert top.species[0].common_name == "Morels"
    assert top.score_norm == 1.0


def test_empty_taxon_ids_means_no_filter_not_no_results(con: psycopg.Connection) -> None:
    # issue #79 Phase 2: a device with zero genus selections must see everything nearby, not
    # nothing - `taxon_ids=[]` has to mean "no restriction", not "match no taxon".
    ranked = rank_destinations(
        con,
        months=[4, 10],
        taxon_ids=[],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
    )
    seen_taxa = {hit.taxon_id for region in ranked for hit in region.species}
    assert seen_taxa == {MOREL, CHANTERELLE}


def test_october_ranks_chanterelle_region_first(con: psycopg.Connection) -> None:
    ranked = rank_destinations(
        con,
        months=[10],
        taxon_ids=[MOREL, CHANTERELLE],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
    )
    top = ranked[0]
    assert abs(top.center_lat - OCT_LAT) < CELL
    assert top.species[0].common_name == "Chanterelles"


def test_radius_filters_far_regions(con: psycopg.Connection) -> None:
    # Home right on the APR region, tiny radius -> OCT region excluded.
    ranked = rank_destinations(
        con,
        months=[4, 10],
        taxon_ids=[MOREL, CHANTERELLE],
        home_lat=APR_LAT,
        home_lng=APR_LNG,
        radius_km=50,
        cell_deg=CELL,
    )
    assert all(abs(region.center_lat - APR_LAT) < CELL for region in ranked)


def test_non_research_grade_excluded_from_scoring(con: psycopg.Connection) -> None:
    # A third taxon with only casual-grade observations should never surface in scoring,
    # even though it has plenty of in-season, in-radius rows - the research-grade filter is
    # enforced centrally (scoring._BINNED), not just at ingest time.
    casual_taxon = 333
    with con.cursor() as cur:
        cur.execute(
            "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)",
            (casual_taxon, "Amanita", "Amanitas"),
        )
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, year,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (9000 + i, casual_taxon, APR_LAT, APR_LNG, dt.date(2022, 4, 15), 4, 2022, "casual", 10)
                for i in range(20)
            ],
        )
    build_phenology(con, CELL)
    ranked = rank_destinations(
        con,
        months=[4],
        taxon_ids=[MOREL, CHANTERELLE, casual_taxon],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
    )
    assert all(hit.taxon_id != casual_taxon for region in ranked for hit in region.species)


def test_place_calendar_peaks_in_expected_month(con: psycopg.Connection) -> None:
    # Find the OCT region id via the regions table.
    row = con.execute("SELECT region_id FROM regions ORDER BY abs(center_lat - %s) LIMIT 1", [OCT_LAT]).fetchone()
    assert row is not None
    region_id = row[0]
    calendar = place_calendar(con, region_id=region_id, taxon_ids=[MOREL, CHANTERELLE])
    peak_month = max(calendar, key=lambda month: calendar[month]["total"])
    assert peak_month == 10
    assert calendar[10]["species"]["Cantharellus (Chanterelles)"] == 30


def test_place_calendar_empty_taxon_ids_means_no_filter(con: psycopg.Connection) -> None:
    row = con.execute("SELECT region_id FROM regions ORDER BY abs(center_lat - %s) LIMIT 1", [OCT_LAT]).fetchone()
    assert row is not None
    region_id = row[0]
    calendar = place_calendar(con, region_id=region_id, taxon_ids=[])
    assert calendar[10]["species"]["Cantharellus (Chanterelles)"] == 30


def test_place_calendar_caps_species_breakdown_when_unfiltered(con: psycopg.Connection) -> None:
    # 20 more distinct genera, all observed in the OCT region in October - well over the cap,
    # simulating an unfiltered device seeing the full ~6,018-genus catalog (issue #79).
    extra_taxa = [(1000 + i, f"Genus{i}", f"Common{i}") for i in range(20)]
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)",
            extra_taxa,
        )
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, year,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (20000 + i, taxon_id, OCT_LAT, OCT_LNG, dt.date(2022, 10, 15), 10, 2022, "research", 10)
                for i, (taxon_id, _, _) in enumerate(extra_taxa)
            ],
        )
    build_phenology(con, CELL)

    row = con.execute("SELECT region_id FROM regions ORDER BY abs(center_lat - %s) LIMIT 1", [OCT_LAT]).fetchone()
    assert row is not None
    region_id = row[0]
    calendar = place_calendar(con, region_id=region_id, taxon_ids=[])

    assert len(calendar[10]["species"]) <= 15
    # total still reflects every matching row, not just the capped breakdown.
    assert calendar[10]["total"] == 30 + 20


def test_place_calendar_disambiguates_duplicate_display_names(con: psycopg.Connection) -> None:
    # The scientific name is the primary label now, so a real collision needs two distinct
    # taxon_ids sharing the same name - a data-quality edge case (the catalog has no
    # uniqueness constraint on `name`), not something that happens in normal operation, but
    # the safety net must still hold rather than silently dropping one genus's counts.
    dup_a, dup_b = 2001, 2002
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)",
            [(dup_a, "Amanitopsis", None), (dup_b, "Amanitopsis", None)],
        )
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, year,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (30001, dup_a, OCT_LAT, OCT_LNG, dt.date(2022, 10, 15), 10, 2022, "research", 10),
                (30002, dup_b, OCT_LAT, OCT_LNG, dt.date(2022, 10, 15), 10, 2022, "research", 10),
            ],
        )
    build_phenology(con, CELL)

    row = con.execute("SELECT region_id FROM regions ORDER BY abs(center_lat - %s) LIMIT 1", [OCT_LAT]).fetchone()
    assert row is not None
    region_id = row[0]
    calendar = place_calendar(con, region_id=region_id, taxon_ids=[dup_a, dup_b])

    assert calendar[10]["species"]["Amanitopsis"] == 1
    disambiguated = [key for key in calendar[10]["species"] if key.startswith("Amanitopsis #")]
    assert len(disambiguated) == 1


def test_alerts_only_recent(con: psycopg.Connection) -> None:
    # Fixture observations are from 2022 -> nothing within the trailing window.
    active = alerts(
        con,
        taxon_ids=[MOREL, CHANTERELLE],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
        weeks=4,
    )
    assert active == []


def test_haversine_known_distance() -> None:
    # Seattle -> Portland is ~233 km.
    distance = haversine_km(47.6062, -122.3321, 45.5152, -122.6784)
    assert 220 < distance < 245
