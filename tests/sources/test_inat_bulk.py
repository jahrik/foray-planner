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
from foray.cache import upsert_taxa
from foray.config import Settings, Spaces
from foray.sources import inat_bulk
from foray.sources.http import HttpRangeReader
from foray.sources.inat_bulk import (
    DWCA_ENTRY,
    _parse_date,
    iter_scope_us_rows,
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
_HEADER[29] = "taxonID"
_HEADER[31] = "taxonRank"
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
    taxon_id: str = "",
    taxon_rank: str = "",
) -> list[str]:
    row = [""] * 48
    row[0] = str(obs_id)
    row[16] = event_date
    row[20] = lat
    row[21] = lng
    row[22] = uncertainty
    row[24] = country
    row[29] = taxon_id
    row[31] = taxon_rank
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
    # no Range header (inat_bulk.iter_scope_us_rows' single continuous stream, see this module's
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


def test_iter_scope_us_rows_filters_kingdom_country_and_missing_coords() -> None:
    zip_bytes = _dwca_zip(
        [
            _dwca_row(1, kingdom="Fungi", country="US", taxon_id="7001", taxon_rank="species"),  # kept
            _dwca_row(2, kingdom="Animalia", country="US"),  # wrong kingdom
            _dwca_row(3, kingdom="Fungi", country="CA"),  # wrong country
            _dwca_row(4, kingdom="Fungi", country="US", lat="", lng=""),  # no coords
        ]
    )
    with _mock_client(zip_bytes) as client:
        rows = list(iter_scope_us_rows(client, frozenset({"Fungi"})))
    assert [row["id"] for row in rows] == [1]
    assert rows[0]["genus"] == "Amanita"
    assert rows[0]["lat"] == 47.6
    assert (rows[0]["taxon_id"], rows[0]["taxon_rank"]) == (7001, "species")  # issue #464: staged for the loader


def _seed_amanita(con: psycopg.Connection) -> None:
    """Genus Amanita (48701) with a section between it and species Amanita muscaria (7001), plus a
    variety of it (7002) - the shape the rank-driven rollup has to step through."""
    upsert_taxa(
        con,
        [
            {"taxon_id": 47170, "name": "Fungi", "rank": "kingdom"},
            {"taxon_id": 48701, "name": "Amanita", "rank": "genus", "ancestor_ids": [47170]},
            {"taxon_id": 9001, "name": "Amanita", "rank": "section", "ancestor_ids": [47170, 48701]},
            {"taxon_id": 7001, "name": "Amanita muscaria", "rank": "species", "ancestor_ids": [47170, 48701, 9001]},
            {
                "taxon_id": 7002,
                "name": "Amanita muscaria var. alba",
                "rank": "variety",
                "ancestor_ids": [47170, 48701, 9001, 7001],
            },
        ],
    )


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
    _seed_amanita(con)
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


def test_load_inat_names_new_rows_and_fills_existing_unnamed_ones(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #449: the loader stores each row's own identification, and - since it is otherwise
    insert-only - also names rows it seeded before the column existed, without overwriting a
    name a live ingest already set."""
    _seed_amanita(con)
    con.execute(
        "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade)"
        " VALUES (5, 48701, 47.6, -122.3, '2026-05-01', 5, 'research')"
    )
    con.execute(
        "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade, taxon_name)"
        " VALUES (6, 48701, 47.6, -122.3, '2026-05-01', 5, 'research', 'Amanita pantherina')"
    )
    base = {
        "genus": "Amanita",
        "lat": 47.6,
        "lng": -122.3,
        "event_date": "2026-06-01",
        "coordinate_uncertainty_m": None,
    }
    payload = _write_parquet_bytes(
        [
            {"id": 1, **base, "scientific_name": "Amanita muscaria"},
            {"id": 5, **base, "scientific_name": "Amanita augusta"},
            {"id": 6, **base, "scientific_name": "Amanita muscaria"},
        ],
        inat_bulk._SNAPSHOT_SCHEMA,
    )
    monkeypatch.setattr(
        inat_bulk.spaces, "download_file", lambda cfg, key, dest_path: Path(dest_path).write_bytes(payload)
    )

    load_inat(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    names = con.execute("SELECT id, taxon_name FROM observations ORDER BY id").fetchall()
    assert names == [(1, "Amanita muscaria"), (5, "Amanita augusta"), (6, "Amanita pantherina")]


def test_load_inat_triggers_phenology_rebuild_over_threshold(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_amanita(con)
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


def test_load_inat_raises_when_taxa_catalog_empty(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inat_bulk.spaces, "download_file", lambda cfg, key, dest: None)
    with pytest.raises(RuntimeError, match="taxa catalog is empty"):
        load_inat(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")


_NEW_BASE = {"lat": 47.6, "lng": -122.3, "event_date": "2026-06-01", "coordinate_uncertainty_m": None}


def _stub_snapshot(monkeypatch: pytest.MonkeyPatch, rows: list[dict], schema: pa.Schema) -> None:
    payload = _write_parquet_bytes(rows, schema)
    monkeypatch.setattr(inat_bulk.spaces, "download_file", lambda cfg, key, dest: Path(dest).write_bytes(payload))


def test_load_inat_resolves_by_taxon_id_to_genus_and_species(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #464: the observation's own taxonID rolls up by rank - species and its variety land on
    the species (stepping over the same-named section), a genus-only ID has no species, an unknown
    ID is skipped."""
    _seed_amanita(con)
    rows = [
        {"id": 1, "genus": "Amanita", **_NEW_BASE, "taxon_id": 7001, "taxon_rank": "species"},
        {"id": 2, "genus": "Amanita", **_NEW_BASE, "taxon_id": 7002, "taxon_rank": "variety"},
        {"id": 3, "genus": "Amanita", **_NEW_BASE, "taxon_id": 48701, "taxon_rank": "genus"},
        {"id": 4, "genus": "Amanita", **_NEW_BASE, "taxon_id": 424242, "taxon_rank": "species"},  # uncataloged
        {"id": 5, "genus": "Amanita", **_NEW_BASE, "taxon_id": 47170, "taxon_rank": "kingdom"},  # above genus
    ]
    _stub_snapshot(monkeypatch, rows, inat_bulk._SNAPSHOT_SCHEMA)

    load_inat(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    assert con.execute("SELECT id, taxon_id, species_id FROM observations ORDER BY id").fetchall() == [
        (1, 48701, 7001),
        (2, 48701, 7001),
        (3, 48701, None),
    ]


def test_load_inat_fills_species_on_rows_cached_before_the_column(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loader is insert-only, so rows seeded before ``species_id`` existed get it filled in
    from the snapshot - and a row that already has a species is left alone."""
    _seed_amanita(con)
    con.execute(
        "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade)"
        " VALUES (5, 48701, 47.6, -122.3, '2026-05-01', 5, 'research')"
    )
    con.execute(
        "INSERT INTO observations (id, taxon_id, species_id, lat, lng, observed_on, month, quality_grade)"
        " VALUES (6, 48701, 123, 47.6, -122.3, '2026-05-01', 5, 'research')"
    )
    rows = [
        {"id": 5, "genus": "Amanita", **_NEW_BASE, "taxon_id": 7001, "taxon_rank": "species"},
        {"id": 6, "genus": "Amanita", **_NEW_BASE, "taxon_id": 7001, "taxon_rank": "species"},
    ]
    _stub_snapshot(monkeypatch, rows, inat_bulk._SNAPSHOT_SCHEMA)

    load_inat(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    assert con.execute("SELECT id, species_id FROM observations ORDER BY id").fetchall() == [(5, 7001), (6, 123)]


def test_load_inat_falls_back_to_genus_name_for_a_snapshot_staged_before_taxon_id(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshots staged before issue #464 have no ``taxon_id`` column: match the genus name against
    ``taxa`` (no species) until the next weekly stage replaces the snapshot."""
    _seed_amanita(con)
    old_schema = pa.schema(
        [
            ("id", pa.int64()),
            ("genus", pa.string()),
            ("lat", pa.float64()),
            ("lng", pa.float64()),
            ("event_date", pa.string()),
            ("coordinate_uncertainty_m", pa.string()),
            ("scientific_name", pa.string()),
        ]
    )
    rows = [
        {"id": 1, "genus": "Amanita", **_NEW_BASE, "scientific_name": "Amanita muscaria"},
        {"id": 2, "genus": "Nonexistentia", **_NEW_BASE, "scientific_name": None},
    ]
    _stub_snapshot(monkeypatch, rows, old_schema)

    load_inat(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    assert con.execute("SELECT id, taxon_id, species_id FROM observations ORDER BY id").fetchall() == [(1, 48701, None)]
