"""Tree-density ingest tests (issue #85) - no network; iNat calls mocked."""

from __future__ import annotations

from unittest.mock import patch

import psycopg

from foray.cache import is_ingested, upsert_tree_associations, upsert_tree_density
from foray.config import CoverageRegion
from foray.scoring import trees_near
from foray.trees import _region_id, fetch_tree_density, ingest_trees_region

HOME_LAT, HOME_LNG = 47.6, -122.3


def _fake_obs(lat: float, lng: float) -> dict:
    return {"geojson": {"coordinates": [lng, lat]}}


def test_region_id_matches_scoring_binning_convention() -> None:
    # floor(47.6 / 0.25) == 190, floor(-122.3 / 0.25) == -490
    assert _region_id(47.6, -122.3, 0.25) == "190_-490"


def test_fetch_tree_density_aggregates_and_averages_real_points() -> None:
    region = CoverageRegion(name="Washington", place_id=46)

    with (
        patch("foray.trees.resolve_genus_taxon_id", return_value=123),
        patch("foray.trees.iter_observations") as mock_iter,
    ):
        mock_iter.return_value = iter([_fake_obs(47.60, -122.30), _fake_obs(47.61, -122.31)])
        rows = fetch_tree_density(region=region, cell_deg=0.25, host_genera=["Pinus"])

    assert len(rows) == 1
    region_id, genus, center_lat, center_lng, cnt = rows[0]
    assert genus == "Pinus"
    assert cnt == 2
    assert center_lat == (47.60 + 47.61) / 2
    assert center_lng == (-122.30 + -122.31) / 2


def test_fetch_tree_density_skips_unresolvable_host_genus() -> None:
    region = CoverageRegion(name="Washington", place_id=46)

    with (
        patch("foray.trees.resolve_genus_taxon_id", return_value=None),
        patch("foray.trees.iter_observations") as mock_iter,
    ):
        rows = fetch_tree_density(region=region, cell_deg=0.25, host_genera=["Notatreegenus"])

    mock_iter.assert_not_called()
    assert rows == []


def test_ingest_trees_region_reads_tracked_host_genera_and_records_ingest(con: psycopg.Connection) -> None:
    upsert_tree_associations(con, [("Boletus", "Pinus", 10)])
    region = CoverageRegion(name="Washington", place_id=46)

    with (
        patch("foray.trees.resolve_genus_taxon_id", return_value=123),
        patch("foray.trees.iter_observations") as mock_iter,
    ):
        mock_iter.return_value = iter([_fake_obs(47.60, -122.30)])
        count = ingest_trees_region(region, 0.25, con)

    assert count == 1
    assert is_ingested(con, "trees:place:46")
    row = con.execute("SELECT genus, cnt FROM tree_density").fetchone()
    assert row == ("Pinus", 1)


def test_ingest_trees_region_skips_when_already_ingested(con: psycopg.Connection) -> None:
    upsert_tree_associations(con, [("Boletus", "Pinus", 10)])
    region = CoverageRegion(name="Washington", place_id=46)

    with (
        patch("foray.trees.resolve_genus_taxon_id", return_value=123),
        patch("foray.trees.iter_observations") as mock_iter,
    ):
        mock_iter.return_value = iter([_fake_obs(47.60, -122.30)])
        ingest_trees_region(region, 0.25, con)
        mock_iter.return_value = iter([_fake_obs(47.60, -122.30)])
        count = ingest_trees_region(region, 0.25, con)

    assert count == 0


def test_ingest_trees_region_skips_when_no_associations_yet(con: psycopg.Connection) -> None:
    region = CoverageRegion(name="Washington", place_id=46)
    count = ingest_trees_region(region, 0.25, con)
    assert count == 0


def test_trees_near_filters_by_radius_and_genus(con: psycopg.Connection) -> None:
    upsert_tree_density(
        con,
        [
            ("r1", "Pinus", HOME_LAT + 0.01, HOME_LNG, 42),
            ("r2", "Quercus", HOME_LAT + 0.01, HOME_LNG, 8),
            ("r3", "Pinus", HOME_LAT + 10.0, HOME_LNG, 99),  # far outside radius
        ],
    )

    everything = trees_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0)
    assert {cell.genus for cell in everything} == {"Pinus", "Quercus"}

    pinus_only = trees_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, genera={"Pinus"})
    assert [cell.genus for cell in pinus_only] == ["Pinus"]
    assert pinus_only[0].cnt == 42


def test_trees_near_no_rows_ingested_returns_empty(con: psycopg.Connection) -> None:
    assert trees_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0) == []
