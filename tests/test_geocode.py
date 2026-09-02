"""Geocoding tests - coordinate parsing (offline) and a mocked Nominatim lookup."""

from __future__ import annotations

import httpx
import pytest

from foray import geocode
from foray.geocode import notable_place_name, resolve, reverse, suggest


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nominatim's ~1/s throttle (see geocode._throttle) is a real budget in production, but
    would add ~1.1s per test here for no benefit - each test's MockTransport never talks to
    the real service, so there's nothing to protect."""
    monkeypatch.setattr(geocode._throttle, "min_interval", 0.0)


def test_parses_raw_coordinates() -> None:
    loc = resolve("43.3665, -124.2179")
    assert loc.lat == pytest.approx(43.3665)
    assert loc.lng == pytest.approx(-124.2179)


def test_parses_space_separated_coordinates() -> None:
    loc = resolve("47.6 -122.3")
    assert loc.lat == pytest.approx(47.6)
    assert loc.lng == pytest.approx(-122.3)


def test_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValueError):
        resolve("999, 999")


def test_geocodes_place_name_via_nominatim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "Coos Bay, OR"
        assert "foray-planner" in request.headers["user-agent"]
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "43.3665",
                    "lon": "-124.2179",
                    "display_name": "Coos Bay, Coos County, Oregon",
                }
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    loc = resolve("Coos Bay, OR", client=client)
    assert loc.lat == pytest.approx(43.3665)
    assert loc.lng == pytest.approx(-124.2179)
    assert "Coos Bay" in loc.name


def test_geocode_no_match_raises() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])))
    with pytest.raises(LookupError):
        resolve("asdfqwerzxcv nowhere", client=client)


def test_suggest_returns_multiple_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "Bend"
        assert request.url.params["limit"] == "5"
        return httpx.Response(
            200,
            json=[
                {"lat": "44.058", "lon": "-121.315", "display_name": "Bend, Deschutes County, Oregon"},
                {"lat": "45.612", "lon": "-121.199", "display_name": "Bend, Klickitat County, Washington"},
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hits = suggest("Bend", client=client)
    assert [hit.lat for hit in hits] == pytest.approx([44.058, 45.612])
    assert "Deschutes" in hits[0].name


def test_suggest_blank_query_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("blank query should not hit Nominatim")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert suggest("   ", client=client) == []


def test_suggest_passes_through_raw_coordinates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a raw coordinate pair should short-circuit")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hits = suggest("44.05, -121.31", client=client)
    assert len(hits) == 1
    assert hits[0].lat == pytest.approx(44.05)


def test_reverse_geocodes_coordinates_via_nominatim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["lat"] == "43.3665"
        assert request.url.params["lon"] == "-124.2179"
        assert "foray-planner" in request.headers["user-agent"]
        return httpx.Response(200, json={"display_name": "Coos Bay, Coos County, Oregon"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    loc = reverse(43.3665, -124.2179, client=client)
    assert loc.lat == pytest.approx(43.3665)
    assert loc.lng == pytest.approx(-124.2179)
    assert loc.name == "Coos Bay, Coos County, Oregon"


def test_reverse_geocode_no_match_raises() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    with pytest.raises(LookupError):
        reverse(0.0, 0.0, client=client)


def test_notable_place_name_prefers_forest_over_city() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["addressdetails"] == "1"
        return httpx.Response(
            200,
            json={
                "display_name": "Mt. Hood National Forest, Hood River County, Oregon",
                "address": {"forest": "Mt. Hood National Forest", "city": "Hood River", "county": "Hood River County"},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert notable_place_name(45.3, -121.7, client=client) == "Mt. Hood National Forest"


def test_notable_place_name_falls_back_to_city_when_no_notable_place() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"display_name": "Springfield", "address": {"city": "Springfield", "state": "Oregon"}}
            )
        )
    )
    assert notable_place_name(44.0, -123.0, client=client) == "Springfield"


def test_notable_place_name_returns_none_when_nothing_matches() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"display_name": "middle of nowhere", "address": {}})
        )
    )
    assert notable_place_name(0.0, 0.0, client=client) is None


def test_notable_place_name_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValueError):
        notable_place_name(999.0, 999.0)
