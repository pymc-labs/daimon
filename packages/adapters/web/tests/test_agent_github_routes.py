"""`/api/agents/{id}/github`: the panel read and its owner's actions."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

import httpx
from cryptography.fernet import Fernet
from daimon.core.config import Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import github_access, github_push_resync, security_audit
from daimon.core.stores.user_skills import upsert_user_skill
from daimon.testing.factories import make_github_app_installation, make_tenant_github_repo
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import (
    NOW,
    OWN,
    agent_client,
    agent_people,
    make_admin,
    sign_in_token,
)

ASK = "Ask an admin or the agent's owner."
URL = "https://github.com/acme/skills"
SKILL = {"type": "custom", "skill_id": "skill_repo", "version": "latest"}


def _configured() -> Settings:
    """Settings with the full GitHub App connect flow configured."""
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        github_app={  # pyright: ignore[reportArgumentType]
            "app_id": "1",
            "app_slug": "daimon-test",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
            "client_id": "c",
            "client_secret": "s",
        },
        mcp={"public_url": "https://app.example.com/mcp"},  # pyright: ignore[reportArgumentType]
        crypto={"keys": [Fernet.generate_key().decode()]},  # pyright: ignore[reportArgumentType]
    )


async def _repos(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, granter: uuid.UUID
) -> None:
    """Grant acme/app and acme/docs to the agent, each connected for it alone."""
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_w")
    names = {101: "acme/docs", 102: "acme/app"}
    install = 7000 + uuid.uuid4().int % 1000
    async with factory.begin() as db:
        await make_github_app_installation(
            db,
            installation_id=install,
            account_login="acme",
            repo_full_names=list(names.values()),
            github_repo_full_names=list(names.values()),
        )
        for repo_id, name in names.items():
            await make_tenant_github_repo(
                db,
                tenant_id=tenant_id,
                repo_id=repo_id,
                full_name=name,
                installation_id=install,
                scope_agent_id=agent_id,
                authorized_by_account_id=granter,
            )
            await github_access.stage_grant(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=repo_id,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=granter,
            )


async def _repo_skill(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    """Record the attached skill as synced from acme/skills on main."""
    async with factory.begin() as db:
        await upsert_user_skill(
            db,
            tenant_id=tenant_id,
            principal_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_w"),
            agent_name="helper",
            name="deploy",
            source_repo_url=URL,
            source_repo_branch="main",
            source_path="skills",
            content_hash="h1",
            anthropic_id="skill_repo",
            anthropic_latest_version="1",
        )


async def _tools(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> set[str]:
    async with factory() as db:
        return {e.tool_name for e in await security_audit.list_recent(db, tenant_id=tenant_id)}


async def test_get_panel_when_owner_then_lists_repos_skills_and_release(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An owner sees the working repo first, the repo skill's source and the release."""
    agents, owner, _other = await agent_people(
        db_session,
        skills=[SKILL],
    )
    agents.agent = agents.agent.model_copy(
        update={
            "metadata": {
                **agents.agent.metadata,
                "source_release": "v1.2",
                "source_release_at": "2030-01-10T09:00:00+00:00",
                "source_release_url": "https://example.com/releases/v1.2",
            }
        }
    )
    await _repos(db_session_factory, owner.tenant_id, owner.account_id)
    await _repo_skill(db_session_factory, owner.tenant_id)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        working = await client.put(
            "/api/agents/ag_w/github/working", json={"repo": "acme/docs"}, headers=OWN
        )
        got = await client.get("/api/agents/ag_w/github")
    assert working.status_code == 200, working.text
    body: dict[str, Any] = got.json()
    assert got.status_code == 200, got.text
    assert body["configured"] is False and body["can_manage"] is True, "owner manages"
    assert [r["name"] for r in body["repos"]] == ["acme/docs", "acme/app"], "working first"
    assert body["repos"][0]["working"] and body["repos"][0]["installation"] == "active"
    [skill] = body["skills"]
    assert (skill["origin"], skill["repo"], skill["branch"]) == ("repo", "acme/skills", "main")
    assert body["release"]["name"] == "v1.2", "the stamped release should show"
    assert body["sync"] is None, "no push job means no sync state"


async def test_get_panel_when_skills_listing_fails_then_still_answers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A failing provider skill listing degrades to ids, never a 500."""
    agents, owner, _other = await agent_people(db_session, skills=[SKILL])
    # Ahead of the listing route Agents serves: the first match answers.
    agents.router.routes.insert(0, ("GET", re.compile(r"/v1/skills"), lambda _r, _m: _fail()))
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        got = await client.get("/api/agents/ag_w/github")
    assert got.status_code == 200, got.text
    assert [s["title"] for s in got.json()["skills"]] == ["skill_repo"], "falls back to id"


def _fail() -> httpx.Response:
    return httpx.Response(400, json={"type": "error", "error": {"type": "api_error"}})


async def test_get_panel_when_stranger_or_unknown_then_403_or_404(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A member who does not own the agent is refused; a missing agent is not found."""
    agents, owner, other = await agent_people(db_session)
    agents.router.add(
        "GET",
        r"/v1/agents/ag_missing$",
        lambda _r, _m: httpx.Response(
            404, json={"type": "error", "error": {"type": "not_found_error"}}
        ),
    )
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        refused = await client.get("/api/agents/ag_w/github")
        connect = await client.post("/api/agents/ag_w/github/connect", headers=OWN)
        missing = await client.get("/api/agents/ag_missing/github")
    assert refused.status_code == 403 and ASK in refused.text, "a stranger is refused"
    assert connect.status_code == 403, "a stranger cannot connect"
    assert missing.status_code == 404, missing.text


async def test_get_panel_when_admin_then_manages(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A workspace admin who does not own the agent may manage it."""
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        got = await client.get("/api/agents/ag_w/github")
    assert got.status_code == 200 and got.json()["can_manage"] is True, got.text


async def test_connect_when_unconfigured_then_409_and_when_configured_then_link(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Connect refuses without the App setup and mints an audited link with it."""
    agents, owner, _other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        refused = await client.post("/api/agents/ag_w/github/connect", headers=OWN)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_configured()
    ) as client:
        minted = await client.post("/api/agents/ag_w/github/connect", headers=OWN)
        panel = await client.get("/api/agents/ag_w/github")
    assert refused.status_code == 409, refused.text
    assert minted.status_code == 200, minted.text
    assert minted.json()["url"].startswith("https://app.example.com/oauth/github/connect/")
    assert panel.json()["configured"] is True, "a full App setup reads as configured"
    assert "github_connect" in await _tools(db_session_factory, owner.tenant_id), "audited"


async def test_working_and_remove_when_owner_then_returns_panel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Setting, clearing and removing repos return the panel; bad names are refused."""
    agents, owner, _other = await agent_people(db_session)
    await _repos(db_session_factory, owner.tenant_id, owner.account_id)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        put = await client.put(
            "/api/agents/ag_w/github/working", json={"repo": "acme/app"}, headers=OWN
        )
        cleared = await client.put(
            "/api/agents/ag_w/github/working", json={"repo": None}, headers=OWN
        )
        unknown = await client.put(
            "/api/agents/ag_w/github/working", json={"repo": "acme/none"}, headers=OWN
        )
        bad = await client.put("/api/agents/ag_w/github/working", json={"repo": "x"}, headers=OWN)
        removed = await client.delete("/api/agents/ag_w/github/repos/acme/app", headers=OWN)
        again = await client.delete("/api/agents/ag_w/github/repos/acme/app", headers=OWN)
    assert put.json()["repos"][0]["name"] == "acme/app", "the working repo leads"
    assert not any(r["working"] for r in cleared.json()["repos"]), "null clears it"
    assert (unknown.status_code, bad.status_code) == (422, 422), "unknown or bad name"
    assert [r["name"] for r in removed.json()["repos"]] == ["acme/docs"], "only that repo goes"
    assert again.status_code == 404, "a repo no longer listed is not found"
    assert "set_working_repo" in await _tools(db_session_factory, owner.tenant_id), "audited"


async def test_actions_when_not_manager_then_403(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A member who does not own the agent can change nothing."""
    agents, owner, other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_configured()
    ) as client:
        replies = [
            await client.put("/api/agents/ag_w/github/working", json={"repo": None}, headers=OWN),
            await client.delete("/api/agents/ag_w/github/repos/acme/app", headers=OWN),
            await client.post("/api/agents/ag_w/github/resync", headers=OWN),
        ]
    assert [r.status_code for r in replies] == [403, 403, 403], [r.text for r in replies]


async def test_resync_when_unconfigured_running_or_no_grant_then_refuses_or_skips(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Resync needs the App, refuses while a push sync runs and skips a repo without a grant."""
    agents, owner, _other = await agent_people(db_session, skills=[SKILL])
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        unconfigured = await client.post("/api/agents/ag_w/github/resync", headers=OWN)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_configured()
    ) as client:
        empty = await client.post("/api/agents/ag_w/github/resync", headers=OWN)
    await _repo_skill(db_session_factory, owner.tenant_id)
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_configured()
    ) as client:
        skipped = await client.post("/api/agents/ag_w/github/resync", headers=OWN)
    async with db_session_factory.begin() as db:
        await github_push_resync.enqueue(
            db, repo_full_name="acme/skills", ref="refs/heads/main", delivery_id="d1"
        )
        claimed = await github_push_resync.claim_due(
            db, lease_owner=uuid.uuid4(), lease_for=timedelta(minutes=5), now=NOW
        )
    assert claimed is not None, "the push job should be claimed"
    async with agent_client(
        agents, owner.tenant_id, db_session_factory, token, settings=_configured()
    ) as client:
        busy = await client.post("/api/agents/ag_w/github/resync", headers=OWN)
        panel = await client.get("/api/agents/ag_w/github")
    assert unconfigured.status_code == 409, unconfigured.text
    assert empty.json() == {"results": []}, "no repo skills means nothing to sync"
    assert skipped.json()["results"] == [
        {
            "name": "acme/skills",
            "action": "skipped",
            "detail": "This agent has no access to the repo.",
        }
    ], skipped.text
    assert busy.status_code == 409, "a running push sync blocks a manual one"
    assert panel.json()["sync"]["state"] == "running", "the panel shows the running sync"
    assert "github_resync" in await _tools(db_session_factory, owner.tenant_id), "audited"
