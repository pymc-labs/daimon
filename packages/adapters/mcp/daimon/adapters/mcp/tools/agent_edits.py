"""Chat tools for the agent page's remaining writes: revert to a recorded version, change
owners and helpers and save a skill's files. Each calls what the web app's route calls."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._rule_view import load_isolation_view
from daimon.adapters.mcp.tools._web_admin import audit_to, now
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
from daimon.adapters.mcp.tools.agents import (
    _capture_backend_source,  # pyright: ignore[reportPrivateUsage]
    _edit_actor,  # pyright: ignore[reportPrivateUsage]
    _edit_deps,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import refuse_helper, require_turn_origin
from daimon.core import agent_admin, agent_changes, agent_editing, agent_github, agent_skill_files
from daimon.core.agent_changes import AgentChangeDeps
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_github import SkillResyncDeps
from daimon.core.agent_view import AgentConfigDoc, AgentViewDeps, HelperDoc, OwnMcpServer
from daimon.core.anthropic_guard import anthropic_disabled
from daimon.core.channel_backend import NO_CAPTURE, make_backend_service
from daimon.core.defaults.loader import load_skill_descriptions
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.mcp_tool_listing import McpToolLister
from daimon.core.skill_library import SyncedSkill
from daimon.core.stores import agent_helpers
from daimon.core.stores.selfhosted_resources import list_live_agent_facts
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_OWNERS_ACTION = "Changed agent owners"
_HELPERS_ACTION = "Changed agent helpers"
_PINNED = "Saved a skill version"
_RESYNCED = "Resynced repo skills"


@dataclass(frozen=True)
class AgentReverted:
    agent_id: str
    reverted_to: int
    version: int
    web_url: WebUrl = None


@dataclass(frozen=True)
class AgentOwnersSet:
    agent_id: str
    changed: bool
    web_url: WebUrl = None


@dataclass(frozen=True)
class AgentHelper:
    agent_id: str
    name: str


@dataclass(frozen=True)
class AgentHelpersSet:
    agent_id: str
    helpers: list[AgentHelper]
    web_url: WebUrl = None


@dataclass(frozen=True)
class AgentSkillSaved:
    agent_id: str
    skill_id: str
    version: str
    applied: bool
    """False when the agent already pinned this version."""
    web_url: WebUrl = None


@dataclass(frozen=True)
class AgentSkillsResynced:
    agent_id: str
    results: list[SyncedSkill]
    web_url: WebUrl = None


def _web_edit_deps(runtime: McpRuntime) -> AgentEditDeps:
    """The edit service's dependencies, as the web app builds them."""
    settings = runtime.settings
    public_url = settings.mcp.public_url
    return AgentEditDeps(
        client=runtime.client,
        session_factory=runtime.session_factory,
        public_url=str(public_url) if public_url is not None else None,
        denied_tools=settings.tool_safety.denied,
        resolver_cache=runtime.resolver_cache,
        deployment_default=runtime.deployment_default,
    )


def _change_deps(runtime: McpRuntime, auth: AuthIdentity | None = None) -> AgentChangeDeps:
    """The change service's dependencies, as the web app builds them.

    Only an `auth` can switch an agent's provider: its loss consent is read from the
    agent's source as that caller. Without one, a switch is refused.
    """
    settings = runtime.settings
    public_url = settings.mcp.public_url
    base = str(public_url) if public_url is not None else None
    secret = settings.mcp.jwt_secret
    own = (
        OwnMcpServer(url=base, secret=secret.get_secret_value().encode(), now=now)
        if base is not None and secret is not None
        else None
    )
    return AgentChangeDeps(
        edit=_web_edit_deps(runtime),
        view=AgentViewDeps(
            client=runtime.client,
            session_factory=runtime.session_factory,
            markup=settings.billing.markup,
            deployment_default=runtime.deployment_default,
            skill_descriptions=load_skill_descriptions(settings.defaults_root / "skills"),
            mcp_tools=McpToolLister(),
            fernet=runtime.fernet,
            own_server=own,
        ),
        backends=make_backend_service(
            runtime.session_factory,
            settings.turn,
            capture_source=NO_CAPTURE
            if auth is None
            else partial(_capture_backend_source, runtime, auth, None),
        ),
    )


async def _revert_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_id: str,
    version: int,
    origin_context_id: str | None = None,
) -> AgentReverted:
    await require_card(runtime, auth, tool_name="revert_agent", origin_context_id=origin_context_id)
    try:
        written = await agent_changes.revert(
            _change_deps(runtime, auth),
            actor=_edit_actor(auth),
            ma_agent_id=agent_id,
            version=version,
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    links = await tenant_links(runtime, auth)
    return AgentReverted(
        agent_id=agent_id,
        reverted_to=version,
        version=written.version,
        web_url=links.agent(agent_id) if links else None,
    )


async def _owners_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_id: str,
    owner: str | None,
    co_owners: list[str],
    origin_context_id: str | None = None,
) -> AgentOwnersSet:
    await require_card(
        runtime, auth, tool_name="set_agent_owners", origin_context_id=origin_context_id
    )
    try:
        changed = await agent_admin.set_owners_by_ref(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            ma_agent_id=agent_id,
            owner=owner,
            co_owners=co_owners,
            audited=audit_to(auth, _OWNERS_ACTION),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    links = await tenant_links(runtime, auth)
    return AgentOwnersSet(
        agent_id=agent_id, changed=changed, web_url=links.agent(agent_id) if links else None
    )


async def _live_agents(runtime: McpRuntime, auth: AuthIdentity) -> list[BetaManagedAgentsAgent]:
    """The workspace's live agents; a self-hosted conversation reads its own records."""
    if auth.anthropic_disabled or anthropic_disabled():
        async with runtime.session_factory() as session:
            return await list_live_agent_facts(session, tenant_id=auth.tenant_id)
    return await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)


def _agent_named(ref: str, agents: list[BetaManagedAgentsAgent]) -> BetaManagedAgentsAgent:
    ref = ref.strip()
    found = next(
        (a for a in agents if ref in (a.id, a.metadata.get(MA_METADATA_KEY_NAME, a.name))), None
    )
    if found is None:
        raise ToolError(f"No agent called '{ref}' here. Nothing changed.")
    return found


async def _helpers_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_id: str,
    helpers: list[str],
    origin_context_id: str | None = None,
) -> AgentHelpersSet:
    _require_admin(auth)
    origin = refuse_helper(await require_turn_origin(runtime, auth, origin_context_id))
    await require_card(
        runtime, auth, tool_name="set_agent_helpers", origin_context_id=origin_context_id
    )
    roster = await _live_agents(runtime, auth)
    view = await load_isolation_view(
        runtime, auth, agents=roster, location_channel_id=origin.parent_channel_id
    )
    visible = [agent for agent in roster if view.sees_agent(agent)]
    target = _agent_named(agent_id, visible)
    wanted = [_agent_named(ref, visible).id for ref in helpers]
    async with runtime.session_factory() as session:
        before = await agent_helpers.list_helper_ids(
            session, tenant_id=auth.tenant_id, agent_id=target.id
        )
    # A helper homed in a channel this one cannot see is not this list's to drop.
    visible_ids = {agent.id for agent in visible}
    wanted = list(dict.fromkeys([*wanted, *(i for i in before if i not in visible_ids)]))
    actor = _edit_actor(auth)
    names = {a.id: _agent_name(a) for a in roster}
    try:
        if _is_versioned(auth, target):
            saved = await _helpers_versioned(
                runtime, actor, target, before=before, wanted=wanted, names=names
            )
        else:
            saved = await agent_admin.set_helpers(
                runtime.session_factory,
                actor=actor,
                ma_agent_id=target.id,
                helper_ids=wanted,
                roster=visible,
                audited=audit_to(auth, _HELPERS_ACTION),
            )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    links = await tenant_links(runtime, auth)
    return AgentHelpersSet(
        agent_id=target.id,
        helpers=[
            AgentHelper(agent_id=i, name=names[i]) for i in saved if i in names and i in visible_ids
        ],
        web_url=links.agent(target.id) if links else None,
    )


def _agent_name(agent: BetaManagedAgentsAgent) -> str:
    return agent.metadata.get(MA_METADATA_KEY_NAME, agent.name)


def _is_versioned(auth: AuthIdentity, agent: BetaManagedAgentsAgent) -> bool:
    """Whether the agent's helpers live in its canvas versions: a normal agent on a provider
    that keeps them. A seeded agent has no canvas edits and a self-hosted one no versions."""
    if auth.anthropic_disabled or anthropic_disabled():
        return False
    return agent_editing.system_agent_rejection(agent) is None


def with_helpers(
    config: AgentConfigDoc, helper_ids: list[str], names: dict[str, str]
) -> AgentConfigDoc:
    """``config`` listing exactly ``helper_ids``; a name is the listed one, else ``names``'."""
    listed = {helper.id: helper.name for helper in config.helpers or []}
    return config.model_copy(
        update={
            "helpers": [HelperDoc(id=i, name=listed.get(i) or names.get(i, i)) for i in helper_ids]
        }
    )


async def _helpers_versioned(
    runtime: McpRuntime,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    *,
    before: list[str],
    wanted: list[str],
    names: dict[str, str],
) -> list[str]:
    """The list as the canvas's apply saves it: a recorded version, so a draft begun
    earlier is refused as out of date instead of silently reverting it."""
    if set(wanted) == set(before):
        return before
    await agent_changes.apply_change(
        _change_deps(runtime),
        actor=actor,
        ma_agent_id=agent.id,
        change=lambda config: with_helpers(config, wanted, names),
        note=_HELPERS_ACTION,
    )
    return wanted


async def _save_skill_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_id: str,
    skill_id: str | None,
    version: str | None,
    files: dict[str, str | None],
) -> AgentSkillSaved:
    actor = _edit_actor(auth)
    try:
        saved = await agent_skill_files.save_skill_files(
            _edit_deps(runtime),
            actor=actor,
            ma_agent_id=agent_id,
            skill_id=skill_id,
            version=version,
            files=files,
        )
        try:
            await agent_changes.apply_change(
                _change_deps(runtime),
                actor=actor,
                ma_agent_id=agent_id,
                change=lambda config: agent_skill_files.with_skill(config, saved),
                note=_PINNED,
            )
            applied = True
        except AgentEditError as err:
            if err.kind != "conflict":
                raise
            applied = False
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    links = await tenant_links(runtime, auth)
    return AgentSkillSaved(
        agent_id=agent_id,
        skill_id=saved.skill_id,
        version=saved.version,
        applied=applied,
        web_url=links.agent(agent_id, f"skill:{saved.skill_id}") if links else None,
    )


async def _resync_impl(
    runtime: McpRuntime, auth: AuthIdentity, agent_id: str
) -> AgentSkillsResynced:
    settings = runtime.settings
    if runtime.fernet is None:
        raise ToolError(agent_github.NOT_CONFIGURED)
    try:
        async with httpx.AsyncClient() as http:
            resynced = await agent_github.resync(
                _web_edit_deps(runtime),
                actor=_edit_actor(auth),
                ma_agent_id=agent_id,
                sync=SkillResyncDeps(
                    fernet=runtime.fernet,
                    http_client=http,
                    app_id=settings.github_app.app_id,
                    app_private_key=settings.github_app.private_key,
                    max_tarball_bytes=settings.github.max_tarball_bytes,
                    max_tarball_decompressed_bytes=settings.github.max_tarball_decompressed_bytes,
                ),
                audited=audit_to(auth, _RESYNCED),
            )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    links = await tenant_links(runtime, auth)
    return AgentSkillsResynced(
        agent_id=agent_id,
        results=resynced.results,
        web_url=links.agent(agent_id) if links else None,
    )


def register_agent_edit_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def revert_agent(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, agent_id: str, version: int, origin_context_id: str
    ) -> AgentReverted:
        """Restore a recorded version of an agent's setup as its next version; name, avatar,
        places and sign-ins stay."""
        return await _revert_impl(runtime, await _auth(ctx), agent_id, version, origin_context_id)

    @mcp.tool(tags={"admin"})
    async def set_agent_owners(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_id: str,
        owner: str | None,
        co_owners: list[str] | None = None,
        *,
        origin_context_id: str,
    ) -> AgentOwnersSet:
        """Set an agent's owner and co-owners to exactly these web person or account ids."""
        return await _owners_impl(
            runtime, await _auth(ctx), agent_id, owner, co_owners or [], origin_context_id
        )

    @mcp.tool(tags={"admin"})
    async def set_agent_helpers(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_id: str,
        helpers: list[str],
        *,
        origin_context_id: str,
    ) -> AgentHelpersSet:
        """Set the agents an agent may ask for help to exactly these names or ids, for any agent
        you can see; helpers kept to channels you can't see stay. A normal agent gets a new
        version, so an open draft goes out of date; the default agent and self-hosted agents
        change in place. The person approves first."""
        return await _helpers_impl(runtime, await _auth(ctx), agent_id, helpers, origin_context_id)

    @mcp.tool
    async def save_agent_skill_files(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_id: str,
        files: dict[str, str | None],
        skill_id: str | None = None,
        version: str | None = None,
    ) -> AgentSkillSaved:
        """Edit an agent's skill file: publish text files as a new version of one of its skills,
        or a new skill when ``skill_id`` is left out, and pin it. A null file keeps its bytes
        from ``version`` (the one you read)."""
        return await _save_skill_impl(runtime, await _auth(ctx), agent_id, skill_id, version, files)

    @mcp.tool
    async def resync_agent_skills(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, agent_id: str
    ) -> AgentSkillsResynced:
        """Sync an agent's repo skills now from their recorded repos; an unchanged skill is
        skipped. Only an owner of the agent or an admin."""
        return await _resync_impl(runtime, await _auth(ctx), agent_id)


__all__ = ["register_agent_edit_tools"]
