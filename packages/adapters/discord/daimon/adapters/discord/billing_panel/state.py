"""Frozen state snapshot for the /billing visibility panel.

Pure logic only. No I/O, no clock, no DB. The read layer (`read.py`) loads
this from stores + Discord cache; the panel layer (`panel.py`) renders it.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from daimon.adapters.discord import theme

COLOR_OVER_CAP = theme.COLOR_RED  # caller is over their effective cap


@dataclasses.dataclass(frozen=True)
class MemberRow:
    platform_user_id: str
    display_name: str
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

    # Guild-scoped balance (both views)
    guild_balance_usd: Decimal  # SUM(tenant_ledger.delta_usd); negative = depleted

    # Guild-scoped activity (admin view only; zeros/empty for regular view)
    guild_spend: float
    guild_turns: int
    guild_distinct_members: int  # "K members" — distinct spending users only
    member_rows: tuple[MemberRow, ...]  # already sorted + top-25-capped
    over_cap_count: int  # number of additional spending members beyond top 25
