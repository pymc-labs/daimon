"""S3-compatible private artifact storage with short-lived GET capabilities."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Protocol, cast
from urllib.parse import quote

# Botocore does not ship inline type metadata for these runtime imports.
from botocore.config import Config  # pyright: ignore[reportMissingTypeStubs]
from botocore.session import Session  # pyright: ignore[reportMissingTypeStubs]
from daimon.core.artifacts import ArtifactStore, StoredArtifact
from daimon.core.config import ArtifactsSettings


class _S3Client(Protocol):
    def put_object(self, **kwargs: object) -> object: ...

    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: dict[str, str],
        ExpiresIn: int,
    ) -> str: ...


class S3ArtifactStore:
    """Artifact store backed by any S3-compatible private bucket."""

    def __init__(
        self,
        client: _S3Client,
        *,
        bucket: str,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def upload_and_presign(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str,
        ttl_seconds: int,
    ) -> StoredArtifact:
        """Upload without a public ACL, then sign only this object's GET."""

        def _store_and_sign() -> str:
            basename = key.rsplit("/", maxsplit=1)[-1]
            disposition = f"attachment; filename*=UTF-8''{quote(basename)}"
            response_content_type = (
                "application/octet-stream" if content_type == "image/svg+xml" else content_type
            )
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                ContentDisposition=disposition,
            )
            return self._client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ResponseContentDisposition": disposition,
                    "ResponseContentType": response_content_type,
                },
                ExpiresIn=ttl_seconds,
            )

        url = await asyncio.to_thread(_store_and_sign)
        return StoredArtifact(
            key=key,
            url=url,
            expires_at=self._clock() + dt.timedelta(seconds=ttl_seconds),
        )


def build_artifact_store(settings: ArtifactsSettings) -> ArtifactStore:
    """Construct the production S3-compatible store from validated settings."""
    client = Session().create_client(  # pyright: ignore[reportUnknownMemberType]  # botocore has no inline types
        "s3",
        endpoint_url=str(settings.endpoint_url).rstrip("/"),
        aws_access_key_id=settings.access_key_id.get_secret_value(),
        aws_secret_access_key=settings.secret_access_key.get_secret_value(),
        region_name=settings.region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
        ),
    )
    return S3ArtifactStore(cast(_S3Client, client), bucket=settings.bucket)
