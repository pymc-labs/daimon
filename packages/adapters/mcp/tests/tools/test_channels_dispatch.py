"""Dispatch tests: shared channel tool names route by auth.platform."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.tools.channels import (
    _slack_unsupported,  # pyright: ignore[reportPrivateUsage]
    register_channel_tools,
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
    SlackSettings,
)
from daimon.core.mcp_auth import mint_jwt
from daimon.core.scope import DeploymentDefault
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

_SECRET = b"a" * 32

_INIT_BODY: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_INIT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@contextlib.asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol around a raw ASGITransport call.

    Mirrors ``test_rbac.py``'s helper of the same name — duplicated locally
    per the testing guideline (inline data/helpers, no cross-test-file
    sharing of private setup).
    """
    send_queue: asyncio.Queue[Message] = asyncio.Queue()
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        await send_queue.put(message)

    async def run_lifespan() -> None:
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(run_lifespan())

    await receive_queue.put({"type": "lifespan.startup"})
    msg = await send_queue.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        msg = await send_queue.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


def _parse_jsonrpc_response(resp: httpx.Response) -> dict[str, object]:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE response: {resp.text!r}")
    return resp.json()  # type: ignore[return-value]


async def _call_tool(
    app: ASGIApp, *, token: str, tool_name: str, arguments: dict[str, object]
) -> dict[str, object]:
    """Initialize an MCP HTTP session and call a tool; return the JSON-RPC result."""
    headers = dict(_INIT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        init_resp = await c.post("/mcp", json=_INIT_BODY, headers=headers)
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        resp = await c.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"tools/call failed ({resp.status_code}): {resp.text}"
        result = _parse_jsonrpc_response(resp)
    return result.get("result", result)  # type: ignore[return-value]


def _make_app(sessionmaker: async_sessionmaker[AsyncSession]) -> ASGIApp:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr(_SECRET.decode()), public_url=HttpUrl("https://x/mcp")
            ),
            # Slack settings present (so register_channel_tools' discord-or-slack
            # gate fires) with NO crypto keys and NO discord settings — this
            # deliberately leaves both platform impls unconfigured so a Slack
            # caller hits the Slack-only crypto-keys error and a Discord caller
            # hits the Discord-only bot-token error, pinning the dispatch.
            slack=SlackSettings(
                signing_secret=SecretStr("x" * 32), app_token=SecretStr("xapp-test")
            ),
            _env_file=None,  # type: ignore[call-arg]  # isolate from repo .env (DAIMON_DISCORD__BOT_TOKEN etc.)
        ),
        sessionmaker=sessionmaker,
    )


def _output_text(call_result: dict[str, object]) -> str:
    content = call_result.get("content", [])  # type: ignore[union-attr]
    return " ".join(item.get("text", "") for item in content if isinstance(item, dict))  # type: ignore[union-attr]


def test_slack_unsupported_message() -> None:
    with pytest.raises(ToolError, match="not supported on Slack yet"):
        _slack_unsupported("search_messages")


@pytest.mark.asyncio
async def test_register_channel_tools_registers_shared_names() -> None:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
    )
    runtime = McpRuntime(
        session_factory=MagicMock(),  # type: ignore[arg-type]  # unused by registration
        client=MagicMock(),  # type: ignore[arg-type]  # unused by registration
        settings=settings,
        deployment_default=DeploymentDefault(),
    )
    mcp = FastMCP(name="test")
    register_channel_tools(mcp, runtime)

    tools = await mcp.list_tools()
    tool_names = {tool.name for tool in tools}
    assert {
        "list_channels",
        "list_threads",
        "read_channel",
        "read_thread",
        "get_message",
        "parse_link",
        "send_message",
        "search_messages",
    } <= tool_names, "all shared channel tool names must be registered once"


# ---------------------------------------------------------------------------
# Behavioral dispatch through the REGISTERED tools (real IdentityMiddleware +
# JWT verifier + DB-backed Tenant.platform), not just direct impl calls.
# ---------------------------------------------------------------------------


async def _seed_slack_bound_account(db_session: AsyncSession, *, workspace_id: str) -> uuid.UUID:
    tenant = await make_tenant(db_session, platform="slack", workspace_id=workspace_id)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    return account.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("list_threads", {"channel_id": "C_TEST"}),
        ("parse_link", {"url": "https://discord.com/channels/1/2"}),
        ("send_message", {"channel_id": "C_TEST", "content": "hi"}),
    ],
)
async def test_slack_caller_calling_unsupported_tool_raises_own_name(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    """A Slack caller invoking a Slack-unsupported registered tool gets a ToolError
    naming that exact tool ("<tool_name> is not supported on Slack yet"), pinning
    the per-site name literal in ``daimon.adapters.mcp.tools.channels``."""
    account_id = await _seed_slack_bound_account(
        db_session, workspace_id=f"slack-unsupported-{tool_name}"
    )
    token = mint_jwt(account_id=account_id, secret=_SECRET, now=dt.datetime.now(dt.UTC))
    app = _make_app(sessionmaker)

    call_result = await _call_tool(app, token=token, tool_name=tool_name, arguments=arguments)

    assert call_result.get("isError") is True, (
        f"{tool_name} must raise a ToolError for a Slack caller; got {call_result!r}"
    )
    output_text = _output_text(call_result)
    assert f"{tool_name} is not supported on Slack yet" in output_text, (
        f"expected {tool_name}'s own name in the Slack-unsupported message; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_search_messages_slack_caller_no_longer_hits_unsupported_branch(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """search_messages used to be Slack-unsupported; it now routes to the Slack
    search impl. With no DAIMON_CRYPTO__KEYS configured on the runtime, the
    Slack impl's ``slack_read_client`` fails with a Slack-specific error the
    old "not supported on Slack yet" branch could never produce — pinning that
    the dispatch now reaches ``_slack_search_messages_impl`` instead. Needs a
    linked PlatformPrincipal (platform_user_id) since the search impl checks
    slack identity before the crypto-keys check, unlike the other
    Slack-unsupported tools which never reach identity resolution."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="slack-search-messages")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U_SLACK_CALLER",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()
    token = mint_jwt(account_id=account.id, secret=_SECRET, now=dt.datetime.now(dt.UTC))
    app = _make_app(sessionmaker)

    call_result = await _call_tool(
        app, token=token, tool_name="search_messages", arguments={"content": "q"}
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "search_messages is not supported on Slack yet" not in output_text, (
        "search_messages must no longer hit the Slack-unsupported branch"
    )
    assert "slack tools require DAIMON_CRYPTO__KEYS" in output_text, (
        f"Slack caller's search_messages must hit the Slack-only crypto-keys error; "
        f"got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_read_channel_slack_caller_routes_to_slack_impl_and_fails_on_missing_crypto(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Slack-bound caller's read_channel call routes to the Slack impl.

    With no DAIMON_CRYPTO__KEYS configured on the runtime, the Slack impl's
    ``slack_web_client`` fails with a Slack-specific error the Discord impl
    could never produce ("slack tools require DAIMON_CRYPTO__KEYS"). That
    error only surfaces past the identity gates, so the caller needs a linked
    PlatformPrincipal (platform_user_id) for the tenant's platform.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id="slack-read-channel")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U_SLACK_CALLER",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()
    token = mint_jwt(account_id=account.id, secret=_SECRET, now=dt.datetime.now(dt.UTC))
    app = _make_app(sessionmaker)

    call_result = await _call_tool(
        app, token=token, tool_name="read_channel", arguments={"channel_id": "C_TEST"}
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "slack tools require DAIMON_CRYPTO__KEYS" in output_text, (
        f"Slack caller's read_channel must hit the Slack-only crypto-keys error; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_read_channel_discord_caller_routes_to_discord_impl_and_fails_on_missing_bot_token(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The same registered read_channel tool routes a Discord-bound caller to the
    Discord impl, producing the Discord-side error instead — the asymmetry with
    the Slack test above pins the platform-based dispatch without needing full
    end-to-end Discord/Slack fixtures."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="discord-read-channel")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="U_DISCORD_CALLER",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()
    token = mint_jwt(account_id=account.id, secret=_SECRET, now=dt.datetime.now(dt.UTC))
    app = _make_app(sessionmaker)

    call_result = await _call_tool(
        app, token=token, tool_name="read_channel", arguments={"channel_id": "C_TEST"}
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "discord tools require DAIMON_DISCORD__BOT_TOKEN" in output_text, (
        f"Discord caller's read_channel must hit the Discord-side bot-token error; got {output_text!r}"
    )
