"""Exercise service startup and real async identity transport against a local MI stub."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from azure.identity.aio import get_bearer_token_provider
from daimon_harness.model_adapter import AzureModelAdapter
from daimon_harness.service import create_app

SCOPE = "https://cognitiveservices.azure.com/.default"
TOKEN = "synthetic-managed-identity-token"
HARNESS_TOKEN = "synthetic-identity-probe-harness-token"
CLIENT_ID = "11111111-1111-1111-1111-111111111111"


@asynccontextmanager
async def managed_identity_endpoint():
    calls = []

    async def respond(reader, writer):
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode()
            method, path, _ = headers.split("\r\n", 1)[0].split()
            assert method == "GET"
            assert urlsplit(path).path == "/metadata/identity/oauth2/token"
            assert "x-identity-header: synthetic-identity-header" in headers.lower()
            query = parse_qs(urlsplit(path).query)
            assert query["resource"] == [SCOPE.removesuffix("/.default")]
            assert query["client_id"] == [CLIENT_ID]
            calls.append(query)
            payload = json.dumps(
                {
                    "access_token": TOKEN,
                    "expires_on": str(int(time.time()) + 3600),
                    "resource": SCOPE.removesuffix("/.default"),
                    "token_type": "Bearer",
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        with patch.dict(
            os.environ,
            {
                "IDENTITY_ENDPOINT": f"http://127.0.0.1:{port}/metadata/identity/oauth2/token",
                "IDENTITY_HEADER": "synthetic-identity-header",
                "AZURE_CLIENT_ID": CLIENT_ID,
                "AZURE_OPENAI_ENDPOINT": "https://unit-test.openai.azure.com",
                "HARNESS_TOKEN": HARNESS_TOKEN,
            },
        ):
            for name in (
                "AZURE_OPENAI_API_KEY",
                "AZURE_CLIENT_SECRET",
                "AZURE_CLIENT_CERTIFICATE_PATH",
                "AZURE_FEDERATED_TOKEN_FILE",
                "AZURE_TOKEN_CREDENTIALS",
            ):
                os.environ.pop(name, None)
            yield calls


async def main():
    assert os.geteuid() == 0
    async with managed_identity_endpoint() as calls:
        assert "AZURE_OPENAI_API_KEY" not in os.environ
        app = create_app(home=Path("/home/harness"))
        assert os.geteuid() == 10000
        adapter = app.state.supervisor.adapter
        assert isinstance(adapter, AzureModelAdapter)
        assert not adapter.key and adapter.credential is not None
        async with app.router.lifespan_context(app):
            provider = get_bearer_token_provider(adapter.credential, SCOPE)
            assert await provider() == TOKEN
            assert "IDENTITY_ENDPOINT" not in os.environ
            assert "IDENTITY_HEADER" not in os.environ
            assert "AZURE_CLIENT_ID" not in os.environ
            assert len(calls) == 1
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://harness"
            ) as client:
                response = await client.get(
                    "/v0/healthz", headers={"Authorization": "Bearer " + HARNESS_TOKEN}
                )
                assert response.status_code == 200 and response.json()["ok"]


if __name__ == "__main__":
    asyncio.run(main())
