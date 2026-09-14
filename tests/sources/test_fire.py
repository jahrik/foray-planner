"""Wildfire ingest (issue #227): feature parsing, the active lane's replace semantics, the
severity join (issue #335 PR 4 - now RAVG-fed, see test_ravg.py for its own stager/loader), and
refresh_fire end to end against mocked ArcGIS services."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import psycopg

from foray import cache
from foray.config import CoverageRegion, Settings
from foray.sources import fire

THIS_YEAR = dt.date.today().year


def _poly(lng: float, lat: float) -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": [[[lng, lat], [lng + 0.1, lat], [lng + 0.1, lat + 0.1], [lng, lat]]]}


def _feature(props: dict[str, Any], *, geometry: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "Feature", "geometry": geometry or _poly(-121.0, 44.0), "properties": props}


def _geojson(*features: dict[str, Any]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features), "exceededTransferLimit": False}


def _cfg() -> Settings:
    return Settings(coverage=[CoverageRegion(name="OR", place_id=1, bbox=(-124.6, 42.0, -116.4, 46.3))])


def _transport(routes: dict[str, dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, payload in routes.items():
            if fragment in str(request.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json=_geojson())

    return httpx.MockTransport(handler)


def test_feature_row_parses_an_active_perimeter() -> None:
    row = fire._feature_row(
        source_key=fire.LANE_ACTIVE,
        feature=_feature(
            {
                "OBJECTID": 7,
                "poly_IncidentName": "Cedar Creek",
                "irwin_IrwinID": "{ABC-123}",
                "attr_PercentContained": 40,
                "poly_GISAcres": 12000.5,
                "attr_FireDiscoveryDateTime": int(dt.datetime(THIS_YEAR, 7, 1, tzinfo=dt.UTC).timestamp() * 1000),
            }
        ),
        status="active",
        is_point=False,
    )
    assert row is not None
    assert row[0] == "wfigs_active:7"
    assert row[3] == "{ABC-123}"  # irwin_id
    assert row[4] == "Cedar Creek"
    assert row[5] == "active"
    assert row[6] == THIS_YEAR  # fire_year from the discovery date
    assert row[8] == 40.0  # percent_contained
    assert row[9] == 12000.5  # gis_acres


def test_feature_row_reads_the_history_layers_incident_field() -> None:
    # InterAgencyFirePerimeterHistory's actual name field is "INCIDENT", not any of the
    # WFIGS-current-style candidates - discovered live 2026-09-14 debugging why local dev's
    # entire history lane had fallen through to "Unnamed fire" (issue #335 PR 4's RAVG join
    # never matched anything as a result).
    row = fire._feature_row(
        source_key=fire.LANE_HISTORY,
        feature=_feature({"OBJECTID": 1, "INCIDENT": "Big Bug", "FIRE_YEAR": THIS_YEAR}),
        status="historical",
        is_point=False,
    )
    assert row is not None
    assert row[4] == "Big Bug"


def test_replace_fire_lane_drops_contained_fires(con: psycopg.Connection) -> None:
    def make(fire_id: str) -> tuple[Any, ...]:
        return (
            fire_id,
            fire.LANE_ACTIVE,
            fire_id.split(":")[1],
            None,
            fire_id,
            "active",
            THIS_YEAR,
            None,
            10.0,
            100.0,
            "http://x",
            False,
            44.05,
            -120.95,
            "{}",
            dt.datetime.now(dt.UTC),
        )

    cache.replace_fire_lane(con, fire.LANE_ACTIVE, [make("wfigs_active:1"), make("wfigs_active:2")])
    assert {r[0] for r in con.execute("SELECT id FROM fire_perimeters")} == {"wfigs_active:1", "wfigs_active:2"}
    # Fire 2 contained -> gone from the source's next refresh.
    cache.replace_fire_lane(con, fire.LANE_ACTIVE, [make("wfigs_active:1")])
    assert {r[0] for r in con.execute("SELECT id FROM fire_perimeters")} == {"wfigs_active:1"}
    # A different lane is untouched by the active-lane replace.
    con.execute("INSERT INTO fire_perimeters (id, source_key) VALUES ('perimeter_history:9', 'perimeter_history')")
    cache.replace_fire_lane(con, fire.LANE_ACTIVE, [])
    assert {r[0] for r in con.execute("SELECT id FROM fire_perimeters")} == {"perimeter_history:9"}


def test_apply_fire_severity_joins_on_irwin_id(con: psycopg.Connection) -> None:
    con.execute(
        "INSERT INTO fire_perimeters (id, source_key, irwin_id, status, fire_year) "
        "VALUES ('perimeter_history:1', 'perimeter_history', '{IRW-1}', 'historical', %s)",
        [THIS_YEAR - 2],
    )
    updated = cache.apply_fire_severity(con, [("irwin_id", "{IRW-1}", 5.0, 300.0, 120.0, 20.0, "low", "MT-EVENT-1")])
    assert updated == 1
    row = con.execute(
        "SELECT dominant_severity, severity_low_acres, mtbs_fire_id FROM fire_perimeters WHERE id = %s",
        ["perimeter_history:1"],
    ).fetchone()
    assert row == ("low", 300.0, "MT-EVENT-1")


def test_apply_fire_severity_joins_on_name_year(con: psycopg.Connection) -> None:
    # issue #335 PR 4 (RAVG) - no shared id field with our WFIGS-derived rows, so this source
    # matches on a normalized name + year instead. Also checks the "Fire" suffix is normalized
    # away, since RAVG and WFIGS disagree on carrying it.
    con.execute(
        "INSERT INTO fire_perimeters (id, source_key, name, status, fire_year) "
        "VALUES ('perimeter_history:2', 'perimeter_history', 'No Man Fire', 'historical', %s)",
        [THIS_YEAR - 1],
    )
    updated = cache.apply_fire_severity(
        con, [("name_year", ("NO MAN", THIS_YEAR - 1), 0.0, 50.0, 20.0, 5.0, "low", None)]
    )
    assert updated == 1
    row = con.execute(
        "SELECT dominant_severity, severity_low_acres FROM fire_perimeters WHERE id = %s",
        ["perimeter_history:2"],
    ).fetchone()
    assert row == ("low", 50.0)


def test_apply_fire_severity_name_year_skips_ambiguous_matches(con: psycopg.Connection) -> None:
    # Two unrelated fires nationwide can share a common name in the same year (Copilot review,
    # PR #366) - different irwin_id (or both NULL, different id) means "different fire", and the
    # update must not guess which one RAVG's row was actually about.
    con.execute(
        "INSERT INTO fire_perimeters (id, source_key, name, status, fire_year) "
        "VALUES ('perimeter_history:or', 'perimeter_history', 'Bear Fire', 'historical', %s), "
        "('perimeter_history:ca', 'perimeter_history', 'Bear Fire', 'historical', %s)",
        [THIS_YEAR - 1, THIS_YEAR - 1],
    )
    updated = cache.apply_fire_severity(
        con, [("name_year", ("BEAR", THIS_YEAR - 1), 0.0, 50.0, 20.0, 5.0, "low", None)]
    )
    assert updated == 0
    rows = con.execute("SELECT dominant_severity FROM fire_perimeters WHERE name = 'Bear Fire'").fetchall()
    assert all(row[0] is None for row in rows)


def test_apply_fire_severity_name_year_updates_both_lanes_of_the_same_fire(con: psycopg.Connection) -> None:
    # The active/history lanes can carry the same real fire twice during the brief overlap
    # window - both rows share irwin_id, so this is NOT the ambiguous-match case above and both
    # should still be updated.
    con.execute(
        "INSERT INTO fire_perimeters (id, source_key, name, status, fire_year, irwin_id) "
        "VALUES ('wfigs_active:1', 'wfigs_active', 'Rabbit Fire', 'active', %s, '{X-1}'), "
        "('perimeter_history:1', 'perimeter_history', 'Rabbit Fire', 'historical', %s, '{X-1}')",
        [THIS_YEAR - 1, THIS_YEAR - 1],
    )
    updated = cache.apply_fire_severity(
        con, [("name_year", ("RABBIT", THIS_YEAR - 1), 0.0, 50.0, 20.0, 5.0, "low", None)]
    )
    assert updated == 2


def test_refresh_fire_end_to_end(con: psycopg.Connection) -> None:
    active = _geojson(
        _feature({"OBJECTID": 1, "poly_IncidentName": "Active One", "irwin_IrwinID": "{A1}", "poly_GISAcres": 500})
    )
    history = _geojson(
        _feature({"OBJECTID": 10, "FIRE_NAME": "Old Burn", "FIRE_YEAR": THIS_YEAR - 1, "IRWINID": "{H1}"})
    )
    points = _geojson(
        _feature(
            {"OBJECTID": 2, "IncidentName": "New Start"},
            geometry={"type": "Point", "coordinates": [-121.0, 44.0]},
        )
    )
    client = httpx.Client(
        transport=_transport(
            {
                "Perimeters_Current": active,
                "Incident_Locations_Current": points,
                "InterAgencyFirePerimeterHistory": history,
            }
        )
    )
    counts = fire.refresh_fire(con, _cfg(), client=client)
    assert counts == {"active": 1, "points": 1, "history": 1}
    rows = {r[0]: r for r in con.execute("SELECT id, status FROM fire_perimeters")}
    assert rows["wfigs_active:1"][1] == "active"


def test_refresh_fire_empty_response_guard_keeps_cached_active_rows(con: psycopg.Connection) -> None:
    # issue #332: an empty response on a replace-semantics lane, with rows already cached, is
    # treated as a source hiccup - the cached rows are kept, not wiped to zero.
    for i in range(6):
        con.execute(
            "INSERT INTO fire_perimeters (id, source_key, status) VALUES (%s, %s, 'active')",
            [f"wfigs_active:{i}", fire.LANE_ACTIVE],
        )
    client = httpx.Client(
        transport=_transport(
            {
                "Perimeters_Current": _geojson(),
                "Incident_Locations_Current": _geojson(),
                "InterAgencyFirePerimeterHistory": _geojson(),
            }
        )
    )

    counts = fire.refresh_fire(con, _cfg(), client=client)

    assert counts["active"] == 0
    cached = con.execute("SELECT count(*) FROM fire_perimeters WHERE source_key = %s", [fire.LANE_ACTIVE]).fetchone()
    assert cached is not None and cached[0] == 6


def test_refresh_fire_empty_response_below_guard_still_clears_the_lane(con: psycopg.Connection) -> None:
    # Below the guard threshold, an empty response behaves as before - the source legitimately
    # reporting zero active fires clears the (small) cached lane.
    con.execute(
        "INSERT INTO fire_perimeters (id, source_key, status) VALUES ('wfigs_active:0', %s, 'active')",
        [fire.LANE_ACTIVE],
    )
    client = httpx.Client(
        transport=_transport(
            {
                "Perimeters_Current": _geojson(),
                "Incident_Locations_Current": _geojson(),
                "InterAgencyFirePerimeterHistory": _geojson(),
            }
        )
    )

    counts = fire.refresh_fire(con, _cfg(), client=client)

    assert counts["active"] == 0
    cached = con.execute("SELECT count(*) FROM fire_perimeters WHERE source_key = %s", [fire.LANE_ACTIVE]).fetchone()
    assert cached is not None and cached[0] == 0


def test_refresh_fire_skips_one_bad_lane(con: psycopg.Connection) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "Perimeters_Current" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=_geojson(_feature({"OBJECTID": 10, "FIRE_YEAR": THIS_YEAR})))

    counts = fire.refresh_fire(con, _cfg(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert counts["active"] == 0  # bad lane skipped
    assert counts["history"] == 1  # others still ran
