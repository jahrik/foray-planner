"""``foray.alerting`` - the healthchecks.io ping URL construction and the never-raises
contract (issue #332 Copilot review): a rejected/unreachable healthchecks or ntfy endpoint
must be logged, never raised, and the ping must only ever hit `/start` or `/fail` on a job's
own configured URL - never an arbitrary job-name path segment."""

from __future__ import annotations

import httpx
import pytest

from foray import alerting
from foray.config import Observability, Settings


def _cfg_with_healthchecks(urls: dict[str, str]) -> Settings:
    return Settings(observability=Observability(healthchecks_urls=urls))


def _cfg_with_ntfy(url: str) -> Settings:
    return Settings(observability=Observability(ntfy_url=url))


def test_healthcheck_ping_is_a_noop_when_the_job_has_no_configured_url(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fake_get(*args: object, **kwargs: object) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", _fake_get)
    cfg = _cfg_with_healthchecks({"ingest": "https://hc-ping.com/abc"})

    alerting.healthcheck_ping(cfg, "unconfigured-job", "")

    assert called is False


def test_healthcheck_ping_bare_success_hits_the_jobs_own_url_unmodified(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_urls: list[str] = []

    def _fake_get(url: str, **kwargs: object) -> httpx.Response:
        requested_urls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)
    cfg = _cfg_with_healthchecks({"ingest": "https://hc-ping.com/abc-123"})

    alerting.healthcheck_ping(cfg, "ingest", "")

    assert requested_urls == ["https://hc-ping.com/abc-123"]


@pytest.mark.parametrize("event", ["start", "fail"])
def test_healthcheck_ping_appends_only_the_two_healthchecks_io_event_suffixes(
    monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    requested_urls: list[str] = []

    def _fake_get(url: str, **kwargs: object) -> httpx.Response:
        requested_urls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)
    cfg = _cfg_with_healthchecks({"ingest": "https://hc-ping.com/abc-123"})

    alerting.healthcheck_ping(cfg, "ingest", event)

    assert requested_urls == [f"https://hc-ping.com/abc-123/{event}"]


def test_healthcheck_ping_logs_but_does_not_raise_on_a_4xx_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(404, request=httpx.Request("GET", url)))
    cfg = _cfg_with_healthchecks({"ingest": "https://hc-ping.com/abc-123"})

    alerting.healthcheck_ping(cfg, "ingest", "")  # must not raise


def test_alert_logs_but_does_not_raise_on_a_5xx_ntfy_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(500, request=httpx.Request("POST", url)))
    cfg = _cfg_with_ntfy("https://ntfy.sh/some-topic")

    alerting.alert(cfg, "error", "test message")  # must not raise
