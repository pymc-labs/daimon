"""The real host harness/MCP clients honor an operator CA bundle over TLS."""

import asyncio
import ssl
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import httpx
import pytest
from daimon.core.mcp_oauth import discovery
from daimon.core.turn.selfhosted_deployment import build_harness_http_client
from mux.drivers.selfhosted.transport import DirectTransport


async def test_host_harness_and_mcp_trust_ssl_cert_file(tmp_path, monkeypatch):
    certificate, key = tmp_path / "ca.crt", tmp_path / "tls.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    combined = tmp_path / "ca-bundle.crt"
    combined.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + certificate.read_bytes())
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(certificate, key)
    received: list[bytes] = []

    async def respond(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            received.append(headers)
            for line in headers.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    await reader.readexactly(int(line.split(b":", 1)[1]))
            body = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=server_tls)
    port = server.sockets[0].getsockname()[1]
    endpoint = f"https://127.0.0.1:{port}"

    # This probe normally allows only public URLs. Permit only our synthetic
    # loopback endpoint for this test; actual TCP/TLS and production clients run.
    def local_probe_url(url, *, what):
        assert urlsplit(url).scheme == "https" and urlsplit(url).hostname == "127.0.0.1"

    monkeypatch.setattr(discovery, "assert_public_https_url", local_probe_url)
    try:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        async with build_harness_http_client() as client:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await client.get(endpoint + "/v0/healthz")
        assert received == []
        monkeypatch.setenv("SSL_CERT_FILE", str(combined))
        async with build_harness_http_client() as client:
            transport = DirectTransport(client, base_url=endpoint, token="synthetic-harness")
            assert await transport.request("GET", "/v0/healthz", session_id="s", after_seq=0) == {
                "ok": True
            }
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await client.get(f"https://localhost:{port}/v0/healthz")
        assert (
            await discovery.probe_bearer_token(endpoint + "/mcp", "synthetic-mcp")
        ).accepts_bearer
        assert len(received) == 2
        assert b"Bearer synthetic-harness" in received[0]
        assert b"Bearer synthetic-mcp" in received[1]
    finally:
        server.close()
        await server.wait_closed()
