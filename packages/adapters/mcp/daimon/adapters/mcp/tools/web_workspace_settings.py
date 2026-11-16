"""The web app's workspace settings, from chat: modules, who may start turns and
direct messages. Provider and typed-mention settings have their own tools."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import agent_names, now, refused, web_admin_of
from daimon.core import dm_settings, web_admin
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.dm_access import dm_agents_of
from daimon.core.dm_settings import MAX_SUBJECTS, DmActor, DmRuleSpec, PlatformId
from daimon.core.errors import UserFacingError
from daimon.core.stores.domain import DmRuleSubject
from daimon.core.web_admin import ModuleName, WebAdmin, WebAdminError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class SettingChange:
    setting: str
    value: str


class DmRule(BaseModel):
    """One direct-message rule: whom it matches (``subject_id`` for a user or group),
    the agent they get (none: the default agent) and whether they may switch."""

    model_config = ConfigDict(extra="forbid")
    subject: DmRuleSubject
    subject_id: PlatformId | None = None
    agent: str | None = None
    locked: bool = False


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
    dm_default_agent: str | None = None,
    dm_rules: list[DmRule] | None = None,
) -> WorkspaceSettingsChanged:
    actor = await web_admin_of(runtime, auth)
    await require_card(
        runtime, auth, tool_name="change_workspace_settings", origin_context_id=origin_context_id
    )
    try:
        people = [uuid.UUID(p) for p in invoker_people]
    except ValueError as err:
        raise ToolError("invoker_people must be web person ids.") from err
    dm_changed = await _change_dm(runtime, actor, dm_default_agent, dm_rules)
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
            surface="chat",
        )
    except WebAdminError as err:
        raise refused(err) from err
    return WorkspaceSettingsChanged([*dm_changed, *(SettingChange(s, v) for s, v in changed)])


async def _change_dm(
    runtime: McpRuntime, admin: WebAdmin, default: str | None, rules: list[DmRule] | None
) -> list[SettingChange]:
    """The DM default agent ("" clears it) and the whole ordered rule list, as the web sets them."""
    if default is None and rules is None:
        return []
    agents = dm_agents_of(
        await list_agents_by_tenant(
            runtime.client, tenant_id=admin.tenant_id, records=runtime.session_factory
        )
    )
    actor = DmActor(admin.tenant_id, admin.account_id, now())
    specs = [DmRuleSpec(r.subject, r.subject_id, r.agent, r.locked) for r in rules or ()]
    changes: list[SettingChange] = []
    try:
        async with runtime.session_factory.begin() as db:
            if await dm_settings.dm_platform(db, admin.tenant_id) is None:
                raise ToolError("This workspace has no direct messages.")
            if default is not None and await dm_settings.set_default(
                db, actor, agents, default or None
            ):
                changes.append(SettingChange("DM default agent", default or "None"))
            if rules is not None and await dm_settings.replace_rules(db, actor, agents, specs):
                changes.append(SettingChange("DM rules", f"{len(specs)} rules"))
    except UserFacingError as err:
        raise ToolError(str(err)) from err
    return changes


def register_web_workspace_settings_tool(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(name="change_workspace_settings", tags={"admin"})
    async def change_settings(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        modules: dict[ModuleName, bool] | None = None,
        default_agent: str = "",
        everyone_invokes: bool | None = None,
        invoker_people: list[str] | None = None,
        dm_enabled: bool | None = None,
        dm_memory_read_only: bool | None = None,
        dm_default_agent: str | None = None,
        dm_rules: Annotated[list[DmRule], Field(max_length=MAX_SUBJECTS)] | None = None,
    ) -> WorkspaceSettingsChanged:
        """Change the web modules, web chat agent, who may start turns (everyone or exactly
        ``invoker_people``) and the direct-message settings. ``dm_default_agent`` is the agent
        a DM starts with ("" clears it); ``dm_rules`` replaces the ordered rule list, first
        match wins."""
        return await _change_impl(
            runtime,
            await _auth(ctx),
            modules=modules,
            default_agent=default_agent or None,
            everyone_invokes=everyone_invokes,
            invoker_people=invoker_people or [],
            dm_enabled=dm_enabled,
            dm_memory_read_only=dm_memory_read_only,
            origin_context_id=origin_context_id,
            dm_default_agent=dm_default_agent,
            dm_rules=dm_rules,
        )


__all__ = ["register_web_workspace_settings_tool"]
