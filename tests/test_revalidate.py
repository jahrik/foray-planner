"""Tests for ingest.revalidate - the recurring re-check pass for cross-kingdom homonym genera
(fungal `Olla` vs. the ladybug genus `Olla`, etc) - no network; iNat calls mocked."""

from __future__ import annotations

from unittest.mock import patch

import psycopg
import pytest

from foray.cache import upsert_fungi_genera, upsert_observations
from foray.config import Settings
from foray.ingest import revalidate

OLLA_FUNGUS = 111  # fungal genus taxon_id, homonymous with the ladybug genus below
CANTHARELLUS = 222

_ROW = (
    1,  # id
    OLLA_FUNGUS,  # taxon_id
    47.6,  # lat
    -122.3,  # lng
    "2024-05-15",  # observed_on
    5,  # month
    2024,  # year
    "research",  # quality_grade
    10,  # positional_accuracy
    None,  # place_guess
    "https://inaturalist.org/observations/1",  # uri
    None,  # obscured
)


@pytest.fixture
def cfg_with_home(con: psycopg.Connection, monkeypatch) -> Settings:
    monkeypatch.setenv("FORAY_HOME__NAME", "Home")
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    upsert_fungi_genera(
        con,
        [
            # iNat says this "fungal" genus has 0 observations right now - any cached row makes
            # it a suspect (see cache.suspect_genus_taxon_ids).
            {"taxon_id": OLLA_FUNGUS, "name": "Olla", "common_name": None, "observations_count": 0},
            {
                "taxon_id": CANTHARELLUS,
                "name": "Cantharellus",
                "common_name": "Chanterelles",
                "observations_count": 90000,
            },
        ],
    )
    return Settings()


def _cached_row(obs_id: int, taxon_id: int = OLLA_FUNGUS) -> tuple:
    return (obs_id, taxon_id, *_ROW[2:])


def _live_obs(obs_id: int, *, iconic_taxon_id: int, taxon_id: int, rank: str = "genus", ancestor_ids=None) -> dict:
    return {
        "id": obs_id,
        "geojson": {"coordinates": [-122.3, 47.6]},
        "observed_on": "2024-05-15",
        "quality_grade": "research",
        "positional_accuracy": 10,
        "taxon": {
            "id": taxon_id,
            "rank": rank,
            "ancestor_ids": ancestor_ids or [taxon_id],
            "iconic_taxon_id": iconic_taxon_id,
        },
    }


def test_revalidate_purges_observations_no_longer_fungi(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    upsert_observations(con, [_cached_row(1), _cached_row(2), _cached_row(3)])

    with patch("foray.ingest.fetch_observations") as mock_fetch:
        # iNat now says obs 1 and 2 are a ladybug (Insecta), obs 3 is still genuinely fungal.
        mock_fetch.return_value = [
            _live_obs(1, iconic_taxon_id=47158, taxon_id=999001),  # Insecta
            _live_obs(2, iconic_taxon_id=47158, taxon_id=999002),  # Insecta
            _live_obs(3, iconic_taxon_id=47170, taxon_id=OLLA_FUNGUS),  # Fungi
        ]
        stats = revalidate(cfg_with_home, con)

    # obs 3 is confirmed-still-fungal and gets refreshed, but stays in the same genus - not a
    # reassignment (Copilot review: this counter must only count an actual genus change).
    assert stats == {OLLA_FUNGUS: {"checked": 3, "purged": 2, "reassigned": 0}}
    remaining = con.execute("SELECT id FROM observations ORDER BY id").fetchall()
    assert remaining == [(3,)]


def test_revalidate_reassigns_genus_when_species_moved_to_a_different_fungal_genus(
    con: psycopg.Connection, cfg_with_home: Settings
) -> None:
    upsert_observations(con, [_cached_row(1)])

    with patch("foray.ingest.fetch_observations") as mock_fetch:
        mock_fetch.return_value = [
            _live_obs(
                1,
                iconic_taxon_id=47170,
                taxon_id=555555,
                rank="species",
                ancestor_ids=[47170, CANTHARELLUS, 555555],
            ),
        ]
        stats = revalidate(cfg_with_home, con)

    assert stats == {OLLA_FUNGUS: {"checked": 1, "purged": 0, "reassigned": 1}}
    row = con.execute("SELECT taxon_id FROM observations WHERE id = 1").fetchone()
    assert row is not None
    assert row[0] == CANTHARELLUS


def test_revalidate_purges_ids_inat_no_longer_returns(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    upsert_observations(con, [_cached_row(1), _cached_row(2)])

    with patch("foray.ingest.fetch_observations") as mock_fetch:
        # obs 2 is gone (deleted / made private) - only 1 comes back.
        mock_fetch.return_value = [_live_obs(1, iconic_taxon_id=47170, taxon_id=OLLA_FUNGUS)]
        stats = revalidate(cfg_with_home, con)

    assert stats == {OLLA_FUNGUS: {"checked": 2, "purged": 1, "reassigned": 0}}
    remaining = con.execute("SELECT id FROM observations ORDER BY id").fetchall()
    assert remaining == [(1,)]


def test_revalidate_skips_genera_that_are_not_suspect(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    upsert_observations(con, [_cached_row(1, taxon_id=CANTHARELLUS)])

    with patch("foray.ingest.fetch_observations") as mock_fetch:
        stats = revalidate(cfg_with_home, con)

    mock_fetch.assert_not_called()
    assert stats == {}
