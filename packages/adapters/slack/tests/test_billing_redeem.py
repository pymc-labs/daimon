"""Slack /billing: redeem-code button, form, submission and timed-credit lines."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.billing_panel import redeem
from daimon.adapters.slack.billing_panel.redeem import (
    evaluate_redeem_submission,
    handle_redeem_open,
    redeem_result_text,
    run_redeem_submission,
)
from daimon.adapters.slack.billing_panel.views import build_billing_container
from daimon.core.billing_panel import BillingPanelState
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.promo_credit import ActiveTimedCredit, PromoRedeemed, PromoRedeemRefused
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.testing.factories import make_channel_budget, make_tenant
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

_TEAM = "T_REDEEM"
_USER = "U_ADMIN"
_NOW = datetime(2026, 5, 14, 12, tzinfo=UTC)
_SINCE = datetime(2026, 5, 1, tzinfo=UTC)
_END = datetime(2026, 5, 20, tzinfo=UTC)
_MODULE = "daimon.adapters.slack.billing_panel.redeem"


def _state(**overrides: Any) -> BillingPanelState:
    base: dict[str, Any] = {
        "is_admin": True,
        "caller_user_id": _USER,
        "caller_spend": 0.0,
        "caller_turns": 0,
        "caller_cap": None,
        "guild_balance_usd": Decimal("0"),
        "guild_spend": 0.0,
        "guild_turns": 0,
        "guild_distinct_members": 0,
        "member_rows": (),
        "over_cap_count": 0,
    }
    return BillingPanelState(**(base | overrides))


def _action_ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _texts(blocks: list[dict[str, Any]]) -> str:
    """Every section's and context block's text."""
    texts = [str(b.get("text", {}).get("text", "")) for b in blocks]
    texts += [str(e.get("text", "")) for b in blocks for e in b.get("elements", [])]
    return "\n".join(texts)


def _submission(code: str) -> dict[str, Any]:
    return {
        "view": {
            "id": "V_FORM",
            "private_metadata": json.dumps({"root_view_id": "V_ROOT", "channel_id": "C1"}),
            "state": {"values": {"billing_redeem_code": {"code": {"value": code}}}},
        }
    }


@contextmanager
def _slack(fake: Any, *, admin: bool) -> Iterator[None]:
    with (
        patch(f"{_MODULE}.resolve_web_client", new_callable=AsyncMock, return_value=fake.client),
        patch(f"{_MODULE}.resolve_is_admin", new_callable=AsyncMock, return_value=admin),
    ):
        yield


def _bodies(fake: Any, method: str) -> list[dict[str, Any]]:
    calls: list[Any] = (
        fake.mock.requests.get(("POST", URL(f"https://slack.com/api/{method}"))) or []
    )
    bodies: list[dict[str, Any]] = [call.kwargs["json"] for call in calls]
    return bodies


def test_only_admins_get_the_redeem_button_while_a_code_is_redeemable() -> None:
    """Only the admin panel carries the redeem-code button, and only while a code is redeemable."""
    admin = build_billing_container(_state(has_redeemable_promo_code=True), now=_NOW, since=_SINCE)
    no_code = build_billing_container(_state(), now=_NOW, since=_SINCE)
    member = build_billing_container(
        _state(is_admin=False, has_redeemable_promo_code=True), now=_NOW, since=_SINCE
    )
    assert "billing_redeem_open" in _action_ids(admin), "admins should get the redeem button"
    assert _action_ids(no_code) == ["billing_topup", "billing_lookup"], (
        "no redeemable code should hide the button"
    )
    assert "billing_redeem_open" not in _action_ids(member), "members should not get it"


def test_timed_credit_shows_under_the_total_in_both_views() -> None:
    """Live timed credit shows in both views and is absent without any."""
    credit = (ActiveTimedCredit(remaining_usd=Decimal("7.5"), ends_at=_END),)
    for is_admin in (True, False):
        blocks = build_billing_container(
            _state(is_admin=is_admin, timed_credit=credit), now=_NOW, since=_SINCE
        )
        assert "Includes $7.50 that expires. It's used first." in _texts(blocks), (
            "each view should say how much of the credit expires"
        )
    assert "Includes" not in _texts(build_billing_container(_state(), now=_NOW, since=_SINCE)), (
        "no timed credit should mean no line"
    )


def test_evaluate_redeem_submission() -> None:
    """A blank code is rejected in the form; a real one proceeds with both view ids."""
    empty = evaluate_redeem_submission(_submission("   "))
    assert not empty.proceed and empty.response_payload["response_action"] == "errors", (
        "a blank code should be rejected in the form"
    )
    ok = evaluate_redeem_submission(_submission("abc-def"))
    assert ok.proceed and (ok.code, ok.view_id, ok.root_view_id) == (
        "abc-def",
        "V_FORM",
        "V_ROOT",
    ), "a code should proceed with the form and panel view ids"
    assert ok.response_payload["response_action"] == "update", (
        "a valid submission should update the form in place"
    )


def test_redeem_result_text() -> None:
    """Each redeem outcome renders its own reply."""
    redeemed = PromoRedeemed(
        promo_code_id=uuid.uuid4(),
        kind="credit",
        amount_usd=Decimal("10"),
        credit_starts_at=None,
        credit_ends_at=None,
        granted=True,
        balance_usd=Decimal("12.5"),
    )
    assert redeem_result_text(redeemed) == "🎟️ Redeemed *$10.00* of credit. Balance: *$12.50*.", (
        "a credit reply should name the amount and balance"
    )
    assert redeem_result_text(PromoRedeemRefused("revoked")) == "That code is no longer active.", (
        "a refusal should explain itself"
    )


async def test_open_pushes_the_form_for_admins_only(fake_slack_web_client: Any) -> None:
    """The redeem button pushes the form for admins and a notice for everyone else."""
    payload = {
        "team": {"id": _TEAM},
        "user": {"id": _USER},
        "trigger_id": "TR",
        "view": {"id": "V", "private_metadata": json.dumps({"channel_id": "C1"})},
    }
    with _slack(fake_slack_web_client, admin=False):
        await handle_redeem_open(MagicMock(), payload)
    with _slack(fake_slack_web_client, admin=True):
        await handle_redeem_open(MagicMock(), payload)
    first, second = (b["view"] for b in _bodies(fake_slack_web_client, "views.push"))
    assert "callback_id" not in first and "admins" in _texts(first["blocks"]), (
        "a non-admin should get a notice, not the form"
    )
    assert second["callback_id"] == "billing_redeem", "an admin should get the redeem form"
    assert json.loads(second["private_metadata"]) == {"root_view_id": "V", "channel_id": "C1"}, (
        "the form should remember the panel and the channel it came from"
    )


async def test_submission_redeems_and_refreshes_the_panel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A submitted code credits the workspace and refreshes the panel; a repeat is refused."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    await make_channel_budget(db_session, tenant=tenant, platform="slack", channel_id="C1")
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_promo_code("WELCOME-2026")), terms=terms
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory

    with _slack(fake_slack_web_client, admin=True):
        for code in ("welcome-2026", "WELCOME-2026"):
            await run_redeem_submission(
                runtime,
                fake_slack_web_client.client,
                team_id=_TEAM,
                user_id=_USER,
                decision=evaluate_redeem_submission(_submission(code)),
            )

    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("10"), (
        "the code should be credited only once"
    )
    result, root, retry = _bodies(fake_slack_web_client, "views.update")
    assert result["view_id"] == "V_FORM" and "Redeemed *$10.00*" in _texts(
        result["view"]["blocks"]
    ), "the form should confirm the credit"
    assert root["view_id"] == "V_ROOT" and "*$10.00* total credit left" in _texts(
        root["view"]["blocks"]
    ), "the panel should show the new balance"
    assert "*Channel budget*\n$0.00 of $5.00 used this month" in _texts(root["view"]["blocks"]), (
        "the refreshed panel should keep the channel's budget line"
    )
    assert json.loads(root["view"]["private_metadata"]) == {"channel_id": "C1"}, (
        "and keep the channel for the next refresh"
    )
    assert retry["view"]["callback_id"] == "billing_redeem", "a repeat should reopen the form"
    assert "already redeemed" in _texts(retry["view"]["blocks"]), (
        "a repeat should be refused as already redeemed"
    )


async def test_submission_keeps_the_success_reply_when_the_panel_refresh_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A failed panel refresh after a redemption does not overwrite the success message."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_promo_code("WELCOME-2026")), terms=terms
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    failing_refresh = AsyncMock(
        side_effect=OperationalError("SELECT 1", {}, ConnectionError("connection lost"))
    )

    with (
        _slack(fake_slack_web_client, admin=True),
        patch(f"{_MODULE}.load_billing_snapshot", failing_refresh),
    ):
        await run_redeem_submission(
            runtime,
            fake_slack_web_client.client,
            team_id=_TEAM,
            user_id=_USER,
            decision=evaluate_redeem_submission(_submission("WELCOME-2026")),
        )

    failing_refresh.assert_awaited_once()
    [result] = _bodies(fake_slack_web_client, "views.update")
    assert result["view_id"] == "V_FORM" and "Redeemed *$10.00*" in _texts(
        result["view"]["blocks"]
    ), "the form should keep confirming the credit"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("10"), (
        "the credit should stay in the workspace ledger"
    )


async def test_submission_from_a_non_admin_redeems_nothing(fake_slack_web_client: Any) -> None:
    """A non-admin submission is refused, and audited, before any database lookup."""
    runtime = MagicMock()
    runtime.sessionmaker = MagicMock(side_effect=AssertionError("must not open a session"))
    audit = AsyncMock()
    with (
        _slack(fake_slack_web_client, admin=False),
        patch.object(redeem, "record_panel_write", audit),
    ):
        await run_redeem_submission(
            runtime,
            fake_slack_web_client.client,
            team_id=_TEAM,
            user_id="U_MEMBER",
            decision=evaluate_redeem_submission(_submission("WELCOME-2026")),
        )
    [body] = _bodies(fake_slack_web_client, "views.update")
    assert "admins" in _texts(body["view"]["blocks"]), "a non-admin should be told who can redeem"
    assert audit.await_args is not None
    assert (audit.await_args.kwargs["op"], audit.await_args.kwargs["outcome"]) == (
        "promo_redeem",
        "denied",
    ), "the refusal is audited"
