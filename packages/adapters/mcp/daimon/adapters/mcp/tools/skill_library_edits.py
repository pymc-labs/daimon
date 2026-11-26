"""Chat tools for the skill library's admin pages: save a skill's files as its next version,
restore an older one, add a repository's skills, and read or set who may read a skill. Admin-only,
like the web app's skill pages; the changes that reach others run on the person's card."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

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
from daimon.core import skill_library_files
from daimon.core.agent_editing import AgentEditError
from daimon.core.server_skills.repo import valid_branch
from daimon.core.server_skills.sync import UpstreamGone, UpstreamUnreachable
from daimon.core.skill_library import RESTORED_ACTION, SAVED_ACTION
from daimon.core.skill_library_files import SkillRef
from daimon.core.skill_repos import SkillRepoDecl, add_skill_repo
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

# The web routes' caps on the same fields.
_SkillId = Annotated[str, Field(min_length=1, max_length=200)]
_Scope = Annotated[str | None, Field(min_length=1, max_length=200)]
_Repo = Annotated[str, Field(min_length=3, max_length=200, pattern=r"^[\w.-]+/[\w.-]+$")]
_Branch = Annotated[str, Field(min_length=1, max_length=200)]
_Paths = Annotated[list[str], Field(max_length=50)]
_OriginId = Annotated[str, Field(min_length=1, max_length=200)]


@dataclass(frozen=True)
class LibrarySkillVersion:
    skill_id: str
    name: str
    version: str
    web_url: WebUrl = None


async def _shown(runtime: McpRuntime, auth: AuthIdentity, ref: SkillRef) -> LibrarySkillVersion:
    links = await tenant_links(runtime, auth)
    return LibrarySkillVersion(
        skill_id=ref.id,
        name=ref.name,
        version=ref.version,
        web_url=links.skill(ref.id) if links else None,
    )


async def _save_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    skill_id: str,
    files: dict[str, str | None],
    expected_version: str,
) -> LibrarySkillVersion:
    try:
        ref = await skill_library_files.save_library_files(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            skill_id=skill_id,
            files=files,
            expected_version=expected_version,
            audited=audit_to(auth, SAVED_ACTION),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return await _shown(runtime, auth, ref)


async def _restore_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    skill_id: str,
    version: str,
    origin_context_id: str | None = None,
) -> LibrarySkillVersion:
    await require_card(
        runtime,
        auth,
        tool_name="restore_library_skill_version",
        origin_context_id=origin_context_id,
    )
    try:
        ref = await skill_library_files.restore_library_version(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            skill_id=skill_id,
            version=version,
            audited=audit_to(auth, RESTORED_ACTION),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return await _shown(runtime, auth, ref)


@dataclass(frozen=True)
class SkillScope:
    skill_id: str
    scope: str | None
    """None: everyone who may use the library."""


@dataclass(frozen=True)
class RepoSkill:
    name: str
    action: str
    detail: str | None


@dataclass(frozen=True)
class SkillRepoAdded:
    repo: str
    skills: list[RepoSkill]


async def _scope_impl(runtime: McpRuntime, auth: AuthIdentity, skill_id: str) -> SkillScope:
    try:
        scope = await skill_library_files.skill_scope(
            _edit_deps(runtime), actor=_edit_actor(auth), skill_id=skill_id
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return SkillScope(skill_id=skill_id, scope=scope)


async def _set_scope_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    skill_id: str,
    scope: str | None,
    origin_context_id: str,
) -> SkillScope:
    # A scope decides who reads the skill, so a person confirms it.
    await require_card(
        runtime, auth, tool_name="set_skill_scope", origin_context_id=origin_context_id
    )
    try:
        await skill_library_files.set_skill_scope(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            skill_id=skill_id,
            scope=scope,
            audited=audit_to(auth, "Changed skill scope"),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return SkillScope(skill_id=skill_id, scope=scope)


async def _add_repo_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    repo: str,
    branch: str,
    paths: list[str],
    scope: str | None,
    origin_context_id: str,
) -> SkillRepoAdded:
    if not valid_branch(branch):
        raise ToolError("That is not a branch name.")
    # Adding reaches the whole library, so a person confirms it.
    await require_card(
        runtime, auth, tool_name="add_library_repository", origin_context_id=origin_context_id
    )
    decl = SkillRepoDecl(
        tenant_id=auth.tenant_id, repo=repo, branch=branch, paths=tuple(paths), scope=scope
    )
    try:
        async with httpx.AsyncClient() as http:
            outcomes = await add_skill_repo(
                _edit_deps(runtime),
                runtime.settings.github,
                runtime.settings.github_app,
                actor=_edit_actor(auth),
                decl=decl,
                http=http,
                now=lambda: datetime.now(UTC),
            )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    except UpstreamGone as err:
        raise ToolError("That repository could not be read.") from err
    except UpstreamUnreachable as err:
        raise ToolError("GitHub didn't answer. Try again soon.") from err
    async with runtime.session_factory.begin() as session:
        await audit_to(auth, "Added skill repository")(session, repo)
    return SkillRepoAdded(
        repo=repo, skills=[RepoSkill(o.name, o.action, o.detail) for o in outcomes]
    )


def register_skill_library_edit_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def save_library_skill_files(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, skill_id: str, files: dict[str, str | None], expected_version: str
    ) -> LibrarySkillVersion:
        """Publish text files as a library skill's next version. A file whose content is null
        keeps its bytes from ``expected_version``, the version you read; a later version
        is refused. Admin-only."""
        return await _save_impl(runtime, await _auth(ctx), skill_id, files, expected_version)

    @mcp.tool(tags={"admin"})
    async def restore_library_skill_version(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, skill_id: str, version: str, origin_context_id: str
    ) -> LibrarySkillVersion:
        """Republish an older library skill version as the next one."""
        return await _restore_impl(runtime, await _auth(ctx), skill_id, version, origin_context_id)

    @mcp.tool(tags={"admin"})
    async def get_skill_scope(ctx: Context, skill_id: _SkillId) -> SkillScope:  # pyright: ignore[reportUnusedFunction]
        """Who may read a library skill: a data scope name, or none for everyone who may use
        the library. Admin-only."""
        return await _scope_impl(runtime, await _auth(ctx), skill_id)

    @mcp.tool(tags={"admin"})
    async def set_skill_scope(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, skill_id: _SkillId, scope: _Scope, origin_context_id: _OriginId
    ) -> SkillScope:
        """Set who may read a library skill: a data scope name, or null for everyone who may
        use the library. A person confirms on a card first. Admin-only."""
        return await _set_scope_impl(runtime, await _auth(ctx), skill_id, scope, origin_context_id)

    @mcp.tool(tags={"admin"})
    async def add_library_repository(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        repo: _Repo,
        origin_context_id: _OriginId,
        branch: _Branch = "main",
        paths: _Paths | None = None,
        scope: _Scope = None,
    ) -> SkillRepoAdded:
        """Bring a GitHub repository's folders (``owner/name``) into the library, limited to
        ``paths`` when given, readable by ``scope``. Ones already brought in are kept. A person
        confirms on a card first. Admin-only."""
        return await _add_repo_impl(
            runtime, await _auth(ctx), repo, branch, paths or [], scope, origin_context_id
        )


__all__ = ["register_skill_library_edit_tools"]
