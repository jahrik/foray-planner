"""Overpass client tests - endpoint failover, retry, config (no network: mocked transport)."""

from __future__ import annotations

import httpx
import pytest

from foray.sources import overpass

ENDPOINTS = (
    "https://primary.example/api/interpreter",
    "https://mirror-a.example/api/interpreter",
    "https://mirror-b.example/api/interpreter",
)


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the shared throttle and the retry backoff so the suite doesn't actually sleep."""
    monkeypatch.setattr(overpass._throttle, "min_interval", 0.0)
    monkeypatch.setattr("foray.sources.overpass.time.sleep", lambda _seconds: None)


def _client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_post_returns_json_from_the_first_healthy_endpoint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, json={"elements": [1]})

    result = overpass.post(_client(handler), "q", endpoints=ENDPOINTS)
    assert result == {"elements": [1]}
    assert seen == ["primary.example"]  # never touched the mirrors


def test_post_fails_over_to_the_next_endpoint_on_a_connection_error() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "primary.example":
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(200, json={"ok": True})

    result = overpass.post(_client(handler), "q", endpoints=ENDPOINTS)
    assert result == {"ok": True}
    assert seen == ["primary.example", "mirror-a.example"]


def test_post_fails_over_after_exhausting_retries_on_a_server_error() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "primary.example":
            return httpx.Response(504)
        return httpx.Response(200, json={"ok": True})

    result = overpass.post(_client(handler), "q", attempts=2, endpoints=ENDPOINTS)
    assert result == {"ok": True}
    assert seen == ["primary.example", "primary.example", "mirror-a.example"]  # 2 tries then next host


def test_post_retries_a_throttled_endpoint_before_moving_on() -> None:
    calls = {"primary": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.example":
            calls["primary"] += 1
            if calls["primary"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"recovered": True})
        raise AssertionError("should not have failed over - primary recovered on retry")

    result = overpass.post(_client(handler), "q", endpoints=ENDPOINTS)
    assert result == {"recovered": True}
    assert calls["primary"] == 2


def test_post_raises_the_last_error_when_every_endpoint_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        overpass.post(_client(handler), "q", attempts=1, endpoints=ENDPOINTS)


def test_configured_endpoints_defaults_then_honours_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORAY_OVERPASS_URLS", raising=False)
    assert overpass._configured_endpoints() == overpass._DEFAULT_ENDPOINTS

    monkeypatch.setenv("FORAY_OVERPASS_URLS", "https://one.example/api , https://two.example/api ")
    assert overpass._configured_endpoints() == ("https://one.example/api", "https://two.example/api")
