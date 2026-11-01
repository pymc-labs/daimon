"""Editing an agent's definition: the write path chat tools and the web app share.

Pure rules (what a toolset turns on, which servers are reserved, which agents no
chat or web edit may touch) sit at the top. The shell below them makes the
Managed Agents writes under one version-retry, so every surface sends the same
update for the same change. Each surface keeps its own resolution and wording;
`update_agent` and `create_agent` add the actor's authorization for callers that
have no gate of their own.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast, get_args

import anthropic
import anyio
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_agent import Tool as MATool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.agent_mcp_credentials import agent_mcp_write_lock
from daimon.core.agent_ownership import agent_name_lock, owns_agent, record_agent_creator
from daimon.core.agent_pins import agent_pin_names
from daimon.core.authz import Action, AgentReach, Subject, Surface, authorize
from daimon.core.channel_rules import ChannelRuleRefused, set_agent_rule
from daimon.core.constants import AGENT_MCP_CAP, AGENT_SKILL_CAP, ALLOWED_MODEL_IDS
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.mcp_merge import DAIMON_MCP_SERVER_NAME, get_reserved_mcp_rejection
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.reconcile_agents import AgentNameTaken, reconcile_agent
from daimon.core.defaults.report import Action as ReconcileAction
from daimon.core.defaults.spec_merge import merge_mcp_servers_with_ma, merge_skills_with_ma
from daimon.core.errors import DaimonError
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import ResolverCache
from daimon.core.mcp_attach import replaced_server_url
from daimon.core.model_presets import ModelSelection, model_choice_id
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_agent, retrieve_agent
from daimon.core.mux_compat import update_agent as ma_update_agent
from daimon.core.permissions import WEB_PLACE_ID, agent_permissions, with_agent_rule
from daimon.core.rule_views import binding_refusal, clear_refusal, render_binding_refusal
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.specs import AgentSpec, merge_default_agent_toolset
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
from daimon.core.stores.tenants import get_tenant
from mux.contracts.errors import ScopeViolation
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

#: The built-in tools an agent's toolset can turn on or off (Managed Agents' names).
BuiltinToolName = Literal[
    "bash", "edit", "read", "write", "glob", "grep", "web_fetch", "web_search"
]
BUILTIN_NAMES: Final[tuple[str, ...]] = get_args(BuiltinToolName)
TOOLSET_TYPE: Final[str] = "agent_toolset_20260401"

AgentEditRefusal = Literal["forbidden", "invalid", "conflict", "stale", "not_found", "taken"]


class AgentEditError(DaimonError):
    """A refused agent edit. The text is the surface's copy; ``kind`` maps to a status."""

    def __init__(self, message: str, *, kind: AgentEditRefusal) -> None:
        super().__init__(message)
        self.kind: AgentEditRefusal = kind


class AgentNameTakenError(AgentEditError):
    """A live agent here already holds the name. Surfaces word it for their platform."""

    def __init__(self, name: str) -> None:
        super().__init__(f"agent '{name}' already exists", kind="taken")
        self.name = name


class AgentDiscardFailedError(DaimonError):
    """A create could not finish and removing the half-made agent failed too."""


@dataclass(frozen=True)
class AgentActor:
    """Who is editing: their account, their authorization subject, and the surface."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    subject: Subject
    surface: Surface

    def scope(self) -> Scope:
        return resource_scope(tenant_id=str(self.tenant_id), account_id=str(self.account_id))


@dataclass(frozen=True)
class AgentEditDeps:
    client: AsyncAnthropic
    session_factory: async_sessionmaker[AsyncSession]
    # The deployment's own MCP URL: the reserved server must keep pointing at it.
    public_url: str | None
    # `tool_safety.denied`: `builtin/<name>` entries forbid a built-in on every agent.
    denied_tools: Collection[str] = ()
    # This process's resolver cache. Other processes see an edit when their
    # 30-second agent entry expires; there is no cross-process bust.
    resolver_cache: ResolverCache | None = None
    # The deployment's own default agent: an agent rule is judged against it.
    deployment_default: DeploymentDefault = DeploymentDefault()


@dataclass(frozen=True)
class AgentPatch:
    """Fields to change; None leaves a field as it is.

    ``merge="union"`` adds list entries to what the agent has (chat tools remove
    with their own tools); ``"replace"`` writes the lists as given (an editor
    that shows the whole config).
    """

    model: ModelSelection | None = None
    description: str | None = None
    system: str | None = None
    tools: list[Tool] | None = None
    mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None = None
    skills: list[BetaManagedAgentsSkillParams] | None = None
    merge: Literal["union", "replace"] = "union"

    def touched(self) -> frozenset[str]:
        fields = {
            "model": self.model,
            "description": self.description,
            "system": self.system,
            "tools": self.tools,
            "mcp_servers": self.mcp_servers,
            "skills": self.skills,
        }
        return frozenset(name for name, value in fields.items() if value is not None)


# ---------- Pure rules ----------


def ma_tool_to_param(tool: MATool) -> Tool:
    """Dump an MA response Tool to a Params dict suitable for the SDK update body."""
    return cast(Tool, tool.model_dump(mode="json", exclude_none=True))


def union_tools(spec_tools: list[Tool], ma_agent: BetaManagedAgentsAgent) -> list[Tool]:
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
        elif ttype == TOOLSET_TYPE:
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
        elif entry.type == TOOLSET_TYPE:
            if spec_has_agent_toolset:
                continue
        elif entry.type == "custom" and entry.name in spec_custom_names:
            continue
        extras.append(ma_tool_to_param(entry))
    return list(spec_tools) + extras


def builtin_enabled(tools: list[Tool], tool: str) -> bool | None:
    """Whether any of the agent's toolsets turns `tool` on; None when it has none."""
    found: bool | None = None
    for entry in tools:
        if not str(entry.get("type", "")).startswith("agent_toolset"):
            continue
        raw = cast(dict[str, Any], entry)
        default = cast(dict[str, Any], raw.get("default_config") or {}).get("enabled")
        on = default is not False
        for config in raw.get("configs") or ():
            if config.get("name") == tool:
                enabled = config.get("enabled")
                on = bool(enabled) if enabled is not None else default is not False
                break
        found = on or bool(found)
    return found


def with_builtin(tools: list[Tool], tool: str, enabled: bool) -> list[Tool]:
    """`tools` with `tool` turned on or off; any permission policy it had is kept.

    An agent with no toolset gains one holding only `tool`, never the defaults.
    """
    if builtin_enabled(tools, tool) is None:
        only: Tool = cast(
            Tool,
            {
                "type": TOOLSET_TYPE,
                "default_config": {"enabled": False},
                "configs": [{"name": tool, "enabled": enabled}],
            },
        )
        return [*tools, only]
    updated: list[Tool] = []
    for entry in tools:
        if not str(entry.get("type", "")).startswith("agent_toolset"):
            updated.append(entry)
            continue
        raw = dict(cast(dict[str, Any], entry))
        configs = [dict(config) for config in raw.get("configs") or ()]
        for config in configs:
            if config.get("name") == tool:
                config["enabled"] = enabled
                break
        else:
            configs.append({"name": tool, "enabled": enabled})
        raw["configs"] = configs
        updated.append(cast(Tool, raw))
    return updated


def explicitly_on(tools: list[Tool], tool: str) -> bool:
    """Whether a toolset in `tools` names `tool` and turns it on (not just by default)."""
    for entry in tools:
        if str(entry.get("type", "")).startswith("agent_toolset"):
            for config in cast(dict[str, Any], entry).get("configs") or ():
                if config.get("name") == tool and config.get("enabled") is True:
                    return True
    return False


def denied_builtins(denied_tools: Collection[str]) -> frozenset[str]:
    """Built-ins whoever runs Daimon forbids: ``builtin/<name>`` in tool-safety ``denied``.

    Only that explicit form: a bare name there keeps meaning an MCP tool.
    """
    return frozenset(name for name in BUILTIN_NAMES if f"builtin/{name}" in denied_tools)


def forbidden_builtins_message(names: Collection[str]) -> str:
    return (
        f"Whoever runs Daimon has turned {', '.join(sorted(names))} off for every agent "
        "here. Nothing changed."
    )


def forbidden_turned_on(denied: frozenset[str], before: list[Tool], after: list[Tool]) -> list[str]:
    """The forbidden built-ins a change from `before` to `after` turns on."""
    return sorted(
        name
        for name in denied
        if builtin_enabled(after, name) and not builtin_enabled(before, name)
    )


def forbidden_builtins_off(denied: frozenset[str], tools: list[Tool]) -> list[Tool]:
    """A new agent's default toolset with every forbidden built-in written as off."""
    if not denied:
        return tools
    tools = merge_default_agent_toolset(tools)
    for name in sorted(denied):
        tools = with_builtin(tools, name, False)
    return tools


def system_agent_rejection(agent: BetaManagedAgentsAgent) -> str | None:
    """Why chat and web edits cannot modify ``agent``, or ``None`` if they can.

    A chat edit never stamps the seeded agent's spec hash, so the reconcile
    pipeline's hash short-circuit would skip the drifted agent forever:
    `daimon_managed="true"` is the reconciler's own stamp, and a missing
    `daimon_account` covers older seeded agents that predate the account stamp.
    """
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


def reserved_server_rejection(
    mcp_servers: list[BetaManagedAgentsURLMCPServerParams], public_url: str | None
) -> str | None:
    """Why a server list may not be written, or None.

    A merge replaces servers by name, so without this a caller could re-point
    `daimon-mcp` at any URL. Re-sending the canonical entry unchanged is allowed;
    anything else under the reserved name, or the deployment's own URL under
    another name, is refused.
    """
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
            return rejection
    return None


def unknown_model_refusal(model: ModelSelection) -> str | None:
    """Why ``model`` may not be set, or None when this deployment meters and allows it.

    An unpriced id is invisible to cost accounting: ``pricing.cost_of`` returns
    None for a model it has no rates for, so the turn would be recorded as free.
    """
    choice_id = model_choice_id(model)
    if choice_id not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        return f"Model '{choice_id}' is not available. Choose one of: {allowed}"
    return None


@dataclass(frozen=True)
class PlaceEdit:
    """One place an agent answers: a channel id (or the web chat) and whether it is the default."""

    channel_id: str
    default: bool = False


def _place_ids(places: Sequence[PlaceEdit]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(place.channel_id for place in places))


def _defaults(places: Sequence[PlaceEdit]) -> frozenset[str]:
    return frozenset(p.channel_id for p in places if p.default and p.channel_id != WEB_PLACE_ID)


def places_changed(before: Sequence[PlaceEdit], after: Sequence[PlaceEdit]) -> bool:
    """Whether ``after`` differs from ``before`` in a place or a default. Pure."""
    return set(_place_ids(before)) != set(_place_ids(after)) or _defaults(before) != _defaults(
        after
    )


def default_changes(
    before: Sequence[PlaceEdit], after: Sequence[PlaceEdit]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The channels that become the agent's default, and those it stops being default of.

    A place that is removed stops being a default too. Pure.
    """
    was, now = _defaults(before), _defaults(after)
    return tuple(sorted(now - was)), tuple(sorted(was - now))


def places_policy(
    policy: TenantAccessPolicy,
    *,
    names: tuple[str, ...],
    agent_name: str,
    runs_in: tuple[str, ...] | None,
) -> TenantAccessPolicy:
    """``policy`` with the agent's rule on ``agent_name`` alone, as `set_agent_rule` writes it."""
    for name in names:
        policy = with_agent_rule(policy, name, AgentRule())
    return with_agent_rule(policy, agent_name, AgentRule(runs_in=runs_in))


def places_refusal(
    policy: TenantAccessPolicy,
    *,
    names: tuple[str, ...],
    agent_name: str,
    turn_on: Collection[str],
    turn_off: Collection[str],
) -> str | None:
    """Why making these channels the agent's default, or clearing them, breaks a rule.

    ``policy`` is the one the writes will leave behind. Pure.
    """
    for channel_id in turn_on:
        reason = binding_refusal(policy, agent_names=names, channel_id=channel_id)
        if reason is not None:
            return render_binding_refusal(reason, agent_name=agent_name)
    for channel_id in turn_off:
        cleared = clear_refusal(policy, channel_id=channel_id)
        if cleared is not None:
            return render_binding_refusal(cleared, agent_name=agent_name)
    return None


# ---------- Writes ----------


def _invalidate(deps: AgentEditDeps, tenant_id: uuid.UUID, ma_agent_id: str) -> None:
    if deps.resolver_cache is not None:
        deps.resolver_cache.agents.pop((tenant_id, ma_agent_id), None)


async def write_agent_patch(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    patch: AgentPatch,
    label: str,
    mcp_replace_allowed: bool,
    expected_version: int | None = None,
) -> BetaManagedAgentsAgent:
    """Write ``patch`` to ``agent`` under one version retry. The caller has authorized it.

    List unions are recomputed from the fresh agent on every attempt, so a retry
    after a version conflict picks up concurrent changes instead of re-applying a
    stale merge. With ``expected_version``, a fresh agent at any other version is
    refused as stale rather than overwritten.
    """
    denied = denied_builtins(deps.denied_tools)
    scalar_patch: dict[str, Any] = {
        key: value
        for key, value in (
            ("model", patch.model),
            ("description", patch.description),
            ("system", patch.system),
        )
        if value is not None
    }
    if "system" in scalar_patch:
        scalar_patch["system"] = apply_credential_guidance(scalar_patch["system"])
    replace = patch.merge == "replace"

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        if expected_version is not None and fresh.version != expected_version:
            raise AgentEditError(
                f"'{label}' changed to version {fresh.version} since this edit began.",
                kind="stale",
            )
        body: dict[str, Any] = dict(scalar_patch)
        if patch.skills is not None:
            body["skills"] = (
                list(patch.skills) if replace else merge_skills_with_ma(patch.skills, fresh)
            )
            merged_skill_count = len(body["skills"])
            if merged_skill_count > AGENT_SKILL_CAP:
                raise AgentEditError(
                    f"Cannot attach skills: the merged skill set ({merged_skill_count}) exceeds "
                    f"this organization's per-agent skill limit ({AGENT_SKILL_CAP}). No skills "
                    f"were changed on '{label}'. Attach fewer skills, "
                    "or use remove_skill before adding more.",
                    kind="invalid",
                )
        if patch.mcp_servers is not None:
            if not mcp_replace_allowed and any(
                replaced_server_url(
                    fresh, server_name=str(entry.get("name")), url=str(entry.get("url"))
                )
                for entry in patch.mcp_servers
            ):
                # Attached under this name at another URL since the caller checked.
                raise AgentEditError(
                    f"'{label}' now has one of these server names at another URL; repointing "
                    "it needs an admin. Nothing was changed.",
                    kind="forbidden",
                )
            body["mcp_servers"] = (
                list(patch.mcp_servers)
                if replace
                else merge_mcp_servers_with_ma(patch.mcp_servers, fresh)
            )
            merged_mcp_count = len(body["mcp_servers"] or [])
            if merged_mcp_count > AGENT_MCP_CAP:
                raise AgentEditError(
                    f"Cannot attach connections: the merged set ({merged_mcp_count}) "
                    f"exceeds this organization's per-agent connection limit ({AGENT_MCP_CAP}). "
                    f"Nothing was changed on '{label}'. Attach fewer, or use "
                    "detach_mcp_server before adding more.",
                    kind="invalid",
                )
        if patch.tools is not None:
            body["tools"] = list(patch.tools) if replace else union_tools(patch.tools, fresh)
        # Skills on an agent without the base toolset cannot start a session
        # ("skills require the read tool"), so a skills write carries it.
        if "skills" in body:
            effective_tools: list[Tool] = body.get("tools") or [
                ma_tool_to_param(t) for t in fresh.tools
            ]
            if not any(entry.get("type") == TOOLSET_TYPE for entry in effective_tools):
                body["tools"] = merge_default_agent_toolset(effective_tools)
        if "tools" in body:
            # On the final tools: a skill's base toolset can turn built-ins on too.
            turned_on = forbidden_turned_on(
                denied, [ma_tool_to_param(t) for t in fresh.tools], body["tools"]
            )
            if turned_on:
                raise AgentEditError(forbidden_builtins_message(turned_on), kind="forbidden")
        return await ma_update_agent(
            deps.client, fresh.id, version=fresh.version, payload=body, scope=actor.scope()
        )

    # An MCP server change is serialized with the token forms' attach-then-publish
    # for this agent; other fields need no lock.
    mcp_lock = (
        agent_mcp_write_lock(
            deps.session_factory,
            tenant_id=actor.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=actor.tenant_id, ma_agent_id=agent.id),
        )
        if patch.mcp_servers is not None
        else contextlib.nullcontext()
    )
    try:
        async with mcp_lock:
            updated = await update_agent_with_version_retry(
                deps.client, agent.id, _apply, scope=actor.scope()
            )
    except anthropic.ConflictError as exc:
        raise AgentEditError(
            "the agent was modified concurrently — please retry the operation", kind="conflict"
        ) from exc
    except anthropic.BadRequestError as exc:
        # MA caps skills per agent org-wide and 400s past it; say so rather than
        # leaking the SDK error, which led callers to drop skills silently.
        if patch.skills is not None and "exceeds maximum" in str(exc).lower():
            raise AgentEditError(
                "Cannot attach skills: the merged skill set exceeds this organization's "
                "per-agent skill limit. No skills were changed. Attach fewer skills, or "
                f"use remove_skill on '{label}' before adding more.",
                kind="invalid",
            ) from exc
        raise
    _invalidate(deps, actor.tenant_id, updated.id)
    return updated


async def write_builtin_tool(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    tool: BuiltinToolName,
    enabled: bool,
) -> BetaManagedAgentsAgent:
    """Turn one built-in on or off, keeping the rest of the toolset. The caller authorized it."""

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        tools = with_builtin([ma_tool_to_param(t) for t in fresh.tools], tool, enabled)
        return await ma_update_agent(
            deps.client,
            fresh.id,
            version=fresh.version,
            payload={"tools": tools},
            scope=actor.scope(),
        )

    try:
        updated = await update_agent_with_version_retry(
            deps.client, ma_agent_id, _apply, scope=actor.scope()
        )
    except anthropic.ConflictError as exc:
        raise AgentEditError(
            "the agent was modified concurrently — please retry the operation", kind="conflict"
        ) from exc
    _invalidate(deps, actor.tenant_id, updated.id)
    return updated


# ---------- Authorized entry points ----------


async def load_editable_agent(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str
) -> BetaManagedAgentsAgent:
    """The tenant's live agent ``ma_agent_id``, refused unless the actor may change it.

    The owner (or a co-owner) or an admin, on an agent that is not defaults-managed
    (`Action.CHANGE_SHARED_AGENT`, spec family).
    """
    try:
        agent = await retrieve_agent(deps.client, ma_agent_id, scope=actor.scope())
    except (anthropic.NotFoundError, ScopeViolation) as exc:
        raise AgentEditError("No such agent.", kind="not_found") from exc
    if agent.archived_at is not None or agent.metadata.get(MA_METADATA_KEY_TENANT) != str(
        actor.tenant_id
    ):
        raise AgentEditError("No such agent.", kind="not_found")
    await check_may_edit(deps, actor=actor, agent=agent)
    return agent


async def check_may_edit(
    deps: AgentEditDeps, *, actor: AgentActor, agent: BetaManagedAgentsAgent
) -> None:
    """Refuse unless the actor may change ``agent``: its owner or an admin, never a managed one."""
    rejection = system_agent_rejection(agent)
    async with deps.session_factory() as session:
        owned = await owns_agent(
            session, tenant_id=actor.tenant_id, ma_agent_id=agent.id, account_id=actor.account_id
        )
    decision = authorize(
        TenantAccessPolicy(),
        subject=actor.subject,
        action=Action.CHANGE_SHARED_AGENT,
        surface=actor.surface,
        operation_family="spec",
        reach=AgentReach(managed=rejection is not None, owned=owned),
    )
    if not decision:
        message = (
            rejection
            if rejection is not None
            else f"Changing '{agent.name}' needs its owner or an admin."
        )
        raise AgentEditError(message, kind="forbidden")


async def update_agent(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    patch: AgentPatch,
    expected_version: int | None,
) -> BetaManagedAgentsAgent:
    """Authorize the actor on the agent, check the patch, and write it."""
    if not patch.touched():
        raise AgentEditError("Nothing to change.", kind="invalid")
    if patch.model is not None:
        refusal = unknown_model_refusal(patch.model)
        if refusal is not None:
            raise AgentEditError(refusal, kind="invalid")
    if patch.mcp_servers is not None:
        rejection = reserved_server_rejection(patch.mcp_servers, deps.public_url)
        if rejection is not None:
            raise AgentEditError(f"{rejection} Nothing was saved.", kind="invalid")
    agent = await load_editable_agent(deps, actor=actor, ma_agent_id=ma_agent_id)
    # The owner or an admin passed above, which is who may repoint a server.
    return await write_agent_patch(
        deps,
        actor=actor,
        agent=agent,
        patch=patch,
        label=agent.name,
        mcp_replace_allowed=True,
        expected_version=expected_version,
    )


def _agent_name(agent: BetaManagedAgentsAgent) -> str:
    return agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name


async def check_places(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    before: Sequence[PlaceEdit],
    after: Sequence[PlaceEdit],
) -> None:
    """Refuse a change of places the actor may not make or a rule would break; write nothing."""
    if not places_changed(before, after):
        return
    if not actor.subject.is_admin:
        raise AgentEditError("Only an admin can change where an agent answers.", kind="forbidden")
    name = _agent_name(agent)
    names = tuple(n for n in (name, *agent_pin_names(agent.name, agent.metadata)) if n)
    turn_on, turn_off = default_changes(before, after)
    async with deps.session_factory() as session:
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        tenant = await get_tenant(session, actor.tenant_id)
        if tenant is None:
            raise AgentEditError("No such workspace.", kind="not_found")
        cleared: list[str] = []
        for channel_id in turn_off:
            scope = ChannelScopeRef(tenant_id=actor.tenant_id, channel_id=channel_id)
            row = await get_scope(session, scope=scope)
            if row is not None and row.agent_name in names:
                cleared.append(channel_id)
    refusal = places_refusal(
        places_policy(
            policy, names=names, agent_name=name, runs_in=_rule_target(policy, names, before, after)
        ),
        names=names,
        agent_name=name,
        turn_on=turn_on,
        turn_off=cleared,
    )
    if refusal is not None:
        raise AgentEditError(f"{refusal} Nothing was saved.", kind="invalid")


def _rule_target(
    policy: TenantAccessPolicy,
    names: tuple[str, ...],
    before: Sequence[PlaceEdit],
    after: Sequence[PlaceEdit],
) -> tuple[str, ...] | None:
    """Where the agent's rule should say it runs, or none to leave it unwritten.

    An agent with no rule runs anywhere, and the places it shows are only the
    channels it is default of and the web chat. Adding a place or changing a default
    must not narrow it to the listed ones, so its rule is written only when the edit
    removes a place it shows. Removing the last place of an unruled agent writes
    nothing, so it is not pinned to nowhere.
    """
    listed = _place_ids(after)
    if agent_permissions(policy, names).runs_in:
        return listed
    removed = set(_place_ids(before)) - set(listed)
    return listed if removed and listed else None


async def set_places(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    before: Sequence[PlaceEdit],
    after: Sequence[PlaceEdit],
) -> None:
    """Make ``after`` where the agent answers: its rule (`runs_in`) and its channel defaults.

    Admins only (`Action.SET_AGENT_RULE`, and the admin gate the chat tools put in
    front of `Action.BIND_CHANNEL_DEFAULT`). Everything is checked before the first
    write; the rule is written first, then the defaults in one transaction.
    """
    await check_places(deps, actor=actor, agent=agent, before=before, after=after)
    if not places_changed(before, after):
        return
    name = _agent_name(agent)
    names = tuple(n for n in (name, *agent_pin_names(agent.name, agent.metadata)) if n)
    async with deps.session_factory() as session:
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        tenant = await get_tenant(session, actor.tenant_id)
    if tenant is None:
        raise AgentEditError("No such workspace.", kind="not_found")
    target = _rule_target(policy, names, before, after)
    try:
        if target is not None:
            await set_agent_rule(
                deps.client,
                deps.session_factory,
                tenant_id=actor.tenant_id,
                platform=tenant.platform,
                agent_name=name,
                runs_in=target,
                subject=actor.subject,
                default=deps.deployment_default,
            )
    except ChannelRuleRefused as exc:
        raise AgentEditError(str(exc), kind="invalid") from exc
    turn_on, turn_off = default_changes(before, after)
    async with deps.session_factory.begin() as session:
        for channel_id in turn_on:
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=actor.tenant_id, channel_id=channel_id),
                tenant_id=actor.tenant_id,
                agent_name=name,
                mode="agent",
                actor_account_id=actor.account_id,
                set_by_admin=True,
            )
        for channel_id in turn_off:
            scope = ChannelScopeRef(tenant_id=actor.tenant_id, channel_id=channel_id)
            row = await get_scope(session, scope=scope)
            if row is not None and row.agent_name in names:
                await unset_fields(
                    session, scope=scope, fields=["agent_name"], actor_account_id=actor.account_id
                )


async def reject_name_taken(deps: AgentEditDeps, *, tenant_id: uuid.UUID, name: str) -> None:
    """Refuse a name any live agent in the tenant holds, whoever owns it.

    Tenant-scoped, as the resolver's (daimon_tenant, daimon_name) identity is.
    """
    if await find_agents_by_daimon_tag(deps.client, tenant_id=tenant_id, name=name):
        raise AgentNameTakenError(name)


async def discard_created(deps: AgentEditDeps, actor: AgentActor, ma_agent_id: str) -> bool:
    """Archive an agent this create made and couldn't finish; False when that failed too."""
    try:
        await archive_agent(deps.client, ma_agent_id, scope=actor.scope())
    except Exception as exc:  # boundary: the caller reports the original failure either way
        log.error(
            "create_agent.discard_failed",
            agent_id=ma_agent_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return False
    return True


async def create_agent(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    spec: AgentSpec,
    owner: bool,
    start: Callable[[], Awaitable[object]] | None = None,
) -> str:
    """Create ``spec`` as a user-owned agent and return its MA id.

    With ``owner`` the actor is recorded as its owner, checked and created under one
    name lock so a concurrent creator of the same name is refused. ``start`` runs
    once the agent exists (a backend move); if it or the owner record fails, the new
    agent is archived so it never runs half-made.
    """
    async with contextlib.AsyncExitStack() as stack:
        if owner:
            await stack.enter_async_context(
                agent_name_lock(deps.session_factory, tenant_id=actor.tenant_id, name=spec.name)
            )
        await reject_name_taken(deps, tenant_id=actor.tenant_id, name=spec.name)
        try:
            outcome = await reconcile_agent(
                deps.client,
                spec,
                tenant_id=actor.tenant_id,
                dry_run=False,
                account_id=derive_guild_account_uuid(actor.tenant_id),
                public_url=deps.public_url,
                # managed=True would make the next defaults apply archive it, as
                # it is not in the seeded spec list.
                managed=False,
                references=deps.session_factory,
                # Never update an agent that took the name since the check above.
                create_only=True,
            )
        except AgentNameTaken as exc:
            raise AgentNameTakenError(spec.name) from exc
        if outcome.anthropic_id is None:
            raise AgentEditError(
                "create_agent: reconcile returned no agent id — report this as a bug",
                kind="conflict",
            )
        created = outcome.action == ReconcileAction.CREATED
        try:
            if owner and created:
                await record_agent_creator(
                    deps.session_factory,
                    tenant_id=actor.tenant_id,
                    agent_id=derive_agent_uuid(
                        tenant_id=actor.tenant_id, ma_agent_id=outcome.anthropic_id
                    ),
                    account_id=actor.account_id,
                )
            if start is not None and created:
                await start()
        except BaseException as exc:
            if start is None or not created:
                raise
            # Shielded: a cancellation must not cancel the removal too.
            removed = False
            with anyio.CancelScope(shield=True):
                removed = await discard_created(deps, actor, outcome.anthropic_id)
            if not removed and isinstance(exc, Exception):
                raise AgentDiscardFailedError(spec.name) from exc
            raise
    return outcome.anthropic_id
