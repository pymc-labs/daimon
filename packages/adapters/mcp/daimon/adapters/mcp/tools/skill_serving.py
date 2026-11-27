"""Serve the workspace library's skills to coding keys, the hub and outside clients, behind
`library_skills`: list them and read their files. Chat turns never get these tools.

A scoped skill shows only to a caller who holds its scope under the data audience rule
(`skill_hider`); one refused data sees unscoped skills only. A skill
of an agent across an isolated channel's line stays hidden, as `get_skill` hides it.
"""

from __future__ import annotations

from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._hub_workspace import hub_workspace_tool
from daimon.adapters.mcp.tools.skills import skill_hider
from daimon.core.skill_serving import MAIN_FILE, ServedSkill, read_served, served
from daimon.core.stores import workspace_skills as store
from daimon.core.stores.domain import WorkspaceSkillRow
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


async def visible_skills(runtime: McpRuntime, auth: AuthIdentity) -> list[WorkspaceSkillRow]:
    """The library skills `auth` may read: by scope and by the channel line."""
    async with runtime.session_factory() as session:
        rows = await store.list_skills(session, tenant_id=auth.tenant_id)
    hides = await skill_hider(runtime, auth)
    return [r for r in rows if not hides(r.skill_id, r.name)]


def _info(skill: ServedSkill) -> LibrarySkill:
    return LibrarySkill(
        name=skill.name,
        version=skill.version,
        files=[SkillFileInfo(path=f.path, size=f.size, sha256=f.sha256) for f in skill.files],
    )


def register_skill_serving_tools(mcp: FastMCP, runtime: McpRuntime, *, hub: bool = False) -> None:
    """Register `SERVING_TOOLS` (behind `library_skills`); on the hub each also takes a
    `workspace`."""
    add = hub_workspace_tool(mcp, runtime) if hub else mcp.tool()

    @add
    async def list_library_skills(ctx: Context) -> list[LibrarySkill]:  # pyright: ignore[reportUnusedFunction]
        """The skills in this workspace's library you may read, each with its latest version and
        files. Read one with `read_library_skill`."""
        rows = await visible_skills(runtime, await _auth(ctx))
        async with runtime.session_factory() as session:
            return [_info(await served(session, row)) for row in rows]

    @add
    async def read_library_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, path: str = MAIN_FILE
    ) -> LibrarySkillFile:
        """Read one file of a library skill by its name (its main file by default), as its latest
        version holds it. `list_library_skills` names the files."""
        rows = await visible_skills(runtime, await _auth(ctx))
        async with runtime.session_factory() as session:
            found = await read_served(session, rows, name=name, path=path)
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
