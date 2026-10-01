"""Channel admin tools: who administers one channel on top of the server admins.

``register_channel_admin_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures;
each delegates to a module-private ``_*_impl`` that tests call without a
FastMCP Context. All three are tenant-wide changes, so server admins only.
"""

from __future__ import annotations

from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_admins import (
    CHANNEL_ADMIN_PLATFORMS,
    InvalidChannelAdminIds,
    normalize_channel_admin_ids,
)
from daimon.core.stores.channel_admins import (
    delete_channel_admins,
    list_channel_admins,
    set_channel_admins,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_NOTE = (
    "Channel admins may change the agents that answer only in channels they administer, "
    "and set or clear those channels' default agent. Server admins always may."
)


@dataclass(frozen=True)
class ChannelAdmins:
    """One channel's admins on top of the server admins."""

    channel_id: str
    role_ids: list[str]
    """Discord role ids; always empty on Slack, which has no roles."""
    user_ids: list[str]


@dataclass(frozen=True)
class ChannelAdminsList:
    """Result returned from list_channel_admins."""

    channels: list[ChannelAdmins]
    """Only channels with at least one admin; every other channel has none."""
    note: str


@dataclass(frozen=True)
class SetChannelAdminsResult:
    """Result returned from set_channel_admins and clear_channel_admins."""

    channel: ChannelAdmins
    """The channel's admins after the change; both lists empty once cleared."""
    changed: bool
    note: str


def _platform(auth: AuthIdentity) -> str:
    if auth.platform not in CHANNEL_ADMIN_PLATFORMS:
        raise ToolError("Channel admins exist only on Discord and Slack.")
    return auth.platform


async def _list_channel_admins_impl(runtime: McpRuntime, auth: AuthIdentity) -> ChannelAdminsList:
    _require_admin(auth)
    platform = _platform(auth)
    async with runtime.session_factory() as session:
        rows = await list_channel_admins(session, tenant_id=auth.tenant_id, platform=platform)
    return ChannelAdminsList(
        channels=[
            ChannelAdmins(
                channel_id=row.channel_id, role_ids=list(row.role_ids), user_ids=list(row.user_ids)
            )
            for row in rows
        ],
        note=_NOTE,
    )


async def _set_channel_admins_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    role_ids: list[str],
    user_ids: list[str],
) -> SetChannelAdminsResult:
    _require_admin(auth)
    platform = _platform(auth)
    try:
        channel, roles, users = normalize_channel_admin_ids(
            platform, channel_id=channel_id, role_ids=role_ids, user_ids=user_ids
        )
    except InvalidChannelAdminIds as exc:
        raise ToolError(f"{exc}. Nothing was changed.") from exc
    if not roles and not users:
        return await _clear_channel_admins_impl(runtime, auth, channel_id=channel)
    async with runtime.session_factory.begin() as session:
        row = await set_channel_admins(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            channel_id=channel,
            role_ids=roles,
            user_ids=users,
            actor_account_id=auth.account_id,
        )
    return SetChannelAdminsResult(
        channel=ChannelAdmins(
            channel_id=row.channel_id, role_ids=list(row.role_ids), user_ids=list(row.user_ids)
        ),
        changed=True,
        note=_NOTE,
    )


async def _clear_channel_admins_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, channel_id: str
) -> SetChannelAdminsResult:
    _require_admin(auth)
    platform = _platform(auth)
    async with runtime.session_factory.begin() as session:
        removed = await delete_channel_admins(
            session, tenant_id=auth.tenant_id, platform=platform, channel_id=channel_id.strip()
        )
    return SetChannelAdminsResult(
        channel=ChannelAdmins(channel_id=channel_id.strip(), role_ids=[], user_ids=[]),
        changed=removed,
        note="Only server admins administer this channel now.",
    )


def register_channel_admin_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def list_channel_admins(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> ChannelAdminsList:
        """List the channels that have their own admins, with the roles and members
        named for each. For example, list the admins of #support. Requires Manage
        Server (admin).
        """
        return await _list_channel_admins_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin"})
    async def set_channel_admins(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        role_ids: list[str],
        user_ids: list[str],
    ) -> SetChannelAdminsResult:
        """Name who administers one channel, on top of the server admins. For example,
        let the @support-leads role run #support. Replaces that channel's whole list;
        pass both lists empty to clear it. Requires Manage Server (admin).

        A channel admin may change agents that answer only in the channels they
        administer (instructions, skills, keys, MCP servers) and set or clear those
        channels' default agent. Built-in agents and the workspace default stay with
        server admins.

        Ids are the platform's own: Discord role and user ids, Slack user ids (Slack
        has no roles, so ``role_ids`` must be empty there). ``channel_id`` MUST be the
        parent channel's id, never a thread's.
        """
        return await _set_channel_admins_impl(
            runtime, await _auth(ctx), channel_id=channel_id, role_ids=role_ids, user_ids=user_ids
        )

    @mcp.tool(tags={"admin"})
    async def clear_channel_admins(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
    ) -> SetChannelAdminsResult:
        """Remove every channel admin from one channel, leaving it to the server admins.
        For example, stop the @support-leads role running #support. A no-op when the
        channel has none. Requires Manage Server (admin).
        """
        return await _clear_channel_admins_impl(runtime, await _auth(ctx), channel_id=channel_id)
