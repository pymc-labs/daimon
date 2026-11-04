"""Thread settings that differ from their channel, and clearing one."""

from __future__ import annotations

from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import chat_actor
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_settings import (
    ThreadOverrideKind,
    clear_thread_override,
    list_thread_overrides,
)
from daimon.core.errors import DaimonError, StaleChannelValue
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class ThreadOverrideEntry:
    thread_id: str
    kind: ThreadOverrideKind
    value: str


@dataclass(frozen=True)
class ThreadOverrides:
    channel_id: str
    overrides: list[ThreadOverrideEntry]


@dataclass(frozen=True)
class ThreadOverrideCleared:
    channel_id: str
    thread_id: str
    kind: ThreadOverrideKind
    cleared: bool


async def _list_impl(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> ThreadOverrides:
    require_scope(auth, "tenant:read")
    _require_admin(auth, action=DelegatedAction.CHANNEL_PERMISSIONS)
    async with runtime.session_factory() as session:
        found = await list_thread_overrides(
            session, tenant_id=auth.tenant_id, platform=auth.platform or "", channel_id=channel_id
        )
    return ThreadOverrides(
        channel_id=channel_id,
        overrides=[ThreadOverrideEntry(o.thread_id, o.kind, o.value) for o in found],
    )


async def _clear_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    thread_id: str,
    kind: ThreadOverrideKind,
) -> ThreadOverrideCleared:
    require_scope(auth, "channels:write")
    _require_admin(auth, action=DelegatedAction.CHANNEL_PERMISSIONS)
    try:
        await clear_thread_override(
            runtime.session_factory,
            chat_actor(auth, DelegatedAction.CHANNEL_PERMISSIONS),
            platform=auth.platform or "",
            channel_id=channel_id,
            thread_id=thread_id,
            kind=kind,
        )
    except StaleChannelValue:
        return ThreadOverrideCleared(channel_id, thread_id, kind, cleared=False)
    except DaimonError as err:
        raise ToolError(str(err)) from err
    return ThreadOverrideCleared(channel_id, thread_id, kind, cleared=True)


def register_thread_override_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(name="list_thread_overrides", tags={"admin", *scope_tags("tenant:read")})
    async def list_overrides(ctx: Context, channel_id: str) -> ThreadOverrides:  # pyright: ignore[reportUnusedFunction]
        """List the threads of a channel that keep their own participation, progress plan,
        access rule or handoff. Admin-only."""
        return await _list_impl(runtime, await _auth(ctx), channel_id)

    @mcp.tool(name="clear_thread_override", tags={"admin", *scope_tags("channels:write")})
    async def clear_override(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, channel_id: str, thread_id: str, kind: ThreadOverrideKind
    ) -> ThreadOverrideCleared:
        """Drop one thread's own setting so it follows its channel again. Admin-only.

        ``kind`` is participation, progress_plans, thread_rule or handoff, as listed.
        ``cleared`` is false when the thread had no such setting.
        """
        return await _clear_impl(
            runtime, await _auth(ctx), channel_id=channel_id, thread_id=thread_id, kind=kind
        )


__all__ = ["register_thread_override_tools"]
