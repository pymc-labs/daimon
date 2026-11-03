"""`/api/agents/{id}/tool-rules`: an admin's agent-wide rules, with routine scope."""

from __future__ import annotations

import pytest
from daimon.adapters.web import access
from daimon.core.config import Settings
from daimon.core.stores import web_audit
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, agent_client, agent_people, make_admin, sign_in_token

_SERVERS = [{"type": "url", "name": "notes", "url": "https://notes.example.com/mcp"}]


def _settings(on: bool) -> Settings:
    db = {"url": "postgresql+asyncpg://u:p@h/db"}
    safety = {"enabled": on, "agent_rules": on}
    return Settings(_env_file=None, database=db, tool_safety=safety)  # pyright: ignore[reportCallIssue, reportArgumentType]


async def test_an_admin_sets_a_rule_for_chat_and_routines_and_clears_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session, mcp_servers=_SERVERS)
    await make_admin(db_session_factory, owner)
    token = await sign_in_token(db_session_factory, owner)
    body = {
        "tool": "notes/write",
        "rule": "ask",
        "scope": "chat_and_routines",
        "expected_revision": 0,
    }
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_settings(True)
    ) as client:
        made = await client.put("/api/agents/ag_w/tool-rules", json=body, headers=OWN)
        listed = await client.get("/api/agents/ag_w/tool-rules")
        stale = await client.put("/api/agents/ag_w/tool-rules", json=body, headers=OWN)
        cleared = await client.put(
            "/api/agents/ag_w/tool-rules",
            json={**body, "rule": None, "expected_revision": 1},
            headers=OWN,
        )
        after = await client.get("/api/agents/ag_w/tool-rules")
        bad = await client.put(
            "/api/agents/ag_w/tool-rules", json={**body, "tool": "other/write"}, headers=OWN
        )
    assert made.status_code == 200, made.text
    assert listed.json() == {
        "active": True,
        "rules": [
            {"tool": "notes/write", "rule": "ask", "scope": "chat_and_routines", "revision": 1}
        ],
    }
    assert stale.status_code == 409, "a rule read before another change is refused"
    assert cleared.status_code == 200 and after.json()["rules"][0]["rule"] is None
    assert bad.status_code == 422, "a connection the agent lacks is refused"
    async with db_session_factory() as session:
        actions = [
            r.action for r in await web_audit.list_recent(session, tenant_id=owner.tenant_id)
        ]
    assert actions.count("Changed tool rule") == 2, "each change is in the audit trail"


async def test_members_cannot_read_or_change_rules_and_off_means_no_changes(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session, mcp_servers=_SERVERS)
    body = {"tool": "notes/write", "rule": "deny", "scope": "chat", "expected_revision": 0}
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_settings(True)
    ) as client:
        read = await client.get("/api/agents/ag_w/tool-rules")
        write = await client.put("/api/agents/ag_w/tool-rules", json=body, headers=OWN)
    assert read.status_code == 403 and write.status_code == 403, "the owner is not an admin"
    await make_admin(db_session_factory, owner)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        off = await client.put("/api/agents/ag_w/tool-rules", json=body, headers=OWN)
        state = await client.get("/api/agents/ag_w/tool-rules")
    assert off.status_code == 422 and state.json()["active"] is False


async def test_a_rule_write_needs_the_edit_capability(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents, owner, _other = await agent_people(db_session, mcp_servers=_SERVERS)
    await make_admin(db_session_factory, owner)
    view_only = access._BY_ROLE["owner"] - {"agents.edit"}  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setitem(access._BY_ROLE, "owner", view_only)  # pyright: ignore[reportPrivateUsage]
    body = {"tool": "notes/write", "rule": "deny", "scope": "chat", "expected_revision": 0}
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_settings(True)
    ) as client:
        read = await client.get("/api/agents/ag_w/tool-rules")
        write = await client.put("/api/agents/ag_w/tool-rules", json=body, headers=OWN)
    assert read.status_code == 200, "the view capability reads"
    assert write.status_code == 403, "a write needs the edit capability"
