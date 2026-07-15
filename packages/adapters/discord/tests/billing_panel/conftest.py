"""Fixtures and factories local to billing_panel tests.

Plan 28-02 (which provides the billing_panel module) runs in a parallel
worktree. To keep this conftest importable when the production module is
absent (so other test files in the package still collect), factories import
the production module *inside* the function body. Test modules guard at the
top with `pytest.importorskip("daimon.adapters.discord.billing_panel.*")`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import discord

if TYPE_CHECKING:  # pragma: no cover - import only used for type hints
    from daimon.adapters.discord.billing_panel.state import (
        BillingPanelState,
        MemberRow,
    )
    from daimon.adapters.discord.runtime import DiscordRuntime


def _make_member_row(**overrides: Any) -> MemberRow:
    from daimon.adapters.discord.billing_panel.state import MemberRow

    base: dict[str, Any] = {
        "platform_user_id": "100000000000000001",
        "display_name": "alice",
        "cost_usd": 1.23,
        "turn_count": 4,
        "is_caller": False,
    }
    base.update(overrides)
    return MemberRow(**base)


def _make_state(
    *,
    is_admin: bool = False,
    caller_user_id: str = "100000000000000001",
    caller_spend: float = 0.0,
    caller_turns: int = 0,
    caller_cap: Decimal | None = None,
    guild_balance_usd: Decimal = Decimal("0"),
    guild_spend: float = 0.0,
    guild_turns: int = 0,
    guild_distinct_members: int = 0,
    member_rows: tuple[MemberRow, ...] = (),
    over_cap_count: int = 0,
) -> BillingPanelState:
    from daimon.adapters.discord.billing_panel.state import BillingPanelState

    return BillingPanelState(
        is_admin=is_admin,
        caller_user_id=caller_user_id,
        caller_spend=caller_spend,
        caller_turns=caller_turns,
        caller_cap=caller_cap,
        guild_balance_usd=guild_balance_usd,
        guild_spend=guild_spend,
        guild_turns=guild_turns,
        guild_distinct_members=guild_distinct_members,
        member_rows=member_rows,
        over_cap_count=over_cap_count,
    )


def _make_runtime() -> DiscordRuntime:
    from daimon.adapters.discord.runtime import DiscordRuntime

    return MagicMock(spec=DiscordRuntime)


def _make_guild(
    *,
    owner_id: int = 999,
    members: dict[int, str] | None = None,
) -> discord.Guild:
    """MagicMock Guild whose `get_member(id)` returns a Member-with-display_name
    for ids in `members`, else None.

    members maps snowflake (int) -> display_name (str).
    """
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = owner_id

    def _get_member(snowflake: int) -> Any:
        if members is not None and snowflake in members:
            member = MagicMock(spec=discord.Member)
            member.display_name = members[snowflake]
            return member
        return None

    guild.get_member.side_effect = _get_member
    return guild
