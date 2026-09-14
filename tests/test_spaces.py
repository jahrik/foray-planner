from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from botocore.exceptions import ClientError

from foray import spaces
from foray.config import Spaces

_UNCONFIGURED = Spaces()
_CONFIGURED = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk", region="nyc3")


@pytest.fixture(autouse=True)
def _reset_latest_snapshot_cache() -> None:
    # `latest_snapshot_date` caches in-process, keyed on (bucket, source) - without this reset,
    # tests reusing `_CONFIGURED`/"padus" across different fake clients would read a previous
    # test's cached result instead of exercising the real listing.
    spaces.invalidate_latest_snapshot_cache()


def test_spaces_configured_property() -> None:
    assert _UNCONFIGURED.configured is False
    assert _CONFIGURED.configured is True


def test_client_raises_when_unconfigured() -> None:
    with pytest.raises(spaces.SpacesNotConfigured):
        spaces.client(_UNCONFIGURED)


def test_base_url_defaults_to_plain_space_endpoint() -> None:
    assert _CONFIGURED.base_url == "https://foray-bulk.nyc3.digitaloceanspaces.com"


def test_base_url_prefers_public_url_when_set() -> None:
    cfg = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk", public_url="https://cdn.example.com")
    assert cfg.base_url == "https://cdn.example.com"


def test_snapshot_prefix() -> None:
    assert spaces.snapshot_prefix("padus", date(2026, 1, 8)) == "bulk/padus/2026-01-08/"


def test_snapshot_run_prefix_is_unique_per_run() -> None:
    prefix_a = spaces.snapshot_run_prefix("padus", date(2026, 1, 8), "run-a")
    prefix_b = spaces.snapshot_run_prefix("padus", date(2026, 1, 8), "run-b")
    assert prefix_a == "bulk/padus/2026-01-08/runs/run-a/"
    assert prefix_a != prefix_b


def test_new_run_id_is_unique() -> None:
    assert spaces.new_run_id() != spaces.new_run_id()


class _FakeS3Client:
    """A single object store keyed by full Key, plus the date-level CommonPrefixes a real
    ``Delimiter="/"`` listing of ``bulk/{source}/`` would return - enough to exercise
    `list_snapshot_dates`/`snapshot_run_id` without a real Space."""

    def __init__(self, date_prefixes: list[str], objects: dict[str, bytes] | None = None) -> None:
        self._date_prefixes = date_prefixes
        self._objects: dict[str, bytes] = dict(objects or {})
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.list_calls = 0

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)
        self._objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        self.get_calls.append(key)
        if key not in self._objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"Body": _Body(self._objects[key])}

    def get_paginator(self, name: str) -> Any:
        assert name == "list_objects_v2"
        prefixes = self._date_prefixes
        self.list_calls += 1

        class _Paginator:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                return [{"CommonPrefixes": [{"Prefix": prefix} for prefix in prefixes]}]

        return _Paginator()


def test_put_object_defaults_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(date_prefixes=[])
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    url = spaces.put_object(_CONFIGURED, "bulk/padus/2026-01-08/data.gpkg", b"bytes", "application/octet-stream")

    assert url == "https://foray-bulk.nyc3.digitaloceanspaces.com/bulk/padus/2026-01-08/data.gpkg"
    assert fake.put_calls == [
        {
            "Bucket": "foray-bulk",
            "Key": "bulk/padus/2026-01-08/data.gpkg",
            "Body": b"bytes",
            "ContentType": "application/octet-stream",
        }
    ]


def test_put_object_public_sets_public_read_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(date_prefixes=[])
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    url = spaces.put_object(_CONFIGURED, "satellite/425_-1099/image.jpg", b"bytes", "image/jpeg", public=True)

    assert url == "https://foray-bulk.nyc3.digitaloceanspaces.com/satellite/425_-1099/image.jpg"
    assert fake.put_calls == [
        {
            "Bucket": "foray-bulk",
            "Key": "satellite/425_-1099/image.jpg",
            "Body": b"bytes",
            "ContentType": "image/jpeg",
            "ACL": "public-read",
        }
    ]


def test_object_bytes_reads_from_the_space(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(date_prefixes=[], objects={"bulk/padus/2026-01-08/data.gpkg": b"object-bytes"})
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    assert spaces.object_bytes(_CONFIGURED, "bulk/padus/2026-01-08/data.gpkg") == b"object-bytes"


def test_download_file_streams_via_boto3_download_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: list[tuple[str, str, str]] = []

    class _FakeDownloadClient:
        def download_file(self, bucket: str, key: str, dest_path: str) -> None:
            calls.append((bucket, key, dest_path))

    monkeypatch.setattr(spaces, "client", lambda cfg: _FakeDownloadClient())
    dest = str(tmp_path / "data.gpkg")

    spaces.download_file(_CONFIGURED, "bulk/padus/2026-01-08/runs/run-1/data.gpkg", dest)

    assert calls == [("foray-bulk", "bulk/padus/2026-01-08/runs/run-1/data.gpkg", dest)]


def test_publish_snapshot_writes_a_manifest_naming_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(date_prefixes=[])
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    spaces.publish_snapshot(_CONFIGURED, "padus", date(2026, 1, 8), "run-1")

    assert fake.put_calls == [
        {
            "Bucket": "foray-bulk",
            "Key": "bulk/padus/2026-01-08/_manifest.json",
            "Body": json.dumps({"run_id": "run-1"}).encode(),
            "ContentType": "application/json",
        }
    ]


def test_snapshot_run_id_returns_none_when_never_published(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(date_prefixes=[])
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    assert spaces.snapshot_run_id(_CONFIGURED, "padus", date(2026, 1, 8)) is None


def test_snapshot_run_id_returns_the_published_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(date_prefixes=[])
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)
    spaces.publish_snapshot(_CONFIGURED, "padus", date(2026, 1, 8), "run-1")

    assert spaces.snapshot_run_id(_CONFIGURED, "padus", date(2026, 1, 8)) == "run-1"


def test_republishing_a_date_overwrites_the_manifest_without_touching_the_old_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The core race this design closes: a re-stage of the same date uploads under a brand new
    # run_id and only then flips the manifest - the old run's objects are simply orphaned, never
    # partially mixed into a read.
    fake = _FakeS3Client(date_prefixes=[])
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)
    spaces.publish_snapshot(_CONFIGURED, "padus", date(2026, 1, 8), "run-1")

    spaces.publish_snapshot(_CONFIGURED, "padus", date(2026, 1, 8), "run-2")

    assert spaces.snapshot_run_id(_CONFIGURED, "padus", date(2026, 1, 8)) == "run-2"


def test_list_snapshot_dates_only_counts_published_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(
        date_prefixes=[
            "bulk/padus/2026-01-01/",
            "bulk/padus/2026-01-08/",
            "bulk/padus/2026-01-15/",
            "bulk/padus/not-a-date/",
        ],
        objects={
            "bulk/padus/2026-01-01/_manifest.json": json.dumps({"run_id": "run-1"}).encode(),
            "bulk/padus/2026-01-08/_manifest.json": json.dumps({"run_id": "run-2"}).encode(),
            # 2026-01-15 has data uploaded but no manifest published yet - still in progress.
        },
    )
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    assert spaces.list_snapshot_dates(_CONFIGURED, "padus") == [date(2026, 1, 1), date(2026, 1, 8)]


def test_latest_snapshot_date_returns_none_when_never_published(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client(date_prefixes=[])
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    assert spaces.latest_snapshot_date(_CONFIGURED, "padus") is None


def test_latest_snapshot_date_skips_an_in_progress_newer_date(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-01-15 has a date prefix (data uploading) but no manifest yet - the newest *published*
    # date is still 2026-01-08.
    fake = _FakeS3Client(
        date_prefixes=["bulk/padus/2026-01-01/", "bulk/padus/2026-01-08/", "bulk/padus/2026-01-15/"],
        objects={
            "bulk/padus/2026-01-01/_manifest.json": json.dumps({"run_id": "run-1"}).encode(),
            "bulk/padus/2026-01-08/_manifest.json": json.dumps({"run_id": "run-2"}).encode(),
        },
    )
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    assert spaces.latest_snapshot_date(_CONFIGURED, "padus") == date(2026, 1, 8)


def test_latest_snapshot_date_stops_at_the_first_published_date_instead_of_checking_every_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The performance property a frequently-polled health check needs (issue #357 Copilot
    # review): with the newest date already published, this does exactly one manifest GET, not
    # one per historical date the source has ever staged.
    fake = _FakeS3Client(
        date_prefixes=["bulk/padus/2026-01-01/", "bulk/padus/2026-01-08/", "bulk/padus/2026-01-15/"],
        objects={
            "bulk/padus/2026-01-01/_manifest.json": json.dumps({"run_id": "run-1"}).encode(),
            "bulk/padus/2026-01-08/_manifest.json": json.dumps({"run_id": "run-2"}).encode(),
            "bulk/padus/2026-01-15/_manifest.json": json.dumps({"run_id": "run-3"}).encode(),
        },
    )
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    assert spaces.latest_snapshot_date(_CONFIGURED, "padus") == date(2026, 1, 15)
    assert fake.get_calls == ["bulk/padus/2026-01-15/_manifest.json"]


def test_latest_snapshot_date_is_cached_across_calls_within_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    # Copilot review catch (PR #358): the newest-first short-circuit alone still repeats the
    # ListObjectsV2 listing itself on every call - a short-TTL process cache is what actually
    # bounds a frequently-polled health check's S3 work.
    fake = _FakeS3Client(
        date_prefixes=["bulk/padus/2026-01-08/"],
        objects={"bulk/padus/2026-01-08/_manifest.json": json.dumps({"run_id": "run-1"}).encode()},
    )
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    first = spaces.latest_snapshot_date(_CONFIGURED, "padus")
    second = spaces.latest_snapshot_date(_CONFIGURED, "padus")

    assert first == second == date(2026, 1, 8)
    assert fake.list_calls == 1


def test_latest_snapshot_date_cache_is_scoped_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # Proves the cache key includes the source, not just a single global slot - querying "padus"
    # right after "ridb" must not return ridb's cached date.
    fake_ridb = _FakeS3Client(
        date_prefixes=["bulk/ridb/2026-01-08/"],
        objects={"bulk/ridb/2026-01-08/_manifest.json": json.dumps({"run_id": "run-1"}).encode()},
    )
    monkeypatch.setattr(spaces, "client", lambda cfg: fake_ridb)
    assert spaces.latest_snapshot_date(_CONFIGURED, "ridb") == date(2026, 1, 8)

    fake_padus = _FakeS3Client(
        date_prefixes=["bulk/padus/2026-02-01/"],
        objects={"bulk/padus/2026-02-01/_manifest.json": json.dumps({"run_id": "run-2"}).encode()},
    )
    monkeypatch.setattr(spaces, "client", lambda cfg: fake_padus)
    assert spaces.latest_snapshot_date(_CONFIGURED, "padus") == date(2026, 2, 1)
