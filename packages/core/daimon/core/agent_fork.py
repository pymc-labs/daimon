"""Copy an agent under a new name: the one copy behind every `fork_agent`.

The copy takes the source's prompt, model, tools, MCP definitions and skills
from its live MA state, with the default daimon MCP server and base toolset
guaranteed and the credential guidance applied. It starts with no credentials
(`agent_lifecycle.strip_credentialed_mcp_servers`): the servers and GitHub
repos that need connecting again are named in its receipt (`fork_notes`).
Skills scoped to the source are uploaded again under the copy's own name, so
the two never share a skill id; skills scoped to another agent, and any copy
that fails, are left off and named. Given a `capture_source`, it also takes
the source's provider, model and settings; with `carry="all"` its shared
memory, and, for an admin, its fleet grant (`_carry`); a channel's own copy
takes none of these.
Its face starts rendering as soon as it exists, so its first post has it.
Whether it may be copied is `authorize(FORK)`'s: an admin's call, and never
a pinned agent. The chat `fork_agent` tool, the CLI and
channel isolation use it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core import agent_lifecycle
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.agent_identity import queue_agent_face
from daimon.core.authz import Action, Subject, authorize, build_agent_ref
from daimon.core.channel_backend import AgentBackendService, CarriedBackend, load_backend_agent
from daimon.core.defaults.ma_index import (
    download_skill_version,
    find_agent_by_daimon_tag,
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
    list_skills_strict,
)
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_CHANNEL_COPY,
    MA_METADATA_KEY_COPY_OF,
    MA_METADATA_KEY_ISOLATED,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    build_metadata,
    strip_tenant_prefix,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import AgentNameCollision, DaimonError, UserFacingError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.memory_resource import (
    COPY_MEMORY_LIMIT,
    CopiedMemory,
    copy_shared_memory,
    ensure_memory_store_and_mount,
)
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import create_agent, retrieve_agent
from daimon.core.neutral_resources import HostAgentCopy, NeutralResourceHost
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import bundle_from_upload
from daimon.core.skills.ownership import AttachedSkillOwnership, partition_copy_skills
from daimon.core.specs import merge_default_agent_toolset
from daimon.core.stores import agent_backends, github_access
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.agent_memory_stores import get_memory_store_id
from daimon.core.stores.domain import AgentBackendRevision, BackendAgent, UserSkillRow
from daimon.core.stores.github_access import AgentRepo
from daimon.core.stores.user_skills import list_user_skills_for_tenant
from mux.contracts.resources import Agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_FORK_COPY_FIELDS = frozenset(
    {"name", "model", "description", "system", "tools", "mcp_servers", "skills", "metadata"}
)


_log = structlog.get_logger(__name__)


type CaptureSource = Callable[[BackendAgent], Awaitable[BackendAgent]]
type CopyCarryScope = Literal["all", "settings"]
"""What a copy takes past its definition, by who makes it.

`"all"` (an admin's copy, or an owner's copy of their own agent) also takes
the shared memory, and, only when the forker is an admin, the fleet grant
and shared threads: a copied grant is a second budget under the same limits,
so the source and the copy can together spend twice. `"settings"` (anyone
else, and any caller that doesn't say) takes the provider, model and
settings only. Either way the provider is carried only by a caller that
passes a `capture_source`.
"""


@dataclass(frozen=True)
class CopyCarry:
    """What a copy took over from its source besides its definition, and what it lacks."""

    source_name: str = ""
    backend: CarriedBackend | None = None
    memory: CopiedMemory | None = None
    memory_failed: bool = False
    reconnect: tuple[tuple[str, str], ...] = ()
    """The source's MCP servers (name, scheme://host) left off: their key stays with the source."""
    github_repos: tuple[str, ...] = ()
    """The source's GitHub repos; the copy has no access to them."""
    kept_back: bool = False
    """A settings-only copy: the source's memory and fleet setup stayed with it."""


@dataclass(frozen=True)
class AgentCopy:
    agent: BetaManagedAgentsAgent
    dropped_skills: tuple[str, ...]
    """Skills left off the copy: another agent's, or the source's own that failed to copy."""
    copied_skills: tuple[str, ...] = ()
    """The source's own skills, uploaded again under the copy's name."""
    carry: CopyCarry = CopyCarry()


def fork_notes(copy: AgentCopy, new_name: str) -> tuple[str, ...]:
    """The copy's receipt: what came with it and what this person must connect again. Pure."""
    from daimon.core.turn.notices import provider_name

    carry, source = copy.carry, copy.carry.source_name or "the original"
    notes: list[str] = []
    carried = carry.backend.revision if carry.backend is not None else None
    if carried is not None and carried.backend is not None:
        model = f" ({carried.model})" if carried.model else ""
        notes.append(f"It runs on {provider_name(carried.backend)}{model}, like {source}.")
    left = carry.backend.left_backend if carry.backend is not None else None
    if left is not None and carry.backend is not None:
        provider = provider_name(left)
        reason = carry.backend.left_reason
        if reason == "turned_off":
            notes.append(
                f"{source} runs on {provider}, which is turned off here, "
                f"so {new_name} runs on Anthropic."
            )
        elif reason == "not_runnable":
            notes.append(
                f"{source}'s {provider} setting can't run here any more, "
                f"so {new_name} runs on Anthropic."
            )
        else:
            notes.append(
                f"{source} runs on {provider}, but {new_name} starts on Anthropic: the switch "
                f'needs confirming for it. To move it, ask in chat "switch {new_name} to '
                f'{provider}".'
            )
    if carry.backend is not None and carry.backend.fleet_left:
        notes.append(f"{source}'s fleet setup can't be used here any more, so {new_name} has none.")
    if carry.kept_back:
        notes.append(f"{source}'s memory and fleet setup stay with {source}.")
    if carry.backend is not None and carry.backend.fleet:
        notes.append(
            f"It has {source}'s fleet setup, with the same limits; each agent spends up to "
            "them on its own."
        )
    memory = carry.memory
    if memory is not None and memory.copied:
        notes.append(
            f"It has a copy of {source}'s shared memory. Personal memories stay with {source}."
        )
        if memory.left or memory.failed:
            total = memory.copied + memory.left + memory.failed
            limit = f" A copy takes up to {COPY_MEMORY_LIMIT}." if memory.left else ""
            notes.append(
                f"Only {memory.copied} of {source}'s {total} memory notes were copied.{limit}"
            )
    if carry.memory_failed or (memory is not None and not memory.copied and memory.failed):
        notes.append(f"{source}'s memory couldn't be copied, so {new_name} starts with none.")
    for server, origin in carry.reconnect:
        at = f" ({origin})" if origin else ""
        notes.append(
            f"{server}{at} needs connecting again with its full address (it's on {source}): "
            f'ask in chat "connect {server} to {new_name}".'
        )
    if carry.github_repos:
        repos = ", ".join(carry.github_repos)
        notes.append(
            f"GitHub access isn't copied ({repos}). To give it a repo, ask in chat "
            f'"give {new_name} access to <repo>".'
        )
    return tuple(notes)


@dataclass(frozen=True)
class _OwnSkill:
    skill_id: str
    body: str
    version: str | None
    upload: UserSkillRow | None


@dataclass(frozen=True)
class _SkillSplit:
    kept: list[dict[str, object]]
    own: list[_OwnSkill]
    dropped: list[str]


async def _split_scoped_skills(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    skills: list[dict[str, object]],
) -> _SkillSplit:
    """Split off the skills scoped to one agent: a copy must not reach into another's."""
    if not any(skill.get("type") == "custom" for skill in skills):
        return _SkillSplit(skills, [], [])
    rows = {row.id: row for row in await list_skills_strict(anthropic)}
    body_by_id = {
        skill_id: body
        for skill_id, row in rows.items()
        if (body := strip_tenant_prefix(tenant_id=tenant_id, display_title=row.display_title or ""))
    }
    async with sessionmaker() as session:
        uploads = await list_user_skills_for_tenant(session, tenant_id=tenant_id)
    upload_by_id = {row.anthropic_id: row for row in uploads if row.anthropic_id}
    agent_names = [
        agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name
        for agent in await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    ]
    attached: list[AttachedSkillOwnership] = []
    for skill in skills:
        skill_id = str(skill.get("skill_id"))
        body = body_by_id.get(skill_id)
        upload = upload_by_id.get(skill_id)
        pinned = skill.get("version")
        attached.append(
            AttachedSkillOwnership(
                id=skill_id,
                custom=skill.get("type") == "custom",
                title=body,
                version=str(pinned) if pinned else None,
                upload_owner=upload.agent_name if upload is not None else None,
            )
        )
    partition = partition_copy_skills(attached, source_name=source_name, agent_names=agent_names)
    own: list[_OwnSkill] = []
    for skill in partition.own:
        # The old consumer read latest_version only for source-owned skills.
        # Shared/catalog rows may be SDK-accepted partial records.
        latest = rows[skill.id].latest_version
        own.append(
            _OwnSkill(
                skill.id,
                skill.title,
                skill.version if skill.version is not None else latest,
                upload_by_id.get(skill.id),
            )
        )
    return _SkillSplit(
        [skills[position] for position in partition.kept_positions],
        own,
        list(partition.dropped),
    )


async def _no_recheck(_agent: BetaManagedAgentsAgent) -> None:
    """The copy was authorized as a whole and no channel reaches it yet."""


async def _copy_own_skills(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    copy: BetaManagedAgentsAgent,
    new_name: str,
    own: list[_OwnSkill],
) -> tuple[list[str], list[str]]:
    """Upload each of the source's own skills again as `copy`'s: (copied, failed)."""
    copied: list[str] = []
    failed: list[str] = []
    for skill in own:
        try:
            if skill.version is None:
                raise DaimonError(f"{skill.body} has no version to copy.")
            data = await download_skill_version(
                anthropic,
                skill_id=skill.skill_id,
                version=skill.version,
                scope=resource_scope(tenant_id=str(tenant_id)),
            )
            # A new skill under the copy's title, never a share of the source's id.
            added = await add_agent_skill(
                anthropic,
                sessionmaker,
                tenant_id=tenant_id,
                agent=await retrieve_agent(
                    anthropic, copy.id, scope=resource_scope(tenant_id=str(tenant_id))
                ),
                agent_name=new_name,
                bundle=bundle_from_upload(data, filename="SKILL.zip"),
                origin=skill.upload.origin if skill.upload else f"copied from {source_name}",
                added_by_account_id=skill.upload.added_by_account_id if skill.upload else None,
                recheck=_no_recheck,
            )
        except (DaimonError, APIError) as exc:
            _log.warning(
                "agent_fork.skill_copy_failed",
                tenant_id=str(tenant_id),
                skill_id=skill.skill_id,
                error=type(exc).__name__,
            )
            failed.append(skill.body)
            continue
        copied.append(f"{new_name}/{added.name}")
    return copied, failed


async def copy_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source: BetaManagedAgentsAgent,
    new_name: str,
    public_url: str | None,
    subject: Subject,
    default_agent_name: str | None,
    extra_metadata: Mapping[str, str] | None = None,
    capture_source: CaptureSource | None = None,
    carry: CopyCarryScope = "settings",
    actor_account_id: uuid.UUID | None = None,
) -> AgentCopy:
    """Create `new_name` as a copy of `source`; raise `DaimonError` if `subject` may not.

    `extra_metadata` is stamped on the copy too (an isolation copy's channel).
    `default_agent_name` is the deployment default, which gets no face.
    `capture_source` captures the copy the way
    its turns will, which a provider switch that dropped features needs to be
    carried (`AgentBackendService.carry_to_copy`). `carry` is chosen by who
    makes the copy (`CopyCarryScope`); unsaid, it is `"settings"`.
    `actor_account_id` is who made it, for the audit rows of what it carries.
    """
    async with sessionmaker() as session:
        try:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        except AccessPolicyUnreadable as exc:
            raise UserFacingError(
                "The access settings can't be read. Ask an admin to check them."
            ) from exc
    source_name = source.metadata.get(MA_METADATA_KEY_NAME) or source.name
    decision = authorize(
        policy,
        subject=subject,
        action=Action.FORK,
        agent=build_agent_ref(source.name, source.metadata),
    )
    if decision.reason == "agent_has_rule":
        # The copy would run anywhere with the prompt and skills of one that may not.
        raise UserFacingError(
            "This agent has an agent rule limiting where it runs, so it can't be copied."
        )
    if not decision:
        raise UserFacingError("Only an admin can copy an agent.")
    source_ma = await retrieve_agent(
        anthropic, source.id, scope=resource_scope(tenant_id=str(tenant_id))
    )
    params = source_ma.model_dump(mode="json")
    fork_params: dict[str, object] = {k: params[k] for k in _FORK_COPY_FIELDS if k in params}
    fork_params["name"] = new_name
    fork_params["metadata"] = build_metadata(
        tenant_id=tenant_id, name=new_name, account_id=derive_guild_account_uuid(tenant_id)
    ) | dict(extra_metadata or {})
    seeded = (
        source_name
        if source_ma.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
        else source_ma.metadata.get(MA_METADATA_KEY_COPY_OF)
    )
    if seeded:
        fork_params["metadata"][MA_METADATA_KEY_COPY_OF] = seeded
    fork_params["mcp_servers"] = merge_default_mcp_server(
        cast("list[BetaManagedAgentsURLMCPServerParams] | None", fork_params.get("mcp_servers")),
        public_url,
    )
    toolset = merge_default_mcp_toolset(
        cast("list[Tool] | None", fork_params.get("tools")), public_url
    )
    # A fork copies raw MA state past `dump_agent_spec`, so the base toolset is guaranteed here.
    fork_params["tools"] = merge_default_agent_toolset(toolset)
    # An isolated reader mounts no secrets; the guidance would send it looking for them.
    if source_ma.metadata.get(MA_METADATA_KEY_ISOLATED) != "true":
        fork_params["system"] = apply_credential_guidance(str(fork_params.get("system") or ""))
    source_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(source.id))
    servers, tools, reconnect = await agent_lifecycle.strip_credentialed_mcp_servers(
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        source_agent_uuid=source_uuid,
        mcp_servers=cast("list[dict[str, object]] | None", fork_params.get("mcp_servers")),
        tools=cast("list[dict[str, object]] | None", fork_params.get("tools")),
    )
    fork_params["mcp_servers"], fork_params["tools"] = servers, tools
    split = await _split_scoped_skills(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        source_name=source_name,
        skills=cast("list[dict[str, object]]", fork_params.get("skills") or []),
    )
    if "skills" in fork_params:
        fork_params["skills"] = split.kept
    created = await create_agent(
        anthropic, fork_params, scope=resource_scope(tenant_id=str(tenant_id))
    )
    queue_agent_face(
        sessionmaker,
        tenant_id=tenant_id,
        agent_name=new_name,
        metadata=created.metadata,
        default_agent_name=default_agent_name,
    )
    carry_args = _CarryArgs(
        tenant_id=tenant_id,
        source_name=source_name,
        source_uuid=source_uuid,
        new_name=new_name,
        channel_copy=MA_METADATA_KEY_CHANNEL_COPY in (extra_metadata or {}),
        carry_all=carry == "all",
        actor_account_id=actor_account_id,
        admin=subject.is_admin and not subject.via_agent_key and not subject.client,
        capture_source=capture_source,
        reconnect=reconnect,
    )
    if not split.own:
        carried = await _carry(anthropic, sessionmaker, created, carry_args)
        return AgentCopy(created, tuple(split.dropped), carry=carried)
    copied, failed = await _copy_own_skills(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        source_name=source_name,
        copy=created,
        new_name=new_name,
        own=split.own,
    )
    try:
        agent = await retrieve_agent(
            anthropic, created.id, scope=resource_scope(tenant_id=str(tenant_id))
        )
    except APIError as exc:
        # The copy exists: raising would leave it orphaned and unreported, so
        # return the create's snapshot; `copied_skills` names what it lacks.
        _log.warning(
            "agent_fork.reread_failed",
            tenant_id=str(tenant_id),
            agent_id=created.id,
            error=type(exc).__name__,
        )
        agent = created
    carried = await _carry(anthropic, sessionmaker, agent, carry_args)
    return AgentCopy(agent, (*split.dropped, *failed), tuple(copied), carried)


@dataclass(frozen=True)
class _CarryArgs:
    tenant_id: uuid.UUID
    source_name: str
    source_uuid: uuid.UUID
    new_name: str
    channel_copy: bool
    carry_all: bool
    admin: bool
    actor_account_id: uuid.UUID | None
    capture_source: CaptureSource | None
    reconnect: tuple[tuple[str, str], ...]


async def _carry(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    copy: BetaManagedAgentsAgent,
    args: _CarryArgs,
) -> CopyCarry:
    """Give the new copy what lives outside its definition. Never raises: the copy exists.

    Only a copy made with `carry="all"` takes the shared memory (what the
    source's people told it), and only an admin's such copy the fleet grant
    (a second budget under the same limits). The provider setting comes only
    with a `capture_source`, so a chat fork stays as it was until chat passes
    one. A channel's own copy takes none of it: it exists to keep a channel
    apart, and it has no capture to re-take a switch's consent with. Who owns
    the copy is recorded apart, by the owner rule.

    The provider comes last. A carried consent is pinned to everything the
    copy's capture reads, its shared memory store among them, so the store is
    made first and the pin is checked again once written
    (`AgentBackendService.recheck_copy_consent`).
    """
    full = args.carry_all and not args.channel_copy
    tenant_id = args.tenant_id
    copy_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(copy.id))
    log_fields = {"tenant_id": str(tenant_id), "agent_id": copy.id}
    source_revision: AgentBackendRevision | None = None
    source_store: str | None = None
    try:
        async with sessionmaker() as session:
            source_revision = await agent_backends.current(session, tenant_id, args.source_uuid)
            source_store = await get_memory_store_id(
                session, tenant_id=tenant_id, agent_id=args.source_uuid
            )
    except Exception as exc:
        # Best effort from here on: the copy exists, and raising would report it as not made.
        _log.warning(
            "agent_fork.source_read_failed", error=type(exc).__name__, **log_fields, exc_info=True
        )
    memory: CopiedMemory | None = None
    memory_failed = False
    if full:
        try:
            memory = await copy_shared_memory(
                anthropic,
                sessionmaker,
                tenant_id=tenant_id,
                source_agent_id=args.source_uuid,
                copy_agent_id=copy_uuid,
                copy_name=args.new_name,
            )
        except Exception as exc:
            _log.warning(
                "agent_fork.memory_copy_failed",
                error=type(exc).__name__,
                **log_fields,
                exc_info=True,
            )
            memory_failed = True
    backend: CarriedBackend | None = None
    # Only a caller that can capture the copy carries its provider (the CLI today):
    # without one a chat fork stays as it was, on the default provider, until chat
    # can capture it and show the receipt.
    if source_revision is not None and not args.channel_copy and args.capture_source is not None:
        capture: CaptureSource | None = args.capture_source
        if source_revision.drop_consent is not None:
            try:
                # The pin reads the copy's shared store: it must exist before the capture.
                await ensure_memory_store_and_mount(
                    anthropic,
                    sessionmaker,
                    tenant_id=tenant_id,
                    agent_id=copy_uuid,
                    agent_name=args.new_name,
                )
            except Exception as exc:
                # No stable pin without it: the switch waits to be confirmed again.
                _log.warning(
                    "agent_fork.memory_store_failed",
                    error=type(exc).__name__,
                    **log_fields,
                    exc_info=True,
                )
                capture = None
        try:
            # `copy` is the definition as created: mounting a memory store binds it
            # in the database and leaves the agent itself unchanged, and the capture
            # retrieves the agent afresh anyway.
            backend_agent = await load_backend_agent(
                anthropic,
                sessionmaker,
                tenant_id=tenant_id,
                agent_name=args.new_name,
                actual_agent=copy,
            )
            service = AgentBackendService(sessionmaker, enabled=True, capture_source=capture)
            backend = await service.carry_to_copy(
                source_revision,
                backend_agent,
                # A copied grant is a second budget under the same limits: an admin's call.
                fleet_grant=full and args.admin,
                shared_threads=full and args.admin,
                account_id=args.actor_account_id,
                now=datetime.now(UTC),
            )
            if backend.revision is not None and not await service.recheck_copy_consent(
                backend_agent, backend.revision, account_id=args.actor_account_id
            ):
                backend = CarriedBackend(
                    None,
                    backend.revision.backend,
                    fleet=backend.fleet,
                    left_reason="needs_consent",
                    fleet_left=backend.fleet_left,
                )
        except Exception as exc:
            _log.warning(
                "agent_fork.backend_carry_failed",
                error=type(exc).__name__,
                **log_fields,
                exc_info=True,
            )
    repos: list[AgentRepo] = []
    try:
        async with sessionmaker() as session:
            repos = await github_access.list_agent_repos(
                session, tenant_id=tenant_id, agent_id=args.source_uuid
            )
    except Exception as exc:
        _log.warning(
            "agent_fork.repos_read_failed", error=type(exc).__name__, **log_fields, exc_info=True
        )
    has_fleet = source_revision is not None and source_revision.fleet_grant is not None
    return CopyCarry(
        source_name=args.source_name,
        backend=backend,
        memory=memory,
        memory_failed=memory_failed,
        reconnect=args.reconnect,
        github_repos=tuple(repo.full_name for repo in repos if repo.status == "active"),
        # Said only when the source has a memory or a fleet setup to keep back.
        kept_back=not full and not args.channel_copy and (has_fleet or source_store is not None),
    )


async def fork_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    new_name: str,
    public_url: str | None,
    subject: Subject,
    default_agent_name: str | None,
    extra_metadata: Mapping[str, str] | None = None,
    carry: CopyCarryScope = "settings",
) -> AgentCopy:
    """Copy the agent named `source_name` to `new_name`; raise `DaimonError` if either is wrong."""
    if await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=new_name):
        raise AgentNameCollision(f"An agent named {new_name} already exists. Pick another name.")
    source = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=source_name)
    if source is None:
        raise UserFacingError("The agent you want to copy is missing. Choose another agent.")
    return await copy_agent(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        source=source,
        new_name=new_name,
        public_url=public_url,
        subject=subject,
        default_agent_name=default_agent_name,
        extra_metadata=extra_metadata,
        carry=carry,
    )


async def fork_host_agent(
    host: NeutralResourceHost,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    source_name: str,
    new_name: str,
    subject: Subject,
    default_agent_name: str | None,
    public_url: str | None = None,
    extra_metadata: Mapping[str, str] | None = None,
) -> HostAgentCopy:
    """The same fork policy, with explicit neutral catalog/native resources."""
    tenant_id = uuid.UUID(host.scope.tenant_id)
    source = await host.find(source_name)
    if source is None:
        raise UserFacingError("The agent you want to copy is missing. Choose another agent.")

    async def recheck(agent: Agent) -> None:
        async with sessionmaker() as session:
            try:
                policy = await load_access_policy(session, tenant_id=tenant_id)
            except AccessPolicyUnreadable as exc:
                raise UserFacingError(
                    "The access settings can't be read. Ask an admin to check them."
                ) from exc
        decision = authorize(
            policy,
            subject=subject,
            action=Action.FORK,
            agent=build_agent_ref(agent.spec.name, agent.spec.metadata or {}),
        )
        if decision.reason == "agent_has_rule":
            raise UserFacingError(
                "This agent has an agent rule limiting where it runs, so it can't be copied."
            )
        if not decision:
            raise UserFacingError("Only an admin can copy an agent.")

    await recheck(source.revision.agent)
    made = await host.fork(
        source.revision.catalog_id,
        new_name,
        recheck=recheck,
        public_url=public_url,
        extra_metadata=extra_metadata,
    )
    queue_agent_face(
        sessionmaker,
        tenant_id=tenant_id,
        agent_name=new_name,
        metadata=made.agent.metadata,
        default_agent_name=default_agent_name,
    )
    return made


__all__ = [
    "AgentCopy",
    "CaptureSource",
    "CopyCarry",
    "CopyCarryScope",
    "copy_agent",
    "fork_agent",
    "fork_host_agent",
    "fork_notes",
]
