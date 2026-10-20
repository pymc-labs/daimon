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

A `defaults/` agent's skill limited to some platforms (`SkillRef.platforms`)
rides the same path: it stays off the agent at the provider, and admission
adds it ahead of the channel's on those platforms (`turn_platform_skills`).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, NoReturn, Protocol

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsAnthropicSkill,
    BetaManagedAgentsCustomSkill,
)
from anthropic.types.beta.skill_list_response import SkillListResponse
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import agent_pin_names
from daimon.core.authz import Action, Decision, Place, Subject, authorize
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    list_skills_lenient,
    list_skills_strict,
    match_skills_by_display_title,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    skill_owner_candidates,
    strip_tenant_prefix,
    tenant_scoped_display_title,
)
from daimon.core.errors import SkillsListTruncatedError, StoreError
from daimon.core.ma_resolver import ResolverCache, list_skills_lenient_cached
from daimon.core.permissions import agent_permissions, ruled_agents
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.session_snapshot import MaSkill
from daimon.core.specs import SkillRef
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

# Warnings already logged, keyed by event and what they name: every turn in the
# same place repeats them, so each is logged once per process until it changes.
_LOGGED: set[tuple[str, ...]] = set()
_LOGGED_MAX = 4096

_TRUNCATED = re.compile(r"~[0-9a-f]{4}$")

ChannelSkillRefusal = Literal["not_found", "not_usable", "no_version", "too_many", "mount_clash"]

REFUSALS: Final[dict[ChannelSkillRefusal, str]] = {
    "not_found": "No skill here has that name or id. Nothing changed.",
    "not_usable": (
        "That skill was uploaded to an agent that doesn't answer in this channel, so the "
        "channel can't add it. Nothing changed."
    ),
    "no_version": "That skill has no version to add yet. Nothing changed.",
    "too_many": (
        f"The channel's agent would run with more than {AGENT_SKILL_CAP} skills, the most "
        "one conversation can have. Remove one first. Nothing changed."
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
    platform: str,
    platform_skills: Mapping[str, Sequence[SkillRef]],
) -> ChannelSkillChoice | ChannelSkillRefusal:
    """The skill `skill` (an id, a library name or `agent/name`) names, if the channel may add it.

    `agent` is the agent the channel resolves to now, None when none does;
    `current` is the channel's skills. The skills `platform_skills` adds to
    `agent` on `platform` count toward the cap and the mount check like its
    own (`turn_platform_skills`). Raises SkillsListTruncatedError when the
    workspace's skills can't all be read.
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
    running: list[MaSkill] = []
    if agent is not None:
        running.extend(agent.skills)
        for ref in platform_skill_refs(agent, platform, platform_skills):
            built_in = _built_in(ref)
            if built_in is not None:
                running.append(built_in)
                continue
            title = tenant_scoped_display_title(tenant_id=tenant_id, name=ref.skill_id)
            running.extend(
                BetaManagedAgentsCustomSkill(type="custom", skill_id=match.id, version="latest")
                for match in match_skills_by_display_title(rows, title)[:1]
            )
    held = {s.skill_id for s in running}
    extra = [row.skill_id for row in others if row.skill_id not in held]
    if found.id not in held and len(held) + len(extra) + 1 > AGENT_SKILL_CAP:
        return "too_many"
    mounts = _built_in_names(running) | {
        bodies[sid].rsplit("/", 1)[-1] for sid in (*held, *extra) if sid in bodies
    }
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
    platform_skills: Mapping[str, Sequence[SkillRef]],
) -> ChannelSkillRow | ChannelSkillRefusal:
    """Add `skill` to `channel_id` for whatever agent answers there now, or say why not.

    The caller has already checked the actor may (`may_set_channel_skills`).
    `platform_skills` is `load_platform_skills`' map, whose skills on
    `platform` count like the agent's own. Raises SkillsListTruncatedError
    like `choose_channel_skill`.
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
        platform=platform,
        platform_skills=platform_skills,
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


def _log_once(event: str, key: tuple[str, ...], **fields: object) -> None:
    if (event, *key) in _LOGGED:
        return
    if len(_LOGGED) >= _LOGGED_MAX:
        _LOGGED.clear()
    _LOGGED.add((event, *key))
    _log.warning(event, **fields)


class SkillHolder(Protocol):
    """The skill-selection helpers need only a resolved list, never a provider client."""

    @property
    def skills(self) -> Sequence[MaSkill]: ...


def skill_room(agent: SkillHolder, platform_skills: Sequence[MaSkill] = ()) -> int:
    """How many channel skills a session of `agent` may add under `AGENT_SKILL_CAP`. Pure.

    Every skill the session holds before them counts: the agent's own and the
    platform's (`turn_platform_skills`), Anthropic's built-in ones too, since
    the provider's limit counts the whole `skills` list.
    """
    held = {skill.skill_id for skill in (*agent.skills, *platform_skills)}
    return max(0, AGENT_SKILL_CAP - len(held))


def platform_skill_refs(
    agent: BetaManagedAgentsAgent,
    platform: str,
    platform_skills: Mapping[str, Sequence[SkillRef]],
) -> tuple[SkillRef, ...]:
    """The skills `defaults/` limits to `platform` for `agent`, by its seeded name. Pure.

    `platform_skills` maps each default agent's name to its platform-limited
    refs (`load_platform_skills`). Only the agent the defaults pipeline manages
    gets them, never a fork or another agent that took its name.
    """
    metadata = agent.metadata or {}
    if metadata.get(MA_METADATA_KEY_MANAGED) != "true":
        return ()
    refs = platform_skills.get(metadata.get(MA_METADATA_KEY_NAME, ""), ())
    return tuple(ref for ref in refs if ref.platforms is not None and platform in ref.platforms)


# Ceiling on what `PlatformSkillMemory` keeps; past it, it starts over.
_MEMORY_MAX = 4096
# How long a platform skill the provider refused stays out of new sessions.
PLATFORM_SKILL_RETRY_AFTER: Final = dt.timedelta(hours=1)


@dataclass
class PlatformSkillMemory:
    """What this process remembers about platform-limited skills, per tenant.

    `found` keeps each custom skill's id from the last skills list that showed
    it, so a failed read leaves a session's skills as they were rather than
    dropping the skill and replacing the session. `refused` keeps a skill the
    provider refused at session create out of new sessions until a deadline
    (`refuse`), so the drift check doesn't replace a session on every turn.
    """

    found: dict[tuple[uuid.UUID, str], str] = field(
        default_factory=dict[tuple[uuid.UUID, str], str]
    )
    refused: dict[tuple[uuid.UUID, str], dt.datetime] = field(
        default_factory=dict[tuple[uuid.UUID, str], dt.datetime]
    )

    def refuse(self, tenant_id: uuid.UUID, skill_id: str, *, now: dt.datetime) -> bool:
        """Leave `skill_id` out of the tenant's sessions for a while; False if it already was."""
        self.refused = {key: until for key, until in self.refused.items() if until > now}
        if (tenant_id, skill_id) in self.refused:
            return False
        if len(self.refused) >= _MEMORY_MAX:
            self.refused.clear()
        self.refused[(tenant_id, skill_id)] = now + PLATFORM_SKILL_RETRY_AFTER
        return True

    def is_refused(self, tenant_id: uuid.UUID, skill_id: str, *, now: dt.datetime) -> bool:
        until = self.refused.get((tenant_id, skill_id))
        return until is not None and until > now

    def remember(self, tenant_id: uuid.UUID, name: str, skill_id: str | None) -> None:
        """Record what a complete skills list showed for `name`: its id, or None for gone."""
        if skill_id is None:
            self.found.pop((tenant_id, name), None)
            return
        if len(self.found) >= _MEMORY_MAX:
            self.found.clear()
        self.found[(tenant_id, name)] = skill_id


def _built_in(ref: SkillRef) -> BetaManagedAgentsAnthropicSkill | None:
    """The provider built-in `ref` names, at its latest version; None for a custom skill."""
    if ref.type == "custom":
        return None
    return BetaManagedAgentsAnthropicSkill(type=ref.type, skill_id=ref.skill_id, version="latest")


def _built_in_names(skills: Iterable[MaSkill]) -> set[str]:
    """The mount names of `skills`' provider built-ins, which load under their ids."""
    return {
        skill.skill_id for skill in skills if isinstance(skill, BetaManagedAgentsAnthropicSkill)
    }


async def turn_platform_skills(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent: BetaManagedAgentsAgent,
    platform_skills: Mapping[str, Sequence[SkillRef]],
    now: dt.datetime,
    memory: PlatformSkillMemory | None = None,
    cache: ResolverCache | None = None,
) -> tuple[MaSkill, ...]:
    """The skills `defaults/` adds to a session of `agent` on `platform`, at latest.

    A built-in needs no read. A custom skill is found by its seeded title in
    the skills list (shared through `cache`). A failed or incomplete read
    keeps the id `memory` last saw; one never seen, or gone from a complete
    list, is left out and the turn runs without it, with a warning logged once
    per process until what is missing changes. A skill the provider refused
    (`PlatformSkillMemory.refuse`) stays out until its deadline, and one the
    agent already holds is not added twice. Only the room the agent's own
    skills leave under `AGENT_SKILL_CAP` is filled, the first refs first.
    """
    refs = platform_skill_refs(agent, platform, platform_skills)
    if not refs:
        return ()
    memory = memory if memory is not None else PlatformSkillMemory()
    listed: list[SkillListResponse] = []
    complete = False
    if any(_built_in(ref) is None for ref in refs):
        try:
            listed, truncated = await (
                list_skills_lenient(client)
                if cache is None
                else list_skills_lenient_cached(client, cache)
            )
            complete = not truncated
        except (anthropic.APIError, SkillsListTruncatedError):
            _log.warning("channel_skills.list_failed", tenant_id=str(tenant_id))
    own = {skill.skill_id for skill in agent.skills}
    held = set(own)
    picked: list[MaSkill] = []
    missing: list[str] = []
    for ref in refs:
        skill: MaSkill | None = _built_in(ref)
        if skill is None:
            title = tenant_scoped_display_title(tenant_id=tenant_id, name=ref.skill_id)
            matches = match_skills_by_display_title(listed, title)
            if matches or complete:
                memory.remember(tenant_id, ref.skill_id, matches[0].id if matches else None)
            skill_id = memory.found.get((tenant_id, ref.skill_id))
            if skill_id is None:
                missing.append(ref.skill_id)
                continue
            skill = BetaManagedAgentsCustomSkill(type="custom", skill_id=skill_id, version="latest")
        if skill.skill_id in held or memory.is_refused(tenant_id, skill.skill_id, now=now):
            continue
        held.add(skill.skill_id)
        picked.append(skill)
    if missing:
        _log_once(
            "channel_skills.platform_skills_unavailable",
            (str(tenant_id), platform, *missing),
            tenant_id=str(tenant_id),
            platform=platform,
            missing=missing,
        )
    room = max(0, AGENT_SKILL_CAP - len(own))
    if len(picked) > room:
        # An agent near the cap would otherwise fail every session on this platform.
        dropped = tuple(skill.skill_id for skill in picked[room:])
        _log_once(
            "channel_skills.platform_skills_over_cap",
            (str(tenant_id), platform, agent.id, *dropped),
            tenant_id=str(tenant_id),
            platform=platform,
            agent_id=agent.id,
            dropped=list(dropped),
            cap=AGENT_SKILL_CAP,
        )
    return tuple(picked[:room])


def applicable_skills(
    rows: Sequence[ChannelSkillRow],
    *,
    agent: SkillHolder,
    agent_names: tuple[str | None, ...],
    bodies: dict[str, str] | None,
    platform_skills: Sequence[MaSkill] = (),
) -> tuple[BetaManagedAgentsCustomSkill, ...]:
    """The rows a session of `agent` may add, in order, before the cap (`skill_room`). Pure.

    Left out: another agent's upload, a skill the session holds already (the
    agent's or the platform's), and one whose mount name clashes with an
    earlier skill: a built-in's id, or a custom skill's name in `bodies`.
    `bodies` None (unreadable) checks the built-ins only.
    """
    held = {skill.skill_id for skill in (*agent.skills, *platform_skills)}
    mounts = _built_in_names((*agent.skills, *platform_skills))
    if bodies is not None:
        mounts |= {bodies[sid].rsplit("/", 1)[-1] for sid in held if sid in bodies}
    picked: list[BetaManagedAgentsCustomSkill] = []
    for row in rows:
        if row.owner_agent_name is not None and row.owner_agent_name not in agent_names:
            continue
        if row.skill_id in held or row.name in mounts:
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
    platform_skills: Sequence[MaSkill] = (),
) -> tuple[BetaManagedAgentsCustomSkill, ...]:
    """The extra skills a turn in `channel_id` runs with, after `platform_skills`.

    One DB read; the skills list is read only when the channel adds something,
    to keep a clashing mount (which would fail the session) out. With `cache`
    that list is shared across turns for a few seconds. Skills past the cap
    (`skill_room`) are left out, the most recently added ones (rows come
    oldest first), with a warning naming them, logged once per channel and
    process until the dropped set changes.
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
    wanted = applicable_skills(
        rows, agent=agent, agent_names=agent_names, bodies=bodies, platform_skills=platform_skills
    )
    room = skill_room(agent, platform_skills)
    if len(wanted) > room:
        dropped = tuple(skill.skill_id for skill in wanted[room:])
        _log_once(
            "channel_skills.over_cap",
            (str(tenant_id), platform, channel_id, *dropped),
            tenant_id=str(tenant_id),
            channel_id=channel_id,
            dropped=list(dropped),
            cap=AGENT_SKILL_CAP,
        )
    return wanted[:room]


__all__ = [
    "PLATFORM_SKILL_RETRY_AFTER",
    "REFUSALS",
    "ChannelSkillChoice",
    "ChannelSkillRefusal",
    "PlatformSkillMemory",
    "add_skill_to_channel",
    "applicable_skills",
    "choose_channel_skill",
    "may_set_channel_skills",
    "platform_skill_refs",
    "skill_room",
    "turn_channel_skills",
    "turn_platform_skills",
]
