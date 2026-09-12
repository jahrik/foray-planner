from __future__ import annotations

from datetime import date

import psycopg
import pytest
from psycopg import sql

from foray import ingest_bulk
from foray.config import Settings, Spaces

_UNCONFIGURED = Settings()
_CONFIGURED = Settings(spaces=Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk"))


@pytest.fixture(autouse=True)
def _clear_bulk_snapshot_meta(con: psycopg.Connection):
    # `meta` isn't in conftest's per-test TRUNCATE list (it holds slow-changing app state, not
    # test fixture data) - clear just this module's keys so one test's recorded snapshot can't
    # leak into another's `last_loaded_snapshot` check.
    con.execute("DELETE FROM meta WHERE key LIKE 'bulk_snapshot:%'")
    yield
    con.execute("DELETE FROM meta WHERE key LIKE 'bulk_snapshot:%'")


def test_stage_snapshot_requires_spaces_configured() -> None:
    with pytest.raises(RuntimeError, match="FORAY_SPACES"):
        ingest_bulk.stage_snapshot(_UNCONFIGURED, "padus")


def test_stage_snapshot_unknown_source_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="padus"):
        ingest_bulk.stage_snapshot(_CONFIGURED, "padus")


def test_stage_snapshot_calls_registered_stager(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Settings, date]] = []
    monkeypatch.setitem(ingest_bulk.STAGERS, "padus", lambda cfg, d: calls.append((cfg, d)))

    result = ingest_bulk.stage_snapshot(_CONFIGURED, "padus", date(2026, 1, 1))

    assert result == date(2026, 1, 1)
    assert calls == [(_CONFIGURED, date(2026, 1, 1))]


def test_ingest_bulk_requires_spaces_configured(con: psycopg.Connection) -> None:
    with pytest.raises(RuntimeError, match="FORAY_SPACES"):
        ingest_bulk.ingest_bulk(con, _UNCONFIGURED, "padus")


def test_ingest_bulk_unknown_source_raises_keyerror(con: psycopg.Connection) -> None:
    with pytest.raises(KeyError, match="padus"):
        ingest_bulk.ingest_bulk(con, _CONFIGURED, "padus")


def test_ingest_bulk_no_snapshots_staged_returns_none(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ingest_bulk.LOADERS, "padus", lambda con, cfg, d: None)
    monkeypatch.setattr(ingest_bulk, "list_snapshot_dates", lambda cfg, source: [])

    assert ingest_bulk.ingest_bulk(con, _CONFIGURED, "padus") is None


def test_ingest_bulk_loads_newest_unseen_snapshot(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[date] = []
    monkeypatch.setitem(ingest_bulk.LOADERS, "padus", lambda con, cfg, d: loaded.append(d))
    monkeypatch.setattr(ingest_bulk, "list_snapshot_dates", lambda cfg, source: [date(2026, 1, 1), date(2026, 1, 8)])

    result = ingest_bulk.ingest_bulk(con, _CONFIGURED, "padus")

    assert result == date(2026, 1, 8)
    assert loaded == [date(2026, 1, 8)]
    assert ingest_bulk.last_loaded_snapshot(con, "padus") == date(2026, 1, 8)


def test_ingest_bulk_skips_already_loaded_snapshot(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    ingest_bulk.record_snapshot_loaded(con, "padus", date(2026, 1, 8))
    loaded: list[date] = []
    monkeypatch.setitem(ingest_bulk.LOADERS, "padus", lambda con, cfg, d: loaded.append(d))
    monkeypatch.setattr(ingest_bulk, "list_snapshot_dates", lambda cfg, source: [date(2026, 1, 8)])

    result = ingest_bulk.ingest_bulk(con, _CONFIGURED, "padus")

    assert result is None
    assert loaded == []


def test_copy_and_swap_leaves_table_untouched_on_failure(con: psycopg.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS ingest_bulk_test_table (id INT)")
    con.execute("INSERT INTO ingest_bulk_test_table VALUES (1)")

    def boom(con: psycopg.Connection, staging: str) -> None:
        raise ValueError("copy failed")

    try:
        with pytest.raises(ValueError, match="copy failed"):
            ingest_bulk.copy_and_swap(
                con,
                "ingest_bulk_test_table",
                "CREATE TABLE ingest_bulk_test_table_staging (id INT)",
                boom,
                "BEGIN; DROP TABLE ingest_bulk_test_table; "
                "ALTER TABLE ingest_bulk_test_table_staging RENAME TO ingest_bulk_test_table; COMMIT",
            )

        assert con.execute("SELECT id FROM ingest_bulk_test_table").fetchall() == [(1,)]
    finally:
        con.execute("DROP TABLE IF EXISTS ingest_bulk_test_table")
        con.execute("DROP TABLE IF EXISTS ingest_bulk_test_table_staging")


def test_copy_and_swap_replaces_table_on_success(con: psycopg.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS ingest_bulk_test_table (id INT)")
    con.execute("INSERT INTO ingest_bulk_test_table VALUES (1)")

    def load_two(con: psycopg.Connection, staging: str) -> None:
        con.execute(sql.SQL("INSERT INTO {} VALUES (2), (3)").format(sql.Identifier(staging)))

    try:
        ingest_bulk.copy_and_swap(
            con,
            "ingest_bulk_test_table",
            "CREATE TABLE ingest_bulk_test_table_staging (id INT)",
            load_two,
            "DROP TABLE ingest_bulk_test_table; "
            "ALTER TABLE ingest_bulk_test_table_staging RENAME TO ingest_bulk_test_table",
        )

        assert con.execute("SELECT id FROM ingest_bulk_test_table ORDER BY id").fetchall() == [(2,), (3,)]
    finally:
        con.execute("DROP TABLE IF EXISTS ingest_bulk_test_table")
        con.execute("DROP TABLE IF EXISTS ingest_bulk_test_table_staging")
