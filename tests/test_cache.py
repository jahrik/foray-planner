"""cache.py upsert tests on hand-built fixtures - no network (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray.cache import (
    SCHEMA_VERSION,
    _schema_is_current,
    add_genus,
    apply_schema,
    connection,
    copy_upsert,
    delete_observations,
    genus_taxon_ids,
    is_ingested,
    latest_obs_date,
    list_selected_genera,
    load_genera,
    load_region_place,
    load_region_satellite,
    mark_revalidated,
    observation_ids_for_genus,
    observation_taxon_ids,
    observations_missing_elevation,
    record_ingest,
    remove_genus,
    save_region_place,
    save_region_satellite,
    search_fungi_genera,
    set_observation_elevations,
    stale_observation_ids,
    suspect_genus_taxon_ids,
    upsert_campsites,
    upsert_fungi_genera,
    upsert_observations,
    upsert_rows,
)
from foray.geo import haversine_km

# Seattle, used as the "home" point for latest_obs_date's haversine region-matching tests.
_HOME_LAT, _HOME_LNG = 47.6, -122.3

_ROW = (
    1,  # id
    111,  # taxon_id
    47.6,  # lat
    -122.3,  # lng
    dt.date(2022, 4, 15),  # observed_on
    4,  # month
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

    reidentified = (*_ROW[:1], 222, *_ROW[2:6], "research", *_ROW[7:])
    _insert(con, reidentified)

    row = con.execute("SELECT taxon_id, quality_grade FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row is not None
    taxon_id, quality_grade = row
    assert taxon_id == 222
    assert quality_grade == "research"


def test_reupsert_preserves_place_guess_when_new_value_is_null(con: psycopg.Connection) -> None:
    _insert(con, _ROW)

    # A later fetch that doesn't carry place_guess shouldn't blank out what's already stored.
    row_without_place_guess = (*_ROW[:8], None, *_ROW[9:])
    _insert(con, row_without_place_guess)

    row = con.execute("SELECT place_guess FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row is not None
    assert row[0] == "Seattle, WA"


def test_reupsert_preserves_taxon_id_and_quality_grade_when_new_value_is_null(con: psycopg.Connection) -> None:
    # A well-formed first write, then a re-upsert from a path that doesn't carry these
    # columns (e.g. a partial bulk loader) - the healed/correct values must survive, not get
    # wiped back to NULL.
    _insert(con, _ROW)

    row_without_taxon_or_grade = (*_ROW[:1], None, *_ROW[2:6], None, *_ROW[7:])
    _insert(con, row_without_taxon_or_grade)

    row = con.execute("SELECT taxon_id, quality_grade FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row is not None
    taxon_id, quality_grade = row
    assert taxon_id == _ROW[1]
    assert quality_grade == _ROW[6]


def test_reupsert_refreshes_lat_lng_and_positional_accuracy(con: psycopg.Connection) -> None:
    """A re-fetch (e.g. ingest.revalidate) must be able to correct a since-edited location or
    accuracy on iNat's side - these used to be frozen at whatever the first insert wrote."""
    _insert(con, _ROW)

    corrected = (*_ROW[:2], 48.0, -121.0, *_ROW[4:6], _ROW[6], 5, *_ROW[8:])
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


def test_is_ingested_false_for_unknown_key(con: psycopg.Connection) -> None:
    assert is_ingested(con, "obs:111:47.6:-122.3:150:2015-01-01:2026-07-11") is False


def test_is_ingested_true_after_record_ingest(con: psycopg.Connection) -> None:
    key = "obs:111:47.6:-122.3:150:2015-01-01:2026-07-11"

    record_ingest(con, key, 42, lat=_HOME_LAT, lng=_HOME_LNG, radius_km=150)

    assert is_ingested(con, key) is True


def test_latest_obs_date_none_when_nothing_ingested(con: psycopg.Connection) -> None:
    assert latest_obs_date(con, 111, _HOME_LAT, _HOME_LNG, radius_km=150) is None


def test_latest_obs_date_matches_query_disk_fully_inside_ingested_disk(con: psycopg.Connection) -> None:
    # Previously ingested a 200km disk around home; a query for a smaller 150km disk at the
    # same center is fully covered by it (dist=0, 0 + 150 <= 200), so the cached end-date applies.
    record_ingest(con, "obs:111:47.6:-122.3:200:2015-01-01:2026-06-01", 10, lat=_HOME_LAT, lng=_HOME_LNG, radius_km=200)

    assert latest_obs_date(con, 111, _HOME_LAT, _HOME_LNG, radius_km=150) == "2026-06-01"


def test_latest_obs_date_none_when_query_disk_not_covered(con: psycopg.Connection) -> None:
    # Same ingested disk as above, but the query radius alone already exceeds what was
    # ingested (0 + 250 > 200) - not covered, so there's no usable cached end-date.
    record_ingest(con, "obs:111:47.6:-122.3:200:2015-01-01:2026-06-01", 10, lat=_HOME_LAT, lng=_HOME_LNG, radius_km=200)

    assert latest_obs_date(con, 111, _HOME_LAT, _HOME_LNG, radius_km=250) is None


def test_latest_obs_date_accounts_for_distance_between_centers(con: psycopg.Connection) -> None:
    # A different center (Portland, OR) some real distance from the ingested disk's center -
    # covered only if dist + query_radius <= ingested_radius.
    portland_lat, portland_lng = 45.5, -122.7
    dist = haversine_km(_HOME_LAT, _HOME_LNG, portland_lat, portland_lng)
    record_ingest(
        con,
        "obs:111:47.6:-122.3:400:2015-01-01:2026-06-01",
        10,
        lat=_HOME_LAT,
        lng=_HOME_LNG,
        radius_km=dist + 50,  # covers a 50km disk around Portland, no more
    )

    assert latest_obs_date(con, 111, portland_lat, portland_lng, radius_km=40) == "2026-06-01"
    assert latest_obs_date(con, 111, portland_lat, portland_lng, radius_km=60) is None


def test_latest_obs_date_picks_the_max_end_date_among_covering_disks(con: psycopg.Connection) -> None:
    record_ingest(con, "obs:111:47.6:-122.3:200:2015-01-01:2026-01-01", 10, lat=_HOME_LAT, lng=_HOME_LNG, radius_km=200)
    record_ingest(con, "obs:111:47.6:-122.3:200:2015-01-01:2026-06-01", 10, lat=_HOME_LAT, lng=_HOME_LNG, radius_km=200)

    assert latest_obs_date(con, 111, _HOME_LAT, _HOME_LNG, radius_km=150) == "2026-06-01"


def test_latest_obs_date_ignores_other_tokens(con: psycopg.Connection) -> None:
    # Ingest log for a different taxon token shouldn't leak into this token's lookup.
    record_ingest(con, "obs:222:47.6:-122.3:200:2015-01-01:2026-06-01", 10, lat=_HOME_LAT, lng=_HOME_LNG, radius_km=200)

    assert latest_obs_date(con, 111, _HOME_LAT, _HOME_LNG, radius_km=150) is None


_CAMPSITE_ROW = ("osm:way/1", "Old Name", "reported", None, None, 47.6, -122.3, "osm", "https://example.com/1")


def test_upsert_rows_empty_is_a_noop(con: psycopg.Connection) -> None:
    assert upsert_rows(con, "campsites", ("id", "name"), []) == 0


def test_upsert_rows_refreshes_every_non_conflict_column(con: psycopg.Connection) -> None:
    cols = ("id", "name", "kind", "fee", "free", "lat", "lng", "source", "url")
    upsert_rows(con, "campsites", cols, [("x:1", "Old", "reported", None, None, 1.0, 2.0, "osm", "u")])
    upsert_rows(con, "campsites", cols, [("x:1", "New", "reported", None, None, 3.0, 4.0, "osm", "u")])
    assert con.execute("SELECT name, lat FROM campsites WHERE id = 'x:1'").fetchone() == ("New", 3.0)


def test_upsert_rows_coalesce_preserves_a_healed_value_against_a_null(con: psycopg.Connection) -> None:
    cols = ("id", "name")
    upsert_rows(con, "campsites", cols, [("x:2", "Named")])
    upsert_rows(con, "campsites", cols, [("x:2", None)], coalesce={"name"})
    assert con.execute("SELECT name FROM campsites WHERE id = 'x:2'").fetchone() == ("Named",)


def test_copy_upsert_empty_is_a_noop(con: psycopg.Connection) -> None:
    assert copy_upsert(con, "campsites", ("id", "name"), []) == 0


def test_copy_upsert_dedups_a_repeated_conflict_key_within_one_batch(con: psycopg.Connection) -> None:
    # A paginated source that straddles the same id twice must not trip
    # "ON CONFLICT DO UPDATE command cannot affect row a second time".
    cols = ("id", "name")
    assert copy_upsert(con, "campsites", cols, [("x:1", "First"), ("x:1", "Second")]) == 2
    # One row, and the last occurrence in the batch wins - matches executemany's row-by-row order.
    assert con.execute("SELECT name FROM campsites WHERE id = 'x:1'").fetchall() == [("Second",)]


def test_copy_upsert_fires_the_geom_trigger_on_the_insert_select(con: psycopg.Connection) -> None:
    # The COPY lands in a staging table with no trigger; the INSERT ... SELECT into the real
    # observations table is what fires the BEFORE INSERT trigger that derives geom.
    _insert(con, _ROW)
    row = con.execute("SELECT ST_Y(geom::geometry), ST_X(geom::geometry) FROM observations WHERE id = 1").fetchone()
    assert row == (47.6, -122.3)


def test_connection_passes_through_a_caller_owned_connection(con: psycopg.Connection) -> None:
    with connection(con) as db:
        assert db is con
    assert not con.closed  # context manager must not close a connection it did not open


def test_upsert_campsites_same_key_twice_updates_not_duplicates(con: psycopg.Connection) -> None:
    upsert_campsites(con, [_CAMPSITE_ROW])
    updated = (*_CAMPSITE_ROW[:1], "New Name", *_CAMPSITE_ROW[2:])
    upsert_campsites(con, [updated])

    rows = con.execute("SELECT id, name FROM campsites WHERE id = %s", [_CAMPSITE_ROW[0]]).fetchall()
    assert rows == [("osm:way/1", "New Name")]


def test_load_region_place_not_found_for_unresolved_region(con: psycopg.Connection) -> None:
    assert load_region_place(con, "425_-1099") == (False, None)


def test_save_and_load_region_place_round_trips(con: psycopg.Connection) -> None:
    save_region_place(con, "425_-1099", "Mt. Hood National Forest")

    assert load_region_place(con, "425_-1099") == (True, "Mt. Hood National Forest")


def test_save_region_place_caches_a_negative_result(con: psycopg.Connection) -> None:
    """A `None` place_name means "looked up, nothing notable nearby" - distinct from
    load_region_place's own `(False, None)` for "never looked up"."""
    save_region_place(con, "425_-1099", None)

    assert load_region_place(con, "425_-1099") == (True, None)


def test_save_region_place_keeps_first_result_on_reinsert(con: psycopg.Connection) -> None:
    save_region_place(con, "425_-1099", "Mt. Hood National Forest")
    save_region_place(con, "425_-1099", "Something Else")

    assert load_region_place(con, "425_-1099") == (True, "Mt. Hood National Forest")


def test_load_region_satellite_missing_returns_none(con: psycopg.Connection) -> None:
    assert load_region_satellite(con, "425_-1099") is None


def test_save_and_load_region_satellite_round_trips(con: psycopg.Connection) -> None:
    save_region_satellite(con, "425_-1099", b"\xff\xd8jpeg-bytes", b"\x89PNGlabel-bytes")

    assert load_region_satellite(con, "425_-1099") == (b"\xff\xd8jpeg-bytes", b"\x89PNGlabel-bytes")


def test_save_region_satellite_keeps_first_result_on_reinsert(con: psycopg.Connection) -> None:
    save_region_satellite(con, "425_-1099", b"first-image", b"first-labels")
    save_region_satellite(con, "425_-1099", b"second-image", b"second-labels")

    assert load_region_satellite(con, "425_-1099") == (b"first-image", b"first-labels")


def _obs_row(
    obs_id: int, *, obscured: bool = False, quality_grade: str = "research", lat: float = 47.6, lng: float = -122.3
) -> tuple:
    return (obs_id, 111, lat, lng, dt.date(2022, 4, 15), 4, quality_grade, 10, "Seattle, WA", None, obscured)


def test_observations_missing_elevation_lists_unenriched_precise_rows(con: psycopg.Connection) -> None:
    _insert(con, _obs_row(1))
    _insert(con, _obs_row(2, obscured=True))  # decoy point - skipped
    _insert(con, _obs_row(3))
    set_observation_elevations(con, [(3, 800)])  # already enriched - skipped
    _insert(con, _obs_row(4, quality_grade="needs_id"))  # not research-grade - skipped
    _insert(con, _obs_row(5, lat=999.0, lng=-122.3))  # out-of-range coords - skipped, would wedge the queue

    assert observations_missing_elevation(con, 10) == [(1, 47.6, -122.3)]


def test_observations_missing_elevation_respects_limit(con: psycopg.Connection) -> None:
    for obs_id in (1, 2, 3):
        _insert(con, _obs_row(obs_id))

    assert [row[0] for row in observations_missing_elevation(con, 2)] == [1, 2]


def test_observations_missing_elevation_near_orders_by_distance(con: psycopg.Connection) -> None:
    # `near` makes a Refresh drain the cells on screen first, not the oldest-id rows nationwide.
    _insert(con, _obs_row(1, lat=44.0, lng=-121.0))  # central Oregon - lowest id, ~in box
    _insert(con, _obs_row(2, lat=47.6, lng=-122.3))  # Seattle - on top of `near`
    _insert(con, _obs_row(3, lat=45.5, lng=-122.7))  # Portland - between the two
    _insert(con, _obs_row(4, lat=42.4, lng=-71.1))  # Boston - outside the +-15 deg box

    ordered = [row[0] for row in observations_missing_elevation(con, 10, near=(47.6, -122.3))]

    assert ordered == [2, 3, 1]  # sorted by distance; Boston excluded by the bounding box
    # Without `near` it's still oldest-id-first, no box (the whole-backlog cron path).
    assert [row[0] for row in observations_missing_elevation(con, 10)] == [1, 2, 3, 4]


def test_observations_missing_elevation_near_handles_the_antimeridian(con: psycopg.Connection) -> None:
    # A visitor in the western Aleutians: the +-15 deg box wraps past +-180, so the longitude
    # test has to match both sides of the dateline and the distance sort has to see them as close.
    _insert(con, _obs_row(1, lat=52.0, lng=179.5))  # just west of the dateline, ~on `near`
    _insert(con, _obs_row(2, lat=52.0, lng=-179.0))  # just east of it - ~55 km away, must be found
    _insert(con, _obs_row(3, lat=52.0, lng=170.0))  # ~10 deg west - in box, further
    _insert(con, _obs_row(4, lat=52.0, lng=-71.1))  # far side of the world - excluded

    ordered = [row[0] for row in observations_missing_elevation(con, 10, near=(52.0, 179.5))]

    assert ordered == [1, 2, 3]  # both dateline sides found; wrapped distance keeps 2 ahead of 3


def test_set_observation_elevations_round_trips(con: psycopg.Connection) -> None:
    _insert(con, _obs_row(1))
    _insert(con, _obs_row(2))

    assert set_observation_elevations(con, [(1, 1204), (2, 15)]) == 2
    rows = con.execute("SELECT id, elevation_m FROM observations ORDER BY id").fetchall()
    assert rows == [(1, 1204), (2, 15)]
    assert observations_missing_elevation(con, 10) == []


def test_apply_schema_backfills_a_migration_column_on_a_preexisting_table(con: psycopg.Connection) -> None:
    # Prod's `observations` table predates migration 9, and the API server applies the schema
    # itself (never cache.connect()), so apply_schema - not just the CREATE TABLE baseline -
    # has to run the migration chain (prod Refresh 500: "column elevation_m does not exist").
    con.execute("ALTER TABLE observations DROP COLUMN elevation_m")
    con.execute("DELETE FROM schema_migrations WHERE version = 9")

    apply_schema(con)

    cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'observations'"
        ).fetchall()
    }
    assert "elevation_m" in cols
    assert observations_missing_elevation(con, 10) == []


def test_apply_schema_drops_the_bbox_columns_on_a_preexisting_table(con: psycopg.Connection) -> None:
    # Prod's layer tables predate PR 5 (issue #268) and carry the four bbox columns + the
    # ix_trails_bbox / ix_fire_perimeters_bbox btrees; migrations 23-27 remove them. Re-add and
    # replay to prove the drop lands (and is idempotent - the migration statements say IF EXISTS).
    add_bbox = (
        "ADD COLUMN min_lat double precision, ADD COLUMN min_lng double precision, "
        "ADD COLUMN max_lat double precision, ADD COLUMN max_lng double precision"
    )
    con.execute("ALTER TABLE trails " + add_bbox)
    con.execute("ALTER TABLE public_land " + add_bbox)
    con.execute("ALTER TABLE fire_perimeters " + add_bbox)
    con.execute("CREATE INDEX ix_trails_bbox ON trails (min_lat, max_lat, min_lng, max_lng)")
    con.execute("CREATE INDEX ix_fire_perimeters_bbox ON fire_perimeters (min_lat, max_lat, min_lng, max_lng)")
    con.execute("DELETE FROM schema_migrations WHERE version = ANY(%s)", [[23, 24, 25, 26, 27]])

    apply_schema(con)

    for table in ("trails", "public_land", "fire_perimeters"):
        cols = {
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s", [table]
            ).fetchall()
        }
        assert not ({"min_lat", "min_lng", "max_lat", "max_lng"} & cols), table
    assert con.execute("SELECT to_regclass('ix_trails_bbox')").fetchone() == (None,)
    assert con.execute("SELECT to_regclass('ix_fire_perimeters_bbox')").fetchone() == (None,)


def test_schema_is_current_on_a_freshly_bootstrapped_db(con: psycopg.Connection) -> None:
    # The session fixture's `cache.connect()` already ran the full apply_schema - the fast-path
    # guard should now report everything current so later connect()s / cron ticks skip it.
    assert _schema_is_current(con) is True


def test_schema_is_current_false_when_the_schema_version_is_behind(con: psycopg.Connection) -> None:
    con.execute("UPDATE meta SET value = %s WHERE key = 'schema_version'", [str(SCHEMA_VERSION - 1)])
    try:
        assert _schema_is_current(con) is False
    finally:
        con.execute("UPDATE meta SET value = %s WHERE key = 'schema_version'", [str(SCHEMA_VERSION)])


def test_point_geom_populated_from_latlng_on_insert(con: psycopg.Connection) -> None:
    _insert(con, _obs_row(1, lat=47.6, lng=-122.3))

    row = con.execute("SELECT ST_Y(geom::geometry), ST_X(geom::geometry) FROM observations WHERE id = 1").fetchone()
    assert row is not None
    assert row == pytest.approx((47.6, -122.3))


def test_point_geom_null_when_coordinates_missing(con: psycopg.Connection) -> None:
    con.execute("INSERT INTO observations (id, taxon_id, quality_grade) VALUES (1, 111, 'research')")

    assert con.execute("SELECT geom FROM observations WHERE id = 1").fetchone() == (None,)


def test_point_geom_refreshed_when_coordinates_change(con: psycopg.Connection) -> None:
    _insert(con, _obs_row(1, lat=40.0, lng=-100.0))
    _insert(con, (*_obs_row(1)[:2], 41.0, -101.0, *_obs_row(1)[4:]))

    row = con.execute("SELECT ST_Y(geom::geometry), ST_X(geom::geometry) FROM observations WHERE id = 1").fetchone()
    assert row == pytest.approx((41.0, -101.0))


def test_layer_geom_populated_from_geojson(con: psycopg.Connection) -> None:
    con.execute(
        "INSERT INTO trails (id, name, geojson) VALUES ('osm:way/1', 'T', %s)",
        ['{"type": "LineString", "coordinates": [[-120.0, 45.0], [-120.1, 45.1]]}'],
    )

    got = con.execute("SELECT ST_GeometryType(geom::geometry) FROM trails WHERE id = 'osm:way/1'").fetchone()
    assert got == ("ST_LineString",)


def test_layer_geom_null_and_no_error_on_malformed_geojson(con: psycopg.Connection) -> None:
    # One bad feature must not abort the batch - it lands with geom NULL (Phase 1 queries skip it).
    con.execute("INSERT INTO trails (id, name, geojson) VALUES ('osm:way/bad', 'B', %s)", ['{"type": "Nope"}'])

    assert con.execute("SELECT geom FROM trails WHERE id = 'osm:way/bad'").fetchone() == (None,)


def test_layer_geom_null_when_geojson_absent(con: psycopg.Connection) -> None:
    con.execute("INSERT INTO trails (id, name) VALUES ('osm:way/n', 'N')")

    assert con.execute("SELECT geom FROM trails WHERE id = 'osm:way/n'").fetchone() == (None,)


def test_apply_schema_full_path_heals_a_cleared_middle_migration(con: psycopg.Connection) -> None:
    # A deliberately-cleared middle version leaves max(version) unchanged, so the guard counts
    # applied rows instead - and apply_schema then re-runs the whole chain to heal it.
    con.execute("DELETE FROM schema_migrations WHERE version = 9")
    assert _schema_is_current(con) is False

    apply_schema(con)

    assert _schema_is_current(con) is True
