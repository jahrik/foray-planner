"""The taxon catalog as a bulk source (issue #464): iNat's weekly taxonomy export, staged by GitHub
Actions and loaded on the droplet like the other ``ingest_bulk`` sources.

``https://www.inaturalist.org/taxa/inaturalist-taxonomy.dwca.zip`` (~80 MB, regenerated about
weekly) holds ``taxa.csv`` (every iNat taxon, 1.4M of them: ``id``, ``parentNameUsageID`` as a URL,
the kingdom..genus *names*, ``scientificName``, ``taxonRank``) and one ``VernacularNames-<language>.csv``
per language. It has **no ancestor ids**, so the stager rebuilds each taxon's path by walking
``parentNameUsageID`` (:func:`foray.taxa.build_ancestor_ids`; a handful of parent refs dangle, so a
broken chain keeps its partial path), and it has no ``observations_count`` / ``is_active`` /
iconic taxon: counts keep coming from ``foray genera-refresh``, a taxon missing from a newer
snapshot is the only removal signal, and the iconic group is derived from the lineage.

The stager has no database, so it narrows to the scope by the export's *kingdom name* column
(``Settings.scope_kingdoms``) and keeps every taxon under a scope root plus the ancestors of those
roots; the loader's ``mark_taxa_inactive`` then applies the exact root test. Ranks between species
and genus (section, subgenus, subsection, complex) are staged as the taxa they are: a section can
share a genus's name (the *Morchella* section vs. the genus), which is why :mod:`foray.taxa` walks
the chain by rank and never by name.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import psycopg
import pyarrow as pa
from stream_unzip import stream_unzip

from foray import spaces
from foray.cache import (
    bump_taxa_version,
    mark_taxa_inactive,
    upsert_taxa,
    upsert_taxon_names,
)
from foray.config import Settings
from foray.scoring import rank_cache
from foray.sources.http import USER_AGENT
from foray.sources.inat_bulk import iter_csv_lines
from foray.taxa import TaxonNode, build_ancestor_ids, iconic_taxon_id, ids_under_scope

logger = logging.getLogger(__name__)

TAXONOMY_URL = "https://www.inaturalist.org/taxa/inaturalist-taxonomy.dwca.zip"

_TAXA_FILENAME = "taxa.parquet"
_NAMES_FILENAME = "names.parquet"
_TAXA_SCHEMA = pa.schema(
    [
        ("taxon_id", pa.int64()),
        ("parent_id", pa.int64()),
        ("name", pa.string()),
        ("rank", pa.string()),
        ("ancestor_ids", pa.list_(pa.int64())),
    ]
)
_NAMES_SCHEMA = pa.schema([("taxon_id", pa.int64()), ("name", pa.string()), ("lexicon", pa.string())])

_STREAM_CHUNK_SIZE = 1024 * 1024
_BATCH_SIZE = 5000
_VERNACULAR_PREFIX = b"VernacularNames-"


def _taxon_id_from_url(url: str) -> int | None:
    """The trailing numeric id of an iNat taxon URL (``https://www.inaturalist.org/taxa/47170``)."""
    tail = url.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _read_taxa(
    rows: Iterator[list[str]], header: list[str], kingdoms: set[str]
) -> dict[int, tuple[int | None, str, str]]:
    """``id -> (parent_id, rank, name)`` for every row in one of ``kingdoms`` (the export's own kingdom
    column, so no other kingdom's 1.3M rows are ever held)."""
    col_id, col_parent = header.index("id"), header.index("parentNameUsageID")
    col_kingdom, col_name, col_rank = header.index("kingdom"), header.index("scientificName"), header.index("taxonRank")
    width = len(header)
    taxa: dict[int, tuple[int | None, str, str]] = {}
    for row in rows:
        if len(row) != width or row[col_kingdom] not in kingdoms:
            continue
        taxa[int(row[col_id])] = (_taxon_id_from_url(row[col_parent]), row[col_rank], row[col_name])
    return taxa


def scoped_taxa(taxa: dict[int, tuple[int | None, str, str]], scope_roots: list[int]) -> list[dict[str, Any]]:
    """Staged taxa rows: every taxon under a scope root, plus the ancestors of those roots (so a
    non-kingdom root still has its lineage), each with its rebuilt ``ancestor_ids``."""
    nodes = [TaxonNode(taxon_id, parent_id, rank) for taxon_id, (parent_id, rank, _) in taxa.items()]
    lineage = build_ancestor_ids(nodes)
    keep: set[int] = set()
    for taxon_id, ancestors in lineage.items():
        if ids_under_scope(taxon_id, ancestors, scope_roots):
            keep.add(taxon_id)
    for root in scope_roots:
        if root in lineage:
            keep.update(lineage[root])
    if unreachable := len(taxa) - len(keep):
        # In the right kingdom by name, but not on a chain that reaches a scope root (a parent
        # reference the export leaves dangling, ~15 file-wide) or sitting outside every root.
        logger.info("taxa_bulk: %d taxa in the scope kingdoms are not under a scope root, left out", unreachable)
    return [
        {
            "taxon_id": taxon_id,
            "parent_id": taxa[taxon_id][0],
            "name": taxa[taxon_id][2],
            "rank": taxa[taxon_id][1],
            "ancestor_ids": lineage[taxon_id],
        }
        for taxon_id in sorted(keep)
    ]


def iter_taxonomy(client: httpx.Client, kingdoms: set[str]) -> Iterator[tuple[str, Any]]:
    """Stream the export once, yielding ``("taxa", {id: (parent, rank, name)})`` after ``taxa.csv``
    and ``("names", [(id, name, lexicon), ...])`` for each vernacular file that follows it (the
    archive lists ``taxa.csv`` first, so the taxon set is known before any names are read)."""
    with client.stream("GET", TAXONOMY_URL) as response:
        response.raise_for_status()
        byte_chunks = response.iter_bytes(chunk_size=_STREAM_CHUNK_SIZE)
        for file_name, _size, chunks in stream_unzip(byte_chunks):
            if file_name == b"taxa.csv":
                reader = csv.reader(iter_csv_lines(chunks))
                header = next(reader)
                yield "taxa", _read_taxa(reader, header, kingdoms)
            elif file_name.startswith(_VERNACULAR_PREFIX):
                reader = csv.reader(iter_csv_lines(chunks))
                header = next(reader)
                col_id, col_name = header.index("id"), header.index("vernacularName")
                col_lexicon = header.index("lexicon")
                width = len(header)
                rows = [
                    (int(row[col_id]), row[col_name], row[col_lexicon])
                    for row in reader
                    if len(row) == width and row[col_id].isdigit() and row[col_name]
                ]
                yield "names", rows
            else:
                for _ in chunks:  # stream_unzip requires every entry's chunks drained
                    pass


def stage_taxa(cfg: Settings, snapshot_date: date, run_id: str) -> None:
    """Stager: stream the taxonomy export, keep the scoped taxa (with rebuilt ancestor paths) and
    their names, and upload both as Parquet under this run's Space prefix. No database; runs in
    GitHub Actions."""
    kingdoms = set(cfg.scope_kingdoms)
    staged: list[dict[str, Any]] = []
    staged_ids: set[int] = set()
    names: list[dict[str, Any]] = []
    with httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT}) as client:
        for kind, payload in iter_taxonomy(client, kingdoms):
            if kind == "taxa":
                staged = scoped_taxa(payload, cfg.scope_roots)
                staged_ids = {row["taxon_id"] for row in staged}
                logger.info("taxa_bulk: %d taxa in scope (of %d in %s)", len(staged), len(payload), sorted(kingdoms))
            else:
                names.extend(
                    {"taxon_id": taxon_id, "name": name, "lexicon": lexicon}
                    for taxon_id, name, lexicon in payload
                    if taxon_id in staged_ids
                )
    if not staged:
        raise RuntimeError(f"taxonomy export held no taxa for kingdoms {sorted(kingdoms)} - not publishing")
    spaces.write_snapshot_parquet(cfg.spaces, "taxa", snapshot_date, run_id, _TAXA_FILENAME, staged, _TAXA_SCHEMA)
    spaces.write_snapshot_parquet(cfg.spaces, "taxa", snapshot_date, run_id, _NAMES_FILENAME, names, _NAMES_SCHEMA)
    logger.info("taxa_bulk: staged %d taxa and %d names", len(staged), len(names))


def load_taxa(con: psycopg.Connection, cfg: Settings, snapshot_date: date, run_id: str) -> None:
    """Loader: upsert the staged taxa and names, flag scope taxa the snapshot no longer lists as
    inactive, and give taxa still lacking an English name the first one the export has.

    Additive and idempotent: a taxon's ``observations_count`` and API-sourced ``common_name`` are
    never overwritten (the export has neither). A scope taxon absent from the snapshot goes
    ``is_active = false`` (iNat retired / merged it; ``resync`` heals the cached rows)."""
    loaded: set[int] = set()
    total = 0
    for batch in spaces.read_snapshot_parquet(
        cfg.spaces, "taxa", snapshot_date, run_id, _TAXA_FILENAME, batch_size=_BATCH_SIZE
    ):
        rows = [
            {
                **record,
                "iconic_taxon_id": iconic_taxon_id(record["taxon_id"], record["ancestor_ids"]),
            }
            for record in batch
        ]
        upsert_taxa(con, rows)
        loaded.update(record["taxon_id"] for record in batch)
        total += len(batch)
    named = 0
    for batch in spaces.read_snapshot_parquet(
        cfg.spaces, "taxa", snapshot_date, run_id, _NAMES_FILENAME, batch_size=_BATCH_SIZE
    ):
        named += upsert_taxon_names(
            con, [(record["taxon_id"], record["name"], record["lexicon"], True) for record in batch]
        )
    con.execute(
        """
        UPDATE taxa SET common_name = english.name
        FROM (
            SELECT DISTINCT ON (taxon_id) taxon_id, name FROM taxon_names
            WHERE lexicon = 'English' ORDER BY taxon_id, name
        ) english
        WHERE taxa.taxon_id = english.taxon_id AND taxa.common_name IS NULL
        """
    )
    # A fresh bulk load leaves the planner without statistics: the first searches would seq-scan.
    con.execute("ANALYZE taxa")
    con.execute("ANALYZE taxon_names")
    retired = mark_taxa_inactive(con, cfg.scope_roots, loaded) if loaded else 0
    bump_taxa_version(con)
    # Cached target expansions / ranked results were built from the previous catalog.
    rank_cache.invalidate()
    logger.info("taxa_bulk: loaded %d taxa, %d names, %d retired", total, named, retired)
