"""cache.py upsert tests on hand-built fixtures - no network (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray.cache import (
    add_genus,
    delete_observations,
    genus_taxon_ids,
    list_selected_genera,
    load_genera,
    mark_revalidated,
    observation_ids_for_genus,
    observation_taxon_ids,
    remove_genus,
    search_fungi_genera,
    stale_observation_ids,
    suspect_genus_taxon_ids,
    upsert_fungi_genera,
    upsert_observations,
)

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


def test_reupsert_refreshes_lat_lng_and_positional_accuracy(con: psycopg.Connection) -> None:
    """A re-fetch (e.g. ingest.revalidate) must be able to correct a since-edited location or
    accuracy on iNat's side - these used to be frozen at whatever the first insert wrote."""
    _insert(con, _ROW)

    corrected = (*_ROW[:2], 48.0, -121.0, *_ROW[4:7], _ROW[7], 5, *_ROW[9:])
    _insert(con, corrected)

    row = con.execute("SELECT lat, lng, positional_accuracy FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row == (48.0, -121.0, 5)


def test_reupsert_preserves_lat_lng_when_new_value_is_null(con: psycopg.Connection) -> None:
    _insert(con, _ROW)

    row_without_coords = (*_ROW[:2], None, None, *_ROW[4:])
    _insert(con, row_without_coords)

    row = con.execute("SELECT lat, lng FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row == (_ROW[2], _ROW[3])


def test_suspect_genus_taxon_ids_flags_cached_count_far_above_live_count(con: psycopg.Connection) -> None:
    upsert_fungi_genera(
        con,
        [
            {"taxon_id": 1, "name": "Olla", "common_name": None, "observations_count": 2},
            {"taxon_id": 2, "name": "Cantharellus", "common_name": None, "observations_count": 90000},
        ],
    )
    # 10 cached rows under a genus iNat says has only 2 observations total - suspect (10 > 3*2).
    for obs_id in range(10):
        _insert(con, (obs_id, 1, *_ROW[2:]))
    # 5 cached rows under a genus iNat says has 90000 - nowhere near suspect.
    for obs_id in range(10, 15):
        _insert(con, (obs_id, 2, *_ROW[2:]))

    assert suspect_genus_taxon_ids(con, ratio=3.0) == [1]


def test_suspect_genus_taxon_ids_flags_zero_live_count(con: psycopg.Connection) -> None:
    upsert_fungi_genera(con, [{"taxon_id": 1, "name": "Ghost", "common_name": None, "observations_count": 0}])
    _insert(con, (1, 1, *_ROW[2:]))

    assert suspect_genus_taxon_ids(con) == [1]


def test_observation_ids_for_genus_returns_matching_ids(con: psycopg.Connection) -> None:
    _insert(con, (1, 111, *_ROW[2:]))
    _insert(con, (2, 111, *_ROW[2:]))
    _insert(con, (3, 222, *_ROW[2:]))

    assert sorted(observation_ids_for_genus(con, 111)) == [1, 2]


def test_delete_observations_removes_rows(con: psycopg.Connection) -> None:
    _insert(con, (1, 111, *_ROW[2:]))
    _insert(con, (2, 111, *_ROW[2:]))

    deleted = delete_observations(con, [1])

    assert deleted == 1
    remaining = con.execute("SELECT id FROM observations").fetchall()
    assert remaining == [(2,)]


def test_stale_observation_ids_prefers_never_checked_then_oldest(con: psycopg.Connection) -> None:
    _insert(con, (1, 111, *_ROW[2:]))
    _insert(con, (2, 111, *_ROW[2:]))
    _insert(con, (3, 111, *_ROW[2:]))
    # id 2 was checked recently; ids 1 and 3 have never been checked (revalidated_at IS NULL) and
    # must sort first (NULLS FIRST).
    mark_revalidated(con, [2])

    assert sorted(stale_observation_ids(con, limit=2)) == [1, 3]
    assert stale_observation_ids(con, limit=1)[0] in (1, 3)
    assert sorted(stale_observation_ids(con, limit=10)) == [1, 2, 3]


def test_stale_observation_ids_respects_limit(con: psycopg.Connection) -> None:
    for obs_id in range(5):
        _insert(con, (obs_id, 111, *_ROW[2:]))

    assert len(stale_observation_ids(con, limit=2)) == 2


def test_observation_taxon_ids_maps_current_cached_taxon(con: psycopg.Connection) -> None:
    _insert(con, (1, 111, *_ROW[2:]))
    _insert(con, (2, 222, *_ROW[2:]))

    assert observation_taxon_ids(con, [1, 2, 999]) == {1: 111, 2: 222}


def test_mark_revalidated_stamps_timestamp(con: psycopg.Connection) -> None:
    _insert(con, (1, 111, *_ROW[2:]))

    mark_revalidated(con, [1])

    row = con.execute("SELECT revalidated_at FROM observations WHERE id = 1").fetchone()
    assert row is not None
    assert row[0] is not None


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


def test_genus_taxon_ids_maps_full_catalog(con: psycopg.Connection) -> None:
    upsert_fungi_genera(con, _GENERA)

    assert genus_taxon_ids(con) == {
        "Cantharellus": 47348,
        "Entoloma": 47165,
        "Obscurella": 999999,
    }


def test_genus_taxon_ids_rejects_duplicate_names(con: psycopg.Connection) -> None:
    # `name` has no uniqueness constraint - a duplicate must raise, not silently drop one
    # of the two taxon_ids from the map.
    upsert_fungi_genera(
        con,
        [
            {"taxon_id": 1, "name": "Amanita", "common_name": None, "observations_count": 1},
            {"taxon_id": 2, "name": "Amanita", "common_name": None, "observations_count": 1},
        ],
    )

    with pytest.raises(ValueError, match="duplicate name"):
        genus_taxon_ids(con)


def test_load_genera_empty_for_fresh_device(con: psycopg.Connection) -> None:
    assert load_genera(con, "device-a") == []


def test_add_and_load_genera_is_scoped_per_device(con: psycopg.Connection) -> None:
    add_genus(con, "device-a", 47348)
    add_genus(con, "device-a", 47165)
    add_genus(con, "device-b", 999999)

    assert sorted(load_genera(con, "device-a")) == [47165, 47348]
    assert load_genera(con, "device-b") == [999999]


def test_add_genus_is_idempotent(con: psycopg.Connection) -> None:
    add_genus(con, "device-a", 47348)
    add_genus(con, "device-a", 47348)

    assert load_genera(con, "device-a") == [47348]


def test_remove_genus(con: psycopg.Connection) -> None:
    add_genus(con, "device-a", 47348)
    add_genus(con, "device-a", 47165)

    remove_genus(con, "device-a", 47348)

    assert load_genera(con, "device-a") == [47165]


def test_list_selected_genera_joins_catalog_names(con: psycopg.Connection) -> None:
    upsert_fungi_genera(
        con,
        [
            {"taxon_id": 47348, "name": "Cantharellus", "common_name": "Chanterelles", "observations_count": 90000},
            {"taxon_id": 999999, "name": "Obscurella", "common_name": None, "observations_count": 3},
        ],
    )
    add_genus(con, "device-a", 47348)
    add_genus(con, "device-a", 999999)

    hits = list_selected_genera(con, "device-a")

    assert hits == [
        {"taxon_id": 47348, "name": "Cantharellus", "common_name": "Chanterelles"},
        {"taxon_id": 999999, "name": "Obscurella", "common_name": None},
    ]
