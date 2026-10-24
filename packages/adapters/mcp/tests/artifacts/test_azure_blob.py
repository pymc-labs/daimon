from __future__ import annotations

import base64
import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from azure.storage.blob import BlobServiceClient, ContentSettings, UserDelegationKey
from daimon.adapters.mcp.artifacts import build_artifact_store
from daimon.adapters.mcp.artifacts.azure_blob import AzureBlobArtifactStore
from daimon.core.config import ArtifactsSettings

NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)


class FakeBlobService:
    account_name = "privateaccount"

    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, dict[str, Any]]] = []
        self.delegations: list[tuple[dt.datetime, dt.datetime]] = []
        self.closed = False
        self.fail_upload = False
        self.container = ""
        self.key = ""
        self.url = "https://privateaccount.blob.core.windows.net/charts/tenant/t/chart.svg"

    def get_blob_client(self, *, container: str, blob: str) -> FakeBlobService:
        self.container, self.key = container, blob
        return self

    def upload_blob(self, content: bytes, **kwargs: Any) -> None:
        if self.fail_upload:
            raise RuntimeError("upload failed")
        self.uploads.append((content, kwargs))

    def get_user_delegation_key(self, start: dt.datetime, expiry: dt.datetime) -> UserDelegationKey:
        self.delegations.append((start, expiry))
        key = UserDelegationKey()
        key.signed_oid = "identity-id"
        key.signed_tid = "tenant-id"
        key.signed_start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_expiry = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_service = "b"
        key.signed_version = "2025-11-05"
        key.value = base64.b64encode(b"fake-delegation-key").decode()
        return key

    @contextmanager
    def open(self) -> Iterator[BlobServiceClient]:
        try:
            yield cast(BlobServiceClient, self)
        finally:
            self.closed = True


@pytest.mark.parametrize("content_type", ["image/png", "image/svg+xml"])
async def test_delegation_sas_is_read_only_per_blob_with_exact_expiry(content_type: str) -> None:
    client = FakeBlobService()
    store = AzureBlobArtifactStore(client.open, container="charts", clock=lambda: NOW)
    stored = await store.upload_and_presign(
        key="tenant/t/chart report.svg",
        content=b"chart",
        content_type=content_type,
        ttl_seconds=321,
    )
    assert client.container == "charts"
    assert client.key == stored.key == "tenant/t/chart report.svg"
    assert client.uploads[0][0] == b"chart"
    assert client.uploads[0][1]["overwrite"] is True
    headers: ContentSettings = client.uploads[0][1]["content_settings"]
    assert headers.content_type == content_type
    assert headers.content_disposition == "attachment; filename*=UTF-8''chart%20report.svg"
    assert client.delegations == [(NOW - dt.timedelta(minutes=5), NOW + dt.timedelta(seconds=321))]
    assert stored.expires_at == NOW + dt.timedelta(seconds=321)
    query = parse_qs(urlsplit(stored.url).query)
    assert query["sp"] == ["r"]
    assert query["sr"] == ["b"]
    assert query["spr"] == ["https"]
    assert query["skoid"] == ["identity-id"]
    assert query["sktid"] == ["tenant-id"]
    assert query["se"] == [stored.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")]
    assert query["rscd"] == [headers.content_disposition]
    assert query["rsct"] == [
        "application/octet-stream" if content_type == "image/svg+xml" else content_type
    ]
    assert client.closed


async def test_failed_upload_does_not_issue_a_capability_and_closes_client() -> None:
    client = FakeBlobService()
    client.fail_upload = True
    store = AzureBlobArtifactStore(client.open, container="charts")
    with pytest.raises(RuntimeError, match="upload failed"):
        await store.upload_and_presign(
            key="chart.png", content=b"png", content_type="image/png", ttl_seconds=600
        )
    assert not client.delegations
    assert client.closed


async def test_factory_selects_managed_identity_and_closes_credential() -> None:
    settings = ArtifactsSettings(
        backend="azure_blob",
        endpoint_url="https://example.blob.core.windows.net",
        bucket="charts",
        azure_client_id="charts-identity",
    )
    with (
        patch("daimon.adapters.mcp.artifacts.azure_blob.ManagedIdentityCredential") as credential,
        patch("daimon.adapters.mcp.artifacts.azure_blob.BlobServiceClient") as service,
    ):
        client = FakeBlobService()
        service.return_value.__enter__.return_value = client
        store = build_artifact_store(settings)
        assert isinstance(store, AzureBlobArtifactStore)
        credential.assert_not_called()  # Construction performs no authentication or network I/O.
        await store.upload_and_presign(
            key="chart.png", content=b"png", content_type="image/png", ttl_seconds=600
        )
        credential.assert_called_once_with(client_id="charts-identity")
        service.assert_called_once_with(
            "https://example.blob.core.windows.net", credential.return_value.__enter__.return_value
        )
        credential.return_value.__exit__.assert_called_once()
        service.return_value.__exit__.assert_called_once()
        service.from_connection_string.assert_not_called()
