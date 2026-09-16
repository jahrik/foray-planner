"""Fungi genus catalog (issue #79): the full ~6,018-genus reference table, refreshed
weekly by `foray genera-refresh` - see `foray.sources.inat.iter_fungi_genera`."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import psycopg


def upsert_fungi_genera(con: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> None:
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fungi_genera (taxon_id, name, common_name, observations_count)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (taxon_id) DO UPDATE SET
                name = EXCLUDED.name,
                common_name = EXCLUDED.common_name,
                observations_count = EXCLUDED.observations_count
            """,
            [(row["taxon_id"], row["name"], row.get("common_name"), row.get("observations_count")) for row in rows],
        )


def genus_taxon_ids(con: psycopg.Connection) -> dict[str, int]:
    """Full genus-name -> taxon_id map from the catalog (issue #79 Phase 3: the bulk loader
    matches every catalog genus now, not just the old 21-genus seed list).

    ``name`` has no uniqueness constraint in the schema, so this checks for duplicates
    rather than silently keeping whichever row happens to win a dict build - a silent drop
    here would make the bulk iNat loader (``foray.sources.inat_bulk``) quietly skip that
    genus's observations with no error to explain why.
    """
    rows = con.execute("SELECT name, taxon_id FROM fungi_genera").fetchall()
    genera: dict[str, int] = {}
    for name, taxon_id in rows:
        if name in genera:
            raise ValueError(f"fungi_genera has duplicate name {name!r} (taxon_ids {genera[name]} and {taxon_id})")
        genera[name] = taxon_id
    return genera


def known_genus_taxon_ids(con: psycopg.Connection) -> set[int]:
    """The full set of catalog taxon_ids, for callers (ingest.py's genus-ancestry resolver)
    that only need membership, not the name map - unlike ``genus_taxon_ids()``, this never
    raises on a duplicate ``name`` (irrelevant here; taxon_id is already the schema's PK)."""
    rows = con.execute("SELECT taxon_id FROM fungi_genera").fetchall()
    return {taxon_id for (taxon_id,) in rows}


def search_fungi_genera(con: psycopg.Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Genus catalog search by scientific or common name, ranked by iNat's observation count.

    Empty ``query`` returns the most-observed genera (a sane browse default), not everything -
    the catalog has ~6,018 rows, too many to dump into a dropdown unfiltered.
    """
    stripped = query.strip()
    if stripped:
        rows = con.execute(
            """
            SELECT taxon_id, name, common_name
            FROM fungi_genera
            WHERE name ILIKE %s OR common_name ILIKE %s
            ORDER BY observations_count DESC NULLS LAST, name
            LIMIT %s
            """,
            [f"%{stripped}%", f"%{stripped}%", limit],
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT taxon_id, name, common_name
            FROM fungi_genera
            ORDER BY observations_count DESC NULLS LAST, name
            LIMIT %s
            """,
            [limit],
        ).fetchall()
    return [{"taxon_id": taxon_id, "name": name, "common_name": common_name} for taxon_id, name, common_name in rows]
