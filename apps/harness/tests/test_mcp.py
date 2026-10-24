"""Real MCP transport approval, redaction and redirect refusal."""

from __future__ import annotations

import httpx
from daimon_harness import model_adapter

from .mcp_probe import mcp_turn_probe


async def test_mcp_tool_executes_after_confirmation(tmp_path, monkeypatch):
    await mcp_turn_probe(tmp_path, monkeypatch)


async def test_credential_http_client_does_not_follow_redirects(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.example/mcp"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        model_adapter.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(redirect), **kwargs),
    )
    client = model_adapter.bound_mcp_http_client(headers={"X-Credential": "synthetic-secret"})
    async with client:
        response = await client.get("https://allowed.example/mcp")
    assert response.status_code == 307
    assert len(requests) == 1
    assert requests[0].url.host == "allowed.example"
