"""Chat tools for the skill library's version writes: save a skill's files as its next
version and restore an older one. Admin-only, like the web app's skill pages."""

from __future__ import annotations

from dataclasses import dataclass

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
from daimon.core.skill_library import RESTORED_ACTION, SAVED_ACTION
from daimon.core.skill_library_files import SkillRef
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


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


__all__ = ["register_skill_library_edit_tools"]
