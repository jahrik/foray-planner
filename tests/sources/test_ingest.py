"""Tests for home-radius ingest (issue #79 Phase 4: whole-Fungi-kingdom query, one genus
resolved per observation via its own taxon ancestry) - no network; iNat calls mocked."""

from __future__ import annotations

import threading
from unittest.mock import patch

import httpx
import psycopg
import pytest

from foray.cache import upsert_taxa
from foray.config import Settings
from foray.sources.ingest import backfill_elevations, ingest

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
    upsert_taxa(
        con,
        [
            {"taxon_id": MOREL, "name": "Morchella", "common_name": "Morels", "rank": "genus"},
            {"taxon_id": CHANTERELLE, "name": "Cantharellus", "common_name": "Chanterelles", "rank": "genus"},
        ],
    )
    return Settings()


SCOPE_ROOT = 47170  # the default scope root (Fungi): every in-scope observation's lineage runs through it


def _fake_obs(
    obs_id: int,
    taxon_id: int,
    *,
    rank: str = "genus",
    ancestor_ids: list[int] | None = None,
) -> dict:
    return {
        "id": obs_id,
        "geojson": {"coordinates": [-122.3, 47.6]},
        "observed_on": "2024-05-15",
        "quality_grade": "research",
        "positional_accuracy": 10,
        "taxon": {
            "id": taxon_id,
            "rank": rank,
            "ancestor_ids": ancestor_ids or [SCOPE_ROOT, taxon_id],
        },
    }


def test_ingest_queries_the_scope_roots(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    """A single query over the configured scope roots, not one call per genus."""
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(1, MOREL), _fake_obs(2, CHANTERELLE)])
        counts = ingest(cfg_with_home, con)

    assert counts == {MOREL: 1, CHANTERELLE: 1}
    mock_iter.assert_called_once()
    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["taxon_id"] == [47170]  # Settings.scope_roots default
    assert call_kwargs["lat"] == 47.6
    assert call_kwargs["radius_km"] == 200

    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 2


def test_ingest_backfills_elevation_for_new_observations(
    con: psycopg.Connection, cfg_with_home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foray.sources import elevation

    seen: list[list[tuple[float, float]]] = []

    def fake_batch(coords: list[tuple[float, float]], **_kwargs: object) -> list[int | None]:
        seen.append(list(coords))
        return [123] * len(coords)

    monkeypatch.setattr(elevation, "lookup_batch", fake_batch)

    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(1, MOREL), _fake_obs(2, CHANTERELLE)])
        ingest(cfg_with_home, con)

    assert seen == [[(47.6, -122.3), (47.6, -122.3)]]
    rows = con.execute("SELECT elevation_m FROM observations ORDER BY id").fetchall()
    assert [row[0] for row in rows] == [123, 123]


def test_ingest_survives_elevation_backfill_network_failure(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    # The autouse `_no_elevation_network` fixture makes lookup_batch raise; ingest must still
    # complete and leave elevation_m NULL for a later backfill.
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(1, MOREL)])
        counts = ingest(cfg_with_home, con)

    assert counts == {MOREL: 1}
    row = con.execute("SELECT elevation_m FROM observations WHERE id = 1").fetchone()
    assert row is not None and row[0] is None


def _seed_unenriched(con: psycopg.Connection, count: int) -> None:
    rows = [(obs_id, MOREL, 47.6, -122.3, "2024-05-15", 5, "research", 10) for obs_id in range(1, count + 1)]
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade,"
            " positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )


def test_backfill_elevations_respects_max_points(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from foray.sources import elevation

    _seed_unenriched(con, 250)
    monkeypatch.setattr(elevation, "lookup_batch", lambda coords, **_kw: [500] * len(coords))

    assert backfill_elevations(con, max_points=150) == 150
    row = con.execute("SELECT count(*) FROM observations WHERE elevation_m IS NULL").fetchone()
    assert row is not None and row[0] == 100


def test_backfill_elevations_stops_cleanly_on_rate_limit(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foray.sources import elevation

    _seed_unenriched(con, 250)
    calls = {"n": 0}

    def flaky(coords: list[tuple[float, float]], **_kw: object) -> list[int]:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise httpx.HTTPStatusError(
                "rate limited", request=httpx.Request("GET", "http://x"), response=httpx.Response(429)
            )
        return [500] * len(coords)

    monkeypatch.setattr(elevation, "lookup_batch", flaky)

    # First batch of 100 lands; the 429 on the second stops the run without raising.
    assert backfill_elevations(con) == 100


def test_ingest_resolves_genus_from_species_rank_ancestry(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    """A species-rank observation gets tagged with its genus ancestor's taxon_id, not its own."""
    species_taxon_id = 555555  # e.g. Morchella esculenta, not in the catalog itself
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter(
            [_fake_obs(1, species_taxon_id, rank="species", ancestor_ids=[47170, MOREL, species_taxon_id])]
        )
        counts = ingest(cfg_with_home, con)

    assert counts == {MOREL: 1}
    row = con.execute("SELECT taxon_id, species_id FROM observations WHERE id = 1").fetchone()
    assert row is not None
    assert row == (MOREL, species_taxon_id)  # genus is the hot key; the species rides alongside


def test_ingest_skips_observations_with_no_known_genus_ancestor(
    con: psycopg.Connection, cfg_with_home: Settings
) -> None:
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter(
            [_fake_obs(1, MOREL), _fake_obs(2, 999999, rank="family", ancestor_ids=[47170, 999999])]
        )
        counts = ingest(cfg_with_home, con)

    assert counts == {MOREL: 1}
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 1


def test_ingest_skips_observations_outside_the_scope_roots(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    """A handful of fungal genus names are homonyms of established animal genera (fungal Olla vs.
    the ladybug genus, etc): the animal has its own taxon id and a lineage that never runs through a
    scope root, so it is not admitted even though its name matches a cataloged genus."""
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter(
            [
                _fake_obs(1, MOREL),
                _fake_obs(2, 424242, ancestor_ids=[1, 47158, 424242]),  # an Insecta genus
            ]
        )
        counts = ingest(cfg_with_home, con)

    assert counts == {MOREL: 1}
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 1


def test_ingest_records_a_single_ingest_log_entry(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(1, MOREL)])
        ingest(cfg_with_home, con)

    log_row = con.execute("SELECT key FROM ingest_log WHERE key LIKE %s", ["obs:fungi:%"]).fetchone()
    assert log_row is not None


def test_ingest_incremental_overlaps_by_a_week(con: psycopg.Connection, cfg_with_home: Settings) -> None:
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count, lat, lng, radius_km) "
        "VALUES ('obs:fungi:47.6:-122.3:200.0:2015-01-01:2024-06-01', now(), 5, 47.6, -122.3, 200)"
    )
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([])
        ingest(cfg_with_home, con)

    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["d1"] >= "2024-05-25"


def test_ingest_ignores_place_scoped_coverage_with_multiple_countries(
    con: psycopg.Connection, cfg_with_home: Settings
) -> None:
    """With more than one configured country there's no lat/lng-to-country containment check,
    so a recently-ingested *other* country must not be allowed to advance window_start for a
    home that might not even be in it - falls back to the full since_year window instead."""
    cfg = cfg_with_home.model_copy(
        update={
            "countries": [
                *cfg_with_home.countries,
                cfg_with_home.countries[0].model_copy(update={"name": "Other", "place_id": 999}),
            ]
        }
    )
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count) VALUES (%s, now(), 5)",
        ["obs:fungi:place:999:2015-01-01:2024-06-01"],
    )
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([])
        ingest(cfg, con)

    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["d1"] == f"{cfg.ingest.since_year}-01-01"


def test_ingest_incremental_overlaps_with_country_scoped_coverage(
    con: psycopg.Connection, cfg_with_home: Settings
) -> None:
    """A place-scoped ingest_log row (ingest_region()/the nightly --countries cron, or a bulk
    load - no lat/lng, see cache.record_ingest) must still narrow the window - not just the
    lat/lng-scoped rows ingest() itself writes (issue #141)."""
    place_id = cfg_with_home.countries[0].place_id
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count) VALUES (%s, now(), 5)",
        [f"obs:fungi:place:{place_id}:2015-01-01:2024-06-01"],
    )
    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([])
        ingest(cfg_with_home, con)

    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["d1"] >= "2024-05-25"


def test_ingest_fails_fast_on_empty_catalog(con: psycopg.Connection, monkeypatch) -> None:
    """A never-refreshed/misconfigured taxa catalog must abort loudly, not silently
    ingest only already-genus-rank observations while dropping every finer-rank one."""
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    cfg = Settings()

    with pytest.raises(RuntimeError, match="genera-refresh"):
        ingest(cfg, con)


def test_ingest_cancelled_run_keeps_rows_but_skips_record_ingest(
    con: psycopg.Connection, cfg_with_home: Settings
) -> None:
    """A cancelled run must not advance the incremental cursor (record_ingest) - a later run's
    latest_obs_date() would otherwise treat the un-fetched rest of the window as already
    covered. Rows already upserted before cancellation are real data and stay."""
    abort_event = threading.Event()

    def obs_stream():
        yield _fake_obs(1, MOREL)
        abort_event.set()  # cancelled after the first observation is queued for upsert
        yield _fake_obs(2, CHANTERELLE)  # never reached - loop checks abort_event first

    with patch("foray.sources.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = obs_stream()
        counts = ingest(cfg_with_home, con, abort_event=abort_event)

    assert counts == {MOREL: 1}
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 1

    log_row = con.execute("SELECT key FROM ingest_log WHERE key LIKE %s", ["obs:fungi:%"]).fetchone()
    assert log_row is None


def test_to_row_keeps_the_observations_own_identification() -> None:
    """Issue #449: besides the genus taxon_id, a row carries the observation's own taxon name."""
    from foray.sources.ingest import _to_row

    obs = {
        "id": 7,
        "location": "47.6,-122.3",
        "observed_on": "2024-05-01",
        "quality_grade": "research",
        "taxon": {"name": "Morchella importuna", "preferred_common_name": "Landscape Morel"},
    }
    row = _to_row(obs, 56830, 1062674)
    assert row is not None
    assert row[1] == 56830
    assert row[-3:] == ("Morchella importuna", "Landscape Morel", 1062674)
