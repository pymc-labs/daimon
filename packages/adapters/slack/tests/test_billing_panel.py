"""Tests for the Slack billing_panel views and command handler.

Covers:
- build_billing_container renders top-up static_select ONLY when is_admin
- empty-period clean render (zero usage produces a no-usage line, not an error)
- a failed snapshot load replaces the Loading… modal

The shared figures and the snapshot read are covered in core's test_billing_panel.py.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.core.billing_panel import BillingPanelState, MemberRow, load_billing_snapshot
from daimon.core.channel_budget import ChannelBudgetStatus
from daimon.core.promo_credit import ActiveTimedCredit
from daimon.core.stores.domain import ChannelBudgetRow
from daimon.testing.factories import make_channel_budget, make_ledger_entry, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        assert "*This channel*\\n$1.20 of $5.00 used this month" in texts["C1"]
        assert "*This channel*" not in texts["C2"], "no budget, no section"


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


def _budget(channel_id: str, spent: str, limit: str = "10") -> ChannelBudgetStatus:
    budget = ChannelBudgetRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        platform="slack",
        channel_id=channel_id,
        limit_usd=Decimal(limit),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    return ChannelBudgetStatus(budget=budget, spent_usd=Decimal(spent), is_active=True)


def _credit(remaining: str, day: int) -> ActiveTimedCredit:
    return ActiveTimedCredit(Decimal(remaining), datetime(2025, 1, day, 18, 0, tzinfo=UTC))


def _shape(blocks: list[dict[str, Any]]) -> list[str]:
    """Each block as `type: text`, or the action ids of an actions block."""
    out: list[str] = []
    for block in blocks:
        kind = block["type"]
        if kind == "section":
            out.append(f"section: {block['text']['text']}")
        elif kind == "context":
            out.append(f"context: {block['elements'][0]['text']}")
        elif kind == "actions":
            out.append("actions: " + " ".join(e["action_id"] for e in block["elements"]))
        else:
            out.append(kind)
    return out


def test_the_admin_panel_is_month_credit_channel_and_actions_apart() -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_view

    state = dataclasses.replace(
        _make_admin_state(),
        member_rows=(_row("U0123ABCD"),),
        timed_credit=(_credit("20", 20), _credit("5", 31)),
        channel_budget=_budget("C1", "1.2", "5"),
        channel_budgets=(_budget("C1", "1.2", "5"),),
        has_redeemable_promo_code=True,
    )
    view = build_billing_view(state, now=_NOW, since=_SINCE)
    assert view["type"] == "modal" and view["title"]["text"] == "Billing"
    assert _shape(view["blocks"]) == [
        "context: January 2025 · $10.00 spent by 2 people",
        "divider",
        "section: *$50.00* total credit left",
        "context: Includes $25.00 that expires. It's used first.",
        "divider",
        "section: *This channel*\n$1.20 of $5.00 used this month",
        "divider",
        "section: *Top spenders*\n1. <@U0123ABCD>  $2.00",
        "divider",
        "section: *Channel budgets*\n<#C1>  $1.20 of $5.00 used this month",
        "divider",
        "actions: billing_topup billing_redeem_open billing_expiry_open",
        "actions: billing_lookup",
    ]
    assert view["blocks"][-1]["elements"][0]["type"] == "users_select"


def test_the_member_panel_adds_your_use_and_asks_an_admin_for_credit() -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    blocks = build_billing_container(_make_member_state(), now=_NOW, since=_SINCE)
    assert _shape(blocks) == [
        "context: January 2025",
        "divider",
        "section: *You*\n$0.75 of your $5.00 this month",
        "divider",
        "section: *$30.00* total credit left",
        "context: Ask an admin to add credit.",
    ], "a member without timed credit has no actions at all"
    with_timed = dataclasses.replace(_make_member_state(), timed_credit=(_credit("5", 20),))
    blocks = build_billing_container(with_timed, now=_NOW, since=_SINCE)
    assert _shape(blocks)[-1] == "actions: billing_expiry_open", "only Expiry dates"


def test_a_negative_balance_says_no_credit_left_and_still_shows_timed_credit() -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    state = dataclasses.replace(
        _make_member_state(), guild_balance_usd=Decimal("-3"), timed_credit=(_credit("5", 20),)
    )
    shape = _shape(build_billing_container(state, now=_NOW, since=_SINCE))
    assert shape[4:6] == [
        "section: *No credit left* $3.00 spent beyond it",
        "context: Includes $5.00 that expires. It's used first.\nAsk an admin to add credit.",
    ]


def test_expiry_dates_lists_each_credit_soonest_first() -> None:
    from daimon.adapters.slack.billing_panel.views import build_expiry_view

    first, last = _credit("5", 31), _credit("20", 20)
    view = build_expiry_view((first, last))
    assert view["title"]["text"] == "Expiry dates" and view["close"]["text"] == "Back"
    epoch = int(last.ends_at.timestamp())
    assert _shape(view["blocks"])[0] == (
        "section: Unused credit expires:\n"
        f"$20.00 · <!date^{epoch}^{{date_short_pretty}}|2025-01-20>\n"
        f"$5.00 · <!date^{int(first.ends_at.timestamp())}^{{date_short_pretty}}|2025-01-31>"
    )


def _spender_rows(state: BillingPanelState) -> list[str]:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    blocks = build_billing_container(state, now=_NOW, since=_SINCE)
    text = next(b["text"]["text"] for b in blocks if "*Top spenders*" in str(b))
    return text.splitlines()[1:]


def _row(user_id: str, *, is_caller: bool = False) -> MemberRow:
    return MemberRow(user_id, f"User {user_id[-4:]}", 2.0, 3, is_caller)


def test_top_spenders_are_mentions_which_a_modal_shows_as_names() -> None:
    state = dataclasses.replace(
        _make_admin_state(), member_rows=(_row("U0123ABCD"), _row("W0456EFGH", is_caller=True))
    )
    assert _spender_rows(state) == ["1. <@U0123ABCD>  $2.00", "2. <@W0456EFGH> _(you)_  $2.00"]


def test_an_id_that_is_not_a_slack_user_id_keeps_the_escaped_label() -> None:
    odd = MemberRow("U1|<!channel>", "User <!channel>", 1.0, 1, False)
    state = dataclasses.replace(
        _make_admin_state(), member_rows=(odd, _row("u0123abcd"), _row("B0123ABCD"))
    )
    lines = _spender_rows(state)
    assert "<!channel>" not in "\n".join(lines) and "<@" not in "\n".join(lines), lines
    assert lines[1].startswith("2. User abcd"), "a lowercase id is not a user id"
    assert lines[2].startswith("3. User ABCD"), "a bot id is not a user id"


def test_top_spenders_and_channel_budgets_count_the_rest_and_a_lookup_shows_below() -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    rows = tuple(_row(f"U{i:08d}") for i in range(7))
    budgets = tuple(_budget(f"C{i}", str(9 - i)) for i in range(7))
    state = dataclasses.replace(
        _make_admin_state(), member_rows=rows, over_cap_count=1, channel_budgets=budgets
    )
    blocks = build_billing_container(
        state, now=_NOW, since=_SINCE, lookup=("U00000001", "$2.00 this month")
    )
    shape = _shape(blocks)
    spenders = next(i for i, line in enumerate(shape) if "*Top spenders*" in line)
    assert shape[spenders + 1] == "context: + 3 more"
    budgets_at = next(i for i, line in enumerate(shape) if "*Channel budgets*" in line)
    assert shape[budgets_at].startswith(
        "section: *Channel budgets*\n<#C0>  $9.00 of $10.00 used this month"
    )
    assert "<#C5>" not in shape[budgets_at] and shape[budgets_at + 1] == "context: + 2 more"
    assert shape[-2:] == ["actions: billing_lookup", "section: *<@U00000001>*\n$2.00 this month"]
    assert blocks[-1]["block_id"] == "billing_lookup_result"


def test_a_member_sees_neither_spenders_nor_channel_budgets() -> None:
    from daimon.adapters.slack.billing_panel.views import build_billing_container

    state = dataclasses.replace(
        _make_member_state(),
        member_rows=(_row("U0123ABCD"),),
        channel_budgets=(_budget("C1", "1"),),
    )
    text = str(build_billing_container(state, now=_NOW, since=_SINCE))
    assert "Top spenders" not in text and "Channel budgets" not in text
    assert "billing_lookup" not in text, "no picker for a member"


def _panel_payload(action_id: str, **action: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": "T_PANEL"},
        "user": {"id": "U_CALLER"},
        "trigger_id": "TRIGGER",
        "view": {
            "id": "V_PANEL",
            "hash": "H_PANEL",
            "private_metadata": json.dumps({"channel_id": "C1"}),
        },
        "actions": [{"action_id": action_id, **action}],
    }


def _sent(fake: Any, method: str) -> list[dict[str, Any]]:
    from yarl import URL

    calls: list[Any] = (
        fake.mock.requests.get(("POST", URL(f"https://slack.com/api/{method}"))) or []
    )
    return [call.kwargs["json"] for call in calls]


@contextmanager
def _panel_patches(fake: Any, *, admin: bool, **more: Any) -> Iterator[None]:
    module = "daimon.adapters.slack.billing_panel.actions"
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(f"{module}.resolve_web_client", new_callable=AsyncMock, return_value=fake.client)
        )
        stack.enter_context(
            patch(f"{module}.resolve_is_admin", new_callable=AsyncMock, return_value=admin)
        )
        for name, value in more.items():
            stack.enter_context(patch(f"{module}.{name}", new=value))
        yield


async def test_expiry_dates_pushes_the_end_dates(fake_slack_web_client: Any) -> None:
    from daimon.adapters.slack.billing_panel.actions import handle_panel_action

    runtime = MagicMock()
    credits = AsyncMock(return_value=[_credit("20", 20)])
    with _panel_patches(fake_slack_web_client, admin=False, get_active_timed_credit=credits):
        await handle_panel_action(runtime, _panel_payload("billing_expiry_open"))

    [pushed] = _sent(fake_slack_web_client, "views.push")
    assert pushed["trigger_id"] == "TRIGGER" and pushed["view"]["title"]["text"] == "Expiry dates"


async def test_look_up_a_person_refuses_a_non_admin_before_reading(
    fake_slack_web_client: Any,
) -> None:
    from daimon.adapters.slack.billing_panel.actions import LOOKUP_ADMIN_ONLY, handle_panel_action

    runtime = MagicMock()
    runtime.sessionmaker.side_effect = AssertionError("no read for a non-admin")
    with _panel_patches(fake_slack_web_client, admin=False):
        await handle_panel_action(runtime, _panel_payload("billing_lookup", selected_user="U_X"))

    [pushed] = _sent(fake_slack_web_client, "views.push")
    assert LOOKUP_ADMIN_ONLY in json.dumps(pushed), "the refusal says why"
    assert _sent(fake_slack_web_client, "views.update") == [], "the panel is left as it was"


async def test_a_pick_redraws_the_panel_with_that_persons_spend(
    fake_slack_web_client: Any, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.adapters.slack.billing_panel.actions import handle_panel_action

    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    snapshot = AsyncMock(return_value=_make_admin_state())
    with _panel_patches(fake_slack_web_client, admin=True, load_billing_snapshot=snapshot):
        await handle_panel_action(
            runtime, _panel_payload("billing_lookup", selected_user="U0123ABCD")
        )

    assert snapshot.await_args is not None
    kwargs = snapshot.await_args.kwargs
    assert kwargs["is_admin"] is True and kwargs["channel_id"] == "C1", "read fresh, same channel"
    [updated] = _sent(fake_slack_web_client, "views.update")
    assert updated["view_id"] == "V_PANEL"
    assert updated["hash"] == "H_PANEL", "a slower, earlier pick can't overwrite a newer one"
    assert json.loads(updated["view"]["private_metadata"]) == {"channel_id": "C1"}
    result = updated["view"]["blocks"][-1]
    assert result["block_id"] == "billing_lookup_result"
    assert result["text"]["text"] == "*<@U0123ABCD>*\nNothing used this month", (
        "the pick shows that person's spend under the picker"
    )
