"""Slack /billing: redeem-code button, form, submission and timed-credit lines."""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.billing_panel.redeem import (
    evaluate_redeem_submission,
    handle_redeem_open,
    promo_channel,
    redeem_result_text,
    run_redeem_submission,
)
from daimon.adapters.slack.billing_panel.views import build_billing_container
from daimon.core.billing_panel import BillingPanelState
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.promo_credit import ActiveTimedCredit, PromoRedeemed, PromoRedeemRefused
from daimon.core.stores import channel_budgets as budget_store
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.core.stores.security_audit import list_events
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
        patch(f"{_MODULE}.resolve_is_platform_admin", new_callable=AsyncMock, return_value=admin),
    ):
        yield


def _bodies(fake: Any, method: str) -> list[dict[str, Any]]:
    calls: list[Any] = (
        fake.mock.requests.get(("POST", URL(f"https://slack.com/api/{method}"))) or []
    )
    bodies: list[dict[str, Any]] = [call.kwargs["json"] for call in calls]
    return bodies


def test_the_redeem_button_shows_while_a_code_the_viewer_may_redeem_is_redeemable() -> None:
    """Both views carry the redeem button while the snapshot found a code the viewer may redeem."""
    admin = build_billing_container(_state(has_redeemable_promo_code=True), now=_NOW, since=_SINCE)
    no_code = build_billing_container(_state(), now=_NOW, since=_SINCE)
    member = build_billing_container(
        _state(is_admin=False, has_redeemable_promo_code=True), now=_NOW, since=_SINCE
    )
    member_no_code = build_billing_container(_state(is_admin=False), now=_NOW, since=_SINCE)
    assert "billing_redeem_open" in _action_ids(admin), "admins should get the redeem button"
    assert _action_ids(no_code) == ["billing_topup", "billing_lookup"], (
        "no redeemable code should hide the button"
    )
    assert _action_ids(member) == [], "only platform admins redeem channel codes too"
    assert "billing_redeem_open" not in _action_ids(member_no_code), "and not without one"


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
    assert redeem_result_text(redeemed) == "🎟️ Added *$10.00* of credit.\n\nBalance: *$12.50*", (
        "a credit reply should name the amount and balance"
    )
    timed = dataclasses.replace(
        redeemed, kind="timed", credit_ends_at=datetime(2026, 5, 20, tzinfo=UTC)
    )
    assert redeem_result_text(timed).startswith(
        "🎟️ Added *$10.00* of credit.\n\nUsed before credit with no expiry. "
    )
    assert redeem_result_text(PromoRedeemRefused("revoked")) == "That code is no longer active.", (
        "a refusal should explain itself"
    )


async def test_open_pushes_the_form_for_anyone(fake_slack_web_client: Any) -> None:
    """The redeem button pushes the form; what the code may do is decided on submit."""
    payload = {
        "team": {"id": _TEAM},
        "user": {"id": "U_MEMBER"},
        "trigger_id": "TR",
        "view": {"id": "V", "private_metadata": json.dumps({"channel_id": "C1"})},
    }
    with _slack(fake_slack_web_client, admin=False):
        await handle_redeem_open(MagicMock(), payload)
    [body] = _bodies(fake_slack_web_client, "views.push")
    assert body["view"]["callback_id"] == "billing_redeem", "a member should get the form"
    assert json.loads(body["view"]["private_metadata"]) == {
        "root_view_id": "V",
        "channel_id": "C1",
    }, "the form should remember the panel and the channel it came from"


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
    assert result["view_id"] == "V_FORM" and "Added *$10.00*" in _texts(result["view"]["blocks"]), (
        "the form should confirm the credit"
    )
    assert root["view_id"] == "V_ROOT" and "*$10.00* total credit left" in _texts(
        root["view"]["blocks"]
    ), "the panel should show the new balance"
    assert "*This channel*\n$0.00 of $5.00 used this month" in _texts(root["view"]["blocks"]), (
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
    assert result["view_id"] == "V_FORM" and "Added *$10.00*" in _texts(result["view"]["blocks"]), (
        "the form should keep confirming the credit"
    )
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("10"), (
        "the credit should stay in the workspace ledger"
    )


def _conversations(fake: Any, **kinds: dict[str, bool]) -> None:
    """Answer conversations.info for each channel id with its Slack type flags."""
    for channel_id, flags in kinds.items():
        fake.mock.get(
            re.compile(rf"https://slack\.com/api/conversations\.info\?.*channel={channel_id}.*"),
            payload={"ok": True, "channel": {"id": channel_id, **flags}},
            repeat=True,
        )


async def test_a_platform_admin_redeems_a_channel_code_into_a_real_channel_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Public and private channels take a member's channel code; a DM and a group DM never do.

    The conversation type is Slack's own (conversations.info): the group DM
    here uses Slack's documented MPIM id, a ``G`` id like a private channel's.
    A member's server-wide code is refused; an admin's still lands server-wide
    from a group DM. Every attempt is audited with the actor and the channel.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    for channel_id in ("C1", "G0PRIVATE"):
        await make_channel_budget(
            db_session, tenant=tenant, platform="slack", channel_id=channel_id
        )
    for code, cap in (("TEAM-ROOM-2026", Decimal("20")), ("WELCOME-2026", None)):
        terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False, channel_cap_usd=cap)
        await promo_store.insert_promo_code(
            db_session, code_hash=hash_promo_code(normalize_promo_code(code)), terms=terms
        )
    _conversations(
        fake_slack_web_client,
        C1={"is_channel": True},
        G0PRIVATE={"is_group": True, "is_private": True},
        G024BE91L={"is_group": True, "is_mpim": True},
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory

    def submission(code: str, channel_id: str) -> Any:
        payload = _submission(code)
        payload["view"]["private_metadata"] = json.dumps(
            {"root_view_id": "", "channel_id": channel_id}
        )
        return evaluate_redeem_submission(payload)

    async def submit(code: str, channel_id: str, *, admin: bool = False) -> None:
        with _slack(fake_slack_web_client, admin=admin):
            await run_redeem_submission(
                runtime,
                fake_slack_web_client.client,
                team_id=_TEAM,
                user_id=_USER if admin else "U_MEMBER",
                decision=submission(code, channel_id),
            )

    await submit("team-room-2026", "C1", admin=True)
    await submit("TEAM-ROOM-2026", "G0PRIVATE", admin=True)
    await submit("TEAM-ROOM-2026", "G024BE91L", admin=True)
    await submit("TEAM-ROOM-2026", "D1", admin=True)
    await submit("WELCOME-2026", "C1")
    await submit("WELCOME-2026", "G024BE91L", admin=True)

    public, private, group_dm, dm, server_code, admin = (
        _texts(body["view"]["blocks"]) for body in _bodies(fake_slack_web_client, "views.update")
    )
    assert "Added *$10.00* to <#C1>'s budget" in public and "*$15.00*" in public
    assert "Added *$10.00* to <#G0PRIVATE>'s budget" in private, "a private channel counts"
    assert "not a DM" in group_dm, "a group DM has no channel to credit"
    assert "not a DM" in dm
    assert "Only a platform admin can redeem that code" in server_code
    assert "Added *$10.00* of credit" in admin, "an admin's server-wide code still lands"
    db_session.expire_all()
    limits = {
        b.channel_id: b.limit_usd
        for b in await budget_store.list_channel_budgets(db_session, tenant_id=tenant.id)
    }
    assert limits == {"C1": Decimal("15"), "G0PRIVATE": Decimal("15")}
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("30"), (
        "two channel credits and one server-wide credit, nothing for the DMs"
    )
    events = await list_events(db_session, tenant_id=tenant.id)
    assert [(e.outcome, e.reason, e.target_channel_id) for e in events] == [
        ("allowed", "completed", "C1"),
        ("allowed", "completed", "G0PRIVATE"),
        ("denied", "promo:needs_channel", "G024BE91L"),
        ("denied", "promo:needs_channel", "D1"),
        ("denied", "promo:admin_only", "C1"),
        ("allowed", "completed", "G024BE91L"),
    ]
    assert {e.platform_user_id for e in events} == {"U_MEMBER", _USER}, "each actor is named"


async def test_a_channel_slack_will_not_describe_is_refused_for_a_channel_code(
    fake_slack_web_client: Any,
) -> None:
    """Fail closed: an unreadable conversation is not treated as a channel."""
    fake_slack_web_client.mock.get(
        re.compile(r"https://slack\.com/api/conversations\.info.*"),
        payload={"ok": False, "error": "missing_scope"},
        repeat=True,
    )
    assert await promo_channel(fake_slack_web_client.client, "G024BE91L") is None
    assert await promo_channel(fake_slack_web_client.client, "") is None
