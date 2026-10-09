"""Taxonomy bulk source (issue #464): stager and loader on a hand-built export - no network."""

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
from foray.sources import taxa_bulk
from foray.sources.taxa_bulk import load_taxa, stage_taxa

_SPACES_CFG = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk")
_RealClient = httpx.Client

_TAXA_HEADER = [
    "id",
    "taxonID",
    "identifier",
    "parentNameUsageID",
    "kingdom",
    "phylum",
    "class",
    "order",
    "family",
    "genus",
    "specificEpithet",
    "infraspecificEpithet",
    "modified",
    "scientificName",
    "taxonRank",
    "references",
]
_URL = "https://www.inaturalist.org/taxa/"

FUNGI, CLASS, ORDER, FAMILY, GENUS, SECTION, SPECIES, VARIETY = 47170, 50814, 47169, 47167, 47168, 9001, 48715, 9002


def _taxon(taxon_id: int, parent: int | None, kingdom: str, name: str, rank: str) -> list[str]:
    row = [""] * len(_TAXA_HEADER)
    row[0] = str(taxon_id)
    row[3] = f"{_URL}{parent}" if parent else ""
    row[4] = kingdom
    row[13] = name
    row[14] = rank
    return row


_TAXA_ROWS = [
    _taxon(1, None, "Animalia", "Animalia", "kingdom"),
    _taxon(3, 1, "Animalia", "Aves", "class"),
    _taxon(47126, None, "Plantae", "Plantae", "kingdom"),
    _taxon(FUNGI, None, "Fungi", "Fungi", "kingdom"),
    _taxon(CLASS, FUNGI, "Fungi", "Agaricomycetes", "class"),
    _taxon(ORDER, CLASS, "Fungi", "Agaricales", "order"),
    _taxon(FAMILY, ORDER, "Fungi", "Amanitaceae", "family"),
    _taxon(GENUS, FAMILY, "Fungi", "Amanita", "genus"),
    _taxon(SECTION, GENUS, "Fungi", "Amanita", "section"),
    _taxon(SPECIES, SECTION, "Fungi", "Amanita muscaria", "species"),
    _taxon(VARIETY, SPECIES, "Fungi", "Amanita muscaria var. alba", "variety"),
    _taxon(77, 999999, "Fungi", "Orphanus", "genus"),  # parent missing from the export
]
_NAME_HEADER = [
    "id",
    "vernacularName",
    "language",
    "locality",
    "countryCode",
    "source",
    "lexicon",
    "contributor",
    "created",
]
_ENGLISH = [
    [str(SPECIES), "Fly Agaric", "en", "", "", "", "English", "x", ""],
    [str(SPECIES), "Fly Amanita", "en", "", "", "", "English", "x", ""],
    [str(GENUS), "Amanitas", "en", "", "", "", "English", "x", ""],
    ["3", "Birds", "en", "", "", "", "English", "x", ""],  # not in scope: must not be staged
]
_SPANISH = [[str(SPECIES), "Matamoscas", "es", "", "", "", "Spanish", "x", ""]]


def _csv_text(header: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _export_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("eml.xml", "<eml/>")
        zf.writestr("taxa.csv", _csv_text(_TAXA_HEADER, _TAXA_ROWS))
        zf.writestr("VernacularNames-english.csv", _csv_text(_NAME_HEADER, _ENGLISH))
        zf.writestr("VernacularNames-spanish.csv", _csv_text(_NAME_HEADER, _SPANISH))
    return buf.getvalue()


def _stage(monkeypatch: pytest.MonkeyPatch, cfg: Settings | None = None) -> dict[str, list[dict]]:
    uploaded: dict[str, bytes] = {}
    monkeypatch.setattr(
        taxa_bulk.spaces,
        "upload_file",
        lambda cfg, key, src_path, content_type: uploaded.update({key: Path(src_path).read_bytes()}),
    )
    monkeypatch.setattr(
        taxa_bulk.httpx,
        "Client",
        lambda **kw: _RealClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=_export_zip()))
        ),
    )
    stage_taxa(cfg or Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")
    prefix = spaces.snapshot_run_prefix("taxa", date(2026, 1, 1), "run1")
    return {
        key.removeprefix(prefix): pq.read_table(pa.BufferReader(data)).to_pylist() for key, data in uploaded.items()
    }


def test_stage_keeps_the_scope_kingdom_with_rebuilt_ancestor_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    staged = _stage(monkeypatch)
    taxa = {row["taxon_id"]: row for row in staged["taxa.parquet"]}

    assert set(taxa) == {FUNGI, CLASS, ORDER, FAMILY, GENUS, SECTION, SPECIES, VARIETY}  # no animals or plants
    assert taxa[FUNGI]["ancestor_ids"] == []
    assert taxa[GENUS]["ancestor_ids"] == [FUNGI, CLASS, ORDER, FAMILY]
    assert taxa[VARIETY]["ancestor_ids"] == [FUNGI, CLASS, ORDER, FAMILY, GENUS, SECTION, SPECIES]
    # 77's parent is missing from the export: its chain never reaches a scope root, so it is left
    # out (and counted in the stager's log) rather than failing the stage.
    assert 77 not in taxa
    # The section shares the genus's name; both are staged, told apart by rank.
    assert (taxa[GENUS]["name"], taxa[GENUS]["rank"]) == ("Amanita", "genus")
    assert (taxa[SECTION]["name"], taxa[SECTION]["rank"]) == ("Amanita", "section")


def test_stage_keeps_names_only_for_staged_taxa_in_every_language(monkeypatch: pytest.MonkeyPatch) -> None:
    names = {(row["taxon_id"], row["name"], row["lexicon"]) for row in _stage(monkeypatch)["names.parquet"]}
    assert names == {
        (SPECIES, "Fly Agaric", "English"),
        (SPECIES, "Fly Amanita", "English"),
        (GENUS, "Amanitas", "English"),
        (SPECIES, "Matamoscas", "Spanish"),
    }


def test_stage_scope_root_narrows_to_a_subtree_and_its_ancestors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-kingdom root (how trees would be added): its descendants plus its own lineage."""
    cfg = Settings(spaces=_SPACES_CFG, scope_roots=[FAMILY])
    taxa = {row["taxon_id"] for row in _stage(monkeypatch, cfg)["taxa.parquet"]}
    assert taxa == {FUNGI, CLASS, ORDER, FAMILY, GENUS, SECTION, SPECIES, VARIETY}  # not the orphan genus


def test_stage_refuses_to_publish_an_empty_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="no taxa"):
        _stage(monkeypatch, Settings(spaces=_SPACES_CFG, scope_kingdoms=["Nonexistia"], scope_roots=[1]))


def _stub_download(monkeypatch: pytest.MonkeyPatch, staged: dict[str, list[dict]]) -> None:
    schemas = {"taxa.parquet": taxa_bulk._TAXA_SCHEMA, "names.parquet": taxa_bulk._NAMES_SCHEMA}

    def download(cfg: Spaces, key: str, dest_path: str) -> None:
        filename = key.rsplit("/", 1)[-1]
        buf = pa.BufferOutputStream()
        with pq.ParquetWriter(buf, schemas[filename]) as writer:
            writer.write_table(pa.Table.from_pylist(staged[filename], schema=schemas[filename]))
        Path(dest_path).write_bytes(buf.getvalue().to_pybytes())

    monkeypatch.setattr(taxa_bulk.spaces, "download_file", download)


def test_load_upserts_taxa_names_and_derives_iconic_taxon(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_download(monkeypatch, _stage(monkeypatch))

    load_taxa(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    row = con.execute(
        "SELECT rank, parent_id, ancestor_ids, iconic_taxon_id, is_active FROM taxa WHERE taxon_id = %s", [SPECIES]
    ).fetchone()
    assert row == ("species", SECTION, [FUNGI, CLASS, ORDER, FAMILY, GENUS, SECTION], FUNGI, True)
    assert con.execute("SELECT count(*) FROM taxon_names").fetchone() == (4,)
    assert con.execute(
        "SELECT rank_level FROM taxa WHERE taxon_id IN (%s, %s) ORDER BY taxon_id", [GENUS, SECTION]
    ).fetchall() == [
        (13,),
        (20,),
    ]


def test_load_fills_a_missing_english_name_but_never_replaces_the_api_one(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_taxa(con, [{"taxon_id": GENUS, "name": "Amanita", "rank": "genus", "common_name": "Amanitas (API)"}])
    _stub_download(monkeypatch, _stage(monkeypatch))

    load_taxa(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    names = dict(
        con.execute("SELECT taxon_id, common_name FROM taxa WHERE taxon_id IN (%s, %s)", [GENUS, SPECIES]).fetchall()
    )
    assert names[GENUS] == "Amanitas (API)"
    assert names[SPECIES] == "Fly Agaric"  # the alphabetically-first English name


def test_load_retires_scope_taxa_absent_from_the_snapshot_and_is_idempotent(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_taxa(
        con,
        [
            {"taxon_id": 31337, "name": "Retiredia", "rank": "genus", "ancestor_ids": [FUNGI]},
            {"taxon_id": 424242, "name": "Elsewhere", "rank": "genus", "ancestor_ids": [1, 47158]},  # old scope
        ],
    )
    _stub_download(monkeypatch, _stage(monkeypatch))

    for _ in range(2):  # the second pass changes nothing
        load_taxa(con, Settings(spaces=_SPACES_CFG), date(2026, 1, 1), "run1")

    # The snapshot is the whole catalog for the scope: a leftover from a narrowed scope is retired too.
    active = dict(
        con.execute("SELECT taxon_id, is_active FROM taxa WHERE taxon_id IN (31337, 424242, %s)", [GENUS]).fetchall()
    )
    assert active == {31337: False, 424242: False, GENUS: True}
    assert con.execute("SELECT count(*) FROM taxa").fetchone() == (10,)
    assert con.execute("SELECT count(*) FROM taxon_names").fetchone() == (4,)


def test_taxa_is_a_registered_source() -> None:
    from foray import ingest_bulk

    assert ingest_bulk.STAGERS["taxa"] is taxa_bulk.stage_taxa
    assert ingest_bulk.LOADERS["taxa"] is taxa_bulk.load_taxa
