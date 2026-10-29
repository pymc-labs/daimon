"""The bounded MCP client watch polls use: pinned address, no redirects, capped reads."""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.core.mcp_tool_call import (
    MAX_RESPONSE_BYTES,
    McpCallError,
    mcp_tool_session,
    resolve_public_address,
)
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.preview_parity_mcp import build_parity_host

TOKEN = "watch-sentinel-" + "s" * 40
URL = "https://parity.example/mcp"
PINNED = "93.184.216.34"


async def pinned(host: str, port: int) -> str:
    assert (host, port) == ("parity.example", 443)
    return PINNED


class Recording(httpx.AsyncBaseTransport):
    """Every request the client sends, then the wrapped transport's answer."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


def rpc_reply(request: httpx.Request, result: dict[str, Any]) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    if "id" not in body:
        return httpx.Response(202)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def fake_server(
    call_result: dict[str, Any] | None = None, *, tools: list[dict[str, Any]] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "tools/list":
            return rpc_reply(request, {"tools": tools or []})
        if method == "tools/call":
            return rpc_reply(request, call_result or {})
        return rpc_reply(request, {"protocolVersion": "2025-06-18", "capabilities": {}})

    return httpx.MockTransport(handler)


async def test_reads_the_parity_fixture_through_the_pinned_address_only(tmp_path: Path):
    app = build_parity_host(token=TOKEN, journal_path=tmp_path / "calls.jsonl")
    transport = Recording(httpx.ASGITransport(app=app))
    async with (
        asgi_lifespan(app),
        mcp_tool_session(URL, TOKEN, resolve=pinned, transport=transport) as source,
    ):
        listed = {tool["name"]: tool for tool in await source.list_tools()}
        hints = listed["fixture_counter_read"]["annotations"]
        assert isinstance(hints, dict) and hints["readOnlyHint"] is True
        assert await source.call_tool("fixture_counter_read", {}) == {"markers": []}
    assert transport.requests
    for request in transport.requests:
        # Connect to the pinned address; TLS and Host still name the registered host.
        assert request.url.host == PINNED and request.url.path == "/mcp"
        assert request.headers["host"] == "parity.example"
        assert request.extensions["sni_hostname"] == "parity.example"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert "cookie" not in request.headers


async def test_sse_reply_with_multi_line_data_is_read():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        reply = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"ok": 1}}, indent=1)
        frames = "event: message\n" + "".join(f"data: {line}\n" for line in reply.splitlines())
        noise = 'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        if body["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": '{"rows": [1, 2]}'}]}
            reply = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            frames = f"data: {reply}\n"
        return httpx.Response(
            200, text=noise + frames + "\n", headers={"content-type": "text/event-stream"}
        )

    async with mcp_tool_session(
        URL, None, resolve=pinned, transport=httpx.MockTransport(handler)
    ) as source:
        assert await source.call_tool("t", {}) == {"rows": [1, 2]}


async def test_json_reply_prefers_structured_content_then_one_text_item():
    structured = {"structuredContent": {"a": 1}, "content": [{"type": "text", "text": "x"}]}
    async with mcp_tool_session(
        URL, None, resolve=pinned, transport=fake_server(structured)
    ) as source:
        assert await source.call_tool("t", {}) == {"a": 1}
    plain = {"content": [{"type": "text", "text": "not json"}]}
    async with mcp_tool_session(URL, None, resolve=pinned, transport=fake_server(plain)) as source:
        assert await source.call_tool("t", {}) == "not json"
    for content in ([], [{"type": "image", "data": ""}], [{"type": "text", "text": "a"}] * 2):
        async with mcp_tool_session(
            URL, None, resolve=pinned, transport=fake_server({"content": content})
        ) as source:
            with pytest.raises(McpCallError) as failure:
                await source.call_tool("t", {})
            assert failure.value.kind == "protocol"


async def test_is_error_raises_a_class_without_the_servers_text():
    secret = "remote stack trace with secret-ish detail"
    result = {"isError": True, "content": [{"type": "text", "text": secret}]}
    async with mcp_tool_session(
        URL, TOKEN, resolve=pinned, transport=fake_server(result)
    ) as source:
        with pytest.raises(McpCallError) as failure:
            await source.call_tool("t", {})
    assert failure.value.kind == "tool_error"
    assert secret not in repr(failure.value) and TOKEN not in repr(failure.value)
    assert failure.value.__cause__ is None and failure.value.__context__ is None


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirect_is_refused_and_the_bearer_never_reaches_its_target(status: int):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers={"location": "http://169.254.169.254/latest"})

    with pytest.raises(McpCallError) as failure:
        async with mcp_tool_session(
            URL, TOKEN, resolve=pinned, transport=httpx.MockTransport(handler)
        ):
            pass
    assert failure.value.kind == "redirect"
    assert len(seen) == 1 and seen[0].url.host == PINNED


async def test_an_oversized_stream_is_cut_while_it_streams():
    pulled = 0

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal pulled
            while True:
                pulled += 1
                yield b"[" * 65536

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Endless())

    with pytest.raises(McpCallError) as failure:
        async with mcp_tool_session(
            URL, None, resolve=pinned, transport=httpx.MockTransport(handler)
        ):
            pass
    assert failure.value.kind == "too_large"
    assert pulled * 65536 <= MAX_RESPONSE_BYTES + 65536


async def test_a_huge_sse_frame_is_cut():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: " + b"x" * (10 * 1024 * 1024),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.raises(McpCallError, match="too_large"):
        async with mcp_tool_session(
            URL, None, resolve=pinned, transport=httpx.MockTransport(handler)
        ):
            pass


@pytest.mark.parametrize(
    "body",
    [
        b'{"jsonrpc":"2.0","id":1,"result":{"x":NaN}}',
        b'{"jsonrpc":"2.0","id":1,"result":{"x":Infinity}}',
        b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}',
        b"[" * 200000,
        b'{"jsonrpc":"2.0","id":2,"result":{}}',
        b'{"jsonrpc":"2.0","id":1,"error":{"message":"nope"}}',
        b"\xff\xfe",
    ],
)
async def test_hostile_or_malformed_replies_fail_closed(body: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with pytest.raises(McpCallError) as failure:
        async with mcp_tool_session(
            URL, None, resolve=pinned, transport=httpx.MockTransport(handler)
        ):
            pass
    assert failure.value.kind == "protocol"


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_refusals_are_a_class(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="details " + TOKEN)

    with pytest.raises(McpCallError) as failure:
        async with mcp_tool_session(
            URL, TOKEN, resolve=pinned, transport=httpx.MockTransport(handler)
        ):
            pass
    assert failure.value.kind == "refused" and TOKEN not in str(failure.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://parity.example/mcp",
        "https://127.0.0.1/mcp",
        "https://169.254.169.254/latest",
        "https://[::ffff:10.0.0.1]/mcp",
        "https://localhost/mcp",
        "https://user:pw@parity.example/mcp",
        "https://Bücher.example/mcp",
    ],
)
async def test_unsafe_urls_are_refused_before_any_connection(url: str):
    async def never(host: str, port: int) -> str:
        raise AssertionError("resolved an unsafe URL")

    with pytest.raises(McpCallError) as failure:
        async with mcp_tool_session(url, TOKEN, resolve=never, transport=fake_server()):
            pass
    assert failure.value.kind == "address"


@pytest.mark.parametrize(
    "answers",
    [
        ["10.0.0.7"],
        ["169.254.169.254"],
        ["::ffff:127.0.0.1"],
        ["2002:7f00:1::1"],
        ["fd00::1"],
        # NAT64 and the deprecated IPv4-compatible form are judged by the IPv4 inside.
        ["64:ff9b::a9fe:a9fe"],
        ["::a9fe:a9fe"],
        # DNS rebinding: one public and one private answer is refused as a whole.
        ["93.184.216.34", "127.0.0.1"],
        [],
    ],
)
async def test_names_resolving_to_any_non_public_address_are_refused(
    monkeypatch: pytest.MonkeyPatch, answers: list[str]
):
    async def getaddrinfo(host: str, port: int, **_: object) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, 1, 6, "", (a, port)) for a in answers
        ]

    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    with pytest.raises(McpCallError) as failure:
        await resolve_public_address("parity.example", 443)
    assert failure.value.kind == "address"


async def test_a_public_name_resolves_to_its_first_address(monkeypatch: pytest.MonkeyPatch):
    async def getaddrinfo(host: str, port: int, **_: object) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port))]

    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    assert await resolve_public_address("parity.example", 443) == "93.184.216.34"


async def test_environment_proxies_are_never_used(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}
    real = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setattr(httpx, "AsyncClient", client)
    async with mcp_tool_session(URL, None, resolve=pinned, transport=fake_server()):
        pass
    assert seen["trust_env"] is False and seen["follow_redirects"] is False


async def test_catalogue_pages_are_bounded():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") != "tools/list":
            return rpc_reply(request, {})
        page = int(body["params"].get("cursor", "0"))
        return rpc_reply(request, {"tools": [], "nextCursor": str(page + 1)})

    async with mcp_tool_session(
        URL, None, resolve=pinned, transport=httpx.MockTransport(handler)
    ) as source:
        with pytest.raises(McpCallError, match="too_large"):
            await source.list_tools()


async def test_a_gzip_bomb_is_refused_unread_with_little_memory():
    """200 MiB of zeros in about 200 KB of gzip: refused before httpx decodes a byte."""
    import tracemalloc
    import zlib

    packer = zlib.compressobj(9, zlib.DEFLATED, 31)
    block = bytes(1024 * 1024)
    bomb = b"".join(packer.compress(block) for _ in range(200)) + packer.flush()
    del block
    assert len(bomb) < 512 * 1024
    pulled = 0

    class Bomb(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal pulled
            for start in range(0, len(bomb), 65536):
                pulled += 1
                yield bomb[start : start + 65536]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=Bomb(),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )

    tracemalloc.start()
    try:
        with pytest.raises(McpCallError) as failure:
            async with mcp_tool_session(
                URL, None, resolve=pinned, transport=httpx.MockTransport(handler)
            ):
                pass
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert failure.value.kind == "protocol"
    assert peak < 1024 * 1024
    assert pulled == 0


@pytest.mark.parametrize("encoding", ["deflate", "br", "zstd", "identity, gzip", " GZIP "])
async def test_any_content_encoding_is_refused(encoding: str):
    def handler(request: httpx.Request) -> httpx.Response:
        # A stream, as on the network: nothing is decoded before the client looks.
        return httpx.Response(
            200, stream=httpx.ByteStream(b"{}"), headers={"content-encoding": encoding}
        )

    with pytest.raises(McpCallError) as failure:
        async with mcp_tool_session(
            URL, None, resolve=pinned, transport=httpx.MockTransport(handler)
        ):
            pass
    assert failure.value.kind == "protocol"


async def test_identity_encoding_is_read():
    def handler(request: httpx.Request) -> httpx.Response:
        response = rpc_reply(request, {"tools": []})
        response.headers["content-encoding"] = "identity"
        return response

    async with mcp_tool_session(
        URL, None, resolve=pinned, transport=httpx.MockTransport(handler)
    ) as source:
        assert await source.list_tools() == ()
