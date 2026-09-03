"""Wildfire ingest (issue #227): feature parsing, the active lane's replace semantics, the
MTBS severity join, and refresh_fire end to end against mocked ArcGIS services."""

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
    mtbs = _geojson(
        _feature(
            {"Irwin_ID": "{H1}", "Event_ID": "MT1", "Acres_Low": 200.0, "Acres_Moderate": 50.0, "Acres_High": 10.0}
        )
    )
    client = httpx.Client(
        transport=_transport(
            {
                "Perimeters_Current": active,
                "Incident_Locations_Current": points,
                "InterAgencyFirePerimeterHistory": history,
                "MTBS": mtbs,
            }
        )
    )
    counts = fire.refresh_fire(con, _cfg(), client=client)
    assert counts == {"active": 1, "points": 1, "history": 1, "severity": 1}
    rows = {r[0]: r for r in con.execute("SELECT id, status, dominant_severity FROM fire_perimeters")}
    assert rows["wfigs_active:1"][1] == "active"
    assert rows["perimeter_history:10"][2] == "low"  # MTBS severity joined by irwin id


def test_refresh_fire_skips_one_bad_lane(con: psycopg.Connection) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "Perimeters_Current" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=_geojson(_feature({"OBJECTID": 10, "FIRE_YEAR": THIS_YEAR})))

    counts = fire.refresh_fire(con, _cfg(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert counts["active"] == 0  # bad lane skipped
    assert counts["history"] == 1  # others still ran
