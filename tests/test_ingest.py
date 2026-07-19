"""Tests for home-radius ingest (issue #79 Phase 4: whole-Fungi-kingdom query, one genus
resolved per observation via its own taxon ancestry) - no network; iNat calls mocked."""

from __future__ import annotations

from unittest.mock import patch

import psycopg
import pytest

from foray.cache import upsert_fungi_genera
from foray.config import Settings
from foray.ingest import ingest

MOREL = 111
CHANTERELLE = 222


@pytest.fixture
def cfg_with_home(con: psycopg.Connection, monkeypatch) -> Settings:
    monkeypatch.setenv("FORAY_HOME__NAME", "Home")
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    monkeypatch.setenv("FORAY_INGEST__SINCE_YEAR", "2015")
    monkeypatch.setenv("FORAY_INGEST__QUALITY_GRADE", "research")
    upsert_fungi_genera(
        con,
        [
            {"taxon_id": MOREL, "name": "Morchella", "common_name": "Morels"},
            {"taxon_id": CHANTERELLE, "name": "Cantharellus", "common_name": "Chanterelles"},
        ],
    )
    return Settings()


def _fake_obs(obs_id: int, taxon_id: int, *, rank: str = "genus", ancestor_ids: list[int] | None = None) -> dict:
    return {
        "id": obs_id,
        "geojson": {"coordinates": [-122.3, 47.6]},
        "observed_on": "2024-05-15",
        "quality_grade": "research",
        "positional_accuracy": 10,
        "taxon": {"id": taxon_id, "rank": rank, "ancestor_ids": ancestor_ids or [taxon_id]},
    }


def test_ingest_queries_whole_fungi_kingdom(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    """A single taxon_id=FUNGI_TAXON_ID query, not one call per genus."""
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(1, MOREL), _fake_obs(2, CHANTERELLE)])
        counts = ingest(cfg_with_home, con)

    assert counts == {MOREL: 1, CHANTERELLE: 1}
    mock_iter.assert_called_once()
    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["taxon_id"] == 47170  # foray.inat.FUNGI_TAXON_ID
    assert call_kwargs["lat"] == 47.6
    assert call_kwargs["radius_km"] == 200

    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 2


def test_ingest_resolves_genus_from_species_rank_ancestry(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    """A species-rank observation gets tagged with its genus ancestor's taxon_id, not its own."""
    species_taxon_id = 555555  # e.g. Morchella esculenta, not in the catalog itself
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter(
            [_fake_obs(1, species_taxon_id, rank="species", ancestor_ids=[47170, MOREL, species_taxon_id])]
        )
        counts = ingest(cfg_with_home, con)

    assert counts == {MOREL: 1}
    row = con.execute("SELECT taxon_id FROM observations WHERE id = 1").fetchone()
    assert row is not None
    assert row[0] == MOREL


def test_ingest_skips_observations_with_no_known_genus_ancestor(
    con: psycopg.Connection, cfg_with_home: Settings
) -> None:
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter(
            [_fake_obs(1, MOREL), _fake_obs(2, 999999, rank="family", ancestor_ids=[47170, 999999])]
        )
        counts = ingest(cfg_with_home, con)

    assert counts == {MOREL: 1}
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 1


def test_ingest_records_a_single_ingest_log_entry(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(1, MOREL)])
        ingest(cfg_with_home, con)

    log_row = con.execute("SELECT key FROM ingest_log WHERE key LIKE %s", ["obs:fungi:%"]).fetchone()
    assert log_row is not None


def test_ingest_incremental_overlaps_by_a_week(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count, lat, lng, radius_km) "
        "VALUES ('obs:fungi:47.6:-122.3:200.0:2015-01-01:2024-06-01', now(), 5, 47.6, -122.3, 200)"
    )
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([])
        ingest(cfg_with_home, con)

    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["d1"] >= "2024-05-25"
