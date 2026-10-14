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
from typing import Final, Literal, NoReturn

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
from daimon.core.errors import SkillsListTruncatedError, StoreError
from daimon.core.ma_resolver import ResolverCache, list_skills_lenient_cached
from daimon.core.permissions import agent_permissions, ruled_agents
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_skills import add_channel_skill, list_channel_skills
from daimon.core.stores.domain import ChannelSkillRow
from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.user_skills import list_user_skills_for_tenant
from mux.contracts.ids import Provider, Scope
from mux.errors import ScopeViolation
from mux.profiles import neutral_catalog_profile_for
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


@dataclass(frozen=True)
class NativeSkillDeleteGuard:
    """Authorized native operator edge, absent from the Anthropic default path.

    This grants attached-delete inspection/refusal, not provider deletion.
    An unlocked reference check cannot authorize a later destructive request.
    """

    scope: Scope
    provider: Provider
    account_scope_id: str
    operator_enabled: bool = False
    expected_agent_revisions: tuple[tuple[str, int], ...] = ()

    def require_enabled(self) -> None:
        if self.operator_enabled is not True:
            raise NativeSkillDeleteUnavailable("guard_disabled")
        if (
            neutral_catalog_profile_for(self.provider) is None
            or not self.account_scope_id.strip()
            or self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or not self.scope.principal_id.strip()
            or not self.scope.authorization_id.strip()
        ):
            raise ScopeViolation(
                "skill-delete", "explicit native tenant/account authorization required"
            )
        try:
            uuid.UUID(self.scope.tenant_id)
            uuid.UUID(self.scope.account_id)
        except ValueError:
            raise ScopeViolation("skill-delete", "tenant/account UUIDs required") from None


class NativeSkillDeleteUnavailable(StoreError):
    """No successful deletion is implied by an unsupported or stale host edge."""

    def __init__(
        self, reason: Literal["guard_disabled", "catalog_changed", "unattached_delete_unbound"]
    ) -> None:
        self.reason = reason
        super().__init__(
            "Native skill deletion is not enabled. Nothing was deleted."
            if reason == "guard_disabled"
            else "The skill catalog or channel references changed. Nothing was deleted; retry."
            if reason == "catalog_changed"
            else "Unattached native skill deletion has no fenced host edge. Nothing was deleted."
        )


class NativeSkillStillAttached(StoreError):
    def __init__(
        self,
        skill_id: str,
        *,
        agents: tuple[tuple[str, int], ...],
        channels: tuple[str, ...],
    ) -> None:
        self.skill_id, self.agents, self.channels = skill_id, agents, channels
        super().__init__("Skill is still attached to an agent or channel. Nothing was deleted.")


async def refuse_native_skill_delete(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    guard: NativeSkillDeleteGuard,
    skill: str,
) -> NoReturn:
    """Read real scoped references and fail closed; never send a provider DELETE.

    Retained older agent revisions may still answer existing sessions. Inspect
    those too. Revision/channel changes invalidate this observation. Even a
    stable unattached observation cannot fence a subsequent attachment, so that
    deletion stays unavailable until a shared catalog mutation fence exists.
    """
    guard.require_enabled()
    tenant_id = uuid.UUID(guard.scope.tenant_id)
    catalog = PostgresNeutralCatalog(
        sessionmaker, provider=guard.provider, account_scope_id=guard.account_scope_id
    )
    versions = await catalog.list_skills(guard.scope)
    matches = {
        row.skill_id
        for row in versions
        if skill
        in (row.skill_id, row.bundle.preview.name, f"{row.agent_name}/{row.bundle.preview.name}")
        or row.display_title == tenant_scoped_display_title(tenant_id=tenant_id, name=skill)
    }
    if len(matches) != 1:
        raise StoreError(
            "No unique skill with that name or id exists in the authorized catalog. "
            "Nothing was deleted."
        )
    skill_id = next(iter(matches))
    before = await catalog.list_agents(guard.scope)
    revisions = {row.catalog_id: row.local_revision for row in before}
    if any(revisions.get(id_) != revision for id_, revision in guard.expected_agent_revisions):
        raise NativeSkillDeleteUnavailable("catalog_changed")
    held: list[tuple[str, int]] = []
    for latest in before:
        for revision in range(1, latest.local_revision + 1):
            row = await catalog.get_agent(guard.scope, latest.catalog_id, local_revision=revision)
            if row is None:
                raise NativeSkillDeleteUnavailable("catalog_changed")
            if any(pin.id == skill_id for pin in row.agent.spec.skills or ()):
                held.append((row.catalog_id, revision))
    async with sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None:
            raise ScopeViolation("skill-delete", "tenant no longer exists")
        channels = await list_channel_skills(session, tenant_id=tenant_id, platform=tenant.platform)
    after = await catalog.list_agents(guard.scope)
    fresh_versions = await catalog.list_skills(guard.scope)
    async with sessionmaker() as session:
        fresh_channels = await list_channel_skills(
            session, tenant_id=tenant_id, platform=tenant.platform
        )
    if before != after or versions != fresh_versions or channels != fresh_channels:
        raise NativeSkillDeleteUnavailable("catalog_changed")
    attached_channels = tuple(row.channel_id for row in channels if row.skill_id == skill_id)
    if held or attached_channels:
        raise NativeSkillStillAttached(skill_id, agents=tuple(held), channels=attached_channels)
    raise NativeSkillDeleteUnavailable("unattached_delete_unbound")


def _tenant_bodies(rows: Iterable[SkillListResponse], tenant_id: uuid.UUID) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for row in rows:
        if row.source != "custom":
            continue
        body = strip_tenant_prefix(tenant_id=tenant_id, display_title=row.display_title or "")
        if body is not None:
            bodies[row.id] = body
    return bodies


def _lists_held_skills(rows: Iterable[SkillListResponse], agent: BetaManagedAgentsAgent) -> bool:
    listed = {row.id for row in rows}
    return all(skill.skill_id in listed for skill in agent.skills if skill.type == "custom")


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
    cache: ResolverCache | None = None,
) -> tuple[BetaManagedAgentsCustomSkill, ...]:
    """The extra skills a turn in `channel_id` runs with, decided once per turn.

    One DB read; the skills list is read only when the channel adds something,
    to keep a clashing mount (which would fail the session) out. With `cache`
    that list is shared across turns for a few seconds.
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
        listed, truncated = await (
            list_skills_lenient(client)
            if cache is None
            else list_skills_lenient_cached(client, cache)
        )
        if cache is not None and not truncated and not _lists_held_skills(listed, agent):
            # The shared listing predates a skill the agent now holds, whose
            # mount would escape the clash check: read it fresh.
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
