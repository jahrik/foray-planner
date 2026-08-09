"""ECM fungus genus -> host tree genus associations, sourced from GloBI (issue #85).

Nothing in this repo, iNat, or the genus catalog records which tree genera an ectomycorrhizal
(ECM) fungus associates with, so this queries GloBI (api.globalbioticinteractions.org), which
aggregates species-interaction records from academic datasets under a precise, ontology-backed
interaction type - ``hasEctomycorrhizalHost`` (source: fungus, target: plant root; RO_0002805) -
rather than anything text-mined or hand-guessed.

Two data-quality filters, both confirmed necessary by live testing against the real API during
planning:

- GloBI's ``sourceTaxon=`` query is fuzzy/broader than an exact genus match: an unfiltered query
  for the saprobic (non-ECM) genus Agaricus returned 100 rows that were all actually Amanita
  records. Every row is re-checked against its own ``source_taxon_name``.
- A small amount of cross-kingdom noise exists in the underlying aggregated data - e.g. a
  mollusk genus, *Clavilithes*, showed up as a "host" for several fungi genera. Every row's
  ``target_taxon_path`` must look plant-like.

After both filters, live testing against 18 known ECM genera and 2 saprobic controls
(Agaricus, Coprinus) returned mycologically sensible results throughout (Pinus/Quercus/Picea/
Betula dominate) with the saprobic controls dropping to near zero.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx
import psycopg

from foray.cache import genus_taxon_ids, upsert_tree_associations

logger = logging.getLogger(__name__)

_GLOBI_URL = "https://api.globalbioticinteractions.org/interaction"
USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

# GloBI publishes no documented rate limit, but this is a one-time bulk job over the whole
# ~6,000-genus catalog - pace conservatively as a good API citizen rather than hammering it.
_MIN_REQUEST_INTERVAL = 0.5

# Drop single/double-record noise - live testing found the saprobic genus Coprinus picking up
# a couple of spurious hasEctomycorrhizalHost hits despite not being ECM, while genuine ECM
# genera cleared this threshold easily (dozens to thousands of kept records each).
_MIN_PAIR_WEIGHT = 3

_PLANT_MARKERS = ("Plantae", "Streptophyta", "Tracheophyta")


def _make_throttle(min_interval: float) -> Callable[[], None]:
    """A pacer that blocks until at least ``min_interval`` has passed since the last call."""
    last = [0.0]

    def throttle() -> None:
        if min_interval <= 0:
            return
        wait = min_interval - (time.monotonic() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()

    return throttle


def _is_plant_path(path: str) -> bool:
    """GloBI aggregates several source taxonomies with inconsistent lineage vocabulary (some
    say "Plantae", others "Streptophyta"/"Tracheophyta" without "Plantae") - accept any plant
    marker, but always reject a path that also says "Animalia" (the Clavilithes noise case)."""
    return "Animalia" not in path and any(marker in path for marker in _PLANT_MARKERS)


def fetch_host_genera(client: httpx.Client, fungus_genus: str) -> Counter[str]:
    """Query GloBI's hasEctomycorrhizalHost for one fungus genus.

    Returns a Counter of host genus name -> kept-record count, after filtering rows whose
    source doesn't exactly match ``fungus_genus`` or whose target doesn't look plant-like
    (see module docstring).
    """
    resp = client.get(
        _GLOBI_URL,
        params={
            "sourceTaxon": fungus_genus,
            "interactionType": "hasEctomycorrhizalHost",
            "limit": 5000,
        },
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    idx = {name: i for i, name in enumerate(data.get("columns", []))}
    required = ("source_taxon_name", "target_taxon_name", "target_taxon_path")
    missing = [column for column in required if column not in idx]
    if missing:
        logger.warning(
            "tree_associations: GloBI response for %s missing column(s) %s, skipping",
            fungus_genus,
            missing,
        )
        return Counter()
    counts: Counter[str] = Counter()
    for row in data.get("data", []):
        source_name = row[idx["source_taxon_name"]] or ""
        if not source_name or source_name.split()[0] != fungus_genus:
            continue
        target_path = row[idx["target_taxon_path"]] or ""
        if not _is_plant_path(target_path):
            continue
        target_name = row[idx["target_taxon_name"]] or ""
        host_genus = target_name.split()[0] if target_name else ""
        if host_genus and host_genus[0].isupper() and host_genus.isalpha() and len(host_genus) > 2:
            counts[host_genus] += 1
    return counts


def refresh_tree_associations(
    con: psycopg.Connection,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Rebuild ``tree_associations`` wholesale from GloBI, for every genus in the ``fungi_genera``
    catalog. One-time manual job (see ``cli.py``'s ``tree-associations-refresh``) - not
    scheduled, since ECM biology doesn't change on human timescales. Returns rows upserted.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    throttle = _make_throttle(_MIN_REQUEST_INTERVAL)
    names = sorted(genus_taxon_ids(con))
    refreshed_genera: list[str] = []
    rows: list[tuple[str, str, int]] = []
    try:
        for index, fungus_genus in enumerate(names):
            if progress_cb:
                progress_cb(
                    f"Checking {fungus_genus} for ECM host associations ({index + 1}/{len(names)})…",
                    ((index + 1) / len(names)) * 100.0,
                )
            throttle()
            try:
                host_counts = fetch_host_genera(client, fungus_genus)
            except httpx.HTTPError as exc:
                logger.warning("tree_associations: GloBI request failed for %s: %s", fungus_genus, exc)
                continue
            # A genus whose fetch succeeded but produced no rows above _MIN_PAIR_WEIGHT still
            # needs its stale prior pairs cleared - only a failed fetch should leave old rows
            # in place (see upsert_tree_associations's replace-per-genus semantics).
            refreshed_genera.append(fungus_genus)
            for host_genus, weight in host_counts.items():
                if weight >= _MIN_PAIR_WEIGHT:
                    rows.append((fungus_genus, host_genus, weight))
    finally:
        if owns:
            client.close()
    return upsert_tree_associations(con, rows, replace_genera=refreshed_genera)
