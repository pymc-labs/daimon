"""The little of an MCP client Daimon needs outside a model's tool loop.

Streamable HTTP only: one JSON-RPC request per POST, answered as JSON or as
the protocol's SSE form. Callers own the `httpx.AsyncClient` (no redirects:
the configured URL is the only authority) and check the URL first. Anything
but a well-formed result raises `ValueError`; a refused request raises its
subclass `McpStatusError`, which carries the HTTP status.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

import httpx
from pydantic import JsonValue

PROTOCOL_VERSION = "2025-06-18"
INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "daimon", "version": "0"},
    },
}
MAX_RESPONSE_BYTES = 1024 * 1024


class McpStatusError(ValueError):
    """The server answered a request with something other than a bounded 200."""

    def __init__(self, status_code: int) -> None:
        super().__init__("MCP request refused")
        self.status_code = status_code


def mcp_headers(bearer_token: str) -> dict[str, str]:
    """Headers for every request in one exchange; `mcp_request` adds the session id."""
    return {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Authorization": f"Bearer {bearer_token}",
    }


async def mcp_request(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: Mapping[str, object],
) -> dict[str, JsonValue]:
    """POST one request and return its `result`.

    Keeps the server's `Mcp-Session-Id` in `headers` for the requests after it.
    """
    response = await http.post(url, json=body, headers=headers)
    if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
        raise McpStatusError(response.status_code)
    session = response.headers.get("mcp-session-id")
    if session:
        headers["Mcp-Session-Id"] = session
    return rpc_result(response.content, response.headers.get("content-type", ""), body["id"])


def rpc_result(content: bytes, content_type: str, request_id: object) -> dict[str, JsonValue]:
    """The `result` of the response to `request_id`, sent as JSON or as SSE frames.

    Raises `ValueError` for an error response or anything but one matching result.
    """
    try:
        if content_type.startswith("text/event-stream"):
            frames = [
                cast(JsonValue, json.loads(line[5:].strip()))
                for line in content.decode().splitlines()
                if line.startswith("data:")
            ]
            results = [
                item for item in frames if isinstance(item, dict) and item.get("id") == request_id
            ]
            if len(results) != 1:
                raise ValueError("MCP response is ambiguous")
            payload = results[0]
        else:
            payload = cast(JsonValue, json.loads(content))
        if not isinstance(payload, dict) or payload.get("id") != request_id or "error" in payload:
            raise ValueError("invalid MCP response")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("invalid MCP result")
        return result
    except (ValueError, KeyError):
        raise ValueError("invalid MCP response") from None


async def open_mcp_session(http: httpx.AsyncClient, url: str, headers: dict[str, str]) -> None:
    """`initialize`, then the `initialized` notification the protocol requires before use."""
    await mcp_request(http, url, headers, INITIALIZE_BODY)
    response = await http.post(
        url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    if response.status_code not in (200, 202, 204):
        raise ValueError("MCP initialization refused")
