"""Frozen state snapshot for the Slack /billing visibility panel.

Pure logic only. No I/O, no clock, no DB. The read layer (read.py) loads
this from stores; the views layer (views.py) renders it.

Ported from daimon.adapters.discord.billing_panel.state — drops
theme/COLOR_OVER_CAP (D-03: no color signaling in Block Kit). Decimal is
kept for caller_cap and guild_balance_usd to preserve precision.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal


@dataclasses.dataclass(frozen=True)
class MemberRow:
    platform_user_id: str
    display_name: str  # "User XXXX" fallback (no guild cache in Slack)
    cost_usd: float
    turn_count: int
    is_caller: bool


@dataclasses.dataclass(frozen=True)
class BillingPanelState:
    # Caller-scoped (always populated; even regular view uses these)
    is_admin: bool
    caller_user_id: str
    caller_spend: float
    caller_turns: int
    caller_cap: Decimal | None  # None when no effective cap configured

    # Workspace-scoped balance (both views)
    guild_balance_usd: Decimal  # SUM(tenant_ledger.delta_usd); negative = depleted

    # Workspace-scoped activity (admin view only; zeros/empty for regular view)
    guild_spend: float
    guild_turns: int
    guild_distinct_members: int  # distinct spending users count
    member_rows: tuple[MemberRow, ...]  # sorted + top-25-capped
    over_cap_count: int  # additional spending members beyond top 25
