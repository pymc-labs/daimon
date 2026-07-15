"""Load + sort + cap the (user, tenant)-attributed billing snapshot for /billing.

Composition layer: reads from core stores, resolves Discord member display
names via the cache (no API calls), assembles a BillingPanelState.

`is_guild_admin` is Discord-native: manage_guild | administrator | owner_id.
Gating on a daimon-DB role would block legitimate guild admins, so we resolve
permissions from Discord at click time instead.
"""

from __future__ import annotations

from datetime import datetime

from daimon.adapters.discord.billing_panel.state import (
    BillingPanelState,
    MemberRow,
)
from daimon.adapters.discord.checks import is_member_guild_admin
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import tenant_user_caps
from daimon.core.stores.tenant_ledger import get_balance
from daimon.core.stores.usage_events import (
    cost_for_tenant_since,
    cost_for_user_in_tenant_since,
    costs_by_user_in_tenant_since,
    turn_count_for_tenant_since,
    turn_count_for_user_in_tenant_since,
    turns_by_user_in_tenant_since,
)
from sqlalchemy.ext.asyncio import AsyncSession

import discord
from discord import Interaction
from discord.ext import commands

BotInteraction = Interaction[commands.Bot]

_TOP_MEMBERS_CAP = 25  # D-SORT-02 — same number as Phase 27 _PICKER_CAP, different semantics


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


def _resolve_member_name(guild: discord.Guild | None, user_id: str) -> str:
    """Cache-only display-name lookup.

    Returns `member.display_name` on cache hit; `User XXXX` (last 4 chars of
    snowflake) on cache miss; `<unknown user>` if the id is too short to slice.
    """
    if guild is not None:
        try:
            member = guild.get_member(int(user_id))
        except ValueError:
            member = None
        if member is not None:
            return member.display_name
    if len(user_id) >= 4:
        return f"User {user_id[-4:]}"
    return "<unknown user>"


async def load_billing_snapshot(
    session: AsyncSession,
    *,
    guild: discord.Guild,
    guild_id: str,
    caller_user_id: str,
    is_admin: bool,
    since: datetime,
) -> BillingPanelState:
    """Read everything needed to render /billing for a single invocation.

    For regular (non-admin) viewers, only the caller-scoped reads happen.
    For admin viewers, additionally pulls tenant aggregates + per-member
    breakdown, applies sort+cap, and resolves display names from the guild
    member cache.
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
        )

    # Admin path
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
                display_name=_resolve_member_name(guild, user_id),
                cost_usd=costs_by_user.get(user_id, 0.0),
                turn_count=turns_by_user.get(user_id, 0),
                is_caller=(user_id == caller_user_id),
            )
        )

    # D-SORT-01: by cost_usd DESC, tie-break by platform_user_id ASC.
    rows.sort(key=lambda r: (-r.cost_usd, r.platform_user_id))
    over_cap_count = max(0, len(rows) - _TOP_MEMBERS_CAP)
    capped = tuple(rows[:_TOP_MEMBERS_CAP])

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
    )
