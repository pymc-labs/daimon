"""The web app's workspace settings, from chat: modules, who may start turns and
direct messages. Provider and typed-mention settings have their own tools."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import agent_names, now, refused, web_admin_of
from daimon.core import web_admin
from daimon.core.web_admin import ModuleName, WebAdminError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class SettingChange:
    setting: str
    value: str


@dataclass(frozen=True)
class WorkspaceSettingsChanged:
    changes: list[SettingChange]
    """What changed; empty when nothing differed."""


async def _change_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    modules: dict[ModuleName, bool] | None,
    default_agent: str | None,
    everyone_invokes: bool | None,
    invoker_people: list[str],
    dm_enabled: bool | None,
    dm_memory_read_only: bool | None,
    origin_context_id: str,
) -> WorkspaceSettingsChanged:
    actor = await web_admin_of(runtime, auth)
    await require_card(
        runtime, auth, tool_name="change_workspace_settings", origin_context_id=origin_context_id
    )
    try:
        people = [uuid.UUID(p) for p in invoker_people]
    except ValueError as err:
        raise ToolError("invoker_people must be web person ids.") from err
    try:
        changed = await web_admin.change_settings(
            runtime.session_factory,
            actor,
            now(),
            agents=await agent_names(runtime, actor.tenant_id),
            modules=modules,
            default_agent=default_agent,
            everyone_invokes=everyone_invokes,
            invoker_people=people,
            dm_enabled=dm_enabled,
            dm_memory_read_only=dm_memory_read_only,
        )
    except WebAdminError as err:
        raise refused(err) from err
    return WorkspaceSettingsChanged([SettingChange(s, v) for s, v in changed])


def register_web_workspace_settings_tool(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(name="change_workspace_settings", tags={"admin"})
    async def change_settings(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        modules: dict[ModuleName, bool] | None = None,
        default_agent: str | None = None,
        everyone_invokes: bool | None = None,
        invoker_people: list[str] | None = None,
        dm_enabled: bool | None = None,
        dm_memory_read_only: bool | None = None,
    ) -> WorkspaceSettingsChanged:
        """Change the workspace's web modules (chat, agents, channels, routines, credits,
        integrations), its web chat agent, who may start turns (everyone, or exactly the
        web people in ``invoker_people``) and the direct-message rules. Admin-only.
        Runs on the person's Confirm card in chat; every call needs this active turn's
        origin_context_id."""
        return await _change_impl(
            runtime,
            await _auth(ctx),
            modules=modules,
            default_agent=default_agent,
            everyone_invokes=everyone_invokes,
            invoker_people=invoker_people or [],
            dm_enabled=dm_enabled,
            dm_memory_read_only=dm_memory_read_only,
            origin_context_id=origin_context_id,
        )


__all__ = ["register_web_workspace_settings_tool"]
