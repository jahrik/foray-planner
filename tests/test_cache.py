"""cache.py upsert tests on hand-built fixtures - no network (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt

import psycopg

from foray.cache import search_fungi_genera, upsert_fungi_genera, upsert_observations

_ROW = (
    1,  # id
    111,  # taxon_id
    47.6,  # lat
    -122.3,  # lng
    dt.date(2022, 4, 15),  # observed_on
    4,  # month
    2022,  # year
    "needs_id",  # quality_grade
    10,  # positional_accuracy
    "Seattle, WA",  # place_guess
    "https://inaturalist.org/observations/1",  # uri
    False,  # obscured
)


def _insert(con: psycopg.Connection, row: tuple) -> None:
    upsert_observations(
        con,
        [row],
    )


def test_reupsert_heals_taxon_id_and_quality_grade(con: psycopg.Connection) -> None:
    # First write: wrong taxon_id (e.g. a since-corrected iNat ID) and not-yet-research-grade.
    _insert(con, _ROW)

    reidentified = (*_ROW[:1], 222, *_ROW[2:7], "research", *_ROW[8:])
    _insert(con, reidentified)

    row = con.execute("SELECT taxon_id, quality_grade FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row is not None
    taxon_id, quality_grade = row
    assert taxon_id == 222
    assert quality_grade == "research"


def test_reupsert_preserves_place_guess_when_new_value_is_null(con: psycopg.Connection) -> None:
    _insert(con, _ROW)

    # A later fetch that doesn't carry place_guess shouldn't blank out what's already stored.
    row_without_place_guess = (*_ROW[:9], None, *_ROW[10:])
    _insert(con, row_without_place_guess)

    row = con.execute("SELECT place_guess FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row is not None
    assert row[0] == "Seattle, WA"


def test_reupsert_preserves_taxon_id_and_quality_grade_when_new_value_is_null(con: psycopg.Connection) -> None:
    # A well-formed first write, then a re-upsert from a path that doesn't carry these
    # columns (e.g. a partial bulk loader) - the healed/correct values must survive, not get
    # wiped back to NULL.
    _insert(con, _ROW)

    row_without_taxon_or_grade = (*_ROW[:1], None, *_ROW[2:7], None, *_ROW[8:])
    _insert(con, row_without_taxon_or_grade)

    row = con.execute("SELECT taxon_id, quality_grade FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row is not None
    taxon_id, quality_grade = row
    assert taxon_id == _ROW[1]
    assert quality_grade == _ROW[7]


_GENERA = [
    {"taxon_id": 47348, "name": "Cantharellus", "common_name": "Chanterelles", "observations_count": 90000},
    {"taxon_id": 47165, "name": "Entoloma", "common_name": "Pinkgills", "observations_count": 40000},
    {"taxon_id": 999999, "name": "Obscurella", "common_name": None, "observations_count": 3},
]


def test_search_fungi_genera_matches_scientific_or_common_name(con: psycopg.Connection) -> None:
    upsert_fungi_genera(con, _GENERA)

    by_scientific = search_fungi_genera(con, "cantharell")
    assert [hit["taxon_id"] for hit in by_scientific] == [47348]

    by_common = search_fungi_genera(con, "pinkgill")
    assert [hit["taxon_id"] for hit in by_common] == [47165]


def test_search_fungi_genera_empty_query_ranks_by_observation_count(con: psycopg.Connection) -> None:
    upsert_fungi_genera(con, _GENERA)

    hits = search_fungi_genera(con, "")
    assert [hit["taxon_id"] for hit in hits] == [47348, 47165, 999999]


def test_search_fungi_genera_common_name_is_optional(con: psycopg.Connection) -> None:
    upsert_fungi_genera(con, _GENERA)

    hits = search_fungi_genera(con, "obscurella")
    assert hits == [{"taxon_id": 999999, "name": "Obscurella", "common_name": None}]


def test_upsert_fungi_genera_reupsert_updates_in_place(con: psycopg.Connection) -> None:
    upsert_fungi_genera(con, [{"taxon_id": 1, "name": "Foo", "common_name": None, "observations_count": 1}])
    upsert_fungi_genera(con, [{"taxon_id": 1, "name": "Foo", "common_name": "Foos", "observations_count": 2}])

    row = con.execute("SELECT common_name, observations_count FROM fungi_genera WHERE taxon_id = 1").fetchone()
    assert row == ("Foos", 2)
