"""Shape tests for BillingPanelState + MemberRow (frozen dataclasses)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

# Plan 28-02 ships the billing_panel module in a parallel worktree. Skip cleanly
# when it isn't available locally; tests execute for real on main after merge.
pytest.importorskip("daimon.adapters.discord.billing_panel.state")

from daimon.adapters.discord.billing_panel.state import COLOR_OVER_CAP  # noqa: E402

from .conftest import _make_member_row, _make_state  # noqa: E402


def test_billing_panel_state_is_frozen() -> None:
    state = _make_state()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.caller_spend = 99.99  # type: ignore[misc]


def test_member_row_is_frozen() -> None:
    row = _make_member_row()
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.cost_usd = 99.99  # type: ignore[misc]


def test_billing_panel_state_default_member_rows_is_empty_tuple() -> None:
    state = _make_state(is_admin=False)
    assert state.member_rows == (), "regular view should ship with empty member_rows"


def test_billing_panel_state_caller_cap_accepts_none() -> None:
    state = _make_state(caller_cap=None)
    assert state.caller_cap is None, "absent cap should round-trip as None"


def test_billing_panel_state_caller_cap_accepts_decimal() -> None:
    state = _make_state(caller_cap=Decimal("100.00"))
    assert state.caller_cap == Decimal("100.00"), "cap should round-trip as Decimal"


def test_color_constant_over_cap_is_discord_red() -> None:
    assert COLOR_OVER_CAP == 0xED4245, "over-cap color should be Discord red"


def test_billing_panel_state_guild_balance_usd_field_present() -> None:
    state = _make_state(guild_balance_usd=Decimal("42.50"))
    assert state.guild_balance_usd == Decimal("42.50"), (
        "guild_balance_usd must round-trip as Decimal"
    )


def test_billing_panel_state_guild_balance_usd_accepts_zero() -> None:
    state = _make_state(guild_balance_usd=Decimal("0"))
    assert state.guild_balance_usd == Decimal("0"), (
        "zero balance (empty ledger) should round-trip as Decimal('0')"
    )


def test_billing_panel_state_guild_balance_usd_accepts_negative() -> None:
    state = _make_state(guild_balance_usd=Decimal("-5.00"))
    assert state.guild_balance_usd == Decimal("-5.00"), (
        "negative balance (depleted) must be representable (D-14)"
    )
