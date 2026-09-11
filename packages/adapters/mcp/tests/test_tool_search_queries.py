"""Authored lexical regression cases through the real platform-scoped tool search.

These cases measure ranking, not conversational model behavior. Whether the
model chooses a target and gives the right handoff is operator QA on staging.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from aioresponses import aioresponses
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsModelConfig
from cryptography.fernet import Fernet
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    GeminiSettings,
    GithubSettings,
    McpSettings,
    NotebookSettings,
    Settings,
    SlackSettings,
)
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.scope import TenantScopeRef
from daimon.core.stores.credential_requests import peek_credential_request
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic, build_stub_anthropic, list_response
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from yarl import URL

from .factories import mcp_session


@dataclass(frozen=True)
class SearchCase:
    query: str
    top_hit: str | frozenset[str]
    co_surface: frozenset[str] = frozenset()


CASES = [
    SearchCase("give research-bot a Toggl key", "request_agent_key", frozenset([])),
    SearchCase(
        "add the Higgsfield API key so people can try it", "request_agent_key", frozenset([])
    ),
    SearchCase(
        "this platform just launched; give you its API key for everyone here",
        "request_agent_key",
        frozenset([]),
    ),
    SearchCase(
        "connect research-bot to our MCP endpoint with a token",
        "request_mcp_token",
        frozenset(["attach_mcp_server"]),
    ),
    SearchCase("load our .env into research-bot", "request_agent_key", frozenset([])),
    SearchCase("what keys does research-bot have", "list_agent_keys", frozenset(["get_agent"])),
    SearchCase("remove the old Toggl key from research-bot", "remove_agent_key", frozenset([])),
    SearchCase(
        "let research-bot read our private repo github.com/acme/data",
        "request_repo_binding",
        frozenset(["post_github_app_install_link"]),
    ),
    SearchCase(
        "install the GitHub app",
        "post_github_app_install_link",
        frozenset(["request_repo_binding"]),
    ),
    SearchCase(
        "install skills from github.com/acme/skills into research-bot",
        "sync_skills",
        frozenset(["request_skill_repo_token"]),
    ),
    SearchCase(
        "the skills repo is private",
        "request_skill_repo_token",
        frozenset(["request_repo_binding"]),
    ),
    SearchCase(
        "add the build-models skill to research-bot", "update_agent", frozenset(["remove_skill"])
    ),
    SearchCase(
        "stop research-bot using the eda skill", "remove_skill", frozenset(["delete_skill"])
    ),
    SearchCase(
        "delete the eda skill from the workspace", "delete_skill", frozenset(["remove_skill"])
    ),
    SearchCase(
        "connect research-bot to Linear", "request_mcp_token", frozenset(["attach_mcp_server"])
    ),
    SearchCase(
        "add the Context7 MCP server to research-bot",
        "attach_mcp_server",
        frozenset(["request_mcp_token"]),
    ),
    SearchCase("disconnect Linear from research-bot", "detach_mcp_server", frozenset([])),
    SearchCase(
        "make churn-explorer answer in #growth",
        "set_agent_default",
        frozenset(["clear_agent_default"]),
    ),
    SearchCase(
        "make research-bot the default for the whole server", "set_agent_default", frozenset([])
    ),
    SearchCase(
        "stop churn-explorer answering in #growth",
        "clear_agent_default",
        frozenset(["set_agent_default"]),
    ),
    SearchCase("who answers in #growth", "explain_agent_resolution", frozenset([])),
    SearchCase("make a copy of Daimon I can edit", "fork_agent", frozenset([])),
    SearchCase("create an agent called churn-explorer on Opus", "create_agent", frozenset([])),
    SearchCase("switch research-bot to Opus", "update_agent", frozenset([])),
    SearchCase("change research-bot's prompt", "update_agent", frozenset([])),
    SearchCase("what can research-bot access", "get_agent", frozenset(["list_agent_keys"])),
    SearchCase("delete churn-explorer", "archive_agent", frozenset([])),
]


def _make_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    role: str = "admin",
    tenant_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    client: AsyncAnthropic | None = None,
    crypto_keys: tuple[SecretStr, ...] = (),
) -> Starlette:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=HttpUrl("https://example.com/mcp")),
            discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
            slack=SlackSettings(signing_secret=SecretStr("test"), app_token=SecretStr("xapp-test")),
            github=GithubSettings(app_slug="test-app"),
            crypto=CryptoSettings(keys=crypto_keys),
            gemini=GeminiSettings(api_key=SecretStr("gemini-test")),
            notebook=NotebookSettings(
                host_url=HttpUrl("http://notebook.test:8001"), admin_secret=SecretStr("test")
            ),
        ),
        sessionmaker=sessionmaker,
        anthropic=client if client is not None else build_stub_anthropic(),
        auth=StaticTokenVerifier(
            tokens={
                "test-token": {
                    "sub": str(account_id or uuid.uuid4()),
                    "tenant_id": str(tenant_id or uuid.uuid4()),
                    "role": role,
                    "client_id": "test",
                    "platform": platform,
                    "external_id": "T_TEST" if platform == "slack" else "111",
                    "platform_user_id": "U_TEST" if platform == "slack" else "42",
                }
            }
        ),
    )


def _result_text(response: dict[str, object]) -> str:
    result = response.get("result", response)
    assert isinstance(result, dict), f"expected a tool result, got {response}"
    content = result.get("content", [])
    assert isinstance(content, list), f"expected content blocks, got {result}"
    return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))


@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.query)
async def test_setup_query_ranks_tool_and_siblings(
    sessionmaker: async_sessionmaker[AsyncSession],
    platform: str,
    case: SearchCase,
) -> None:
    app = _make_app(sessionmaker, platform=platform)
    response = await mcp_session(
        app,
        token="test-token",
        method="tools/call",
        params={
            "name": "search_tools",
            "arguments": {"query": case.query},
        },
    )
    hits = re.findall(r"^### (\w+)", _result_text(response), re.MULTILINE)
    expected = {case.top_hit} if isinstance(case.top_hit, str) else case.top_hit
    detail = (
        f"{case.query!r}: expected {expected}, co-surface {case.co_surface}; ordered hits={hits}"
    )
    assert hits and hits[0] in expected, detail
    assert case.co_surface <= set(hits), detail
    assert len(hits) <= 5, f"search window exceeded five: {detail}"


@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_member_cannot_search_or_call_routing_mutation(
    sessionmaker: async_sessionmaker[AsyncSession],
    platform: str,
) -> None:
    app = _make_app(sessionmaker, platform=platform, role="user")
    found = await mcp_session(
        app,
        token="test-token",
        method="tools/call",
        params={
            "name": "search_tools",
            "arguments": {"query": "make research-bot the channel default"},
        },
    )
    assert "### set_agent_default" not in _result_text(found), (
        "member search must hide routing mutation"
    )
    called = await mcp_session(
        app,
        token="test-token",
        method="tools/call",
        params={
            "name": "call_tool",
            "arguments": {"name": "set_agent_default", "arguments": {"agent_name": "research-bot"}},
        },
    )
    text = _result_text(called)
    assert "Unknown tool" in text or "not found" in text, (
        f"hidden routing tool must remain uncallable: {text}"
    )
    assert "/agent-setup" not in text and "DAIMON_" not in text, (
        "refusal must not expose retired paths or settings"
    )


@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_member_default_agent_edit_refuses_with_admin_handoff(
    sessionmaker: async_sessionmaker[AsyncSession],
    platform: str,
) -> None:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform=platform)
        account = await make_account(session, tenant=tenant)
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant.id),
            tenant_id=tenant.id,
            agent_name="research-bot",
            mode="agent",
        )
    agent = BetaManagedAgentsAgent(
        id="ag_research",
        name="research-bot",
        type="agent",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-5"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        tools=[],
        skills=[],
        mcp_servers=[],
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_name": "research-bot",
            "daimon_account": str(account.id),
        },
    )
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/v1/agents", (
            "refused edit must not write upstream"
        )
        return list_response([agent.model_dump(mode="json")])

    app = _make_app(
        sessionmaker,
        platform=platform,
        role="user",
        tenant_id=tenant.id,
        account_id=account.id,
        client=build_fake_anthropic(transport),
    )
    found = await mcp_session(
        app,
        token="test-token",
        method="tools/call",
        params={
            "name": "search_tools",
            "arguments": {"query": "change research-bot prompt"},
        },
    )
    assert "### update_agent" in _result_text(found), (
        "direct spec edit remains discoverable for members"
    )
    called = await mcp_session(
        app,
        token="test-token",
        method="tools/call",
        params={
            "name": "call_tool",
            "arguments": {
                "name": "update_agent",
                "arguments": {"name": "research-bot", "system": "a revised prompt"},
            },
        },
    )
    text = _result_text(called)
    assert "research-bot" in text and "admin" in text and "forking" in text, (
        f"handoff must preserve target and non-gated alternative: {text}"
    )
    assert "/agent-setup" not in text and "DAIMON_" not in text, (
        "refusal must not disclose deployment internals"
    )
    assert len(requests) == 1, "refusal must follow lookup without a mutation"


async def test_member_can_request_new_key_on_managed_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = SecretStr(Fernet.generate_key().decode())
    fernet = build_multifernet((key.get_secret_value(),))
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id="T_TEST")
        account = await make_account(session, tenant=tenant)
        await upsert_slack_bot_token(
            session, team_id="T_TEST", encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
    agent = BetaManagedAgentsAgent(
        id="ag_daimon",
        name="daimon",
        type="agent",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-5"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        tools=[],
        skills=[],
        mcp_servers=[],
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_name": "daimon",
            "daimon_managed": "true",
        },
    )

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/v1/agents", (
            "request should only resolve its target upstream"
        )
        return list_response([agent.model_dump(mode="json")])

    app = _make_app(
        committing_sessionmaker,
        platform="slack",
        role="user",
        tenant_id=tenant.id,
        account_id=account.id,
        client=build_fake_anthropic(transport),
        crypto_keys=(key,),
    )
    found = await mcp_session(
        app,
        token="test-token",
        method="tools/call",
        params={
            "name": "search_tools",
            "arguments": {"query": "give Daimon a new API key"},
        },
    )
    assert "### request_agent_key" in _result_text(found), "member must discover key enrollment"
    with aioresponses() as slack:
        slack.get(
            re.compile(r"https://slack\.com/api/conversations\.info.*"),
            payload={"ok": True, "channel": {"id": "C_TEST", "is_private": False}},
        )
        slack.get(
            re.compile(r"https://slack\.com/api/users\.info.*"),
            payload={"ok": True, "user": {"id": "U_TEST", "is_restricted": False}},
        )
        slack.post("https://slack.com/api/chat.postMessage", payload={"ok": True, "ts": "123.456"})
        called = await mcp_session(
            app,
            token="test-token",
            method="tools/call",
            params={
                "name": "call_tool",
                "arguments": {
                    "name": "request_agent_key",
                    "arguments": {
                        "agent_name": "daimon",
                        "key": "NEW_SERVICE_API_KEY",
                        "purpose": "use the new service",
                        "channel_id": "C_TEST",
                    },
                },
            },
        )
        text = _result_text(called)
        assert "123.456" in text and "NEW_SERVICE_API_KEY" in text, (
            f"member enrollment must actually post: {text}"
        )
        payload = slack.requests[("POST", URL("https://slack.com/api/chat.postMessage"))][0].kwargs[
            "json"
        ]
    token = next(block for block in payload["blocks"] if block["type"] == "actions")["elements"][0][
        "value"
    ]
    async with committing_sessionmaker() as session:
        row = await peek_credential_request(session, token=token)
    assert row is not None and row.target == "NEW_SERVICE_API_KEY", (
        "posted token must identify a persisted request"
    )
    assert row.tenant_id == tenant.id and row.requester_platform_user_id == "U_TEST", (
        "request must stay tenant- and requester-bound"
    )
