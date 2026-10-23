"""Agent tools: list / get / create / update / fork / archive.

``register_agent_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.
"""

from __future__ import annotations

import contextlib
import datetime
import uuid
from collections.abc import Mapping
from functools import partial
from typing import Any, Final, Literal, cast

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_agent import Tool as MATool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import reachability
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_policy import require_agent_creatable, turn_origin_place
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._owner import (
    caller_owned_ids,
    caller_owns_agent,
    may_own,
    owned_in,
)
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools._rule_view import (
    load_caller_view,
    load_isolation_view,
    load_skill_owners,
)
from daimon.adapters.mcp.tools._words import admin_noun, space_noun
from daimon.adapters.mcp.tools.agent_chat import require_outside_chat_turn
from daimon.adapters.mcp.tools.setup_target import (
    get_chat_origin,
    origin_channel_id,
    resolve_setup_agent,
)
from daimon.core import channel_backend
from daimon.core.agent_fork import copy_agent
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_lifecycle import forget_archived_agent
from daimon.core.agent_mcp_credentials import agent_mcp_write_lock
from daimon.core.agent_ownership import agent_name_lock, record_agent_creator
from daimon.core.constants import AGENT_MCP_CAP, AGENT_SKILL_CAP, ALLOWED_MODEL_IDS
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.defaults.ma_index import (
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
)
from daimon.core.defaults.mcp_merge import (
    DAIMON_MCP_SERVER_NAME,
    get_reserved_mcp_rejection,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_SETUP_HIDDEN,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.reconcile_agents import AgentNameTaken, reconcile_agent
from daimon.core.defaults.report import Action as ReconcileAction
from daimon.core.defaults.skills import resolve_custom_skill_titles, resolve_skill_names
from daimon.core.defaults.spec_merge import merge_mcp_servers_with_ma, merge_skills_with_ma
from daimon.core.errors import DaimonError, DefaultsError
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_attach import (
    McpServerReplaceRefusedError,
    attach_mcp_server_to_agent,
    decide_mcp_replacement,
    replaced_server_url,
)
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.model_presets import ModelSelection, model_choice_id
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_agent, retrieve_agent, update_agent
from daimon.core.routing_facts import build_unrouted_note
from daimon.core.skill_sync import SyncRepoFailure, sync_agent_skills, sync_report_failures
from daimon.core.specs import (
    AgentSpec,
    SkillRepo,
    merge_default_agent_toolset,
)
from daimon.core.stores.domain import BackendAgent, TurnOriginRow
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import DEFAULT_BACKEND, ThreadMode
from mux.contracts.ids import Provider
from mux.contracts.profile import Capability
from mux.profiles import selectable_models
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

log = structlog.get_logger()


class AgentMcpServerInfo(BaseModel):
    name: str
    url: str


class AgentSkillInfo(BaseModel):
    # Plain str, NOT Literal["anthropic", "custom"] — upstream-controlled
    # value set; MA may ship a skill type the pinned SDK does not model.
    type: str
    skill_id: str
    name: str | None
    version: str


class AgentInfo(BaseModel):
    name: str
    id: str
    description: str | None
    model: str
    created_at: datetime.datetime | None
    """``None`` means withheld from a non-admin caller (setup detail)."""
    mcp_servers: list[AgentMcpServerInfo] | None
    """Attached server names and URLs. ``None`` means withheld from a non-admin
    caller; ``[]`` means the agent has none."""
    skills: list[AgentSkillInfo]
    sync_warnings: list[SyncRepoFailure] | None = None
    system: str | None = None
    """Set only by ``get_agent``, and only for an admin caller on an agent chat
    tools may edit: the full system prompt (``""`` when it has none). ``None``
    means withheld, not empty — a non-admin caller, or a defaults-managed or
    system agent."""
    applies: str | None = None
    """Set only by ``update_agent`` when it changed ``model`` and/or ``system``:
    person-facing confirmation that the change reaches this conversation on
    its next message, not the one running now."""
    answering: str | None = None
    """Set only by ``create_agent`` and ``fork_agent``, and only when nothing
    routes to the new agent yet: post it verbatim, so the person learns the
    agent exists but answers nowhere and what to say to change that."""
    dropped_skills: list[str] | None = None
    """Set only by ``fork_agent``: skills left off the copy (another agent's, or the
    source's own that failed to copy; its own are otherwise copied). Tell the person."""
    copied_skills: list[str] | None = None
    """Set only by ``fork_agent``: the source's own skills, uploaded again under the
    copy's name. The copy has them even when ``skills`` does not list them yet; do
    not add them again."""
    owned_by_you: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    """Set by ``get_agent`` and ``list_agents``: whether the caller owns this agent
    (created it, or an admin named them), so may change it without an admin."""

    @classmethod
    def from_ma(
        cls,
        agent: BetaManagedAgentsAgent,
        *,
        sync_warnings: list[SyncRepoFailure] | None = None,
        skill_titles: Mapping[str, str] | None = None,
    ) -> AgentInfo:
        titles = skill_titles or {}
        return cls(
            name=agent.name,
            id=agent.id,
            description=agent.description,
            model=model_choice_id(agent.model),
            created_at=agent.created_at,
            mcp_servers=[AgentMcpServerInfo(name=s.name, url=s.url) for s in agent.mcp_servers],
            skills=[
                AgentSkillInfo(
                    type=sk.type,
                    skill_id=sk.skill_id,
                    name=titles.get(sk.skill_id),
                    version=sk.version,
                )
                for sk in agent.skills
            ],
            sync_warnings=sync_warnings,
        )

    def for_member(self) -> AgentInfo:
        """Capabilities only: drop the setup details a non-admin must not see.

        Name, description, model, skills and the agent id stay. The id is the
        identity chat callers pass back as ``expected_ma_agent_id``; the rest
        (attached MCP servers and their URLs, creation date, sync warnings) is
        how the agent was set up, which is for admins.
        """
        return self.model_copy(
            update={"created_at": None, "mcp_servers": None, "sync_warnings": None}
        )


async def _build_agent_info(
    client: AsyncAnthropic,
    agent: BetaManagedAgentsAgent,
    *,
    tenant_id: uuid.UUID,
    sync_warnings: list[SyncRepoFailure] | None = None,
) -> AgentInfo:
    """Map an MA agent to ``AgentInfo`` with custom skill names resolved."""
    skill_titles, _truncated = await resolve_custom_skill_titles(
        client, agents=[agent], tenant_id=tenant_id
    )
    return AgentInfo.from_ma(agent, sync_warnings=sync_warnings, skill_titles=skill_titles)


async def _with_answering_note(
    runtime: McpRuntime, auth: AuthIdentity, info: AgentInfo
) -> AgentInfo:
    """Attach the routing handoff when nothing in the tenant routes to ``info``.

    A freshly created or forked agent exists but answers nowhere, and the
    person who asked for it reliably expects to be able to talk to it by name.
    One config-cascade read decides; a reachable agent gets no note.
    """
    async with runtime.session_factory() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=auth.tenant_id,
            agent_name=info.name,
            default=runtime.deployment_default,
        )
    if reachable:
        return info
    return info.model_copy(
        update={
            "answering": build_unrouted_note(
                agent_name=info.name, channel_label=None, is_admin=auth.is_admin
            )
        }
    )


_CREATE_FIELDS: Final = frozenset(
    {
        "name",
        "model",
        "description",
        "system",
        "tools",
        "mcp_servers",
        "metadata",
        # "skills" excluded — create_agent rejects non-empty skills until the
        # skills tool group ships; attach skills via update_agent instead.
    }
)


_DEFAULT_MCP_TOOLSET_CONFIG: Final[dict[str, Any]] = {
    "permission_policy": {"type": "always_allow"},
}


def _ma_tool_to_param(tool: MATool) -> Tool:
    """Dump an MA response Tool to a Params dict suitable for the SDK update body."""
    return cast(Tool, tool.model_dump(mode="json", exclude_none=True))


def _union_tools(spec_tools: list[Tool], ma_agent: BetaManagedAgentsAgent) -> list[Tool]:
    """Union caller's tools with MA's existing tools.

    Caller wins on collision; MA-only entries are appended in MA order. Keying:

    * `mcp_toolset`     — by `mcp_server_name`
    * `agent_toolset_20260401` — singleton (MA allows only one)
    * `custom`         — by `name`

    The chat `update_agent` is an
    additions surface (panel handles removals), so a per-field replace would
    drop everything the user didn't explicitly resend. Mirror the panel,
    which goes through `reconcile_agent`'s merge helpers.
    """
    spec_mcp_names: set[str] = set()
    spec_custom_names: set[str] = set()
    spec_has_agent_toolset = False
    for tool in spec_tools:
        ttype = tool.get("type")
        if ttype == "mcp_toolset":
            name = tool.get("mcp_server_name")
            if isinstance(name, str):
                spec_mcp_names.add(name)
        elif ttype == "agent_toolset_20260401":
            spec_has_agent_toolset = True
        elif ttype == "custom":
            name = tool.get("name")
            if isinstance(name, str):
                spec_custom_names.add(name)

    extras: list[Tool] = []
    for entry in ma_agent.tools:
        if entry.type == "mcp_toolset":
            if entry.mcp_server_name in spec_mcp_names:
                continue
        elif entry.type == "agent_toolset_20260401":
            if spec_has_agent_toolset:
                continue
        elif entry.type == "custom" and entry.name in spec_custom_names:
            continue
        extras.append(_ma_tool_to_param(entry))
    return list(spec_tools) + extras


def _reject_system_agent(agent: BetaManagedAgentsAgent) -> None:
    """Reject defaults-owned agents from chat mutating tools.

    Unconditional — applies to admins too, with no bypass. A chat edit never
    stamps the seeded agent's spec hash, so the reconcile pipeline's hash
    short-circuit skips the drifted agent forever the next time defaults are
    applied: the drift is permanent and `daimon defaults apply` cannot repair
    it. Forking is the edit path.

    Two markers, because either one on its own leaks:

    - `daimon_managed="true"` is the reconciler's own provenance stamp and the
      authoritative one. Keying on the absence of `daimon_account` alone did
      NOT work: the guild seed path account-stamps seeded agents, so the
      account key is present on them and this guard silently never fired. The
      Discord panel hit the same trap and moved to this marker; the MCP
      tools did not follow until now. Panel forks stamp `managed=False` and
      chat/CLI creates leave it unset, so both stay editable.
    - a missing `daimon_account` still rejects, preserving cover for older
      unstamped seeded agents that predate the account stamp.
    """
    rejection = _system_agent_rejection(agent)
    if rejection is not None:
        raise ToolError(rejection)


def _system_agent_rejection(agent: BetaManagedAgentsAgent) -> str | None:
    """Return why chat tools cannot modify ``agent``, or ``None`` if they can."""
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        return (
            f"agent '{agent.name}' is managed by defaults; chat tools cannot modify it. "
            "Anyone talking to it can make their own editable copy with fork_agent (an "
            "admin can copy it from anywhere), or create_agent a new one."
        )
    if agent.metadata.get(MA_METADATA_KEY_ACCOUNT) is None:
        return (
            f"agent '{agent.name}' is a system agent; chat tools cannot modify it. "
            "Anyone talking to it can make their own editable copy with fork_agent (an "
            "admin can copy it from anywhere), or create_agent a new one."
        )
    return None


def _setup_hidden(agent: BetaManagedAgentsAgent, auth: AuthIdentity) -> bool:
    """A member's own copy of an agent they talked to: its inherited setup stays an
    admin's to read, as on the source, though they own it."""
    return not auth.is_admin and agent.metadata.get(MA_METADATA_KEY_SETUP_HIDDEN) == "true"


async def _reject_guild_name_collision(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> None:
    """Raise ToolError if any non-archived agent with this name already exists in the tenant.

    Tenant-scoped name uniqueness matches the resolver's (daimon_tenant, daimon_name)
    identity model exactly (ma_index keys on tenant+name only); legacy personal-stamped
    agents now also block. Any non-empty match raises regardless of owner.
    """
    matches = await find_agents_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if matches:
        raise ToolError(
            f"agent '{name}' already exists in this {space_noun(auth.platform)} — pick another name"
        )


async def _free_agent_name(runtime: McpRuntime, auth: AuthIdentity, name: str) -> str:
    """`name`, or the first of `name-2`, `name-3`... that no live agent here holds."""
    rows = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    taken = {row.metadata.get(MA_METADATA_KEY_NAME) for row in rows}
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{name}-{n}"
    return candidate


async def _list_agents_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    page: str | None,
    origin_context_id: str | None = None,
    mine: bool = False,
) -> list[AgentInfo]:
    del page
    rows = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    owned = await caller_owned_ids(runtime, auth)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    caller = await load_caller_view(
        runtime,
        auth,
        agents=rows,
        location_channel_id=origin_channel_id(origin),
        location_thread_id=origin.thread_id if origin is not None else None,
    )
    # An owner always sees their own agents, routed anywhere or nowhere.
    rows = [
        row
        for row in rows
        if (owned_in(owned, auth, row.id) or (not mine and caller.sees_agent(row)))
    ]
    skill_titles, _truncated = await resolve_custom_skill_titles(
        runtime.client, agents=rows, tenant_id=auth.tenant_id
    )
    infos = [
        AgentInfo.from_ma(a, skill_titles=skill_titles).model_copy(
            update={"owned_by_you": owned_in(owned, auth, a.id)}
        )
        for a in rows
    ]
    if auth.is_admin:
        return infos
    hidden = {row.id for row in rows if _setup_hidden(row, auth)}
    return [
        info if info.owned_by_you and info.id not in hidden else info.for_member() for info in infos
    ]


async def _get_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    expected_ma_agent_id: str | None = None,
    origin_context_id: str | None = None,
) -> AgentInfo:
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=name,
        expected_ma_agent_id=expected_ma_agent_id,
        require_identity=False,
        discovery=True,
        location_channel_id=origin_channel_id(origin),
        location_thread_id=origin.thread_id if origin is not None else None,
    )
    info = await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)
    # The prompt is readable only by callers who could replace it: admins and the
    # agent's owner, on an agent chat tools may edit at all. A defaults-managed or system
    # agent's prompt stays withheld even from admins — the edit path is a fork,
    # and the fork's own prompt is then readable.
    owned = await caller_owns_agent(runtime, auth, agent.id)
    sees_setup = auth.is_admin or (owned and not _setup_hidden(agent, auth))
    if sees_setup and _system_agent_rejection(agent) is None:
        info = info.model_copy(update={"system": agent.system or ""})
    if not sees_setup:
        info = info.for_member()
    return info.model_copy(update={"owned_by_you": owned})


def _reject_unknown_model(model: ModelSelection) -> None:
    """Raise unless ``model`` is one this deployment meters and allows.

    The panel paths have validated free-text model input against
    ALLOWED_MODEL_IDS; the chat paths never did, so a typo or a
    hallucinated id was accepted here and only surfaced later as a session that
    would not start. An unpriced id is also invisible to cost accounting —
    ``pricing.cost_of`` returns None for a model it has no rates for, so the
    turn is billed by Anthropic and recorded as free by us.
    """
    choice_id = model_choice_id(model)
    if choice_id not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        raise ToolError(f"Model '{choice_id}' is not available. Choose one of: {allowed}")


async def _move_this_thread(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    agent_name: str,
    target: Provider,
    *,
    pinned_here: bool,
) -> str:
    """After a confirmed switch, move the requester's own slot of this thread too.

    Only when the switched agent is the one answering here, and never a shared
    thread (it stays pinned for everyone in it). Says plainly what happens.
    """
    from daimon.core.stores import mux_state, thread_agent_bindings
    from daimon.core.thread_release import caller_slot, request_thread_release
    from daimon.core.turn.notices import provider_name

    provider = provider_name(target)
    if pinned_here:
        return "This channel pins its own provider, so this conversation stays on it."
    async with runtime.session_factory() as session:
        handed = await thread_agent_bindings.get_binding(
            session,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
        )
    # Who answers this conversation's next message: a hand-off's agent, else this
    # turn's responder. A setup conversation's configuration target never moves it.
    answering = (
        handed.responder_name
        if handed is not None and handed.kind == "handoff"
        else origin.responder_name
    )
    if (handed is not None and handed.kind == "setup") or agent_name != answering:
        return f"{agent_name} isn't the one answering here, so this conversation is unchanged."
    slot = caller_slot(
        tenant_id=auth.tenant_id,
        platform=origin.platform,
        channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        account_id=auth.account_id,
    )
    async with runtime.session_factory.begin() as session:
        shared = slot.model_copy(update={"account_id": None})
        if await mux_state.get_binding(session, shared) is not None:
            return (
                "This conversation is shared, so it stays on its current model; "
                f"start a new thread to talk to {agent_name} on {provider}."
            )
        pinned = await mux_state.get_binding(session, slot, include_released=True)
        await request_thread_release(
            session,
            slot,
            generation=pinned.generation if pinned is not None else 0,
            now=datetime.datetime.now(datetime.UTC),
        )
    return (
        f"This conversation moves to {provider} from the next message; "
        "earlier messages here aren't carried over."
    )


def _resolve_native_agent_model(model: ModelSelection) -> ModelSelection:
    """Native agent edits share the alias resolver without changing model controls."""
    choice = model_choice_id(model)
    if choice in ALLOWED_MODEL_IDS:
        return model
    from daimon.core.agent_model_names import agent_catalog, model_name, resolve_model
    from daimon.core.turn.notices import provider_name
    from mux.profiles import MANAGED_AGENTS

    choices = tuple(
        entry.model for entry in agent_catalog() if entry.model.provider == MANAGED_AGENTS.provider
    )
    try:
        resolved = resolve_model(choice, available=choices).id
    except ValueError as exc:
        # Another provider's model: say how to get there instead of a dead end.
        with contextlib.suppress(ValueError):
            other = resolve_model(choice)
            if other.provider != MANAGED_AGENTS.provider:
                raise ToolError(
                    f"{model_name(other)} is a {provider_name(other.provider)} model. Create "
                    "the agent on a Claude model such as sonnet, then switch it to "
                    f"{provider_name(other.provider)} ({other.id}). Nothing was created yet."
                ) from exc
        if "could mean" not in str(exc):
            # Unknown (not ambiguous): the refusal lists what this deployment offers.
            _reject_unknown_model(model)
        raise ToolError(str(exc)) from exc
    return resolved if isinstance(model, str) else {**model, "id": resolved}


def _build_create_spec(
    *,
    name: str,
    model: ModelSelection,
    description: str | None,
    system: str | None,
    tools: list[Tool] | None,
    mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None,
    skill_repos: list[SkillRepo] | None,
) -> AgentSpec:
    """Assemble an ``AgentSpec`` from ``create_agent``'s flat parameters.

    ``create_agent`` takes the same flat parameters as ``update_agent`` rather
    than a single nested ``spec`` object: the two tools disagreeing on shape was
    the top cause of failed chat agent-creation (callers passed ``name``/``model``
    at the top level and hit a ``spec``-missing validation error). A pydantic
    ``ValidationError`` here (e.g. ``mcp_servers`` without a matching
    ``mcp_toolset``) is reshaped into a readable ``ToolError`` rather than leaking
    raw validator output.
    """
    model = _resolve_native_agent_model(model)
    _reject_unknown_model(model)
    try:
        return AgentSpec(
            name=name,
            model=model,
            description=description,
            system=system,
            tools=tools,
            mcp_servers=mcp_servers,
            skill_repos=skill_repos or [],
        )
    except ValidationError as exc:
        raise ToolError(
            "create_agent: invalid agent configuration; check the supplied fields "
            "and ensure every mcp_servers entry has a matching mcp_toolset entry. "
            "Nothing was saved."
        ) from exc


async def _create_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    spec: AgentSpec,
    origin_context_id: str | None = None,
) -> AgentInfo:
    if spec.skills:
        raise ToolError(
            "create_agent: skills must be empty. To add skills, either sync a "
            "repo via skill_repos or use sync_skills after the agent "
            "is created."
        )
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    await require_agent_creatable(
        runtime, auth, origin=turn_origin_place(origin) if origin is not None else None
    )
    owner = may_own(auth)
    async with contextlib.AsyncExitStack() as stack:
        if owner:
            # Check and create under one lock, so a concurrent creator of the same
            # name is refused instead of owning (or rewriting) this agent.
            await stack.enter_async_context(
                agent_name_lock(runtime.session_factory, tenant_id=auth.tenant_id, name=spec.name)
            )
        await _reject_guild_name_collision(runtime, auth, spec.name)
        public_url = (
            str(runtime.settings.mcp.public_url)
            if runtime.settings.mcp.public_url is not None
            else None
        )
        try:
            outcome = await reconcile_agent(
                runtime.client,
                spec,
                tenant_id=auth.tenant_id,
                dry_run=False,
                account_id=derive_guild_account_uuid(auth.tenant_id),
                public_url=public_url,
                # New agents created from chat are user-owned, NOT seeded resources —
                # managed=True would stamp daimon_managed=true and make them
                # sweep-eligible, so the next defaults apply (every boot/deploy)
                # archives them because they aren't in the seeded spec list.
                managed=False,
                references=runtime.session_factory,
                # Never update an agent that took the name since the check above.
                create_only=True,
            )
        except AgentNameTaken as exc:
            raise ToolError(
                f"agent '{spec.name}' already exists in this {space_noun(auth.platform)} "
                "— pick another name"
            ) from exc
        if outcome.anthropic_id is None:
            raise ToolError("create_agent: reconcile returned no agent id — report this as a bug")
        if owner and outcome.action == ReconcileAction.CREATED:
            # The person who created it owns it: they may edit it without an admin.
            await record_agent_creator(
                runtime.session_factory,
                tenant_id=auth.tenant_id,
                agent_id=derive_agent_uuid(
                    tenant_id=auth.tenant_id, ma_agent_id=outcome.anthropic_id
                ),
                account_id=auth.account_id,
            )
    queue_agent_face(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        agent_name=spec.name,
        metadata=None,  # managed=False above: only the default name can make it built-in
        default_agent_name=runtime.deployment_default.agent_name,
    )
    ma_agent = await retrieve_agent(
        runtime.client,
        outcome.anthropic_id,
        scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
    )
    # agents.create succeeded — always return AgentInfo even if sync fails.
    # if agents.create itself raises, let it propagate as ToolError.
    warnings: list[SyncRepoFailure] | None = None
    if spec.skill_repos:
        async with httpx.AsyncClient() as http_client:
            report = await sync_agent_skills(
                principal_id=auth.account_id,
                tenant_id=auth.tenant_id,
                agent_name=spec.name,
                repos=spec.skill_repos,
                sessionmaker=runtime.session_factory,
                fernet=runtime.fernet,
                http_client=http_client,
                anthropic_client=runtime.client,
                app_id=runtime.settings.github_app.app_id,
                app_private_key=runtime.settings.github_app.private_key,
                max_tarball_decompressed_bytes=(
                    runtime.settings.github.max_tarball_decompressed_bytes
                ),
            )
        warnings = sync_report_failures(report) or None
    info = await _build_agent_info(
        runtime.client, ma_agent, tenant_id=auth.tenant_id, sync_warnings=warnings
    )
    return await _with_answering_note(runtime, auth, info)


def _reject_reserved_servers(
    runtime: McpRuntime, mcp_servers: list[BetaManagedAgentsURLMCPServerParams]
) -> None:
    """The same reserved-name guard `attach_mcp_server` applies, for `update_agent`.

    Its merge replaces servers by name, so without this a caller could re-point
    `daimon-mcp` at any URL. Re-sending the canonical entry unchanged (a
    get_agent round trip) is allowed; anything else under the reserved name,
    or the deployment's own URL under another name, is refused.
    """
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    for server in mcp_servers:
        name = str(server.get("name") or "")
        url = str(server.get("url") or "")
        if (
            name == DAIMON_MCP_SERVER_NAME
            and public_url is not None
            and url.rstrip("/") == public_url.rstrip("/")
        ):
            continue
        rejection = get_reserved_mcp_rejection(server_name=name, url=url, public_url=public_url)
        if rejection is not None:
            raise ToolError(f"update_agent: {rejection} Nothing was saved.")


async def _update_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    model: ModelSelection | None,
    description: str | None,
    system: str | None,
    tools: list[Tool] | None,
    mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None,
    skills: list[str | BetaManagedAgentsSkillParams] | None,
    expected_ma_agent_id: str | None = None,
    origin_context_id: str | None = None,
) -> AgentInfo:
    if model is not None:
        model = _resolve_native_agent_model(model)
        _reject_unknown_model(model)
    scalars: dict[str, Any] = {"model": model, "description": description, "system": system}
    list_fields = (tools, mcp_servers, skills)
    if all(v is None for v in scalars.values()) and all(v is None for v in list_fields):
        raise ToolError("update_agent: at least one field is required")
    if mcp_servers is not None:
        _reject_reserved_servers(runtime, mcp_servers)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=name,
        expected_ma_agent_id=expected_ma_agent_id,
        location_channel_id=origin_channel_id(origin),
    )
    _reject_system_agent(agent)
    await require_pin_write_access(runtime, auth, ma_agent=agent, origin=None)

    touched_fields = {field_name for field_name, value in scalars.items() if value is not None}
    if tools is not None:
        touched_fields.add("tools")
    if mcp_servers is not None:
        touched_fields.add("mcp_servers")
    if skills is not None:
        touched_fields.add("skills")
    if touched_fields & reachability.REACHABILITY_GATED_FIELDS:
        await reachability.require_admin_for_reachable_agent(
            runtime, auth, agent_name=name, agent=agent
        )
    mcp_replace_allowed = (
        await _require_mcp_replace_allowed(
            runtime,
            auth,
            agent,
            [(str(entry.get("name")), str(entry.get("url"))) for entry in mcp_servers],
        )
        if mcp_servers is not None
        else False
    )

    # Resolve skill names outside the closure — name resolution does not depend on
    # the agent's current state and must not be repeated on each retry attempt.
    resolved_skills: list[BetaManagedAgentsSkillParams] | None = None
    if skills is not None:
        try:
            caller = await load_isolation_view(
                runtime, auth, location_channel_id=origin_channel_id(origin)
            )
            owners = await load_skill_owners(runtime, caller, auth.tenant_id)
            resolved_skills = await resolve_skill_names(
                runtime.client,
                skills,
                tenant_id=auth.tenant_id,
                is_skill_hidden=lambda skill_id, body: caller.hides_skill(
                    owners, skill_id=skill_id, body=body
                ),
            )
        except DefaultsError as exc:
            raise ToolError(str(exc)) from exc

    # Build scalar patch outside the closure (scalars are caller-supplied, not
    # derived from the agent's current state).
    scalar_patch: dict[str, Any] = {k: v for k, v in scalars.items() if v is not None}
    if "system" in scalar_patch:
        scalar_patch["system"] = apply_credential_guidance(scalar_patch["system"])

    # Version-retry closure. All agent-derived unions (skills, mcp_servers,
    # tools) are recomputed from `fresh` on every attempt so a retry after a stale-
    # version conflict picks up any concurrent mutations rather than re-applying a
    # stale merge. MA treats list fields as per-field replaces; chat tools are an
    # additions surface (dedicated removal tools handle removals), so union caller's values with
    # MA's current state.
    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        patch: dict[str, Any] = dict(scalar_patch)
        if resolved_skills is not None:
            patch["skills"] = merge_skills_with_ma(resolved_skills, fresh)
            merged_skill_count = len(patch["skills"])
            if merged_skill_count > AGENT_SKILL_CAP:
                raise ToolError(
                    f"Cannot attach skills: the merged skill set ({merged_skill_count}) exceeds "
                    f"this organization's per-agent skill limit ({AGENT_SKILL_CAP}). No skills "
                    f"were changed on '{name}'. Attach fewer skills, "
                    "or use remove_skill before adding more."
                )
        if mcp_servers is not None:
            if not mcp_replace_allowed and any(
                replaced_server_url(
                    fresh, server_name=str(entry.get("name")), url=str(entry.get("url"))
                )
                for entry in mcp_servers
            ):
                # Attached under this name at another URL since the check above.
                raise ToolError(
                    f"'{name}' now has one of these server names at another URL; repointing "
                    "it needs an admin. Nothing was changed."
                )
            patch["mcp_servers"] = merge_mcp_servers_with_ma(mcp_servers, fresh)
            # merge_mcp_servers_with_ma's return type is `list | None` at the
            # signature level (None only for a None input), but `mcp_servers`
            # is guaranteed non-None in this branch, so the merged result is
            # never actually None — `or []` only satisfies the static type.
            merged_mcp_count = len(patch["mcp_servers"] or [])
            if merged_mcp_count > AGENT_MCP_CAP:
                raise ToolError(
                    f"Cannot attach connections: the merged set ({merged_mcp_count}) "
                    f"exceeds this organization's per-agent connection limit ({AGENT_MCP_CAP}). "
                    f"Nothing was changed on '{name}'. Attach fewer, or use "
                    "detach_mcp_server before adding more."
                )
        if tools is not None:
            patch["tools"] = _union_tools(tools, fresh)
        # Attaching skills to an agent that lacks agent_toolset_20260401 produces a
        # skills-unusable hole — MA rejects session creation ("skills require the read tool").
        # If this update includes skills, ensure the effective tools list carries the base toolset.
        if "skills" in patch:
            effective_tools: list[Tool] = patch.get("tools") or [
                _ma_tool_to_param(t) for t in fresh.tools
            ]
            has_base_toolset = any(
                entry.get("type") == "agent_toolset_20260401" for entry in effective_tools
            )
            if not has_base_toolset:
                patch["tools"] = merge_default_agent_toolset(effective_tools)
        return await update_agent(
            runtime.client,
            fresh.id,
            version=fresh.version,
            payload=patch,
            scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
        )

    # An MCP server change is serialized with the token forms' attach-then-
    # publish for this agent; other fields need no lock.
    mcp_lock = (
        agent_mcp_write_lock(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id),
        )
        if mcp_servers is not None
        else contextlib.nullcontext()
    )
    try:
        async with mcp_lock:
            updated = await update_agent_with_version_retry(
                runtime.client,
                agent.id,
                _apply,
                scope=resource_scope(
                    tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)
                ),
            )
    except anthropic.ConflictError as exc:
        # Residual conflict after the one retry — surface as a clean ToolError.
        raise ToolError("the agent was modified concurrently — please retry the operation") from exc
    except anthropic.BadRequestError as exc:
        # MA caps skills-per-agent org-wide. When the merged skill set blows
        # the cap, MA 400s and (before this) the raw pydantic/SDK error leaked
        # to chat, prompting the model to silently drop skills. Surface a clear,
        # actionable message instead — and leave the agent untouched.
        if resolved_skills is not None and "exceeds maximum" in str(exc).lower():
            raise ToolError(
                "Cannot attach skills: the merged skill set exceeds this organization's "
                "per-agent skill limit. No skills were changed. Attach fewer skills, or "
                f"use remove_skill on '{name}' before adding more."
            ) from exc
        raise
    result = await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)
    if _setup_hidden(updated, auth):
        # The owner's edit answers with capabilities only: never the inherited setup.
        result = result.for_member()
    applies_lines: list[str] = []
    if model is not None:
        applies_lines.append(
            render_change_confirmation(
                ConfigurationChange(target_name=name, kind="model", availability="next_message")
            )
        )
    if system is not None:
        applies_lines.append(
            render_change_confirmation(
                ConfigurationChange(
                    target_name=name, kind="instructions", availability="next_message"
                )
            )
        )
    if applies_lines:
        result = result.model_copy(update={"applies": "\n".join(applies_lines)})
    return result


async def _require_mcp_replace_allowed(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent: BetaManagedAgentsAgent,
    servers: list[tuple[str, str]],
) -> bool:
    """Gate repointing an existing server name at another URL (`mcp_replace`).

    Returns whether a replacement is authorized, for the fresh-agent re-check
    in the write. Refuses here when one is needed and the caller may not make
    it; the attachment rules count handoff threads and personal defaults as
    shared, which the plain reachability gate does not.
    """
    replaced = [
        name for name, url in servers if replaced_server_url(agent, server_name=name, url=url)
    ]
    if not replaced:
        return False
    outcome = await decide_mcp_replacement(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        platform=auth.platform or "",
        agent=agent,
        caller=reachability.admin_caller(auth),
        default=runtime.deployment_default,
        owned=await caller_owns_agent(runtime, auth, agent.id),
    )
    if outcome != "allow":
        raise ToolError(
            f"'{agent.name}' already has {', '.join(repr(n) for n in replaced)} at another URL "
            f"and is shared, so repointing it needs its owner or {admin_noun(auth.platform)}, "
            "and the caller "
            "is not one. Nothing was changed. Do not retry under another name."
        )
    return True


async def _attach_mcp_server_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    server_name: str,
    url: str,
    expected_ma_agent_id: str | None = None,
    origin_context_id: str | None = None,
) -> AgentInfo:
    # Guard the reserved daimon-mcp entry before even looking at the agent.
    # Also reject any URL that points at the deployment's own public_url under a
    # different name — that would make the next reconcile append a second daimon-mcp.
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    rejection = get_reserved_mcp_rejection(server_name=server_name, url=url, public_url=public_url)
    if rejection is not None:
        raise ToolError(rejection)
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=agent_name,
        expected_ma_agent_id=expected_ma_agent_id,
        location_channel_id=origin_channel_id(origin),
    )
    _reject_system_agent(agent)
    await require_pin_write_access(runtime, auth, ma_agent=agent, origin=None)
    await reachability.require_admin_for_reachable_agent(
        runtime, auth, agent_name=agent_name, agent=agent
    )

    existing = list(agent.mcp_servers or [])
    # No-op check on the initially-found agent (acceptable: a concurrent change
    # between this check and the update is exactly what the version-retry covers).
    for s in existing:
        if s.name == server_name and s.url == url:
            return await _build_agent_info(runtime.client, agent, tenant_id=auth.tenant_id)

    # The spec recompute lives in core.mcp_attach so the Discord
    # credential modal performs the identical write — it must attach the server
    # it just stored a vault credential for, and cannot import this module.
    # The reserved-server guard above is not repeated there: it depends only on
    # caller inputs, so each entry point applies its own policy.
    try:
        # Serialized with the token forms' attach-then-publish for this agent.
        async with agent_mcp_write_lock(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent.id),
        ):
            replace_allowed = await _require_mcp_replace_allowed(
                runtime, auth, agent, [(server_name, url)]
            )
            updated = await attach_mcp_server_to_agent(
                runtime.client,
                agent.id,
                server_name=server_name,
                url=url,
                replace_allowed=replace_allowed,
            )
    except McpServerReplaceRefusedError as exc:
        raise ToolError(str(exc)) from exc
    except anthropic.ConflictError as exc:
        # Residual conflict after the one retry — surface as a clean ToolError.
        raise ToolError("the agent was modified concurrently — please retry the operation") from exc
    info = await _build_agent_info(runtime.client, updated, tenant_id=auth.tenant_id)
    return info.for_member() if _setup_hidden(updated, auth) else info


async def _require_member_copy_room(runtime: McpRuntime, auth: AuthIdentity) -> None:
    """A member may own only so many copies they made by talking to an agent."""
    limit = runtime.settings.mcp.member_copy_limit
    owned = await caller_owned_ids(runtime, auth)
    agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    mine = [
        agent
        for agent in agents
        if owned_in(owned, auth, agent.id)
        and agent.metadata.get(MA_METADATA_KEY_SETUP_HIDDEN) == "true"
    ]
    if len(mine) >= limit:
        raise ToolError(
            f"You already have {len(mine)} copies of your own (the most is {limit}). "
            "Archive one you no longer use, or ask an admin to make this copy. "
            "Nothing was created."
        )


async def _fork_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    source_name: str,
    new_name: str,
    expected_ma_agent_id: str | None = None,
    new_copy_if_taken: bool = False,
) -> AgentInfo:
    # Forking another agent is an admin's call: a fork routes nowhere and has no
    # channel pin of its own, so it is free to run anywhere the source can't. Anyone
    # may copy the agent answering their own chat turn (it is theirs to own; TARGET-V2
    # J10), so only a person's own turn credential gets that far.
    if not auth.is_admin and (auth.chat_agent_id is None or not may_own(auth)):
        _require_admin(auth)
    public_url = runtime.settings.mcp.public_url
    async with contextlib.AsyncExitStack() as stack:
        # Pick, check and create under the name's lock, so two copies at once
        # never both take the same name.
        await stack.enter_async_context(
            agent_name_lock(runtime.session_factory, tenant_id=auth.tenant_id, name=new_name)
        )
        if new_copy_if_taken:
            # "Make a copy of yourself" wants a new copy, never an earlier one.
            picked = await _free_agent_name(runtime, auth, new_name)
            if picked != new_name:
                # A creator naming `picked` outright locks that name, not ours.
                await stack.enter_async_context(
                    agent_name_lock(runtime.session_factory, tenant_id=auth.tenant_id, name=picked)
                )
                new_name = picked
        await _reject_guild_name_collision(runtime, auth, new_name)
        source = await resolve_setup_agent(
            runtime, auth, name=source_name, expected_ma_agent_id=expected_ma_agent_id
        )
        # From the turn's credential, never a model-supplied id.
        answering = (
            auth.chat_agent_id is not None
            and auth.agent_id is None
            and auth.hold is None
            and derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=source.id)
            == auth.chat_agent_id
        )
        if not auth.is_admin and not answering:
            _require_admin(auth)
        if not auth.is_admin:
            await _require_member_copy_room(runtime, auth)
        try:
            copy = await copy_agent(
                runtime.client,
                runtime.session_factory,
                tenant_id=auth.tenant_id,
                source=source,
                new_name=new_name,
                public_url=str(public_url) if public_url is not None else None,
                subject=mcp_subject(auth, is_admin=auth.is_admin),
                default_agent_name=runtime.deployment_default.agent_name,
                # Whoever made the copy owns it.
                owner=auth.account_id if may_own(auth) else None,
                answering=answering,
                hold=auth.hold,
                # A member's copy keeps the source's setup an admin's to read.
                hide_setup=not auth.is_admin,
            )
        except DaimonError as exc:
            raise ToolError(f"fork_agent: {exc} Nothing was created. Do not retry.") from exc
    info = await _build_agent_info(runtime.client, copy.agent, tenant_id=auth.tenant_id)
    if _setup_hidden(copy.agent, auth):
        info = info.for_member()
    if copy.dropped_skills:
        info = info.model_copy(update={"dropped_skills": list(copy.dropped_skills)})
    if copy.copied_skills:
        info = info.model_copy(update={"copied_skills": list(copy.copied_skills)})
    return await _with_answering_note(runtime, auth, info)


async def _archive_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    expected_ma_agent_id: str | None = None,
) -> None:
    _require_admin(auth)
    agent = await resolve_setup_agent(
        runtime, auth, name=name, expected_ma_agent_id=expected_ma_agent_id
    )
    _reject_system_agent(agent)
    await archive_agent(
        runtime.client,
        agent.id,
        scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
    )
    try:
        await archive_memory_store_for_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id)),
        )
    except anthropic.APIError:
        # Best-effort degrade: the agent is already archived and the retry path
        # is dead (archived agents are filtered from lookup), so a transient
        # memory-store archive failure must not strand the agent in a failed
        # state — mirrors the mount-side policy in memory_resource.py.
        log.warning(
            "archive_agent.memory_store_archive_failed",
            tenant_id=str(auth.tenant_id),
            agent_name=name,
            ma_agent_id=agent.id,
        )
    # Outside the best-effort degrade on purpose: a transient memory-store
    # failure must not skip the scope clear, or every turn in the affected
    # channel — or the whole install, for the workspace row — keeps resolving
    # to an archived agent. A failure here surfaces to the tool caller.
    await forget_archived_agent(
        runtime.session_factory, tenant_id=auth.tenant_id, name=name, agent=agent
    )


async def _capture_backend_source(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    source: BackendAgent,
) -> BackendAgent:
    from daimon.adapters.mcp.tools._session_gate import session_card_gap
    from daimon.core.authz import Place
    from daimon.core.ma_resolver import new_resolver_cache
    from daimon.core.scope import ScopeContext
    from daimon.core.turn.admission import capture_backend_source
    from daimon.core.turn.deps import build_turn_deps

    if origin is not None and (
        gap := await session_card_gap(
            runtime,
            auth,
            origin,
            tool_name="request_agent_backend_switch",
            require_public_context=True,
        )
    ):
        # The gap names the cause, in the refusal log line and its copy.
        raise channel_backend.AgentBackendRefused("source_context_unavailable", detail=gap)
    deps = build_turn_deps(
        runtime.settings,
        runtime.client,
        runtime.session_factory,
        deployment_default=runtime.deployment_default,
        resolver_cache=new_resolver_cache(),
        billing_config=None,
    )
    return await capture_backend_source(
        deps,
        source,
        context=ScopeContext(
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=auth.platform,
            channel_id=origin.parent_channel_id if origin is not None else None,
            thread_id=origin.thread_id if origin is not None else None,
        ),
        subject=mcp_subject(auth, is_admin=auth.is_admin),
        place=turn_origin_place(origin) if origin is not None else Place(),
        is_dm=False,
        slack_turn_context_id=None,
        private_dm_id=None,
    )


#: What a target provider can't carry, in plain words (closed obligation labels).
_MISSING_WORDS: Final[Mapping[str, str]] = {
    "credentials": "a saved key that one of its kept connections needs",
    "instructions": "instructions that can't be carried over unchanged",
    "source_definition": "a setting or attachment that provider can't represent",
    "source_context": "setup that couldn't be read completely",
    "source_representation": "a saved version for that provider that is out of date",
    "tools": "tools that must ask before publishing",
    "model_options": "model options that provider doesn't take",
}

#: Why a source read failed for a reason that a retry won't fix.
_SOURCE_DETAIL_WORDS: Final[Mapping[str, str]] = {
    "identity": "this request doesn't come from the account that can change it here",
    "source": "its saved setup doesn't match the agent that's live now. Ask its owner to "
    "check it still exists",
    "environment": "its working environment is missing or archived",
    "place": "this conversation's channel couldn't be verified",
}

#: Why this conversation can't show the switch's Confirm card (a `CardGap`).
_CARD_GAP_WORDS: Final[Mapping[str, str]] = {
    "source_context_unavailable": (
        "I can't confirm a change from a direct message or from a conversation that "
        "started somewhere else. Ask in a server channel or thread where I answer."
    ),
    "no_live_session": (
        "I couldn't find my conversation in this thread, so I can't post the Confirm card. "
        "Mention me again here, or in a new thread."
    ),
    "other_agents_session": (
        "Another agent's conversation runs in this thread, so I can't post the Confirm "
        "card here. Mention me in a new thread."
    ),
    "session_not_gated": (
        "This thread's conversation started without the Confirm card for this change. "
        "Mention me in a new thread."
    ),
    "session_unreadable": (
        "I couldn't check this conversation just now, so I can't post the Confirm card. "
        "Try again in a minute."
    ),
}


def _backend_switch_refusal(
    exc: DaimonError | ValidationError,
    agent_name: str,
    target: Provider | None,
    auth: AuthIdentity,
    *,
    with_code: bool = False,
    current: Provider | None = None,
) -> ToolError:
    """The real reason a provider change was refused, and what the requester can do.

    ``with_code`` appends the stable reason for direct API and coding-tool callers.
    Never shows an exception type or an internal string.
    """
    from daimon.core.turn.notices import provider_name

    refused = exc if isinstance(exc, channel_backend.AgentBackendRefused) else None
    reason = (
        refused.reason
        if refused is not None
        else "invalid_request"
        if isinstance(exc, ValidationError)
        else "backend_unsupported"
        if isinstance(exc, channel_backend.BackendUnsupported)
        else "internal"
    )
    log.warning(
        "agent_backend_switch_refused",
        agent=agent_name,
        target=target,
        reason=reason,
        error_type=type(exc).__name__,
        missing=list(getattr(refused, "missing", ())),
        detail=getattr(refused, "detail", None),
    )
    provider = provider_name(target or getattr(refused, "provider", None))
    admin = admin_noun(auth.platform)
    space = space_noun(auth.platform)
    if reason == "backend_unsupported":
        # Only the copy written for people passes through; internal labels never do.
        said = str(exc)
        text = (
            said
            if said.endswith(".") and " " in said and "_" not in said
            else f"{provider} can't run here right now. Pick another, or ask whoever runs Daimon."
        )
    elif reason == "source_unsupported":
        missing: tuple[str, ...] = getattr(refused, "missing", ())
        things = [_MISSING_WORDS[item] for item in missing if item in _MISSING_WORDS]
        text = (
            f"'{agent_name}' can't move to {provider}: it has "
            + ("; ".join(things) if things else "something that provider can't carry")
            + ". Its owner can remove that from the agent, or keep it on "
            + (provider_name(current) if current is not None else "its current provider")
            + "."
        )
    elif reason == "source_context_unavailable":
        text = _CARD_GAP_WORDS.get(
            getattr(refused, "detail", None) or reason,
            _CARD_GAP_WORDS["source_context_unavailable"],
        )
    elif reason == "source_unavailable":
        detail: str | None = getattr(refused, "detail", None)
        text = (
            f"I can't change '{agent_name}' from here: {_SOURCE_DETAIL_WORDS[detail]}."
            if detail in _SOURCE_DETAIL_WORDS
            else f"I couldn't read '{agent_name}''s current setup (the provider didn't "
            "answer in time). Try again in a minute."
        )
    else:
        text = {
            "agent_not_found": f"There's no agent named '{agent_name}' here. Ask me to list them.",
            "tenant_mismatch": (
                f"'{agent_name}' belongs to another {space} and can't be changed here."
            ),
            "owner_or_admin_required": f"Changing '{agent_name}' needs its owner or {admin}.",
            "admin_required": f"This change needs {admin}.",
            "actor_not_found": (
                "I couldn't match you to an account here. Send a message first, then retry."
            ),
            "admin_required_for_shared_threads": f"Sharing threads in every channel needs {admin}.",
            "feature_disabled": f"Choosing a model provider is turned off. Ask {admin}.",
            "backend_not_allowed": (
                f"{provider} isn't allowed in this {space}. Ask {admin} to allow it."
            ),
            "model_not_selectable": f"That model isn't available on {provider}. Pick another.",
            "profile_not_selectable": f"That model isn't available on {provider}. Pick another.",
            "selfhosted_requires_channel_configuration": (
                f"A self-hosted model is set per channel, not per agent. Ask {admin}."
            ),
            "stale_preview": f"'{agent_name}' changed since the preview. Ask me to preview again.",
            "stale_revision": (
                f"'{agent_name}''s provider changed since the preview. Ask me to preview again."
            ),
            "re_confirm": "This change needs a fresh confirmation. Ask me to preview again.",
            "drops_required": "The confirmation didn't accept what this switch drops. "
            "Ask me to preview again.",
            "drops_mismatch": "What this switch drops changed. Ask me to preview again.",
            "revision_busy": "Another change to this agent is in progress. Retry in a moment.",
            "invalid_request": "That request didn't match the preview. Ask me to preview again.",
        }.get(
            reason,
            "Something on Daimon's side refused this change. "
            "If it repeats, tell whoever runs Daimon.",
        )
    return ToolError(f"{text} Nothing changed." + (f" [{reason}]" if with_code else ""))


async def _provider_now(
    service: channel_backend.AgentBackendService, agent: BackendAgent
) -> Provider:
    """The provider the agent runs on now: its setting, else the default."""
    setting = await service.current(agent)
    return (setting.backend if setting is not None else None) or DEFAULT_BACKEND


def _resolve_agent_model(backend: Provider | None, model: str | None) -> str | None:
    from daimon.core.agent_model_names import resolve_switch_model

    try:
        return resolve_switch_model(backend, model)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _require_selectable_model(backend: Provider | None, model: str | None) -> None:
    """Name the models on offer instead of a generic refusal for an unknown one."""
    if model is None:
        return
    models = selectable_models(backend) if backend is not None else ()
    if model not in models:
        offer = f" Pick one of: {', '.join(models)}." if models else ""
        raise ToolError(f"{model} isn't available on {backend or 'the default'} here.{offer}")


async def _require_agent_editor(
    service: channel_backend.AgentBackendService,
    agent: BackendAgent,
    actor: channel_backend.BackendActor,
    auth: AuthIdentity,
) -> None:
    """The agent's owner or a current admin; the service checks again when it writes."""
    if not await service.may_edit(agent, actor):
        raise ToolError(
            f"Changing '{agent.name}' needs its owner or {admin_noun(auth.platform)}. "
            "Nothing changed. Do not retry."
        )


async def _set_agent_backend_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    backend: Provider | None,
    expected_revision: int | None = None,
    preview_only: bool = False,
    model: str | None = None,
) -> dict[str, JsonValue]:
    model = _resolve_agent_model(backend, model)
    _require_selectable_model(backend, model)
    if auth.agent_id is not None:
        raise ToolError("An agent key cannot change the model provider.")
    try:
        require_outside_chat_turn(auth)
    except ToolError as exc:
        raise ToolError(
            "Use request_agent_backend_switch in chat so the admin can confirm the preview."
        ) from exc
    if not channel_backend.backend_selection_enabled(runtime.settings.turn):
        raise ToolError("Choosing a model provider is turned off. Nothing changed.")
    service = channel_backend.make_backend_service(
        runtime.session_factory,
        runtime.settings.turn,
        capture_source=partial(_capture_backend_source, runtime, auth, None),
    )
    actor = channel_backend.BackendActor(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        subject=mcp_subject(auth, is_admin=auth.is_admin),
    )
    on_now: Provider | None = None
    try:
        agent = await channel_backend.load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=name,
        )
        await _require_agent_editor(service, agent, actor, auth)
        on_now = await _provider_now(service, agent)
        preview = await service.preview(agent, backend, model)
        if preview_only:
            return {
                "agent": name,
                "applied": False,
                "preview": cast(
                    dict[str, JsonValue],
                    TypeAdapter(type(preview)).dump_python(preview, mode="json"),
                ),
                "applies": "No setting was changed. Existing threads and channel overrides remain.",
            }
        current = await channel_backend.load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=name,
        )
        local = preview.setting_revision if expected_revision is None else expected_revision
        if backend is None:
            revision = await service.clear_backend(
                current, local, actor, expected_preview_digest=preview.digest
            )
        else:
            revision = await service.set_backend(
                current, backend, local, actor, expected_preview_digest=preview.digest, model=model
            )
    except (channel_backend.AgentBackendRefused, channel_backend.BackendUnsupported) as exc:
        raise _backend_switch_refusal(
            exc, name, backend, auth, with_code=True, current=on_now
        ) from exc
    return {
        "agent": name,
        "backend": revision.backend,
        "model": revision.model,
        "revision": revision.local,
        "preview": cast(
            dict[str, JsonValue],
            TypeAdapter(type(preview)).dump_python(preview, mode="json"),
        ),
        "applies": "New threads use this setting; existing threads and channel overrides remain.",
    }


class AgentBackendSwitchInfo(BaseModel):
    status: Literal["preview", "changed"]
    agent_name: str
    target_backend: Provider
    model: str | None = Field(default=None, exclude_if=lambda value: value is None)
    models: tuple[str, ...] = ()
    expected_revision: int
    preview_digest: str
    unsupported_skills: tuple[str, ...]
    unsupported_mcps: tuple[str, ...]
    unsupported_capabilities: tuple[str, ...]
    shared_thread_limits: tuple[str, ...]
    cost_line: str
    summary: str
    declared_drops: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    agent_settings: str | None = Field(default=None, exclude_if=lambda value: value is None)
    # This chat's channel pins its own provider, which outranks the agent's setting here.
    channel_pin: str | None = Field(default=None, exclude_if=lambda value: value is None)


async def _channel_pin_line(runtime: McpRuntime, origin: TurnOriginRow) -> str | None:
    """Say so when this chat's channel pins a provider: the agent's switch won't apply there."""
    from daimon.core.channel_backend import channel_ref, current_backend
    from daimon.core.stores import mux_state
    from daimon.core.turn.notices import provider_name

    channel = channel_ref(origin.tenant_id, origin.platform, origin.parent_channel_id)
    async with runtime.session_factory() as session:
        pinned = await current_backend(session, channel)
        if (
            pinned is None
            or await mux_state.config_origin(session, channel, pinned.local) != "channel"
        ):
            return None
    model = f" ({pinned.model})" if pinned.model else ""
    return (
        f"This channel pins {provider_name(pinned.backend)}{model}, so this agent keeps "
        'answering on that here until an admin clears it: say "clear this channel\'s pin".'
    )


async def _request_agent_backend_switch_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    target_backend: Provider,
    expected_revision: int | None = None,
    preview_digest: str | None = None,
    origin_context_id: str | None = None,
    accepted_drops: tuple[str, ...] | None = None,
    model: str | None = None,
    thread_mode: ThreadMode | None = None,
    required_capabilities: tuple[Capability, ...] | None = None,
) -> AgentBackendSwitchInfo:
    model = _resolve_agent_model(target_backend, model)
    _require_selectable_model(target_backend, model)
    if auth.agent_id is not None or auth.platform_user_id is None:
        raise ToolError("Request this change from your active chat conversation.")
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    if origin is None:
        raise ToolError("Request this change from your active chat conversation.")

    from daimon.adapters.mcp.tools._session_gate import session_card_gap
    from daimon.core.channel_backend import (
        BackendActor,
        load_backend_agent,
        make_backend_service,
    )
    from daimon.core.confirmation import (
        BackendSwitchReference,
        reference_settings,
        settings_line,
        stated_settings,
    )
    from pydantic import ValidationError

    on_now: Provider | None = None
    try:
        service = make_backend_service(
            runtime.session_factory,
            runtime.settings.turn,
            capture_source=partial(_capture_backend_source, runtime, auth, origin),
        )
        if not service.enabled:
            raise ToolError("Choosing a model provider is turned off. Nothing changed.")
        gap = await session_card_gap(
            runtime, auth, origin, tool_name="request_agent_backend_switch"
        )
        if gap is not None:
            raise ToolError(
                "This conversation cannot verify the confirmation card. Nothing changed."
            )
        agent = await load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=agent_name,
        )
        await _require_agent_editor(
            service,
            agent,
            BackendActor(
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                subject=mcp_subject(auth, is_admin=auth.is_admin),
            ),
            auth,
        )
        on_now = await _provider_now(service, agent)
        if thread_mode == "shared" and not (auth.is_admin and auth.agent_id is None):
            # The service refuses it too; say so before a card is posted.
            raise ToolError(
                f"Sharing threads applies in every channel, so it needs "
                f"{admin_noun(auth.platform)}. Nothing changed."
            )
        settings = stated_settings(thread_mode, required_capabilities)
        preview = await service.preview(agent, target_backend, model, settings)
        if not preview.allowed:
            raise ToolError("That model provider isn't turned on here. Nothing changed.")
        status: Literal["preview", "changed"] = "preview"
        summary = (
            "Show this compatibility and cost preview. If they want to proceed, call "
            "request_agent_backend_switch again with its expected_revision and preview_digest; "
            "the requester must Confirm on the card. Nothing has changed yet."
        )
        if preview_digest is not None:
            reference = BackendSwitchReference.model_validate(
                {
                    "agent": agent_name,
                    "target_backend": target_backend,
                    "model": model,
                    "expected_revision": expected_revision,
                    "preview_digest": preview_digest,
                    "accepted_drops": accepted_drops,
                    "thread_mode": thread_mode,
                    "required_capabilities": required_capabilities,
                }
            )
            # The card check awaited provider I/O; reread configuration at the write edge.
            agent = await load_backend_agent(
                runtime.client,
                runtime.session_factory,
                tenant_id=auth.tenant_id,
                agent_name=agent_name,
            )
            actor = BackendActor(
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                subject=mcp_subject(auth, is_admin=auth.is_admin),
            )
            from daimon.core.turn.notices import provider_name

            before = await service.current(agent)
            old_provider = provider_name((before.backend if before else None) or DEFAULT_BACKEND)
            await service.set_backend(
                agent,
                target_backend,
                reference.expected_revision,
                actor,
                expected_preview_digest=reference.preview_digest,
                model=reference.model,
                settings=reference_settings(reference),
                **(
                    {"accepted_drops": reference.accepted_drops}
                    if reference.accepted_drops is not None
                    else {}
                ),
            )
            status = "changed"
            summary = (
                f"Model provider changed. New conversations with {agent_name} use "
                f"{provider_name(target_backend)}; other existing threads keep {old_provider}. "
                + await _move_this_thread(
                    runtime,
                    auth,
                    origin,
                    agent_name,
                    target_backend,
                    pinned_here=await _channel_pin_line(runtime, origin) is not None,
                )
            )
        channel_pin = await _channel_pin_line(runtime, origin)
        if channel_pin is not None:
            summary = f"{summary} {channel_pin}"
        return AgentBackendSwitchInfo(
            status=status,
            agent_name=agent.name,
            target_backend=preview.target_backend,
            model=preview.target_model,
            models=selectable_models(preview.target_backend),
            expected_revision=preview.setting_revision,
            preview_digest=preview.digest,
            unsupported_skills=preview.unsupported_skills,
            unsupported_mcps=preview.unsupported_mcps,
            unsupported_capabilities=preview.unsupported_capabilities,
            shared_thread_limits=preview.shared_thread_limits,
            cost_line=preview.cost_line,
            summary=summary,
            declared_drops=preview.declared_drops,
            agent_settings=settings_line(preview.target_settings)
            if preview.target_settings
            else None,
            channel_pin=channel_pin,
        )
    except (DaimonError, ValidationError) as exc:
        raise _backend_switch_refusal(
            exc, agent_name, target_backend, auth, current=on_now
        ) from exc


def register_agent_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    if channel_backend.backend_selection_enabled(runtime.settings.turn):

        @mcp.tool
        async def set_agent_backend(  # pyright: ignore[reportUnusedFunction]
            ctx: Context,
            name: str,
            backend: Provider | None,
            expected_revision: int | None = None,
            preview_only: bool = False,
            model: str | None = None,
        ) -> dict[str, JsonValue]:
            """Set an enabled model provider, or clear the agent setting with backend=null.

            The agent's owner or an admin, for direct coding-tool or API requests. Chat
            turns use request_agent_backend_switch so the requester confirms the preview.
            The returned preview names compatibility limits and list prices.
            Existing threads keep their provider; channel settings still apply.
            expected_revision refuses a setting changed since it was read.
            preview_only returns the compatibility and cost preview without applying it.
            model picks one of the provider's selectable models (Gemini: gemini-3.8-flash or
            gemini-4-argon); omit it for the provider's default.
            """
            return await _set_agent_backend_impl(
                runtime,
                await _auth(ctx),
                name,
                backend=backend,
                expected_revision=expected_revision,
                preview_only=preview_only,
                model=model,
            )

    @mcp.tool
    async def list_agents(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        page: str | None = None,
        origin_context_id: str | None = None,
        mine: bool = False,
    ) -> list[AgentInfo]:
        """List agents visible here, including their attached ``mcp_servers`` and ``skills``.

        Each agent says ``owned_by_you``: the caller owns it and may change it without
        an admin. ``mine=true`` lists only the caller's own agents ("which agents do I
        own?"), wherever they answer, including ones that answer nowhere yet.
        ``mcp_servers`` and ``created_at`` are null for a caller who is neither an
        admin nor its owner: setup details are for them, capabilities (name,
        description, model, skills) are not. Members see the channel/thread
        responders, agents explicitly allowed to run here, and their own agents;
        other unrouted agents and other channels' agents are hidden.
        Always pass this turn's ``origin_context_id`` to verify its location: without
        it a member sees only their own agents. Admins retain their existing
        visibility. ``page`` is reserved for pagination.
        """
        return await _list_agents_impl(
            runtime, await _auth(ctx), page, origin_context_id, mine=mine
        )

    @mcp.tool
    async def get_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        expected_ma_agent_id: str | None = None,
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Show what an agent can access: attached MCP servers and skills.

        Use ``list_agent_keys`` for stored key names. Configuration does not prove
        the answering session's access. Returns server names/URLs and skills; custom
        skills have a display name (null if deleted), Anthropic skills have a readable id.
        Non-admin callers get null ``mcp_servers`` and ``created_at``.
        For an admin on an editable agent, ``system`` is the full system prompt;
        null means withheld (non-admin caller, or Daimon/defaults-managed agent).
        Pass this turn's ``origin_context_id`` so its channel counts."""
        return await _get_agent_impl(
            runtime,
            await _auth(ctx),
            name,
            expected_ma_agent_id=expected_ma_agent_id,
            origin_context_id=origin_context_id,
        )

    @mcp.tool
    async def create_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        model: ModelSelection,
        *,
        description: str | None = None,
        system: str | None = None,
        tools: list[Tool] | None = None,
        mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None = None,
        skill_repos: list[SkillRepo] | None = None,
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Create an agent called, for example, churn-explorer. Pass fields directly —
        there is NO ``spec`` wrapper.

        Required: ``name`` and ``model``. Use ``"claude-sonnet-5-5"`` when the user
        asks for Sonnet and ``"claude-opus-5-5"`` when they ask for Opus — always
        the current generation. Only pass an older id (``claude-sonnet-5``,
        ``claude-opus-5``, …) when the user names that version themselves.
        Optional: ``description``, ``system`` (the system prompt), ``tools``,
        ``mcp_servers``, and ``skill_repos`` — GitHub repos to sync skills from,
        e.g. ``[{"url": "https://github.com/owner/repo", "branch": "main"}]``.

        Do not pass a ``skills`` field here. To add skills, either sync a repo via
        ``skill_repos`` or use ``sync_skills`` after the agent is created.

        A returned ``answering`` field says the new agent is routed nowhere yet:
        post it verbatim. Always pass this turn's ``origin_context_id``: a chat turn
        without it is refused while a channel in the workspace is confidential, and with
        """
        spec = _build_create_spec(
            name=name,
            model=model,
            description=description,
            system=system,
            tools=tools,
            mcp_servers=mcp_servers,
            skill_repos=skill_repos,
        )
        return await _create_agent_impl(runtime, await _auth(ctx), spec, origin_context_id)

    @mcp.tool
    async def update_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        *,
        model: ModelSelection | None = None,
        description: str | None = None,
        system: str | None = None,
        tools: list[Tool] | None = None,
        mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None = None,
        skills: list[str | BetaManagedAgentsSkillParams] | None = None,
        expected_ma_agent_id: str | None = None,
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Change an agent's system prompt or switch its model; add existing skills such as
        build-models. Scalar ``model``, ``description`` and ``system`` fields replace.

        Prefer current-generation ``claude-sonnet-5-5`` for Sonnet and ``claude-opus-5-5``
        for Opus unless an older version is explicitly requested. List fields
        (``tools``, ``mcp_servers``, ``skills``) are added to, never replaced; shorter
        lists remove nothing. Use ``remove_skill`` or ``detach_mcp_server`` to remove.
        Daimon requires ``fork_agent`` first. Needs the agent's owner (whoever created it
        in chat, or whom an admin named) or an admin.

        Prompt, model and skill changes reach a conversation on its next message, not the
        one running now. ``skills`` accepts names such as
        ``["build-models", "compare-models"]``, resolved server-side; explicit
        ``{"type": "custom", "skill_id": "skill_..."}`` entries also work. A model or
        prompt change also returns ``applies``: post it verbatim as part of the reply.
        Pass this turn's ``origin_context_id`` so its channel counts."""
        return await _update_agent_impl(
            runtime,
            await _auth(ctx),
            name,
            model=model,
            description=description,
            system=system,
            tools=tools,
            mcp_servers=mcp_servers,
            skills=skills,
            expected_ma_agent_id=expected_ma_agent_id,
            origin_context_id=origin_context_id,
        )

    @mcp.tool
    async def attach_mcp_server(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        server_name: str,
        url: str,
        expected_ma_agent_id: str | None = None,
        origin_context_id: str | None = None,
    ) -> AgentInfo:
        """Add an MCP server that needs no token, such as Context7, to an agent.

        For Linear or other bearer-authenticated endpoints, use
        ``request_mcp_token`` instead;
        ``detach_mcp_server`` disconnects it. Fork Daimon with ``fork_agent`` before
        directly changing its setup. Needs the agent's owner or an admin.

        Connects the server; its tools are available from the agent's next message,
        not the one running now. Reusing a server name replaces the URL; the same
        name and URL is a no-op. Other connections are preserved.
        Pass this turn's ``origin_context_id`` so its channel counts."""
        return await _attach_mcp_server_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            url=url,
            expected_ma_agent_id=expected_ma_agent_id,
            origin_context_id=origin_context_id,
        )

    @mcp.tool
    async def fork_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        source_name: str,
        new_name: str,
        expected_ma_agent_id: str | None = None,
        new_copy_if_taken: bool = False,
    ) -> AgentInfo:
        """Make a copy of Daimon or another agent that you can edit under a new name.
        Anyone may copy you (the agent answering them), and owns that copy; copying
        any other agent is admin-only. Only for a saved, named copy someone asked
        to keep. The copy keeps its source's tool rules, and starts on the server's
        default fleet (the source's own fleet settings don't carry over). Running
        copies of yourself on several models at once is not this: that is a fleet
        of task workers (fleet_spawn), which needs no admin.
        Copies its prompt, model, skills and the MCP servers that need
        no stored token. The copy starts with no credentials: no repo binding or
        GitHub access, no API/service keys and no connector tokens; attach its own
        with ``request_agent_key`` and the repo tools. An agent an operator pinned
        to channels can't be copied.

        Use ``update_agent`` to edit the copy. Daimon cannot be edited directly.

        Continue configuring it through Daimon with the copy named as the setup
        target. A returned ``answering`` field says the copy is routed nowhere yet:
        post it verbatim.

        ``new_copy_if_taken``: when ``new_name`` is taken, copy under the first free
        ``new_name-2``, ``new_name-3``... instead of refusing; the result's ``name``
        is the copy's: name the copy by it, not ``new_name``. Pass true for "make a
        copy of yourself" asks, where the person named no copy; when the person
        names the copy, leave this false."""
        return await _fork_agent_impl(
            runtime,
            await _auth(ctx),
            source_name,
            new_name,
            expected_ma_agent_id=expected_ma_agent_id,
            new_copy_if_taken=new_copy_if_taken,
        )

    @mcp.tool(tags={"admin"})
    async def archive_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, expected_ma_agent_id: str | None = None
    ) -> None:
        """Delete an agent, for example churn-explorer, by archiving it. Admin-only.

        This removes the agent from the tenant pool and clears its channel/workspace
        defaults so those channels fall back to the next routing tier."""
        await _archive_agent_impl(
            runtime,
            await _auth(ctx),
            name,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    # The shared factory owns the enablement decision and validation.
    from daimon.core.channel_backend import backend_selection_enabled

    if backend_selection_enabled(runtime.settings.turn):
        from daimon.adapters.mcp.tools.fleet_config import register_fleet_config_tools

        register_fleet_config_tools(mcp, runtime)

        @mcp.tool
        async def request_agent_backend_switch(  # pyright: ignore[reportUnusedFunction]
            ctx: Context,
            agent_name: str,
            target_backend: Provider,
            expected_revision: int | None = None,
            preview_digest: str | None = None,
            origin_context_id: str | None = None,
            accepted_drops: tuple[str, ...] | None = None,
            model: str | None = None,
            thread_mode: ThreadMode | None = None,
            required_capabilities: tuple[Capability, ...] | None = None,
        ) -> AgentBackendSwitchInfo:
            """Preview a requested agent backend switch, then Confirm in this chat.

            Use when an admin or the agent's owner asks to switch it to Anthropic, OpenAI
            or Gemini,
            optionally naming a model naturally: "switch Researcher to argon" or
            "switch Researcher to luna". For an Anthropic model change such as
            "set Writer to opus 5.5", use update_agent with that natural model name.
            ``model`` accepts natural names or one of
            the returned ``models``; omit it for the provider's default. A provider
            alone ("switch to GPT", "to Gemini") means its default: omit ``model`` and
            preview; never ask which model. Never ask the person for a model id.
            Ambiguous model names return the choices.
            The switch applies to
            new conversations wherever the agent answers, and to this conversation
            from the next message when the switched agent answers here (the result's
            summary says which); switching the default agent applies to every channel on
            this server. For "make a copy of yourself on Gemini": copy the agent with
            fork_agent and new_copy_if_taken=true (always a NEW copy, never an earlier
            one; use the name and id it returns from here on), hand this conversation
            to the copy with hand_off_task while it is still on this provider, then
            switch the copy here; the confirmed switch moves
            this conversation to Gemini from the next message.
            First call without preview_digest and show the returned compatibility,
            shared-thread limits, what won't be available, and the cost line. Call again
            with the same model and that preview's expected_revision, preview_digest and
            exact declared_drops as accepted_drops: the requester must Confirm
            on the card before anything is changed. A stale preview or expired card
            changes nothing; preview again. Pass this active turn's origin_context_id.
            The same tool states the agent's own settings: ``thread_mode`` ("shared"
            to share threads where the channel allows it, admins only; or "per_caller") and
            ``required_capabilities`` (a channel that can't give one refuses the turn).
            Pass both together; an omitted one returns to the default. To change only
            settings, pass the agent's current target_backend and model.
            """
            return await _request_agent_backend_switch_impl(
                runtime,
                await _auth(ctx),
                agent_name=agent_name,
                target_backend=target_backend,
                expected_revision=expected_revision,
                preview_digest=preview_digest,
                origin_context_id=origin_context_id,
                accepted_drops=accepted_drops,
                model=model,
                thread_mode=thread_mode,
                required_capabilities=required_capabilities,
            )
