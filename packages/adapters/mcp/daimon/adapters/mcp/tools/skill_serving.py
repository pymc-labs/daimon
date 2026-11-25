"""Serve the workspace library's skills to MCP clients: list them and read their files.

What a caller sees is decided by each skill's scope (`daimon.core.workspace_skills`): an
unscoped skill shows to everyone who may use the library, a scoped one only to a caller the
store's scopes name. A chat turn, a held credential and an outside client are no person
asking for themselves, so they see unscoped skills only.
"""

from __future__ import annotations

import uuid
from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._hub_workspace import hub_workspace_tool
from daimon.core.skill_serving import MAIN_FILE, ServedSkill, list_served, read_served
from daimon.core.workspace_skills import skill_scopes
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

MAX_TEXT: Final = 100_000
"""Characters of one file a read returns."""
_NOT_FOUND = "No such skill or file, or not visible to you."


class SkillFileInfo(BaseModel):
    path: str
    size: int
    sha256: str


class LibrarySkill(BaseModel):
    name: str
    version: str
    files: list[SkillFileInfo]


class LibrarySkillFile(BaseModel):
    name: str
    version: str
    path: str
    size: int
    text: str | None
    """None for a file that is not text."""
    truncated: bool


def _asker(auth: AuthIdentity) -> uuid.UUID | None:
    """The person the skills are read for, or None when nobody asks for themselves."""
    return (
        None if auth.is_chat_execution or auth.hold is not None or auth.client else auth.account_id
    )


def _info(skill: ServedSkill) -> LibrarySkill:
    return LibrarySkill(
        name=skill.name,
        version=skill.version,
        files=[SkillFileInfo(path=f.path, size=f.size, sha256=f.sha256) for f in skill.files],
    )


def register_skill_serving_tools(mcp: FastMCP, runtime: McpRuntime, *, hub: bool = False) -> None:
    """Register `SERVING_TOOLS`; on the hub each also takes a `workspace`."""
    add = hub_workspace_tool(mcp, runtime) if hub else mcp.tool()

    @add
    async def list_library_skills(ctx: Context) -> list[LibrarySkill]:  # pyright: ignore[reportUnusedFunction]
        """The skills in this workspace's library you may read, each with its latest version and
        files. Read one with `read_library_skill`; start with its SKILL.md."""
        auth = await _auth(ctx)
        async with runtime.session_factory() as session:
            skills = await list_served(
                session,
                skill_scopes(runtime.settings),
                tenant_id=auth.tenant_id,
                account_id=_asker(auth),
            )
        return [_info(s) for s in skills]

    @add
    async def read_library_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, path: str = MAIN_FILE
    ) -> LibrarySkillFile:
        """Read one file of a library skill by its name (SKILL.md by default), as its latest
        version holds it. `list_library_skills` names the files."""
        auth = await _auth(ctx)
        async with runtime.session_factory() as session:
            found = await read_served(
                session,
                skill_scopes(runtime.settings),
                tenant_id=auth.tenant_id,
                account_id=_asker(auth),
                name=name,
                path=path,
            )
        if found is None:
            raise ToolError(_NOT_FOUND)
        skill, body = found
        try:
            text = body.decode()
        except UnicodeDecodeError:
            text = None
        return LibrarySkillFile(
            name=skill.name,
            version=skill.version,
            path=path,
            size=len(body),
            text=None if text is None else text[:MAX_TEXT],
            truncated=text is not None and len(text) > MAX_TEXT,
        )
