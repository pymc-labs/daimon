"""Private Azure Blob uploads and read-only, per-object SAS capabilities."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from urllib.parse import quote

from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)
from daimon.core.artifacts import StoredArtifact
from daimon.core.config import ArtifactsSettings


class AzureBlobArtifactStore:
    """Use delegation SAS on Azure; shared-key SAS only for local Azurite."""

    def __init__(
        self,
        client_factory: Callable[[], AbstractContextManager[BlobServiceClient]],
        *,
        container: str,
        local_account_key: str | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._container = container
        self._local_account_key = local_account_key
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def upload_and_presign(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str,
        ttl_seconds: int,
    ) -> StoredArtifact:
        """Upload privately and sign a GET with the exact returned expiry."""
        if not 0 < ttl_seconds <= 86_400:
            raise ValueError("artifact URL TTL must be between 1 and 86400 seconds")

        def _store_and_sign() -> StoredArtifact:
            with self._client_factory() as client:
                blob = client.get_blob_client(container=self._container, blob=key)
                basename = key.rsplit("/", maxsplit=1)[-1]
                disposition = f"attachment; filename*=UTF-8''{quote(basename, safe='')}"
                response_type = (
                    "application/octet-stream" if content_type == "image/svg+xml" else content_type
                )
                blob.upload_blob(
                    content,
                    overwrite=True,
                    content_settings=ContentSettings(
                        content_type=content_type, content_disposition=disposition
                    ),
                )
                now = self._clock().replace(microsecond=0)
                expires_at = now + dt.timedelta(seconds=ttl_seconds)
                # Backdate the start for clock skew, without extending the URL's expiry.
                starts_at = now - dt.timedelta(minutes=5)
                delegation_key = (
                    client.get_user_delegation_key(starts_at, expires_at)
                    if self._local_account_key is None
                    else None
                )
                if client.account_name is None:
                    raise ValueError("Azure Blob account name is required")
                sas = generate_blob_sas(
                    account_name=client.account_name,
                    container_name=self._container,
                    blob_name=key,
                    account_key=self._local_account_key,
                    user_delegation_key=delegation_key,
                    permission=BlobSasPermissions(read=True),
                    start=starts_at,
                    expiry=expires_at,
                    protocol="https" if self._local_account_key is None else "http,https",
                    content_disposition=disposition,
                    content_type=response_type,
                )
                return StoredArtifact(key=key, url=f"{blob.url}?{sas}", expires_at=expires_at)

        # SDK clients and credentials are closed in the worker, including on errors.
        return await asyncio.to_thread(_store_and_sign)


def build_azure_blob_store(settings: ArtifactsSettings) -> AzureBlobArtifactStore:
    """Build without I/O; AZURE_CLIENT_ID selects the user-assigned identity."""
    connection_string = (
        settings.azure_connection_string.get_secret_value()
        if settings.azure_connection_string is not None
        else None
    )
    account_key: str | None = None
    if connection_string is not None:
        parts = {
            key.upper(): value
            for part in connection_string.split(";")
            if "=" in part
            for key, value in [part.split("=", 1)]
        }
        account_key = parts.get("ACCOUNTKEY")
        if not account_key:
            raise ValueError("Azurite connection string requires AccountKey")

    @contextmanager
    def client_factory() -> Iterator[BlobServiceClient]:
        if connection_string is not None:
            with BlobServiceClient.from_connection_string(connection_string) as client:
                yield client
        else:
            with (
                DefaultAzureCredential() as credential,
                BlobServiceClient(str(settings.endpoint_url).rstrip("/"), credential) as client,
            ):
                yield client

    return AzureBlobArtifactStore(
        client_factory, container=settings.bucket, local_account_key=account_key
    )
