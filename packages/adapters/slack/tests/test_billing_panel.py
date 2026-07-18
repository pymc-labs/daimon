"""Tests for billing_panel: state, read, views.

Covers:
- _fmt_usd pure formatter
- estimate_turns pure formula
- load_billing_snapshot member path (is_admin=False) — real DB, no member rows
- load_billing_snapshot admin path (is_admin=True) — real DB, rows sorted by
  (-cost, platform_user_id) capped at 25
- build_billing_container renders top-up static_select ONLY when is_admin
- empty-period clean render (zero usage produces a no-usage line, not an error)

Real Postgres via the db_session / db_session_factory fixtures. No method-level
AsyncMock, no module-level singletons.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.stores import usage_events
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEAM_ID = "T_BILLING_TEST"
_CALLER_ID = "U_CALLER"
_OTHER_ID = "U_OTHER"
_SINCE = datetime(2025, 1, 1, tzinfo=UTC)
_NOW = datetime(2025, 1, 15, tzinfo=UTC)


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    """Slack tenant with two usage-event seeded users."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM_ID)

    # Seed usage for caller (1000 input + 1000 output tokens on claude-opus-4-7)
    usage_caller = BetaManagedAgentsSpanModelUsage(
        input_tokens=1000,
        output_tokens=1000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant.id,
        platform_user_id=_CALLER_ID,
        managed_session_id="sess-caller-1",
        model="claude-opus-4-7",
        model_usage=usage_caller,
        event_id="evt-billing-caller-1",
    )

    # Seed usage for the other user (larger spend so they appear above caller)
    usage_other = BetaManagedAgentsSpanModelUsage(
        input_tokens=10_000,
        output_tokens=5_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    await usage_events.record(
        db_session,
        tenant_id=tenant.id,
        platform_user_id=_OTHER_ID,
        managed_session_id="sess-other-1",
        model="claude-opus-4-7",
        model_usage=usage_other,
        event_id="evt-billing-other-1",
    )
    await db_session.commit()
    return tenant.id


@pytest_asyncio.fixture
async def empty_tenant_id(db_session: AsyncSession) -> uuid.UUID:
    """Slack tenant with no usage events (empty period)."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_BILLING_EMPTY")
    await db_session.commit()
    return tenant.id


# ---------------------------------------------------------------------------
# Pure formatter: _fmt_usd
# ---------------------------------------------------------------------------


def test_fmt_usd_formats_small_amount() -> None:
    from daimon.adapters.slack.billing_panel.views import _fmt_usd

    assert _fmt_usd(12.5) == "$12.50", "_fmt_usd should format 12.5 as $12.50"


def test_fmt_usd_formats_zero() -> None:
    from daimon.adapters.slack.billing_panel.views import _fmt_usd

    assert _fmt_usd(0.0) == "$0.00", "_fmt_usd should format 0.0 as $0.00"


def test_fmt_usd_formats_large_decimal_with_comma() -> None:
    from daimon.adapters.slack.billing_panel.views import _fmt_usd

    assert _fmt_usd(Decimal("1000")) == "$1,000.00", (
        "_fmt_usd should use comma-separated thousands for Decimal('1000')"
    )


def test_fmt_usd_formats_float_large_with_comma() -> None:
    from daimon.adapters.slack.billing_panel.views import _fmt_usd

    assert _fmt_usd(2500.75) == "$2,500.75", (
        "_fmt_usd should format large floats with comma separator"
    )


# ---------------------------------------------------------------------------
# Pure formula: estimate_turns
# ---------------------------------------------------------------------------


def test_estimate_turns_uses_fallback_when_no_history() -> None:
    from daimon.adapters.slack.billing_panel.views import estimate_turns

    turns = estimate_turns(10.0, guild_spend=0.0, guild_turns=0)
    assert turns == 100, (
        "estimate_turns with no history should use $0.10/turn fallback → $10 = 100 turns"
    )


def test_estimate_turns_uses_guild_average_when_history_exists() -> None:
    from daimon.adapters.slack.billing_panel.views import estimate_turns

    # $20 total spend, 4 turns → $5/turn. $10 / $5 = 2 turns.
    turns = estimate_turns(10.0, guild_spend=20.0, guild_turns=4)
    assert turns == 2, "estimate_turns should use guild average cost per turn when history exists"


def test_estimate_turns_falls_back_when_spend_is_zero_but_turns_nonzero() -> None:
    """Edge case: guild_spend=0 but guild_turns>0 → use fallback."""
    from daimon.adapters.slack.billing_panel.views import estimate_turns

    turns = estimate_turns(10.0, guild_spend=0.0, guild_turns=5)
    assert turns == 100, "estimate_turns should fall back to $0.10/turn when guild_spend == 0"


# ---------------------------------------------------------------------------
# DB: load_billing_snapshot — member path (is_admin=False)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_billing_snapshot_member_returns_only_caller_data(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """Member path: returns caller spend/turns/cap, no member rows."""
    from daimon.adapters.slack.billing_panel.read import load_billing_snapshot

    state = await load_billing_snapshot(
        db_session,
        team_id=_TEAM_ID,
        platform_user_id=_CALLER_ID,
        is_admin=False,
        since=_SINCE,
    )

    assert state.is_admin is False, "is_admin must be False for member path"
    assert state.caller_user_id == _CALLER_ID, "caller_user_id must match the caller"
    assert state.caller_spend > 0.0, "caller_spend must be positive (seeded usage event)"
    assert state.caller_turns == 1, "caller_turns must be 1 (one seeded session)"
    assert len(state.member_rows) == 0, "member path must return no member rows (self-only)"
    assert state.guild_spend == 0.0, "member path must return zero guild_spend"
    assert state.guild_turns == 0, "member path must return zero guild_turns"
    assert state.guild_distinct_members == 0, "member path must return zero guild_distinct_members"


# ---------------------------------------------------------------------------
# DB: load_billing_snapshot — admin path (is_admin=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_billing_snapshot_admin_returns_sorted_member_rows(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """Admin path: returns per-member rows sorted by (-cost, platform_user_id)."""
    from daimon.adapters.slack.billing_panel.read import load_billing_snapshot

    state = await load_billing_snapshot(
        db_session,
        team_id=_TEAM_ID,
        platform_user_id=_CALLER_ID,
        is_admin=True,
        since=_SINCE,
    )

    assert state.is_admin is True, "is_admin must be True for admin path"
    assert len(state.member_rows) == 2, "admin path must return 2 member rows"

    # Other user has more spend → must appear first (D-SORT-01)
    first = state.member_rows[0]
    second = state.member_rows[1]
    assert first.platform_user_id == _OTHER_ID, (
        "member rows must be sorted by cost_usd DESC; other user has more spend"
    )
    assert first.cost_usd >= second.cost_usd, "member rows must be sorted descending by cost_usd"


@pytest.mark.asyncio
async def test_load_billing_snapshot_admin_caps_at_25_members(
    db_session: AsyncSession,
) -> None:
    """Admin path caps member rows at 25 even when more users exist."""
    workspace_id = f"T_BILLING_MANY_{uuid.uuid4().hex[:8]}"
    tenant = await make_tenant(db_session, platform="slack", workspace_id=workspace_id)

    for i in range(30):
        await usage_events.record(
            db_session,
            tenant_id=tenant.id,
            platform_user_id=f"U_MANY_{i:03d}",
            managed_session_id=f"sess-many-{i}",
            model="claude-opus-4-7",
            model_usage=BetaManagedAgentsSpanModelUsage(
                input_tokens=100 * (i + 1),
                output_tokens=50 * (i + 1),
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            event_id=f"evt-many-{i}",
        )
    await db_session.commit()

    from daimon.adapters.slack.billing_panel.read import load_billing_snapshot

    state = await load_billing_snapshot(
        db_session,
        team_id=workspace_id,
        platform_user_id="U_MANY_000",
        is_admin=True,
        since=_SINCE,
    )

    assert len(state.member_rows) == 25, "admin path must cap member rows at 25"
    assert state.over_cap_count == 5, "over_cap_count must reflect rows beyond the 25 cap"


# ---------------------------------------------------------------------------
# DB: load_billing_snapshot — empty period
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_billing_snapshot_member_empty_period(
    db_session: AsyncSession,
    empty_tenant_id: uuid.UUID,
) -> None:
    """Empty period: caller spend and turns are both 0; state is well-formed."""
    from daimon.adapters.slack.billing_panel.read import load_billing_snapshot

    state = await load_billing_snapshot(
        db_session,
        team_id="T_BILLING_EMPTY",
        platform_user_id="U_EMPTY",
        is_admin=False,
        since=_SINCE,
    )

    assert state.caller_spend == 0.0, "empty period should have 0 caller_spend"
    assert state.caller_turns == 0, "empty period should have 0 caller_turns"
    assert state.member_rows == (), "empty period should have no member rows"


# ---------------------------------------------------------------------------
# Views: build_billing_container — top-up select admin gate
# ---------------------------------------------------------------------------


def _find_topup_select(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Walk blocks and look for a static_select with action_id 'billing_topup'."""
    for block in blocks:
        elements = block.get("elements", [])
        for element in elements:
            if (
                element.get("type") == "static_select"
                and element.get("action_id") == "billing_topup"
            ):
                return element
    return None


def _make_admin_state() -> Any:
    from daimon.adapters.slack.billing_panel.state import BillingPanelState, MemberRow

    return BillingPanelState(
        is_admin=True,
        caller_user_id="U_ADMIN",
        caller_spend=1.5,
        caller_turns=3,
        caller_cap=None,
        guild_balance_usd=Decimal("50.00"),
        guild_spend=10.0,
        guild_turns=20,
        guild_distinct_members=2,
        member_rows=(
            MemberRow(
                platform_user_id="U_TOP",
                display_name="User U_TOP",
                cost_usd=8.0,
                turn_count=15,
                is_caller=False,
            ),
            MemberRow(
                platform_user_id="U_ADMIN",
                display_name="User DMIN",
                cost_usd=1.5,
                turn_count=3,
                is_caller=True,
            ),
        ),
        over_cap_count=0,
    )


def _make_member_state() -> Any:
    from daimon.adapters.slack.billing_panel.state import BillingPanelState

    return BillingPanelState(
        is_admin=False,
        caller_user_id="U_MEMBER",
        caller_spend=0.75,
        caller_turns=2,
        caller_cap=Decimal("5.00"),
        guild_balance_usd=Decimal("30.00"),
        guild_spend=0.0,
        guild_turns=0,
        guild_distinct_members=0,
        member_rows=(),
        over_cap_count=0,
    )


def test_build_billing_container_renders_topup_select_for_admin() -> None:
    """Admin view must include a static_select with action_id 'billing_topup'."""
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    state = _make_admin_state()
    blocks = build_billing_container(state, now=_NOW, since=_SINCE)

    topup = _find_topup_select(blocks)
    assert topup is not None, (
        "build_billing_container must render static_select with billing_topup for admin"
    )
    assert topup["action_id"] == "billing_topup", (
        "top-up select must have action_id 'billing_topup'"
    )
    # Must have exactly 4 preset options: $10, $25, $50, $100
    assert len(topup["options"]) == 4, "top-up select must have 4 preset amount options"
    values = [opt["value"] for opt in topup["options"]]
    assert values == ["10", "25", "50", "100"], "top-up options must be preset amounts 10/25/50/100"


def test_build_billing_container_omits_topup_select_for_member() -> None:
    """Member view must NOT include a top-up select."""
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    state = _make_member_state()
    blocks = build_billing_container(state, now=_NOW, since=_SINCE)

    topup = _find_topup_select(blocks)
    assert topup is None, "build_billing_container must NOT render top-up select for a non-admin"


def test_build_billing_container_empty_period_renders_cleanly() -> None:
    """Zero usage renders a clean 'no usage' line — no crash, no KeyError."""
    from daimon.adapters.slack.billing_panel.state import BillingPanelState
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    state = BillingPanelState(
        is_admin=False,
        caller_user_id="U_EMPTY",
        caller_spend=0.0,
        caller_turns=0,
        caller_cap=None,
        guild_balance_usd=Decimal("0"),
        guild_spend=0.0,
        guild_turns=0,
        guild_distinct_members=0,
        member_rows=(),
        over_cap_count=0,
    )
    blocks = build_billing_container(state, now=_NOW, since=_SINCE)

    # Must produce at least one block without raising
    assert len(blocks) > 0, "empty-period render must produce blocks without crashing"
    # Check that some block mentions 'no usage' to assert clean empty render
    all_text = " ".join(
        str(b.get("text", {}).get("text", "") or b.get("elements", [])) for b in blocks
    )
    assert "no usage" in all_text.lower(), (
        "empty-period render must include a 'no usage' line rather than implying an error"
    )


def test_build_billing_container_admin_empty_period_renders_cleanly() -> None:
    """Admin view with zero member rows renders cleanly."""
    from daimon.adapters.slack.billing_panel.state import BillingPanelState
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    state = BillingPanelState(
        is_admin=True,
        caller_user_id="U_ADMIN",
        caller_spend=0.0,
        caller_turns=0,
        caller_cap=None,
        guild_balance_usd=Decimal("0"),
        guild_spend=0.0,
        guild_turns=0,
        guild_distinct_members=0,
        member_rows=(),
        over_cap_count=0,
    )
    blocks = build_billing_container(state, now=_NOW, since=_SINCE)
    assert len(blocks) > 0, "empty-period admin render must produce blocks without crashing"
