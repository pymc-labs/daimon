"""Load + sort + cap the (user, tenant)-attributed billing snapshot for /billing.

Core's snapshot (`daimon.core.billing_panel`), with the top spenders the panel
shows named from Discord: the member cache, else one member fetch each under a
short shared timeout, else the name stored when they were last seen, else
their account's own name.

`is_platform_admin` is Discord-native: manage_guild | administrator | owner_id.
Gating on a daimon-DB role would block legitimate guild admins, so we resolve
permissions from Discord at click time instead.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import structlog
from daimon.adapters.discord.checks import is_platform_admin as is_platform_admin
from daimon.core.billing_panel import TOP_SPENDERS_SHOWN, BillingPanelState, MemberRow
from daimon.core.billing_panel import load_billing_snapshot as load_core_snapshot
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.platform_names import KnownName, remember_user_names, resolve_names
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord
from discord import Interaction
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

_log = structlog.get_logger()

# The panel waits at most this long for the member fetches, all together.
NAME_FETCH_TIMEOUT_S = 1.5


def invoking_channel_id(interaction: BotInteraction) -> str | None:
    """The channel a budget applies to: the invoking channel, or a thread's parent."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return str(channel.parent_id)
    return str(interaction.channel_id) if interaction.channel_id is not None else None


def invoking_thread_id(interaction: BotInteraction) -> str | None:
    """The invoking thread, or None outside one."""
    channel = interaction.channel
    return str(channel.id) if isinstance(channel, discord.Thread) else None


def _snowflake(user_id: str) -> int | None:
    try:
        return int(user_id)
    except ValueError:
        return None


def _member_name(member: discord.Member) -> KnownName:
    return KnownName(display_name=member.display_name, handle=member.name)


async def _fetch_member_name(guild: discord.Guild, user_id: str) -> KnownName | None:
    """The member's name from the cache, else one REST fetch; None when Discord can't say.

    A single-member fetch needs no privileged members intent. NotFound (they
    left), Forbidden and any other HTTP error all read as unknown.
    """
    if (snowflake := _snowflake(user_id)) is None:
        return None
    if (cached := guild.get_member(snowflake)) is not None:
        return _member_name(cached)
    try:
        member = await guild.fetch_member(snowflake)
    except discord.HTTPException as exc:
        _log.info("billing.member_fetch_failed", status=exc.status)
        return None
    return _member_name(member)


async def _fetch_user_name(client: discord.Client, user_id: str) -> KnownName | None:
    """The account's own name, which Discord gives for someone who left the server too."""
    if (snowflake := _snowflake(user_id)) is None:
        return None
    try:
        user = await client.fetch_user(snowflake)
    except discord.HTTPException as exc:
        _log.info("billing.user_fetch_failed", status=exc.status)
        return None
    return KnownName(display_name=user.global_name, handle=user.name)


async def resolve_shown_names(
    guild: discord.Guild,
    rows: tuple[MemberRow, ...],
    *,
    client: discord.Client | None = None,
    timeout_s: float = NAME_FETCH_TIMEOUT_S,
) -> tuple[tuple[MemberRow, ...], dict[str, KnownName]]:
    """Name the rows the panel shows, and return the names Discord gave, to remember.

    Per person (`daimon.core.platform_names.resolve_names`): the member cache,
    else one member fetch; else the row's stored name; else, for someone never
    named to us, ``client``'s user fetch, which answers for people who left.
    All fetches share ``timeout_s``; whoever is still waiting keeps their
    stored name. A row nobody could name keeps ``display_name=None``, which the
    panel shows as a mention.
    """
    shown = rows[:TOP_SPENDERS_SHOWN]
    stored = {row.platform_user_id: row.display_name for row in shown if row.display_name}

    async def live(user_id: str) -> KnownName | None:
        return await _fetch_member_name(guild, user_id)

    async def handle(user_id: str) -> KnownName | None:
        return None if client is None else await _fetch_user_name(client, user_id)

    labels, found = await resolve_names(
        [row.platform_user_id for row in shown],
        live=live,
        stored=stored,
        handle=handle,
        timeout_s=timeout_s,
        log_event="billing.member_fetch_timed_out",
    )
    named = tuple(
        dataclasses.replace(row, display_name=labels.get(row.platform_user_id)) for row in shown
    )
    return named + rows[TOP_SPENDERS_SHOWN:], found


async def load_billing_snapshot(
    session: AsyncSession,
    *,
    guild: discord.Guild,
    guild_id: str,
    caller_user_id: str,
    is_admin: bool,
    since: datetime,
    now: datetime,
    channel_id: str | None = None,
    client: discord.Client | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> BillingPanelState:
    """Core's snapshot for this server, with the shown top spenders named from Discord.

    Naming is `resolve_shown_names`, with ``client`` for people who left. With a
    ``sessionmaker``, the names Discord gave are remembered in the background.
    """
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    state = await load_core_snapshot(
        session,
        tenant_id=tenant_id,
        platform_user_id=caller_user_id,
        is_admin=is_admin,
        since=since,
        now=now,
        platform="discord",
        channel_id=channel_id,
    )
    if not is_admin:
        return state
    rows, found = await resolve_shown_names(guild, state.member_rows, client=client)
    if sessionmaker is not None and found:
        remember_user_names(sessionmaker, tenant_id=tenant_id, platform="discord", names=found)
    return dataclasses.replace(state, member_rows=rows)
