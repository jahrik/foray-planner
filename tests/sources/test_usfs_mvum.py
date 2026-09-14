"""USFS MVUM roads bulk-snapshot ingest tests - no network (mocked ArcGIS transport + Space)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from foray import spaces
from foray.cache import prune_trails_missing_from, upsert_trails
from foray.config import Settings, Spaces
from foray.scoring import trails_near
from foray.sources import usfs_mvum
from foray.sources.usfs_mvum import (
    _ArcGISQueryError,
    _attrs,
    _geojson_to_wkb,
    _get,
    _iter_pages,
    _motor_vehicle,
    _parse_feature,
    _tracktype,
    load_usfs_mvum,
    stage_usfs_mvum,
)

HOME_LAT, HOME_LNG = 41.35, -124.0
_SPACES_CFG = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk")


def _line(lat: float, lng: float, size: float = 0.01) -> dict:
    return {"type": "LineString", "coordinates": [[lng - size, lat - size], [lng + size, lat + size]]}


def _write_snapshot(rows: list[tuple]) -> bytes:
    dict_rows = [
        dict(zip(usfs_mvum._TRAIL_COLUMNS, (*row[:7], _geojson_to_wkb(row[7]), *row[8:]), strict=True)) for row in rows
    ]
    buf = pa.BufferOutputStream()
    with pq.ParquetWriter(buf, usfs_mvum._BULK_SNAPSHOT_SCHEMA) as writer:
        writer.write_table(pa.Table.from_pylist(dict_rows, schema=usfs_mvum._BULK_SNAPSHOT_SCHEMA))
    return buf.getvalue().to_pybytes()


def test_get_is_case_insensitive() -> None:
    props = {"rte_cn": "123", "NAME": "Lost Man Road"}
    assert _get(props, "RTE_CN") == "123"
    assert _get(props, "NAME") == "Lost Man Road"
    assert _get(props, "missing") is None


def test_tracktype_inverts_usfs_maint_level_to_osm_grade() -> None:
    assert _tracktype("1 - BASIC CUSTODIAL CARE") == "grade5"
    assert _tracktype("5 - DOUBLE LANE PAVED") == "grade1"
    assert _tracktype("3 - SUITABLE FOR PASSENGER CARS") == "grade3"
    assert _tracktype(None) is None
    assert _tracktype("not a level") is None
    assert _tracktype("9 - out of range") is None


def test_motor_vehicle_is_no_unless_a_standard_vehicle_class_is_open() -> None:
    # Only ATV/motorcycle open - closed to ordinary cars/trucks, so walk-in for foraging purposes.
    assert _motor_vehicle({"ATV": "open", "PASSENGERVEHICLE": None}) == "no"
    # No vehicle-class field carries any data at all - nothing to derive from, not a closure.
    assert _motor_vehicle({}) is None
    # A standard vehicle class open -> a normal drivable road, no override.
    assert _motor_vehicle({"PASSENGERVEHICLE": "open"}) is None
    assert _motor_vehicle({"HIGHCLEARANCEVEHICLE": "open", "ATV": None}) is None
    assert _motor_vehicle({"TRUCK": "Open"}) is None  # case-insensitive


def test_attrs_keeps_the_minimal_field_subset() -> None:
    props = {
        "OPERATIONALMAINTLEVEL": "2 - HIGH CLEARANCE VEHICLES",
        "SEASONAL": "seasonal",
        "ID": "300",
        "SURFACETYPE": "NAT - NATIVE MATERIAL",
        "JURISDICTION": "FS - FOREST SERVICE",
        "PASSENGERVEHICLE": None,
        "HIGHCLEARANCEVEHICLE": "open",
        "TRUCK": None,
    }
    attrs = _attrs(props)
    assert attrs == {
        "road_maint_level": "2 - HIGH CLEARANCE VEHICLES",
        "tracktype": "grade4",
        "seasonal": "yes",
        "ref": "300",
        "road_surface": "NAT - NATIVE MATERIAL",
        "managing_org": "FS - FOREST SERVICE",
    }


def test_attrs_omits_seasonal_when_yearlong_and_motor_vehicle_when_open() -> None:
    props = {"SEASONAL": "yearlong", "PASSENGERVEHICLE": "open", "SURFACETYPE": "AGG - GRAVEL"}
    attrs = _attrs(props)
    assert attrs is not None
    assert "seasonal" not in attrs
    assert "motor_vehicle" not in attrs


def test_attrs_returns_none_when_nothing_present() -> None:
    assert _attrs({}) is None


def test_parse_feature_builds_row_and_falls_back_on_missing_name() -> None:
    row = _parse_feature({"properties": {"OBJECTID": "42", "ID": "300"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    assert row[0] == "usfs:road/42"
    assert row[1] == "FR 300"  # name absent -> fallback built from the route number
    assert row[2] == "road"
    assert row[3] == "usfs_mvum"
    assert json.loads(row[7])["type"] == "LineString"
    assert row[8] is None  # connects
    assert row[9] > 0  # recomputed length_km


def test_parse_feature_falls_back_to_generic_name_with_no_route_number() -> None:
    row = _parse_feature({"properties": {"OBJECTID": "42"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    assert row[1] == "USFS road"


def test_parse_feature_skips_missing_geometry_or_id() -> None:
    assert _parse_feature({"properties": {"OBJECTID": "1"}, "geometry": None}) is None
    assert _parse_feature({"properties": {}, "geometry": _line(HOME_LAT, HOME_LNG)}) is None


def test_parse_feature_keeps_distinct_segments_sharing_one_route_number() -> None:
    # A Copilot review catch: RTE_CN is a route-level id shared by every physical segment of a
    # route (confirmed live - e.g. "TRAIL RIVER ROAD" split into a 0.2mi and a 0.303mi segment,
    # two different OBJECTIDs, both RTE_CN=168010270), so it can't be the row id without silently
    # collapsing segments. OBJECTID (per-feature) is the id; RTE_CN is kept as attrs.route_cn.
    segment_a = _parse_feature(
        {
            "properties": {"OBJECTID": "1001", "RTE_CN": "168010270", "NAME": "TRAIL RIVER ROAD"},
            "geometry": _line(41.0, -124.0),
        }
    )
    segment_b = _parse_feature(
        {
            "properties": {"OBJECTID": "1002", "RTE_CN": "168010270", "NAME": "TRAIL RIVER ROAD"},
            "geometry": _line(41.1, -124.0),
        }
    )
    assert segment_a is not None and segment_b is not None
    assert segment_a[0] == "usfs:road/1001"
    assert segment_b[0] == "usfs:road/1002"  # distinct ids - neither overwrites the other
    assert json.loads(segment_a[10])["route_cn"] == "168010270"


def test_iter_pages_requests_the_system_road_symbol_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("where") == "symbol IN ('1','2','3','4','11','12')"
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    list(_iter_pages(client))


def test_stage_usfs_mvum_uploads_deduped_rows_as_parquet(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://apps.fs.usda.gov/")
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {"properties": {"OBJECTID": "7"}, "geometry": _line(HOME_LAT, HOME_LNG)},
                    {"properties": {"OBJECTID": "7"}, "geometry": _line(HOME_LAT, HOME_LNG)},  # dupe
                ],
            },
        )

    uploaded: dict[str, bytes] = {}
    monkeypatch.setattr(
        "foray.sources.usfs_mvum.spaces.upload_file",
        lambda cfg, key, src_path, content_type: uploaded.__setitem__(key, Path(src_path).read_bytes()),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    stage_usfs_mvum(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    assert len(uploaded) == 1
    ((key, data),) = uploaded.items()
    assert key == spaces.snapshot_run_prefix("usfs_mvum", date(2026, 1, 1), "run1") + "trails.parquet"
    rows = pq.read_table(pa.BufferReader(data)).to_pylist()
    assert [row["id"] for row in rows] == ["usfs:road/7"]  # deduped


def test_iter_pages_raises_on_an_arcgis_error_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 400, "message": "Invalid field"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(_ArcGISQueryError, match="ArcGIS query error"):
        list(_iter_pages(client))


def test_stage_usfs_mvum_refuses_to_publish_a_zero_row_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    uploaded: dict[str, bytes] = {}
    monkeypatch.setattr(
        "foray.sources.usfs_mvum.spaces.upload_file",
        lambda cfg, key, src_path, content_type: uploaded.__setitem__(key, Path(src_path).read_bytes()),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="zero rows"):
        stage_usfs_mvum(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    assert uploaded == {}


def test_load_usfs_mvum_upserts_and_prunes_stale_rows(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = _parse_feature({"properties": {"OBJECTID": "gone"}, "geometry": _line(40.0, -120.0)})
    assert stale is not None
    upsert_trails(con, [stale])

    fresh = _parse_feature({"properties": {"OBJECTID": "9"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert fresh is not None
    payload = _write_snapshot([fresh])
    monkeypatch.setattr(
        "foray.sources.usfs_mvum.spaces.download_file",
        lambda cfg, key, dest_path: Path(dest_path).write_bytes(payload),
    )

    load_usfs_mvum(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    ids = {row[0] for row in con.execute("SELECT id FROM trails WHERE source = 'usfs_mvum'").fetchall()}
    assert ids == {"usfs:road/9"}  # stale row pruned, fresh row loaded

    rows = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0, kind="road")
    assert [trail.id for trail in rows] == ["usfs:road/9"]
    assert rows[0].source == "usfs_mvum"


def test_load_usfs_mvum_does_not_prune_usfs_trail_nfs_rows(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `usfs_mvum` and `usfs_trails` (PR 3a) are distinct `source` values precisely so each load's
    # prune-to-exactly-what's-listed step only ever touches its own rows.
    from foray.sources.usfs_trails import _parse_feature as parse_trail_nfs

    trail_nfs_row = parse_trail_nfs({"properties": {"TRAIL_CN": "1"}, "geometry": _line(40.0, -120.0)})
    assert trail_nfs_row is not None
    upsert_trails(con, [trail_nfs_row])

    payload = _write_snapshot([])
    monkeypatch.setattr(
        "foray.sources.usfs_mvum.spaces.download_file",
        lambda cfg, key, dest_path: Path(dest_path).write_bytes(payload),
    )
    load_usfs_mvum(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    ids = {row[0] for row in con.execute("SELECT id FROM trails WHERE source = 'usfs'").fetchall()}
    assert ids == {"usfs:trail/1"}  # untouched by the (empty) usfs_mvum load


def test_load_usfs_mvum_records_ingest_under_the_trails_prefix(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _parse_feature({"properties": {"OBJECTID": "1"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    payload = _write_snapshot([row])
    monkeypatch.setattr(
        "foray.sources.usfs_mvum.spaces.download_file",
        lambda cfg, key, dest_path: Path(dest_path).write_bytes(payload),
    )

    load_usfs_mvum(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    marker = con.execute("SELECT 1 FROM ingest_log WHERE key = %s", ["trails:usfs_mvum:bulk:2026-01-01"]).fetchone()
    assert marker is not None


def test_prune_trails_missing_from_does_nothing_when_ids_is_empty(con: psycopg.Connection) -> None:
    row = _parse_feature({"properties": {"OBJECTID": "1"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    upsert_trails(con, [row])
    assert prune_trails_missing_from(con, "usfs_mvum", []) == 0
    ids = {row[0] for row in con.execute("SELECT id FROM trails WHERE source = 'usfs_mvum'").fetchall()}
    assert ids == {"usfs:road/1"}
