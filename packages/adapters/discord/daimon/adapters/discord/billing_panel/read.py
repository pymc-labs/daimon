"""Load + sort + cap the (user, tenant)-attributed billing snapshot for /billing.

Composition layer: reads from core stores, resolves the display names of the
top spenders the panel shows (the member cache, else one member fetch each
under a short shared timeout), assembles a BillingPanelState.

`is_guild_admin` is Discord-native: manage_guild | administrator | owner_id.
Gating on a daimon-DB role would block legitimate guild admins, so we resolve
permissions from Discord at click time instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime

import structlog
from daimon.adapters.discord.billing_panel.state import (
    BillingPanelState,
    MemberRow,
)
from daimon.adapters.discord.checks import is_member_guild_admin
from daimon.core.billing_panel import member_label
from daimon.core.channel_budget import get_channel_budget_status, list_channel_budget_statuses
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.promo_credit import get_active_timed_credit
from daimon.core.stores import tenant_user_caps
from daimon.core.stores.promo_codes import has_redeemable_promo_code
from daimon.core.stores.tenant_ledger import get_balance
from daimon.core.stores.usage_events import (
    cost_for_tenant_since,
    cost_for_user_in_tenant_since,
    costs_by_user_in_tenant_since,
    turn_count_for_tenant_since,
    turn_count_for_user_in_tenant_since,
    turns_by_user_in_tenant_since,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

import discord
from discord import Interaction
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

_log = structlog.get_logger()

_TOP_MEMBERS_CAP = 25  # same number as _PICKER_CAP, different semantics
TOP_SPENDERS_SHOWN = 5  # rows the panel lists by name
# The panel waits at most this long for the member fetches, all together.
NAME_FETCH_TIMEOUT_S = 1.5


def is_guild_admin(interaction: BotInteraction) -> bool:
    """Discord-native admin check: owner OR manage_guild OR administrator.

    Resolves at every render/click (not cached) so role flips take effect
    immediately and we never rely on a daimon-DB role gate.
    """
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    guild = interaction.guild
    owner_id = guild.owner_id if guild is not None else None
    return is_member_guild_admin(member, guild_owner_id=owner_id)


def invoking_channel_id(interaction: BotInteraction) -> str | None:
    """The channel a budget applies to: the invoking channel, or a thread's parent."""
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return str(channel.parent_id)
    return str(interaction.channel_id) if interaction.channel_id is not None else None


async def _fetch_member_name(guild: discord.Guild, user_id: str) -> str | None:
    """The member's display name from one REST fetch, or None when Discord can't say.

    A single-member fetch needs no privileged members intent. NotFound (they
    left), Forbidden and any other HTTP error all read as unknown.
    """
    try:
        member = await guild.fetch_member(int(user_id))
    except ValueError:
        return None
    except discord.HTTPException as exc:
        _log.info("billing.member_fetch_failed", status=exc.status)
        return None
    return member.display_name


async def resolve_shown_names(
    guild: discord.Guild,
    rows: tuple[MemberRow, ...],
    *,
    timeout_s: float = NAME_FETCH_TIMEOUT_S,
) -> tuple[MemberRow, ...]:
    """Name the rows the panel shows; the rest, and anyone unresolved, keep `User XXXX`.

    The member cache answers first. Misses are fetched concurrently, and
    whatever has not answered within ``timeout_s`` is cancelled.
    """
    shown = rows[:TOP_SPENDERS_SHOWN]
    names: dict[str, str] = {}
    misses: list[str] = []
    for row in shown:
        try:
            cached = guild.get_member(int(row.platform_user_id))
        except ValueError:
            continue
        if cached is not None:
            names[row.platform_user_id] = cached.display_name
        else:
            misses.append(row.platform_user_id)
    if misses:
        tasks = {asyncio.create_task(_fetch_member_name(guild, uid)): uid for uid in misses}
        done, pending = await asyncio.wait(tasks, timeout=timeout_s)
        for task in pending:
            task.cancel()
        if pending:
            _log.info("billing.member_fetch_timed_out", unresolved=len(pending))
        for task in done:
            if not task.cancelled() and task.exception() is None and (name := task.result()):
                names[tasks[task]] = name
    named = tuple(
        dataclasses.replace(row, display_name=names[row.platform_user_id])
        if row.platform_user_id in names
        else row
        for row in shown
    )
    return named + rows[TOP_SPENDERS_SHOWN:]


async def _has_redeemable_promo_code_best_effort(session: AsyncSession, *, now: datetime) -> bool:
    """Whether a promo code is redeemable now; False when the lookup fails.

    Runs in a savepoint so a failed lookup leaves the session usable.
    """
    try:
        async with session.begin_nested():
            return await has_redeemable_promo_code(session, now=now)
    except SQLAlchemyError as exc:
        _log.warning("promo_code_lookup_failed", error=str(exc))
        return False


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
) -> BillingPanelState:
    """Read everything needed to render /billing for a single invocation.

    For regular (non-admin) viewers, only the caller-scoped reads happen.
    For admin viewers, additionally pulls tenant aggregates + per-member
    breakdown, applies sort+cap, and resolves the shown rows' display names
    (`resolve_shown_names`).
    """
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

    caller_spend = await cost_for_user_in_tenant_since(
        session,
        platform_user_id=caller_user_id,
        tenant_id=tenant_id,
        since=since,
    )
    caller_turns = await turn_count_for_user_in_tenant_since(
        session,
        platform_user_id=caller_user_id,
        tenant_id=tenant_id,
        since=since,
    )
    caller_cap = await tenant_user_caps.get_effective_cap(
        session,
        tenant_id=tenant_id,
        user_id=caller_user_id,
    )
    guild_balance = await get_balance(session, tenant_id=tenant_id)
    timed_credit = tuple(await get_active_timed_credit(session, tenant_id=tenant_id, now=now))
    channel_budget = (
        None
        if channel_id is None
        else await get_channel_budget_status(
            session,
            tenant_id=tenant_id,
            platform="discord",
            channel_id=channel_id,
            now=now,
        )
    )

    if not is_admin:
        return BillingPanelState(
            is_admin=False,
            caller_user_id=caller_user_id,
            caller_spend=caller_spend,
            caller_turns=caller_turns,
            caller_cap=caller_cap,
            guild_balance_usd=guild_balance,
            guild_spend=0.0,
            guild_turns=0,
            guild_distinct_members=0,
            member_rows=(),
            over_cap_count=0,
            timed_credit=timed_credit,
            channel_budget=channel_budget,
        )

    # Admin path
    has_redeemable = await _has_redeemable_promo_code_best_effort(session, now=now)
    guild_spend = await cost_for_tenant_since(
        session,
        tenant_id=tenant_id,
        since=since,
    )
    guild_turns = await turn_count_for_tenant_since(
        session,
        tenant_id=tenant_id,
        since=since,
    )
    costs_by_user = await costs_by_user_in_tenant_since(
        session,
        tenant_id=tenant_id,
        since=since,
    )
    turns_by_user = await turns_by_user_in_tenant_since(
        session,
        tenant_id=tenant_id,
        since=since,
    )

    # Merge cost dict and turn dict by platform_user_id — keys may differ in
    # edge cases; union both key sets defensively.
    all_user_ids = set(costs_by_user) | set(turns_by_user)
    rows: list[MemberRow] = []
    for user_id in all_user_ids:
        rows.append(
            MemberRow(
                platform_user_id=user_id,
                display_name=member_label(user_id),
                cost_usd=costs_by_user.get(user_id, 0.0),
                turn_count=turns_by_user.get(user_id, 0),
                is_caller=(user_id == caller_user_id),
            )
        )

    # D-SORT-01: by cost_usd DESC, tie-break by platform_user_id ASC.
    rows.sort(key=lambda r: (-r.cost_usd, r.platform_user_id))
    over_cap_count = max(0, len(rows) - _TOP_MEMBERS_CAP)
    capped = await resolve_shown_names(guild, tuple(rows[:_TOP_MEMBERS_CAP]))

    return BillingPanelState(
        is_admin=True,
        caller_user_id=caller_user_id,
        caller_spend=caller_spend,
        caller_turns=caller_turns,
        caller_cap=caller_cap,
        guild_balance_usd=guild_balance,
        guild_spend=guild_spend,
        guild_turns=guild_turns,
        guild_distinct_members=len(all_user_ids),
        member_rows=capped,
        over_cap_count=over_cap_count,
        timed_credit=timed_credit,
        channel_budget=channel_budget,
        has_redeemable_promo_code=has_redeemable,
        channel_budgets=tuple(
            await list_channel_budget_statuses(
                session, tenant_id=tenant_id, platform="discord", now=now
            )
        ),
    )
