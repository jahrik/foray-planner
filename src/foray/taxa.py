"""Rank-agnostic taxonomy helpers (issue #464): pure functions, no database.

iNaturalist models every rank in one ``taxa`` table, each row carrying its ``rank`` and an
ancestor path. The cached ``taxa`` table here mirrors that. Everything below is deliberately
rank-driven rather than name- or nearest-parent-driven: a section can share a genus's name
(*Morchella* section vs. the genus), and species sit under subgenus / section / subsection /
complex nodes, so "the genus above this taxon" is the ancestor whose **rank** is ``genus``.
"""

from __future__ import annotations

from collections import ChainMap
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

# iNat's numeric rank levels (lower = finer). Used to order ranks and to pick which node is "the"
# species / genus; ``rank_level`` is stored on ``taxa`` so a query can say "genus or coarser".
RANK_LEVELS: dict[str, int] = {
    "stateofmatter": 100,
    "kingdom": 70,
    "phylum": 60,
    "subphylum": 57,
    "superclass": 53,
    "class": 50,
    "subclass": 47,
    "superorder": 45,
    "order": 40,
    "suborder": 37,
    "superfamily": 33,
    "epifamily": 32,
    "family": 30,
    "subfamily": 27,
    "supertribe": 26,
    "tribe": 25,
    "subtribe": 24,
    "genus": 20,
    "genushybrid": 20,
    "subgenus": 15,
    "section": 13,
    "subsection": 12,
    "complex": 11,
    "species": 10,
    "hybrid": 10,
    "subspecies": 5,
    "variety": 5,
    "form": 5,
    "infrahybrid": 5,
}

# The ranks a user can search and target: species up to class (the issue's range), plus the
# coarser kingdom / phylum a target could in principle name.
SEARCHABLE_RANKS: tuple[str, ...] = ("species", "genus", "family", "order", "class", "phylum", "kingdom")

# iNat's "iconic taxa" - the fixed coarse groups it files every taxon under. The taxonomy export has no
# iconic_taxon_id column, so a taxon's is the first of these on its lineage (self included).
ICONIC_TAXON_IDS: frozenset[int] = frozenset(
    {1, 3, 20978, 26036, 40151, 47115, 47119, 47126, 47158, 47170, 47178, 47686, 48222}
)

GENUS_RANK = "genus"
SPECIES_RANK = "species"


def rank_level(rank: str | None) -> int | None:
    """iNat's numeric level for ``rank`` (``None`` for an unknown / missing rank)."""
    return RANK_LEVELS.get(rank) if rank else None


class TaxonNode(NamedTuple):
    """The slice of a taxon the lineage walk needs."""

    taxon_id: int
    parent_id: int | None
    rank: str


def build_ancestor_ids(nodes: Iterable[TaxonNode]) -> dict[int, list[int]]:
    """``taxon_id -> [root, ..., parent]`` by walking each node's ``parent_id`` chain.

    The taxonomy export has parent refs only (no ancestor ids) and a handful of them dangle, so
    a chain that runs off the known set just stops there (the partial path is kept) instead of
    failing. A cycle is cut at the repeated node rather than looping forever.
    """
    by_id = {node.taxon_id: node for node in nodes}
    cache: dict[int, list[int]] = {}

    def lineage(taxon_id: int) -> list[int]:
        if taxon_id in cache:
            return cache[taxon_id]
        path: list[int] = []
        seen = {taxon_id}
        current = by_id[taxon_id].parent_id
        while current is not None and current in by_id and current not in seen:
            path.append(current)
            seen.add(current)
            # A parent whose own path is already known finishes this one in one step.
            if current in cache:
                path.extend(ancestor for ancestor in cache[current][::-1] if ancestor not in seen)
                break
            current = by_id[current].parent_id
        path.reverse()
        cache[taxon_id] = path
        return path

    return {taxon_id: lineage(taxon_id) for taxon_id in by_id}


class Rollup(NamedTuple):
    """Where an observation's own identification lands: the genus it counts toward (hot key
    ``observations.taxon_id``) and, if it was identified to species or finer, the species."""

    genus_id: int | None
    species_id: int | None


def rollup_for(
    taxon_id: int,
    ancestor_ids: Iterable[int],
    ranks: Mapping[int, str],
) -> Rollup:
    """Roll a taxon up to its genus and species by walking its chain **by rank**.

    ``ancestor_ids`` is root-to-parent. Infraspecific ranks (subspecies, variety, form,
    hybrid-of-species) roll up to their species; a genus-only identification has no species; an
    identification coarser than genus has neither. The chain is scanned nearest-first, so a
    same-named section never shadows the real genus and a subgenus / section between species and
    genus is stepped over.
    """
    chain = [*ancestor_ids, taxon_id]
    genus_id: int | None = None
    species_id: int | None = None
    for node_id in reversed(chain):
        rank = ranks.get(node_id)
        if species_id is None and rank == SPECIES_RANK:
            species_id = node_id
        elif genus_id is None and rank == GENUS_RANK:
            genus_id = node_id
        if genus_id is not None:
            break
    return Rollup(genus_id, species_id)


def ids_under_scope(
    taxon_id: int,
    ancestor_ids: Iterable[int],
    scope_roots: Iterable[int],
) -> bool:
    """True when ``taxon_id`` is one of ``scope_roots`` or sits beneath one (the "still under a
    scope root" test that replaced "still Fungi")."""
    roots = set(scope_roots)
    return taxon_id in roots or any(ancestor in roots for ancestor in ancestor_ids)


class Resolver:
    """Resolve a live iNat observation to the genus (+ species) it counts toward, within scope.

    ``ranks`` maps every cataloged taxon id to its rank (from ``taxa``); ``scope_roots`` are the
    configured root taxa. An observation resolves only when its identification sits under a scope
    root (this replaced the hardcoded "iconic taxon is Fungi" test: the check is on the live
    response's own lineage, so a fungal-homonym animal never passes) and rolls up to a genus by
    rank. Anything coarser than genus, or outside scope, resolves to ``None``.
    """

    def __init__(self, ranks: dict[int, str], scope_roots: Iterable[int]) -> None:
        self.ranks = ranks
        self.scope_roots = frozenset(scope_roots)

    def resolve(self, obs: Mapping[str, Any]) -> Rollup | None:
        """The ``Rollup`` of ``obs["taxon"]``, or ``None`` when out of scope / above genus."""
        taxon = obs.get("taxon") or {}
        taxon_id = taxon.get("id")
        if taxon_id is None:
            return None
        # The API's ``ancestor_ids`` is kingdom -> self; drop the trailing self.
        ancestors = [ancestor for ancestor in taxon.get("ancestor_ids") or () if ancestor != taxon_id]
        if not ids_under_scope(taxon_id, ancestors, self.scope_roots):
            return None
        own_rank = taxon.get("rank")
        ranks: Mapping[int, str] = ChainMap({taxon_id: own_rank}, self.ranks) if own_rank else self.ranks
        rollup = rollup_for(taxon_id, ancestors, ranks)
        return rollup if rollup.genus_id is not None else None


def taxa_rows(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``taxa`` rows from iNat ``/v1/taxa`` records (``foray genera-refresh``).

    The API gives each taxon its ``ancestor_ids`` (kingdom first; it ends at the taxon itself, which
    is dropped), ``parent_id``, rank, observation count and preferred English name directly, so this
    is a field mapping - the lineage walk :func:`build_ancestor_ids` is only for the taxonomy export,
    which lacks ancestor ids.
    """
    return [
        {
            "taxon_id": record["id"],
            "parent_id": record.get("parent_id"),
            "name": record["name"],
            "common_name": record.get("preferred_common_name"),
            "rank": record["rank"],
            "ancestor_ids": [ancestor for ancestor in record.get("ancestor_ids") or () if ancestor != record["id"]],
            "iconic_taxon_id": record.get("iconic_taxon_id"),
            "observations_count": record.get("observations_count"),
        }
        for record in records
    ]


def iconic_taxon_id(taxon_id: int, ancestor_ids: Iterable[int]) -> int | None:
    """The iNat iconic taxon a taxon sits under: the nearest of ``ICONIC_TAXON_IDS`` on its lineage
    (itself first), or ``None`` when it sits under none of them."""
    for candidate in (taxon_id, *reversed(list(ancestor_ids))):
        if candidate in ICONIC_TAXON_IDS:
            return candidate
    return None
