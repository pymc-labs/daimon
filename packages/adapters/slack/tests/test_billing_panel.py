"""Tests for the Slack billing_panel views and command handler.

Covers:
- build_billing_container renders top-up static_select ONLY when is_admin
- empty-period clean render (zero usage produces a no-usage line, not an error)
- a failed snapshot load replaces the Loading… modal

The shared figures and the snapshot read are covered in core's test_billing_panel.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from daimon.core.billing_panel import BillingPanelState, MemberRow, load_billing_snapshot
from daimon.testing.factories import make_channel_budget, make_ledger_entry, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_SINCE = datetime(2025, 1, 1, tzinfo=UTC)
_NOW = datetime(2025, 1, 15, tzinfo=UTC)


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


async def test_both_views_show_the_invoking_channels_budget_only_when_it_has_one(
    db_session: AsyncSession,
) -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_BUDGET")
    await make_channel_budget(db_session, tenant=tenant, channel_id="C1", limit_usd=Decimal("5"))
    await make_ledger_entry(
        db_session, tenant=tenant, delta_usd=Decimal("-1.2"), channel_id="C1", occurred_at=_NOW
    )

    for is_admin in (False, True):
        texts: dict[str, str] = {}
        for channel_id in ("C1", "C2"):
            state = await load_billing_snapshot(
                db_session,
                tenant_id=tenant.id,
                platform_user_id="U_CALLER",
                is_admin=is_admin,
                since=_SINCE,
                now=_NOW,
                platform="slack",
                channel_id=channel_id,
            )
            blocks = build_billing_container(state, now=_NOW, since=_SINCE)
            texts[channel_id] = str(blocks)
        assert "this channel: $1.20 of $5.00 (monthly)" in texts["C1"]
        assert "this channel" not in texts["C2"]


def test_build_billing_container_empty_period_renders_cleanly() -> None:
    """Zero usage renders a clean 'no usage' line — no crash, no KeyError."""
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


async def test_handle_billing_command_replaces_loading_modal_with_error_on_failure(
    fake_slack_web_client: Any,
) -> None:
    """A failed snapshot load must not leave the Loading… modal spinning."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from daimon.adapters.slack.billing_panel.actions import handle_billing_command
    from sqlalchemy.exc import OperationalError
    from yarl import URL

    runtime = MagicMock()
    payload: dict[str, Any] = {
        "team_id": "T_BILLING_ERR",
        "user_id": "U_BILLING_ERR",
        "channel_id": "C_BILLING_ERR",
        "trigger_id": "TRIGGER_TEST",
    }

    with (
        patch(
            "daimon.adapters.slack.billing_panel.actions.resolve_web_client",
            new_callable=AsyncMock,
            return_value=fake_slack_web_client.client,
        ),
        patch(
            "daimon.adapters.slack.billing_panel.actions.load_billing_snapshot",
            new_callable=AsyncMock,
            side_effect=OperationalError("SELECT secret", {"p": "xoxb-leak"}, Exception("x")),
        ),
    ):
        await handle_billing_command(runtime, payload)

    update_key = ("POST", URL("https://slack.com/api/views.update"))
    update_calls = fake_slack_web_client.mock.requests.get(update_key)
    assert update_calls, "the Loading… modal must be replaced after the load fails"
    body: dict[str, Any] = update_calls[-1].kwargs["json"]
    assert body["view"]["title"]["text"] == "Billing"
    text = body["view"]["blocks"][0]["text"]["text"]
    assert "Database error" in text
    assert "xoxb-leak" not in text, "bound parameters must never reach the modal"
    assert "rid:" in text
