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


async def _list_tool_names(app: ASGIApp, *, token: str) -> list[str]:
    """Initialize an MCP HTTP session and call tools/list; return tool names."""
    headers = dict(_INIT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        init_resp = await c.post("/mcp", json=_INIT_BODY, headers=headers)
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        body: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        resp = await c.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"tools/list failed: {resp.text}"
        result = _parse_jsonrpc_response(resp)
    tools_payload = result.get("result", result)
    return [t["name"] for t in tools_payload.get("tools", [])]  # type: ignore[union-attr]


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
        "create_thread",
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
async def test_slack_caller_tools_list_omits_list_threads(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Slack caller's tools/list no longer contains list_threads (tagged
    {"discord"} — see the tool-visibility quick task). ``parse_link`` is NOT
    covered here any more: it is now dual-tagged {"discord","slack"} and
    routes to a real Slack branch (see the dispatch test below). The tool's
    runtime gate (channels.py's ``_slack_unsupported("list_threads")``
    branch) survives as defense-in-depth against a transform regression, but
    a Slack caller can no longer reach it through the registered surface at
    all: the tool is both unlisted and uncallable, and a direct
    ``tools/call`` returns the registry's own unknown-tool error, not the
    gate copy."""
    account_id = await _seed_slack_bound_account(db_session, workspace_id="slack-list-threads")
    token = mint_jwt(account_id=account_id, secret=_SECRET, now=dt.datetime.now(dt.UTC))
    app = _make_app(sessionmaker)

    tool_names = await _list_tool_names(app, token=token)

    assert "list_threads" not in tool_names, (
        "list_threads is discord-only tagged; a Slack caller must not see it"
    )

    call_result = await _call_tool(
        app, token=token, tool_name="list_threads", arguments={"channel_id": "C_TEST"}
    )

    assert call_result.get("isError") is True, (
        f"a hidden tool must be uncallable, not just unlisted; got {call_result!r}"
    )
    output_text = _output_text(call_result)
    assert "list_threads is not supported on Slack yet" not in output_text, (
        "a Slack caller calling a hidden tool must hit the registry's unknown-tool "
        f"error, not the runtime gate copy (unreachable through the registered "
        f"surface); got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_parse_link_slack_caller_routes_to_slack_branch(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Slack-token caller reaches the Slack branch through the registered
    parse_link tool: a Slack permalink in, a SlackParsedLink-shaped result
    out (channel_id + message_ts, no guild_id)."""
    account_id = await _seed_slack_bound_account(db_session, workspace_id="slack-parse-link")
    token = mint_jwt(account_id=account_id, secret=_SECRET, now=dt.datetime.now(dt.UTC))
    app = _make_app(sessionmaker)

    call_result = await _call_tool(
        app,
        token=token,
        tool_name="parse_link",
        arguments={"url": "https://acme.slack.com/archives/C0123456789/p1717171717000100"},
    )

    assert call_result.get("isError") is not True, (
        f"a Slack caller's parse_link call must succeed; got {call_result!r}"
    )
    output_text = _output_text(call_result)
    assert '"channel_id":"C0123456789"' in output_text.replace(" ", ""), (
        f"the Slack branch's result must carry the parsed channel_id; got {output_text!r}"
    )
    assert '"message_ts":"1717171717.000100"' in output_text.replace(" ", ""), (
        f"the Slack branch's result must carry the dotted message_ts; got {output_text!r}"
    )
    assert "guild_id" not in output_text, (
        "a Slack parse_link result must not carry Discord's guild_id field"
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
async def test_send_message_slack_caller_no_longer_hits_unsupported_branch(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """send_message used to be Slack-unsupported; it now routes to the Slack send
    impl. With no DAIMON_CRYPTO__KEYS configured on the runtime, the Slack impl's
    ``slack_web_client`` fails with a Slack-specific error the old "not supported
    on Slack yet" branch could never produce — pinning that the dispatch now
    reaches ``_slack_send_message_impl`` instead. Needs a linked PlatformPrincipal
    (platform_user_id) since the send impl resolves slack identity before
    touching crypto."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="slack-send-message")
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
        app,
        token=token,
        tool_name="send_message",
        arguments={"channel_id": "C_TEST", "content": "hi"},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "send_message is not supported on Slack yet" not in output_text, (
        "send_message must no longer hit the Slack-unsupported branch"
    )
    assert "slack tools require DAIMON_CRYPTO__KEYS" in output_text, (
        f"Slack caller's send_message must hit the Slack-only crypto-keys error; "
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


@pytest.mark.asyncio
async def test_read_channel_slack_caller_rejects_discord_before_param(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Slack caller passing Discord's before param gets a ToolError, not a
    silent first page — repeating page 1 reads as a completed sweep (#74)."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="slack-before-param")
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
        app,
        token=token,
        tool_name="read_channel",
        arguments={"channel_id": "C_TEST", "before": "1000"},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "before is Discord-only" in output_text, (
        f"Slack caller's before param must be rejected, not dropped; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_read_channel_discord_caller_rejects_slack_cursor_param(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Discord caller passing Slack's cursor param gets a ToolError, not a
    silent first page."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="discord-cursor-param")
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
        app,
        token=token,
        tool_name="read_channel",
        arguments={"channel_id": "C_TEST", "cursor": "CURSOR_1"},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "cursor is Slack-only" in output_text, (
        f"Discord caller's cursor param must be rejected, not dropped; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_read_thread_slack_caller_rejects_discord_before_param(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Slack caller passing before to read_thread gets a ToolError — Slack
    threads have no pagination, so dropping it would replay page 1 as a sweep."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="slack-thread-before")
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
        app,
        token=token,
        tool_name="read_thread",
        arguments={"thread_id": "C_TEST:1717171717.123456", "before": "1000"},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "before is Discord-only" in output_text, (
        f"Slack caller's before param must be rejected, not dropped; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_read_channel_slack_caller_treats_empty_pagination_params_as_absent(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """MCP clients often send "" for an optional param they mean to omit.

    An empty before must not trip the Discord-only rejection, and an empty
    cursor must not be forwarded to Slack — the call proceeds into the Slack
    impl and fails on the missing crypto keys like any other routed call.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id="slack-empty-params")
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
        app,
        token=token,
        tool_name="read_channel",
        arguments={"channel_id": "C_TEST", "before": "", "cursor": ""},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "slack tools require DAIMON_CRYPTO__KEYS" in output_text, (
        f"empty pagination params must read as absent, not as misuse; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_read_channel_discord_caller_treats_empty_cursor_as_absent(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An empty cursor from a Discord caller must not trip the Slack-only rejection."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="discord-empty-params")
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
        app,
        token=token,
        tool_name="read_channel",
        arguments={"channel_id": "C_TEST", "before": "", "cursor": ""},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "discord tools require DAIMON_DISCORD__BOT_TOKEN" in output_text, (
        f"empty pagination params must read as absent, not as misuse; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_read_thread_slack_caller_treats_empty_before_as_absent(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An empty before on read_thread must not trip the Discord-only rejection."""
    tenant = await make_tenant(
        db_session, platform="slack", workspace_id="slack-thread-empty-before"
    )
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
        app,
        token=token,
        tool_name="read_thread",
        arguments={"thread_id": "C_TEST:1717171717.123456", "before": ""},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "slack tools require DAIMON_CRYPTO__KEYS" in output_text, (
        f"an empty before must read as absent, not as misuse; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_create_thread_slack_caller_routes_to_slack_impl_and_fails_on_missing_crypto(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A Slack-bound caller's create_thread call routes to the Slack impl.

    With no DAIMON_CRYPTO__KEYS configured on the runtime, the Slack impl's
    ``slack_web_client`` fails with a Slack-specific error the Discord impl
    could never produce. Needs a linked PlatformPrincipal for the tenant's
    platform since the Slack impl resolves identity before touching crypto.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id="slack-create-thread")
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
        app,
        token=token,
        tool_name="create_thread",
        arguments={"channel_id": "C_TEST", "name": "t", "content": "hi"},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "slack tools require DAIMON_CRYPTO__KEYS" in output_text, (
        f"Slack caller's create_thread must hit the Slack-only crypto-keys error; got {output_text!r}"
    )


@pytest.mark.asyncio
async def test_create_thread_discord_caller_routes_to_discord_impl_and_fails_on_missing_bot_token(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The same registered create_thread tool routes a Discord-bound caller to
    the Discord impl, producing the Discord-side bot-token error instead — the
    asymmetry with the Slack test above pins the platform-based dispatch.

    A valid name ("t") is passed: ``_create_thread_impl`` validates ``name``
    before ``_require_bot_token``, so an empty/invalid name would surface the
    name-validation error instead of the bot-token error this test pins.
    """
    tenant = await make_tenant(db_session, platform="discord", workspace_id="discord-create-thread")
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
        app,
        token=token,
        tool_name="create_thread",
        arguments={"channel_id": "C_TEST", "name": "t", "content": "hi"},
    )

    assert call_result.get("isError") is True
    output_text = _output_text(call_result)
    assert "discord tools require DAIMON_DISCORD__BOT_TOKEN" in output_text, (
        f"Discord caller's create_thread must hit the Discord-side bot-token error; got {output_text!r}"
    )
