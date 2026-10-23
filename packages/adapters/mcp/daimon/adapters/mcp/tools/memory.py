"""Memory tools for provider turns that cannot mount memory (Gemini, later OpenAI).

Only a `provider_turn` credential with a `TurnMemoryGrant` (looked up by its jti)
may call them; every other identity is refused. Paths are `shared/<path>` for
the agent's memory and `personal/<path>` for the caller's own, exactly as the
native mount separates them. See `daimon.core.memory_tools`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core import memory_tools
from daimon.core.memory_tools import MemoryRefused
from daimon.core.stores.turn_memory_grants import TurnMemoryGrantRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class MemoryListing:
    memories: list[memory_tools.MemoryEntry]
    personal_available: bool
    read_only: bool


@dataclass(frozen=True)
class MemoryContent:
    path: str
    content: str | None
    """None when there is no memory at that path."""


async def _grant(runtime: McpRuntime, auth: AuthIdentity) -> TurnMemoryGrantRow:
    if not auth.no_confirmation_surface or auth.token_jti is None:
        raise ToolError("Memory isn't available in this conversation.")
    try:
        return await memory_tools.turn_grant(
            runtime.session_factory,
            jti=auth.token_jti,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            agent_id=auth.chat_agent_id,
            now=datetime.now(UTC),
        )
    except MemoryRefused as exc:
        raise ToolError(str(exc)) from exc


async def _memory_list_impl(runtime: McpRuntime, auth: AuthIdentity) -> MemoryListing:
    grant = await _grant(runtime, auth)
    try:
        entries = await memory_tools.list_memories(runtime.client, runtime.session_factory, grant)
        writable = await memory_tools.writable_now(runtime.session_factory, grant)
    except MemoryRefused as exc:
        raise ToolError(str(exc)) from exc
    return MemoryListing(
        memories=entries, personal_available=grant.personal, read_only=not writable
    )


async def _memory_read_impl(runtime: McpRuntime, auth: AuthIdentity, path: str) -> MemoryContent:
    grant = await _grant(runtime, auth)
    try:
        content = await memory_tools.read_memory_at(
            runtime.client, runtime.session_factory, grant, path
        )
    except MemoryRefused as exc:
        raise ToolError(str(exc)) from exc
    return MemoryContent(path=path, content=content)


async def _memory_write_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    path: str,
    content: str,
    expected_content_sha256: str | None,
) -> str:
    grant = await _grant(runtime, auth)
    try:
        digest = await memory_tools.write_memory_at(
            runtime.client,
            runtime.session_factory,
            grant,
            path,
            content,
            expected_content_sha256=expected_content_sha256,
        )
    except MemoryRefused as exc:
        raise ToolError(str(exc)) from exc
    return f"Saved {path} (content_sha256 {digest})."


async def _memory_delete_impl(
    runtime: McpRuntime, auth: AuthIdentity, path: str, expected_content_sha256: str
) -> str:
    grant = await _grant(runtime, auth)
    try:
        await memory_tools.delete_memory_at(
            runtime.client,
            runtime.session_factory,
            grant,
            path,
            expected_content_sha256=expected_content_sha256,
        )
    except MemoryRefused as exc:
        raise ToolError(str(exc)) from exc
    return f"Deleted {path}."


def register_memory_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def memory_list(ctx: Context) -> MemoryListing:  # pyright: ignore[reportUnusedFunction]
        """List your memories (path and content_sha256), and whether memory is read-only here.

        shared/ is this agent's memory for everyone it works with; personal/ is only
        the person you are talking with, when there is exactly one. Never put one
        person's private details in shared/.
        """
        return await _memory_list_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def memory_read(ctx: Context, path: str) -> MemoryContent:  # pyright: ignore[reportUnusedFunction]
        """Read one memory by path: shared/<path> or personal/<path>."""
        return await _memory_read_impl(runtime, await _auth(ctx), path)

    @mcp.tool
    async def memory_write(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, path: str, content: str, expected_content_sha256: str | None = None
    ) -> str:
        """Save a memory at shared/<path> or personal/<path>.

        To replace an existing memory, read it first and pass its content_sha256;
        a memory that changed since is never overwritten. Refused where memory is
        read-only.
        """
        return await _memory_write_impl(
            runtime, await _auth(ctx), path, content, expected_content_sha256
        )

    @mcp.tool
    async def memory_delete(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, path: str, expected_content_sha256: str
    ) -> str:
        """Delete one memory, only while it still has the content_sha256 you read."""
        return await _memory_delete_impl(runtime, await _auth(ctx), path, expected_content_sha256)
