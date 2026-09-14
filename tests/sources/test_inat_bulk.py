"""Bulk iNat DwC-A stager/loader tests (issue #334 PR 2) - no network (mocked HTTP transport)."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from foray import spaces
from foray.cache import upsert_rows
from foray.config import Settings, Spaces
from foray.sources import inat_bulk
from foray.sources.http import HttpRangeReader
from foray.sources.inat_bulk import (
    DWCA_ENTRY,
    _parse_date,
    iter_fungi_us_rows,
    load_inat,
    stage_inat,
)

_SPACES_CFG = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk")

# Same column layout as the real archive's meta.xml (47 DwC fields after the core id), padded
# with empty strings for every column this module doesn't read.
_HEADER = [""] * 48
_HEADER[0] = "id"
_HEADER[16] = "eventDate"
_HEADER[20] = "decimalLatitude"
_HEADER[21] = "decimalLongitude"
_HEADER[22] = "coordinateUncertaintyInMeters"
_HEADER[24] = "countryCode"
_HEADER[32] = "kingdom"
_HEADER[37] = "genus"


def _dwca_row(
    obs_id: int,
    *,
    kingdom: str = "Fungi",
    country: str = "US",
    lat: str = "47.6",
    lng: str = "-122.3",
    genus: str = "Amanita",
    event_date: str = "2026-06-01",
    uncertainty: str = "",
) -> list[str]:
    row = [""] * 48
    row[0] = str(obs_id)
    row[16] = event_date
    row[20] = lat
    row[21] = lng
    row[22] = uncertainty
    row[24] = country
    row[32] = kingdom
    row[37] = genus
    return row


def _dwca_zip(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_HEADER)
    writer.writerows(rows)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr(DWCA_ENTRY, buf.getvalue())
    return zip_buf.getvalue()


_RealClient = httpx.Client  # captured before any test patches `httpx.Client` globally


def _mock_client(zip_bytes: bytes) -> httpx.Client:
    # Serves both access patterns: HEAD+Range (foray.sources.http.HttpRangeReader, still used by
    # camps.py's RIDB stager and exercised below against this same fixture) and a plain GET with
    # no Range header (inat_bulk.iter_fungi_us_rows' single continuous stream, see this module's
    # docstring for why it moved off range reads).
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(zip_bytes))})
        range_header = request.headers.get("Range")
        if range_header is None:
            return httpx.Response(200, content=zip_bytes)
        start_s, end_s = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_s), int(end_s)
        content_range = f"bytes {start}-{end}/{len(zip_bytes)}"
        return httpx.Response(206, headers={"Content-Range": content_range}, content=zip_bytes[start : end + 1])

    return _RealClient(transport=httpx.MockTransport(handler))


def test_http_range_reader_reassembles_full_content_via_zipfile() -> None:
    zip_bytes = _dwca_zip([_dwca_row(1)])
    with _mock_client(zip_bytes) as client:
        reader = HttpRangeReader(client, "https://example.test/archive.zip")
        buffered = io.BufferedReader(reader, buffer_size=64)  # small buffer forces multiple ranges
        with zipfile.ZipFile(buffered) as zf:
            assert zf.namelist() == [DWCA_ENTRY]
            assert zf.read(DWCA_ENTRY).decode().splitlines()[0].split(",")[0] == "id"


def test_iter_fungi_us_rows_filters_kingdom_country_and_missing_coords() -> None:
    zip_bytes = _dwca_zip(
        [
            _dwca_row(1, kingdom="Fungi", country="US"),  # kept
            _dwca_row(2, kingdom="Animalia", country="US"),  # wrong kingdom
            _dwca_row(3, kingdom="Fungi", country="CA"),  # wrong country
            _dwca_row(4, kingdom="Fungi", country="US", lat="", lng=""),  # no coords
        ]
    )
    with _mock_client(zip_bytes) as client:
        rows = list(iter_fungi_us_rows(client))
    assert [row["id"] for row in rows] == [1]
    assert rows[0]["genus"] == "Amanita"
    assert rows[0]["lat"] == 47.6


def _write_parquet_bytes(rows: list[dict], schema: pa.Schema) -> bytes:
    buf = pa.BufferOutputStream()
    with pq.ParquetWriter(buf, schema) as writer:
        writer.write_table(pa.Table.from_pylist(rows, schema=schema))
    return buf.getvalue().to_pybytes()


def test_parse_date_handles_missing_and_malformed() -> None:
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("not-a-date") is None
    assert _parse_date("2026-06-01T00:00:00") == date(2026, 6, 1)


def test_stage_inat_uploads_filtered_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    zip_bytes = _dwca_zip([_dwca_row(1, genus="Amanita"), _dwca_row(2, kingdom="Plantae")])
    uploaded: dict[str, bytes] = {}

    def fake_upload_file(cfg: Spaces, key: str, src_path: str, content_type: str) -> None:
        uploaded[key] = Path(src_path).read_bytes()

    monkeypatch.setattr(inat_bulk.spaces, "upload_file", fake_upload_file)
    monkeypatch.setattr(inat_bulk.httpx, "Client", lambda **kw: _mock_client(zip_bytes))

    stage_inat(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    assert len(uploaded) == 1
    ((key, data),) = uploaded.items()
    assert key == spaces.snapshot_run_prefix("inat", date(2026, 1, 1), "run1") + "fungi_us.parquet"
    rows = pq.read_table(pa.BufferReader(data)).to_pylist()
    assert [row["id"] for row in rows] == [1]


def test_stage_inat_retries_after_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    zip_bytes = _dwca_zip([_dwca_row(1, genus="Amanita")])
    uploaded: dict[str, bytes] = {}
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(zip_bytes))})
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadError("simulated dropped connection", request=request)
        return httpx.Response(200, content=zip_bytes)

    def fake_upload_file(cfg: Spaces, key: str, src_path: str, content_type: str) -> None:
        uploaded[key] = Path(src_path).read_bytes()

    monkeypatch.setattr(inat_bulk.spaces, "upload_file", fake_upload_file)
    monkeypatch.setattr(inat_bulk.httpx, "Client", lambda **kw: _RealClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(inat_bulk.time, "sleep", lambda seconds: None)

    stage_inat(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    assert attempts["n"] == 2  # first attempt dropped, second succeeded
    assert len(uploaded) == 1


def test_stage_inat_raises_after_exhausting_all_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "0"})
        raise httpx.ReadError("simulated dropped connection", request=request)

    monkeypatch.setattr(inat_bulk.httpx, "Client", lambda **kw: _RealClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(inat_bulk.time, "sleep", lambda seconds: None)

    with pytest.raises(httpx.ReadError):
        stage_inat(Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")


def test_load_inat_resolves_genus_and_upserts_observations(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_rows(con, "fungi_genera", ("taxon_id", "name"), [(48701, "Amanita")], conflict="taxon_id")
    payload_rows = [
        {
            "id": 1,
            "genus": "Amanita",
            "lat": 47.6,
            "lng": -122.3,
            "event_date": "2026-06-01",
            "coordinate_uncertainty_m": None,
        },
        {
            "id": 2,
            "genus": "Unknown Genus",
            "lat": 47.6,
            "lng": -122.3,
            "event_date": "2026-06-01",
            "coordinate_uncertainty_m": None,
        },
        {"id": 3, "genus": "Amanita", "lat": 47.6, "lng": -122.3, "event_date": None, "coordinate_uncertainty_m": None},
    ]
    payload = _write_parquet_bytes(payload_rows, inat_bulk._SNAPSHOT_SCHEMA)

    def fake_download_file(cfg: Spaces, key: str, dest_path: str) -> None:
        Path(dest_path).write_bytes(payload)

    monkeypatch.setattr(inat_bulk.spaces, "download_file", fake_download_file)

    load_inat(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    rows = con.execute("SELECT id, taxon_id, quality_grade FROM observations ORDER BY id").fetchall()
    assert rows == [(1, 48701, "research")]  # unknown genus and missing-date rows dropped
    marker = con.execute(
        "SELECT row_count FROM ingest_log WHERE key = %s", ["obs:fungi:place:1:2000-01-01:2026-06-01"]
    ).fetchone()
    assert marker == (1,)


def test_load_inat_triggers_phenology_rebuild_over_threshold(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_rows(con, "fungi_genera", ("taxon_id", "name"), [(48701, "Amanita")], conflict="taxon_id")
    payload_rows = [
        {
            "id": 1,
            "genus": "Amanita",
            "lat": 47.6,
            "lng": -122.3,
            "event_date": "2026-06-01",
            "coordinate_uncertainty_m": None,
        }
    ]
    payload = _write_parquet_bytes(payload_rows, inat_bulk._SNAPSHOT_SCHEMA)
    monkeypatch.setattr(inat_bulk.spaces, "download_file", lambda cfg, key, dest: Path(dest).write_bytes(payload))
    rebuilt: list[int] = []
    monkeypatch.setattr(
        inat_bulk, "maybe_rebuild_phenology", lambda con, cfg, new_rows: rebuilt.append(new_rows) or True
    )

    load_inat(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    assert rebuilt == [1]


def test_load_inat_raises_when_genus_catalog_empty(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inat_bulk.spaces, "download_file", lambda cfg, key, dest: None)
    with pytest.raises(RuntimeError, match="fungi_genera"):
        load_inat(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")
