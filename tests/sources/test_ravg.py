"""RAVG bulk-snapshot severity ingest tests (issue #335 PR 4) - no network (mocked ArcGIS
transport + Space)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from foray.config import Settings, Spaces
from foray.sources.ravg import (
    _BULK_SNAPSHOT_SCHEMA,
    _ArcGISQueryError,
    _iter_pages,
    _parse_feature,
    load_ravg,
    normalize_fire_name,
    stage_ravg,
)

_SPACES_CFG = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk")
THIS_YEAR = 2026


def _write_ravg_snapshot(rows: list[dict]) -> bytes:
    buf = pa.BufferOutputStream()
    with pq.ParquetWriter(buf, _BULK_SNAPSHOT_SCHEMA) as writer:
        writer.write_table(pa.Table.from_pylist(rows, schema=_BULK_SNAPSHOT_SCHEMA))
    return buf.getvalue().to_pybytes()


def test_normalize_fire_name_strips_trailing_fire_and_uppercases() -> None:
    assert normalize_fire_name("No Man Fire") == "NO MAN"
    assert normalize_fire_name("No Man") == "NO MAN"
    assert normalize_fire_name("  lane 1  ") == "LANE 1"


def test_parse_feature_derives_severity_inputs() -> None:
    row = _parse_feature(
        {
            "attributes": {
                "event_id": "OR1",
                "fire_name": "No Man",
                "fire_year": "2024",
                "acres": 2098.26,
                "tree_acres": 2096.568,
                "tree_ac_50": 257.298,
                "tree_ac_75": 98.79,
            }
        }
    )
    assert row is not None
    assert row["fire_name"] == "No Man"
    assert row["fire_year"] == 2024
    assert row["tree_ac_75"] == 98.79


def test_parse_feature_is_case_insensitive_to_arcgis_field_casing() -> None:
    # EDW services commonly echo outFields back uppercase regardless of the requested casing -
    # a plain lowercase .get would see every row as missing fire_name/fire_year (Copilot review).
    row = _parse_feature(
        {
            "attributes": {
                "EVENT_ID": "OR1",
                "FIRE_NAME": "No Man",
                "FIRE_YEAR": "2024",
                "ACRES": 100.0,
                "TREE_ACRES": 90.0,
                "TREE_AC_50": 20.0,
                "TREE_AC_75": 5.0,
            }
        }
    )
    assert row is not None
    assert row["fire_name"] == "No Man"
    assert row["fire_year"] == 2024
    assert row["tree_ac_75"] == 5.0


def test_parse_feature_skips_fires_with_no_forested_acres_assessed() -> None:
    assert _parse_feature({"attributes": {"fire_name": "Desert Fire", "fire_year": "2024", "tree_acres": 0}}) is None
    assert _parse_feature({"attributes": {"fire_name": "Desert Fire", "fire_year": "2024"}}) is None


def test_parse_feature_skips_missing_name_or_year() -> None:
    assert _parse_feature({"attributes": {"fire_year": "2024", "tree_acres": 10.0}}) is None
    assert _parse_feature({"attributes": {"fire_name": "X", "tree_acres": 10.0}}) is None


def test_iter_pages_raises_on_an_arcgis_error_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 400, "message": "Invalid field"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(_ArcGISQueryError, match="ArcGIS query error"):
        list(_iter_pages(client))


def test_iter_pages_raises_on_a_response_missing_features() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(_ArcGISQueryError, match="malformed response"):
        list(_iter_pages(client))


def test_stage_ravg_uploads_rows_as_parquet(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "geometry" not in request.url.params  # attribute-only query, no spatial filter
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"features": []})
        return httpx.Response(
            200,
            json={
                "features": [
                    {
                        "attributes": {
                            "event_id": "OR1",
                            "fire_name": "No Man",
                            "fire_year": "2024",
                            "acres": 100.0,
                            "tree_acres": 90.0,
                            "tree_ac_50": 20.0,
                            "tree_ac_75": 5.0,
                        }
                    }
                ]
            },
        )

    uploaded: dict[str, bytes] = {}
    monkeypatch.setattr(
        "foray.sources.ravg.spaces.upload_file",
        lambda cfg, key, src_path, content_type: uploaded.__setitem__(key, Path(src_path).read_bytes()),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    stage_ravg(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    assert len(uploaded) == 1
    ((_key, data),) = uploaded.items()
    rows = pq.read_table(pa.BufferReader(data)).to_pylist()
    assert [row["fire_name"] for row in rows] == ["No Man"]


def test_stage_ravg_refuses_to_publish_a_zero_row_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": []})

    uploaded: dict[str, bytes] = {}
    monkeypatch.setattr(
        "foray.sources.ravg.spaces.upload_file",
        lambda cfg, key, src_path, content_type: uploaded.__setitem__(key, Path(src_path).read_bytes()),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="zero rows"):
        stage_ravg(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1", client=client)

    assert uploaded == {}


def test_load_ravg_applies_severity_by_name_year(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    con.execute(
        "INSERT INTO fire_perimeters (id, source_key, name, status, fire_year) "
        "VALUES ('perimeter_history:1', 'perimeter_history', 'No Man Fire', 'historical', %s)",
        [2024],
    )
    payload = _write_ravg_snapshot(
        [
            {
                "event_id": "OR1",
                "fire_name": "No Man",
                "fire_year": 2024,
                "acres": 100.0,
                "tree_acres": 90.0,
                "tree_ac_50": 20.0,
                "tree_ac_75": 5.0,
            }
        ]
    )
    monkeypatch.setattr(
        "foray.sources.ravg.spaces.download_file",
        lambda cfg, key, dest_path: Path(dest_path).write_bytes(payload),
    )

    load_ravg(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    row = con.execute(
        "SELECT dominant_severity, severity_low_acres, severity_moderate_acres, severity_high_acres, "
        "severity_unburned_acres FROM fire_perimeters WHERE id = %s",
        ["perimeter_history:1"],
    ).fetchone()
    # low = tree_acres - tree_ac_50 = 70, moderate = tree_ac_50 - tree_ac_75 = 15, high = 5,
    # unburned = acres - tree_acres = 10 -> dominant is "low" (largest of low/moderate/high).
    assert row == ("low", 70.0, 15.0, 5.0, 10.0)

    marker = con.execute("SELECT 1 FROM ingest_log WHERE key = %s", ["fire:ravg:bulk:2026-01-01"]).fetchone()
    assert marker is not None


def test_load_ravg_no_match_updates_nothing(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _write_ravg_snapshot(
        [
            {
                "event_id": "OR1",
                "fire_name": "Nonexistent",
                "fire_year": 2024,
                "acres": 10.0,
                "tree_acres": 10.0,
                "tree_ac_50": 1.0,
                "tree_ac_75": 0.0,
            }
        ]
    )
    monkeypatch.setattr(
        "foray.sources.ravg.spaces.download_file",
        lambda cfg, key, dest_path: Path(dest_path).write_bytes(payload),
    )

    load_ravg(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    marker = con.execute("SELECT 1 FROM ingest_log WHERE key = %s", ["fire:ravg:bulk:2026-01-01"]).fetchone()
    assert marker is not None  # still recorded even with zero matches
