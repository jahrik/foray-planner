"""USFS Trail_NFS bulk-snapshot ingest tests - no network (mocked ArcGIS transport + Space)."""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

import httpx
import psycopg
import pytest

from foray import spaces
from foray.cache import prune_trails_missing_from, upsert_trails
from foray.config import Settings, Spaces
from foray.scoring import trails_near
from foray.sources.usfs_trails import (
    _attrs,
    _get,
    _parse_feature,
    _tracktype,
    load_usfs_trails,
    stage_usfs_trails,
)

HOME_LAT, HOME_LNG = 41.35, -124.0
_SPACES_CFG = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk")


def _line(lat: float, lng: float, size: float = 0.01) -> dict:
    return {"type": "LineString", "coordinates": [[lng - size, lat - size], [lng + size, lat + size]]}


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


def test_stage_usfs_trails_uploads_deduped_rows_as_gzip_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
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

    def fake_put_object(cfg: Spaces, key: str, data: bytes, content_type: str, **kwargs: object) -> str:
        uploaded[key] = data
        return f"https://space/{key}"

    monkeypatch.setattr("foray.sources.usfs_trails.spaces.put_object", fake_put_object)
    client = httpx.Client(transport=httpx.MockTransport(handler))

    stage_usfs_trails(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    assert len(uploaded) == 1
    ((key, data),) = uploaded.items()
    assert key == spaces.snapshot_run_prefix("usfs_trails", date(2026, 1, 1), "run1") + "trails.jsonl.gz"
    rows = [json.loads(line) for line in gzip.decompress(data).decode().splitlines()]
    assert [row[0] for row in rows] == ["usfs:trail/7"]  # deduped


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
        "foray.sources.usfs_trails.spaces.put_object",
        lambda cfg, key, data, content_type, **kw: uploaded.__setitem__(key, data),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    stage_usfs_trails(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    ((_key, data),) = uploaded.items()
    ids = {json.loads(line)[0] for line in gzip.decompress(data).decode().splitlines()}
    assert "usfs:trail/1000" in ids
    assert len(ids) == 1001


def test_stage_usfs_trails_keeps_parsed_rows_when_a_later_page_fails(monkeypatch: pytest.MonkeyPatch) -> None:
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
        "foray.sources.usfs_trails.spaces.put_object",
        lambda cfg, key, data, content_type, **kw: uploaded.__setitem__(key, data),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    stage_usfs_trails(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    # best-effort: the first page's rows still get staged despite the later transport error
    ((_key, data),) = uploaded.items()
    rows = gzip.decompress(data).decode().splitlines()
    assert len(rows) == 1000


def test_load_usfs_trails_upserts_and_prunes_stale_rows(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = _parse_feature({"properties": {"TRAIL_CN": "gone"}, "geometry": _line(40.0, -120.0)})
    assert stale is not None
    upsert_trails(con, [stale])

    fresh = _parse_feature({"properties": {"TRAIL_CN": "9"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert fresh is not None
    payload = "\n".join(json.dumps(row) for row in [fresh]).encode()

    def fake_download_file(cfg: Spaces, key: str, dest_path: str) -> None:
        Path(dest_path).write_bytes(gzip.compress(payload))

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
    payload = "\n".join(json.dumps(r) for r in [row]).encode()
    monkeypatch.setattr(
        "foray.sources.usfs_trails.spaces.download_file",
        lambda cfg, key, dest_path: Path(dest_path).write_bytes(gzip.compress(payload)),
    )

    load_usfs_trails(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    marker = con.execute("SELECT 1 FROM ingest_log WHERE key = %s", ["trails:usfs:bulk:2026-01-01"]).fetchone()
    assert marker is not None


def test_prune_trails_missing_from_does_nothing_when_ids_is_empty(con: psycopg.Connection) -> None:
    row = _parse_feature({"properties": {"TRAIL_CN": "1"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    upsert_trails(con, [row])
    assert prune_trails_missing_from(con, "usfs", []) == 0
    ids = {r[0] for r in con.execute("SELECT id FROM trails WHERE source = 'usfs'").fetchall()}
    assert ids == {"usfs:trail/1"}
