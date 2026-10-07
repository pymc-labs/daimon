"""Channel skills: extra skills a channel's turns run with, on top of the agent's own.

A shared agent can carry a skill one team needs without every channel
getting it. Only a server admin or an operator token adds or removes one
(`authorize`'s SET_CHANNEL_SKILLS); a channel's own admins can't, since a
skill changes what a shared agent does.

A channel may add a workspace library skill (`{t8}-{name}`), or one uploaded
to an agent (`{t8}-{agent}/{name}`) only while that agent answers there and
belongs to no other isolated channel. Each row pins the version added, and
a turn's skills are decided once at admission (`turn_channel_skills`), so a
session's creation and its drift check build the same list
(`daimon.core.session_snapshot.session_skills`). A later upload of the skill
reaches the channel when it is added again.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsCustomSkill
from anthropic.types.beta.skill_list_response import SkillListResponse
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import agent_pin_names
from daimon.core.authz import Action, Decision, Place, Subject, authorize
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    list_skills_lenient,
    list_skills_strict,
)
from daimon.core.defaults.metadata import (
    skill_owner_candidates,
    strip_tenant_prefix,
    tenant_scoped_display_title,
)
from daimon.core.errors import SkillsListTruncatedError
from daimon.core.permissions import agent_permissions, ruled_agents
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_skills import add_channel_skill, list_channel_skills
from daimon.core.stores.domain import ChannelSkillRow
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.user_skills import list_user_skills_for_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

_TRUNCATED = re.compile(r"~[0-9a-f]{4}$")

ChannelSkillRefusal = Literal["not_found", "not_usable", "no_version", "too_many", "mount_clash"]

REFUSALS: Final[dict[ChannelSkillRefusal, str]] = {
    "not_found": "No skill of this workspace has that name or id. Nothing changed.",
    "not_usable": (
        "That skill was uploaded to an agent that doesn't answer in this channel, so the "
        "channel can't add it. Nothing changed."
    ),
    "no_version": "That skill has no version to add yet. Nothing changed.",
    "too_many": (
        f"The channel's agent would run with more than {AGENT_SKILL_CAP} skills, the most a "
        "session may have. Remove one first. Nothing changed."
    ),
    "mount_clash": (
        "That skill loads under the same name as one the channel's agent or this channel "
        "already has. Nothing changed."
    ),
}


def may_set_channel_skills(subject: Subject, channel_id: str) -> Decision:
    """Whether `subject` may add or remove `channel_id`'s skills: server admins only. Pure."""
    return authorize(
        TenantAccessPolicy(),
        subject=subject,
        action=Action.SET_CHANNEL_SKILLS,
        place=Place(channel_id=channel_id),
    )


@dataclass(frozen=True)
class ChannelSkillChoice:
    """A skill a channel may add, looked up once: what to store."""

    skill_id: str
    version: str
    name: str
    owner_agent_name: str | None


def _tenant_bodies(rows: Iterable[SkillListResponse], tenant_id: uuid.UUID) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for row in rows:
        if row.source != "custom":
            continue
        body = strip_tenant_prefix(tenant_id=tenant_id, display_title=row.display_title or "")
        if body is not None:
            bodies[row.id] = body
    return bodies


def _find(
    rows: Sequence[SkillListResponse], *, tenant_id: uuid.UUID, skill: str
) -> SkillListResponse | None:
    by_id = [row for row in rows if row.id == skill]
    if by_id:
        return by_id[0]
    agent_name, _, name = skill.rpartition("/")
    title = tenant_scoped_display_title(
        tenant_id=tenant_id, name=name, agent_name=agent_name or None
    )
    matches = [row for row in rows if row.source == "custom" and row.display_title == title]
    return max(matches, key=lambda row: row.created_at) if matches else None


async def choose_channel_skill(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    skill: str,
    agent: BetaManagedAgentsAgent | None,
    current: Sequence[ChannelSkillRow],
) -> ChannelSkillChoice | ChannelSkillRefusal:
    """The skill `skill` (an id, a library name or `agent/name`) names, if the channel may add it.

    `agent` is the agent the channel resolves to now, None when none does;
    `current` is the channel's skills. Raises SkillsListTruncatedError when
    the workspace's skills can't all be read.
    """
    rows = await list_skills_strict(client)
    found = _find(rows, tenant_id=tenant_id, skill=skill.strip())
    bodies = _tenant_bodies(rows, tenant_id)
    if found is None or found.id not in bodies:
        return "not_found"
    body = bodies[found.id]
    policy = await load_access_policy(session, tenant_id=tenant_id)
    uploads = await list_user_skills_for_tenant(session, tenant_id=tenant_id)
    stored = next((u.agent_name for u in uploads if u.anthropic_id == found.id), None)
    names = {n for n in (agent_pin_names(agent.name, agent.metadata) if agent else ()) if n}
    owners = skill_owner_candidates(
        body, stored_owner=stored, agent_names={*names, *ruled_agents(policy)}
    )
    if not owners and "/" not in body and _TRUNCATED.search(body):
        return "not_usable"  # a cut title may have lost its agent part
    if owners and (
        not owners <= names or agent_permissions(policy, owners).home not in (None, channel_id)
    ):
        return "not_usable"
    owner = min(owners) if owners else None
    name = body.rsplit("/", 1)[-1]
    if not found.latest_version:
        return "no_version"
    others = [row for row in current if row.skill_id != found.id]
    held: set[str] = {s.skill_id for s in agent.skills} if agent is not None else set()
    extra = [row.skill_id for row in others if row.skill_id not in held]
    if found.id not in held and len(held) + len(extra) + 1 > AGENT_SKILL_CAP:
        return "too_many"
    mounts = [bodies[sid].rsplit("/", 1)[-1] for sid in (*held, *extra) if sid in bodies]
    if found.id not in held and name in mounts:
        return "mount_clash"
    return ChannelSkillChoice(
        skill_id=found.id, version=found.latest_version, name=name, owner_agent_name=owner
    )


async def add_skill_to_channel(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    skill: str,
    default: DeploymentDefault,
    actor_account_id: uuid.UUID | None,
) -> ChannelSkillRow | ChannelSkillRefusal:
    """Add `skill` to `channel_id` for whatever agent answers there now, or say why not.

    The caller has already checked the actor may (`may_set_channel_skills`).
    Raises SkillsListTruncatedError like `choose_channel_skill`.
    """
    resolved = await resolve(
        session, context=ScopeContext(tenant_id=tenant_id, channel_id=channel_id), default=default
    )
    agent = (
        await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name=resolved.agent_name)
        if resolved.agent_name
        else None
    )
    current = await list_channel_skills(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    )
    choice = await choose_channel_skill(
        session,
        client,
        tenant_id=tenant_id,
        channel_id=channel_id,
        skill=skill,
        agent=agent,
        current=current,
    )
    if isinstance(choice, str):
        return choice
    return await add_channel_skill(
        session,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        skill_id=choice.skill_id,
        version=choice.version,
        name=choice.name,
        owner_agent_name=choice.owner_agent_name,
        actor_account_id=actor_account_id,
    )


def applicable_skills(
    rows: Sequence[ChannelSkillRow],
    *,
    agent: BetaManagedAgentsAgent,
    agent_names: tuple[str | None, ...],
    bodies: dict[str, str] | None,
) -> tuple[BetaManagedAgentsCustomSkill, ...]:
    """The rows a session of `agent` adds, in order. Pure.

    Left out: another agent's upload, a skill the agent holds already, one
    whose mount name `bodies` shows clashing with an earlier skill, and any
    past the session cap. `bodies` None (unreadable) skips the mount check.
    """
    held = {skill.skill_id for skill in agent.skills}
    mounts: set[str] = (
        set()
        if bodies is None
        else {bodies[sid].rsplit("/", 1)[-1] for sid in held if sid in bodies}
    )
    room = AGENT_SKILL_CAP - len(held)
    picked: list[BetaManagedAgentsCustomSkill] = []
    for row in rows:
        if row.owner_agent_name is not None and row.owner_agent_name not in agent_names:
            continue
        if row.skill_id in held or row.name in mounts or len(picked) >= room:
            continue
        mounts.add(row.name)
        held.add(row.skill_id)
        picked.append(
            BetaManagedAgentsCustomSkill(type="custom", skill_id=row.skill_id, version=row.version)
        )
    return tuple(picked)


async def turn_channel_skills(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    agent: BetaManagedAgentsAgent,
    agent_names: tuple[str | None, ...],
    rows: Sequence[ChannelSkillRow] | None = None,
) -> tuple[BetaManagedAgentsCustomSkill, ...]:
    """The extra skills a turn in `channel_id` runs with, decided once per turn.

    One DB read; the skills list is read only when the channel adds something,
    to keep a clashing mount (which would fail the session) out.
    """
    if rows is None:
        async with sessionmaker() as session:
            rows = await list_channel_skills(
                session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
            )
    if not rows:
        return ()
    bodies: dict[str, str] | None
    try:
        listed, truncated = await list_skills_lenient(client)
        bodies = None if truncated else _tenant_bodies(listed, tenant_id)
    except (anthropic.APIError, SkillsListTruncatedError):
        _log.warning("channel_skills.list_failed", tenant_id=str(tenant_id))
        bodies = None
    return applicable_skills(rows, agent=agent, agent_names=agent_names, bodies=bodies)


__all__ = [
    "REFUSALS",
    "ChannelSkillChoice",
    "ChannelSkillRefusal",
    "add_skill_to_channel",
    "applicable_skills",
    "choose_channel_skill",
    "may_set_channel_skills",
    "turn_channel_skills",
]
