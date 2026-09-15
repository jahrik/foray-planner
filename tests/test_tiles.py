"""`/api/tiles/*` - the martin vector-tile proxy (issue #336). No real martin instance: the
module-level `_martin_client` is monkeypatched with an `httpx.MockTransport` per test, same
"hermetic, only Postgres is real network" boundary as test_api.py."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from foray.api import create_app
from foray.config import Settings

MARTIN_URL = "http://martin:3000"


@pytest.fixture
def cfg(con: psycopg.Connection) -> Settings:
    return Settings(basemap_url="", terrain_url="", martin_url=MARTIN_URL)


@pytest.fixture
def client(cfg: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(cfg)) as client:
        yield client


def _mock_martin(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport | None = None) -> None:
    import foray.api.routes.tiles as tiles

    monkeypatch.setattr(tiles, "_martin_client", httpx.AsyncClient(transport=handler))


def test_trails_tile_proxies_martin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{MARTIN_URL}/trails/8/40/96"
        return httpx.Response(200, content=b"\x1a\x02fake-mvt")

    _mock_martin(monkeypatch, httpx.MockTransport(handler))
    response = client.get("/api/tiles/trails/8/40/96.pbf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert response.content == b"\x1a\x02fake-mvt"


def test_land_and_fire_tiles_hit_their_own_martin_table(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"\x1a\x02fake-mvt")

    _mock_martin(monkeypatch, httpx.MockTransport(handler))
    assert client.get("/api/tiles/land/6/10/22.pbf").status_code == 200
    assert client.get("/api/tiles/fire/6/10/22.pbf").status_code == 200
    assert seen == [f"{MARTIN_URL}/land/6/10/22", f"{MARTIN_URL}/fire/6/10/22"]


@pytest.mark.parametrize(
    "url",
    ["/api/tiles/trails/-1/0/0.pbf", "/api/tiles/land/8/999/0.pbf", "/api/tiles/fire/8/0/999.pbf"],
)
def test_tile_out_of_range_coordinates_rejected(client: TestClient, url: str) -> None:
    # No martin call should even be attempted for a bogus z/x/y - no mock transport installed,
    # so this would error loudly if the range check were skipped.
    response = client.get(url)
    assert response.status_code == 400


def test_tile_upstream_failure_is_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("martin unreachable", request=request)

    _mock_martin(monkeypatch, httpx.MockTransport(handler))
    response = client.get("/api/tiles/trails/8/40/96.pbf")
    assert response.status_code == 502


def test_tiles_404_when_martin_disabled(con: psycopg.Connection) -> None:
    disabled_cfg = Settings(basemap_url="", terrain_url="", martin_url="")
    with TestClient(create_app(disabled_cfg)) as client:
        assert client.get("/api/tiles/trails/8/40/96.pbf").status_code == 404
        assert client.get("/api/tiles/land/8/40/96.pbf").status_code == 404
        assert client.get("/api/tiles/fire/8/40/96.pbf").status_code == 404
