"""foray.inat pagination tests - no network, get_taxa mocked (see test_ingest_region.py)."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
import requests.exceptions
from pyrate_limiter.exceptions import BucketFullException

from foray.inat import _RATE_LIMIT_ATTEMPTS, FUNGI_TAXON_ID, InatQuotaExceeded, _with_retries, iter_fungi_genera


def _page(ids: list[int]) -> dict:
    return {"results": [{"id": taxon_id, "name": f"Genus{taxon_id}", "rank": "genus"} for taxon_id in ids]}


def test_iter_fungi_genera_walks_id_above_pages() -> None:
    with patch("foray.inat.get_taxa") as mock_get_taxa, patch("foray.inat._PAGE_SIZE", 2):
        mock_get_taxa.side_effect = [
            _page([1, 2]),
            _page([3]),
        ]
        results = list(iter_fungi_genera())

    assert [r["id"] for r in results] == [1, 2, 3]
    assert mock_get_taxa.call_count == 2
    first_kwargs = mock_get_taxa.call_args_list[0].kwargs
    assert first_kwargs["taxon_id"] == FUNGI_TAXON_ID
    assert first_kwargs["rank"] == "genus"
    assert first_kwargs["id_above"] == 0
    second_kwargs = mock_get_taxa.call_args_list[1].kwargs
    assert second_kwargs["id_above"] == 2


def test_iter_fungi_genera_empty_result_stops_immediately() -> None:
    with patch("foray.inat.get_taxa") as mock_get_taxa:
        mock_get_taxa.return_value = {"results": []}
        results = list(iter_fungi_genera())

    assert results == []
    mock_get_taxa.assert_called_once()


@pytest.mark.parametrize("attempts", [0, -1])
def test_with_retries_rejects_non_positive_attempts(attempts: int) -> None:
    fn = Mock()
    with pytest.raises(ValueError, match="attempts must be >= 1"):
        _with_retries(fn, attempts=attempts)
    fn.assert_not_called()


def _http_error(status: int, retry_after: str | None = None) -> requests.exceptions.HTTPError:
    response = Mock()
    response.status_code = status
    response.headers = {"Retry-After": retry_after} if retry_after else {}
    return requests.exceptions.HTTPError(response=response)


def test_with_retries_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("foray.inat.time.sleep", sleeps.append)
    fn = Mock(side_effect=[_http_error(429), "ok"])

    result = _with_retries(fn, attempts=3, base_delay=1.0)

    assert result == "ok"
    assert fn.call_count == 2
    assert sleeps == [1.0]


def test_with_retries_honors_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("foray.inat.time.sleep", sleeps.append)
    fn = Mock(side_effect=[_http_error(429, retry_after="7"), "ok"])

    result = _with_retries(fn, attempts=3, base_delay=1.0)

    assert result == "ok"
    assert sleeps == [7.0]


def test_with_retries_429_gets_its_own_larger_attempt_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 always eventually clears, so it isn't bound by the generic ``attempts`` budget -
    it gets ``_RATE_LIMIT_ATTEMPTS`` tries regardless (this is what a sustained iNat throttle
    window needs; the old shared 5-attempt budget crashed a real multi-hour resync run twice)."""
    monkeypatch.setattr("foray.inat.time.sleep", Mock())
    fn = Mock(side_effect=_http_error(429))

    with pytest.raises(requests.exceptions.HTTPError):
        _with_retries(fn, attempts=2, base_delay=1.0)

    assert fn.call_count == _RATE_LIMIT_ATTEMPTS


def test_with_retries_caps_backoff_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("foray.inat.time.sleep", sleeps.append)
    fn = Mock(side_effect=[_http_error(429)] * 6 + ["ok"])

    result = _with_retries(fn, attempts=3, base_delay=1.0)

    assert result == "ok"
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]


def test_with_retries_reraises_5xx_after_exhausting_generic_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foray.inat.time.sleep", Mock())
    fn = Mock(side_effect=_http_error(503))

    with pytest.raises(requests.exceptions.HTTPError):
        _with_retries(fn, attempts=2, base_delay=1.0)

    assert fn.call_count == 2


def test_with_retries_does_not_retry_non_retryable_status() -> None:
    fn = Mock(side_effect=_http_error(404))

    with pytest.raises(requests.exceptions.HTTPError):
        _with_retries(fn, attempts=3, base_delay=1.0)

    fn.assert_called_once()


def _bucket_full(remaining_time: float) -> BucketFullException:
    return BucketFullException("api.inaturalist.org", Mock(), remaining_time)


def test_with_retries_waits_out_a_short_quota_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("foray.inat.time.sleep", sleeps.append)
    fn = Mock(side_effect=[_bucket_full(5.0), "ok"])

    result = _with_retries(fn, attempts=3, base_delay=1.0)

    assert result == "ok"
    assert sleeps == [5.0]


def test_with_retries_raises_quota_exceeded_for_a_long_wait() -> None:
    fn = Mock(side_effect=_bucket_full(3600.0))

    with pytest.raises(InatQuotaExceeded, match="60 min") as exc_info:
        _with_retries(fn, attempts=3, base_delay=1.0)

    assert exc_info.value.retry_after_seconds == 3600.0
    fn.assert_called_once()


def test_with_retries_raises_quota_exceeded_after_exhausting_short_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foray.inat.time.sleep", Mock())
    fn = Mock(side_effect=_bucket_full(5.0))

    with pytest.raises(InatQuotaExceeded):
        _with_retries(fn, attempts=3, base_delay=1.0)

    assert fn.call_count == _RATE_LIMIT_ATTEMPTS
