"""foray.genus_icons: the shape-group tables and the bespoke tier (issue #449)."""

from __future__ import annotations

import pytest

from foray.genus_icons import (
    BESPOKE_GENERA,
    CLASS_GROUPS,
    FAMILY_GROUPS,
    GENUS_GROUPS,
    ICON_GROUPS,
    ORDER_GROUPS,
    catalog_rows,
    genus_icon,
    icon_group,
)


@pytest.mark.parametrize(
    ("genus", "family", "order", "class_name", "expected"),
    [
        # Order defaults
        ("Pholiota", "Strophariaceae", "Agaricales", "Agaricomycetes", "gilled"),
        ("Leccinum", "Boletaceae", "Boletales", "Agaricomycetes", "bolete"),
        ("Phellinus", "Hymenochaetaceae", "Hymenochaetales", "Agaricomycetes", "bracket"),
        ("Craterellus", "Hydnaceae", "Cantharellales", "Agaricomycetes", "vase"),
        ("Peziza", "Pezizaceae", "Pezizales", "Pezizomycetes", "cup"),
        ("Tremella", "Tremellaceae", "Tremellales", "Tremellomycetes", "jelly"),
        ("Phallus", "Phallaceae", "Phallales", "Agaricomycetes", "stinkhorn"),
        ("Geastrum", "Geastraceae", "Geastrales", "Agaricomycetes", "earthstar"),
        ("Gymnosporangium", "Gymnosporangiaceae", "Pucciniales", "Pucciniomycetes", "rust"),
        ("Hypoxylon", "Hypoxylaceae", "Xylariales", "Sordariomycetes", "crust"),
        ("Parmelia", "Parmeliaceae", "Lecanorales", "Lecanoromycetes", "leafy-lichen"),
        # Family overrides
        ("Lycoperdon", "Lycoperdaceae", "Agaricales", "Agaricomycetes", "puffball"),
        ("Clavaria", "Clavariaceae", "Agaricales", "Agaricomycetes", "coral"),
        ("Gomphidius", "Gomphidiaceae", "Boletales", "Agaricomycetes", "gilled"),
        ("Pisolithus", "Pisolithaceae", "Boletales", "Agaricomycetes", "puffball"),
        ("Hericium", "Hericiaceae", "Russulales", "Agaricomycetes", "tooth"),
        ("Stereum", "Stereaceae", "Russulales", "Agaricomycetes", "crust"),
        ("Gyromitra", "Discinaceae", "Pezizales", "Pezizomycetes", "morel"),
        ("Verpa", "Morchellaceae", "Pezizales", "Pezizomycetes", "morel"),
        ("Ophiocordyceps", "Ophiocordycipitaceae", "Hypocreales", "Sordariomycetes", "coral"),
        ("Cladonia", "Cladoniaceae", "Lecanorales", "Lecanoromycetes", "shrubby-lichen"),
        ("Hydnellum", "Boletopsidaceae", "Thelephorales", "Agaricomycetes", "tooth"),
        # Genus overrides
        ("Calvatia", "Lycoperdaceae", "Agaricales", "Agaricomycetes", "puffball"),
        ("Paxillus", "Paxillaceae", "Boletales", "Agaricomycetes", "gilled"),
        ("Scleroderma", "Sclerodermataceae", "Boletales", "Agaricomycetes", "puffball"),
        ("Sparassis", "Sparassidaceae", "Polyporales", "Agaricomycetes", "coral"),
        ("Hydnum", "Hydnaceae", "Cantharellales", "Agaricomycetes", "tooth"),
        ("Clavulina", "Hydnaceae", "Cantharellales", "Agaricomycetes", "coral"),
        ("Artomyces", "Auriscalpiaceae", "Russulales", "Agaricomycetes", "coral"),
        ("Gomphus", "Gomphaceae", "Gomphales", "Agaricomycetes", "vase"),
        ("Ramaria", "Gomphaceae", "Gomphales", "Agaricomycetes", "coral"),
        ("Usnea", "Parmeliaceae", "Lecanorales", "Lecanoromycetes", "shrubby-lichen"),
        ("Ramalina", "Ramalinaceae", "Lecanorales", "Lecanoromycetes", "shrubby-lichen"),
        ("Teloschistes", "Teloschistaceae", "Teloschistales", "Lecanoromycetes", "shrubby-lichen"),
        ("Xylaria", "Xylariaceae", "Xylariales", "Sordariomycetes", "coral"),
        ("Pseudohydnum", None, "Auriculariales", "Agaricomycetes", "tooth"),
        # Corrections from the FungalTraits cross-check
        ("Erysiphe", "Erysiphaceae", "Helotiales", "Leotiomycetes", "rust"),
        ("Lentinus", "Polyporaceae", "Polyporales", "Agaricomycetes", "gilled"),
        ("Panus", "Panaceae", "Polyporales", "Agaricomycetes", "gilled"),
        ("Phlebia", "Meruliaceae", "Polyporales", "Agaricomycetes", "crust"),
        ("Merulius", "Meruliaceae", "Polyporales", "Agaricomycetes", "bracket"),
        ("Rickenella", "Rickenellaceae", "Hymenochaetales", "Agaricomycetes", "gilled"),
        ("Phylloporus", "Boletaceae", "Boletales", "Agaricomycetes", "gilled"),
        ("Cora", "Hygrophoraceae", "Agaricales", "Agaricomycetes", "leafy-lichen"),
        ("Calocera", "Dacrymycetaceae", "Dacrymycetales", "Dacrymycetes", "coral"),
        ("Spathularia", "Cudoniaceae", "Rhytismatales", "Leotiomycetes", "coral"),
        # Class fallback for an unlisted order, then generic
        ("Lichenomphalia", None, "Somelichenales", "Lecanoromycetes", "leafy-lichen"),
        ("Penicillium", "Aspergillaceae", "Eurotiales", "Eurotiomycetes", "generic"),
        ("Mucor", "Mucoraceae", "Mucorales", "Mucoromycetes", "generic"),
        ("Unplaced", None, None, None, "generic"),
    ],
)
def test_icon_group(genus: str, family: str | None, order: str | None, class_name: str | None, expected: str) -> None:
    assert icon_group(genus, family, order, class_name) == expected


def test_every_table_maps_to_a_known_group() -> None:
    for table in (ORDER_GROUPS, FAMILY_GROUPS, GENUS_GROUPS, CLASS_GROUPS):
        assert set(table.values()) <= set(ICON_GROUPS)


def test_every_group_but_generic_is_reachable() -> None:
    reachable = {*ORDER_GROUPS.values(), *FAMILY_GROUPS.values(), *GENUS_GROUPS.values(), *CLASS_GROUPS.values()}
    assert reachable == set(ICON_GROUPS) - {"generic"}


def test_bespoke_keys_are_lowercase_and_never_shadow_a_group() -> None:
    assert len(set(BESPOKE_GENERA)) == len(BESPOKE_GENERA)
    assert all(key == key.lower() for key in BESPOKE_GENERA)
    assert not set(BESPOKE_GENERA) & set(ICON_GROUPS)


def test_genus_icon_prefers_bespoke_over_group() -> None:
    assert genus_icon("Morchella", "Morchellaceae", "Pezizales", "Pezizomycetes") == "morchella"
    assert genus_icon("Verpa", "Morchellaceae", "Pezizales", "Pezizomycetes") == "morel"


def test_catalog_rows_names_each_rank_from_ancestor_ids() -> None:
    ranks = [
        {"id": 1, "name": "Pezizomycetes", "rank": "class"},
        {"id": 2, "name": "Pezizales", "rank": "order"},
        {"id": 3, "name": "Morchellaceae", "rank": "family"},
    ]
    genera = [
        {"id": 10, "name": "Verpa", "ancestor_ids": [47170, 1, 2, 3, 10], "observations_count": 5},
        # An ancestor iNat added between the two listings: unknown ids are skipped, not fatal.
        {"id": 11, "name": "Newgenus", "ancestor_ids": [1, 2, 999, 11]},
    ]
    rows = catalog_rows(genera, ranks)
    assert rows[0] == {
        "taxon_id": 10,
        "name": "Verpa",
        "common_name": None,
        "observations_count": 5,
        "class_name": "Pezizomycetes",
        "order_id": 2,
        "order_name": "Pezizales",
        "family_id": 3,
        "family_name": "Morchellaceae",
    }
    assert (rows[1]["order_name"], rows[1]["family_name"]) == ("Pezizales", None)
