"""Add skill, from Details: paste a SKILL.md, see what it holds, then add it.

Discord's and Slack's form, in a dialog. A dialog has no file input, so the
SKILL.md is pasted, as on Slack; a `.zip` goes through chat, where
`add_skill(attachment_url=…)` runs the same checks. The first submit, or one
whose text changed, comes back as the form with a preview and the previewed
hash; an unchanged one adds. The skill becomes the agent's own copy; the
shared library and the built-in agents are never touched.

Who may: Discord's rule (`skill_change_refusal`). A server admin may add to
any agent they could edit, a channel admin to one that answers only in their
channels, anyone to one that nobody else uses. The panel lives in the 1:1
chat, outside every channel, so a pinned agent takes an add only from a
server admin or an admin of every channel it is pinned to. Opening the form,
each submit and the add itself re-check live, the last on the re-read agent
right before the upload and the attach.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.teams.card_actions import Actor
from daimon.adapters.teams.channel_admin_groups import channel_admin_caller
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.agent_pins import agent_pin_names, pin_refusal
from daimon.core.agent_reach import load_target_facts
from daimon.core.authz import Place, Subject
from daimon.core.channel_admins import load_live_subject
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.operation_policy import decide_operation
from daimon.core.roster import RosterAgent
from daimon.core.skills.add import SkillAddResult, add_agent_skill
from daimon.core.skills.ingest import SkillBundle

log = structlog.get_logger()

BUILT_IN: Final = (
    "This is a starting agent and can't be changed directly. Ask an admin to copy it, "
    "or ask me to make you a new agent."
)


def needs_admin(agent_name: str) -> str:
    return (
        f"Others use {agent_name} (a default, a thread, or someone else's routine or "
        "conversation), so adding a skill needs an admin or an admin of every channel "
        "it answers in."
    )


class SkillAddRefused(Exception):
    """A last-moment re-check refused the add; `refusal` is shown as it is."""

    def __init__(self, refusal: str) -> None:
        super().__init__(refusal)
        self.refusal = refusal


async def skill_change_refusal(
    runtime: TeamsRuntime,
    actor: Actor,
    agent: RosterAgent,
    *,
    account_id: uuid.UUID | None,
    ma_agent: BetaManagedAgentsAgent | None = None,
) -> str | None:
    """Why `actor` may not change this agent's skills from the panel now, or None.

    Pass the re-read `ma_agent` for the final check, so its routing name counts.
    Without it the agent is read only when the tenant pins anything.
    """
    if agent.is_built_in:
        return BUILT_IN
    caller = await channel_admin_caller(
        runtime, tenant_id=actor.tenant_id, user_id=actor.user_id, is_admin=actor.is_admin
    )
    names = (agent.name, *(agent_pin_names(ma_agent.name, ma_agent.metadata) if ma_agent else ()))

    async def target() -> BetaManagedAgentsAgent:
        if ma_agent is not None:
            return ma_agent
        return await runtime.anthropic.beta.agents.retrieve(agent.ma_agent_id)

    async with runtime.sessionmaker() as session:

        async def live_subject() -> Subject:
            return await load_live_subject(
                session, tenant_id=actor.tenant_id, platform="teams", caller=caller
            )

        # The panel is in the 1:1 chat: no channel, so a pin is kept only by its admins.
        pinned = await pin_refusal(
            session,
            tenant_id=actor.tenant_id,
            load_subject=live_subject,
            load_agent=target,
            place=Place(),
        )
        if pinned is not None:
            return pinned
        facts = await load_target_facts(
            session,
            "skill_add",
            tenant_id=actor.tenant_id,
            platform="teams",
            agent_names=names,
            ma_agent_id=agent.ma_agent_id,
            default=runtime.deployment_default,
            caller=caller,
            is_daimon_managed=False,
            caller_account_id=account_id,
            caller_platform_user_id=actor.user_id,
        )
    if decide_operation("skill_add", is_admin=caller.is_server_admin, target=facts) == "allow":
        return None
    return needs_admin(agent.name)


def _stamp_refusal(metadata: dict[str, str], *, tenant_id: str, name: str) -> str | None:
    """Re-read from the agent itself: another tenant's, built-in or system agents are refused."""
    if metadata.get(MA_METADATA_KEY_TENANT) != tenant_id:
        return f"{name} is not an agent of this organisation."
    if metadata.get(MA_METADATA_KEY_MANAGED) == "true" or MA_METADATA_KEY_ACCOUNT not in metadata:
        return BUILT_IN
    return None


async def add_previewed_skill(
    runtime: TeamsRuntime,
    actor: Actor,
    agent: RosterAgent,
    bundle: SkillBundle,
    *,
    account_id: uuid.UUID,
) -> SkillAddResult:
    """Re-read the agent, re-check, and add `bundle` as its own skill.

    Raises `SkillAddRefused` when a re-check refuses, before or during the add.
    """

    async def recheck(fresh: BetaManagedAgentsAgent) -> None:
        refusal = _stamp_refusal(
            fresh.metadata, tenant_id=str(actor.tenant_id), name=agent.name
        ) or await skill_change_refusal(
            runtime, actor, agent, account_id=account_id, ma_agent=fresh
        )
        if refusal is not None:
            raise SkillAddRefused(refusal)

    fresh = await runtime.anthropic.beta.agents.retrieve(agent.ma_agent_id)
    await recheck(fresh)
    result = await add_agent_skill(
        runtime.anthropic,
        runtime.sessionmaker,
        tenant_id=actor.tenant_id,
        agent=fresh,
        agent_name=agent.name,
        bundle=bundle,
        origin="pasted",
        added_by_account_id=account_id,
        recheck=recheck,
    )
    log.info("teams.agent_setup.skill_added", agent_name=agent.name, action=result.action)
    return result
