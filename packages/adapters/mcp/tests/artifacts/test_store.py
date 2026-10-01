from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlparse

from daimon.adapters.mcp.artifacts import S3ArtifactStore, build_artifact_store
from daimon.core.config import ArtifactsSettings


class FakeS3Client:
    def __init__(self) -> None:
        self.put_kwargs: dict[str, Any] | None = None
        self.presign_kwargs: dict[str, Any] | None = None

    def put_object(self, **kwargs: object) -> object:
        self.put_kwargs = kwargs
        return {}

    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: dict[str, str],
        ExpiresIn: int,
    ) -> str:
        self.presign_kwargs = {
            "client_method": client_method,
            "Params": Params,
            "ExpiresIn": ExpiresIn,
        }
        return "https://bucket.example.test/private/chart.png?signature=secret"


async def test_s3_store_uploads_privately_and_honours_presigned_ttl() -> None:
    client = FakeS3Client()
    now = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.UTC)
    store = S3ArtifactStore(client, bucket="private-artifacts", clock=lambda: now)

    stored = await store.upload_and_presign(
        key="tenant/t/account/a/session/s/chart.png",
        content=b"png-bytes",
        content_type="image/png",
        ttl_seconds=321,
    )

    assert client.put_kwargs == {
        "Bucket": "private-artifacts",
        "Key": "tenant/t/account/a/session/s/chart.png",
        "Body": b"png-bytes",
        "ContentType": "image/png",
        "ContentDisposition": "attachment; filename*=UTF-8''chart.png",
    }
    assert "ACL" not in client.put_kwargs
    assert client.presign_kwargs == {
        "client_method": "get_object",
        "Params": {
            "Bucket": "private-artifacts",
            "Key": "tenant/t/account/a/session/s/chart.png",
            "ResponseContentDisposition": "attachment; filename*=UTF-8''chart.png",
            "ResponseContentType": "image/png",
        },
        "ExpiresIn": 321,
    }
    assert stored.expires_at == now + dt.timedelta(seconds=321)
    assert stored.url.endswith("signature=secret")


async def test_s3_store_forces_svg_download_as_octet_stream() -> None:
    client = FakeS3Client()
    store = S3ArtifactStore(client, bucket="private-artifacts")

    await store.upload_and_presign(
        key="tenant/t/account/a/session/s/chart report.svg",
        content=b'<svg onload="alert(1)"></svg>',
        content_type="image/svg+xml",
        ttl_seconds=600,
    )

    assert client.put_kwargs == {
        "Bucket": "private-artifacts",
        "Key": "tenant/t/account/a/session/s/chart report.svg",
        "Body": b'<svg onload="alert(1)"></svg>',
        "ContentType": "image/svg+xml",
        "ContentDisposition": "attachment; filename*=UTF-8''chart%20report.svg",
    }
    assert client.presign_kwargs == {
        "client_method": "get_object",
        "Params": {
            "Bucket": "private-artifacts",
            "Key": "tenant/t/account/a/session/s/chart report.svg",
            "ResponseContentDisposition": "attachment; filename*=UTF-8''chart%20report.svg",
            "ResponseContentType": "application/octet-stream",
        },
        "ExpiresIn": 600,
    }


async def test_production_store_presigns_with_virtual_host_and_sigv4() -> None:
    settings = ArtifactsSettings(
        endpoint_url="https://t3.storageapi.dev/",
        bucket="private-artifacts",
        access_key_id="access-key",
        secret_access_key="secret-key",
        region="auto",
    )
    store = build_artifact_store(settings)
    client = store._client  # pyright: ignore[reportAttributeAccessIssue,reportPrivateUsage]

    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.bucket, "Key": "tenant/t/account/a/chart.png"},
        ExpiresIn=600,
    )

    parsed = urlparse(url)
    assert parsed.hostname == "private-artifacts.t3.storageapi.dev"
    assert parsed.path == "/tenant/t/account/a/chart.png"
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in parsed.query
