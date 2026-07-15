"""Load + sort + cap the (user, workspace)-attributed billing snapshot for Slack /billing.

Ported from daimon.adapters.discord.billing_panel.read — replaces the guild-cache
member-name resolution with a simple "User XXXX" fallback since Slack has no
equivalent member cache (display-name resolution via users.info would require
an extra API call per member and is OUT OF SCOPE per the plan).

`is_admin` is resolved upstream (resolve_is_admin in interactions/actions) and
passed as a parameter so this function stays a pure DB read.
"""

from __future__ import annotations

from datetime import datetime

from daimon.adapters.slack.billing_panel.state import BillingPanelState, MemberRow
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

_TOP_MEMBERS_CAP = 25  # same cap as Discord D-SORT-02; Slack static_select ≤ 100 but 25 is UX


def _resolve_member_name(user_id: str) -> str:
    """Display-name fallback for Slack (no member cache).

    Returns "User XXXX" (last 4 chars of the Slack user ID) or
    "<unknown user>" if the ID is too short to slice.
    """
    if len(user_id) >= 4:
        return f"User {user_id[-4:]}"
    return "<unknown user>"


async def load_billing_snapshot(
    session: AsyncSession,
    *,
    team_id: str,
    platform_user_id: str,
    is_admin: bool,
    since: datetime,
) -> BillingPanelState:
    """Read everything needed to render /billing for a single Slack invocation.

    For regular (non-admin) viewers, only the caller-scoped reads happen.
    For admin viewers, additionally pulls workspace aggregates + per-member
    breakdown, applies sort + cap, and fills display_name with the "User XXXX"
    fallback (Slack has no member-name cache; batch users.info is out of scope).

    Args:
        session:            Async DB session (injected).
        team_id:            Slack workspace ID (used to derive tenant_id).
        platform_user_id:   Caller's Slack user ID.
        is_admin:           Whether the caller is a workspace admin (resolved
                            upstream via resolve_is_admin).
        since:              Period start (typically first day of current month).

    Returns:
        A frozen BillingPanelState ready for views.build_billing_container.
    """
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    caller_spend = await cost_for_user_in_tenant_since(
        session,
        platform_user_id=platform_user_id,
        tenant_id=tenant_id,
        since=since,
    )
    caller_turns = await turn_count_for_user_in_tenant_since(
        session,
        platform_user_id=platform_user_id,
        tenant_id=tenant_id,
        since=since,
    )
    caller_cap = await tenant_user_caps.get_effective_cap(
        session,
        tenant_id=tenant_id,
        user_id=platform_user_id,
    )
    guild_balance = await get_balance(session, tenant_id=tenant_id)

    if not is_admin:
        return BillingPanelState(
            is_admin=False,
            caller_user_id=platform_user_id,
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

    # Admin path — workspace aggregates + per-member breakdown
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

    # Merge cost and turn dicts — keys may differ in edge cases; union defensively
    all_user_ids = set(costs_by_user) | set(turns_by_user)
    rows: list[MemberRow] = []
    for user_id in all_user_ids:
        rows.append(
            MemberRow(
                platform_user_id=user_id,
                display_name=_resolve_member_name(user_id),
                cost_usd=costs_by_user.get(user_id, 0.0),
                turn_count=turns_by_user.get(user_id, 0),
                is_caller=(user_id == platform_user_id),
            )
        )

    # D-SORT-01: cost_usd DESC, tie-break platform_user_id ASC
    rows.sort(key=lambda r: (-r.cost_usd, r.platform_user_id))
    over_cap_count = max(0, len(rows) - _TOP_MEMBERS_CAP)
    capped = tuple(rows[:_TOP_MEMBERS_CAP])

    return BillingPanelState(
        is_admin=True,
        caller_user_id=platform_user_id,
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
