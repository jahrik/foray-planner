"""cache.py upsert tests on hand-built fixtures - no network (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt

import psycopg

from foray.cache import upsert_observations

_ROW = (
    1,  # id
    111,  # taxon_id
    47.6,  # lat
    -122.3,  # lng
    dt.date(2022, 4, 15),  # observed_on
    4,  # month
    2022,  # year
    "needs_id",  # quality_grade
    10,  # positional_accuracy
    "Seattle, WA",  # place_guess
    "https://inaturalist.org/observations/1",  # uri
    False,  # obscured
)


def _insert(con: psycopg.Connection, row: tuple) -> None:
    upsert_observations(
        con,
        [row],
    )


def test_reupsert_heals_taxon_id_and_quality_grade(con: psycopg.Connection) -> None:
    # First write: wrong taxon_id (e.g. a since-corrected iNat ID) and not-yet-research-grade.
    _insert(con, _ROW)

    reidentified = (*_ROW[:1], 222, *_ROW[2:7], "research", *_ROW[8:])
    _insert(con, reidentified)

    row = con.execute("SELECT taxon_id, quality_grade FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row is not None
    taxon_id, quality_grade = row
    assert taxon_id == 222
    assert quality_grade == "research"


def test_reupsert_preserves_place_guess_when_new_value_is_null(con: psycopg.Connection) -> None:
    _insert(con, _ROW)

    # A later fetch that doesn't carry place_guess shouldn't blank out what's already stored.
    row_without_place_guess = (*_ROW[:9], None, *_ROW[10:])
    _insert(con, row_without_place_guess)

    row = con.execute("SELECT place_guess FROM observations WHERE id = %s", [_ROW[0]]).fetchone()
    assert row is not None
    assert row[0] == "Seattle, WA"
