"""Which map / list icon each fungus genus gets (issue #449).

Two tiers. A **shape group** (gilled cap, bracket, coral, ...) covers every genus, picked from
its iNat taxonomy: the genus's own override wins, then its family's, then its order's, then its
class's, else ``generic``. On top of that the most-observed genera get a **bespoke** icon of
their own (``BESPOKE_GENERA``); everyone else shows their group's.

These are stylised category markers for a map, not identification aids - a group says roughly
what shape of thing was found, nothing about what it is (AGENTS.md "Not in scope"). The tables
follow the common field-guide shape of each taxon, so an odd member of a family (a gilled
bolete, a coral in a tooth family) needs a genus override rather than a new rule.

``foray genera-refresh`` stores each genus's class / order / family on ``fungi_genera``
(``catalog_rows``); reads resolve the icon from them with ``genus_icon``, so a change to these
tables applies on the next deploy without a refresh. The API sends the key and the client draws
the matching SVG.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, cast, get_args

IconGroup = Literal[
    "gilled",
    "bolete",
    "bracket",
    "crust",
    "coral",
    "puffball",
    "earthstar",
    "cup",
    "morel",
    "tooth",
    "vase",
    "jelly",
    "stinkhorn",
    "leafy-lichen",
    "shrubby-lichen",
    "rust",
    "generic",
]

# The 30 genera with the most precise (unobscured) observations on prod as of 2026-10-05, most
# observed first. Each key is the lowercased genus name. Add more by count; the frontend's icon
# map is typed against ``GenusIcon``, so a key without art fails its typecheck.
BespokeGenus = Literal[
    "trametes",
    "amanita",
    "laetiporus",
    "pleurotus",
    "fomitopsis",
    "cerioporus",
    "ganoderma",
    "lactarius",
    "stereum",
    "schizophyllum",
    "mycena",
    "suillus",
    "cladonia",
    "omphalotus",
    "coprinus",
    "flavoparmelia",
    "cantharellus",
    "hericium",
    "desarmillaria",
    "lobaria",
    "hypomyces",
    "chlorophyllum",
    "russula",
    "cortinarius",
    "artomyces",
    "apioperdon",
    "entoloma",
    "agaricus",
    "leucocoprinus",
    "morchella",
]

GenusIcon = Literal[IconGroup, BespokeGenus]

ICON_GROUPS: tuple[str, ...] = get_args(IconGroup)
BESPOKE_GENERA: tuple[str, ...] = get_args(BespokeGenus)

# Order -> group. Orders left out (moulds, yeasts, insect pathogens: Mucorales, Eurotiales,
# Entomophthorales, Laboulbeniales, ...) have no field shape to draw and fall to the class table,
# then to ``generic``.
ORDER_GROUPS: dict[str, IconGroup] = {
    # Mushrooms and their relatives (Agaricomycetes)
    "Agaricales": "gilled",
    "Russulales": "gilled",
    "Boletales": "bolete",
    "Polyporales": "bracket",
    "Hymenochaetales": "bracket",
    "Gloeophyllales": "bracket",
    "Cantharellales": "vase",
    "Gomphales": "coral",
    "Tremellodendropsidales": "coral",
    "Thelephorales": "crust",
    "Corticiales": "crust",
    "Atheliales": "crust",
    "Amylocorticiales": "crust",
    "Trechisporales": "crust",
    "Sebacinales": "crust",
    "Xenasmatellales": "crust",
    "Stereopsidales": "crust",
    "Lepidostromatales": "coral",
    "Phallales": "stinkhorn",
    "Hysterangiales": "puffball",
    "Geastrales": "earthstar",
    "Auriculariales": "jelly",
    "Tremellales": "jelly",
    "Dacrymycetales": "jelly",
    # Cup fungi and their relatives
    "Pezizales": "cup",
    "Helotiales": "cup",
    "Orbiliales": "cup",
    "Thelebolales": "cup",
    "Leotiales": "coral",
    "Geoglossales": "coral",
    "Neolectales": "coral",
    # Flask fungi: mostly crusts and black dots on wood
    "Xylariales": "crust",
    "Hypocreales": "crust",
    "Diaporthales": "crust",
    "Sordariales": "crust",
    "Glomerellales": "crust",
    "Chaetosphaeriales": "crust",
    "Boliniales": "crust",
    "Amphisphaeriales": "crust",
    "Coronophorales": "crust",
    "Pleosporales": "crust",
    "Botryosphaeriales": "crust",
    "Hysteriales": "crust",
    "Capnodiales": "crust",
    "Phacidiales": "crust",
    # Rusts, smuts, galls, leaf spots
    "Pucciniales": "rust",
    "Platygloeales": "rust",
    "Ustilaginales": "rust",
    "Urocystidales": "rust",
    "Entylomatales": "rust",
    "Exobasidiales": "rust",
    "Microstromatales": "rust",
    "Microbotryales": "rust",
    "Taphrinales": "rust",
    "Synchytriales": "rust",
    "Rhytismatales": "rust",
    "Phyllachorales": "rust",
    "Venturiales": "rust",
    "Mycosphaerellales": "rust",
    # Lichens: leafy by default, shrubby genera/families overridden below
    "Lecanorales": "leafy-lichen",
    "Peltigerales": "leafy-lichen",
    "Teloschistales": "leafy-lichen",
    "Caliciales": "leafy-lichen",
    "Pertusariales": "leafy-lichen",
    "Candelariales": "leafy-lichen",
    "Ostropales": "leafy-lichen",
    "Graphidales": "leafy-lichen",
    "Arthoniales": "leafy-lichen",
    "Umbilicariales": "leafy-lichen",
    "Rhizocarpales": "leafy-lichen",
    "Acarosporales": "leafy-lichen",
    "Lecideales": "leafy-lichen",
    "Baeomycetales": "leafy-lichen",
    "Trapeliales": "leafy-lichen",
    "Lichinales": "leafy-lichen",
    "Coniocybales": "leafy-lichen",
    "Verrucariales": "leafy-lichen",
    "Pyrenulales": "leafy-lichen",
    "Mycocaliciales": "leafy-lichen",
    "Trypetheliales": "leafy-lichen",
    "Strigulales": "leafy-lichen",
    "Sarrameanales": "leafy-lichen",
    "Hymeneliales": "leafy-lichen",
    "Leprocaulales": "leafy-lichen",
}

# Family -> group, where a family's usual shape differs from its order's.
FAMILY_GROUPS: dict[str, IconGroup] = {
    # Agaricales
    "Lycoperdaceae": "puffball",
    "Battarreaceae": "puffball",
    "Clavariaceae": "coral",
    "Typhulaceae": "coral",
    "Pterulaceae": "coral",
    "Nidulariaceae": "cup",
    "Fistulinaceae": "bracket",
    "Schizophyllaceae": "bracket",
    # Boletales
    "Paxillaceae": "gilled",
    "Hygrophoropsidaceae": "gilled",
    "Gomphidiaceae": "gilled",
    "Tapinellaceae": "gilled",
    "Sclerodermataceae": "puffball",
    "Pisolithaceae": "puffball",
    "Rhizopogonaceae": "puffball",
    "Calostomataceae": "puffball",
    "Diplocystidaceae": "earthstar",
    "Serpulaceae": "crust",
    "Coniophoraceae": "crust",
    # Polyporales
    "Sparassidaceae": "coral",
    # Russulales
    "Hericiaceae": "tooth",
    "Auriscalpiaceae": "tooth",
    "Stereaceae": "crust",
    "Peniophoraceae": "crust",
    "Bondarzewiaceae": "bracket",
    "Albatrellaceae": "bracket",
    "Echinodontiaceae": "bracket",
    # Cantharellales
    "Clavulinaceae": "coral",
    "Aphelariaceae": "coral",
    "Botryobasidiaceae": "crust",
    "Ceratobasidiaceae": "crust",
    "Tulasnellaceae": "crust",
    # Thelephorales
    "Boletopsidaceae": "tooth",
    "Bankeraceae": "tooth",
    # Pezizales
    "Morchellaceae": "morel",
    "Discinaceae": "morel",
    "Helvellaceae": "morel",
    "Tuberaceae": "puffball",
    # Hypocreales: the club-shaped insect and truffle parasites
    "Cordycipitaceae": "coral",
    "Ophiocordycipitaceae": "coral",
    # Lichens
    "Cladoniaceae": "shrubby-lichen",
    "Sphaerophoraceae": "shrubby-lichen",
}

# Genus -> group, for a genus whose shape differs from its family's (or that iNat places in no
# family at all).
GENUS_GROUPS: dict[str, IconGroup] = {
    # Agaricales
    "Podaxis": "puffball",
    "Calvatia": "puffball",
    "Calbovista": "puffball",
    "Mycenastrum": "puffball",
    # Boletales
    "Paxillus": "gilled",
    "Hygrophoropsis": "gilled",
    "Scleroderma": "puffball",
    # Polyporales
    "Sparassis": "coral",
    # Russulales
    "Artomyces": "coral",
    "Lentinellus": "gilled",
    # Cantharellales
    "Hydnum": "tooth",
    "Clavulina": "coral",
    "Multiclavula": "coral",
    # Gomphales
    "Gomphus": "vase",
    "Turbinellus": "vase",
    # Thelephorales
    "Boletopsis": "bracket",
    # Auriculariales
    "Pseudohydnum": "tooth",
    # Ascomycetes
    "Xylaria": "coral",
    "Elaphomyces": "puffball",
    # Atractiellales
    "Phleogena": "puffball",
    # Shrubby lichens in leafy families
    "Usnea": "shrubby-lichen",
    "Bryoria": "shrubby-lichen",
    "Alectoria": "shrubby-lichen",
    "Evernia": "shrubby-lichen",
    "Pseudevernia": "shrubby-lichen",
    "Letharia": "shrubby-lichen",
    "Ramalina": "shrubby-lichen",
    "Niebla": "shrubby-lichen",
    "Teloschistes": "shrubby-lichen",
    "Stereocaulon": "shrubby-lichen",
}

# Class -> group, the last stop before ``generic`` for an order the table above leaves out.
CLASS_GROUPS: dict[str, IconGroup] = {
    "Lecanoromycetes": "leafy-lichen",
    "Lichinomycetes": "leafy-lichen",
    "Arthoniomycetes": "leafy-lichen",
    "Pezizomycetes": "cup",
    "Leotiomycetes": "cup",
    "Pucciniomycetes": "rust",
    "Ustilaginomycetes": "rust",
    "Exobasidiomycetes": "rust",
    "Taphrinomycetes": "rust",
    "Tremellomycetes": "jelly",
    "Dacrymycetes": "jelly",
    "Dothideomycetes": "crust",
    "Sordariomycetes": "crust",
}


def icon_group(genus: str, family: str | None, order: str | None, class_name: str | None) -> IconGroup:
    """The shape group for a genus: genus override, then family, order, class, else generic."""
    for table, key in (
        (GENUS_GROUPS, genus),
        (FAMILY_GROUPS, family),
        (ORDER_GROUPS, order),
        (CLASS_GROUPS, class_name),
    ):
        if key is not None and key in table:
            return table[key]
    return "generic"


def genus_icon(genus: str, family: str | None, order: str | None, class_name: str | None) -> GenusIcon:
    """The icon key for a genus: its bespoke key if it has one, else its shape group."""
    key = genus.lower()
    if key in BESPOKE_GENERA:
        return cast(GenusIcon, key)
    return icon_group(genus, family, order, class_name)


def catalog_rows(genera: Iterable[dict[str, Any]], ranks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """``fungi_genera`` rows from iNat ``/v1/taxa`` records (``foray genera-refresh``).

    ``genera`` are the genus records (``inat.iter_fungi_genera``); ``ranks`` the class / order /
    family records (``inat.iter_fungi_ranks``) that name each genus's ``ancestor_ids``. An
    ancestor missing from ``ranks`` (iNat added it between the two listings) just leaves that
    rank NULL - the icon then resolves from the ranks it does have.
    """
    by_id = {rank["id"]: rank for rank in ranks}
    rows = []
    for genus in genera:
        ancestry = {
            by_id[ancestor_id]["rank"]: by_id[ancestor_id]
            for ancestor_id in genus.get("ancestor_ids") or ()
            if ancestor_id in by_id
        }
        class_taxon, order, family = ancestry.get("class"), ancestry.get("order"), ancestry.get("family")
        class_name = class_taxon["name"] if class_taxon else None
        order_name = order["name"] if order else None
        family_name = family["name"] if family else None
        rows.append(
            {
                "taxon_id": genus["id"],
                "name": genus["name"],
                "common_name": genus.get("preferred_common_name"),
                "observations_count": genus.get("observations_count"),
                "class_name": class_name,
                "order_id": order["id"] if order else None,
                "order_name": order_name,
                "family_id": family["id"] if family else None,
                "family_name": family_name,
            }
        )
    return rows
