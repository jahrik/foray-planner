"""OSM bulk stager + shared trails snapshot loader tests (issue #442) - tiny .osm fixtures, no
network (the Geofabrik download is replaced by a copy of the fixture)."""

from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from foray import cache
from foray.cache import upsert_public_land, upsert_trails, upsert_trails_changed
from foray.config import CoverageRegion, Settings, Spaces
from foray.sources import osm_trails, trails_snapshot

_SPACES_CFG = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk")

# A forest road with a gate on it, a path that's a hiking-route member (so no row of its own),
# a footway (not ingested), a trailhead on the route, and the route itself.
_STATE_A = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="44.0000" lon="-122.0000"/>
  <node id="2" lat="44.0010" lon="-122.0000"><tag k="barrier" v="gate"/></node>
  <node id="3" lat="44.0020" lon="-122.0000"/>
  <node id="4" lat="44.0100" lon="-122.0100"><tag k="highway" v="trailhead"/><tag k="name" v="Ridge TH"/></node>
  <node id="5" lat="44.0200" lon="-122.0100"/>
  <node id="6" lat="44.0300" lon="-122.0300"/>
  <node id="7" lat="44.0310" lon="-122.0300"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="highway" v="track"/><tag k="ref" v="NF-2710"/></way>
  <way id="11"><nd ref="4"/><nd ref="5"/><tag k="highway" v="path"/><tag k="name" v="Ridge Trail"/></way>
  <way id="12"><nd ref="6"/><nd ref="7"/><tag k="highway" v="footway"/></way>
  <relation id="20">
    <member type="way" ref="11" role=""/>
    <member type="way" ref="31" role=""/>
    <tag k="route" v="hiking"/><tag k="name" v="Crest Route"/>
  </relation>
</osm>
"""

# The neighbouring state: the route's other member way, and the same forest road again (a way
# crossing the state line appears in both extracts).
_STATE_B = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="44.0000" lon="-122.0000"/>
  <node id="2" lat="44.0010" lon="-122.0000"><tag k="barrier" v="gate"/></node>
  <node id="3" lat="44.0020" lon="-122.0000"/>
  <node id="5" lat="44.0200" lon="-122.0100"/>
  <node id="8" lat="44.0400" lon="-122.0100"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="highway" v="track"/><tag k="ref" v="NF-2710"/></way>
  <way id="31"><nd ref="5"/><nd ref="8"/><tag k="highway" v="path"/><tag k="name" v="Ridge Trail"/></way>
  <relation id="20">
    <member type="way" ref="11" role=""/>
    <member type="way" ref="31" role=""/>
    <tag k="route" v="hiking"/><tag k="name" v="Crest Route"/>
  </relation>
</osm>
"""


def _regions(*names: str) -> list[CoverageRegion]:
    return [CoverageRegion(name=name, place_id=index + 1) for index, name in enumerate(names)]


def _fake_download(fixtures: dict[str, str]):
    def download(_client: httpx.Client, url: str, dest: Path) -> None:
        slug = url.rsplit("/", 1)[-1].removesuffix("-latest.osm.pbf")
        # pyosmium picks the parser from the file extension - keep the fixtures as XML.
        dest.with_suffix("").with_suffix(".osm").write_text(fixtures[slug])
        dest.symlink_to(dest.with_suffix("").with_suffix(".osm").name)

    return download


@pytest.fixture
def two_states(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(osm_trails, "_download", _fake_download({"state-a": _STATE_A, "state-b": _STATE_B}))
    # The symlinked fixture keeps its .osm.pbf name, so read it by the XML file it points to.
    original = osm_trails._state_elements
    monkeypatch.setattr(osm_trails, "_state_elements", lambda path, routes: original(path.resolve(), routes))
    return Settings(coverage=_regions("State A", "State B"), spaces=_SPACES_CFG)


def _rows_by_id(cfg: Settings, tmp_path: Path) -> dict[str, tuple]:
    with httpx.Client() as client:
        return {row[0]: row for row in osm_trails._iter_rows(cfg, client, tmp_path)}


def test_state_slug_matches_geofabrik_names() -> None:
    assert osm_trails._state_slug("New York") == "new-york"
    assert osm_trails._state_slug("Oregon") == "oregon"


def test_stager_builds_the_same_rows_the_overpass_path_would(two_states: Settings, tmp_path: Path) -> None:
    rows = _rows_by_id(two_states, tmp_path)

    road = rows["osm:way/10"]
    assert road[2] == "road"
    assert json.loads(road[10])["barrier"] == "gate"  # the gate node on the line was matched
    assert "osm:way/11" not in rows  # a hiking-route member: covered by the route's own row
    assert "osm:way/12" not in rows  # footway: not a class we ingest
    trailhead = rows["osm:node/4"]
    assert trailhead[2] == "trailhead"
    assert "osm:relation/20" in trailhead[8]  # linked to the route it sits on


def test_stager_stitches_a_route_from_every_state_it_crosses(two_states: Settings, tmp_path: Path) -> None:
    rows = _rows_by_id(two_states, tmp_path)

    route = rows["osm:relation/20"]
    assert route[2] == "route"
    geometry = json.loads(route[7])
    # One member way from each state's extract, not just the last state's piece.
    assert geometry["type"] == "MultiLineString"
    assert len(geometry["coordinates"]) == 2


def test_stager_emits_a_way_seen_in_two_extracts_once(two_states: Settings, tmp_path: Path) -> None:
    with httpx.Client() as client:
        ids = [row[0] for row in osm_trails._iter_rows(two_states, client, tmp_path)]
    assert ids.count("osm:way/10") == 1


def test_stager_refuses_a_state_with_no_trails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = '<?xml version="1.0" encoding="UTF-8"?><osm version="0.6"></osm>'
    monkeypatch.setattr(osm_trails, "_download", _fake_download({"state-a": _STATE_A, "empty": empty}))
    original = osm_trails._state_elements
    monkeypatch.setattr(osm_trails, "_state_elements", lambda path, routes: original(path.resolve(), routes))
    cfg = Settings(coverage=_regions("State A", "Empty"), spaces=_SPACES_CFG)
    with pytest.raises(RuntimeError, match="empty produced no trails"):
        _rows_by_id(cfg, tmp_path)


def _row(row_id: str, name: str = "Ridge Trail", lat: float = 44.0, source: str = "osm") -> tuple:
    coords = [[-122.0, lat], [-122.0, lat + 0.01]]
    return (
        row_id,
        name,
        "path",
        source,
        "https://example.com",
        lat,
        -122.0,
        json.dumps({"type": "LineString", "coordinates": coords}),
        None,
        1.1,
        None,
    )


def test_upsert_trails_changed_writes_only_new_and_changed_rows(con: psycopg.Connection) -> None:
    upsert_trails(con, [_row("osm:way/1"), _row("osm:way/2")])

    written = upsert_trails_changed(con, [_row("osm:way/1"), _row("osm:way/2", name="Renamed"), _row("osm:way/3")])

    assert sorted(written) == ["osm:way/2", "osm:way/3"]
    assert con.execute("SELECT name FROM trails WHERE id = 'osm:way/2'").fetchone() == ("Renamed",)


def _prune_twin(con: psycopg.Connection) -> tuple[tuple, tuple, tuple]:
    """Cache an OSM way beside a USFS trail plus a trailhead linked to it, and prune the way as
    the USFS trail's twin. Returns (twin, usfs, trailhead)."""
    twin = (*_row("osm:way/7")[:5], 44.0002, -122.0, *_row("osm:way/7")[7:])
    twin = (
        *twin[:7],
        json.dumps({"type": "LineString", "coordinates": [[-122.0, 44.0002], [-122.0, 44.0102]]}),
        *twin[8:],
    )
    usfs = _row("usfs:trail/1", name="RIDGE", source="usfs")
    trailhead = (
        "osm:node/9",
        "TH",
        "trailhead",
        "osm",
        "https://example.com",
        44.0,
        -122.0,
        json.dumps({"type": "Point", "coordinates": [-122.0, 44.0]}),
        ["osm:way/7"],
        None,
        None,
    )
    upsert_trails(con, [twin, usfs, trailhead])
    cache.prune_trail_duplicates(con, min_lat=43.9, min_lng=-122.1, max_lat=44.1, max_lng=-121.9)
    assert con.execute("SELECT 1 FROM trails WHERE id = 'osm:way/7'").fetchone() is None
    return twin, usfs, trailhead


def test_a_pruned_twin_stays_pruned_when_the_next_snapshot_lists_it_again(con: psycopg.Connection) -> None:
    # issue #442: every weekly OSM snapshot still lists a twin the dedup deleted. Without a
    # tombstone the diff load would see it as new, re-insert it, and the prune would delete it
    # again - every week.
    twin, _usfs, trailhead = _prune_twin(con)

    written = upsert_trails_changed(con, [twin, trailhead])

    assert written == []  # neither the twin nor the trailhead (already remapped) is "changed"
    assert con.execute("SELECT 1 FROM trails WHERE id = 'osm:way/7'").fetchone() is None
    stored = con.execute("SELECT connects FROM trails WHERE id = 'osm:node/9'").fetchone()
    assert stored is not None and stored[0] == ["usfs:trail/1"]


def test_an_edited_twin_is_let_back_in(con: psycopg.Connection) -> None:
    # Copilot review, PR #443: a tombstone keyed on the id alone would hide every later edit to
    # the way - here it's been renamed and moved well off the USFS trail.
    twin, _usfs, _trailhead = _prune_twin(con)
    moved = json.dumps({"type": "LineString", "coordinates": [[-121.5, 44.0], [-121.5, 44.01]]})
    edited = (twin[0], "Lookout Way", *twin[2:7], moved, *twin[8:])

    assert upsert_trails_changed(con, [edited]) == ["osm:way/7"]

    assert con.execute("SELECT name FROM trails WHERE id = 'osm:way/7'").fetchone() == ("Lookout Way",)
    assert con.execute("SELECT count(*) FROM trail_duplicates").fetchone() == (0,)


def test_rewriting_the_kept_row_releases_its_tombstones(con: psycopg.Connection) -> None:
    # The USFS trail was rerouted: the old "same trail" verdict no longer stands.
    _twin, usfs, _trailhead = _prune_twin(con)
    rerouted = json.dumps({"type": "LineString", "coordinates": [[-121.5, 44.0], [-121.5, 44.01]]})

    upsert_trails_changed(con, [(*usfs[:7], rerouted, *usfs[8:])])

    assert con.execute("SELECT count(*) FROM trail_duplicates").fetchone() == (0,)


def test_a_twin_comes_back_in_the_same_load_that_drops_its_kept_row(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Copilot review, PR #443: the snapshot no longer lists the OSM route but still lists its
    # member path. The path is held back by its tombstone, then the route's prune cascades the
    # tombstone away - the path must still end up cached, not wait for next week's snapshot.
    # Rows as a snapshot carries them (GeoJSON re-serialised from WKB), so the tombstone's
    # fingerprint matches the listed path and it really is held back.
    path = trails_snapshot.record_to_row(trails_snapshot.row_to_record(_row("osm:way/11")))
    route = (*path[:2], "route", *path[3:])
    route = ("osm:relation/20", *route[1:])
    upsert_trails(con, [path, route])
    cache.prune_trail_duplicates(con, min_lat=43.9, min_lng=-122.1, max_lat=44.1, max_lng=-121.9)
    assert con.execute("SELECT kept_id FROM trail_duplicates").fetchall() == [("osm:relation/20",)]
    _serve_snapshot(monkeypatch, [path])

    osm_trails.load_osm_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 10, 4), "run1")

    assert {row[0] for row in con.execute("SELECT id FROM trails").fetchall()} == {"osm:way/11"}


def test_a_load_that_died_before_its_dedup_is_deduped_in_full_on_the_rerun(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first load wrote the twin and died before deduping. The rerun's diff writes nothing
    # (the twin is cached and unchanged), so only the pending marker gets its tile deduped. An
    # earlier load completed, so "never loaded" isn't what triggers the full dedup here.
    cache.record_ingest(con, "trails:osm:bulk:2026-09-27", 0)
    twin = trails_snapshot.record_to_row(trails_snapshot.row_to_record(_row("osm:way/11")))
    route = ("osm:relation/20", twin[1], "route", *twin[3:])
    _serve_snapshot(monkeypatch, [twin, route])
    monkeypatch.setattr(cache, "prune_trail_duplicates_tiled", _raise_runtime_error)
    with pytest.raises(RuntimeError):
        osm_trails.load_osm_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 10, 4), "run1")
    monkeypatch.undo()
    _serve_snapshot(monkeypatch, [twin, route])

    osm_trails.load_osm_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 10, 4), "run1")

    assert {row[0] for row in con.execute("SELECT id FROM trails").fetchall()} == {"osm:relation/20"}
    assert con.execute("SELECT 1 FROM meta WHERE key LIKE 'trails_dedup_pending:%'").fetchone() is None


def test_a_source_never_loaded_to_completion_is_deduped_in_full(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Copilot review, PR #444: a first load that died before the pending marker existed left
    # its rows cached with no marker. Until a load completes, every tile is deduped.
    twin = trails_snapshot.record_to_row(trails_snapshot.row_to_record(_row("osm:way/11")))
    route = ("osm:relation/20", twin[1], "route", *twin[3:])
    upsert_trails(con, [twin, route])
    _serve_snapshot(monkeypatch, [twin, route])

    osm_trails.load_osm_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 10, 4), "run1")

    assert {row[0] for row in con.execute("SELECT id FROM trails").fetchall()} == {"osm:relation/20"}


def _raise_runtime_error(*_args: object) -> int:
    raise RuntimeError("killed")


def test_tiled_dedup_batches_a_long_id_list(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foray.cache.land_trails._PRUNE_ID_BATCH", 1)
    path = _row("osm:way/11")
    upsert_trails(con, [path, ("osm:relation/20", path[1], "route", *path[3:]), _row("osm:way/12", lat=46.5)])

    assert cache.prune_trail_duplicates_tiled(con, "osm", ["osm:way/11", "osm:way/12", "osm:relation/20"]) == 1


def test_a_tombstone_goes_away_with_the_row_that_replaced_it(con: psycopg.Connection) -> None:
    upsert_trails(con, [_row("usfs:trail/1", source="usfs")])
    con.execute("INSERT INTO trail_duplicates (osm_id, kept_id) VALUES ('osm:way/7', 'usfs:trail/1')")

    cache.prune_trails_missing_from(con, "usfs", ["usfs:trail/other"])

    assert con.execute("SELECT count(*) FROM trail_duplicates").fetchone() == (0,)
    upsert_trails(con, [_row("osm:way/7")])  # the OSM row is free to come back
    assert con.execute("SELECT 1 FROM trails WHERE id = 'osm:way/7'").fetchone() is not None


def _snapshot_bytes(rows: list[tuple]) -> bytes:
    buf = io.BytesIO()
    records = [trails_snapshot.row_to_record(row) for row in rows]
    with pq.ParquetWriter(buf, trails_snapshot.SNAPSHOT_SCHEMA) as writer:
        writer.write_table(pa.Table.from_pylist(records, schema=trails_snapshot.SNAPSHOT_SCHEMA))
    return buf.getvalue()


def _serve_snapshot(monkeypatch: pytest.MonkeyPatch, rows: list[tuple]) -> None:
    payload = _snapshot_bytes(rows)
    monkeypatch.setattr("foray.spaces.download_file", lambda cfg, key, dest_path: Path(dest_path).write_bytes(payload))


def test_load_osm_trails_diff_loads_and_prunes_what_osm_no_longer_lists(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_trails(con, [_row("osm:way/1"), _row("osm:way/gone", lat=45.0)])
    _serve_snapshot(monkeypatch, [_row("osm:way/1"), _row("osm:way/2", lat=44.5)])

    osm_trails.load_osm_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 10, 4), "run1")

    ids = {row[0] for row in con.execute("SELECT id FROM trails").fetchall()}
    assert ids == {"osm:way/1", "osm:way/2"}


def test_load_snapshot_does_not_prune_on_a_truncated_snapshot(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Four cached OSM rows, a snapshot listing one: far more likely truncated than real.
    upsert_trails(con, [_row(f"osm:way/{index}", lat=44.0 + index * 0.1) for index in range(4)])
    _serve_snapshot(monkeypatch, [_row("osm:way/0")])

    osm_trails.load_osm_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 10, 4), "run1")

    assert con.execute("SELECT count(*) FROM trails WHERE source = 'osm'").fetchone() == (4,)


def test_land_parts_lookup_tags_trails_like_whole_polygons(con: psycopg.Connection) -> None:
    # A wilderness inside a forest: the smaller unit wins, through the subdivided parts table.
    forest = json.dumps(
        {"type": "Polygon", "coordinates": [[[-123, 43], [-121, 43], [-121, 45], [-123, 45], [-123, 43]]]}
    )
    wilderness = json.dumps(
        {
            "type": "Polygon",
            "coordinates": [[[-122.1, 43.9], [-121.9, 43.9], [-121.9, 44.1], [-122.1, 44.1], [-122.1, 43.9]]],
        }
    )
    upsert_public_land(
        con,
        [
            ("usfs:1", "USFS", "Big National Forest", "usfs", "https://example.com", forest),
            ("usfs:2", "USFS", "Small Wilderness", "usfs", "https://example.com", wilderness),
        ],
    )
    assert cache.ensure_land_parts(con) is False  # the land upsert already built it
    upsert_trails(con, [_row("osm:way/1", lat=44.0), _row("osm:way/2", lat=44.5)])

    labels = dict(con.execute("SELECT id, land_unit FROM trails").fetchall())
    assert labels == {"osm:way/1": "Small Wilderness", "osm:way/2": "Big National Forest"}
