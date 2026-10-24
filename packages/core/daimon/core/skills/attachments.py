"""Where a skill is still attached, and the guard that keeps a delete from stranding it.

A session resolves every skill its agent pins and every skill its channel rule
adds when it is created. Deleting a skill one of those still names fails that
session creation (`Could not resolve one or more skills`), and every later
turn with it, until the reference is removed. So a delete first looks for
references: non-archived agents on Managed Agents (skills are workspace-wide,
so any tenant's agent counts) and `channel_skills` rows. With references, the
delete is refused naming them, unless the caller asked to detach everywhere
first.

This is the one place a delete checks for references. Attaches take no lock:
one that lands between the final check and the provider delete leaves a
reference to a gone skill, and the next session there fails with
`SkillUnavailable`, which names the skill and says who must remove it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from daimon.core.defaults.ma_index import list_agents_referencing_skill
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.errors import UserFacingError
from daimon.core.ma import delete_skill_and_versions, update_agent_with_version_retry
from daimon.core.mux_compat import update_agent
from daimon.core.stores.channel_skills import (
    list_skill_channel_rules,
    remove_skill_from_every_channel,
)
from daimon.core.stores.domain import ChannelSkillRow
from daimon.core.stores.user_skills import delete_user_skills_for_skill_id
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AgentAttachment:
    agent_id: str
    name: str
    tenant: str | None
    """The agent's `daimon_tenant` metadata, or None for an agent Daimon did not stamp."""


@dataclass(frozen=True)
class SkillAttachments:
    agents: tuple[AgentAttachment, ...]
    channels: tuple[ChannelSkillRow, ...]

    def __bool__(self) -> bool:
        return bool(self.agents or self.channels)

    def outside(self, tenant_id: uuid.UUID) -> tuple[int, int]:
        """How many of the agents and channel rules belong to another tenant."""
        tenant = str(tenant_id)
        return (
            sum(1 for agent in self.agents if agent.tenant != tenant),
            sum(1 for row in self.channels if row.tenant_id != tenant_id),
        )


class SkillStillAttached(UserFacingError):
    """A delete was refused because agents or channel rules still use the skill."""

    def __init__(self, message: str, *, attachments: SkillAttachments) -> None:
        super().__init__(message)
        self.attachments = attachments


async def find_skill_attachments(
    client: AsyncAnthropic,
    session: AsyncSession,
    *,
    skill_id: str,
) -> SkillAttachments:
    """Every non-archived agent pinning `skill_id` and every channel rule adding it.

    On its own this is a preview: use `delete_skill_unless_attached` to act on it.
    """
    agents = [
        AgentAttachment(
            agent_id=agent.id, name=agent.name, tenant=agent.metadata.get(MA_METADATA_KEY_TENANT)
        )
        for agent in await list_agents_referencing_skill(client, skill_id)
    ]
    channels = await list_skill_channel_rules(session, skill_id=skill_id)
    return SkillAttachments(agents=tuple(agents), channels=tuple(channels))


@dataclass(frozen=True)
class GuardedDelete:
    deleted: bool
    """False when the skill is still attached and `detach` was not asked for."""
    attachments: SkillAttachments
    """What the final check found: refused on, or detached before deleting."""


async def delete_skill_unless_attached(
    client: AsyncAnthropic,
    session: AsyncSession,
    *,
    skill_id: str,
    detach: bool,
    own_agent_ids: Collection[str] = (),
    scope: Scope | None = None,
) -> GuardedDelete:
    """Delete `skill_id` unless something references it, or detach it first.

    Runs in `session`'s open transaction: checks again, detaches (channel
    rows are deleted on `session`, so they commit with it), then deletes every
    version and the skill, and forgets every saved upload or sync of it, so
    re-adding the same content creates a new skill instead of re-pinning this id.

    `own_agent_ids` are agents whose pin the caller is retiring with the skill
    (a skill sync's orphan on the agent being synced): their pins are detached
    and never refuse the delete; any other reference still does. `scope` is
    the tenant scope of the provider delete; without it the delete runs under
    the legacy host scope. Detaching crosses tenants and keeps the host scope.
    """
    attachments = await find_skill_attachments(client, session, skill_id=skill_id)
    others = SkillAttachments(
        agents=tuple(a for a in attachments.agents if a.agent_id not in own_agent_ids),
        channels=attachments.channels,
    )
    if others and not detach:
        return GuardedDelete(deleted=False, attachments=others)
    if attachments:
        await _detach_agents(client, skill_id=skill_id, attachments=attachments)
        await remove_skill_from_every_channel(session, skill_id=skill_id)
    await delete_skill_and_versions(client, skill_id, scope=scope)
    await delete_user_skills_for_skill_id(session, anthropic_id=skill_id)
    return GuardedDelete(deleted=True, attachments=attachments)


def still_attached_message(
    name: str,
    attachments: SkillAttachments,
    *,
    agent_detach: str,
    channel_detach: str,
    force_hint: str | None,
    viewer_tenant_id: uuid.UUID | None = None,
) -> str:
    """The refusal: where `name` is attached and how to detach it.

    `agent_detach`, `channel_detach` and `force_hint` are the caller's surface's own words
    (a CLI flag, a chat tool), so the steps are ones the reader can take.
    With `viewer_tenant_id`, only that tenant's agents and channels are
    named; references from elsewhere are only counted, so a workspace admin
    never learns another workspace's agent names or channel ids.
    """
    viewer = None if viewer_tenant_id is None else str(viewer_tenant_id)
    places: list[str] = []
    if attachments.agents:
        shown = sorted({a.name for a in attachments.agents if viewer is None or a.tenant == viewer})
        hidden = sum(1 for a in attachments.agents if viewer is not None and a.tenant != viewer)
        parts = list(shown) + ([f"{hidden} agent(s) elsewhere"] if hidden else [])
        places.append(f"agent(s) {', '.join(parts)}")
    if attachments.channels:
        shown_rules = [
            f"{row.platform} channel {row.channel_id}"
            for row in attachments.channels
            if viewer is None or str(row.tenant_id) == viewer
        ]
        hidden = len(attachments.channels) - len(shown_rules)
        parts = shown_rules + ([f"{hidden} channel(s) elsewhere"] if hidden else [])
        places.append(f"channel rule(s) on {', '.join(parts)}")
    steps: list[str] = []
    if attachments.agents:
        steps.append(agent_detach)
    if attachments.channels:
        steps.append(channel_detach)
    message = (
        f"skill {name!r} is still attached to {' and '.join(places)}; deleting it would "
        "break every conversation that uses it. First " + "; ".join(steps) + "."
    )
    if force_hint is not None:
        message += f" {force_hint}"
    return message


async def _detach_agents(
    client: AsyncAnthropic, *, skill_id: str, attachments: SkillAttachments
) -> None:
    """Remove `skill_id` from every agent in `attachments`.

    Each agent is re-read before its update (`update_agent_with_version_retry`),
    and keeps its other skills at the versions it pins.
    """
    # Skills are workspace-wide, so a detach crosses tenants by design: the
    # agents come from an org-wide reference read, not the caller's tenant.
    backend_scope = Scope.legacy_host_authorized(
        call_site="daimon.core.skills.attachments:_detach_agents"
    )
    for attachment in attachments.agents:

        async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
            kept = [
                cast(BetaManagedAgentsSkillParams, skill.model_dump(mode="json"))
                for skill in fresh.skills
                if skill.skill_id != skill_id
            ]
            if len(kept) == len(fresh.skills):
                return fresh
            return await update_agent(
                client,
                fresh.id,
                version=fresh.version,
                payload={"skills": kept},
                scope=backend_scope,
            )

        await update_agent_with_version_retry(
            client, attachment.agent_id, _apply, scope=backend_scope
        )


__all__ = [
    "AgentAttachment",
    "SkillAttachments",
    "GuardedDelete",
    "SkillStillAttached",
    "delete_skill_unless_attached",
    "find_skill_attachments",
    "still_attached_message",
]
