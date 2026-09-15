"""Public-land ingest tests - no network (mocked ArcGIS transport)."""

from __future__ import annotations

import json

import httpx
import psycopg
import pytest

from foray.cache import is_ingested, record_ingest
from foray.config import CoverageRegion, Settings, coverage_envelope
from foray.sources.land import (
    _LAND_SOURCES_VERSION,
    SOURCES,
    LandSource,
    _bounds,
    _envelope,
    _get,
    _padus_agency,
    _padus_id,
    _parse_feature,
    fetch_public_land,
    ingest_public_land_coverage,
)

HOME_LAT, HOME_LNG = 47.6, -122.3

BLM = LandSource(
    key="blm",
    agency="BLM",
    query_url="https://example.test/blm/query",
    where="ADMIN_AGENCY_CODE='BLM'",
    name_field="ADMIN_UNIT_NAME",
    fallback_name="BLM land",
)
USFS = LandSource(
    key="usfs",
    agency="USFS",
    query_url="https://example.test/usfs/query",
    where="1=1",
    name_field="FORESTNAME",
    fallback_name="National Forest",
)


def _polygon(lat: float, lng: float, size: float = 0.1) -> dict:
    """A small square GeoJSON polygon centered near (lat, lng)."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lng - size, lat - size],
                [lng + size, lat - size],
                [lng + size, lat + size],
                [lng - size, lat + size],
                [lng - size, lat - size],
            ]
        ],
    }


def test_get_is_case_insensitive() -> None:
    # ArcGIS geojson lowercases requested field names, so lookups must not be case-sensitive.
    props = {"forestname": "Gifford Pinchot National Forest", "OBJECTID": 5}
    assert _get(props, "FORESTNAME") == "Gifford Pinchot National Forest"
    assert _get(props, "OBJECTID") == 5
    assert _get(props, "missing") is None


def test_bounds_walks_nested_multipolygon_coords() -> None:
    multipolygon = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[-122.5, 47.5], [-122.0, 47.5], [-122.0, 48.0], [-122.5, 48.0], [-122.5, 47.5]]],
            [[[-123.0, 47.0], [-122.8, 47.0], [-122.8, 47.2], [-123.0, 47.2], [-123.0, 47.0]]],
        ],
    }
    assert _bounds(multipolygon["coordinates"]) == (-123.0, 47.0, -122.0, 48.0)
    assert _bounds([]) is None


def test_envelope_encloses_the_home_disk() -> None:
    xmin, ymin, xmax, ymax = _envelope(HOME_LAT, HOME_LNG, radius_km=50.0)
    assert ymin < HOME_LAT < ymax
    assert xmin < HOME_LNG < xmax
    # Longitude degrees are shorter than latitude at this latitude → wider lng span.
    assert (xmax - xmin) > (ymax - ymin)


def test_parse_feature_builds_row_and_falls_back_on_missing_name() -> None:
    row = _parse_feature(
        BLM,
        {"properties": {"OBJECTID": 42}, "geometry": _polygon(47.7, -122.1)},
    )
    assert row is not None
    assert row[0] == "blm:42"
    assert row[1] == "BLM"
    assert row[2] == "BLM land"  # name absent → fallback
    assert row[3] == "blm"
    assert row[4] == "https://example.test/blm"  # /query stripped
    # (id, agency, unit, source, url, geojson)
    assert json.loads(row[5])["type"] == "Polygon"


def test_parse_feature_skips_missing_geometry_or_id() -> None:
    assert _parse_feature(BLM, {"properties": {"OBJECTID": 1}, "geometry": None}) is None
    assert _parse_feature(BLM, {"properties": {}, "geometry": _polygon(47.6, -122.3)}) is None


def test_padus_source_is_registered_alongside_blm_usfs_tribal() -> None:
    keys = {source.key for source in SOURCES}
    assert keys == {"blm", "usfs", "tribal", "padus"}


def test_padus_agency_resolves_coded_value_and_falls_back_to_the_raw_code() -> None:
    assert _padus_agency({"Mang_Name": "SPR"}) == "State Park and Recreation"
    assert _padus_agency({"Mang_Name": "NPS"}) == "National Park Service"
    assert _padus_agency({"Mang_Name": "XYZ"}) == "XYZ"  # unknown code -> passthrough
    assert _padus_agency({}) == "PAD-US"  # missing code -> generic label


def test_padus_id_is_stable_and_independent_of_objectid() -> None:
    # PAD-US's OBJECTID isn't stable across releases (issue #335) - the id must be derived
    # from content instead, and the same content must always hash to the same id.
    props = {"Mang_Name": "SPR", "Unit_Nm": "Prairie Creek Redwoods State Park", "OBJECTID": 189457}
    bounds = (-124.05, 41.30, -123.95, 41.40)
    same_props_new_objectid = {**props, "OBJECTID": 999999}
    assert _padus_id(props, bounds) == _padus_id(same_props_new_objectid, bounds)
    assert _padus_id({"Mang_Name": "NPS", "Unit_Nm": "Redwood National Park"}, bounds) != _padus_id(props, bounds)


def test_padus_id_disambiguates_identically_named_features_by_location() -> None:
    # A Copilot review catch (PR #352): PAD-US has many distinct polygons sharing the generic
    # Unit_Nm "Unnamed site - Other State" - a unit-name-only hash collapsed them onto one id,
    # so `_fetch_public_land_envelope`'s id-keyed dedup silently dropped all but the last.
    props = {"Mang_Name": "OTHS", "Unit_Nm": "Unnamed site - Other State"}
    here = (-124.05, 41.30, -123.95, 41.40)
    elsewhere = (-120.05, 38.30, -119.95, 38.40)
    assert _padus_id(props, here) != _padus_id(props, elsewhere)


def test_padus_out_fields_include_mang_name_alongside_id_and_name() -> None:
    padus = next(source for source in SOURCES if source.key == "padus")
    fields = set(padus.out_fields.split(","))
    assert {"OBJECTID", "Unit_Nm", "Mang_Name"} <= fields


def test_parse_feature_uses_padus_make_id_and_agency_of() -> None:
    padus = next(source for source in SOURCES if source.key == "padus")
    props = {"OBJECTID": 189457, "Mang_Name": "SPR", "Unit_Nm": "Prairie Creek Redwoods State Park"}
    geometry = _polygon(41.35, -124.0)
    row = _parse_feature(padus, {"properties": props, "geometry": geometry})
    bounds = _bounds(geometry["coordinates"])
    assert row is not None
    assert bounds is not None
    assert row[0] == f"padus:{_padus_id(props, bounds)}"
    assert row[1] == "State Park and Recreation"
    assert row[2] == "Prairie Creek Redwoods State Park"
    assert row[3] == "padus"


def test_fetch_public_land_does_not_collide_padus_features_sharing_a_generic_name() -> None:
    # End-to-end regression for the same Copilot catch, through the dedup path in
    # `_fetch_public_land_envelope` rather than `_padus_id` in isolation.
    padus = next(source for source in SOURCES if source.key == "padus")

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "properties": {"OBJECTID": 1, "Mang_Name": "OTHS", "Unit_Nm": "Unnamed site - Other State"},
                        "geometry": _polygon(41.35, -124.0),
                    },
                    {
                        "properties": {"OBJECTID": 2, "Mang_Name": "OTHS", "Unit_Nm": "Unnamed site - Other State"},
                        "geometry": _polygon(38.35, -120.0),
                    },
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_public_land(lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, client=client, sources=(padus,))
    assert len(rows) == 2  # both survive the id-keyed dedup instead of one overwriting the other


def test_fetch_public_land_dedupes_and_skips_a_failing_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "usfs" in str(request.url):
            return httpx.Response(500)  # this source is down → must be skipped, not fatal
        # Same BLM feature returned for every page-0 request; page-1 is empty → terminates.
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {"properties": {"OBJECTID": 7}, "geometry": _polygon(47.65, -122.35)},
                    {"properties": {"OBJECTID": 7}, "geometry": _polygon(47.65, -122.35)},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_public_land(
        lat=HOME_LAT,
        lng=HOME_LNG,
        radius_km=50.0,
        client=client,
        sources=(BLM, USFS),
    )
    assert [row[0] for row in rows] == ["blm:7"]  # deduped; USFS 500 skipped


def test_fetch_public_land_skips_a_source_returning_malformed_payload() -> None:
    # A 200 that isn't well-formed GeoJSON (decode error) must be skipped like a transport
    # error - ownership ingest is best-effort and must not abort the refresh.
    def handler(request: httpx.Request) -> httpx.Response:
        if "usfs" in str(request.url):
            return httpx.Response(200, text="<html>maintenance</html>")  # not JSON
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"OBJECTID": 3}, "geometry": _polygon(47.6, -122.3)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_public_land(lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, client=client, sources=(BLM, USFS))
    assert [row[0] for row in rows] == ["blm:3"]  # BLM ingested; malformed USFS skipped


def test_fetch_public_land_pages_until_transfer_limit_clears() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset == 0:
            features = [
                {
                    "properties": {"OBJECTID": index},
                    "geometry": _polygon(47.6 + index * 0.001, -122.3),
                }
                for index in range(1000)  # a full page → exceededTransferLimit
            ]
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": features,
                    "exceededTransferLimit": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"OBJECTID": 1000}, "geometry": _polygon(47.7, -122.3)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_public_land(lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, client=client, sources=(BLM,))
    ids = {row[0] for row in rows}
    assert "blm:1000" in ids  # the second page was fetched
    assert len(ids) == 1001


def test_coverage_envelope_unions_region_bboxes() -> None:
    regions = [
        CoverageRegion(name="A", place_id=1, bbox=(-124.0, 45.0, -120.0, 49.0)),
        CoverageRegion(name="B", place_id=2, bbox=(-121.0, 41.0, -116.0, 46.0)),
        CoverageRegion(name="No bbox", place_id=3),
    ]
    assert coverage_envelope(regions) == (-124.0, 41.0, -116.0, 49.0)


def test_coverage_envelope_raises_when_nothing_has_a_bbox() -> None:
    with pytest.raises(ValueError, match="bbox"):
        coverage_envelope([CoverageRegion(name="No bbox", place_id=1)])


def test_ingest_public_land_coverage_upserts_and_records_ingest(con: psycopg.Connection) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "usfs" in str(request.url):
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"OBJECTID": 9}, "geometry": _polygon(47.6, -122.3)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = Settings(coverage=[CoverageRegion(name="Washington", place_id=46, bbox=(-124.8, 45.5, -116.9, 49.0))])
    count = ingest_public_land_coverage(cfg, con, client=client, sources=(BLM, USFS))
    assert count == 1
    assert is_ingested(con, f"land:coverage:v{_LAND_SOURCES_VERSION}")
    # Second call skips before ever opening a client - if it didn't, this would try (and fail)
    # to reach the real ArcGIS services, since no client is passed here.
    assert ingest_public_land_coverage(cfg, con, sources=(BLM, USFS)) == 0


def test_ingest_public_land_coverage_reruns_past_an_unversioned_marker(con: psycopg.Connection) -> None:
    # A Copilot review catch (PR #352): adding PAD-US to SOURCES without versioning the marker
    # would leave a deployment that already recorded the pre-PAD-US "land:coverage" key stuck
    # skipping forever - PAD-US would never be fetched. The versioned key must not match it.
    record_ingest(con, "land:coverage", 1)  # simulate a pre-#335 deployment's marker

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"OBJECTID": 1}, "geometry": _polygon(47.6, -122.3)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = Settings(coverage=[CoverageRegion(name="Washington", place_id=46, bbox=(-124.8, 45.5, -116.9, 49.0))])
    assert ingest_public_land_coverage(cfg, con, client=client, sources=(BLM,)) == 1
    assert is_ingested(con, f"land:coverage:v{_LAND_SOURCES_VERSION}")
