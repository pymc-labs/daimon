"""/billing redeem-code button, modal and timed-credit lines."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.billing_panel import redeem

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
from daimon.core.promo_codes import (
    build_promo_code_terms,
    generate_promo_code,
    hash_promo_code,
    normalize_chosen_promo_code,
    normalize_promo_code,
)
from daimon.core.promo_credit import ActiveTimedCredit, PromoRedeemed, PromoRedeemRefused
from daimon.core.stores import channel_budgets as budget_store
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_account, make_channel_budget, make_tenant
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 888000000000000001
CHANNEL_ID = 888000000000000777
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


def _view(
    *, is_admin: bool, runtime: Any = None, has_redeemable_promo_code: bool = True
) -> BillingPanelView:
    return BillingPanelView(
        _state(is_admin=is_admin, has_redeemable_promo_code=has_redeemable_promo_code),
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
    interaction.channel = None
    interaction.channel_id = CHANNEL_ID
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


def test_the_redeem_button_shows_while_a_code_the_viewer_may_redeem_is_redeemable() -> None:
    """Admin and member panels both offer redemption while the snapshot found a code for them."""
    assert _buttons(_view(is_admin=True)) == ["Redeem code", "Refresh", "Done"], (
        "the admin view should offer redemption first"
    )
    assert _buttons(_view(is_admin=True, has_redeemable_promo_code=False)) == [
        "Refresh",
        "Done",
    ], "without a redeemable code the admin view should not offer redemption"
    assert _buttons(_view(is_admin=False)) == ["Redeem code", "Refresh", "Done"], (
        "a member view should offer redemption while a channel code is redeemable"
    )
    assert "Redeem code" not in _buttons(_view(is_admin=False, has_redeemable_promo_code=False))


def test_timed_credit_shows_under_the_total_in_both_views() -> None:
    """Live timed credit shows in both views and is absent without any."""
    credit = (ActiveTimedCredit(remaining_usd=Decimal("7.5"), ends_at=END),)
    for is_admin in (True, False):
        container = build_billing_container(
            _state(is_admin=is_admin, timed_credit=credit), now=NOW, since=SINCE
        )
        text = _text(container)
        assert "-# Includes $7.50 that expires. It's used first." in text, (
            "each view should say how much of the credit expires"
        )
    plain = build_billing_container(_state(), now=NOW, since=SINCE)
    assert "Includes" not in _text(plain), "no timed credit should mean no line"


def test_ready_embed_mentions_redemption_only_when_a_code_is_redeemable() -> None:
    """A deployment without promo codes keeps its ready message unchanged."""
    assert "promo" not in (_build_ready_embed("daimon").description or ""), "no codes, no mention"
    with_codes = _build_ready_embed("daimon", promo_codes=True).description or ""
    assert "Redeem it in `/billing`" in with_codes, "people are pointed at /billing"


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
        "🎟️ Added **$10.00** of credit.\n\nBalance: **$12.50**"
    ), "a credit reply should name the amount and balance"
    later = redeemed(kind="timed", credit_starts_at=NOW, credit_ends_at=END, granted=False)
    assert f"from <t:{int(NOW.timestamp())}:f> until" in redeem_result_text(later), (
        "a timed credit not yet granted should name its window"
    )
    active = redeemed(kind="timed", credit_ends_at=END)
    assert redeem_result_text(active) == (
        "🎟️ Added **$10.00** of credit.\n\n"
        f"Used before credit with no expiry. Anything unused expires <t:{int(END.timestamp())}:f>."
    )
    assert redeem_result_text(PromoRedeemRefused("expired")) == "🎟️ That code has expired.", (
        "a refusal should explain itself"
    )


async def test_redeem_button_opens_the_modal_for_anyone_it_is_shown_to() -> None:
    """Members and admins both get the modal; what a code may do is decided on submit."""
    view = _view(is_admin=False)
    button = next(c for c in view.walk_children() if isinstance(c, _RedeemButton))
    for admin in (False, True):
        click = _interaction(admin=admin)
        await button.callback(click)
        assert isinstance(click.response.send_modal.call_args.args[0], RedeemCodeModal), (
            "the button should open the redeem modal"
        )


async def test_a_member_cannot_redeem_a_server_wide_code(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server-wide code from a member (or a demoted admin) is refused, audited, and not credited."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    tenant = await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    account = await make_account(db_session, tenant=tenant)
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_promo_code("WELCOME-2026")), terms=terms
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory
    audit = AsyncMock()
    monkeypatch.setattr(redeem, "record_panel_write", audit)
    rerender = AsyncMock()
    modal = RedeemCodeModal(runtime=runtime, account_id=account.id, rerender=rerender)
    modal.code_in._value = "WELCOME-2026"
    interaction = _interaction(admin=False)
    await modal.on_submit(interaction)
    assert interaction.followup.send.call_args.args[0] == (
        "🎟️ Only an admin can redeem that code, because it adds credit for everyone here."
    ), "a member should be told who can redeem it"
    rerender.assert_not_awaited()
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant_id) == Decimal("0")
    assert audit.await_args is not None
    assert audit.await_args.kwargs | {"tenant_id": None} == {
        "tenant_id": None,
        "platform": "discord",
        "platform_user_id": str(interaction.user.id),
        "op": "promo_redeem",
        "target_channel_id": str(CHANNEL_ID),
        "outcome": "denied",
        "reason": "promo:admin_only",
    }, "the refusal is audited with the channel"


async def test_modal_submit_answers_when_the_database_fails() -> None:
    """After defer() the admin still gets an ephemeral error, not a silent spinner."""
    runtime = MagicMock(spec=DiscordRuntime)
    lost = OperationalError("SELECT 1", {}, ConnectionError("connection lost"))
    runtime.sessionmaker = MagicMock(side_effect=lost)
    runtime.sessionmaker.begin = MagicMock(side_effect=lost)  # the audit row is lost too
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
    calls: list[str] = []

    async def rerender_panel(_submitted: discord.Interaction) -> None:
        calls.append("rerender")

    def record_reply(*_args: object, **_kwargs: object) -> None:
        calls.append("reply")

    rerender = AsyncMock(side_effect=rerender_panel)
    modal = RedeemCodeModal(runtime=runtime, account_id=account.id, rerender=rerender)
    modal.code_in._value = "welcome-2026"

    interaction = _interaction(admin=True)
    interaction.followup.send.side_effect = record_reply
    await modal.on_submit(interaction)

    interaction.response.defer.assert_awaited_once()
    rerender.assert_awaited_once_with(interaction)
    assert calls == ["reply", "rerender"], "the reply should go out before the panel rerenders"
    assert "Added **$10.00**" in interaction.followup.send.call_args.args[0], (
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


async def test_a_failed_reply_after_a_redemption_is_audited_as_the_redemption(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The credit committed, so the audit row says allowed even when Discord drops the reply."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    tenant = await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant_id, platform="discord", external_id="42"
    )
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_promo_code("WELCOME-2026")), terms=terms
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory
    rerender = AsyncMock()
    modal = RedeemCodeModal(runtime=runtime, account_id=principal.account_id, rerender=rerender)
    modal.code_in._value = "WELCOME-2026"
    interaction = _interaction(admin=True)
    interaction.followup.send.side_effect = discord.HTTPException(MagicMock(), "unknown webhook")

    await modal.on_submit(interaction)

    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("10")
    events = await list_events(db_session, tenant_id=tenant_id)
    assert [(e.outcome, e.reason, e.target_channel_id, e.account_id) for e in events] == [
        ("allowed", "completed", str(CHANNEL_ID), principal.account_id)
    ], "one row, for what the redemption did, not for the lost reply"
    rerender.assert_awaited_once_with(interaction)


async def test_modal_submit_keeps_the_success_reply_when_the_rerender_fails(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A failed panel rerender after a redemption sends no error over the success reply."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    tenant = await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    account = await make_account(db_session, tenant=tenant)
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_promo_code("WELCOME-2026")), terms=terms
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory
    rerender = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "unknown message"))
    modal = RedeemCodeModal(runtime=runtime, account_id=account.id, rerender=rerender)
    modal.code_in._value = "WELCOME-2026"
    interaction = _interaction(admin=True)

    await modal.on_submit(interaction)

    rerender.assert_awaited_once_with(interaction)
    [reply] = interaction.followup.send.call_args_list
    assert "Added **$10.00**" in reply.args[0], "the only reply should confirm the credit"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant_id) == Decimal("10"), (
        "the credit should stay in the server ledger"
    )


def _live_interaction(*, admin: bool = True) -> MagicMock:
    """An admin interaction whose response flips to done once deferred, as Discord's does."""
    interaction = _interaction(admin=admin)
    interaction.guild.get_member.return_value = None
    deferred = False

    def is_done() -> bool:
        return deferred

    async def defer(*_args: object, **_kwargs: object) -> None:
        nonlocal deferred
        deferred = True

    interaction.response.is_done.side_effect = is_done
    interaction.response.defer = AsyncMock(side_effect=defer)
    interaction.edit_original_response = AsyncMock()
    return interaction


def _submitted(modal: RedeemCodeModal, code: str, *, admin: bool = True) -> MagicMock:
    """A modal submit carrying ``code`` in the field Discord was sent, read back by discord.py."""

    def fill(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in components:
            if c["type"] == 1:  # action row
                out.append({"type": 1, "components": fill(c["components"])})
            elif c["type"] == 18:  # label
                out.append({"type": 18, "component": fill([c["component"]])[0]})
            else:
                out.append({"type": c["type"], "custom_id": c["custom_id"], "value": code})
        return out

    interaction = _live_interaction(admin=admin)
    modal._refresh(interaction, cast(Any, fill(modal.to_dict()["components"])), {})
    return interaction


async def test_cli_issued_code_redeems_once_through_the_panel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A code issued the way `daimon promo create` issues it redeems once from /billing.

    Drives the real panel: the button opens the modal, discord.py reads the
    typed code back from the submit payload, the handler redeems and redraws
    the panel. A second redemption and an unknown code are each refused in words.
    """
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    shown = generate_promo_code()  # what the CLI prints
    terms = build_promo_code_terms(amount_usd=Decimal("25"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_chosen_promo_code(shown)), terms=terms
    )
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant_id, platform="discord", external_id="42"
    )
    await db_session.commit()
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory
    now = datetime.now(UTC)
    view = BillingPanelView(
        await _admin_snapshot(db_session, now=now),
        runtime=runtime,
        allowed_user_id=42,
        is_admin=True,
        account_id=principal.account_id,
        now=now,
        since=SINCE,
    )
    button = next(c for c in view.walk_children() if isinstance(c, _RedeemButton))

    click = _live_interaction()
    await button.callback(click)
    modal = click.response.send_modal.call_args.args[0]
    assert isinstance(modal, RedeemCodeModal), "the button should open the redeem modal"

    first = _submitted(modal, f"  {shown.lower()} ")
    await modal.on_submit(first)
    assert first.followup.send.call_args.args[0] == (
        "🎟️ Added **$25.00** of credit.\n\nBalance: **$25.00**"
    ), "a fresh code, pasted loosely, should credit the server once"
    first.edit_original_response.assert_awaited_once()
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant_id) == Decimal("25"), (
        "the credit should land in the server ledger"
    )

    again = _submitted(modal, shown)
    await modal.on_submit(again)
    assert again.followup.send.call_args.args[0] == "🎟️ That code was already redeemed here.", (
        "a second redemption should be refused in words"
    )
    unknown = _submitted(modal, generate_promo_code())
    await modal.on_submit(unknown)
    assert unknown.followup.send.call_args.args[0] == (
        "🎟️ That code is not valid. Check it and try again."
    ), "an unknown code should be refused in words"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant_id) == Decimal("25"), (
        "refusals should not move the balance"
    )
    again.edit_original_response.assert_not_awaited()
    unknown.edit_original_response.assert_not_awaited()


async def test_a_member_redeems_a_channel_code_into_this_channel_through_the_panel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A member's panel offers a channel code; it raises this channel's budget only."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    tenant = await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    for channel_id in (CHANNEL_ID, CHANNEL_ID + 1):
        await make_channel_budget(
            db_session, tenant=tenant, platform="discord", channel_id=str(channel_id)
        )
    shown = generate_promo_code()
    terms = build_promo_code_terms(
        amount_usd=Decimal("5"), timed=False, channel_cap_usd=Decimal("10")
    )
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_chosen_promo_code(shown)), terms=terms
    )
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant_id, platform="discord", external_id="42"
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = db_session_factory
    now = datetime.now(UTC)
    state = await load_billing_snapshot(
        db_session,
        guild=MagicMock(spec=discord.Guild),
        guild_id=str(GUILD_ID),
        caller_user_id="42",
        is_admin=False,
        since=SINCE,
        now=now,
        channel_id=str(CHANNEL_ID),
    )
    assert state.has_redeemable_promo_code, "a channel code is offered to a member"
    view = BillingPanelView(
        state,
        runtime=runtime,
        allowed_user_id=42,
        is_admin=False,
        account_id=principal.account_id,
        now=now,
        since=SINCE,
    )
    button = next(c for c in view.walk_children() if isinstance(c, _RedeemButton))
    click = _live_interaction(admin=False)
    await button.callback(click)
    modal = click.response.send_modal.call_args.args[0]
    assert isinstance(modal, RedeemCodeModal)

    first = _submitted(modal, shown, admin=False)
    await modal.on_submit(first)
    again = _submitted(modal, shown, admin=False)
    await modal.on_submit(again)

    assert first.followup.send.call_args.args[0] == (
        f"🎟️ Added **$5.00** to <#{CHANNEL_ID}>'s budget.\n\nIts limit is now **$10.00**."
    )
    first.edit_original_response.assert_awaited_once()
    assert again.followup.send.call_args.args[0] == (
        "🎟️ You already redeemed that code in this channel."
    )
    db_session.expire_all()
    limits = [
        budget.limit_usd
        for budget in await budget_store.list_channel_budgets(db_session, tenant_id=tenant_id)
    ]
    assert sorted(limits) == [Decimal("5"), Decimal("10")], "only this channel's limit rose"


async def _admin_snapshot(session: AsyncSession, *, now: datetime) -> BillingPanelState:
    return await load_billing_snapshot(
        session,
        guild=MagicMock(spec=discord.Guild),
        guild_id=str(GUILD_ID),
        caller_user_id="42",
        is_admin=True,
        since=SINCE,
        now=now,
    )


async def test_snapshot_offers_redemption_only_while_a_code_is_redeemable(
    db_session: AsyncSession,
) -> None:
    """The redeem button stays hidden until some code can be redeemed."""
    now = datetime.now(UTC)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    before = await _admin_snapshot(db_session, now=now)
    assert not before.has_redeemable_promo_code, "no code should mean no redeem button"

    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(db_session, code_hash="h", terms=terms)
    after = await _admin_snapshot(db_session, now=now)
    assert after.has_redeemable_promo_code, "a redeemable code should show the redeem button"
    view = BillingPanelView(
        after,
        runtime=MagicMock(spec=DiscordRuntime),
        allowed_user_id=42,
        is_admin=True,
        account_id=uuid.uuid4(),
        now=now,
        since=SINCE,
    )
    assert "Redeem code" in _buttons(view), "the admin panel should render the button"


async def test_snapshot_hides_redemption_when_the_lookup_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed promo lookup hides the button and leaves the rest of the read working."""

    async def failing_lookup(session: AsyncSession, *, now: datetime, **_: object) -> bool:
        await session.execute(text("SELECT 1/0"))  # aborts the surrounding transaction
        return True

    monkeypatch.setattr(
        "daimon.adapters.discord.billing_panel.read.has_redeemable_promo_code", failing_lookup
    )
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD_ID))
    await make_tenant(db_session, id=tenant_id, workspace_id=str(GUILD_ID))
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(db_session, code_hash="h", terms=terms)

    state = await _admin_snapshot(db_session, now=datetime.now(UTC))

    assert state.is_admin and not state.has_redeemable_promo_code, (
        "a failed lookup should still load the admin panel, without the redeem button"
    )


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
