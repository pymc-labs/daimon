"""Channel environment tools: which environment a channel's turns run in.

``register_channel_environment_tools(mcp, runtime)`` wires the ``@mcp.tool``
closures; each delegates to a module-private ``_*_impl`` that tests call
without a FastMCP Context. The workspace default needs a server admin; a
channel's environment also admits that channel's admins, as its default agent
does.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord import resolve_visible_channel
from daimon.adapters.mcp.tools.reachability import require_scope_admin
from daimon.core.channel_environments import (
    build_clear_environment_note,
    build_set_environment_note,
    save_scope_environment,
)
from daimon.core.defaults.ma_index import find_environment_by_daimon_tag
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class ChannelEnvironmentResult:
    """Result returned from set_channel_environment and clear_channel_environment."""

    scope: str
    """'workspace' or 'channel:<channel_id>'"""
    environment_name: str | None
    """The scope's own environment after the change; None once cleared."""
    previous_environment_name: str | None
    """The environment the scope named before, or None if it had none."""
    changed: bool
    note: str
    """What changed and from when, to report back as is."""


def _scope_label(channel_id: str | None) -> str:
    return f"channel:{channel_id}" if channel_id is not None else "workspace"


async def _environment_channel(
    runtime: McpRuntime, auth: AuthIdentity, channel_id: str | None, *, lenient: bool = False
) -> str | None:
    """The channel an environment is stored under; None is the workspace default.

    A thread id resolves to its parent: Slack's `<channel>:<thread ts>` by
    splitting, a Discord thread through a lookup that also checks the caller
    can see it. `lenient` takes a Discord id the lookup refuses as given, so a
    deleted channel's pick can still be cleared.
    """
    if channel_id is None:
        return None
    target = channel_id.strip()
    if auth.platform == "slack":
        target = target.partition(":")[0].strip()
    if not target:
        raise ToolError(
            "channel_id is empty. Omit it for the workspace default, or pass the channel's id."
        )
    if auth.platform == "slack":
        return target
    if auth.platform != "discord":
        return target
    if not target.isdigit():
        raise ToolError(f"{target!r} is not a Discord channel id")
    if lenient:
        with contextlib.suppress(ToolError):
            return await resolve_visible_channel(runtime, auth, target)
        return target
    return await resolve_visible_channel(runtime, auth, target)


async def _set_channel_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    environment_name: str,
    channel_id: str | None,
) -> ChannelEnvironmentResult:
    channel_id = await _environment_channel(runtime, auth, channel_id)
    await require_scope_admin(runtime, auth, channel_id=channel_id)
    name = environment_name.strip()
    environment = await find_environment_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=name
    )
    if environment is None:
        raise ToolError(
            f"No environment named '{name}' exists in this workspace. Nothing was changed. "
            "Use list_environments to pick an existing one, or create_environment first."
        )
    async with runtime.session_factory.begin() as session:
        previous = await save_scope_environment(
            session,
            tenant_id=auth.tenant_id,
            channel_id=channel_id,
            environment_name=name,
            actor_account_id=auth.account_id,
        )
    return ChannelEnvironmentResult(
        scope=_scope_label(channel_id),
        environment_name=name,
        previous_environment_name=previous,
        changed=previous != name,
        note=build_set_environment_note(environment_name=name, channel=channel_id),
    )


async def _clear_channel_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str | None,
) -> ChannelEnvironmentResult:
    channel_id = await _environment_channel(runtime, auth, channel_id, lenient=True)
    await require_scope_admin(runtime, auth, channel_id=channel_id)
    async with runtime.session_factory.begin() as session:
        previous = await save_scope_environment(
            session,
            tenant_id=auth.tenant_id,
            channel_id=channel_id,
            environment_name=None,
            actor_account_id=auth.account_id,
        )
    return ChannelEnvironmentResult(
        scope=_scope_label(channel_id),
        environment_name=None,
        previous_environment_name=previous,
        changed=previous is not None,
        note=build_clear_environment_note(channel=channel_id, cleared=previous is not None),
    )


def register_channel_environment_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", "channel-admin"})
    async def set_channel_environment(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        environment_name: str,
        channel_id: str | None = None,
    ) -> ChannelEnvironmentResult:
        """Choose the environment a channel's turns run in, or the workspace default.
        For example, run #growth in the pymc environment. Changes where the agent runs,
        not which agent answers; use ``set_agent_default`` for that.

        Omit ``channel_id`` to set the workspace default, which every channel without
        its own environment uses. The environment must already exist
        (``list_environments``). Conversations pick it up from their next message.
        The workspace default requires Manage Server (admin); an admin of the channel
        may set that channel's environment.

        Pass the parent channel's id: Discord
        ``<channel platform="discord" id="..." role="parent_channel">``, Slack
        ``<channel platform="slack" id="...">``; a thread id resolves to its parent.
        """
        return await _set_channel_environment_impl(
            runtime, await _auth(ctx), environment_name=environment_name, channel_id=channel_id
        )

    @mcp.tool(tags={"admin", "channel-admin"})
    async def clear_channel_environment(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str | None = None,
    ) -> ChannelEnvironmentResult:
        """Stop a channel picking its own environment, so it uses the workspace default.
        For example, put #growth back on the default environment. A no-op when the
        channel has none of its own.

        Omit ``channel_id`` to clear the workspace default, leaving the deployment
        default. The workspace default requires Manage Server (admin); an admin of the
        channel may clear that channel's environment. A thread id resolves to its
        parent channel.
        """
        return await _clear_channel_environment_impl(
            runtime, await _auth(ctx), channel_id=channel_id
        )
