"""Whether an agent follows a server skill's new versions or stays on the one it pins."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import ChangeRecorder, load_target, require_setup
from daimon.core.server_skills.anthropic import PROVIDER, ManagedAgentsTarget
from daimon.core.server_skills.sync import TargetError
from daimon.core.skill_library_files import skill_lock
from daimon.core.stores import server_skills as store
from daimon.core.stores.domain import ServerSkillStatus
from pydantic import BaseModel

__all__ = ["SkillFollow", "agent_follows", "apply_held_update", "set_agent_follow"]


class SkillFollow(BaseModel):
    follow: bool
    status: ServerSkillStatus | Literal["needs_reconfirm", "managed"]
    """`needs_reconfirm`: the update would change what the agent's backend drops.
    `managed`: the agent comes from settings, so it keeps the version they set."""
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
        held_at = await store.held_updates(
            session, tenant_id=actor.tenant_id, agent_id=target.agent.id
        )
        for m in mirrors:
            held = (await store.list_deployments(session, m.id)).get(PROVIDER)
            follow = chosen.get(m.skill_id, True)
            status: ServerSkillStatus | Literal["needs_reconfirm", "managed"] = m.status
            if target.managed:
                status = "managed"
            elif follow and held_at.get(m.skill_id) == m.digest:
                status = "needs_reconfirm"
            out[m.skill_id] = SkillFollow(
                follow=follow and not target.managed,
                status=status,
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
    """Follow the skill's new versions, or keep the version the agent pins.

    The caller moves a newly following agent with `sync.follow_now`.
    """
    target = await load_target(deps, actor=actor, ma_agent_id=agent_id)
    require_setup(target, "Skills")
    _require_unmanaged(target.managed)
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


def _require_unmanaged(managed: bool) -> None:
    if managed:
        raise AgentEditError("Managed by settings.", kind="forbidden")


async def apply_held_update(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_id: str,
    skill_id: str,
    audited: ChangeRecorder | None = None,
) -> None:
    """Move the agent to the update its backend consent held back.

    The owner's own edit, like changing the skill by hand: the agent's backend then
    asks its owner to confirm the drops anew before it answers again. Runs under the
    skill's lock, so the sync and this see the same follow and hold.
    """
    target = await load_target(deps, actor=actor, ma_agent_id=agent_id)
    require_setup(target, "Skills")
    _require_unmanaged(target.managed)
    agent = target.agent.id
    async with skill_lock(deps, tenant_id=actor.tenant_id, skill_id=skill_id):
        async with deps.session_factory() as session:
            mirror = await store.mirror_of_skill(
                session, tenant_id=actor.tenant_id, skill_id=skill_id
            )
            held_at = await store.held_updates(session, tenant_id=actor.tenant_id, agent_id=agent)
            pinned = await store.pinned_agents(
                session, tenant_id=actor.tenant_id, skill_id=skill_id
            )
            deployed = None if mirror is None else await store.list_deployments(session, mirror.id)
        if mirror is None or deployed is None:
            raise AgentEditError("No such server skill.", kind="not_found")
        held = deployed.get(PROVIDER)
        if (
            agent in pinned
            or held_at.get(skill_id) != mirror.digest
            or held is None
            or held.digest != mirror.digest
        ):
            raise AgentEditError("No update is waiting for this agent.", kind="conflict")
        try:
            await ManagedAgentsTarget(deps.client).repin(mirror, agent, held.version)
        except TargetError as exc:
            raise AgentEditError(
                "The update couldn't be applied. Try again.", kind="unavailable"
            ) from exc
        async with deps.session_factory() as session, session.begin():
            await store.release_holds(
                session, tenant_id=actor.tenant_id, agent_ids=[agent], skill_id=skill_id
            )
            if audited is not None:
                await audited(session, target.agent.name)
