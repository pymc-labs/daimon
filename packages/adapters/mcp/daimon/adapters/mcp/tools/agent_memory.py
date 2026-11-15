"""Memory tools for agents whose backend has no memory stores of its own.

Hidden unless the credential is a provider turn's (`HOST_MEMORY_TAG`); the
store is chosen from the credential and turn origin (`daimon.core.host_memory`).
"""

from __future__ import annotations

from collections.abc import Awaitable

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.anthropic_guard import anthropic_configured
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.host_memory import (
    HOST_MEMORY_TAG,
    HostMemory,
    HostMemoryError,
    HostMemoryPlace,
    choose_store,
)
from daimon.core.ma_identity import derive_agent_uuid
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def open_host_memory(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    personal: bool,
) -> HostMemory:
    """The turn's memory store, refused where a Claude session would mount none."""
    if auth.routine_id is not None or auth.execution_id is not None or auth.is_external_agent:
        raise ToolError("Memory tools need a conversation.")
    if (
        not auth.no_confirmation_surface
        or auth.anthropic_disabled
        or not anthropic_configured(runtime.client)
    ):
        raise ToolError("Memory is not available for this agent.")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if not origin.attended and not auth.host_builtins:
        # Memory is a built-in on Claude too: off unattended unless policy allows it.
        raise ToolError("Memory is off when nobody is in the conversation.")
    features = await resolve_features(
        runtime.session_factory,
        runtime.settings,
        FeaturePlace.of(
            auth.tenant_id,
            origin.platform,
            origin.parent_channel_id,
            origin.thread_id,
            agent_id=origin.responder_ma_agent_id,
        ),
        keys=("memory_shared", "memory_personal", "memory_channel", "dm_memory_read_only"),
    )
    is_dm = origin.thread_id.startswith("dm:")
    place = HostMemoryPlace(
        tenant_id=auth.tenant_id,
        agent_id=derive_agent_uuid(
            tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id
        ),
        agent_name=origin.responder_name,
        account_id=auth.account_id,
        hold=auth.hold,
        shared_thread=auth.requester_account_id is not None,
        read_only=origin.memory_read_only is not False
        or (is_dm and features.on("dm_memory_read_only")),
    )
    try:
        kind = choose_store(
            place,
            personal=personal,
            shared_on=features.on("memory_shared"),
            personal_on=features.on("memory_personal"),
            channel_on=features.on("memory_channel"),
        )
    except HostMemoryError as exc:
        raise ToolError(str(exc)) from exc
    return HostMemory(runtime.client, runtime.session_factory, place, kind)


async def _open(
    ctx: Context, runtime: McpRuntime, origin_context_id: str, personal: bool
) -> HostMemory:
    return await open_host_memory(
        runtime, await _auth(ctx), origin_context_id=origin_context_id, personal=personal
    )


async def _answer(call: Awaitable[str]) -> str:
    try:
        return await call
    except HostMemoryError as exc:
        raise ToolError(str(exc)) from exc


def register_agent_memory_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    tags = {HOST_MEMORY_TAG}

    @mcp.tool(tags=tags)
    async def memory_view(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str, path: str = "/", personal: bool = False
    ) -> str:
        """Read a memory file, or list the memory paths under a directory.

        Memory persists across conversations. `personal` selects the caller's own
        memory instead of the agent's shared memory.
        """
        memory = await _open(ctx, runtime, origin_context_id, personal)
        return await _answer(memory.view(path))

    @mcp.tool(tags=tags)
    async def memory_create(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str, path: str, content: str, personal: bool = False
    ) -> str:
        """Save a new memory file at `path` (for example `/notes/project.md`)."""
        memory = await _open(ctx, runtime, origin_context_id, personal)
        return await _answer(memory.create(path, content))

    @mcp.tool(tags=tags)
    async def memory_str_replace(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        path: str,
        old_str: str,
        new_str: str,
        personal: bool = False,
    ) -> str:
        """Replace the one occurrence of `old_str` in a memory file with `new_str`."""
        memory = await _open(ctx, runtime, origin_context_id, personal)
        return await _answer(memory.str_replace(path, old_str, new_str))

    @mcp.tool(tags=tags)
    async def memory_delete(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str, path: str, personal: bool = False
    ) -> str:
        """Delete a memory file."""
        memory = await _open(ctx, runtime, origin_context_id, personal)
        return await _answer(memory.delete(path))
