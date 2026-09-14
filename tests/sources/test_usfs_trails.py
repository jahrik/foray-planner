"""USFS Trail_NFS bulk-snapshot ingest tests - no network (mocked ArcGIS transport + Space)."""

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
from foray.sources import usfs_trails
from foray.sources.usfs_trails import (
    _ArcGISQueryError,
    _attrs,
    _geojson_to_wkb,
    _get,
    _iter_pages,
    _parse_feature,
    _tracktype,
    load_usfs_trails,
    stage_usfs_trails,
)

HOME_LAT, HOME_LNG = 41.35, -124.0
_SPACES_CFG = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk")


def _line(lat: float, lng: float, size: float = 0.01) -> dict:
    return {"type": "LineString", "coordinates": [[lng - size, lat - size], [lng + size, lat + size]]}


def _write_trail_snapshot(rows: list[tuple]) -> bytes:
    """Encode `_parse_feature`-shaped tuples (GeoJSON text at index 7) as the Parquet bytes a
    real `stage_usfs_trails` run would upload (WKB bytes for that column instead)."""
    dict_rows = [
        dict(zip(usfs_trails._TRAIL_COLUMNS, (*row[:7], _geojson_to_wkb(row[7]), *row[8:]), strict=True))
        for row in rows
    ]
    buf = pa.BufferOutputStream()
    with pq.ParquetWriter(buf, usfs_trails._BULK_SNAPSHOT_SCHEMA) as writer:
        writer.write_table(pa.Table.from_pylist(dict_rows, schema=usfs_trails._BULK_SNAPSHOT_SCHEMA))
    return buf.getvalue().to_pybytes()


def test_get_is_case_insensitive() -> None:
    props = {"trail_name": "Lost Man Creek Trail", "TRAIL_CN": "123"}
    assert _get(props, "TRAIL_NAME") == "Lost Man Creek Trail"
    assert _get(props, "TRAIL_CN") == "123"
    assert _get(props, "missing") is None


def test_tracktype_inverts_usfs_trail_class_to_osm_grade() -> None:
    # USFS class 1 (minimal/undeveloped) is the roughest -> OSM's roughest grade5, and class 5
    # (fully developed) -> grade1 (best maintained) - opposite numbering scales.
    assert _tracktype(1) == "grade5"
    assert _tracktype(5) == "grade1"
    assert _tracktype(3) == "grade3"
    assert _tracktype(None) is None
    assert _tracktype("not a number") is None
    assert _tracktype(9) is None  # out of the 1-5 range


def test_attrs_keeps_the_minimal_field_subset() -> None:
    props = {
        "TRAIL_CLASS": 2,
        "TRAIL_SURFACE": "native",
        "MANAGING_ORG": "Six Rivers National Forest",
        "NATIONAL_TRAIL_DESIGNATION": "",
    }
    attrs = _attrs(props)
    assert attrs == {
        "trail_class": "2",
        "tracktype": "grade4",
        "trail_surface": "native",
        "managing_org": "Six Rivers National Forest",
    }


def test_attrs_returns_none_when_nothing_present() -> None:
    assert _attrs({}) is None


def test_parse_feature_builds_row_and_falls_back_on_missing_name() -> None:
    row = _parse_feature({"properties": {"TRAIL_CN": "42"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    assert row[0] == "usfs:trail/42"
    assert row[1] == "USFS trail"  # name absent -> fallback
    assert row[2] == "path"
    assert row[3] == "usfs"
    assert json.loads(row[7])["type"] == "LineString"
    assert row[8] is None  # connects
    assert row[9] > 0  # recomputed length_km, not trusted from the source


def test_parse_feature_skips_missing_geometry_or_id() -> None:
    assert _parse_feature({"properties": {"TRAIL_CN": "1"}, "geometry": None}) is None
    assert _parse_feature({"properties": {}, "geometry": _line(HOME_LAT, HOME_LNG)}) is None


def test_stage_usfs_trails_uploads_deduped_rows_as_parquet(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://apps.fs.usda.gov/")
        assert "geometry" not in request.url.params  # no envelope - the whole national table
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {"properties": {"TRAIL_CN": "7"}, "geometry": _line(HOME_LAT, HOME_LNG)},
                    {"properties": {"TRAIL_CN": "7"}, "geometry": _line(HOME_LAT, HOME_LNG)},  # dupe
                ],
            },
        )

    uploaded: dict[str, bytes] = {}

    def fake_upload_file(cfg: Spaces, key: str, src_path: str, content_type: str) -> None:
        uploaded[key] = Path(src_path).read_bytes()

    monkeypatch.setattr("foray.sources.usfs_trails.spaces.upload_file", fake_upload_file)
    client = httpx.Client(transport=httpx.MockTransport(handler))

    stage_usfs_trails(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    assert len(uploaded) == 1
    ((key, data),) = uploaded.items()
    assert key == spaces.snapshot_run_prefix("usfs_trails", date(2026, 1, 1), "run1") + "trails.parquet"
    rows = pq.read_table(pa.BufferReader(data)).to_pylist()
    assert [row["id"] for row in rows] == ["usfs:trail/7"]  # deduped


def test_stage_usfs_trails_pages_until_transfer_limit_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset == 0:
            features = [
                {"properties": {"TRAIL_CN": str(index)}, "geometry": _line(41.3 + index * 0.001, -124.0)}
                for index in range(1000)
            ]
            return httpx.Response(
                200, json={"type": "FeatureCollection", "features": features, "exceededTransferLimit": True}
            )
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"TRAIL_CN": "1000"}, "geometry": _line(41.4, -124.0)}],
            },
        )

    uploaded: dict[str, bytes] = {}
    monkeypatch.setattr(
        "foray.sources.usfs_trails.spaces.upload_file",
        lambda cfg, key, src_path, content_type: uploaded.__setitem__(key, Path(src_path).read_bytes()),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    stage_usfs_trails(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    ((_key, data),) = uploaded.items()
    ids = {row["id"] for row in pq.read_table(pa.BufferReader(data)).to_pylist()}
    assert "usfs:trail/1000" in ids
    assert len(ids) == 1001


def test_iter_pages_keeps_paging_on_a_transfer_limit_page_shorter_than_page_size() -> None:
    # ArcGIS sets exceededTransferLimit when EITHER the record-count limit or the response's
    # transfer-size limit is hit - a page truncated by size can carry fewer than _PAGE_SIZE
    # features and still have more data waiting at the next offset. Stopping on "short page"
    # alone (the original logic) would silently drop the remainder - a Copilot review catch.
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset == 0:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [{"properties": {"TRAIL_CN": "1"}, "geometry": _line(HOME_LAT, HOME_LNG)}],
                    "exceededTransferLimit": True,  # size-truncated, well under _PAGE_SIZE
                },
            )
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"TRAIL_CN": "2"}, "geometry": _line(HOME_LAT, HOME_LNG)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pages = list(_iter_pages(client))
    ids = {feature["properties"]["TRAIL_CN"] for page in pages for feature in page}
    assert ids == {"1", "2"}  # both pages read, not just the first


def test_iter_pages_requests_outsr_4326() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("outSR") == "4326"
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    list(_iter_pages(client))


def test_stage_usfs_trails_propagates_a_later_page_failure_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unlike the live/best-effort area ingests, a stage failure must NOT be swallowed: this
    # function runs before ingest_bulk.stage_snapshot's unconditional publish_snapshot call, so
    # catching the error here would publish a truncated snapshot as if it were the complete,
    # authoritative export - which load_usfs_trails' prune step would then read literally,
    # deleting every USFS trail the partial fetch didn't happen to reach (a Copilot review
    # catch). No upload_file call should happen at all.
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(500)
        features = [
            {"properties": {"TRAIL_CN": str(index)}, "geometry": _line(41.3 + index * 0.001, -124.0)}
            for index in range(1000)
        ]
        return httpx.Response(
            200, json={"type": "FeatureCollection", "features": features, "exceededTransferLimit": True}
        )

    uploaded: dict[str, bytes] = {}
    monkeypatch.setattr(
        "foray.sources.usfs_trails.spaces.upload_file",
        lambda cfg, key, src_path, content_type: uploaded.__setitem__(key, Path(src_path).read_bytes()),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        stage_usfs_trails(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    assert uploaded == {}


def test_iter_pages_raises_on_an_arcgis_error_payload() -> None:
    # ArcGIS reports query errors (a bad field name, an over-budget request, ...) as an HTTP
    # 200 with an `error` body, not an HTTP error status - raise_for_status() never sees it. An
    # empty `features` list is the normal end-of-pagination signal, so without this check an
    # error payload looks identical to "no more pages" and gets silently treated as success
    # (a Copilot review catch).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 400, "message": "Invalid field"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(_ArcGISQueryError, match="ArcGIS query error"):
        list(_iter_pages(client))


def test_iter_pages_raises_on_a_response_missing_features() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(_ArcGISQueryError, match="malformed response"):
        list(_iter_pages(client))


def test_stage_usfs_trails_refuses_to_publish_a_zero_row_result(monkeypatch: pytest.MonkeyPatch) -> None:
    # This source is never legitimately empty - an empty result means something went wrong
    # upstream (a where-clause typo, a service hiccup returning well-formed-but-empty pages),
    # not a valid "zero trails today" snapshot. Publishing it would still look like a
    # successful, complete, authoritative export to the loader's prune step (a Copilot review
    # catch, alongside the two above).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    uploaded: dict[str, bytes] = {}
    monkeypatch.setattr(
        "foray.sources.usfs_trails.spaces.upload_file",
        lambda cfg, key, src_path, content_type: uploaded.__setitem__(key, Path(src_path).read_bytes()),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="zero rows"):
        stage_usfs_trails(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    assert uploaded == {}


def test_load_usfs_trails_upserts_and_prunes_stale_rows(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = _parse_feature({"properties": {"TRAIL_CN": "gone"}, "geometry": _line(40.0, -120.0)})
    assert stale is not None
    upsert_trails(con, [stale])

    fresh = _parse_feature({"properties": {"TRAIL_CN": "9"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert fresh is not None
    payload = _write_trail_snapshot([fresh])

    def fake_download_file(cfg: Spaces, key: str, dest_path: str) -> None:
        Path(dest_path).write_bytes(payload)

    monkeypatch.setattr("foray.sources.usfs_trails.spaces.download_file", fake_download_file)

    load_usfs_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    ids = {row[0] for row in con.execute("SELECT id FROM trails WHERE source = 'usfs'").fetchall()}
    assert ids == {"usfs:trail/9"}  # stale row pruned, fresh row loaded

    rows = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0, kind="path")
    assert [trail.id for trail in rows] == ["usfs:trail/9"]
    assert rows[0].source == "usfs"


def test_load_usfs_trails_records_ingest_under_the_trails_prefix(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Namespaced under "trails:" (not "usfs_trails:") so /healthz/data's freshness reporting,
    # which reads every trails:-prefixed ingest_log key, picks this bulk load up.
    row = _parse_feature({"properties": {"TRAIL_CN": "1"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    payload = _write_trail_snapshot([row])
    monkeypatch.setattr(
        "foray.sources.usfs_trails.spaces.download_file",
        lambda cfg, key, dest_path: Path(dest_path).write_bytes(payload),
    )

    load_usfs_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    marker = con.execute("SELECT 1 FROM ingest_log WHERE key = %s", ["trails:usfs:bulk:2026-01-01"]).fetchone()
    assert marker is not None


def test_prune_trails_missing_from_does_nothing_when_ids_is_empty(con: psycopg.Connection) -> None:
    row = _parse_feature({"properties": {"TRAIL_CN": "1"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    upsert_trails(con, [row])
    assert prune_trails_missing_from(con, "usfs", []) == 0
    ids = {row[0] for row in con.execute("SELECT id FROM trails WHERE source = 'usfs'").fetchall()}
    assert ids == {"usfs:trail/1"}
