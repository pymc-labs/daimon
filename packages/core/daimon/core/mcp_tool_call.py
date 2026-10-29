"""One bounded MCP client for unattended watch polls: `tools/list` and `tools/call`.

A poll runs with nobody watching and may carry a credential, so every request:

- goes only to the URL it was given, which must be public https
  (`assert_public_https_url`); the caller passes the registry's canonical URL;
- connects to one address resolved once and checked public, so DNS cannot
  answer differently between the check and the connection (TLS still verifies
  the certificate for the URL's host);
- never follows a redirect (a 3xx is a failure), never reads proxy settings
  from the environment and never sends a cookie back;
- stops reading a response past `MAX_RESPONSE_BYTES`, while it streams, and
  gives each request `REQUEST_TIMEOUT_S`;
- parses JSON strictly: no NaN or Infinity, no duplicate keys.

A failure raises `McpCallError` carrying only a fixed class name. Nothing here
logs, and no error holds the token, the server's text or an HTTP exception.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Final, Literal, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_https_url
from pydantic import JsonValue

__all__ = [
    "MAX_RESPONSE_BYTES",
    "REQUEST_TIMEOUT_S",
    "McpCallError",
    "McpToolSession",
    "mcp_tool_session",
    "resolve_public_address",
]

MAX_RESPONSE_BYTES: Final = 262144
REQUEST_TIMEOUT_S: Final = 10.0
MAX_CATALOGUE_PAGES: Final = 20
PROTOCOL_VERSION: Final = "2025-06-18"

CallFailure = Literal[
    "address", "redirect", "refused", "too_large", "timeout", "transport", "protocol", "tool_error"
]

Resolver = Callable[[str, int], Awaitable[str]]


class McpCallError(Exception):
    """A poll's MCP request failed. `kind` is the only detail ever kept."""

    def __init__(self, kind: CallFailure) -> None:
        super().__init__(kind)
        self.kind: CallFailure = kind


def _public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        # An IPv6 form that carries an IPv4 address is judged by that address.
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is None and address.teredo is not None:
            embedded = address.teredo[1]
        if embedded is not None and not _public(embedded):
            return False
    return address.is_global and not address.is_multicast


async def resolve_public_address(host: str, port: int) -> str:
    """The address to connect to; every address the name resolves to must be public."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]
    except (OSError, ValueError):
        raise McpCallError("address") from None
    if not addresses or not all(_public(address) for address in addresses):
        raise McpCallError("address")
    return str(addresses[0])


def _refuse_constant(_: str) -> object:
    raise ValueError("non-finite number")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def strict_json(text: str) -> JsonValue:
    """Parse JSON refusing NaN, Infinity and duplicate keys; deep nesting fails too."""
    try:
        return cast(
            JsonValue,
            json.loads(text, parse_constant=_refuse_constant, object_pairs_hook=_no_duplicates),
        )
    except (ValueError, RecursionError):
        raise McpCallError("protocol") from None


def _sse_messages(text: str) -> list[JsonValue]:
    """Each event's `data:` lines, joined as the SSE format says, parsed as JSON."""
    messages: list[JsonValue] = []
    data: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line == "":
            if data:
                messages.append(strict_json("\n".join(data)))
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    return messages


class McpToolSession:
    """One initialised MCP session against one pinned address. Use `mcp_tool_session`."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        target: str,
        host_header: str,
        sni_host: str,
        token: str | None,
    ) -> None:
        self._http = http
        self._target = target
        self._sni_host = sni_host
        self._headers = {
            "Host": host_header,
            "Accept": "application/json, text/event-stream",
            # The cap counts decoded bytes either way; this just asks for none.
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token}"
        self._next_id = 1

    async def _post(self, body: Mapping[str, object]) -> tuple[int, str, bytes]:
        request = self._http.build_request(
            "POST",
            self._target,
            content=json.dumps(body).encode(),
            headers=self._headers,
            extensions={"sni_hostname": self._sni_host},
        )
        try:
            response = await self._http.send(request, stream=True, follow_redirects=False)
        except httpx.TimeoutException:
            raise McpCallError("timeout") from None
        except httpx.HTTPError:
            raise McpCallError("transport") from None
        try:
            if 300 <= response.status_code < 400:
                raise McpCallError("redirect")
            chunks: list[bytes] = []
            size = 0
            try:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise McpCallError("too_large")
                    chunks.append(chunk)
            except httpx.TimeoutException:
                raise McpCallError("timeout") from None
            except httpx.HTTPError:
                raise McpCallError("transport") from None
            session = response.headers.get("mcp-session-id")
            if session is not None:
                if not (0 < len(session) <= 256 and session.isascii() and session.isprintable()):
                    raise McpCallError("protocol")
                self._headers["Mcp-Session-Id"] = session
            return (
                response.status_code,
                response.headers.get("content-type", ""),
                b"".join(chunks),
            )
        finally:
            await response.aclose()
            # A server's cookie never rides on the next request.
            self._http.cookies.clear()

    async def _rpc(self, method: str, params: Mapping[str, object]) -> dict[str, JsonValue]:
        request_id = self._next_id
        self._next_id += 1
        status, content_type, raw = await self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        if status != 200:
            raise McpCallError("refused")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise McpCallError("protocol") from None
        if content_type.startswith("text/event-stream"):
            replies = [
                message
                for message in _sse_messages(text)
                if isinstance(message, dict) and message.get("id") == request_id
            ]
            if len(replies) != 1:
                raise McpCallError("protocol")
            reply = replies[0]
        else:
            reply = strict_json(text)
        if not isinstance(reply, dict) or reply.get("id") != request_id or "error" in reply:
            raise McpCallError("protocol")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise McpCallError("protocol")
        return result

    async def initialize(self) -> None:
        await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "daimon-watch", "version": "0"},
            },
        )
        status, _, _ = await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if status not in (200, 202, 204):
            raise McpCallError("refused")

    async def list_tools(self) -> tuple[dict[str, JsonValue], ...]:
        """Every tool the server lists, as sent; at most `MAX_CATALOGUE_PAGES` pages."""
        listed: list[dict[str, JsonValue]] = []
        names: set[str] = set()
        cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_CATALOGUE_PAGES):
            result = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise McpCallError("protocol")
            for tool in tools:
                name = tool.get("name") if isinstance(tool, dict) else None
                if not isinstance(tool, dict) or not isinstance(name, str) or name in names:
                    raise McpCallError("protocol")
                names.add(name)
                listed.append(tool)
            following = result.get("nextCursor")
            if following is None:
                return tuple(listed)
            if not isinstance(following, str) or not following or following in cursors:
                raise McpCallError("protocol")
            cursors.add(following)
            cursor = following
        raise McpCallError("too_large")

    async def call_tool(self, name: str, arguments: Mapping[str, JsonValue]) -> JsonValue:
        """The tool's result: `structuredContent`, else its one text item (JSON when it parses).

        `isError` raises `McpCallError("tool_error")`; the error text is dropped.
        """
        result = await self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})
        if result.get("isError") is True:
            raise McpCallError("tool_error")
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise McpCallError("protocol")
        item = content[0]
        if not isinstance(item, dict) or item.get("type") != "text":
            raise McpCallError("protocol")
        text = item.get("text")
        if not isinstance(text, str):
            raise McpCallError("protocol")
        try:
            return strict_json(text)
        except McpCallError:
            return text


@asynccontextmanager
async def mcp_tool_session(
    url: str,
    token: str | None,
    *,
    resolve: Resolver = resolve_public_address,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[McpToolSession]:
    """An initialised session with `url`, sending `token` (if any) to that URL only.

    `url` must already be the canonical ASCII form the registry stores
    (`ascii_mcp_url`). `transport` and `resolve` exist for tests.
    """
    try:
        assert_public_https_url(url, what="watch source")
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except (McpUrlError, ValueError):
        raise McpCallError("address") from None
    if not host or not host.isascii() or parts.username or parts.password:
        raise McpCallError("address")
    address = await resolve(host, port or 443)
    literal = f"[{address}]" if ":" in address else address
    target = urlunsplit(
        ("https", literal if port is None else f"{literal}:{port}", parts.path, parts.query, "")
    )
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT_S,
    ) as http:
        session = McpToolSession(
            http,
            target=target,
            host_header=host if port is None else f"{host}:{port}",
            sni_host=host,
            token=token,
        )
        await session.initialize()
        yield session
