"""A GPT or Gemini turn's own skills, read through Daimon MCP (J6 b).

Off Anthropic, an agent's custom skills don't load by themselves: the turn
lists them (`skill_list`) and reads one (`skill_read`) when it needs it, through
the `call_tool` proxy its MCP catalogue already carries.

Who may read what:
- Only the turn's own credential, and only one that lives for that turn (an
  agent template mints one per turn and revokes it at the end): the budget kept
  on its row is that turn's. An operator row's long-lived credential never
  reads; its skills are deployed natively.
- The agent comes from the credential (`chat_agent_id`), never from the model's
  arguments; a skill resolves only when it is attached to that agent and its
  title carries this tenant's prefix.
- The agent's live pins are read, not a captured revision: a skill attached or
  removed while a conversation runs shows on its next call, as the skill list
  of an Anthropic agent does in its next conversation. The person attaching it
  already holds the right to change the agent.

What it costs, all bounded per turn on the credential's row:
- each call (list or read) is counted before any download, at most
  `SKILL_CALLS_PER_TURN`;
- a read returns at most `SKILL_READ_MAX_BYTES`, and a turn's reads at most
  `SKILL_READ_TURN_BYTES` together;
- `skill_list` reads names only, never a skill's files;
- a version's files are cached per tenant (`_FILES`): a version never changes,
  so a loop of reads downloads it once. One over `_CACHE_MAX_BYTES` isn't cached.
A retried turn mints a new credential and so starts a new budget, bounded by
the turn's own retries.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
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
#: The most skill calls (list and read) one turn makes.
SKILL_CALLS_PER_TURN = 40
#: How long a downloaded version's files stay cached, and how many are kept.
_CACHE_SECONDS = 600
_CACHE_ENTRIES = 64
#: A version larger than this is read but not cached, so the cache holds at most
#: `_CACHE_ENTRIES` times this.
_CACHE_MAX_BYTES = 2 * 1024 * 1024
_FILES: dict[tuple[uuid.UUID, str, str], tuple[float, dict[str, bytes]]] = {}

_NOT_HERE = (
    "Only an agent's own reply on GPT or Gemini reads its skills this way; on "
    "Anthropic they load by themselves. Nothing was read."
)
_SPENT = (
    "This reply has read as much of its skills as it may. Work from what it has "
    "read. Nothing more was read."
)


class SkillEntry(BaseModel):
    name: str


class SkillFile(BaseModel):
    name: str
    path: str
    content: str
    truncated: bool
    files: list[str]


def _require_turn(auth: AuthIdentity) -> uuid.UUID:
    """A provider turn's own credential: no agent key, no held or confirmation turn."""
    if (
        auth.chat_agent_id is None
        or auth.agent_id is not None
        or auth.token_jti is None
        or not auth.no_confirmation_surface
        or auth.hold is not None
    ):
        raise ToolError(_NOT_HERE)
    return auth.token_jti


async def _begin_call(runtime: McpRuntime, auth: AuthIdentity) -> int:
    """Count this call on the turn's credential, before any download: bytes left."""
    jti = _require_turn(auth)
    async with runtime.session_factory() as session, session.begin():
        left = await mcp_tokens.begin_skill_call(
            session,
            jti=jti,
            tenant_id=auth.tenant_id,
            now=datetime.now(UTC),
            max_calls=SKILL_CALLS_PER_TURN,
            budget=SKILL_READ_TURN_BYTES,
        )
    if left is None:
        raise ToolError(_SPENT)
    return left


async def _turn_skills(runtime: McpRuntime, auth: AuthIdentity) -> dict[str, tuple[str, str]]:
    """This turn's agent's own custom skills: name -> (skill id, concrete version)."""
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
    """A version's files, downloaded once per tenant while cached."""
    skill_id, version = pin
    key = (auth.tenant_id, skill_id, version)
    cached = _FILES.get(key)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]
    data = await download_skill_version(
        runtime.client,
        skill_id=skill_id,
        version=version,
        scope=resource_scope(tenant_id=str(auth.tenant_id)),
    )
    files = retained_archive_files(data)
    if sum(len(body) for body in files.values()) > _CACHE_MAX_BYTES:
        return files
    for stale in [k for k, (until, _) in _FILES.items() if until <= now]:
        del _FILES[stale]
    while len(_FILES) >= _CACHE_ENTRIES:
        del _FILES[next(iter(_FILES))]
    _FILES[key] = (now + _CACHE_SECONDS, files)
    return files


async def _skill_list_impl(runtime: McpRuntime, auth: AuthIdentity) -> list[SkillEntry]:
    await _begin_call(runtime, auth)
    return [SkillEntry(name=name) for name in sorted(await _turn_skills(runtime, auth))]


async def _skill_read_impl(
    runtime: McpRuntime, auth: AuthIdentity, name: str, path: str = "SKILL.md"
) -> SkillFile:
    left = await _begin_call(runtime, auth)
    pin = (await _turn_skills(runtime, auth)).get(name)
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
    returned = body[: min(SKILL_READ_MAX_BYTES, left)]
    try:
        text = returned.decode("utf-8")
    except UnicodeDecodeError:
        if len(returned) == len(body):
            raise ToolError(f"{path!r} in {name!r} isn't text. Nothing was read.") from None
        # A cut through a multi-byte character: drop the partial character.
        text = returned.decode("utf-8", errors="ignore")
    async with runtime.session_factory() as session, session.begin():
        charged = await mcp_tokens.charge_skill_bytes(
            session,
            jti=_require_turn(auth),
            tenant_id=auth.tenant_id,
            now=datetime.now(UTC),
            size=len(text.encode("utf-8")),
            budget=SKILL_READ_TURN_BYTES,
        )
    if not charged:
        raise ToolError(_SPENT)
    return SkillFile(
        name=name,
        path=path,
        content=text,
        truncated=len(text.encode("utf-8")) < len(body),
        files=sorted(files),
    )


def register_skill_read_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def skill_list(ctx: Context) -> list[SkillEntry]:  # pyright: ignore[reportUnusedFunction]
        """List your own skills by name. On GPT or Gemini, call this first; then
        skill_read the one that fits before you start."""
        return await _skill_list_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def skill_read(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, path: str = "SKILL.md"
    ) -> SkillFile:
        """Read one of your own skills (from skill_list): its SKILL.md, or another
        of its files by path. Follow its instructions. Each read returns at most
        64 KB, and one reply's reads at most 512 KB together."""
        return await _skill_read_impl(runtime, await _auth(ctx), name, path)
