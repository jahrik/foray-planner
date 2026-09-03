"""The one-off geom backfill (scripts/backfill_geom.py) - fills `<table>.geom` for rows that
predate the PostGIS Phase 0 migration. The write triggers cover new rows; this covers the
backlog, so it must handle a NULL-geom row, a malformed-GeoJSON row (skip, don't wedge), and
be idempotent on a re-run."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import LiteralString

import psycopg

_spec = importlib.util.spec_from_file_location(
    "backfill_geom", Path(__file__).parent.parent / "scripts" / "backfill_geom.py"
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
fill = _mod._fill
TABLES = _mod.TABLES


def _null_the_geom(con: psycopg.Connection, table: LiteralString) -> None:
    """Simulate a pre-migration row: clear geom without firing the trigger's recompute."""
    con.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
    con.execute(f"UPDATE {table} SET geom = NULL")
    con.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")


def test_fills_null_point_geom(con: psycopg.Connection) -> None:
    con.execute(
        "INSERT INTO observations (id, taxon_id, lat, lng, quality_grade) VALUES (1, 9, 44.0, -121.0, 'research')"
    )
    _null_the_geom(con, "observations")
    expr, predicate = TABLES["observations"]

    filled, skipped = fill(con, "observations", expr, predicate, batch_size=100, sleep_s=0.0, dry_run=False)

    assert (filled, skipped) == (1, 0)
    row = con.execute("SELECT ST_Y(geom::geometry), ST_X(geom::geometry) FROM observations WHERE id = 1").fetchone()
    assert row == (44.0, -121.0)


def test_skips_malformed_geojson_row_without_wedging(con: psycopg.Connection) -> None:
    con.execute(
        "INSERT INTO trails (id, name, geojson) VALUES ('ok', 'T', %s), ('bad', 'B', %s)",
        ['{"type": "Point", "coordinates": [-120.0, 45.0]}', '{"type": "Nope"}'],
    )
    _null_the_geom(con, "trails")
    expr, predicate = TABLES["trails"]

    filled, skipped = fill(con, "trails", expr, predicate, batch_size=1, sleep_s=0.0, dry_run=False)

    assert filled == 1
    assert skipped == 1
    geoms = dict(con.execute("SELECT id, geom IS NULL FROM trails").fetchall())
    assert geoms == {"ok": False, "bad": True}
    # A re-run fills nothing new and re-flags the one bad row (then stops - `id <> ALL(bad_ids)`
    # keeps it from looping); the good row is not touched again.
    assert fill(con, "trails", expr, predicate, batch_size=1, sleep_s=0.0, dry_run=False) == (0, 1)
