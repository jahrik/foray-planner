"""Rank-driven taxonomy helpers (issue #464) - pure, no database."""

from __future__ import annotations

from foray.taxa import (
    ICONIC_TAXON_IDS,
    RANK_LEVELS,
    Resolver,
    Rollup,
    TaxonNode,
    build_ancestor_ids,
    iconic_taxon_id,
    ids_under_scope,
    rank_level,
    rollup_for,
    taxa_rows,
)

FUNGI, AGARICOMYCETES, AGARICALES, AMANITACEAE = 47170, 50814, 47169, 47167
AMANITA, AMANITA_SECTION, AMANITA_MUSCARIA, MUSCARIA_VAR = 47168, 9001, 48715, 9002
RANKS = {
    FUNGI: "kingdom",
    AGARICOMYCETES: "class",
    AGARICALES: "order",
    AMANITACEAE: "family",
    AMANITA: "genus",
    AMANITA_SECTION: "section",  # a section shares its genus's name; it is not the genus
    AMANITA_MUSCARIA: "species",
    MUSCARIA_VAR: "variety",
}
LINEAGE = [FUNGI, AGARICOMYCETES, AGARICALES, AMANITACEAE, AMANITA, AMANITA_SECTION, AMANITA_MUSCARIA]


def test_rank_levels_order_species_below_genus_below_family() -> None:
    assert (rank_level("species"), rank_level("genus"), rank_level("family")) == (10, 20, 30)
    assert rank_level("kingdom") == max(RANK_LEVELS[rank] for rank in ("kingdom", "class", "order"))
    assert rank_level("nonsense") is None and rank_level(None) is None


def test_build_ancestor_ids_walks_parent_chain_root_first() -> None:
    nodes = [
        TaxonNode(1, None, "kingdom"),
        TaxonNode(2, 1, "phylum"),
        TaxonNode(3, 2, "class"),
        TaxonNode(4, 3, "order"),
    ]
    assert build_ancestor_ids(nodes) == {1: [], 2: [1], 3: [1, 2], 4: [1, 2, 3]}


def test_build_ancestor_ids_tolerates_a_dangling_parent_and_a_cycle() -> None:
    nodes = [
        TaxonNode(10, 999, "genus"),  # parent missing from the export: keep the partial (empty) path
        TaxonNode(11, 10, "species"),
        TaxonNode(20, 21, "genus"),  # a cycle must be cut, not looped forever
        TaxonNode(21, 20, "species"),
    ]
    lineage = build_ancestor_ids(nodes)
    assert lineage[10] == [] and lineage[11] == [10]
    assert lineage[20] == [21] and lineage[21] == [20]


def test_rollup_species_steps_over_a_same_named_section_to_the_genus() -> None:
    """The section and the genus both read "Amanita": only the rank tells them apart."""
    assert rollup_for(AMANITA_MUSCARIA, LINEAGE[:-1], RANKS) == Rollup(AMANITA, AMANITA_MUSCARIA)


def test_rollup_variety_form_and_subspecies_roll_up_to_their_species() -> None:
    assert rollup_for(MUSCARIA_VAR, [*LINEAGE], RANKS) == Rollup(AMANITA, AMANITA_MUSCARIA)


def test_rollup_genus_only_has_no_species() -> None:
    assert rollup_for(AMANITA, LINEAGE[:4], RANKS) == Rollup(AMANITA, None)


def test_rollup_above_genus_is_neither() -> None:
    assert rollup_for(AMANITACEAE, LINEAGE[:3], RANKS) == Rollup(None, None)
    assert rollup_for(FUNGI, [], RANKS) == Rollup(None, None)


def test_rollup_section_identification_counts_toward_its_genus_not_a_species() -> None:
    assert rollup_for(AMANITA_SECTION, LINEAGE[:5], RANKS) == Rollup(AMANITA, None)


def test_ids_under_scope() -> None:
    assert ids_under_scope(AMANITA, LINEAGE[:4], [FUNGI])
    assert ids_under_scope(FUNGI, [], [FUNGI])  # a root is under itself
    assert not ids_under_scope(424242, [1, 47158], [FUNGI])
    assert ids_under_scope(AMANITA, LINEAGE[:4], [999, AGARICALES])  # any of several roots


def _obs(taxon_id: int, rank: str, ancestors: list[int]) -> dict:
    return {"taxon": {"id": taxon_id, "rank": rank, "ancestor_ids": [*ancestors, taxon_id]}}


def test_resolver_rolls_up_a_live_observation_and_drops_its_trailing_self() -> None:
    resolver = Resolver(RANKS, [FUNGI])
    assert resolver.resolve(_obs(AMANITA_MUSCARIA, "species", LINEAGE[:-1])) == Rollup(AMANITA, AMANITA_MUSCARIA)
    assert resolver.resolve(_obs(AMANITA, "genus", LINEAGE[:4])) == Rollup(AMANITA, None)


def test_resolver_rejects_out_of_scope_and_above_genus() -> None:
    resolver = Resolver(RANKS, [FUNGI])
    # An animal genus that happens to share a fungal genus's name has its own id and lineage.
    assert resolver.resolve(_obs(555, "genus", [1, 47158])) is None
    assert resolver.resolve(_obs(AMANITACEAE, "family", LINEAGE[:3])) is None
    assert resolver.resolve({"taxon": None}) is None


def test_resolver_uses_the_observations_own_rank_for_a_taxon_not_yet_cataloged() -> None:
    resolver = Resolver(RANKS, [FUNGI])
    assert resolver.resolve(_obs(777, "genus", [FUNGI, AGARICOMYCETES])) == Rollup(777, None)


def test_resolver_scope_can_be_a_non_kingdom_root() -> None:
    """Trees are curated orders / families inside one kingdom: a root need not be a kingdom."""
    resolver = Resolver(RANKS, [AGARICALES])
    assert resolver.resolve(_obs(AMANITA, "genus", LINEAGE[:4])) == Rollup(AMANITA, None)
    assert resolver.resolve(_obs(555, "genus", [FUNGI, AGARICOMYCETES, 424242])) is None


def test_taxa_rows_maps_api_records_and_drops_self_from_ancestors() -> None:
    rows = taxa_rows(
        [
            {
                "id": AMANITA,
                "name": "Amanita",
                "rank": "genus",
                "parent_id": AMANITACEAE,
                "preferred_common_name": "Amanitas",
                "observations_count": 5,
                "iconic_taxon_id": FUNGI,
                "ancestor_ids": [FUNGI, AMANITACEAE, AMANITA],
            },
            {"id": 9, "name": "Bare", "rank": "genus"},
        ]
    )
    assert rows[0] == {
        "taxon_id": AMANITA,
        "parent_id": AMANITACEAE,
        "name": "Amanita",
        "common_name": "Amanitas",
        "rank": "genus",
        "ancestor_ids": [FUNGI, AMANITACEAE],
        "iconic_taxon_id": FUNGI,
        "observations_count": 5,
    }
    assert rows[1]["ancestor_ids"] == [] and rows[1]["common_name"] is None


def test_iconic_taxon_id_is_the_nearest_iconic_group_on_the_lineage() -> None:
    assert FUNGI in ICONIC_TAXON_IDS
    assert iconic_taxon_id(AMANITA, [FUNGI, AGARICOMYCETES]) == FUNGI
    assert iconic_taxon_id(FUNGI, []) == FUNGI
    assert iconic_taxon_id(555, [1, 12345]) == 1  # Animalia
    assert iconic_taxon_id(555, [777, 888]) is None
