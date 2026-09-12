from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from foray import spaces
from foray.config import Spaces

_UNCONFIGURED = Spaces()
_CONFIGURED = Spaces(access_key_id="k", secret_access_key="s", bucket="foray-bulk", region="nyc3")


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


class _FakeS3Client:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        class _Body:
            def read(self) -> bytes:
                return b"object-bytes"

        return {"Body": _Body()}

    def get_paginator(self, name: str) -> Any:
        assert name == "list_objects_v2"

        class _Paginator:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                return [
                    {
                        "Contents": [
                            {"Key": "bulk/padus/2026-01-01/data.gpkg"},
                            {"Key": "bulk/padus/2026-01-01/_SUCCESS"},
                            {"Key": "bulk/padus/2026-01-08/data.gpkg"},
                            {"Key": "bulk/padus/2026-01-08/_SUCCESS"},
                            {"Key": "bulk/padus/2026-01-15/data.gpkg"},  # no marker - still uploading
                            {"Key": "bulk/padus/not-a-date/_SUCCESS"},
                        ]
                    }
                ]

        return _Paginator()


def test_put_object_defaults_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client()
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
    fake = _FakeS3Client()
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
    monkeypatch.setattr(spaces, "client", lambda cfg: _FakeS3Client())

    assert spaces.object_bytes(_CONFIGURED, "bulk/padus/2026-01-08/data.gpkg") == b"object-bytes"


def test_mark_snapshot_complete_writes_the_marker_object(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeS3Client()
    monkeypatch.setattr(spaces, "client", lambda cfg: fake)

    spaces.mark_snapshot_complete(_CONFIGURED, "padus", date(2026, 1, 8))

    assert fake.put_calls == [{"Bucket": "foray-bulk", "Key": "bulk/padus/2026-01-08/_SUCCESS", "Body": b""}]


def test_list_snapshot_dates_only_counts_marked_complete_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spaces, "client", lambda cfg: _FakeS3Client())

    # 2026-01-15 has data but no _SUCCESS marker yet (still uploading) - excluded.
    assert spaces.list_snapshot_dates(_CONFIGURED, "padus") == [date(2026, 1, 1), date(2026, 1, 8)]
