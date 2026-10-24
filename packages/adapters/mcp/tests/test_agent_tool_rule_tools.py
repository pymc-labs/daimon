"""DB-backed MCP tests for per-agent tool rules and human-only edits."""

from __future__ import annotations

import dataclasses
import uuid
from typing import cast

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _channel_target as channel_target
from daimon.adapters.mcp.tools.agent_tool_rules import (
    _clear,  # pyright: ignore[reportPrivateUsage]
    _list,  # pyright: ignore[reportPrivateUsage]
    _set,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_tool_rules as store
from daimon.core.stores.domain import Role, TenantRow
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CHANNEL = "222"
_OTHER = "333"
_TOOL = "remote/append_note"
_URL = "https://remote.example/mcp"
_PERSON_ONLY = (
    "Tool rules can only be changed by a person, from the CLI or the hub, "
    "not by an agent in a conversation."
)


@pytest.fixture(autouse=True)
def visible(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
        return channel_id

    monkeypatch.setattr(channel_target, "resolve_visible_channel", fake)


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], tenant: TenantRow) -> McpRuntime:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agents"
        agent = ma_agent(
            name="shared",
            tenant_id=tenant.id,
            mcp_servers=[{"name": "remote", "type": "url", "url": _URL}],
        ).model_dump(mode="json")
        return httpx.Response(200, json={"data": [agent], "next_page": None})

    return McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(handler),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        deployment_default=DeploymentDefault(agent_name="shared"),
    )


async def _seed(factory: async_sessionmaker[AsyncSession]) -> tuple[TenantRow, uuid.UUID]:
    async with factory.begin() as session:
        tenant = await make_tenant(session)
        return tenant, (await make_account(session, tenant=tenant)).id


def _auth(tenant: TenantRow, account_id: uuid.UUID, *, admin: bool) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant.id,
        role=Role.ADMIN if admin else Role.USER,
        platform="discord",
        is_admin=admin,
    )


@pytest.mark.asyncio
async def test_workspace_admin_sets_lists_and_clears(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant)
    admin = _auth(tenant, account_id, admin=True)

    result = await _set(runtime, admin, "shared", _TOOL, "allow")
    row = cast(store.AgentToolRule, result["rule"])
    assert (row.channel_id, row.rule, row.server_url, row.revision) == (None, "allow", _URL, 1)
    listed = cast(list[store.AgentToolRule], (await _list(runtime, admin, "shared"))["rules"])
    assert [(item.tool_key, item.rule) for item in listed] == [(_TOOL, "allow")]

    cleared = cast(store.AgentToolRule, (await _clear(runtime, admin, "shared", _TOOL))["rule"])
    assert cleared.rule is None and cleared.revision == 2
    assert (
        cast(list[store.AgentToolRule], (await _list(runtime, admin, "shared"))["rules"])[0].rule
        is None
    )


@pytest.mark.asyncio
async def test_agent_credentials_cannot_set_or_clear_even_with_admin_signal(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant)
    admin = _auth(tenant, account_id, admin=True)
    for agent_auth in (
        dataclasses.replace(admin, agent_id=uuid.uuid4()),
        dataclasses.replace(admin, chat_agent_id=uuid.uuid4()),
    ):
        with pytest.raises(
            ToolError, match="Tool rules can only be changed by a person"
        ) as set_err:
            await _set(runtime, agent_auth, "shared", _TOOL, "allow")
        assert str(set_err.value) == _PERSON_ONLY
        with pytest.raises(
            ToolError, match="Tool rules can only be changed by a person"
        ) as clear_err:
            await _clear(runtime, agent_auth, "shared", _TOOL)
        assert str(clear_err.value) == _PERSON_ONLY
    chat_agent = dataclasses.replace(admin, chat_agent_id=uuid.uuid4())
    assert (await _list(runtime, chat_agent, "shared"))["rules"] == []


@pytest.mark.asyncio
async def test_missing_mcp_server_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant)
    with pytest.raises(ToolError, match="has no connection named"):
        await _set(
            runtime, _auth(tenant, account_id, admin=True), "shared", "missing/append_note", "allow"
        )
