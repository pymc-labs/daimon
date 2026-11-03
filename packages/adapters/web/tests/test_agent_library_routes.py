"""`/api/agents/skills` and `/api/agents/environments`: an admin's skill library and sandboxes."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsCustomSkill
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.scope import ChannelScopeRef, TenantScopeRef
from daimon.core.stores import web_audit
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.ma_models import ma_environment
from mux.testing.drivers.anthropic.ma import json_body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, Agents, agent_client, agent_people, make_admin, sign_in_token


def _skill(skill_id: str, title: str) -> dict[str, Any]:
    return {
        "id": skill_id,
        "type": "skill",
        "display_title": title,
        "latest_version": "7",
        "source": "custom",
        "created_at": "2030-01-01T00:00:00Z",
        "updated_at": "2030-01-01T00:00:00Z",
    }


def _library(agents: Agents, tenant: str) -> list[str]:
    """Two own skills and another workspace's; deletes are recorded."""
    agents.skills = [
        _skill("skill_notes", f"{tenant[:8]}-notes"),
        _skill("skill_spare", f"{tenant[:8]}-spare"),
        _skill("skill_theirs", "deadbeef-notes"),
    ]
    deleted: list[str] = []

    def versions(_r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "sv_1",
                        "type": "skill_version",
                        "skill_id": "skill_spare",
                        "version": "7",
                        "name": "spare",
                        "description": "",
                        "directory": "spare",
                        "created_at": "2030-01-02T00:00:00Z",
                    }
                ],
                "has_more": False,
                "next_page": None,
            },
        )

    def drop(_r: httpx.Request, m: re.Match[str]) -> httpx.Response:
        deleted.append(m.group(1))
        return httpx.Response(200, json={"id": m.group(1), "type": "skill_deleted"})

    agents.router.add("GET", r"/v1/skills/skill_spare/versions$", versions)
    agents.router.add("DELETE", r"/v1/skills/skill_spare/versions/(7)$", drop)
    agents.router.add("DELETE", r"/v1/skills/(skill_spare)$", drop)
    return deleted


async def test_an_admin_sees_the_library_with_what_uses_each_skill(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    _library(agents, str(owner.tenant_id))
    agents.agent = agents.agent.model_copy(
        update={
            "skills": [
                BetaManagedAgentsCustomSkill(
                    type="custom", skill_id="skill_notes", version="latest"
                )
            ]
        }
    )
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        res = await client.get("/api/agents/skills")
        versions = await client.get("/api/agents/skills/skill_spare/versions")
        foreign = await client.get("/api/agents/skills/skill_theirs/versions")
    assert res.status_code == 200, res.text
    assert [(s["name"], s["title"], s["used_by"]) for s in res.json()] == [
        (
            "notes",
            "Notes",
            [{"kind": "agent", "id": "ag_w", "name": "helper", "version": None}],
        ),
        ("spare", "Spare", []),
    ], "another workspace's skill is left out; a latest pin has no version"
    assert [v["version"] for v in versions.json()] == ["7"]
    assert foreign.status_code == 404


async def test_an_owner_who_is_not_an_admin_cannot_open_the_library(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await agent_people(db_session)
    token = await sign_in_token(db_session_factory, owner)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        codes = [
            (await client.get("/api/agents/skills")).status_code,
            (await client.get("/api/agents/environments")).status_code,
            (await client.delete("/api/agents/skills/skill_spare", headers=OWN)).status_code,
            (
                await client.post(
                    "/api/agents/skills/sync",
                    json={"url": "https://github.com/example/skills"},
                    headers=OWN,
                )
            ).status_code,
        ]
    assert codes == [403, 403, 403, 403]


async def test_a_skill_in_use_is_not_deleted_and_a_spare_one_is(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    deleted = _library(agents, str(owner.tenant_id))
    agents.agent = agents.agent.model_copy(
        update={
            "skills": [
                BetaManagedAgentsCustomSkill(
                    type="custom", skill_id="skill_notes", version="latest"
                )
            ]
        }
    )
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        held = await client.delete("/api/agents/skills/skill_notes", headers=OWN)
        gone = await client.delete("/api/agents/skills/skill_spare", headers=OWN)
    assert held.status_code == 409 and "helper" in held.json()["detail"], "it names who uses it"
    assert gone.status_code == 204, gone.text
    assert deleted == ["7", "skill_spare"], "its versions and then the skill go"
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
    assert [(r.action, r.target) for r in rows] == [("Deleted skill", "spare")]


async def test_the_skill_routes_refuse_a_non_admin_and_a_stale_save(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    _library(agents, str(owner.tenant_id))
    token = await sign_in_token(db_session_factory, owner)
    body = {"files": {"SKILL.md": "x"}, "expected_version": "7"}
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        plain = [
            (await client.get("/api/agents/skills/skill_notes")).status_code,
            (await client.get("/api/agents/skills/skill_notes/files")).status_code,
            (
                await client.put("/api/agents/skills/skill_notes/files", json=body, headers=OWN)
            ).status_code,
            (
                await client.post(
                    "/api/agents/skills/skill_notes/restore", json={"version": "7"}, headers=OWN
                )
            ).status_code,
            (
                await client.post(
                    "/api/agents/skills", json={"name": "Plots", "description": "x"}, headers=OWN
                )
            ).status_code,
        ]
    assert plain == [403] * 5, "the skill page and its writes need an admin"
    await make_admin(db_session_factory, other)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        stale = await client.put(
            "/api/agents/skills/skill_notes/files",
            json={"files": {"SKILL.md": "x"}, "expected_version": "6"},
            headers=OWN,
        )
    assert stale.status_code == 409, "a save against an older version is a conflict"


def _environments(agents: Agents, tenant: Any, *envs: Any) -> list[dict[str, Any]]:
    agents.router.add(
        "GET",
        r"/v1/environments$",
        lambda _r, _m: httpx.Response(
            200,
            json={
                "data": [e.model_dump(mode="json") for e in envs],
                "has_more": False,
                "next_page": None,
            },
        ),
    )
    patched: list[dict[str, Any]] = []

    def patch(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        patched.append(json_body(req))
        env = next(e for e in envs if e.id == m.group(1))
        return httpx.Response(200, json=env.model_dump(mode="json"))

    agents.router.add("POST", r"/v1/environments/(env_[a-z]+)$", patch)
    return patched


async def test_an_admin_lists_and_changes_an_environment(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    mine = ma_environment(id="env_mine", name="Data", tenant_id=owner.tenant_id)
    seeded = ma_environment(
        id="env_seed",
        name="Builtin",
        tenant_id=owner.tenant_id,
        metadata={"daimon_managed": "true"},
    )
    patched = _environments(agents, owner.tenant_id, mine, seeded)
    async with db_session_factory() as session, session.begin():
        for scope in (
            TenantScopeRef(tenant_id=owner.tenant_id),
            ChannelScopeRef(tenant_id=owner.tenant_id, channel_id="c1"),
        ):
            await set_fields(
                session, scope=scope, tenant_id=owner.tenant_id, environment_name="Data"
            )
    token = await sign_in_token(db_session_factory, other)
    spec = {"name": "Data", "packages": {"pip": ["numpy"]}, "network": "limited"}
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        listed = await client.get("/api/agents/environments")
        saved = await client.put(
            "/api/agents/environments/env_mine", json={"spec": spec}, headers=OWN
        )
        locked = await client.put(
            "/api/agents/environments/env_seed", json={"spec": spec}, headers=OWN
        )
    assert [(e["name"], e["managed"]) for e in listed.json()] == [
        ("Builtin", True),
        ("Data", False),
    ]
    uses = {e["name"]: {(u["kind"], u["id"]) for u in e["used_by"]} for e in listed.json()}
    assert uses == {"Builtin": set(), "Data": {("workspace", "workspace"), ("channel", "c1")}}
    assert saved.status_code == 200, saved.text
    assert patched[0]["config"]["packages"]["pip"] == ["numpy"]
    assert patched[0]["config"]["packages"]["npm"] == [], "a removed package goes"
    assert locked.status_code == 403 and len(patched) == 1, "a managed environment is not changed"
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
    assert [(r.action, r.target) for r in rows] == [("Changed environment", "Data")]


async def test_a_stale_environment_form_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    mine = ma_environment(
        id="env_mine",
        name="Data",
        tenant_id=owner.tenant_id,
        updated_at="2030-01-02T00:00:00Z",
    )
    patched = _environments(agents, owner.tenant_id, mine)
    token = await sign_in_token(db_session_factory, other)
    spec = {"name": "Data", "network": "limited"}
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        listed = (await client.get("/api/agents/environments")).json()
        stale = await client.put(
            "/api/agents/environments/env_mine",
            json={"spec": spec, "expected_updated_at": "2030-01-01T00:00:00Z"},
            headers=OWN,
        )
        current = await client.put(
            "/api/agents/environments/env_mine",
            json={"spec": spec, "expected_updated_at": listed[0]["updated_at"]},
            headers=OWN,
        )
    assert stale.status_code == 409 and len(patched) == 1, (
        "a changed environment is not overwritten"
    )
    assert current.status_code == 200, current.text


async def test_an_environment_name_already_taken_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    _environments(
        agents,
        owner.tenant_id,
        ma_environment(id="env_mine", name="Data", tenant_id=owner.tenant_id),
    )
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        res = await client.post("/api/agents/environments", json={"name": "Data"}, headers=OWN)
        bad = await client.post(
            "/api/agents/environments",
            json={"name": "Fresh", "allowed_hosts": ["not a host"]},
            headers=OWN,
        )
    assert res.status_code == 409
    assert bad.status_code == 422


async def test_a_sync_reports_each_skill_and_is_audited(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents, owner, other = await agent_people(db_session)
    await make_admin(db_session_factory, other)
    seen: dict[str, Any] = {}

    async def fake_sync(*_a: Any, **kw: Any) -> list[ResourceOutcome]:
        seen.update(kw)
        return [
            ResourceOutcome(kind="skill", name="eda", action=Action.CREATED, anthropic_id="s1"),
            ResourceOutcome(kind="skill", name="plots", action=Action.UPDATED, anthropic_id="s2"),
            ResourceOutcome(kind="skill", name="bad", action=Action.FAILED, error="No SKILL.md"),
        ]

    monkeypatch.setattr("daimon.core.skill_library.run_skill_sync", fake_sync)
    token = await sign_in_token(db_session_factory, other)
    async with agent_client(agents, owner.tenant_id, db_session_factory, token) as client:
        res = await client.post(
            "/api/agents/skills/sync",
            json={"url": "https://github.com/example/skills", "path": "/lib/"},
            headers=OWN,
        )
    assert res.status_code == 200, res.text
    assert [(s["name"], s["action"]) for s in res.json()] == [
        ("eda", "created"),
        ("plots", "updated"),
        ("bad", "failed"),
    ]
    assert (seen["branch"], seen["path"], seen["token"]) == ("main", "lib", None)
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=owner.tenant_id)
    assert [r.action for r in rows] == ["Synced skills"]
