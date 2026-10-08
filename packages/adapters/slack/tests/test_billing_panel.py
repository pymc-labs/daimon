"""Tests for the Slack billing_panel views and command handler.

Covers:
- build_billing_container renders top-up static_select ONLY when is_admin
- empty-period clean render (zero usage produces a no-usage line, not an error)
- a failed snapshot load replaces the Loading… modal

The shared figures and the snapshot read are covered in core's test_billing_panel.py.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from daimon.core.billing_panel import BillingPanelState, MemberRow, load_billing_snapshot
from daimon.core.channel_budget import ChannelBudgetStatus
from daimon.core.promo_credit import ActiveTimedCredit
from daimon.core.stores.domain import ChannelBudgetRow
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
        assert "$1.20 of $5.00 spent this month" in texts["C1"]
        assert "*Channel budget*" not in texts["C2"], "no budget, no group"


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
    assert "nothing used this month" in all_text.lower(), (
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


def test_the_admin_view_lists_channel_budgets_and_the_member_view_does_not() -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    budgets: list[ChannelBudgetStatus] = []
    for index in range(7):
        budget = ChannelBudgetRow(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            platform="slack",
            channel_id=f"C{index}",
            limit_usd=Decimal("10"),
            window="monthly",
            starts_at=None,
            ends_at=None,
            set_by_account_id=None,
            created_at=_NOW,
            updated_at=_NOW,
        )
        budgets.append(
            ChannelBudgetStatus(budget=budget, spent_usd=Decimal(9 - index), is_active=True)
        )
    admin = str(
        build_billing_container(
            dataclasses.replace(_make_admin_state(), channel_budgets=tuple(budgets)),
            now=_NOW,
            since=_SINCE,
        )
    )
    assert "<#C0>: $9.00 of $10.00 (monthly) · 90% used" in admin
    assert "<#C4>" in admin and "<#C5>" not in admin, "only the five most used"
    assert "2 more channel budgets" in admin
    member = str(
        build_billing_container(
            dataclasses.replace(_make_member_state(), channel_budgets=tuple(budgets)),
            now=_NOW,
            since=_SINCE,
        )
    )
    assert "Channel budgets" not in member, "a member sees no other channel"


# ---------------------------------------------------------------------------
# Top spender names and the credit layout
# ---------------------------------------------------------------------------


def _spender_lines(state: BillingPanelState) -> list[str]:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    blocks = build_billing_container(state, now=_NOW, since=_SINCE)
    text = next(b["text"]["text"] for b in blocks if "Top spenders" in str(b))
    return text.splitlines()[1:]


def _row(user_id: str, *, is_caller: bool = False) -> MemberRow:
    label = f"User {user_id[-4:]}"
    return MemberRow(user_id, label, 2.0, 3, is_caller)


def test_top_spenders_are_user_mentions_which_a_modal_shows_as_names() -> None:
    state = dataclasses.replace(
        _make_admin_state(), member_rows=(_row("U0123ABCD"), _row("W0456EFGH", is_caller=True))
    )
    lines = _spender_lines(state)
    assert lines[0] == "1. <@U0123ABCD>  $2.00", lines
    assert lines[1] == "2. <@W0456EFGH> _(you)_  $2.00", "the caller is still marked"


def test_an_id_that_is_not_a_slack_user_id_keeps_the_escaped_label() -> None:
    odd = MemberRow("U1|<!channel>", "User <!channel>", 1.0, 1, False)
    state = dataclasses.replace(
        _make_admin_state(), member_rows=(odd, _row("u0123abcd"), _row("B0123ABCD"))
    )
    lines = _spender_lines(state)
    assert "<!channel>" not in "\n".join(lines) and "<@" not in "\n".join(lines), lines
    assert lines[1].startswith("2. User abcd"), "a lowercase id is not a user id"
    assert lines[2].startswith("3. User ABCD"), "a bot id is not a user id"


def test_the_billing_blocks_only_ever_go_into_a_modal() -> None:
    """A `<@U…>` mention notifies only in a message; the panel must stay a view."""
    from daimon.adapters.slack.billing_panel import views

    view = views.build_billing_view(_make_admin_state(), now=_NOW, since=_SINCE)
    assert view["type"] == "modal"


def _credit_blocks(state: BillingPanelState) -> list[dict[str, Any]]:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    blocks = build_billing_container(state, now=_NOW, since=_SINCE)
    start = next(i for i, b in enumerate(blocks) if "Server credit" in str(b))
    end = next(
        (i for i, b in enumerate(blocks) if i > start and b["type"] != "context"), len(blocks)
    )
    return blocks[start:end]


def _credit(remaining: str, day: int) -> ActiveTimedCredit:
    return ActiveTimedCredit(Decimal(remaining), datetime(2025, 1, day, 18, 0, tzinfo=UTC))


def test_server_credit_without_timed_credit_is_just_the_total() -> None:
    [section] = _credit_blocks(_make_admin_state())
    assert section["text"]["text"] == "🏦 *Server credit*\n*$50.00* total credit left"


def test_server_credit_details_are_a_context_block_under_the_total() -> None:
    end = int(datetime(2025, 1, 20, 18, 0, tzinfo=UTC).timestamp())
    state = dataclasses.replace(_make_admin_state(), timed_credit=(_credit("20", 20),))
    [section, context] = _credit_blocks(state)
    assert section["text"]["text"].endswith("*$50.00* total credit left")
    assert context["type"] == "context"
    assert context["elements"][0]["text"] == (
        f"Includes $20.00 that expires <!date^{end}^{{date_short_pretty}} {{time}}"
        "|2025-01-20 18:00 UTC>. It's used first."
    )


def test_several_timed_credits_are_one_line_with_the_soonest_end() -> None:
    first = int(datetime(2025, 1, 21, 18, 0, tzinfo=UTC).timestamp())
    credits = tuple(_credit(str(n), 20 + n) for n in range(1, 5))
    state = dataclasses.replace(_make_admin_state(), timed_credit=credits)
    [_, context] = _credit_blocks(state)
    detail = context["elements"][0]["text"]
    assert detail.startswith(f"Includes $10.00 that expires, first on <!date^{first}^"), detail
    assert "\n" not in detail, "one line, however many credits"


def test_a_negative_balance_still_shows_its_total_and_timed_credit() -> None:
    state = dataclasses.replace(
        _make_member_state(), guild_balance_usd=Decimal("-3"), timed_credit=(_credit("5", 20),)
    )
    [section, context] = _credit_blocks(state)
    assert "*No credit left* · $3.00 spent beyond it" in section["text"]["text"]
    detail = context["elements"][0]["text"]
    assert "Includes $5.00 that expires" in detail
    assert detail.endswith("\nAsk an admin to add credit.")


def test_the_header_and_own_use_are_short() -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    admin = build_billing_container(_make_admin_state(), now=_NOW, since=_SINCE)
    assert admin[0]["text"]["text"] == (
        "*💸 Billing · admin view*\nJanuary 2025 · $10.00 spent by 2 people"
    )
    member = build_billing_container(_make_member_state(), now=_NOW, since=_SINCE)
    assert member[0]["text"]["text"] == "*💸 Billing*\nJanuary 2025"
    assert member[2]["text"]["text"] == "*You*\n$0.75 of your $5.00 this month"


def test_channel_budget_is_its_own_group_before_top_spenders() -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    budget = ChannelBudgetRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="slack",
        channel_id="C1",
        limit_usd=Decimal("5"),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    status = ChannelBudgetStatus(budget=budget, spent_usd=Decimal("1.2"), is_active=True)
    state = dataclasses.replace(
        _make_admin_state(), channel_budget=status, channel_budgets=(status,)
    )
    blocks = build_billing_container(state, now=_NOW, since=_SINCE)
    texts = [str(b) for b in blocks]
    at = {
        name: next(i for i, t in enumerate(texts) if name in t)
        for name in ("Server credit", "*Channel budget*", "Top spenders", "Channel budgets")
    }
    assert list(at.values()) == sorted(at.values()), at
    assert blocks[at["*Channel budget*"] + 1] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "$1.20 of $5.00 spent this month"}],
    }
