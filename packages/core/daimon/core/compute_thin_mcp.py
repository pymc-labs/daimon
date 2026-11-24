"""One inline tool call to a session's thin MCP server, the same for every provider.

Errors deliberately omit remote bodies, headers and URLs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

import httpx
from daimon.core.compute_provider import ComputeProviderError
from pydantic import JsonValue, TypeAdapter

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


async def _rpc(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, JsonValue],
) -> httpx.Response:
    async with client.stream("POST", url, headers=headers, json=json) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 8 * 1024 * 1024:
                raise ComputeProviderError(
                    "Session output is too large. Read a smaller file chunk."
                )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(body),
            request=response.request,
        )


async def call_thin_mcp(
    url: str,
    token: str,
    tool: str,
    arguments: dict[str, JsonValue],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    platform_headers: Mapping[str, str] | None = None,
) -> JsonValue:
    """Call `tool` on the thin MCP server at `url` with the session's bearer token.

    `platform_headers` carries what the hosting platform itself checks before the
    request reaches the session. It never replaces the session's `Authorization`.
    """
    # A new thin MCP client session is safe for inline calls. PR 1 creates
    # no detached MCP tasks and promises no background result handles.
    headers = {
        **(platform_headers or {}),
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
    }
    try:
        async with (
            asyncio.timeout(50),
            httpx.AsyncClient(transport=transport, timeout=50, follow_redirects=False) as client,
        ):
            init = await _rpc(
                client,
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "daimon-compute", "version": "1"},
                    },
                },
            )
            init.raise_for_status()
            if "mcp-session-id" in init.headers:
                headers["Mcp-Session-Id"] = init.headers["mcp-session-id"]
            headers["MCP-Protocol-Version"] = "2025-03-26"
            try:
                ready = await _rpc(
                    client,
                    url,
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                ready.raise_for_status()
                result = await _rpc(
                    client,
                    url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
                )
                result.raise_for_status()
            finally:
                # Inline calls retain no task handles, so release stateful
                # MCP sessions instead of leaking one server session per call.
                if "Mcp-Session-Id" in headers:
                    try:
                        async with asyncio.timeout(2):
                            await client.delete(url, headers=headers, timeout=2)
                    except (httpx.HTTPError, TimeoutError):
                        pass

    except (httpx.HTTPError, TimeoutError):
        raise ComputeProviderError(
            "The session did not answer. Check its status before retrying a command."
        ) from None
    try:
        if "text/event-stream" in result.headers.get("content-type", ""):
            messages = [
                line[5:].strip() for line in result.text.splitlines() if line.startswith("data:")
            ]
            payload = next(
                json.loads(line) for line in reversed(messages) if json.loads(line).get("id") == 2
            )
        else:
            payload = result.json()
        if payload.get("error") or payload["result"].get("isError"):
            raise ComputeProviderError("The session refused the operation.")
        data = payload["result"]
        if "structuredContent" in data:
            return _JSON.validate_python(data["structuredContent"])
        return _JSON.validate_python(data.get("content", []))
    except (ValueError, KeyError, StopIteration, TypeError):
        raise ComputeProviderError("The session returned an invalid result.") from None
