"""Transport-level tests for daimon.core.skill_sync.remove.

Patterns (same as test_orchestrator):
- Real AsyncAnthropic over httpx.MockTransport via MARouter — no AsyncMock.
- SDK response objects constructed inline via real constructors.
- Real Postgres via db_session / db_session_factory.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsCustomSkill,
)
from daimon.core.skill_sync.remove import (
    _compute_skills_after_removal,
    remove_agent_skill_repo,
)
from daimon.core.stores.user_skills import (
    list_user_skills_for_agent,
    upsert_user_skill,
)
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# _compute_skills_after_removal — pure
# ---------------------------------------------------------------------------


def test_compute_skills_after_removal_returns_none_when_nothing_attached() -> None:
    """No-op: none of the removed ids are attached → None (skip the update)."""
    attached = [BetaManagedAgentsCustomSkill(skill_id="sk_keep", type="custom", version="1")]
    assert _compute_skills_after_removal(attached, {"sk_gone"}) is None, (
        "removing a skill that isn't attached must be a no-op"
    )


def test_compute_skills_after_removal_drops_only_the_removed_ids() -> None:
    """Partial removal keeps the survivors, sorted, as skill params."""
    attached = [
        BetaManagedAgentsCustomSkill(skill_id="sk_a", type="custom", version="1"),
        BetaManagedAgentsCustomSkill(skill_id="sk_keep", type="custom", version="1"),
    ]
    result = _compute_skills_after_removal(attached, {"sk_a"})
    assert result == [{"type": "custom", "skill_id": "sk_keep"}], (
        "must keep exactly the non-removed skills"
    )


def test_compute_skills_after_removal_returns_empty_list_when_clearing_last_skill() -> None:
    """Detach-all returns [] (a real update), distinct from the no-op None."""
    attached = [BetaManagedAgentsCustomSkill(skill_id="sk_a", type="custom", version="1")]
    assert _compute_skills_after_removal(attached, {"sk_a"}) == [], (
        "clearing the last skill must return an empty list, not None"
    )


# ---------------------------------------------------------------------------
# remove_agent_skill_repo — end to end
# ---------------------------------------------------------------------------


def _build_anthropic(router: MARouter) -> AsyncAnthropic:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    return AsyncAnthropic(api_key="test", http_client=http_client)


def _agent_payload(
    *, tenant_id: uuid.UUID, version: int, skill_ids: list[str]
) -> dict[str, object]:
    return BetaManagedAgentsAgent(
        id="ag1",
        type="agent",
        name="agent",
        model={"id": "claude-opus-4-7"},
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "agent"},
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=version,
        mcp_servers=[],
        skills=[
            BetaManagedAgentsCustomSkill(skill_id=sid, type="custom", version="1")
            for sid in skill_ids
        ],
        tools=[],
        system=None,
    ).model_dump(mode="json")


async def test_remove_agent_skill_repo_detaches_deletes_and_prunes_rows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing a repo detaches its skill from the agent, deletes the MA skill, drops the row.

    A second repo's skill ("keeper") must survive untouched.
    """
    tenant = await make_tenant(db_session)
    repo = "https://github.com/a/one"
    keep = "https://github.com/keep/repo"
    principal = uuid.uuid4()
    for name, aid, repo_url in (("a", "sk_a", repo), ("keeper", "sk_keep", keep)):
        await upsert_user_skill(
            db_session,
            tenant_id=tenant.id,
            principal_id=principal,
            agent_name="agent",
            name=name,
            source_repo_url=repo_url,
            source_repo_branch="main",
            source_path="",
            content_hash=f"h-{name}",
            anthropic_id=aid,
            anthropic_latest_version="1",
        )
    await db_session.commit()

    update_calls: list[dict[str, object]] = []
    deleted_skill_ids: list[str] = []

    def on_list_agents(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return list_response(
            [_agent_payload(tenant_id=tenant.id, version=7, skill_ids=["sk_a", "sk_keep"])]
        )

    def on_retrieve_agent(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200, json=_agent_payload(tenant_id=tenant.id, version=7, skill_ids=["sk_a", "sk_keep"])
        )

    def on_update_agent(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(json.loads(req.content))
        return httpx.Response(
            200, json=_agent_payload(tenant_id=tenant.id, version=8, skill_ids=["sk_keep"])
        )

    def on_list_versions(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "has_more": False})

    def on_delete_skill(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        deleted_skill_ids.append(m.group("sid"))
        return httpx.Response(200, json={})

    router = MARouter()
    router.add("GET", r"/v1/agents", on_list_agents)
    router.add("GET", r"/v1/agents/ag1", on_retrieve_agent)
    router.add("POST", r"/v1/agents/ag1", on_update_agent)
    router.add("GET", r"/v1/skills/(?P<sid>[^/]+)/versions", on_list_versions)
    router.add("DELETE", r"/v1/skills/(?P<sid>[^/]+)", on_delete_skill)
    anthropic_client = _build_anthropic(router)

    report = await remove_agent_skill_repo(
        tenant_id=tenant.id,
        agent_name="agent",
        repo_url=repo,
        sessionmaker=db_session_factory,
        anthropic_client=anthropic_client,
    )

    assert report.removed == 1, f"exactly the repo's one row must be deleted, got {report.removed}"
    assert report.detached == 1, f"one skill must be detached from the agent, got {report.detached}"
    assert deleted_skill_ids == ["sk_a"], (
        f"only the repo's MA skill must be deleted, got {deleted_skill_ids}"
    )
    assert len(update_calls) == 1, "agents.update must be called once to detach"
    assert update_calls[0].get("skills") == [{"type": "custom", "skill_id": "sk_keep"}], (
        f"detach must leave the other repo's skill attached, got {update_calls[0].get('skills')}"
    )

    async with db_session_factory() as s:
        rows = await list_user_skills_for_agent(
            s, tenant_id=tenant.id, principal_id=principal, agent_name="agent"
        )
    assert [r.name for r in rows] == ["keeper"], "only the other repo's row may survive"


async def test_remove_agent_skill_repo_noop_when_repo_has_no_rows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unknown repo → nothing removed, no MA calls at all."""
    tenant = await make_tenant(db_session)
    await db_session.commit()

    def _explode(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        raise AssertionError(f"no MA call expected, got {req.method} {req.url.path}")

    router = MARouter()
    router.add("GET", r"/v1/agents", _explode)
    anthropic_client = _build_anthropic(router)

    report = await remove_agent_skill_repo(
        tenant_id=tenant.id,
        agent_name="agent",
        repo_url="https://github.com/none/here",
        sessionmaker=db_session_factory,
        anthropic_client=anthropic_client,
    )
    assert report.removed == 0, "no rows to remove"
    assert report.detached == 0, "nothing to detach"
