"""The rank-agnostic taxon catalog (issue #464): ``taxa`` (every rank, species to kingdom),
``taxon_names`` (synonyms / common names) and the target expansion that turns a device's picked
taxa into the genus / species ids the scoring queries filter on.

Replaces the genus-only ``fungi_genera`` table (issue #79); that name survives as a view over
``taxa`` for a stale cron image. See :mod:`foray.taxa` for the rank-driven lineage helpers.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Collection, Iterable, Sequence
from typing import Any, NamedTuple

import psycopg

from foray.cache.core import copy_upsert
from foray.config import scope_roots
from foray.genus_icons import genus_icon
from foray.taxa import GENUS_RANK, SEARCHABLE_RANKS, SPECIES_RANK, Rollup, rank_level, rollup_for

_TAXA_COLUMNS = (
    "taxon_id",
    "parent_id",
    "name",
    "common_name",
    "rank",
    "rank_level",
    "ancestor_ids",
    "iconic_taxon_id",
    "is_active",
    "observations_count",
    "refreshed_at",
)


def upsert_taxa(con: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Upsert taxa rows (``taxon_id``, ``name``, ``rank`` required; ``ancestor_ids`` defaults to
    ``[]``). A NULL ``common_name`` / ``observations_count`` never blanks a stored value - the
    taxonomy export carries neither count nor (for most taxa) an English name, and the API refresh
    fills them. A re-upserted taxon becomes active again. Bumps the taxa version so cached target
    expansions are rebuilt. Returns rows attempted."""
    now = dt.datetime.now(dt.UTC)
    tuples = [
        (
            row["taxon_id"],
            row.get("parent_id"),
            row["name"],
            row.get("common_name"),
            row["rank"],
            rank_level(row["rank"]),
            list(row.get("ancestor_ids") or ()),
            row.get("iconic_taxon_id"),
            True,
            row.get("observations_count"),
            now,
        )
        for row in rows
    ]
    if not tuples:
        return 0
    count = copy_upsert(
        con,
        "taxa",
        _TAXA_COLUMNS,
        tuples,
        conflict="taxon_id",
        coalesce={"common_name", "iconic_taxon_id", "observations_count"},
    )
    bump_taxa_version(con)
    return count


def upsert_taxon_names(con: psycopg.Connection, rows: Iterable[tuple[int, str, str, bool]]) -> int:
    """Upsert ``(taxon_id, name, lexicon, is_valid)`` synonym / common-name rows. Returns rows attempted."""
    batch = list(rows)
    if not batch:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO taxon_names (taxon_id, name, lexicon, is_valid) VALUES (%s, %s, %s, %s)
            ON CONFLICT (taxon_id, name, lexicon) DO UPDATE SET is_valid = EXCLUDED.is_valid
            """,
            batch,
        )
    return len(batch)


def mark_taxa_inactive(con: psycopg.Connection, scope_roots: Collection[int], keep_ids: Collection[int]) -> int:
    """Flag every taxon under a scope root that is absent from ``keep_ids`` (the new snapshot) as
    inactive - the export has no ``is_active`` column, so a taxon missing from it is the only
    removal signal. Returns how many rows flipped."""
    cur = con.execute(
        """
        UPDATE taxa SET is_active = FALSE
        WHERE is_active AND NOT (taxon_id = ANY(%s))
          AND (taxon_id = ANY(%s) OR ancestor_ids && %s::bigint[])
        """,
        [list(keep_ids), list(scope_roots), list(scope_roots)],
    )
    if cur.rowcount:
        bump_taxa_version(con)
    return cur.rowcount


def taxa_count(con: psycopg.Connection) -> int:
    """How many active taxa are cached (the "``taxa`` is empty" guard for ingest / the bulk loaders)."""
    row = con.execute("SELECT count(*) FROM taxa WHERE is_active").fetchone()
    return int(row[0]) if row else 0


def rollup_map(con: psycopg.Connection) -> dict[int, Rollup]:
    """``taxon_id -> (genus_id, species_id)`` for every active cached taxon, rolled up by rank
    (:func:`foray.taxa.rollup_for`). An observation's own ``taxonID`` resolves through this: the
    genus goes to ``observations.taxon_id``, the species (if any) to ``species_id``. A taxon coarser
    than genus maps to ``Rollup(None, None)``."""
    rows = con.execute("SELECT taxon_id, rank, ancestor_ids FROM taxa WHERE is_active").fetchall()
    ranks = {taxon_id: rank for taxon_id, rank, _ in rows}
    # ancestors outside the active set (inactive) still carry a rank for the walk
    inactive = con.execute("SELECT taxon_id, rank FROM taxa WHERE NOT is_active").fetchall()
    ranks.update(inactive)
    return {taxon_id: rollup_for(taxon_id, ancestors, ranks) for taxon_id, _, ancestors in rows}


def cached_taxon_ranks(con: psycopg.Connection) -> dict[int, str]:
    """``taxon_id -> rank`` for every cached taxon (active or not: a retired ancestor still has a
    rank for the lineage walk). Empty until the catalog is loaded."""
    return dict(con.execute("SELECT taxon_id, rank FROM taxa").fetchall())


def genus_name_ids(con: psycopg.Connection) -> dict[str, int]:
    """Genus name -> taxon_id over active genus-rank taxa: only for the old-snapshot fallback of the
    bulk observation loader (a snapshot staged before it carried ``taxon_id``). A name shared by two
    genera is dropped rather than guessed - those rows are skipped, as unknown, until re-staged."""
    names: dict[str, int] = {}
    ambiguous: set[str] = set()
    for taxon_id, name in con.execute("SELECT taxon_id, name FROM taxa WHERE rank = %s AND is_active", [GENUS_RANK]):
        if name in names:
            ambiguous.add(name)
        names[name] = taxon_id
    return {name: taxon_id for name, taxon_id in names.items() if name not in ambiguous}


def known_genus_taxon_ids(con: psycopg.Connection) -> set[int]:
    """The set of active genus-rank taxon ids (the live ingest's genus-ancestry resolver)."""
    rows = con.execute("SELECT taxon_id FROM taxa WHERE rank = %s AND is_active", [GENUS_RANK]).fetchall()
    return {taxon_id for (taxon_id,) in rows}


# --- taxa version + target expansion -------------------------------------------------------


def bump_taxa_version(con: psycopg.Connection) -> None:
    """Mark the catalog changed, so per-process target-expansion caches rebuild."""
    con.execute(
        "INSERT INTO meta (key, value) VALUES ('taxa_version', '1') ON CONFLICT (key) DO UPDATE "
        "SET value = (meta.value::bigint + 1)::text"
    )


def _taxa_version(con: psycopg.Connection) -> str:
    row = con.execute("SELECT value FROM meta WHERE key = 'taxa_version'").fetchone()
    return row[0] if row else "0"


class Targets(NamedTuple):
    """A device's picked taxa expanded to what the queries filter on: genus ids (match
    ``observations.taxon_id`` / ``phenology``), species ids (match ``observations.species_id`` /
    ``phenology_species``), and whether the pick covers the whole scope (no filter at all)."""

    genus_ids: tuple[int, ...]
    species_ids: tuple[int, ...]
    covers_all: bool


ALL_TARGETS = Targets((), (), True)

_EXPANSION_CACHE_MAX = 512
_expansion_cache: dict[tuple[str, tuple[int, ...], tuple[int, ...]], Targets] = {}
_expansion_lock = threading.Lock()


def expand_targets(
    con: psycopg.Connection,
    target_ids: Sequence[int],
    scope_roots: Collection[int] = (),
) -> Targets:
    """Expand picked taxon ids of any rank into :class:`Targets`.

    * empty selection, or a pick that is a scope root -> ``covers_all`` (no filter);
    * a species -> its id in ``species_ids``; a genus -> ``genus_ids``;
    * a family / order / class / ... -> every active descendant **genus** (their ``phenology``
      rows sum to the group's);
    * an id missing from ``taxa`` is kept as a genus id - the pre-#464 meaning of a bare
      ``taxon_id`` - so an observation-only fixture (or a not-yet-catalogued genus) still filters;
    * a species whose genus is also picked is dropped (the genus already counts it).

    Cached per ``(taxa version, ids)`` so a request's several ranking queries expand once.
    """
    ids = tuple(sorted(set(target_ids)))
    if not ids:
        return ALL_TARGETS
    roots = tuple(sorted(set(scope_roots)))
    key = (_taxa_version(con), ids, roots)
    with _expansion_lock:
        cached = _expansion_cache.get(key)
    if cached is not None:
        return cached
    result = _expand(con, ids, set(roots))
    with _expansion_lock:
        if len(_expansion_cache) >= _EXPANSION_CACHE_MAX:
            _expansion_cache.clear()
        _expansion_cache[key] = result
    return result


def clear_expansion_cache() -> None:
    """Drop every cached target expansion (tests that write ``taxa`` directly, without the version bump)."""
    with _expansion_lock:
        _expansion_cache.clear()


def as_targets(con: psycopg.Connection, picked: Sequence[int] | Targets) -> Targets:
    """``picked`` as :class:`Targets`: already-expanded targets pass through, a list of picked taxon
    ids expands against the configured scope roots. The scoring functions take either, so the API
    can expand once per request and a caller with plain ids (the CLI, tests) need not."""
    if isinstance(picked, Targets):
        return picked
    return expand_targets(con, picked, scope_roots())


def _expand(con: psycopg.Connection, ids: tuple[int, ...], roots: set[int]) -> Targets:
    if roots.intersection(ids):
        return ALL_TARGETS
    rows = con.execute(
        "SELECT taxon_id, rank, ancestor_ids FROM taxa WHERE taxon_id = ANY(%s) AND is_active", [list(ids)]
    ).fetchall()
    found = {taxon_id: (rank, ancestors) for taxon_id, rank, ancestors in rows}
    genus_ids: set[int] = set()
    species_ids: set[int] = set()
    group_ids: list[int] = []
    for taxon_id in ids:
        entry = found.get(taxon_id)
        if entry is None or entry[0] == GENUS_RANK:
            genus_ids.add(taxon_id)
        elif entry[0] == SPECIES_RANK:
            species_ids.add(taxon_id)
        else:
            group_ids.append(taxon_id)
    if group_ids:
        descendants = con.execute(
            "SELECT taxon_id FROM taxa WHERE rank = %s AND is_active AND ancestor_ids && %s::bigint[]",
            [GENUS_RANK, group_ids],
        ).fetchall()
        genus_ids.update(taxon_id for (taxon_id,) in descendants)
    if species_ids and genus_ids:
        # A species under an already-picked genus (or group) is counted by the genus rows.
        covered = con.execute(
            "SELECT taxon_id FROM taxa WHERE taxon_id = ANY(%s) AND ancestor_ids && %s::bigint[]",
            [list(species_ids), list(genus_ids)],
        ).fetchall()
        species_ids.difference_update(taxon_id for (taxon_id,) in covered)
    return Targets(tuple(sorted(genus_ids)), tuple(sorted(species_ids)), False)


# --- labels + search -----------------------------------------------------------------------


def taxon_labels(con: psycopg.Connection, taxon_ids: Collection[int]) -> dict[int, dict[str, Any]]:
    """``taxon_id -> {name, common_name, rank, icon}`` for the given ids. The icon is the taxon's
    genus's (a species shares its genus's), or the shape group its family / order / class gives
    when the taxon is itself coarser than genus. Ids absent from ``taxa`` are omitted."""
    if not taxon_ids:
        return {}
    rows = con.execute(
        "SELECT taxon_id, name, common_name, rank, ancestor_ids FROM taxa WHERE taxon_id = ANY(%s)",
        [list(taxon_ids)],
    ).fetchall()
    ancestor_ids = {ancestor for *_, ancestors in rows for ancestor in ancestors}
    ancestor_ranks: dict[int, tuple[str, str]] = {}
    if ancestor_ids:
        ancestor_ranks = {
            taxon_id: (rank, name)
            for taxon_id, rank, name in con.execute(
                "SELECT taxon_id, rank, name FROM taxa WHERE taxon_id = ANY(%s) AND rank = ANY(%s)",
                [list(ancestor_ids), [GENUS_RANK, "family", "order", "class"]],
            ).fetchall()
        }
    labels: dict[int, dict[str, Any]] = {}
    for taxon_id, name, common_name, rank, ancestors in rows:
        by_rank = {
            ancestor_ranks[ancestor][0]: ancestor_ranks[ancestor][1]
            for ancestor in ancestors
            if ancestor in ancestor_ranks
        }
        genus_name = name if rank == GENUS_RANK else by_rank.get(GENUS_RANK, "")
        labels[taxon_id] = {
            "name": name,
            "common_name": common_name,
            "rank": rank,
            "icon": genus_icon(genus_name, by_rank.get("family"), by_rank.get("order"), by_rank.get("class")),
        }
    return labels


def search_taxa(
    con: psycopg.Connection, query: str, *, rank: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Search the catalog by scientific name, common name or synonym, optionally within one rank.

    Ranked: exact name, then prefix, then substring; ties by iNat's observation count. A hit found
    through a synonym / vernacular name carries it as ``matched_name``. An empty query returns the
    most-observed taxa of ``rank`` (genera by default) - a sane browse list, not the whole catalog.
    """
    if rank is not None and rank not in SEARCHABLE_RANKS:
        raise ValueError(f"unsearchable rank {rank!r}")
    stripped = query.strip().lower()
    rank_clause = "AND t.rank = %s" if rank else "AND t.rank = ANY(%s)"
    rank_param: Any = rank or list(SEARCHABLE_RANKS)
    if not stripped:
        rows = con.execute(
            """
            SELECT t.taxon_id, NULL::text FROM taxa t
            WHERE t.is_active AND t.rank = %s
            ORDER BY t.observations_count DESC NULLS LAST, t.name
            LIMIT %s
            """,
            [rank or GENUS_RANK, limit],
        ).fetchall()
    else:
        rows = con.execute(
            f"""
            WITH hits AS (
                SELECT t.taxon_id, NULL::text AS matched, lower(t.name) AS hit_name
                FROM taxa t WHERE t.is_active {rank_clause} AND lower(t.name) LIKE %s
                UNION ALL
                SELECT t.taxon_id, NULL::text, lower(t.common_name)
                FROM taxa t WHERE t.is_active {rank_clause} AND lower(t.common_name) LIKE %s
                UNION ALL
                SELECT n.taxon_id, n.name, lower(n.name)
                FROM taxon_names n JOIN taxa t USING (taxon_id)
                WHERE t.is_active {rank_clause} AND lower(n.name) LIKE %s
            ), best AS (
                SELECT DISTINCT ON (taxon_id) taxon_id, matched,
                       CASE WHEN hit_name = %s THEN 0 WHEN hit_name LIKE %s THEN 1 ELSE 2 END AS tier
                FROM hits ORDER BY taxon_id, 3, (matched IS NOT NULL)
            )
            SELECT b.taxon_id, b.matched FROM best b JOIN taxa t USING (taxon_id)
            ORDER BY b.tier, t.rank_level DESC, t.observations_count DESC NULLS LAST, t.name
            LIMIT %s
            """,
            [
                rank_param,
                f"%{stripped}%",
                rank_param,
                f"%{stripped}%",
                rank_param,
                f"%{stripped}%",
                stripped,
                f"{stripped}%",
                limit,
            ],
        ).fetchall()
    labels = taxon_labels(con, [taxon_id for taxon_id, _ in rows])
    return [
        {"taxon_id": taxon_id, **labels[taxon_id], "matched_name": matched}
        for taxon_id, matched in rows
        if taxon_id in labels
    ]
