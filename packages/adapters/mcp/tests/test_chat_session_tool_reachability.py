"""The written record of what a chat-turn-shaped session can actually reach.

A chat turn's daimon-mcp vault credential is minted exactly the way
``ensure_agent_mcp_vault`` mints it: subject is the account, no ``agent_id``
claim, no ``is_admin``, no ``internal``. This module drives that exact shape
of session through the real ``httpx.ASGITransport`` + ``create_mcp_app``
handshake and records, per tool the seeded system prompt currently mentions,
whether it is discoverable via ``search_tools`` and, when it is, whether a
``call_tool`` invocation is refused specifically because the session carries
no ``agent_id`` claim (the ``self_edit`` own-identity gate) or reaches the
tool's real implementation.

CHAT_TURN_TOOL_REACHABILITY below is the source of truth for what the seeded
prompt may claim a chat turn can do. Adding a tool name to the prompt without
adding a row here is exactly the drift this file exists to prevent — the
routines lie (a prompt telling Discord users to press a button that doesn't
exist) and the ``set_repo_binding``-from-chat gap were both this same class
of mistake: prompt prose describing mechanism the live tool surface
contradicts.

One outcome is recorded rather than treated as a bug:

- ``set_repo_binding`` / ``get_repo_binding`` are tagged ``agent-chat`` and
  therefore invisible to a chat-turn session entirely — a chat-turn vault
  credential carries no ``agent_id`` claim, so it can never satisfy the
  precondition these tools' own ``_require_agent_id`` gate enforces.
  Minting an agent claim into the account-scoped vault credential would
  make them reachable, but that credential is deliberately account-scoped,
  not agent-scoped, so it is out of scope here.
- ``request_mcp_credential`` / ``request_env_credential`` reject Slack
  callers and require a platform-bound identity (``auth.platform_user_id``).
  The session built here carries a Discord-shaped platform claim, so both
  reach their implementation; on Slack today they would be refused instead.
  These two tools are Discord-only until a Slack-side implementation lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from typing import NamedTuple

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.mcp_auth import mint_jwt
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

from .factories import make_jwt, make_ma_agent

pytestmark = pytest.mark.asyncio

SECRET = "a" * 32
_NOW = dt.datetime(2026, 4, 24, tzinfo=dt.UTC)


class ExpectedOutcome(NamedTuple):
    """One row of the reachability record.

    `discoverable` — is the tool visible in this session's `search_tools`
    results?
    `blocked_by_agent_id` — for discoverable tools only: does the call refuse
    specifically because the session carries no `agent_id` claim (the
    `self_edit` own-identity gate)? `False` means the call reaches the
    tool's real implementation — which may still fail for unrelated business
    reasons (agent not found, validation, ...); that is not tracked here.
    """

    discoverable: bool
    blocked_by_agent_id: bool = False


CHAT_TURN_TOOL_REACHABILITY: dict[str, ExpectedOutcome] = {
    # self_edit.py — tagged agent-chat, so a chat-turn session (no agent_id
    # claim) never discovers them at all.
    "set_repo_binding": ExpectedOutcome(discoverable=False),
    "get_repo_binding": ExpectedOutcome(discoverable=False),
    # skills.py — reads stay ungated; sync_skills stays admin-only.
    "list_skills": ExpectedOutcome(discoverable=True),
    "sync_skills": ExpectedOutcome(discoverable=False),
    # agents.py — the four tools this plan relaxed, plus the two reads that
    # were already ungated.
    "update_agent": ExpectedOutcome(discoverable=True),
    "attach_mcp_server": ExpectedOutcome(discoverable=True),
    "create_agent": ExpectedOutcome(discoverable=True),
    "fork_agent": ExpectedOutcome(discoverable=True),
    "list_agents": ExpectedOutcome(discoverable=True),
    "get_agent": ExpectedOutcome(discoverable=True),
    "archive_agent": ExpectedOutcome(discoverable=False),
    # credential_requests.py — never admin-gated; Discord-only (see module
    # docstring).
    "request_mcp_credential": ExpectedOutcome(discoverable=True),
    "request_env_credential": ExpectedOutcome(discoverable=True),
    # routines.py — ungated by design; needs a platform user identity, which
    # this Discord-shaped session carries.
    "create_routine": ExpectedOutcome(discoverable=True),
}
"""Source of truth for what the seeded prompt may claim a chat turn can do.
The four chat-removal tools land in a later plan and are covered by the
prompt-claim drift gate instead, not by this record."""

_CALL_ARGS: dict[str, dict[str, object]] = {
    "list_skills": {},
    "update_agent": {"name": "demo-agent", "description": "hi"},
    "attach_mcp_server": {
        "agent_name": "demo-agent",
        "server_name": "ctx7",
        "url": "https://ctx7.example/mcp",
    },
    "create_agent": {"name": "demo-new-agent", "model": "claude-opus-4-5"},
    "fork_agent": {"source_name": "demo-agent", "new_name": "demo-fork"},
    "list_agents": {},
    "get_agent": {"name": "demo-agent"},
    "request_mcp_credential": {
        "agent_name": "demo-agent",
        "server_name": "ctx7",
        "url": "https://ctx7.example/mcp",
        "channel_id": "channel-1",
    },
    "request_env_credential": {
        "agent_name": "demo-agent",
        "key": "MY_KEY",
        "purpose": "testing",
        "channel_id": "channel-1",
    },
    "create_routine": {
        "agent_name": "demo-agent",
        "cron_expr": "0 * * * *",
        "timezone": "UTC",
        "trigger_message": "ping",
    },
}


@contextlib.asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
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


async def _mcp_session(
    app: ASGIApp,
    *,
    token: str,
    method: str,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        init_resp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers=headers,
        )
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        body = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}}
        resp = await c.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"{method} failed ({resp.status_code}): {resp.text}"
        return _parse_jsonrpc_response(resp)


def _make_app(sessionmaker: async_sessionmaker[AsyncSession], anthropic: AsyncAnthropic) -> ASGIApp:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        anthropic=anthropic,
    )


def _build_client() -> AsyncAnthropic:
    """One shared MA fake: every agent lookup returns empty, so every relaxed
    tool that resolves an agent by name hits a business "not found" error —
    a real outcome that still proves the call reached the implementation,
    not the visibility or admin/agent-id gates. create_agent gets its own
    create+retrieve route since it doesn't look anything up first."""
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    router.add("GET", r"/v1/skills", lambda _r, _m: list_response([]))
    router.add(
        "POST",
        r"/v1/agents",
        lambda _r, _m: httpx.Response(
            200, json=make_ma_agent(name="demo-new-agent").model_dump(mode="json")
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _r, _m: httpx.Response(
            200, json=make_ma_agent(name="demo-new-agent").model_dump(mode="json")
        ),
    )
    return build_fake_anthropic(router.dispatch)


async def _seed_chat_turn_session(sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    """Seed a tenant + account shaped exactly like a Discord chat turn and
    return the daimon-mcp-vault-shaped JWT for it: account-scoped subject, no
    agent_id claim, no is_admin, default (non-admin) role, Discord platform
    with a bound platform_user_id."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="chat-turn-reachability")
        account = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s,
            platform="discord",
            external_id="discord-user-1",
            tenant=tenant,
            account=account,
        )
    return make_jwt(account_id=account.id)


@pytest.mark.parametrize("tool_name,expected", sorted(CHAT_TURN_TOOL_REACHABILITY.items()))
async def test_chat_turn_session_matches_recorded_reachability(
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
    expected: ExpectedOutcome,
) -> None:
    """Each tool's discoverability and (when discoverable) call outcome must
    match the row recorded in CHAT_TURN_TOOL_REACHABILITY."""
    token = await _seed_chat_turn_session(sessionmaker)
    app = _make_app(sessionmaker, _build_client())

    search_result = await _mcp_session(
        app,
        token=token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": tool_name.replace("_", " ")}},
    )
    search_payload = search_result.get("result", search_result)
    search_content = search_payload.get("content", [])  # type: ignore[union-attr]
    search_text = " ".join(
        item.get("text", "") for item in search_content if isinstance(item, dict)
    )
    is_discoverable = tool_name in search_text
    assert is_discoverable == expected.discoverable, (
        f"{tool_name}: expected discoverable={expected.discoverable}, got {is_discoverable}; "
        f"search_tools output: {search_text!r}"
    )
    if not expected.discoverable:
        return

    call_result = await _mcp_session(
        app,
        token=token,
        method="tools/call",
        params={
            "name": "call_tool",
            "arguments": {"name": tool_name, "arguments": _CALL_ARGS.get(tool_name, {})},
        },
    )
    call_payload = call_result.get("result", call_result)
    call_content = call_payload.get("content", [])  # type: ignore[union-attr]
    call_text = " ".join(
        item.get("text", "") for item in call_content if isinstance(item, dict)
    ).lower()
    is_blocked_by_agent_id = "not minted for an agent session" in call_text
    assert is_blocked_by_agent_id == expected.blocked_by_agent_id, (
        f"{tool_name}: expected blocked_by_agent_id={expected.blocked_by_agent_id}, "
        f"got {is_blocked_by_agent_id}; call_tool output: {call_text!r}"
    )
    assert "manage server" not in call_text, (
        f"{tool_name}: a chat-turn call must never re-trigger the admin gate; got: {call_text!r}"
    )


async def test_agent_id_claim_session_discovers_agent_chat_and_self_edit_tools_only(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A session narrowed to agent-chat-tagged tools (an agent_id claim)
    discovers the seven agent-chat tools plus the eight self-edit/vault tools
    tagged in this plan, and none of the tenant-wide roster tools."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="agent-chat-reachability")
        account = await make_account(s, tenant=tenant)
    token = mint_jwt(account_id=account.id, secret=SECRET.encode(), now=_NOW, agent_id=uuid.uuid4())
    app = _make_app(sessionmaker, _build_client())

    result = await _mcp_session(app, token=token, method="tools/list")
    payload = result.get("result", result)
    tool_names = {t["name"] for t in payload.get("tools", [])}  # type: ignore[union-attr]

    expected_agent_chat_tools = {
        "describe_agent",
        "list_my_sessions",
        "start_turn",
        "continue_turn",
        "cancel_turn",
        "get_my_session",
        "list_events",
        "self_write_file",
        "self_read_file",
        "self_list_files",
        "self_delete_file",
        "set_repo_binding",
        "get_repo_binding",
        "clear_repo_binding",
        "list_credentials",
    }
    assert tool_names == expected_agent_chat_tools, (
        f"an agent_id-claim session must discover exactly the agent-chat-tagged tool set; "
        f"got: {sorted(tool_names)}"
    )
    roster_tool_names = {
        "create_agent",
        "update_agent",
        "attach_mcp_server",
        "fork_agent",
    }
    assert not (roster_tool_names & tool_names), (
        f"an agent_id-claim session must not discover any tenant-wide roster tool; "
        f"got: {tool_names}"
    )
