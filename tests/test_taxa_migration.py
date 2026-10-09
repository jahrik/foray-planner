"""Migration 60 (issue #464): the genus-only ``fungi_genera`` table becomes ``taxa`` + a view.

Runs the migration's own statement inside a rolled-back transaction on the shared test database
(Postgres DDL is transactional), so the shared schema is left untouched."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from foray.cache.core import _MIGRATIONS

_MIGRATION_60 = dict(_MIGRATIONS)[60]


@pytest.fixture
def legacy(con: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """The database as a pre-#464 deploy left it, inside a transaction that is rolled back."""
    with con.transaction(force_rollback=True):
        con.execute("DROP VIEW fungi_genera")
        con.execute("DROP TABLE taxa, taxon_names, app_targets")
        con.execute("DROP TABLE IF EXISTS phenology_species")
        con.execute(
            """
            CREATE TABLE fungi_genera (
                taxon_id BIGINT PRIMARY KEY, name TEXT NOT NULL, common_name TEXT, observations_count INTEGER,
                class_name TEXT, order_id BIGINT, order_name TEXT, family_id BIGINT, family_name TEXT
            )
            """
        )
        con.execute(
            "INSERT INTO fungi_genera VALUES (47348, 'Cantharellus', 'Chanterelles', 90000, 'Agaricomycetes',"
            " 47350, 'Cantharellales', 48423, 'Hydnaceae'),"
            " (999999, 'Obscurella', NULL, 3, NULL, NULL, NULL, NULL, NULL)"
        )
        con.execute("DROP TABLE IF EXISTS app_genera")  # a long-lived test database may still hold the old one
        con.execute(
            "CREATE TABLE app_genera (device_id TEXT NOT NULL, taxon_id BIGINT NOT NULL,"
            " PRIMARY KEY (device_id, taxon_id))"
        )
        con.execute("INSERT INTO app_genera VALUES ('device-a', 47348)")
        yield con


def test_legacy_fungi_genera_table_is_folded_into_taxa_and_left_as_a_view(legacy: psycopg.Connection) -> None:
    legacy.execute(_MIGRATION_60)

    genus = legacy.execute(
        "SELECT rank, parent_id, ancestor_ids, common_name, observations_count FROM taxa WHERE taxon_id = 47348"
    ).fetchone()
    assert genus == ("genus", 48423, [47170, 47350, 48423], "Chanterelles", 90000)
    assert legacy.execute("SELECT rank, ancestor_ids FROM taxa WHERE taxon_id = 48423").fetchone() == (
        "family",
        [47170, 47350],
    )
    assert legacy.execute("SELECT rank FROM taxa WHERE taxon_id = 47350").fetchone() == ("order",)
    assert legacy.execute("SELECT relkind FROM pg_class WHERE relname = 'fungi_genera'").fetchone() == ("v",)
    assert legacy.execute("SELECT device_id, taxon_id FROM app_targets").fetchall() == [("device-a", 47348)]
    # A stale cron image reading the old table shape sees the same genera, with the family and order
    # names intact (the class is only known once the taxa load or a genera-refresh supplies it).
    view = legacy.execute("SELECT name, order_name, family_name FROM fungi_genera ORDER BY taxon_id").fetchall()
    assert view == [("Cantharellus", "Cantharellales", "Hydnaceae"), ("Obscurella", None, None)]


def test_migration_is_idempotent(legacy: psycopg.Connection) -> None:
    legacy.execute(_MIGRATION_60)
    legacy.execute("UPDATE taxa SET common_name = 'edited' WHERE taxon_id = 47348")

    legacy.execute(_MIGRATION_60)  # two instances can both see 60 as unapplied and race to run it

    assert legacy.execute("SELECT common_name FROM taxa WHERE taxon_id = 47348").fetchone() == ("edited",)
    assert legacy.execute("SELECT count(*) FROM taxa").fetchone() == (4,)


def test_shared_schema_has_the_new_tables_and_the_view(con: psycopg.Connection) -> None:
    assert con.execute(
        "SELECT to_regclass('taxa'), to_regclass('taxon_names'), to_regclass('app_targets')"
    ).fetchone() == ("taxa", "taxon_names", "app_targets")
    assert con.execute("SELECT relkind FROM pg_class WHERE relname = 'fungi_genera'").fetchone() == ("v",)
    assert con.execute(
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_name = 'observations' AND column_name = 'species_id'"
    ).fetchone() == (1,)
