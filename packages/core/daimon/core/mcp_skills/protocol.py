"""The few MCP Skills extension calls Daimon makes, over stateless Streamable HTTP.

The extension (`io.modelcontextprotocol/skills`) needs protocol revision
2026-07-28, whose requests carry their version, client info and capabilities
in `_meta` and mirror the method (and a read's URI) into headers; there is no
`initialize` and no MCP session. A server that answers `server/discover` with
anything else does not serve skills. Callers own the `httpx.AsyncClient` (no
redirects: the configured URL is the only authority).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import httpx
from daimon.core.mcp_rpc import McpStatusError, rpc_result
from pydantic import JsonValue

PROTOCOL_VERSION: Final = "2026-07-28"
SKILLS_EXTENSION: Final = "io.modelcontextprotocol/skills"
CLIENT_INFO: Final[dict[str, JsonValue]] = {"name": "daimon", "version": "0"}
#: A `server/discover` or `skills/list` page; a file read gets its manifest size.
MAX_LISTING_BYTES: Final = 4 * 1024 * 1024
#: `skills/list` pages read before giving up on a server's listing.
MAX_PAGES: Final = 50
_HEADER_SAFE = frozenset(chr(c) for c in range(0x20, 0x7F))


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One server as Daimon reaches it: its URL and the bearer token to send."""

    url: str
    bearer: str


@dataclass(frozen=True, slots=True)
class SkillListing:
    """Every entry of a `skills/list` walk, and how long the server says it stays fresh."""

    entries: tuple[JsonValue, ...]
    ttl_ms: int | None


def _header_value(value: str) -> str:
    """`value` as a header, in the transport's Base64 form when it is not plain ASCII."""
    plain = (
        value
        and set(value) <= _HEADER_SAFE
        and value == value.strip()
        and not (value.startswith("=?base64?") and value.endswith("?="))
    )
    if plain:
        return value
    return f"=?base64?{base64.b64encode(value.encode()).decode()}?="


def request_body(method: str, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """A request carrying the per-request protocol fields the revision requires."""
    meta: dict[str, JsonValue] = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": {"extensions": {SKILLS_EXTENSION: {}}},
    }
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": method,
        "params": {**params, "_meta": meta},
    }


def request_headers(endpoint: Endpoint, method: str, uri: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {endpoint.bearer}",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if uri is not None:
        headers["Mcp-Name"] = _header_value(uri)
    return headers


async def call(
    http: httpx.AsyncClient,
    endpoint: Endpoint,
    method: str,
    params: Mapping[str, JsonValue],
    *,
    max_bytes: int = MAX_LISTING_BYTES,
) -> dict[str, JsonValue]:
    """POST one request; return its complete `result`.

    Raises `McpStatusError` for a non-200 answer or one past `max_bytes`, and
    `ValueError` for an error or any result but a complete one.
    """
    body = request_body(method, params)
    uri = params.get("uri") if method == "resources/read" else None
    headers = request_headers(endpoint, method, uri if isinstance(uri, str) else None)
    async with http.stream("POST", endpoint.url, json=body, headers=headers) as response:
        if response.status_code != 200:
            raise McpStatusError(response.status_code)
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content += chunk
            if len(content) > max_bytes:
                raise McpStatusError(response.status_code)
        content_type = response.headers.get("content-type", "")
    result = rpc_result(bytes(content), content_type, body["id"])
    if result.get("resultType", "complete") != "complete":
        raise ValueError("MCP result is not complete")
    return result


async def fetch_serves_skills(http: httpx.AsyncClient, endpoint: Endpoint) -> bool:
    """Whether the server speaks this revision and declares resources and the extension.

    A server of an earlier revision refuses the request (400, 404 or 405): it
    serves no skills, which is an answer, not a failure.
    """
    try:
        result = await call(http, endpoint, "server/discover", {})
    except McpStatusError as err:
        if err.status_code in (400, 404, 405):
            return False
        raise
    versions = result.get("supportedVersions")
    capabilities = result.get("capabilities")
    if not isinstance(versions, list) or not isinstance(capabilities, dict):
        raise ValueError("invalid server/discover result")
    extensions = capabilities.get("extensions")
    return (
        PROTOCOL_VERSION in versions
        and isinstance(capabilities.get("resources"), dict)
        and isinstance(extensions, dict)
        and isinstance(extensions.get(SKILLS_EXTENSION), dict)
    )


async def fetch_skill_listing(http: httpx.AsyncClient, endpoint: Endpoint) -> SkillListing:
    """Walk `skills/list` to its last page. The shortest page `ttlMs` wins."""
    entries: list[JsonValue] = []
    ttls: list[int] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        result = await call(http, endpoint, "skills/list", {"cursor": cursor} if cursor else {})
        page = result.get("skills")
        if not isinstance(page, list):
            raise ValueError("invalid skills/list result")
        entries.extend(page)
        ttl = result.get("ttlMs")
        if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl >= 0:
            ttls.append(ttl)
        following = result.get("nextCursor")
        if not isinstance(following, str) or not following or following in seen:
            return SkillListing(entries=tuple(entries), ttl_ms=min(ttls) if ttls else None)
        seen.add(following)
        cursor = following
    raise ValueError("skills/list has too many pages")


async def fetch_resource(
    http: httpx.AsyncClient, endpoint: Endpoint, uri: str, *, size: int
) -> bytes:
    """The raw bytes of the one text or blob content `resources/read` returns for `uri`.

    `size` is the manifest's: the answer may carry it base64-encoded and wrapped,
    never much more.
    """
    result = await call(
        http, endpoint, "resources/read", {"uri": uri}, max_bytes=2 * size + 64 * 1024
    )
    contents = result.get("contents")
    if not isinstance(contents, list):
        raise ValueError("invalid resources/read result")
    matching = [item for item in contents if isinstance(item, dict) and item.get("uri") == uri]
    if len(matching) != 1:
        raise ValueError("resources/read did not return the file")
    item = matching[0]
    text, blob = item.get("text"), item.get("blob")
    if isinstance(text, str) and blob is None:
        return text.encode()
    if isinstance(blob, str) and text is None:
        try:
            return base64.b64decode(blob, validate=True)
        except ValueError as err:
            raise ValueError("resources/read returned an invalid blob") from err
    raise ValueError("resources/read returned no content")
