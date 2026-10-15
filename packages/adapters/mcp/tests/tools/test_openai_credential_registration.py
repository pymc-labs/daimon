"""Default-off, isolated native endpoint registration and credential boundaries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import cast

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import openai_credential_requests as native
from daimon.core.stores.domain import Role
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from mux.errors import UnsupportedCapability


async def test_native_registration_disabled_without_factory_or_identity_io() -> None:
    server = FastMCP("default")
    assert await native.register_openai_credential_tools(server) == ()
    assert await server.list_tools() == []


async def test_enabled_without_native_factory_is_typed_refusal() -> None:
    server = FastMCP("native")
    with pytest.raises(UnsupportedCapability):
        await native.register_openai_credential_tools(server, operator_enabled=True)
    assert await server.list_tools() == []


async def test_existing_legacy_tools_are_never_overridden() -> None:
    server = FastMCP("legacy")

    async def github_connect() -> str:
        return "original dispatcher"

    server.tool(github_connect)
    calls: list[object] = []

    async def grant(auth: AuthIdentity, origin: str) -> native.OpenAICredentialGrant:
        calls.append(None)
        raise AssertionError("collision allocated a native grant")

    before = await server.get_tool("github_connect")
    with pytest.raises(ValueError, match="separate"):
        await native.register_openai_credential_tools(server, operator_enabled=True, grants=grant)
    after = await server.get_tool("github_connect")
    assert after == before and calls == []
    assert after is not None
    result = await server.call_tool("github_connect", {})
    content = cast(Sequence[object], result.content)
    assert isinstance(content[0], TextContent)
    assert content[0].text == "original dispatcher"


@pytest.mark.parametrize("legacy", ["agent", "chat", "external"])
async def test_legacy_or_external_credentials_never_reach_native_factory(
    monkeypatch: pytest.MonkeyPatch, legacy: str
) -> None:
    server = FastMCP("native")
    auth = AuthIdentity(
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        role=Role.ADMIN,
        agent_id=uuid.uuid4() if legacy == "agent" else None,
        chat_agent_id=uuid.uuid4() if legacy == "chat" else None,
        is_external=legacy == "external",
    )
    calls: list[object] = []

    async def authenticated(ctx: Context) -> AuthIdentity:
        return auth

    async def grant(auth: AuthIdentity, origin: str) -> native.OpenAICredentialGrant:
        calls.append(None)
        raise AssertionError("legacy identity was translated into native scope")

    monkeypatch.setattr(native, "_auth", authenticated)
    names = await native.register_openai_credential_tools(
        server, operator_enabled=True, grants=grant
    )
    assert names == ("github_connect", "request_agent_key")
    with pytest.raises(ToolError, match="native execution credential"):
        await server.call_tool("github_connect", {"origin_context_id": str(uuid.uuid4())})
    assert calls == []
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert "agent_name" not in tools["github_connect"].parameters["properties"]
    assert "expected_ma_agent_id" not in tools["github_connect"].parameters["properties"]
    assert "value" not in tools["request_agent_key"].parameters["properties"]
