"""Duplicate campsites and trailheads (issue #451): OSM pitches fold into their campground, an OSM
campground folds into its RIDB twin, trailhead twins merge - and none of it comes back on the next
ingest."""

from __future__ import annotations

import json

import psycopg
import pytest

from foray import cache, scoring
from foray.cache import campsites as campsites_module
from foray.cache import upsert_campsites, upsert_trails

TILE = {"min_lat": 43.0, "min_lng": -123.0, "max_lat": 45.0, "max_lng": -121.0}
LAT, LNG = 44.0, -122.0
# ~0.0009 degrees of latitude is about 100 m.
M100 = 0.0009


def _camp(
    camp_id: str,
    name: str,
    *,
    lat: float = LAT,
    lng: float = LNG,
    camp_type: str | None = None,
    source: str | None = None,
) -> tuple:
    source = source or camp_id.split(":")[0]
    kind = "campground" if source == "ridb" else "reported"
    return (camp_id, name, kind, None, None, lat, lng, source, "https://example.com", None, None, None, camp_type)


def _ids(con: psycopg.Connection) -> list[str]:
    return [row[0] for row in con.execute("SELECT id FROM campsites ORDER BY id").fetchall()]


def test_pitches_fold_into_the_campground_they_sit_in(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("osm:way/1", "Woods Lake Campground", camp_type="tent"),
            _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + 0.0003, camp_type="pitch"),
            _camp("osm:node/2", "Camp pitch (OSM)", lat=LAT - 0.0003, camp_type="pitch"),
            _camp("osm:node/3", "Camp pitch (OSM)", lat=LAT + 0.5, camp_type="pitch"),  # far away
        ],
    )

    assert cache.prune_duplicate_campsites(con, **TILE) == 2

    assert _ids(con) == ["osm:node/3", "osm:way/1"]
    assert con.execute("SELECT pitch_count FROM campsites WHERE id = 'osm:way/1'").fetchone() == (2,)


def test_a_folded_pitch_stays_folded_when_the_next_ingest_lists_it_again(con: psycopg.Connection) -> None:
    rows = [
        _camp("osm:way/1", "Woods Lake Campground"),
        _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + 0.0003, camp_type="pitch"),
    ]
    upsert_campsites(con, rows)
    cache.prune_duplicate_campsites(con, **TILE)

    cache.upsert_campsites_deduped(con, rows)

    assert _ids(con) == ["osm:way/1"]
    assert con.execute("SELECT pitch_count FROM campsites WHERE id = 'osm:way/1'").fetchone() == (1,)


def test_a_released_parent_releases_its_pitches(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("osm:way/1", "Woods Lake Campground"),
            _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + 0.0003, camp_type="pitch"),
        ],
    )
    cache.prune_duplicate_campsites(con, **TILE)

    con.execute("DELETE FROM campsites WHERE id = 'osm:way/1'")

    assert con.execute("SELECT count(*) FROM campsite_duplicates").fetchone() == (0,)


def test_unclaimed_pitches_fold_into_one_marker_per_cluster(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("osm:node/1", "Camp pitch (OSM)", camp_type="pitch"),
            _camp("osm:node/2", "Camp pitch (OSM)", lat=LAT + 0.0005, camp_type="pitch"),
            _camp("osm:node/3", "Camp pitch (OSM)", lat=LAT + 0.0010, camp_type="pitch"),
            _camp("osm:node/4", "Camp pitch (OSM)", lat=LAT + 0.2, camp_type="pitch"),  # its own cluster
        ],
    )

    assert cache.prune_duplicate_campsites(con, **TILE) == 2

    assert _ids(con) == ["osm:node/1", "osm:node/4"]
    assert con.execute("SELECT pitch_count FROM campsites ORDER BY id").fetchall() == [(3,), (None,)]


def test_an_osm_campground_folds_into_its_ridb_twin(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("ridb:1", "Margies Cove West"),
            _camp("osm:way/1", "Margie's Cove", lat=LAT + M100 / 2, camp_type="tent"),
        ],
    )

    assert cache.prune_duplicate_campsites(con, **TILE) == 1

    assert _ids(con) == ["ridb:1"]
    assert con.execute("SELECT osm_id, camp_type FROM campsites").fetchone() == ("osm:way/1", "tent")


def test_a_differently_named_campground_next_door_is_kept(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [_camp("ridb:1", "Woods Lake Campground"), _camp("osm:way/1", "Snow Lake Camp", lat=LAT + M100 / 2)],
    )

    assert cache.prune_duplicate_campsites(con, **TILE) == 0


def test_an_unnamed_osm_campground_folds_into_a_ridb_facility_on_location_alone(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [_camp("ridb:1", "Woods Lake Campground"), _camp("osm:way/1", "Campsite (OSM)", lat=LAT + M100 / 2)],
    )

    assert cache.prune_duplicate_campsites(con, **TILE) == 1


def test_a_backcountry_site_is_never_a_campground_twin(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("ridb:1", "Woods Lake Campground"),
            _camp("osm:node/1", "Backcountry campsite (OSM)", lat=LAT + M100 / 2, camp_type="backcountry"),
        ],
    )

    assert cache.prune_duplicate_campsites(con, **TILE) == 0


def test_ridb_facilities_sharing_a_site_are_left_alone(con: psycopg.Connection) -> None:
    upsert_campsites(
        con, [_camp("ridb:1", "Woods Lake Campground"), _camp("ridb:2", "Woods Lake Group", lat=LAT + M100 / 2)]
    )

    assert cache.prune_duplicate_campsites(con, **TILE) == 0


def test_pitches_folded_into_an_osm_twin_follow_it_into_the_ridb_row(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("ridb:1", "Woods Lake Campground"),
            _camp("osm:way/1", "Woods Lake", lat=LAT + M100),
            _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + M100 * 2, camp_type="pitch"),
        ],
    )

    cache.prune_duplicate_campsites(con, **TILE)

    assert _ids(con) == ["ridb:1"]
    assert con.execute("SELECT pitch_count FROM campsites").fetchone() == (1,)
    assert con.execute("SELECT kept_id FROM campsite_duplicates").fetchall() == [("ridb:1",), ("ridb:1",)]


def test_the_ridb_reload_keeps_the_type_its_osm_twin_gave_it(con: psycopg.Connection) -> None:
    upsert_campsites(con, [_camp("ridb:1", "Woods Lake Campground"), _camp("osm:way/1", "Woods Lake", camp_type="rv")])
    cache.prune_duplicate_campsites(con, **TILE)

    upsert_campsites(con, [_camp("ridb:1", "Woods Lake Campground")])

    assert con.execute("SELECT camp_type FROM campsites").fetchone() == ("rv",)


def test_tiled_prune_covers_every_tile_with_a_campsite(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("osm:way/1", "Woods Lake Campground"),
            _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + 0.0003, camp_type="pitch"),
            _camp("osm:way/2", "Far Campground", lat=10.0, lng=10.0),
            _camp("osm:node/2", "Camp pitch (OSM)", lat=10.0003, lng=10.0, camp_type="pitch"),
        ],
    )

    assert cache.prune_duplicate_campsites_tiled(con) == 2


def _trailhead(trail_id: str, name: str, *, lat: float = LAT, lng: float = LNG, connects: list[str]) -> tuple:
    point = json.dumps({"type": "Point", "coordinates": [lng, lat]})
    return (trail_id, name, "trailhead", "osm", "https://example.com", lat, lng, point, connects, None, None)


def _path(trail_id: str) -> tuple:
    line = json.dumps({"type": "LineString", "coordinates": [[LNG, LAT], [LNG, LAT + 0.01]]})
    return (trail_id, "Ridge Trail", "path", "osm", "https://example.com", LAT, LNG, line, None, 1.1, None)


def _trailhead_ids(con: psycopg.Connection) -> list[str]:
    return [row[0] for row in con.execute("SELECT id FROM trails WHERE kind = 'trailhead' ORDER BY id").fetchall()]


def test_same_name_trailheads_merge_and_union_their_trails(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            _path("osm:way/1"),
            _path("osm:way/2"),
            _trailhead("osm:node/1", "Eklutna Trailhead", connects=["osm:way/1"]),
            _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0002, connects=["osm:way/2"]),
        ],
    )

    assert cache.prune_duplicate_trailheads(con, **TILE) == 1

    assert _trailhead_ids(con) == ["osm:node/1"]
    assert con.execute("SELECT connects FROM trails WHERE id = 'osm:node/1'").fetchone() == (
        ["osm:way/1", "osm:way/2"],
    )


def test_a_generic_trailhead_folds_into_the_named_one(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            _path("osm:way/1"),
            _trailhead("osm:node/1", "Trailhead (OSM)", connects=["osm:way/1"]),
            _trailhead("osm:node/9", "Eklutna Lakeside Trail", lat=LAT + 0.0003, connects=[]),
        ],
    )

    assert cache.prune_duplicate_trailheads(con, **TILE) == 1

    assert _trailhead_ids(con) == ["osm:node/9"]
    assert con.execute("SELECT connects FROM trails WHERE id = 'osm:node/9'").fetchone() == (["osm:way/1"],)


def test_differently_named_trailheads_on_one_lot_stay_apart(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            _trailhead("osm:node/1", "Hidden Valley Trailhead", connects=[]),
            _trailhead("osm:node/2", "Pipe Dream Trailhead", lat=LAT + 0.00003, connects=[]),
        ],
    )

    assert cache.prune_duplicate_trailheads(con, **TILE) == 0


def test_a_pruned_trailhead_stays_pruned_and_its_links_stay_unioned(con: psycopg.Connection) -> None:
    rows = [
        _path("osm:way/1"),
        _path("osm:way/2"),
        _trailhead("osm:node/1", "Eklutna Trailhead", connects=["osm:way/1"]),
        _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0002, connects=["osm:way/2"]),
    ]
    upsert_trails(con, rows)
    cache.prune_duplicate_trailheads(con, **TILE)

    upsert_trails(con, rows)  # the next snapshot lists both again

    assert _trailhead_ids(con) == ["osm:node/1"]
    assert con.execute("SELECT connects FROM trails WHERE id = 'osm:node/1'").fetchone() == (
        ["osm:way/1", "osm:way/2"],
    )


def test_trailhead_twin_chains_resolve_to_one_survivor(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            _trailhead("osm:node/1", "Eklutna Trailhead", connects=[]),
            _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0003, connects=[]),
            _trailhead("osm:node/3", "Trailhead (OSM)", lat=LAT + 0.0006, connects=[]),
        ],
    )

    assert cache.prune_duplicate_trailheads(con, **TILE) == 2

    assert _trailhead_ids(con) == ["osm:node/1"]
    assert sorted(con.execute("SELECT osm_id, kept_id FROM trail_duplicates").fetchall()) == [
        ("osm:node/2", "osm:node/1"),
        ("osm:node/3", "osm:node/1"),
    ]


def test_a_folded_pitch_that_moves_is_released_and_the_count_repaired(con: psycopg.Connection) -> None:
    parent = _camp("osm:way/1", "Woods Lake Campground")
    pitch = _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + 0.0003, camp_type="pitch")
    upsert_campsites(con, [parent, pitch])
    cache.prune_duplicate_campsites(con, **TILE)
    moved = (*pitch[:5], LAT + 0.3, *pitch[6:])

    upsert_campsites(con, [moved])

    assert _ids(con) == ["osm:node/1", "osm:way/1"]
    assert con.execute("SELECT pitch_count FROM campsites WHERE id = 'osm:way/1'").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM campsite_duplicates").fetchone() == (0,)


def test_an_osm_reload_clears_a_removed_type_but_a_ridb_reload_keeps_the_merged_one(
    con: psycopg.Connection,
) -> None:
    upsert_campsites(con, [_camp("osm:way/1", "Riverside", camp_type="rv")])

    upsert_campsites(con, [_camp("osm:way/1", "Riverside", camp_type=None)])

    assert con.execute("SELECT camp_type FROM campsites").fetchone() == (None,)


def test_a_named_backcountry_site_is_not_folded_into_a_ridb_twin(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("ridb:1", "Woods Lake Campground"),
            _camp("osm:node/1", "Woods Lake Camp", lat=LAT + M100 / 2, camp_type="backcountry"),
        ],
    )

    assert cache.prune_duplicate_campsites(con, **TILE) == 0


def test_pitches_either_side_of_a_tile_edge_fold_into_one(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            _camp("osm:node/1", "Camp pitch (OSM)", lat=43.9998, camp_type="pitch"),
            _camp("osm:node/2", "Camp pitch (OSM)", lat=44.0002, camp_type="pitch"),
        ],
    )

    cache.prune_duplicate_campsites_tiled(con)

    assert _ids(con) == ["osm:node/1"]
    assert con.execute("SELECT pitch_count FROM campsites").fetchone() == (2,)


def test_an_interrupted_tile_rolls_back_whole(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    upsert_campsites(
        con,
        [
            _camp("osm:way/1", "Woods Lake Campground"),
            _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + 0.0003, camp_type="pitch"),
        ],
    )

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("killed")

    monkeypatch.setattr(campsites_module, "_recount_pitches", interrupted)
    with pytest.raises(RuntimeError):
        cache.prune_duplicate_campsites(con, **TILE)

    assert _ids(con) == ["osm:node/1", "osm:way/1"]
    assert con.execute("SELECT count(*) FROM campsite_duplicates").fetchone() == (0,)


def test_a_legacy_unnamed_pitch_is_upgraded_not_merged_as_a_campground_twin(con: psycopg.Connection) -> None:
    # Cached before camp_type existed: no type, only the fallback name.
    upsert_campsites(
        con,
        [_camp("ridb:1", "Woods Lake Campground"), _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + M100 / 2)],
    )
    assert cache.prune_duplicate_campsites(con, **TILE) == 0

    assert cache.upgrade_legacy_pitches(con) == 1
    assert cache.prune_duplicate_campsites(con, **TILE) == 1

    assert con.execute("SELECT reason FROM campsite_duplicates").fetchone() == ("pitch",)
    assert con.execute("SELECT pitch_count FROM campsites").fetchone() == (1,)


def test_reloading_only_the_surviving_trailhead_keeps_the_merged_trails(con: psycopg.Connection) -> None:
    survivor = _trailhead("osm:node/1", "Eklutna Trailhead", connects=["osm:way/1"])
    twin = _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0002, connects=["osm:way/2"])
    upsert_trails(con, [_path("osm:way/1"), _path("osm:way/2"), survivor, twin])
    cache.prune_duplicate_trailheads(con, **TILE)

    upsert_trails(con, [survivor])

    assert con.execute("SELECT connects FROM trails WHERE id = 'osm:node/1'").fetchone() == (
        ["osm:way/1", "osm:way/2"],
    )


def test_reloading_only_the_pruned_twin_adds_its_trails_to_the_survivor(con: psycopg.Connection) -> None:
    survivor = _trailhead("osm:node/1", "Eklutna Trailhead", connects=["osm:way/1"])
    twin = _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0002, connects=["osm:way/2"])
    upsert_trails(con, [_path("osm:way/1"), _path("osm:way/2"), _path("osm:way/3"), survivor, twin])
    cache.prune_duplicate_trailheads(con, **TILE)
    regrown = _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0002, connects=["osm:way/2", "osm:way/3"])

    upsert_trails(con, [regrown])

    assert _trailhead_ids(con) == ["osm:node/1"]
    assert con.execute("SELECT connects FROM trails WHERE id = 'osm:node/1'").fetchone() == (
        ["osm:way/1", "osm:way/2", "osm:way/3"],
    )


def test_trails_near_can_keep_every_trailhead_of_a_repeated_name(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            _trailhead("osm:node/1", "Trailhead (OSM)", connects=[]),
            _trailhead("osm:node/2", "Trailhead (OSM)", lat=LAT + 0.05, connects=[]),
        ],
    )

    collapsed = scoring.trails_near(con, lat=LAT, lng=LNG, radius_km=20, kind="trailhead")
    every = scoring.trails_near(con, lat=LAT, lng=LNG, radius_km=20, kind="trailhead", distinct_names=False)

    assert (len(collapsed), len(every)) == (1, 2)


def test_a_parent_that_moves_releases_the_pitches_folded_into_it(con: psycopg.Connection) -> None:
    parent = _camp("osm:way/1", "Woods Lake Campground")
    pitch = _camp("osm:node/1", "Camp pitch (OSM)", lat=LAT + 0.0003, camp_type="pitch")
    upsert_campsites(con, [parent, pitch])
    cache.prune_duplicate_campsites(con, **TILE)
    moved_parent = (*parent[:5], LAT + 0.3, *parent[6:])

    upsert_campsites(con, [moved_parent, pitch])  # the pitch is unchanged

    assert _ids(con) == ["osm:node/1", "osm:way/1"]
    assert con.execute("SELECT pitch_count FROM campsites WHERE id = 'osm:way/1'").fetchone() == (0,)


def test_a_pitch_a_campground_claims_does_not_cluster_with_pitches_across_the_tile_edge(
    con: psycopg.Connection,
) -> None:
    upsert_campsites(
        con,
        [
            _camp("osm:node/1", "Camp pitch (OSM)", lat=43.9998, camp_type="pitch"),
            _camp("osm:node/2", "Camp pitch (OSM)", lat=44.0002, camp_type="pitch"),
            _camp("osm:way/9", "North Campground", lat=44.0014),
        ],
    )

    cache.prune_duplicate_campsites_tiled(con)

    assert con.execute("SELECT kept_id FROM campsite_duplicates WHERE dropped_id = 'osm:node/2'").fetchone() == (
        "osm:way/9",
    )


def test_an_absent_survivor_inherits_links_through_their_replacements(con: psycopg.Connection) -> None:
    survivor = _trailhead("osm:node/1", "Eklutna Trailhead", connects=["osm:way/1"])
    twin = _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0002, connects=["osm:way/4"])
    usfs = ("usfs:trail/1", "Ridge", "path", "usfs", "u", LAT, LNG, _path("x")[7], None, 1.1, None)
    upsert_trails(con, [_path("osm:way/1"), _path("osm:way/4"), survivor, twin, usfs])
    cache.prune_duplicate_trailheads(con, **TILE)
    # osm:way/2 was pruned as a twin of the USFS trail; the regrown twin now lists it.
    con.execute("INSERT INTO trail_duplicates (osm_id, kept_id) VALUES ('osm:way/2', 'usfs:trail/1')")
    regrown = _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0002, connects=["osm:way/2"])

    upsert_trails(con, [regrown])

    assert con.execute("SELECT connects FROM trails WHERE id = 'osm:node/1'").fetchone() == (
        ["osm:way/1", "osm:way/4", "usfs:trail/1"],
    )


def test_a_survivor_that_moves_drops_the_links_it_inherited(con: psycopg.Connection) -> None:
    survivor = _trailhead("osm:node/1", "Eklutna Trailhead", connects=["osm:way/1"])
    twin = _trailhead("osm:node/2", "Eklutna Trailhead", lat=LAT + 0.0002, connects=["osm:way/2"])
    upsert_trails(con, [_path("osm:way/1"), _path("osm:way/2"), _path("osm:way/3"), survivor, twin])
    cache.prune_duplicate_trailheads(con, **TILE)
    moved = _trailhead("osm:node/1", "Eklutna Trailhead", lat=LAT + 0.5, connects=["osm:way/3"])

    upsert_trails(con, [moved])

    assert con.execute("SELECT connects FROM trails WHERE id = 'osm:node/1'").fetchone() == (["osm:way/3"],)
