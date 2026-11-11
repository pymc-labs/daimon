"""A capability feature Off refuses its tools at the server, on every path.

Drives the real app: the live registry, the real identity middleware and `call_tool` proxy.
"""

from __future__ import annotations

import uuid

import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.capability_tools import CAPABILITY_TOOLS
from daimon.core.features.tool_gate import STOP_TOOLS, server_gate_key
from daimon.core.repo_memory_tools import REPO_MEMORY_TOOLS
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.factories import make_tenant
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_tool_schema_snapshot import (
    _fully_configured_settings,  # pyright: ignore[reportPrivateUsage]
)

OFF = "is turned off here"


async def _app(sessionmaker: async_sessionmaker[AsyncSession]) -> FastMCP:
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    return app.state.mcp


async def _tenant(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessionmaker() as session, session.begin():
        return (await make_tenant(session)).id


async def _set(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    key: str,
    value: str,
    target: FeatureTarget | None = None,
) -> None:
    async with sessionmaker() as session, session.begin():
        await set_feature_value(
            session,
            tenant_id=tenant_id,
            target=target or FeatureTarget("workspace"),
            key=key,
            value=value,
            set_by=None,
            surface="chat",
        )


def _as(monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, **claims: object) -> None:
    token = AccessToken(
        token="t",
        client_id="c",
        scopes=[],
        claims={"sub": str(uuid.uuid4()), "role": "user", "tenant_id": str(tenant_id), **claims},
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.mcp_identity.get_access_token", lambda: token
    )


async def _error(client: Client[FastMCPTransport], tool: str, arguments: dict[str, object]) -> str:
    try:
        await client.call_tool(tool, arguments)
    except ToolError as exc:
        return str(exc)
    return ""


async def test_every_capability_tool_when_its_feature_is_off_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    registered = {t.name for t in await mcp.local_provider.list_tools()}
    gated = {tool for tools in CAPABILITY_TOOLS.values() for tool in tools}
    assert gated <= registered, f"capability tools missing from the registry: {gated - registered}"
    tenant_id = await _tenant(sessionmaker)
    for key in CAPABILITY_TOOLS:
        await _set(sessionmaker, tenant_id, key, "off")
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        for key, tools in sorted(CAPABILITY_TOOLS.items()):
            for tool in sorted(tools - STOP_TOOLS):
                message = await _error(client, tool, {})
                assert OFF in message, f"{tool} ran with {key} Off: {message!r}"
        for tool in sorted(STOP_TOOLS):
            message = await _error(client, tool, {})
            assert OFF not in message, f"{tool} must stay callable to stop running work"


async def test_repo_memory_tools_when_memory_is_off_for_the_workspace_are_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    await _set(sessionmaker, tenant_id, "repo_memory", "off")
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        for tool in sorted(REPO_MEMORY_TOOLS):
            message = await _error(client, tool, {})
            assert OFF in message, f"{tool} ran with repo_memory Off: {message!r}"


async def test_a_capability_tool_when_its_feature_is_on_is_not_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        message = await _error(client, "list_routines", {})
    assert OFF not in message, f"routines is On by default: {message!r}"


async def test_a_proxied_call_when_the_feature_is_off_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    await _set(sessionmaker, tenant_id, "routines", "off")
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        message = await _error(client, "call_tool", {"name": "list_routines", "arguments": {}})
    assert OFF in message, f"the call_tool proxy reached list_routines: {message!r}"


async def test_an_agent_key_when_its_channel_turns_the_feature_off_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    await _set(sessionmaker, tenant_id, "github", "off", FeatureTarget("channel", channel_id="c1"))
    agent_id = str(uuid.uuid4())
    _as(monkeypatch, tenant_id, platform="discord", agent_id=agent_id, bound_channel_id="c1")
    async with Client(mcp) as client:
        bound = await _error(client, "github_connect", {})
        _as(monkeypatch, tenant_id, platform="discord", agent_id=agent_id, bound_channel_id="c2")
        elsewhere = await _error(client, "github_connect", {})
    assert OFF in bound, f"the key bound to the Off channel reached github_connect: {bound!r}"
    assert OFF not in elsewhere, f"a key in another channel was refused: {elsewhere!r}"


async def test_the_tool_list_when_a_feature_is_off_drops_its_tools_for_an_external_client(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    _as(monkeypatch, tenant_id, platform="cli")
    notebooks = CAPABILITY_TOOLS["notebooks"]
    async with Client(mcp) as client:
        before = {t.name for t in await client.list_tools()}
        await _set(sessionmaker, tenant_id, "notebooks", "off")
        after = {t.name for t in await client.list_tools()}
    assert before & notebooks, "the notebook tools are listed while notebooks is On"
    assert not after & notebooks, f"Off notebook tools listed: {after & notebooks}"
    assert "get_long_job" in after, "a stop tool stays listed though long jobs is Off"


async def test_the_tool_list_for_a_chat_credential_is_left_whole(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native turn checks its live catalogue against its allowlist: the list must not shrink."""
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    _as(monkeypatch, tenant_id, platform="discord", chat_agent_id=str(uuid.uuid4()))
    async with Client(mcp) as client:
        before = {t.name for t in await client.list_tools()}
        await _set(sessionmaker, tenant_id, "routines", "off")
        after = {t.name for t in await client.list_tools()}
    assert before == after, f"a chat credential's list changed: {before ^ after}"


def test_server_gate_key_covers_every_gated_tool_but_the_stop_tools() -> None:
    for key, tools in {**CAPABILITY_TOOLS, "repo_memory": REPO_MEMORY_TOOLS}.items():
        for tool in tools:
            expected = None if tool in STOP_TOOLS else key
            assert server_gate_key(tool) == expected, f"{tool} should be gated by {expected}"
    assert server_gate_key("search_tools") is None, "an ungated tool needs no feature read"
