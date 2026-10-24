"""Watch authorization invariant: every decision input is fetched by REST in
this check: guild identity/owner, roles/permissions, member identity/roles/timeout,
parent overwrites/category, thread identity/type/lock and private-thread membership.
Channel and category protection rules are loaded from the DB in the same check;
REST category metadata is carried into admission, never resolved from the gateway.
Only the credentialed HTTP transport is shared with the gateway. All Discord
objects are parsed into a new, empty ConnectionState; no gateway guild, role,
member, channel or thread object (or cache-derived attribute) supplies authority.
Tenant and destination IDs come from the persisted binding. A missing, invalid
or failed REST proof refuses output without parent fallback. Only proved loss of
access pauses the watch; transient transport failures refuse that output alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.watches import get_firing, pause_watch
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.protection import protection_state
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord
from discord.state import ConnectionState


@dataclass(frozen=True)
class WatchThreadAccess:
    member: discord.Member
    category_id: str | None


def _discard_event(*args: object, **kwargs: object) -> None:
    pass


async def require_watch_thread_access(
    sessions: async_sessionmaker[AsyncSession],
    row: TaskContinuationRow,
    client: discord.Client,
) -> WatchThreadAccess:
    """Never use cached thread membership or redirect a refused wake to its parent."""
    reason: str | None = None
    member: discord.Member | None = None
    category_id: str | None = None
    try:
        async with sessions() as db:
            tenant = await get_tenant(db, row.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            raise ValueError("Watch guild is unavailable")
        guild_id = int(tenant.external_id)
        state = ConnectionState(
            dispatch=_discard_event,
            handlers={},
            hooks={},
            http=client.http,
            intents=discord.Intents.none(),
        )
        guild = discord.Guild(
            data=await client.http.get_guild(guild_id, with_counts=False), state=state
        )
        if guild.id != guild_id or guild.owner_id is None:
            raise ValueError("Watch guild binding changed")
        roles = await guild.fetch_roles()
        guild._roles = {role.id: role for role in roles}  # pyright: ignore[reportPrivateUsage]
        if not any(role.id == guild_id for role in roles):
            raise ValueError("Watch guild roles are incomplete")
        current = await guild.fetch_channel(int(row.thread_id))
        if (
            not isinstance(current, discord.Thread)
            or str(current.id) != row.thread_id
            or str(current.parent_id) != row.parent_channel_id
        ):
            raise ValueError("Reporting thread binding changed")
        thread = current
        member = await guild.fetch_member(int(row.requester_external_user_id))
        if str(member.id) != row.requester_external_user_id:
            raise ValueError("Watch member binding changed")
        parent = await guild.fetch_channel(thread.parent_id)
        if (
            not isinstance(parent, (discord.TextChannel, discord.ForumChannel))
            or str(parent.id) != row.parent_channel_id
        ):
            reason = "Watch paused: reporting thread parent is unavailable."
        else:
            category_id = str(parent.category_id) if parent.category_id is not None else None

            async def rest_category() -> tuple[str | None, bool]:
                return category_id, False

            protection = await protection_state(
                sessions,
                tenant_id=row.tenant_id,
                channel_id=row.parent_channel_id,
                thread_id=row.thread_id,
                resolve_category=rest_category,
            )
            permissions = parent.permissions_for(member)
            if not protection.may_post:
                reason = "Watch paused: channel/category rules block or cannot verify output."
            elif not permissions.view_channel:
                reason = "Watch paused: owner can no longer view the reporting channel."
            elif not permissions.read_message_history:
                reason = "Watch paused: owner can no longer read reporting channel history."
            elif not permissions.send_messages_in_threads:
                reason = "Watch paused: owner can no longer post in the reporting thread."
            elif thread.locked and not permissions.manage_threads:
                reason = "Watch paused: reporting thread is locked and owner cannot manage threads."
            elif (
                thread.type is discord.ChannelType.private_thread and not permissions.manage_threads
            ):
                # REST, not Thread.get_member: gateway membership may be stale.
                membership = await thread.fetch_member(member.id)
                if membership.id != member.id or membership.thread_id != thread.id:
                    raise ValueError("Watch thread membership binding changed")
    except (discord.Forbidden, discord.NotFound, ValueError):
        reason = "Watch paused: reporting thread or owner membership is unavailable."
    if reason is not None:
        if row.reason == "watch_fired":
            async with sessions.begin() as db:
                firing = await get_firing(db, row.idempotency_key)
                if firing is not None:
                    await pause_watch(db, firing.watch_id, reason=reason)
        raise AdmissionDenied(reason="invoker_not_allowed")
    assert member is not None
    return WatchThreadAccess(member=member, category_id=category_id)
