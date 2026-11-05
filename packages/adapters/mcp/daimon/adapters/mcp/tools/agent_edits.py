"""Chat tools for the agent page's remaining writes: revert to a recorded version, change
owners and save a skill's files. Each calls what the web app's route calls."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import audit_to, now
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
from daimon.adapters.mcp.tools.agents import (
    _edit_actor,  # pyright: ignore[reportPrivateUsage]
    _edit_deps,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import agent_admin, agent_changes, agent_github, agent_skill_files
from daimon.core.agent_changes import AgentChangeDeps
from daimon.core.agent_editing import AgentEditDeps, AgentEditError
from daimon.core.agent_github import SkillResyncDeps
from daimon.core.agent_view import AgentViewDeps, OwnMcpServer
from daimon.core.channel_backend import make_backend_service
from daimon.core.defaults.loader import load_skill_descriptions
from daimon.core.mcp_tool_listing import McpToolLister
from daimon.core.skill_library import SyncedSkill
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_OWNERS_ACTION = "Changed agent owners"
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


def _change_deps(runtime: McpRuntime) -> AgentChangeDeps:
    """The change service's dependencies, as the web app builds them."""
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
        backends=make_backend_service(runtime.session_factory, settings.turn),
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
            _change_deps(runtime), actor=_edit_actor(auth), ma_agent_id=agent_id, version=version
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
