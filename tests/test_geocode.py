"""Geocoding tests - coordinate parsing (offline) and a mocked Nominatim lookup."""

from __future__ import annotations

import httpx
import pytest

from foray.geocode import resolve


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
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    )
    with pytest.raises(LookupError):
        resolve("asdfqwerzxcv nowhere", client=client)
