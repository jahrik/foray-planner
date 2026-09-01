"""Tests for the shared area-ingest skeleton (foray.ingest_base.run_area_ingest)."""

from __future__ import annotations

import psycopg

from foray.cache import record_ingest, upsert_campsites
from foray.config import Home, Settings
from foray.ingest_base import run_area_ingest

_CFG = Settings(home=Home(name="Home", lat=47.6, lng=-122.3, radius_km=50.0))

_ROW = ("demo:1", "Camp", "reported", None, None, 47.61, -122.31, "demo", "https://example.test")


def _fetch_one(**_kw: object) -> list[tuple[object, ...]]:
    return [_ROW]


def test_run_area_ingest_fetches_upserts_and_records(con: psycopg.Connection) -> None:
    count = run_area_ingest(
        _CFG, con, prefix="demo:", label="demo", noun="Demo", fetch=_fetch_one, upsert=upsert_campsites
    )
    assert count == 1
    assert con.execute("SELECT id FROM campsites").fetchall() == [("demo:1",)]
    key = f"demo:{_CFG.home.lat}:{_CFG.home.lng}:{_CFG.home.radius_km}"
    assert con.execute("SELECT row_count FROM ingest_log WHERE key = %s", [key]).fetchone() == (1,)


def test_run_area_ingest_skips_a_covered_area_without_fetching(con: psycopg.Connection) -> None:
    key = f"demo:{_CFG.home.lat}:{_CFG.home.lng}:{_CFG.home.radius_km}"
    record_ingest(con, key, 0, lat=_CFG.home.lat, lng=_CFG.home.lng, radius_km=_CFG.home.radius_km)

    def _boom(**_kw: object) -> list[tuple[object, ...]]:
        raise AssertionError("fetch must not run when the area is already covered")

    messages: list[str] = []
    count = run_area_ingest(
        _CFG,
        con,
        prefix="demo:",
        label="demo",
        noun="Demo",
        fetch=_boom,
        upsert=upsert_campsites,
        progress_cb=lambda msg, _pct: messages.append(msg),
    )
    assert count == 0
    assert messages == ["Demo already cached, skipping…"]
