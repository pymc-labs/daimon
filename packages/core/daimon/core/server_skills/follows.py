"""Whether an agent follows a server skill's new versions or stays on the one it pins."""

from __future__ import annotations

from datetime import datetime

from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import ChangeRecorder, load_target, require_setup
from daimon.core.server_skills.anthropic import PROVIDER
from daimon.core.stores import server_skills as store
from daimon.core.stores.domain import ServerSkillStatus
from pydantic import BaseModel

__all__ = ["SkillFollow", "agent_follows", "set_agent_follow"]


class SkillFollow(BaseModel):
    follow: bool
    status: ServerSkillStatus
    version: str | None
    """The newest version agents can be pinned to."""


async def agent_follows(
    deps: AgentEditDeps, *, actor: AgentActor, agent_id: str
) -> dict[str, SkillFollow]:
    """Each server skill the tenant mirrors: whether this agent follows it, and its state."""
    target = await load_target(deps, actor=actor, ma_agent_id=agent_id)
    out: dict[str, SkillFollow] = {}
    async with deps.session_factory() as session:
        mirrors = await store.list_mirrors(session, tenant_id=actor.tenant_id)
        chosen = await store.follows_of_agent(
            session, tenant_id=actor.tenant_id, agent_id=target.agent.id
        )
        for m in mirrors:
            held = (await store.list_deployments(session, m.id)).get(PROVIDER)
            out[m.skill_id] = SkillFollow(
                follow=chosen.get(m.skill_id, True),
                status=m.status,
                version=None if held is None else held.version,
            )
    return out


async def set_agent_follow(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_id: str,
    skill_id: str,
    follow: bool,
    now: datetime,
    audited: ChangeRecorder | None = None,
) -> None:
    """Follow the skill's new versions, or keep the version the agent pins."""
    target = await load_target(deps, actor=actor, ma_agent_id=agent_id)
    require_setup(target, "Skills")
    async with deps.session_factory() as session, session.begin():
        if (
            await store.mirror_of_skill(session, tenant_id=actor.tenant_id, skill_id=skill_id)
            is None
        ):
            raise AgentEditError("No such server skill.", kind="not_found")
        await store.set_follow(
            session,
            tenant_id=actor.tenant_id,
            agent_id=target.agent.id,
            skill_id=skill_id,
            follow=follow,
            now=now,
        )
        if audited is not None:
            await audited(session, target.agent.name)
