"""The written record of what a chat-turn-shaped session can actually reach.

A chat turn's daimon-mcp vault credential is minted exactly the way
``ensure_agent_mcp_vault`` mints it: subject is the account, no ``agent_id``
claim (it carries ``chat_agent_id``), no ``is_admin``, no ``internal``. This module
drives that exact shape of session through the real ``httpx.ASGITransport`` + ``create_mcp_app``
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

Chat execution identity uses ``chat_agent_id``, preserving the ordinary chat
surface. ``agent_id`` remains the restricted external agent-chat credential.

- ``set_repo_binding`` / ``get_repo_binding`` remain tagged ``agent-chat``
  and invisible to ordinary chat sessions.
- ``request_mcp_token`` / ``request_agent_key`` are available on Discord and
  Slack and require a platform-bound caller. This suite exercises Discord;
  the golden-query and authorization journeys cover the platform distinction.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import NamedTuple

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.search_transform import APPROVAL_TOOLS
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.mcp_auth import mint_jwt
from daimon.testing import ma_agent
from daimon.testing.asgi import mcp_session
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

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
    # credential_requests.py — requester-only enrollment, not admin-gated.
    "request_mcp_token": ExpectedOutcome(discoverable=True),
    "request_agent_key": ExpectedOutcome(discoverable=True),
    # routines.py — ungated by design; needs a platform user identity, which
    # this Discord-shaped session carries.
    "create_routine": ExpectedOutcome(discoverable=True),
    # thread_participation.py — untagged on purpose: following a thread is a
    # member action, and the seeded prompt tells the agent to call it.
    "set_thread_participation": ExpectedOutcome(discoverable=True),
    "get_thread_participation": ExpectedOutcome(discoverable=True),
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
    "request_mcp_token": {
        "agent_name": "demo-agent",
        "server_name": "ctx7",
        "url": "https://ctx7.example/mcp",
        "channel_id": "channel-1",
    },
    "request_agent_key": {
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
    "set_thread_participation": {
        "mode": "on",
        "thread_id": "thread-1",
        "channel_id": "channel-1",
    },
    "get_thread_participation": {"thread_id": "thread-1", "channel_id": "channel-1"},
}


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
            200, json=ma_agent(name="demo-new-agent").model_dump(mode="json")
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _r, _m: httpx.Response(
            200, json=ma_agent(name="demo-new-agent").model_dump(mode="json")
        ),
    )
    return build_fake_anthropic(router.dispatch)


async def _seed_chat_turn_session(sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    """Seed a tenant + account shaped exactly like a Discord chat turn and
    return the daimon-mcp-vault-shaped JWT for it: account-scoped subject, no
    agent_id claim, chat_agent_id execution identity, no is_admin, default role, Discord platform
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
    return mint_jwt(
        account_id=account.id, secret=SECRET.encode(), now=_NOW, chat_agent_id=uuid.uuid4()
    )


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

    search_result = await mcp_session(
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
    is_discoverable = f"### {tool_name}" in search_text
    assert is_discoverable == expected.discoverable, (
        f"{tool_name}: expected discoverable={expected.discoverable}, got {is_discoverable}; "
        f"search_tools output: {search_text!r}"
    )
    if not expected.discoverable:
        return

    call_result = await mcp_session(
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
    discovers the eleven agent-chat tools plus the eight self-edit/vault tools
    tagged in this plan, and none of the tenant-wide roster tools."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="agent-chat-reachability")
        account = await make_account(s, tenant=tenant)
    token = mint_jwt(
        account_id=account.id,
        secret=SECRET.encode(),
        now=_NOW,
        agent_id=uuid.uuid4(),
        chat_agent_id=uuid.uuid4(),
    )
    app = _make_app(sessionmaker, _build_client())

    result = await mcp_session(app, token=token, method="tools/list")
    payload = result.get("result", result)
    tool_names = {t["name"] for t in payload.get("tools", [])}  # type: ignore[union-attr]

    expected_agent_chat_tools = {
        "ask",
        "deliver_turn_charts",
        "describe_agent",
        "list_my_sessions",
        "start_turn",
        "continue_turn",
        "get_my_session",
        "list_events",
        "archive_my_session",
        "cancel_turn",
        "get_turn_cost",
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


async def test_chat_execution_identity_preserves_exact_tools_list(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Execution identity must not bypass the search transform."""
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id="chat-list")
        account = await make_account(session, tenant=tenant)
    app = _make_app(sessionmaker, _build_client())
    plain_token = mint_jwt(account_id=account.id, secret=SECRET.encode(), now=_NOW)
    chat_token = mint_jwt(
        account_id=account.id, secret=SECRET.encode(), now=_NOW, chat_agent_id=uuid.uuid4()
    )
    plain = await mcp_session(app, token=plain_token, method="tools/list")
    chat = await mcp_session(app, token=chat_token, method="tools/list")
    # Compare serialized full schemas, not just tool names or callability.
    import json

    assert json.dumps(chat["result"], sort_keys=True) == json.dumps(plain["result"], sort_keys=True)
    assert {tool["name"] for tool in chat["result"]["tools"]} == {
        "call_tool",
        "search_tools",
        *APPROVAL_TOOLS,
    }
