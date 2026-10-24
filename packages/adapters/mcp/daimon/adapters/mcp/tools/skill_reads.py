"""A GPT or Gemini turn's own skills, read through Daimon MCP (J6 b).

Off Anthropic, an agent's custom skills don't load by themselves: the turn
lists them (`skill_list`) and reads one (`skill_read`) when it needs it. Only
the turn's own credential reads, and only the skills attached to the agent it
runs as: the agent comes from the credential (`chat_agent_id`), never from the
model's arguments, and a skill is this tenant's only when its title carries
the tenant's prefix. Reads are read-only, cut at `SKILL_READ_MAX_BYTES`, and a
turn's total is capped at `SKILL_READ_TURN_BYTES` on its credential's row, so
a loop of reads can't run up its tokens.
"""

from __future__ import annotations

import asyncio

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.defaults.loader import parse_skill_markdown
from daimon.core.defaults.ma_index import (
    download_skill_version,
    find_agent_by_derived_uuid,
    list_skills_strict,
)
from daimon.core.defaults.metadata import strip_tenant_prefix
from daimon.core.errors import DaimonError
from daimon.core.mux_backend import resource_scope
from daimon.core.skills.ingest import retained_archive_files
from daimon.core.stores import mcp_tokens
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

#: The most one `skill_read` returns.
SKILL_READ_MAX_BYTES = 64 * 1024
#: The most all of one turn's `skill_read` calls return together.
SKILL_READ_TURN_BYTES = 512 * 1024

_NOT_HERE = (
    "Only an agent's own reply on GPT or Gemini reads its skills this way; on "
    "Anthropic they load by themselves. Nothing was read."
)


class SkillEntry(BaseModel):
    name: str
    description: str


class SkillFile(BaseModel):
    name: str
    path: str
    content: str
    truncated: bool
    files: list[str]


def _require_turn(auth: AuthIdentity) -> None:
    """A provider turn's own credential: no agent key, no held or confirmation turn."""
    if (
        auth.chat_agent_id is None
        or auth.agent_id is not None
        or auth.token_jti is None
        or not auth.no_confirmation_surface
        or auth.hold is not None
    ):
        raise ToolError(_NOT_HERE)


async def _turn_skills(runtime: McpRuntime, auth: AuthIdentity) -> dict[str, tuple[str, str]]:
    """This turn's agent's own custom skills: name -> (skill id, concrete version)."""
    _require_turn(auth)
    assert auth.chat_agent_id is not None
    agent = await find_agent_by_derived_uuid(
        runtime.client, tenant_id=auth.tenant_id, agent_id=auth.chat_agent_id
    )
    if agent is None:
        raise ToolError("The agent replying here wasn't found. Nothing was read.")
    custom = [skill for skill in agent.skills or () if skill.type == "custom"]
    if not custom:
        return {}
    rows = {row.id: row for row in await list_skills_strict(runtime.client)}
    skills: dict[str, tuple[str, str]] = {}
    for skill in custom:
        row = rows.get(skill.skill_id)
        if row is None:
            continue
        # Another tenant's skill has another prefix: it never resolves here.
        name = strip_tenant_prefix(tenant_id=auth.tenant_id, display_title=row.display_title or "")
        pinned = getattr(skill, "version", None)
        version = pinned if pinned not in (None, "latest") else row.latest_version
        if name and version:
            skills[name] = (skill.skill_id, str(version))
    return skills


async def _files(runtime: McpRuntime, auth: AuthIdentity, pin: tuple[str, str]) -> dict[str, bytes]:
    skill_id, version = pin
    data = await download_skill_version(
        runtime.client,
        skill_id=skill_id,
        version=version,
        scope=resource_scope(tenant_id=str(auth.tenant_id)),
    )
    return retained_archive_files(data)


def _description(skill_md: bytes) -> str:
    try:
        spec, _ = parse_skill_markdown(skill_md.decode("utf-8"), source="SKILL.md")
    except (UnicodeDecodeError, DaimonError):
        return ""
    return spec.description.strip()


async def _skill_list_impl(runtime: McpRuntime, auth: AuthIdentity) -> list[SkillEntry]:
    skills = await _turn_skills(runtime, auth)

    async def entry(name: str, pin: tuple[str, str]) -> SkillEntry:
        try:
            files = await _files(runtime, auth, pin)
        except DaimonError:
            return SkillEntry(name=name, description="")
        return SkillEntry(name=name, description=_description(files["SKILL.md"]))

    return list(await asyncio.gather(*(entry(n, p) for n, p in sorted(skills.items()))))


async def _skill_read_impl(
    runtime: McpRuntime, auth: AuthIdentity, name: str, path: str = "SKILL.md"
) -> SkillFile:
    skills = await _turn_skills(runtime, auth)
    pin = skills.get(name)
    if pin is None:
        raise ToolError(
            f"{name!r} isn't one of this agent's skills. Call skill_list for the ones it has."
        )
    try:
        files = await _files(runtime, auth, pin)
    except DaimonError:
        raise ToolError(f"The skill {name!r} couldn't be read. Nothing was read.") from None
    body = files.get(path)
    if body is None:
        raise ToolError(f"{name!r} has no file {path!r}. Its files: {', '.join(sorted(files))}.")
    returned = body[:SKILL_READ_MAX_BYTES]
    try:
        text = returned.decode("utf-8")
    except UnicodeDecodeError:
        # A cut through a multi-byte character: drop the partial character.
        text = returned.decode("utf-8", errors="ignore")
        if len(returned) == len(body):
            raise ToolError(f"{path!r} in {name!r} isn't text. Nothing was read.") from None
    assert auth.token_jti is not None
    async with runtime.session_factory() as session, session.begin():
        charged = await mcp_tokens.charge_skill_bytes(
            session,
            jti=auth.token_jti,
            tenant_id=auth.tenant_id,
            size=len(text.encode("utf-8")),
            budget=SKILL_READ_TURN_BYTES,
        )
    if not charged:
        raise ToolError(
            "This reply has read as much of its skills as it may (512 KB). Work from what "
            "it has read. Nothing more was read."
        )
    return SkillFile(
        name=name,
        path=path,
        content=text,
        truncated=len(body) > SKILL_READ_MAX_BYTES,
        files=sorted(files),
    )


def register_skill_read_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def skill_list(ctx: Context) -> list[SkillEntry]:  # pyright: ignore[reportUnusedFunction]
        """List your own skills, each with when to use it. On GPT or Gemini, call
        this first; then skill_read the one that fits before you start."""
        return await _skill_list_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def skill_read(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, path: str = "SKILL.md"
    ) -> SkillFile:
        """Read one of your own skills (from skill_list): its SKILL.md, or another
        of its files by path. Follow its instructions. Each read returns at most
        64 KB, and one reply's reads at most 512 KB together."""
        return await _skill_read_impl(runtime, await _auth(ctx), name, path)
