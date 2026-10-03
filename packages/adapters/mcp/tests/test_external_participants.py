"""External participants over MCP: never an admin, and refused every tool not allowed them."""

from __future__ import annotations

import uuid

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.auth.verifier import EXTERNAL_CLAIM
from daimon.adapters.mcp.middleware.external_participants import (
    EXTERNAL_ALLOWED_TOOLS,
    external_refusal,
)
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.adapters.mcp.server import create_mcp_app
from daimon.testing.ma import build_stub_anthropic
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.context import Context
from fastmcp.server.transforms.search import BM25SearchTransform
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_tool_schema_snapshot import _fully_configured_settings


async def test_every_allowed_name_is_a_registered_tool(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    names = {tool.name for tool in await app.state.mcp.local_provider.list_tools()}
    assert EXTERNAL_ALLOWED_TOOLS - names == set(), "a renamed tool would be refused silently"
    for name in ("publish_report", "delete_report", "list_agent_keys", "list_skills"):
        assert name in names and external_refusal(name) is not None, f"{name} stays refused"


def _app(captured: list[AuthIdentity], sessionmaker: async_sessionmaker[AsyncSession]) -> FastMCP:

    async def subject(_ctx: object) -> str:
        return str(uuid.uuid4())

    async def admin(_ctx: object) -> str:
        return "admin"

    async def none(_ctx: object) -> str | None:
        return None

    async def true(_ctx: object) -> str:
        return "true"

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=subject,
            tenant_resolver=subject,
            role_resolver=admin,
            agent_id_resolver=none,
            is_admin_resolver=true,
            internal_resolver=true,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool  # An allowed name, so an external call reaches it.
    async def now(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        captured.append(await ctx.get_state("auth"))
        return "ok"

    @mcp.tool
    async def create_routine() -> str:  # pyright: ignore[reportUnusedFunction]
        return "created"

    @mcp.tool
    async def send_direct_message() -> str:  # pyright: ignore[reportUnusedFunction]
        return "sent"

    @mcp.tool
    async def brand_new_tool() -> str:  # pyright: ignore[reportUnusedFunction]
        return "new"

    return mcp


@pytest.mark.parametrize("external", [True, False])
async def test_an_external_token_is_never_an_admin_and_is_refused_setup_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    external: bool,
) -> None:
    claims = {EXTERNAL_CLAIM: external, "administered_channel_ids": ["c-1"]}
    token = AccessToken(token="t", client_id="c", scopes=[], claims=claims)
    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.mcp_identity.get_access_token", lambda: token
    )
    captured: list[AuthIdentity] = []
    async with Client(_app(captured, sessionmaker)) as client:
        await client.call_tool("now", {})
        if external:
            for name in ("create_routine", "send_direct_message", "brand_new_tool"):
                with pytest.raises(ToolError, match="another organisation"):
                    await client.call_tool(name, {})
        else:
            assert (await client.call_tool("create_routine", {})).data == "created"
            assert (await client.call_tool("brand_new_tool", {})).data == "new", (
                "a new tool is refused only to people from another organisation"
            )

    [identity] = captured
    assert (identity.is_external, identity.is_admin) == (external, not external), (
        "an admin role and internal claims count only for our own people"
    )


async def test_the_search_proxy_reaches_only_allowed_tools_for_an_external(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = {EXTERNAL_CLAIM: True}
    token = AccessToken(token="t", client_id="c", scopes=[], claims=claims)
    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.mcp_identity.get_access_token", lambda: token
    )
    mcp = _app([], sessionmaker)
    mcp.add_transform(BM25SearchTransform())
    async with Client(mcp) as client:
        await client.call_tool("search_tools", {"query": "routine"})
        await client.call_tool("call_tool", {"name": "now", "arguments": {}})
        with pytest.raises(ToolError, match="another organisation"):
            await client.call_tool("call_tool", {"name": "create_routine", "arguments": {}})


@pytest.mark.parametrize(
    "name", ["publish_report", "create_notebook_upload_url", "get_skill", "a_tool_added_later"]
)
def test_a_tool_not_allowed_is_refused_and_says_nothing_was_done(name: str) -> None:
    refusal = external_refusal(name)
    assert refusal is not None and "another organisation" in refusal, "refused by default"
    assert "Nothing was done" in refusal, "the agent must not claim it acted"


def test_the_conversation_tools_stay_open() -> None:
    for name in ("read_thread", "send_message", "create_timer", "request_mcp_oauth"):
        assert external_refusal(name) is None, f"{name} acts inside the conversation"
    refusal = external_refusal("send_direct_message")
    assert refusal is not None and "Nothing was sent" in refusal, "a DM gets its own reason"
