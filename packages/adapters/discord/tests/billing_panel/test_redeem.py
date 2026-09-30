"""/billing redeem-code button, modal and timed-credit lines."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

# pyright: reportPrivateUsage=false
from daimon.adapters.discord.billing_panel.panel import (
    BillingPanelView,
    _RedeemButton,
    build_billing_container,
)
from daimon.adapters.discord.billing_panel.read import load_billing_snapshot
from daimon.adapters.discord.billing_panel.redeem import RedeemCodeModal, redeem_result_text
from daimon.adapters.discord.billing_panel.state import BillingPanelState
from daimon.adapters.discord.bot import _build_ready_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.promo_credit import ActiveTimedCredit, PromoRedeemed, PromoRedeemRefused
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 888000000000000001
NOW = datetime(2026, 5, 14, 12, tzinfo=UTC)
SINCE = datetime(2026, 5, 1, tzinfo=UTC)
END = datetime(2026, 5, 20, tzinfo=UTC)


def _state(**overrides: Any) -> BillingPanelState:
    base: dict[str, Any] = {
        "is_admin": True,
        "caller_user_id": "42",
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


def _view(*, is_admin: bool, runtime: Any = None) -> BillingPanelView:
    return BillingPanelView(
        _state(is_admin=is_admin),
        runtime=runtime or MagicMock(spec=DiscordRuntime),
        allowed_user_id=42,
        is_admin=is_admin,
        account_id=uuid.uuid4(),
        now=NOW,
        since=SINCE,
    )


def _interaction(*, admin: bool) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = 43
    user = MagicMock(spec=discord.Member)
    user.id = 42
    user.guild_permissions.administrator = admin
    user.guild_permissions.manage_guild = admin
    interaction = MagicMock()
    interaction.guild_id = GUILD_ID
    interaction.guild = guild
    interaction.user = user
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _text(container: discord.ui.Container[Any]) -> str:
    return "\n".join(
        c.content for c in container.walk_children() if isinstance(c, discord.ui.TextDisplay)
    )


def _buttons(view: BillingPanelView) -> list[str | None]:
    return [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def test_only_the_admin_view_offers_redemption() -> None:
    """Only the admin panel shows the redeem-code button."""
    assert _buttons(_view(is_admin=True)) == ["🎟️ Redeem code", "🔄 Refresh", "Done"], (
        "the admin view should offer redemption first"
    )
    assert "🎟️ Redeem code" not in _buttons(_view(is_admin=False)), (
        "a member view should not offer redemption"
    )


def test_timed_credit_shows_under_server_credit_in_both_views() -> None:
    """Live timed credit shows in both views and is absent without any."""
    credit = (ActiveTimedCredit(remaining_usd=Decimal("7.5"), ends_at=END),)
    for is_admin in (True, False):
        container = build_billing_container(
            _state(is_admin=is_admin, timed_credit=credit), now=NOW, since=SINCE
        )
        text = _text(container)
        assert f"$7.50 timed credit left · ends <t:{int(END.timestamp())}:f>" in text, (
            "each view should show the timed credit and its end"
        )
    plain = build_billing_container(_state(), now=NOW, since=SINCE)
    assert "timed credit" not in _text(plain), "no timed credit should mean no line"


def test_ready_embed_mentions_redemption_only_when_a_code_is_redeemable() -> None:
    """A deployment without promo codes keeps its ready message unchanged."""
    assert "promo" not in (_build_ready_embed().description or ""), "no codes, no mention"
    with_codes = _build_ready_embed(promo_codes=True).description or ""
    assert "Admins can redeem it in `/billing`" in with_codes, "admins are pointed at /billing"


def test_redeem_result_text() -> None:
    """Each redeem outcome renders its own reply."""

    def redeemed(**kw: Any) -> PromoRedeemed:
        base: dict[str, Any] = {
            "promo_code_id": uuid.uuid4(),
            "kind": "credit",
            "amount_usd": Decimal("10"),
            "credit_starts_at": None,
            "credit_ends_at": None,
            "granted": True,
            "balance_usd": Decimal("12.5"),
        }
        return PromoRedeemed(**(base | kw))

    assert redeem_result_text(redeemed()) == (
        "🎟️ Redeemed **$10.00** of credit. Balance: **$12.50**."
    ), "a credit reply should name the amount and balance"
    later = redeemed(kind="timed", credit_starts_at=NOW, credit_ends_at=END, granted=False)
    assert f"usable from <t:{int(NOW.timestamp())}:f> until" in redeem_result_text(later), (
        "a timed credit not yet granted should name its window"
    )
    assert redeem_result_text(PromoRedeemRefused("expired")) == "🎟️ That code has expired.", (
        "a refusal should explain itself"
    )


@pytest.mark.asyncio
async def test_redeem_button_opens_the_modal_only_for_a_live_admin() -> None:
    """The button rechecks admin rights before opening the redeem modal."""
    view = _view(is_admin=True)
    button = next(c for c in view.walk_children() if isinstance(c, _RedeemButton))

    demoted = _interaction(admin=False)
    await button.callback(demoted)
    demoted.response.send_modal.assert_not_awaited()
    assert "Manage Server" in demoted.response.send_message.call_args.args[0], (
        "a demoted admin should be told the permission they lack"
    )

    admin = _interaction(admin=True)
    await button.callback(admin)
    assert isinstance(admin.response.send_modal.call_args.args[0], RedeemCodeModal), (
        "a live admin should get the redeem modal"
    )


@pytest.mark.asyncio
async def test_modal_submit_from_a_demoted_admin_redeems_nothing() -> None:
    """A submit from a user who lost admin rights is refused before any lookup."""
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = MagicMock(side_effect=AssertionError("must not open a session"))
    rerender = AsyncMock()
    modal = RedeemCodeModal(runtime=runtime, account_id=uuid.uuid4(), rerender=rerender)
    modal.code_in._value = "WELCOME-2026"
    interaction = _interaction(admin=False)
    await modal.on_submit(interaction)
    assert "Manage Server" in interaction.response.send_message.call_args.args[0], (
        "a demoted admin should be told the permission they lack"
    )
    rerender.assert_not_awaited()


@pytest.mark.asyncio
async def test_modal_submit_answers_when_the_database_fails() -> None:
    """After defer() the admin still gets an ephemeral error, not a silent spinner."""
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = MagicMock(
        side_effect=OperationalError("SELECT 1", {}, ConnectionError("connection lost"))
    )
    rerender = AsyncMock()
    modal = RedeemCodeModal(runtime=runtime, account_id=uuid.uuid4(), rerender=rerender)
    modal.code_in._value = "WELCOME-2026"
    interaction = _interaction(admin=True)

    await modal.on_submit(interaction)

    interaction.response.defer.assert_awaited_once()
    rerender.assert_not_awaited()
    assert interaction.followup.send.call_args.kwargs == {"ephemeral": True}, (
        "the failure should be answered ephemerally"
    )


@pytest.mark.asyncio
async def test_modal_submit_redeems_rerenders_and_replies(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A valid code credits the server, rerenders the panel and replies; a repeat is refused."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    tenant = await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    account = await make_account(db_session, tenant=tenant)
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_promo_code("WELCOME-2026")), terms=terms
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory
    rerender = AsyncMock()
    modal = RedeemCodeModal(runtime=runtime, account_id=account.id, rerender=rerender)
    modal.code_in._value = "welcome-2026"

    interaction = _interaction(admin=True)
    await modal.on_submit(interaction)

    interaction.response.defer.assert_awaited_once()
    rerender.assert_awaited_once_with(interaction)
    assert "Redeemed **$10.00**" in interaction.followup.send.call_args.args[0], (
        "the reply should confirm the credit"
    )
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant_id) == Decimal("10"), (
        "the credit should land in the server ledger"
    )

    again = _interaction(admin=True)
    await modal.on_submit(again)
    assert again.followup.send.call_args.args[0] == "🎟️ That code was already redeemed here.", (
        "a second redemption should be refused"
    )
    assert rerender.await_count == 1, "a refused repeat should not rerender the panel"


@pytest.mark.asyncio
async def test_snapshot_carries_live_timed_credit(db_session: AsyncSession) -> None:
    """The billing snapshot includes timed credit whose window is open."""
    now = datetime.now(UTC)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    terms = build_promo_code_terms(
        amount_usd=Decimal("5"),
        timed=True,
        credit_starts_at=now - timedelta(hours=1),
        credit_ends_at=now + timedelta(days=1),
    )
    code = await promo_store.insert_promo_code(db_session, code_hash="h", terms=terms)
    assert code is not None, "the promo code should be inserted"
    await promo_store.insert_redemption(
        db_session,
        promo_code_id=code.id,
        tenant_id=tenant_id,
        account_id=None,
        now=now,
        granted=True,
    )
    state = await load_billing_snapshot(
        db_session,
        guild=MagicMock(spec=discord.Guild),
        guild_id=str(GUILD_ID),
        caller_user_id="42",
        is_admin=False,
        since=SINCE,
        now=now,
    )
    assert [c.remaining_usd for c in state.timed_credit] == [Decimal("5")], (
        "the snapshot should carry the live timed credit"
    )
