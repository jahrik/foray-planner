"""Rainfall enrichment against the test Postgres (issue #226): the per-observation antecedent
backfill, the never-record-a-partial rule, the region means, and the recent-rain layer."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray import cache
from foray.config import Settings
from foray.scoring import build_phenology, rank_destinations
from foray.scoring.regions import region_precip_obs
from foray.sources import ingest, precip

CELL = 0.25
GENUS = 55555
# One grid cell around (45.1, -122.1); a second well away at (48.0, -120.0).
LAT, LNG = 45.1, -122.1


def _seed_obs(con: psycopg.Connection, rows: list[tuple]) -> None:
    with con.cursor() as cur:
        cur.execute(
            "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)", (GENUS, "Boletus", "Boletes")
        )
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade, obscured)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'research', %s)",
            rows,
        )


def test_backfill_writes_both_windows_from_one_cell_fetch(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = dt.date(2026, 5, 20)
    _seed_obs(con, [(1, GENUS, LAT, LNG, observed, 5, False), (2, GENUS, LAT + 0.01, LNG + 0.01, observed, 5, False)])

    calls: list[tuple] = []

    def fake_archive(
        clat: float, clng: float, start: dt.date, end: dt.date, *, client: object = None
    ) -> dict[dt.date, float | None]:
        calls.append((start, end))
        return {start + dt.timedelta(days=i): 2.0 for i in range((end - start).days + 1)}

    monkeypatch.setattr(precip, "fetch_archive_precip", fake_archive)

    updated = ingest.backfill_precip(con, cell_deg=CELL)
    assert updated == 2
    assert len(calls) == 1  # both observations share a cell -> one archive call
    row = con.execute("SELECT precip_7d_mm, precip_30d_mm FROM observations WHERE id = 1").fetchone()
    assert row == (14.0, 60.0)  # 7 * 2.0, 30 * 2.0
    # The daily values were cached in precip_daily for reuse.
    cached = con.execute("SELECT count(*) FROM precip_daily WHERE cell_id != ''").fetchall()
    assert cached and cached[0][0] > 0


def test_partial_window_leaves_column_null_and_stays_pending(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = dt.date(2026, 5, 20)
    _seed_obs(con, [(1, GENUS, LAT, LNG, observed, 5, False)])

    def fake_archive(
        clat: float, clng: float, start: dt.date, end: dt.date, *, client: object = None
    ) -> dict[dt.date, float | None]:
        series: dict[dt.date, float | None] = {start + dt.timedelta(days=i): 1.0 for i in range((end - start).days + 1)}
        series[end] = None  # ERA5 still lagging on the most recent day (inside the 7d window)
        return series

    monkeypatch.setattr(precip, "fetch_archive_precip", fake_archive)

    assert ingest.backfill_precip(con, cell_deg=CELL) == 0
    assert con.execute("SELECT precip_7d_mm FROM observations WHERE id = 1").fetchone() == (None,)
    still_pending = cache.observations_missing_precip(con, 10)
    assert [obs_id for obs_id, *_ in still_pending] == [1]


def test_7d_lands_while_30d_stays_pending(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    observed = dt.date(2026, 5, 20)
    _seed_obs(con, [(1, GENUS, LAT, LNG, observed, 5, False)])

    def fake_archive(
        clat: float, clng: float, start: dt.date, end: dt.date, *, client: object = None
    ) -> dict[dt.date, float | None]:
        # All present except one day 20 back - inside the 30d window, outside the 7d window.
        series: dict[dt.date, float | None] = {start + dt.timedelta(days=i): 1.0 for i in range((end - start).days + 1)}
        series[observed - dt.timedelta(days=20)] = None
        return series

    monkeypatch.setattr(precip, "fetch_archive_precip", fake_archive)
    assert ingest.backfill_precip(con, cell_deg=CELL) == 1
    row = con.execute("SELECT precip_7d_mm, precip_30d_mm FROM observations WHERE id = 1").fetchone()
    assert row == (7.0, None)
    # Still pending so the 30d column gets retried once ERA5 fills that day.
    assert [obs_id for obs_id, *_ in cache.observations_missing_precip(con, 10)] == [1]


def test_region_precip_obs_mean_excludes_the_decoy(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    observed = dt.date(2026, 5, 20)
    _seed_obs(
        con,
        [
            (1, GENUS, LAT, LNG, observed, 5, False),
            (2, GENUS, LAT + 0.01, LNG + 0.01, observed, 5, False),
            (3, GENUS, LAT + 0.02, LNG + 0.02, observed, 5, True),  # obscured -> decoy, excluded
        ],
    )

    def fake_archive(
        clat: float, clng: float, start: dt.date, end: dt.date, *, client: object = None
    ) -> dict[dt.date, float | None]:
        # 1 mm/day; the obscured row (id 3) is enriched by hand below to a decoy value.
        return {start + dt.timedelta(days=i): 1.0 for i in range((end - start).days + 1)}

    monkeypatch.setattr(precip, "fetch_archive_precip", fake_archive)
    ingest.backfill_precip(con, cell_deg=CELL)
    con.execute("UPDATE observations SET precip_7d_mm = 100.0, precip_30d_mm = 400.0 WHERE id = 3")

    build_phenology(con, CELL)
    region_id = next(iter(region_precip_obs(con, [r for (r,) in con.execute("SELECT region_id FROM regions")])))
    means = region_precip_obs(con, [region_id])[region_id]
    assert means["precip_obs_7d_mm"] == 7.0  # the decoy's 100.0 is filtered out


def test_refresh_precipitation_populates_the_layer_and_flows_to_ranking(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_obs(con, [(i, GENUS, LAT, LNG, dt.date(2026, 5, 15), 5, False) for i in range(1, 6)])
    build_phenology(con, CELL)

    def fake_recent(
        clat: float, clng: float, *, past_days: int = 30, client: object = None
    ) -> dict[dt.date, float | None]:
        today = dt.date.today()
        return {today - dt.timedelta(days=i): 3.0 for i in range(past_days + 1)}

    monkeypatch.setattr(precip, "fetch_recent_precip", fake_recent)
    written = ingest.refresh_precipitation(con, Settings())
    assert written >= 1

    ranked = rank_destinations(
        con, months=[5], taxon_ids=[GENUS], home_lat=LAT, home_lng=LNG, radius_km=200, cell_deg=CELL
    )
    assert ranked
    assert ranked[0].precip_recent_7d_mm == 21.0  # 7 * 3.0
    assert ranked[0].precip_recent_14d_mm == 42.0
