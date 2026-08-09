"""GloBI ingest tests (issue #85) - fixtures mirror real response shapes recorded during live
testing (see foray/tree_associations.py's docstring), never hit the live API in CI."""

from __future__ import annotations

import httpx
import psycopg

from foray.cache import upsert_fungi_genera
from foray.tree_associations import fetch_host_genera, refresh_tree_associations

_COLUMNS = ["source_taxon_name", "target_taxon_name", "target_taxon_path"]


def _globi_response(rows: list[tuple[str, str, str]]) -> httpx.Response:
    return httpx.Response(200, json={"columns": _COLUMNS, "data": [list(row) for row in rows]})


def test_fetch_host_genera_drops_rows_whose_source_does_not_exactly_match() -> None:
    """Live testing found GloBI's sourceTaxon= query is fuzzy/broader than an exact genus
    match: an unfiltered query for the saprobic genus Agaricus returned rows that were
    actually Amanita records. Rows must be re-checked against their own source_taxon_name."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _globi_response(
            [
                ("Amanita", "Pinus sylvestris", "Eukaryota | Plantae | Streptophyta | Pinus"),
                ("Amanita muscaria", "Quercus robur", "Eukaryota | Plantae | Streptophyta | Quercus"),
            ]
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    counts = fetch_host_genera(client, "Agaricus")
    assert counts == {}


def test_fetch_host_genera_drops_non_plant_targets() -> None:
    """Live testing found cross-kingdom noise in GloBI's aggregated data - e.g. a mollusk
    genus (Clavilithes) tagged as a "host" for several fungi genera. Targets whose lineage
    path isn't plant-like must be dropped even when the source genus matches exactly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _globi_response(
            [
                ("Boletus", "Pinus sylvestris", "Eukaryota | Plantae | Streptophyta | Pinus"),
                ("Boletus", "Clavilithes noae", "Eukaryota | Animalia | Mollusca | Clavilithes"),
            ]
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    counts = fetch_host_genera(client, "Boletus")
    assert counts == {"Pinus": 1}


def test_fetch_host_genera_aggregates_to_genus_level() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _globi_response(
            [
                ("Cortinarius", "Pinus sylvestris", "Eukaryota | Plantae | Streptophyta | Pinus"),
                ("Cortinarius", "Pinus nigra", "Eukaryota | Plantae | Streptophyta | Pinus"),
                ("Cortinarius", "Quercus robur", "Eukaryota | Plantae | Streptophyta | Quercus"),
            ]
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    counts = fetch_host_genera(client, "Cortinarius")
    assert counts == {"Pinus": 2, "Quercus": 1}


def test_refresh_tree_associations_drops_pairs_below_min_weight(con: psycopg.Connection) -> None:
    upsert_fungi_genera(
        con,
        [
            {"taxon_id": 1, "name": "Boletus", "common_name": None, "observations_count": 100},
            {"taxon_id": 2, "name": "Coprinus", "common_name": None, "observations_count": 50},
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        genus = request.url.params.get("sourceTaxon")
        if genus == "Boletus":
            return _globi_response([("Boletus", "Pinus sylvestris", "Eukaryota | Plantae | Streptophyta | Pinus")] * 4)
        # Coprinus (saprobic): a couple of spurious hits, below the noise threshold.
        return _globi_response([("Coprinus", "Betula pendula", "Eukaryota | Plantae | Streptophyta | Betula")] * 2)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    count = refresh_tree_associations(con, client=client)

    rows = con.execute("SELECT fungus_genus, host_genus, weight FROM tree_associations").fetchall()
    assert rows == [("Boletus", "Pinus", 4)]
    assert count == 1
