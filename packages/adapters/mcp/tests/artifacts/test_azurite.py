"""Exercise the local-only shared-key path against a disposable Azurite container."""

from __future__ import annotations

import base64
import datetime as dt
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest
from azure.storage.blob import BlobServiceClient
from daimon.adapters.mcp.artifacts import build_artifact_store
from daimon.core.config import ArtifactsSettings


@pytest.fixture(scope="module")
def azurite_settings() -> Iterator[ArtifactsSettings]:
    if shutil.which("docker") is None:
        pytest.skip("Docker is not installed")
    info = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
    if info.returncode:
        pytest.skip("Docker daemon is unavailable")
    # These random credentials exist only inside this disposable local emulator.
    account = "localartifacts"
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    started = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-p",
            "127.0.0.1::10000",
            "-e",
            f"AZURITE_ACCOUNTS={account}:{key}",
            "mcr.microsoft.com/azure-storage/azurite:3.35.0",
            "azurite-blob",
            "--blobHost",
            "0.0.0.0",
            "--skipApiVersionCheck",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    container = started.stdout.strip()
    try:
        port_output = subprocess.run(
            ["docker", "port", container, "10000/tcp"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        port = port_output.stdout.strip().rsplit(":", 1)[1]
        endpoint = f"http://127.0.0.1:{port}/{account}"
        connection = f"DefaultEndpointsProtocol=http;AccountName={account};AccountKey={key};BlobEndpoint={endpoint}"
        with httpx.Client(timeout=1) as client:
            for _ in range(100):
                try:
                    client.get(endpoint)
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                pytest.fail("Azurite did not become ready")
        with BlobServiceClient.from_connection_string(connection) as service:
            service.create_container("charts")
        yield ArtifactsSettings(
            backend="azure_blob",
            endpoint_url=endpoint,
            bucket="charts",
            azure_connection_string=connection,
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, timeout=15, check=True
        )


async def test_private_upload_sas_read_headers_and_tamper_rejection(
    azurite_settings: ArtifactsSettings,
) -> None:
    store = build_artifact_store(azurite_settings)
    stored = await store.upload_and_presign(
        key="tenant/t/account/a/chart report.svg",
        content=b"<svg></svg>",
        content_type="image/svg+xml",
        ttl_seconds=321,
    )
    parsed = urlsplit(stored.url)
    async with httpx.AsyncClient() as client:
        response = await client.get(stored.url)
        assert response.status_code == 200
        assert response.content == b"<svg></svg>"
        assert response.headers["content-type"] == "application/octet-stream"
        assert (
            response.headers["content-disposition"]
            == "attachment; filename*=UTF-8''chart%20report.svg"
        )
        unsigned = urlunsplit(parsed._replace(query=""))
        assert (await client.get(unsigned)).status_code in {403, 404}
        assert (
            await client.put(
                stored.url, content=b"replace", headers={"x-ms-blob-type": "BlockBlob"}
            )
        ).status_code == 403
        other_blob = urlunsplit(
            parsed._replace(path=parsed.path.replace("chart%20report", "other"))
        )
        assert (await client.get(other_blob)).status_code == 403
        query = dict(parse_qsl(parsed.query))
        assert query["se"] == stored.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        query["se"] = "2000-01-01T00:00:00Z"
        tampered = urlunsplit(parsed._replace(query=urlencode(query)))
        assert (await client.get(tampered)).status_code == 403


async def test_expired_signed_capability_is_refused(
    azurite_settings: ArtifactsSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = build_artifact_store(azurite_settings)
    monkeypatch.setattr(store, "_clock", lambda: dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10))
    stored = await store.upload_and_presign(
        key="tenant/t/expired.png",
        content=b"png",
        content_type="image/png",
        ttl_seconds=1,
    )
    async with httpx.AsyncClient() as client:
        response = await client.get(stored.url)
    assert response.status_code == 403
