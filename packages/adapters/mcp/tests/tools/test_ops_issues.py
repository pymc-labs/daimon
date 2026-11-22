"""The ops tools (`get_ops_overview`, `list_issues`, `get_issue`) through the real token path.

Each call carries a minted operator token, so the scope gate, the admin gate and the
workspace the token names are the production ones.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.ops_issues import register_ops_issues_tools
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.mcp_auth import mint_operator_mcp_token
from daimon.core.membership import Membership
from daimon.core.operator_tokens import OperatorScope
from daimon.core.ops_catalog import OpsCode
from daimon.core.ops_events import OpsEvent, record_of
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import ops_events as store
from daimon.testing.asgi import call_mcp_tool
from fastmcp import FastMCP
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import production_identity_middleware, seed_server_admin

SECRET = b"a" * 32


@pytest.fixture(autouse=True)
def live_admin(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The platform's answer to "is this person an admin?"; every test starts with yes."""
    resolve = AsyncMock(return_value=Membership(is_member=True, is_admin=True))
    monkeypatch.setattr(DiscordRestMembershipResolver, "resolve", resolve)
    return resolve


def _app(sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    client = MagicMock(spec=AsyncAnthropic)
    client.api_key = "sk-test"
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=client,  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr(SECRET.decode()), public_url=HttpUrl("https://x/mcp")
            ),
        ),
        deployment_default=DeploymentDefault(environment_name="default-env"),
    )
    mcp = FastMCP(name="ops", auth=DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker))
    settings = MagicMock(discord=DiscordSettings(bot_token=SecretStr("test")))
    mcp.add_middleware(production_identity_middleware(sessionmaker, settings=settings))
    register_ops_issues_tools(mcp, runtime)
    return mcp.http_app()


async def _workspace(
    sessionmaker: async_sessionmaker[AsyncSession],
    *scopes: OperatorScope,
    code: OpsCode = "turn.failed",
) -> tuple[str, str]:
    """An admin's operator token and the fingerprint of the workspace's one issue."""
    async with sessionmaker.begin() as session:
        tenant_id, account_id = await seed_server_admin(session)
        record = record_of(
            OpsEvent(code, tenant_id, reason="timeout", agent_id="agent-1", platform="discord"),
            at=datetime.now(UTC),
        )
        await store.record_ops_events(session, tenant_id=tenant_id, events=[record])
        token = await mint_operator_mcp_token(
            session,
            account_id=account_id,
            tenant_id=tenant_id,
            scopes=frozenset(scopes),
            label=None,
            secret=SECRET,
            now=datetime.now(UTC),
            ttl_days=1,
        )
    return token, record.fingerprint


async def _call(
    sessionmaker: async_sessionmaker[AsyncSession],
    token: str,
    name: str,
    **arguments: object,
) -> tuple[bool, dict[str, Any]]:
    """Whether the tool refused, and what it returned (its text when it refused)."""
    reply = await call_mcp_tool(_app(sessionmaker), token=token, name=name, arguments=arguments)
    result: dict[str, Any] = reply["result"]  # type: ignore[assignment]
    if result.get("isError"):
        return True, {"text": result["content"][0]["text"]}
    return False, result["structuredContent"]


async def test_an_admin_with_the_scope_reads_the_overview_the_list_and_one_issue(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, fingerprint = await _workspace(committing_sessionmaker, "issues:read")

    refused, overview = await _call(committing_sessionmaker, token, "get_ops_overview")
    assert not refused and [i["fingerprint"] for i in overview["attention"]] == [fingerprint], (
        "the overview lists the issue that needs attention"
    )
    refused, listed = await _call(committing_sessionmaker, token, "list_issues", severity="error")
    assert not refused and [i["fingerprint"] for i in listed["issues"]] == [fingerprint]
    refused, none = await _call(committing_sessionmaker, token, "list_issues", area="dm")
    assert not refused and none["issues"] == [], "a filter that matches nothing returns nothing"
    refused, one = await _call(
        committing_sessionmaker, token, "get_issue", fingerprint=fingerprint, events_limit=5
    )
    assert not refused and one["issue"]["code"] == "turn.failed" and len(one["events"]) == 1


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_ops_overview", {}),
        ("list_issues", {}),
        ("get_issue", {"fingerprint": "anything"}),
    ],
)
async def test_a_token_without_the_issues_scope_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    tool: str,
    arguments: dict[str, object],
) -> None:
    token, _ = await _workspace(committing_sessionmaker, "tenant:read")

    refused, body = await _call(committing_sessionmaker, token, tool, **arguments)

    assert refused and list(body) == ["text"], "the call is refused and returns no data"


async def test_a_token_whose_person_is_not_an_admin_reads_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession], live_admin: AsyncMock
) -> None:
    token, _ = await _workspace(committing_sessionmaker, "issues:read")
    live_admin.return_value = Membership(is_member=True, is_admin=False)

    for tool in ("get_ops_overview", "list_issues"):
        refused, body = await _call(committing_sessionmaker, token, tool)
        assert refused and "requires a server admin" in body["text"], tool


async def test_a_workspace_never_sees_another_workspaces_issues(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    mine, _ = await _workspace(committing_sessionmaker, "issues:read")
    _, theirs = await _workspace(committing_sessionmaker, "issues:read", code="dm.failed")

    _, listed = await _call(committing_sessionmaker, mine, "list_issues")
    refused, body = await _call(committing_sessionmaker, mine, "get_issue", fingerprint=theirs)

    assert [i["code"] for i in listed["issues"]] == ["turn.failed"], "only its own issue is listed"
    assert refused and "No such issue" in body["text"], "another workspace's issue is not found"
    assert json.dumps(body).count(theirs) == 0, "the refusal does not echo or reveal it"
