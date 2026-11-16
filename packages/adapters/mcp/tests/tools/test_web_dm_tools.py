"""Chat's DM settings: `change_workspace_settings` and `set_agent_web_access` write through
the core helper the web app uses, with the same audit rows."""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import web_people as people_tool
from daimon.adapters.mcp.tools import web_workspace_settings as settings_tool
from daimon.adapters.mcp.tools.web_people import AgentDmAccess
from daimon.adapters.mcp.tools.web_workspace_settings import DmRule, WorkspaceSettingsChanged
from daimon.core.stores import dm_access as dm_store
from daimon.core.stores import web_audit, web_people, web_roles
from daimon.core.stores.direct_messages import get_dm_agent_name
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .parity_fakes import fake_runtime, make_auth

AGENT = "ada"


@dataclass
class Chat:
    runtime: McpRuntime
    auth: AuthIdentity
    agent_id: str
    factory: async_sessionmaker[AsyncSession]

    async def change(self, **dm: Any) -> WorkspaceSettingsChanged:
        return await settings_tool._change_impl(  # pyright: ignore[reportPrivateUsage]
            self.runtime,
            self.auth,
            modules=None,
            default_agent=None,
            everyone_invokes=None,
            invoker_people=[],
            dm_enabled=None,
            dm_memory_read_only=None,
            origin_context_id="ctx",
            **dm,
        )

    async def trail(self) -> list[tuple[str, str, str | None]]:
        async with self.factory() as db:
            rows = await web_audit.list_recent(db, tenant_id=self.auth.tenant_id)
        return [(r.action, r.target, r.detail) for r in reversed(rows)]


@pytest.fixture
async def chat(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> Chat:
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, workspace_id=f"g-{uuid.uuid4()}")
        account = await make_account(db, tenant=tenant)
    agent = ma_agent(name=AGENT, tenant_id=tenant.id)

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def agents(*_a: Any, **_k: Any) -> list[Any]:
        return [agent]

    async def names(*_a: Any, **_k: Any) -> dict[str, str]:
        return {agent.id: agent.name}

    monkeypatch.setattr(web_roles, "get_workspace_role", nothing)
    monkeypatch.setattr(web_people, "get_person_for_account", nothing)
    monkeypatch.setattr(settings_tool, "agent_names", names)
    for module in (people_tool, settings_tool):
        monkeypatch.setattr(module, "require_card", nothing)
        monkeypatch.setattr(module, "list_agents_by_tenant", agents)
    runtime = cast(
        "McpRuntime",
        SimpleNamespace(
            session_factory=db_session_factory, client=object(), settings=fake_runtime().settings
        ),
    )
    auth = dataclasses.replace(make_auth(), tenant_id=tenant.id, account_id=account.id)
    return Chat(runtime, auth, agent.id, db_session_factory)


async def test_change_workspace_settings_when_dm_given_then_default_and_rules_change_once(
    chat: Chat,
) -> None:
    rules = [
        DmRule(subject="group", subject_id="r-1", agent=AGENT, locked=True),
        DmRule(subject="user", subject_id="u-1"),
    ]
    first = await chat.change(dm_default_agent=AGENT, dm_rules=rules)
    again = await chat.change(dm_default_agent=AGENT, dm_rules=rules)
    cleared = await chat.change(dm_default_agent="", dm_rules=[])
    async with chat.factory() as db:
        default = await get_dm_agent_name(db, tenant_id=chat.auth.tenant_id)
        stored = await dm_store.list_rules(db, tenant_id=chat.auth.tenant_id)

    assert [(c.setting, c.value) for c in first.changes] == [
        ("DM default agent", AGENT),
        ("DM rules", "2 rules"),
    ], first
    assert again.changes == [], "a repeat changes nothing"
    assert [(c.setting, c.value) for c in cleared.changes] == [
        ("DM default agent", "None"),
        ("DM rules", "0 rules"),
    ], cleared
    assert (default, stored) == (None, []), "an empty name and list clear both"
    assert sorted(await chat.trail()) == [
        ("Changed DM default agent", "Direct messages", "None"),
        ("Changed DM default agent", "Direct messages", AGENT),
        ("Changed DM rules", "Direct messages", "0 rules"),
        ("Changed DM rules", "Direct messages", "2 rules"),
    ], "one row per change, as the web app writes them"


async def test_change_workspace_settings_when_dm_agent_unknown_then_refused_and_unchanged(
    chat: Chat,
) -> None:
    with pytest.raises(ToolError, match="no agent called nobody"):
        await chat.change(dm_rules=[DmRule(subject="everyone", agent="nobody")])
    with pytest.raises(ToolError, match="locked rule needs an agent"):
        await chat.change(dm_rules=[DmRule(subject="everyone", locked=True)])
    async with chat.factory() as db:
        stored = await dm_store.list_rules(db, tenant_id=chat.auth.tenant_id)

    assert stored == [], "a refused list writes no rule"
    assert await chat.trail() == [], "a refused change leaves no audit row"


async def test_set_agent_web_access_when_dm_given_then_grant_is_set_and_listed(
    chat: Chat,
) -> None:
    dm = AgentDmAccess("listed", ["u-1", "u-1"], ["r-1"])
    changed = await people_tool._access_set_impl(  # pyright: ignore[reportPrivateUsage]
        chat.runtime, chat.auth, chat.agent_id, None, None, None, "ctx", dm
    )
    listed = await people_tool._access_list_impl(chat.runtime, chat.auth)  # pyright: ignore[reportPrivateUsage]

    assert changed.dm == AgentDmAccess("listed", ["u-1"], ["r-1"]), changed
    assert [a.dm for a in listed.agents] == [changed.dm], "the list shows the same grant"
    assert await chat.trail() == [("Changed DM access", AGENT, "Listed: 1 person, 1 group")], (
        "one row naming the agent and counts, never a person"
    )
