"""Chat tools for the skills the data server node's server serves, as in the web app's agent
editor: add one to the library, check or approve its update, fork it, follow its updates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import audit_to
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
from daimon.adapters.mcp.tools.agents import (
    _edit_actor,  # pyright: ignore[reportPrivateUsage]
    _edit_deps,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentEditError
from daimon.core.mcp_rpc import McpStatusError
from daimon.core.mcp_skills.install import ServerSkill, fetch_server_skill
from daimon.core.mcp_skills.protocol import Endpoint, fetch_serves_skills, fetch_skill_listing
from daimon.core.node_exchange import NodeAccessError, NodeAsker, NodeExchange, asker_of_account
from daimon.core.server_skills.follows import apply_held_update, set_agent_follow
from daimon.core.server_skills.mirror import add_server_skill
from daimon.core.server_skills.node import (
    NodeCall,
    ServerSkillOption,
    ServesCache,
    SkillsNode,
    ask_node,
    list_server_skills,
    skill_options,
    skill_sync_deps,
    skills_node_of,
)
from daimon.core.server_skills.sync import (
    CheckResult,
    SyncDeps,
    approve_update,
    check_mirror,
    follow_now,
)
from daimon.core.skill_library import MirrorDoc, mirror_doc
from daimon.core.skill_library_files import SkillRef, fork_library_skill
from daimon.core.skills.ingest import SkillIngestError
from daimon.core.stores import server_skills as store
from daimon.core.stores import web_people
from daimon.core.stores.domain import ServerSkillRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

if TYPE_CHECKING:
    from daimon.core.turn.deps import TurnDeps

_ACCESS: dict[str, str] = {
    "no_account": "{label} serves only chat accounts here.",
    "not_served": "{label} doesn't serve this team.",
    "refused": "{label} refused access. Ask your {label} admin.",
    "unconfigured": "{label} isn't set up here.",
    "unreachable": "{label} didn't answer. Try again soon.",
}


# The web routes' caps on the same fields.
_Uri = Annotated[str, Field(min_length=1, max_length=2048)]
_Digest = Annotated[str, Field(min_length=1, max_length=128)]
_SkillId = Annotated[str, Field(min_length=1, max_length=200)]
_AgentId = Annotated[str, Field(min_length=1, max_length=200)]
_Name = Annotated[str, Field(min_length=1, max_length=64)]
_OriginId = Annotated[str, Field(min_length=1, max_length=200)]


@dataclass(frozen=True)
class LibrarySkill:
    skill_id: str
    name: str
    version: str
    web_url: WebUrl = None


@dataclass(frozen=True)
class ServerSkills:
    server: str
    """The node's name, as people know it."""
    offered: list[ServerSkillOption] = field(default_factory=list[ServerSkillOption])
    """What the server offers you, when no skill was named."""
    added: LibrarySkill | None = None


@dataclass(frozen=True)
class ServerSkillCheck:
    result: CheckResult
    server: MirrorDoc | None
    web_url: WebUrl = None


def _now() -> datetime:
    return datetime.now(UTC)


def _node(runtime: McpRuntime) -> SkillsNode:
    node = skills_node_of(runtime.settings)
    if node is None:
        raise ToolError("Server skills aren't set up here.")
    return node


def _exchange(runtime: McpRuntime) -> NodeExchange:
    return NodeExchange(
        sessionmaker=runtime.session_factory,
        fernet=runtime.fernet,
        identity_key=runtime.identity_key,
        held=runtime.node_held,
        now=_now,
    )


def _sync_deps(runtime: McpRuntime, node: SkillsNode) -> SyncDeps:
    def turn_deps() -> TurnDeps:
        from daimon.core.ma_resolver import new_resolver_cache
        from daimon.core.turn.deps import build_turn_deps

        return build_turn_deps(
            runtime.settings,
            runtime.client,
            runtime.session_factory,
            deployment_default=runtime.deployment_default,
            resolver_cache=new_resolver_cache(),
            billing_config=None,
        )

    return skill_sync_deps(
        node=node,
        exchange=_exchange(runtime),
        shared=None,
        edit=_edit_deps(runtime),
        turn_settings=runtime.settings.turn,
        turn_deps=turn_deps,
        now=_now,
    )


async def _require_editor(runtime: McpRuntime, auth: AuthIdentity) -> None:
    """The web app's agent editors: anyone here but a visitor or a suspended person."""
    async with runtime.session_factory() as db:
        person = await web_people.get_person_for_account(
            db, tenant_id=auth.tenant_id, account_id=auth.account_id
        )
    if person is not None and person.status == "suspended":
        raise ToolError("Your web access is suspended. Ask an admin or an owner.")
    if person is not None and person.kind == "visitor":
        raise ToolError("Visitors can't change the skill library. Ask an admin or an owner.")


def _require_library_admin(auth: AuthIdentity) -> None:
    try:
        require_admin(_edit_actor(auth), "The skill library")
    except AgentEditError as err:
        raise ToolError(str(err)) from err


async def _as_caller[T](
    runtime: McpRuntime,
    auth: AuthIdentity,
    node: SkillsNode,
    run: Callable[[NodeExchange, NodeAsker], Awaitable[T]],
) -> T:
    """`run` at the node's server, as the caller's chat account in this workspace."""
    async with runtime.session_factory() as db:
        asker = await asker_of_account(db, tenant_id=auth.tenant_id, account_id=auth.account_id)
    try:
        if asker is None:
            raise NodeAccessError("no_account")
        return await run(_exchange(runtime), asker)
    except NodeAccessError as err:
        raise ToolError(_ACCESS[err.problem].format(label=node.label)) from err
    except (McpStatusError, httpx.HTTPError, ValueError) as err:
        raise ToolError(_ACCESS["unreachable"].format(label=node.label)) from err


async def _asked[T](
    runtime: McpRuntime, auth: AuthIdentity, node: SkillsNode, call: NodeCall[T]
) -> T:
    return await _as_caller(
        runtime, auth, node, lambda exchange, asker: ask_node(exchange, None, node, asker, call)
    )


async def _mirror(runtime: McpRuntime, auth: AuthIdentity, skill_id: str) -> ServerSkillRow:
    async with runtime.session_factory() as db:
        mirror = await store.mirror_of_skill(db, tenant_id=auth.tenant_id, skill_id=skill_id)
    if mirror is None:
        raise ToolError("No such server skill.")
    return mirror


async def _shown(runtime: McpRuntime, auth: AuthIdentity, ref: SkillRef) -> LibrarySkill:
    links = await tenant_links(runtime, auth)
    return LibrarySkill(
        skill_id=ref.id,
        name=ref.name,
        version=ref.version,
        web_url=links.skill(ref.id) if links else None,
    )


async def _add_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    skill: str | None,
    serves: ServesCache,
    origin_context_id: str | None = None,
) -> ServerSkills:
    await _require_editor(runtime, auth)
    if skill is not None:
        # Adding reaches the whole library, so a person confirms it; listing changes nothing.
        await require_card(
            runtime, auth, tool_name="add_server_skill", origin_context_id=origin_context_id
        )
    node = _node(runtime)
    if not await serves.serves(
        node.server.name, lambda: _asked(runtime, auth, node, fetch_serves_skills)
    ):
        raise ToolError(f"{node.label} offers no skills.")
    if skill is None:
        edit, actor = _edit_deps(runtime), _edit_actor(auth)
        offered = await _as_caller(
            runtime,
            auth,
            node,
            lambda exchange, asker: list_server_skills(
                exchange, None, node, asker, edit=edit, actor=actor
            ),
        )
        return ServerSkills(server=node.label, offered=offered)

    async def fetch(http: httpx.AsyncClient, endpoint: Endpoint) -> ServerSkill:
        raw = await fetch_skill_listing(http, endpoint, keep=lambda _raw: True)
        matches = [o for o in skill_options(raw, {}, frozenset()) if skill in (o.name, o.uri)]
        if not matches:
            raise ToolError(f"{node.label} offers you no skill named {skill}.")
        if len(matches) > 1:
            uris = ", ".join(o.uri for o in matches)
            raise ToolError(
                f"{node.label} offers more than one skill named {skill}. "
                f"Add one by its URI: {uris}."
            )
        found = matches[0]
        try:
            return await fetch_server_skill(http, endpoint, server=node.label, skill=found.uri)
        except SkillIngestError as err:
            raise ToolError(str(err)) from err

    fetched = await _asked(runtime, auth, node, fetch)
    try:
        ref = await add_server_skill(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            server=node.server.name,
            fetched=fetched,
            now=_now(),
            audited=audit_to(auth, "Added server skill"),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return ServerSkills(server=node.label, added=await _shown(runtime, auth, ref))


async def _check_impl(runtime: McpRuntime, auth: AuthIdentity, skill_id: str) -> ServerSkillCheck:
    await _require_editor(runtime, auth)
    _require_library_admin(auth)
    node = _node(runtime)
    mirror = await _mirror(runtime, auth, skill_id)
    return await _checked(
        runtime, auth, mirror, await check_mirror(_sync_deps(runtime, node), mirror.id)
    )


async def _approve_impl(
    runtime: McpRuntime, auth: AuthIdentity, skill_id: str, digest: str, origin_context_id: str
) -> ServerSkillCheck:
    await _require_editor(runtime, auth)
    _require_library_admin(auth)
    # An approved update puts new scripts and allowed tools in front of everyone.
    await require_card(
        runtime, auth, tool_name="approve_server_skill_update", origin_context_id=origin_context_id
    )
    node = _node(runtime)
    mirror = await _mirror(runtime, auth, skill_id)
    if mirror.pending_digest is None:
        raise ToolError("No update is waiting.")
    return await _checked(
        runtime,
        auth,
        mirror,
        await approve_update(_sync_deps(runtime, node), mirror.id, digest=digest),
    )


async def _checked(
    runtime: McpRuntime, auth: AuthIdentity, mirror: ServerSkillRow, result: CheckResult
) -> ServerSkillCheck:
    skill_id = mirror.skill_id
    async with runtime.session_factory() as db:
        fresh = await store.get_mirror(db, mirror.id)
    links = await tenant_links(runtime, auth)
    return ServerSkillCheck(
        result=result,
        server=mirror_doc(fresh),
        web_url=links.skill(skill_id) if links else None,
    )


async def _fork_impl(
    runtime: McpRuntime, auth: AuthIdentity, skill_id: str, name: str
) -> LibrarySkill:
    await _require_editor(runtime, auth)
    try:
        ref = await fork_library_skill(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            skill_id=skill_id,
            name=name,
            audited=audit_to(auth, "Forked skill"),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return await _shown(runtime, auth, ref)


async def _follow_impl(
    runtime: McpRuntime, auth: AuthIdentity, agent_id: str, skill_id: str, follow: bool
) -> str:
    await _require_editor(runtime, auth)
    actor = _edit_actor(auth)
    try:
        await set_agent_follow(
            _edit_deps(runtime),
            actor=actor,
            agent_id=agent_id,
            skill_id=skill_id,
            follow=follow,
            now=_now(),
            audited=audit_to(auth, "Changed skill updates"),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    node = skills_node_of(runtime.settings)
    if follow and node is not None:
        await follow_now(
            _sync_deps(runtime, node),
            tenant_id=actor.tenant_id,
            skill_id=skill_id,
            agent_id=agent_id,
        )
        return "The agent follows the skill's updates and has its latest version."
    return "The agent follows the skill's updates." if follow else "The agent keeps its version."


async def _apply_impl(runtime: McpRuntime, auth: AuthIdentity, agent_id: str, skill_id: str) -> str:
    await _require_editor(runtime, auth)
    try:
        await apply_held_update(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            agent_id=agent_id,
            skill_id=skill_id,
            audited=audit_to(auth, "Applied skill update"),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return "The agent has the update. Its owner confirms the change before it answers again."


def register_server_skill_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    serves = ServesCache()

    @mcp.tool
    async def add_server_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, skill: _Uri | None = None, origin_context_id: _OriginId | None = None
    ) -> ServerSkills:
        """Add a skill the connected data server offers to the skill library, by name or
        skill:// URI; it then stays current with the server. Adding needs a person's
        confirmation on a card and your ``origin_context_id``. Without ``skill``, list what
        the server offers you, with each one's library skill_id once added."""
        return await _add_impl(runtime, await _auth(ctx), skill, serves, origin_context_id)

    @mcp.tool(tags={"admin"})
    async def check_server_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, skill_id: _SkillId
    ) -> ServerSkillCheck:
        """Check a server skill for an update now. An update that needs review waits as
        ``pending_digest``; a person approves it with ``approve_server_skill_update``.
        Admin-only."""
        return await _check_impl(runtime, await _auth(ctx), skill_id)

    @mcp.tool(tags={"admin"})
    async def approve_server_skill_update(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, skill_id: _SkillId, digest: _Digest, origin_context_id: _OriginId
    ) -> ServerSkillCheck:
        """Approve the update waiting on a server skill, given as its ``pending_digest``.
        Its new scripts and allowed tools reach every agent that uses the skill, so a
        person confirms on a card first. Admin-only."""
        return await _approve_impl(runtime, await _auth(ctx), skill_id, digest, origin_context_id)

    @mcp.tool(tags={"admin"})
    async def fork_server_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, skill_id: _SkillId, name: _Name
    ) -> LibrarySkill:
        """Copy a server skill's latest version into an editable library skill named
        ``name``, which follows no updates. Admin-only."""
        return await _fork_impl(runtime, await _auth(ctx), skill_id, name)

    @mcp.tool
    async def set_server_skill_follow(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, agent_id: _AgentId, skill_id: _SkillId, follow: bool
    ) -> str:
        """Choose whether an agent takes a server skill's new versions as they come
        (``follow``) or keeps the version it pins."""
        return await _follow_impl(runtime, await _auth(ctx), agent_id, skill_id, follow)

    @mcp.tool
    async def apply_server_skill_update(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, agent_id: _AgentId, skill_id: _SkillId
    ) -> str:
        """Give an agent the server skill update its model provider check held back."""
        return await _apply_impl(runtime, await _auth(ctx), agent_id, skill_id)


__all__ = ["register_server_skill_tools"]
