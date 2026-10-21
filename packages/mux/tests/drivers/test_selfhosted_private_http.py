"""Stored insecure-HTTP consent reaches the direct private-cluster transport."""

import httpx
import pytest
from mux.drivers.selfhosted.transport import DirectTransport


@pytest.mark.parametrize(
    "url", ["http://harness:8088", "http://harness.daimon.svc.cluster.local:8088"]
)
async def test_private_http_needs_explicit_opt_in(url):
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            DirectTransport(client, base_url=url, token="test")
        DirectTransport(client, base_url=url, token="test", allow_insecure_http=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example.com:8088",
        "http://169.254.169.254",
        "http://user:pass@harness:8088",
        "http://metadata.google.internal",
        "http://metadata",
        "http://[fd00:ec2::254]",
        "http://[::ffff:169.254.169.254]",
        "http://[::169.254.169.254]",
    ],
)
async def test_http_opt_in_does_not_allow_external_or_metadata_or_credentials(url):
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            DirectTransport(client, base_url=url, token="test", allow_insecure_http=True)


def private_listener_address():
    import ipaddress
    import subprocess

    networks = [
        ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    for value in subprocess.check_output(["hostname", "-I"], text=True).split():
        address = ipaddress.ip_address(value)
        if address.version == 4 and any(address in network for network in networks):
            return str(address)
    pytest.fail("Real listener test requires a non-loopback RFC1918 interface")


async def test_plain_http_mcp_credential_refuses_before_real_listener_or_token_provider():
    import asyncio

    from mux.errors import ProviderError

    # The listener is reached through the machine's actual non-loopback address,
    # not a mocked HTTP transport or a loopback URL disguised by the test.
    address = private_listener_address()
    connections = 0
    received: list[bytes] = []
    token_calls = 0

    async def respond(reader, writer):
        nonlocal connections
        connections += 1
        try:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}'
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def token_provider():
        nonlocal token_calls
        token_calls += 1
        return "synthetic-harness"

    server = await asyncio.start_server(respond, "0.0.0.0", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            transport = DirectTransport(
                client,
                base_url=f"http://{address}:{port}",
                token_provider=token_provider,
                allow_insecure_http=True,
            )
            with pytest.raises(ProviderError, match="C3.9a") as refused:
                await transport.request(
                    "POST",
                    "/v0/turns",
                    session_id="test",
                    after_seq=0,
                    mcp_authorization="Bearer synthetic-mcp",
                )
            assert refused.value.category == "permission"
            assert refused.value.retryable is False
            assert connections == 0 and received == [] and token_calls == 0
            # Positive control: the very same listener and opted-in transport are
            # reachable when no MCP credential is carried.
            assert await transport.request("POST", "/v0/turns", session_id="test", after_seq=0) == {
                "ok": True
            }
            assert connections == 1 and len(received) == 1 and token_calls == 1
            assert b"x-daimon-mcp-authorization" not in received[0].lower()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("foundry", [False, True])
async def test_mcp_header_reaches_real_https_or_loopback_listener(tmp_path, tls, foundry):
    import asyncio
    import ssl
    import subprocess

    from mux.drivers.selfhosted.transport import FoundryInvocationsTransport

    address = private_listener_address() if tls else "127.0.0.1"
    server_tls = None
    client_tls = ssl.create_default_context()
    if tls:
        certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
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
                f"/CN={address}",
                "-addext",
                f"subjectAltName=IP:{address}",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ],
            check=True,
            capture_output=True,
        )
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(certificate, key)
        client_tls.load_verify_locations(cafile=certificate)
    received: list[bytes] = []

    async def respond(reader, writer):
        try:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}'
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "0.0.0.0", 0, ssl=server_tls)
    port = server.sockets[0].getsockname()[1]
    endpoint = f"{'https' if tls else 'http'}://{address}:{port}"
    try:
        async with httpx.AsyncClient(verify=client_tls, trust_env=False, timeout=2) as client:
            transport = (
                FoundryInvocationsTransport(
                    client, project_endpoint=endpoint, agent_name="test", token="synthetic-harness"
                )
                if foundry
                else DirectTransport(client, base_url=endpoint, token="synthetic-harness")
            )
            assert await transport.request(
                "POST",
                "/v0/turns",
                session_id="test",
                after_seq=0,
                mcp_authorization="Bearer synthetic-mcp",
            ) == {"ok": True}
        assert len(received) == 1
        assert b"x-daimon-mcp-authorization: bearer synthetic-mcp" in received[0].lower()
        assert b"authorization: bearer synthetic-harness" in received[0].lower()
    finally:
        server.close()
        await server.wait_closed()
