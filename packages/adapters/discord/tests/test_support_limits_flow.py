"""Support limits on Discord: the menu, option forms, admin exemption and settling a post."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import feedback_reactions, support_escalation
from daimon.adapters.discord.feedback_reactions import FeedbackReactionCog
from daimon.adapters.discord.support_escalation import (
    SupportEscalateButton,
    SupportMenuSelect,
    SupportModal,
    SupportOptionModal,
    answer_origin,
)
from daimon.adapters.discord.support_settle import SettleModal, SupportSettleButton
from daimon.core.config import SupportSettings
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import support as store
from daimon.core.stores.domain import Role
from daimon.core.stores.platform_names import ChannelSighting, upsert_channel_names
from daimon.core.stores.support_escalation import get_request, submit_request
from daimon.core.support import (
    CLIENT_NOT_NOTIFIED,
    NOT_ENOUGH_CREDITS,
    SETTLE_ELSEWHERE,
    RequestChoice,
    SupportPlace,
    option_value,
    parse_menu,
)
from daimon.core.support_escalation import ALREADY_REQUESTED, UNAVAILABLE
from daimon.core.support_escalation import CUSTOM_ID_TEMPLATE as BUTTON_TEMPLATE
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import mock_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

_GUILD = "111"
_THREAD = "333"
_ANSWER = "444"
_SUPPORT = "999"
D = Decimal
_TWO = D(2)
MENU = parse_menu(
    {
        "name": "Support",
        "options": [
            {"key": "quick", "label": "Quick", "price": {"kind": "fixed", "amount": "0.5"}},
            {
                "key": "review",
                "label": "Review",
                "price": {"kind": "range", "min": "1", "max": "3"},
            },
            {
                "key": "call",
                "label": "Call",
                "price": {
                    "kind": "steps",
                    "label": "Length",
                    "steps": [
                        {"label": "30 min", "amount": "0.5"},
                        {"label": "1 hour", "amount": "1"},
                    ],
                },
                "fields": [
                    {"key": "topic", "kind": "text", "label": "Topic"},
                    {
                        "key": "when",
                        "kind": "choice",
                        "label": "When",
                        "choices": ["Today", "This week"],
                        "required": False,
                    },
                ],
            },
        ],
    }
)


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    menu: bool = True,
    per_person: Decimal | None = _TWO,
    ends_at: datetime | None = None,
    limit: bool = True,
) -> uuid.UUID:
    """A Discord tenant with a workspace limit (unless `limit` is False), on MENU unless `menu` is; its id."""
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=_GUILD)
        if menu:
            row = await store.set_menu(
                session, tenant_id=tenant.id, key="support", spec=MENU, set_by_account_id=None
            )
            await store.set_menu_use(
                session,
                tenant_id=tenant.id,
                scope="workspace",
                scope_id="",
                menu_id=row.id,
                set_by_account_id=None,
            )
        if limit:
            await store.set_limit(
                session,
                tenant_id=tenant.id,
                scope="workspace",
                scope_id="",
                platform_user_id=None,
                shared=None,
                each_person=per_person,
                window="total",
                starts_at=None if ends_at is None else ends_at - timedelta(days=30),
                ends_at=ends_at,
                set_by_account_id=None,
            )
    return tenant.id


def _bot(sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    """A bot whose answer sits in thread 333 of channel 222 and whose support channel is 999."""
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = 222
    support = MagicMock(spec=discord.TextChannel)
    posted = MagicMock(spec=discord.Message)
    posted.id = 555
    support.send = AsyncMock(return_value=posted)
    bot = MagicMock()
    bot.get_channel = MagicMock(side_effect=lambda cid: {333: thread, 999: support}.get(cid))
    bot.runtime.sessionmaker = sessionmaker
    bot.runtime.settings = mock_settings()
    bot.runtime.settings.support = SupportSettings(escalation_channel_id=_SUPPORT)
    bot.runtime.settings.direct_message_policies = {}
    bot.support = support
    return bot


def _interaction(bot: Any, *, user_id: int = 7, channel_id: int | None = None) -> Any:
    interaction = MagicMock()
    interaction.client = bot
    interaction.user.id = user_id
    interaction.user.mention = f"<@{user_id}>"
    interaction.guild_id = None
    interaction.channel_id = channel_id
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.response.is_done = MagicMock(return_value=False)
    for name in ("defer", "send_modal", "send_message"):
        setattr(interaction.response, name, AsyncMock())
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _said(mock: AsyncMock) -> str:
    """The text of the one card a mock sent."""
    embed = mock.await_args.kwargs["embed"]
    return str(embed.description)


def _menu_select() -> SupportMenuSelect:
    return SupportMenuSelect(guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER)


def _value(key: str) -> str:
    """The menu value MENU shows for `key`."""
    option = MENU.option(key)
    assert option is not None, f"{key} is on the menu"
    return option_value(option)


async def _pick(bot: Any, key: str, value: str | None = None, **kwargs: Any) -> Any:
    select = _menu_select()
    select.item._values = [value or _value(key)]  # pyright: ignore[reportPrivateUsage]  # stands in for dispatch's _refresh_state
    interaction = _interaction(bot, **kwargs)
    await select.callback(interaction)
    return interaction


async def _rows(sessionmaker: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sessionmaker() as session:
        result = await session.execute(
            text("SELECT id, status, hold, support_post_id FROM support_escalations")
        )
        return list(result.all())


async def test_button_when_limit_has_a_menu_then_shows_options_and_balance(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The button shows the menu's options and the balance."""
    await _seed(db_session_factory)
    bot = _bot(db_session_factory)
    interaction = _interaction(bot)
    await SupportEscalateButton(guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER).callback(
        interaction
    )
    interaction.response.send_modal.assert_not_awaited()
    kwargs = interaction.followup.send.await_args.kwargs
    embed, view = kwargs["embed"], kwargs["view"]
    assert "**Review**: 1-3 credits" in str(embed.description), "each option shows its price"
    assert embed.footer.text == "2 of 2 credits left", "the balance sits under the menu"
    (select,) = view.children
    assert select.custom_id == f"spm:{_GUILD}:{_THREAD}:{_ANSWER}", "the picker names the answer"
    assert [o.value for o in select.item.options] == [
        _value(k) for k in ("quick", "review", "call")
    ], "each value carries its option's terms"


async def _confirmed(bot: Any, key: str) -> Any:
    """Pick `key`, then Submit its form untouched; the submit's interaction."""
    modal = (await _pick(bot, key)).response.send_modal.await_args.args[0]
    assert isinstance(modal, SupportOptionModal), "every pick opens the option's form"
    submit = _interaction(bot)
    await modal.on_submit(submit)
    return submit


async def test_pick_when_option_has_no_fields_then_confirms_its_terms_before_sending(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory)
    bot = _bot(db_session_factory)
    interaction = await _pick(bot, "quick")
    interaction.response.defer.assert_not_awaited()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, SupportOptionModal)
    [terms] = modal.children
    assert isinstance(terms, discord.ui.TextDisplay) and terms.content == "**Quick**: 0.5 credits"
    bot.support.send.assert_not_awaited()
    assert await _rows(db_session_factory) == [], "nothing recorded before Submit"


async def test_confirm_when_the_post_fails_then_keeps_the_request_and_logs_an_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory)
    bot = _bot(db_session_factory)
    lost = discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "lost access")
    bot.support.send = AsyncMock(side_effect=lost)
    with capture_logs() as logs:
        await _confirmed(bot, "review")
    [(_id, status, _hold, post)] = await _rows(db_session_factory)
    assert (status, post) == ("pending", None), "recorded and held, with no post"
    assert [e["log_level"] for e in logs if e["event"] == "support.request_undelivered"] == [
        "error"
    ], "the failure is an error an operator sees"


async def test_confirm_when_option_has_no_fields_then_posts_pending_with_settle_buttons(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory)
    bot = _bot(db_session_factory)
    interaction = await _confirmed(bot, "review")
    send = bot.support.send.await_args
    body, view = send.args[0], send.kwargs["view"]
    assert "Review: 1-3 credits\nPending: 1-3 credits\n1 of 2 credits left" in body, (
        "a limit request's post names its terms, status and balance"
    )
    assert f"-# {CLIENT_NOT_NOTIFIED}" in body, "decision 8 beside the settle buttons"
    assert [b.item.label for b in view.children] == ["Set charge", "Decline"]
    assert (
        _said(interaction.followup.send)
        == "Request sent. Someone will reply.\nHolding 1 credit. We will confirm the charge."
    )
    ((_id, status, hold, post_id),) = await _rows(db_session_factory)
    assert (status, hold, post_id) == ("pending", D(1), "555"), (
        "holds its minimum; the post id is stored"
    )


async def test_confirm_when_charged_to_the_default_then_post_shows_facts_and_settle_buttons(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A range option with no limit: facts and settle controls, and no balance."""
    await _seed(db_session_factory, limit=False)
    bot = _bot(db_session_factory)
    await _confirmed(bot, "review")
    send = bot.support.send.await_args
    body, view = send.args[0], send.kwargs["view"]
    assert "Review: 1-3 credits\nPending: 1-3 credits" in body, "option, terms and pending price"
    assert "credits left" not in body, "no limit, so no balance"
    assert f"-# {CLIENT_NOT_NOTIFIED}" in body, "the settler is told the client is not notified"
    assert [b.item.label for b in view.children] == ["Set charge", "Decline"]
    ((_id, status, _hold, post_id),) = await _rows(db_session_factory)
    assert (status, post_id) == ("pending", "555"), "held and posted"


async def test_pick_when_option_has_steps_then_held_until_accepted_at_its_price(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory)
    bot = _bot(db_session_factory)
    interaction = await _pick(bot, "call")
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, SupportOptionModal)
    assert len(modal.children) == 4, "terms, the step and two fields"
    assert modal.step_input is not None
    modal.step_input._values = ["1"]  # pyright: ignore[reportPrivateUsage]
    topic = modal.field_inputs["topic"]
    assert isinstance(topic, discord.ui.TextInput)
    topic._value = "Billing export"  # pyright: ignore[reportPrivateUsage]
    await modal.on_submit(_interaction(bot))
    body, view = bot.support.send.await_args.args[0], bot.support.send.await_args.kwargs["view"]
    assert "Call, 1 hour: 1 credit\nPending: 1 credit" in body, "a step waits"
    assert "Topic: Billing export" in body, "the answers are the note"
    assert [b.item.label for b in view.children] == ["Accept", "Decline"], "no amount to type"
    ((_id, status, hold, _post),) = await _rows(db_session_factory)
    assert (status, hold) == ("pending", D(1)), "holds its price"
    click = _on_post(bot, body)
    await view.children[0].callback(click)
    assert _said(click.followup.send) == f"Charged 1 credit. {CLIENT_NOT_NOTIFIED}", "accepted"
    assert "Charged 1 credit by <@8>" in click.edit_original_response.await_args.kwargs["content"]


async def test_pick_when_the_menu_changed_then_options_changed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory)
    interaction = await _pick(_bot(db_session_factory), "quick", "quick:older")
    assert _said(interaction.response.send_message) == "Options changed. Open Ask a person again."
    assert await _rows(db_session_factory) == []


async def test_pick_when_balance_cannot_cover_it_then_not_enough_credits(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, per_person=D("0.5"))
    interaction = await _pick(_bot(db_session_factory), "review")
    assert _said(interaction.response.send_message) == NOT_ENOUGH_CREDITS
    interaction.response.send_modal.assert_not_awaited()


async def _asked_before(
    sessionmaker: async_sessionmaker[AsyncSession], key: str, *, message_id: str = _ANSWER
) -> None:
    """User 7 already asked with `key` on an answer, straight through the ledger."""
    option = MENU.option(key)
    assert option is not None, f"{key} is on the menu"
    async with sessionmaker() as session, session.begin():
        outcome = await submit_request(
            session,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=_GUILD),
            account_id=None,
            platform="discord",
            platform_user_id="7",
            channel_id=_THREAD,
            message_id=message_id,
            ma_session_id=None,
            place=SupportPlace(channel_id="222", agent_id=None),
            choice=RequestChoice.of(option),
            default_allowance=20,
            exempt=False,
        )
    assert outcome.status == "recorded", outcome.status


async def _press(bot: Any) -> Any:
    interaction = _interaction(bot)
    button = SupportEscalateButton(guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER)
    await button.callback(interaction)
    return interaction


async def test_pick_when_already_asked_at_no_credits_then_already_asked_not_credits(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, per_person=D("0.5"))
    await _asked_before(db_session_factory, "quick")
    interaction = await _pick(_bot(db_session_factory), "quick")
    assert _said(interaction.response.send_message) == ALREADY_REQUESTED, "asked comes first"
    interaction.response.send_modal.assert_not_awaited()


async def test_button_when_nothing_is_affordable_then_the_refusal_and_balance_without_a_menu(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, per_person=D("0.5"))
    await _asked_before(db_session_factory, "quick", message_id="445")
    interaction = await _press(_bot(db_session_factory))
    kwargs = interaction.response.send_message.await_args.kwargs
    assert "view" not in kwargs, "no picker when nothing can be picked"
    assert (
        _said(interaction.response.send_message) == f"{NOT_ENOUGH_CREDITS}\n0 of 0.5 credits left."
    )


async def test_button_when_already_asked_and_nothing_else_is_affordable_then_already_asked(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, per_person=D("0.5"))
    await _asked_before(db_session_factory, "quick")
    interaction = await _press(_bot(db_session_factory))
    assert "view" not in interaction.response.send_message.await_args.kwargs, "no picker"
    assert (
        _said(interaction.response.send_message) == f"{ALREADY_REQUESTED}\n0 of 0.5 credits left."
    )


def _cold_bot(sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    """A bot with nothing cached whose every Discord request fails the test."""
    bot = _bot(sessionmaker)
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=AssertionError("fetch_channel before the modal"))
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = 1
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(side_effect=AssertionError("fetch_member before the modal"))
    bot.get_guild = MagicMock(return_value=guild)
    return bot


async def test_pick_when_nothing_is_cached_then_the_form_opens_without_a_discord_request(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory)
    bot = _cold_bot(db_session_factory)
    interaction = await _pick(bot, "call")
    assert isinstance(interaction.response.send_modal.await_args.args[0], SupportOptionModal)
    bot.fetch_channel.assert_not_awaited()
    bot.get_guild.return_value.fetch_member.assert_not_awaited()


async def test_pick_when_nothing_is_cached_then_a_poor_requester_is_refused_without_a_request(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, per_person=D("0.5"))
    bot = _cold_bot(db_session_factory)
    interaction = await _pick(bot, "review")
    assert _said(interaction.response.send_message) == NOT_ENOUGH_CREDITS
    bot.get_guild.return_value.fetch_member.assert_not_awaited()


async def test_pick_when_the_clicker_is_a_cached_admin_then_an_unaffordable_form_opens(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, per_person=D("0.1"))
    bot = _cold_bot(db_session_factory)
    admin = MagicMock(spec=discord.Member)
    admin.id = 7
    admin.guild_permissions = discord.Permissions(administrator=True)
    admin.guild = bot.get_guild.return_value
    bot.get_guild.return_value.get_member.return_value = admin
    interaction = await _pick(bot, "call")
    interaction.response.send_modal.assert_awaited_once()


async def test_button_when_no_menu_and_nothing_is_cached_then_the_note_form_opens_at_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, menu=False)
    bot = _cold_bot(db_session_factory)
    interaction = _interaction(bot)
    await SupportEscalateButton(guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER).callback(
        interaction
    )
    assert isinstance(interaction.response.send_modal.await_args.args[0], SupportModal)
    bot.fetch_channel.assert_not_awaited()


async def test_answer_origin_when_uncached_then_a_recorded_thread_names_its_parent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=_GUILD)
    async with db_session_factory() as session, session.begin():
        await upsert_channel_names(
            session,
            tenant_id=tenant_id,
            platform="discord",
            names={_THREAD: ChannelSighting("help", kind="thread", parent_id="222")},
        )
    bot = _cold_bot(db_session_factory)
    origin = await answer_origin(
        bot.runtime, bot, guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER, fetch=False
    )
    assert (origin.parent_id, origin.thread_id) == ("222", _THREAD)
    bot.fetch_channel.assert_not_awaited()


async def test_button_when_the_limit_ended_then_says_so(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ended limit says so instead of offering anything."""
    ends = datetime(2026, 1, 18, tzinfo=UTC)
    await _seed(db_session_factory, ends_at=ends)
    interaction = _interaction(_bot(db_session_factory))
    await SupportEscalateButton(guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER).callback(
        interaction
    )
    assert _said(interaction.response.send_message) == "Support ended on 17 Jan."


async def test_button_when_the_feature_is_off_then_says_so_before_any_form(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old button must not open a form that can only fail at Send."""
    await _seed(db_session_factory, menu=False)
    off = ResolvedFeature("support_escalation", "off", "default")
    monkeypatch.setattr(support_escalation, "place_feature", AsyncMock(return_value=off))
    interaction = _interaction(_bot(db_session_factory))
    await SupportEscalateButton(guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER).callback(
        interaction
    )
    assert _said(interaction.response.send_message) == UNAVAILABLE
    interaction.response.send_modal.assert_not_awaited()


async def test_default_note_when_requester_is_admin_then_uses_no_credits(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(db_session_factory, menu=False)
    bot = _bot(db_session_factory)
    bot.get_guild = MagicMock(return_value=MagicMock(spec=discord.Guild))
    monkeypatch.setattr(
        support_escalation, "requester_role", AsyncMock(return_value=(Role.ADMIN, []))
    )
    modal = SupportModal(
        runtime=bot.runtime, guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER
    )
    modal.note_input._value = "help please"  # pyright: ignore[reportPrivateUsage]
    interaction = _interaction(bot)
    await modal.on_submit(interaction)
    assert "No charge: an admin asked" in bot.support.send.await_args.args[0]
    assert (
        _said(interaction.followup.send)
        == "Request sent. Someone will reply.\nThis used no credits."
    )
    ((_id, status, hold, _post),) = await _rows(db_session_factory)
    assert (status, hold) == ("settled", D(0)), "an admin's request holds nothing"


async def test_default_note_when_no_limit_then_post_is_unchanged(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a limit the default note posts as before."""
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=_GUILD)
    bot = _bot(db_session_factory)
    modal = SupportModal(
        runtime=bot.runtime, guild_id=_GUILD, channel_id=_THREAD, message_id=_ANSWER
    )
    modal.note_input._value = "help please"  # pyright: ignore[reportPrivateUsage]
    await modal.on_submit(_interaction(bot))
    send = bot.support.send.await_args
    assert send.args[0].split("\n\n")[2:] == ["help please"], "head, link, note: as before"
    assert send.kwargs == {}, "no buttons and no mention override on the default post"


async def _pending(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[Any, str]:
    """A pending Review request on a fresh tenant; its settle view and the posted text."""
    await _seed(sessionmaker)
    bot = _bot(sessionmaker)
    await _confirmed(bot, "review")
    send = bot.support.send.await_args
    return send.kwargs["view"], send.args[0]


def _on_post(bot: Any, content: str) -> Any:
    interaction = _interaction(bot, user_id=8, channel_id=int(_SUPPORT))
    interaction.message.content = content
    return interaction


async def test_set_charge_when_in_range_then_charges_and_closes_the_post(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view, content = await _pending(db_session_factory)
    bot = _bot(db_session_factory)
    click = _on_post(bot, content)
    await view.children[0].callback(click)
    modal = click.response.send_modal.await_args.args[0]
    assert isinstance(modal, SettleModal)
    assert modal.amount_input.default == "1", "prefilled with the hold"
    modal.amount_input._value = "2"  # pyright: ignore[reportPrivateUsage]
    submit = _on_post(bot, content)
    await modal.on_submit(submit)
    edited = submit.edit_original_response.await_args.kwargs
    assert edited["view"] is None, "the buttons go"
    assert "Charged 2 credits by <@8>" in edited["content"]
    assert CLIENT_NOT_NOTIFIED not in edited["content"], "the pending warning goes with them"
    assert _said(submit.followup.send) == f"Charged 2 credits. {CLIENT_NOT_NOTIFIED}"
    again = _on_post(bot, edited["content"])
    await view.children[1].callback(again)
    assert _said(again.followup.send) == "Already charged 2 credits by <@8>."


async def test_set_charge_when_out_of_range_then_names_the_range(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view, content = await _pending(db_session_factory)
    bot = _bot(db_session_factory)
    click = _on_post(bot, content)
    await view.children[0].callback(click)
    modal = click.response.send_modal.await_args.args[0]
    modal.amount_input._value = "5"  # pyright: ignore[reportPrivateUsage]
    submit = _on_post(bot, content)
    await modal.on_submit(submit)
    assert _said(submit.followup.send) == "Enter an amount from 1 to 2.", "capped by the balance"
    submit.edit_original_response.assert_not_awaited()
    ((_id, status, _hold, _post),) = await _rows(db_session_factory)
    assert status == "pending", "nothing was settled"


async def test_decline_when_pending_then_releases_the_hold(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view, content = await _pending(db_session_factory)
    click = _on_post(_bot(db_session_factory), content)
    await view.children[1].callback(click)
    assert _said(click.followup.send) == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}"
    assert "Declined by <@8>" in click.edit_original_response.await_args.kwargs["content"]
    ((escalation_id, status, _hold, _post),) = await _rows(db_session_factory)
    async with db_session_factory() as session:
        row = await get_request(session, escalation_id=escalation_id)
    assert status == "declined" and row is not None and row.settled_by == "8"


async def test_settle_button_when_outside_the_support_channel_then_refuses(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view, content = await _pending(db_session_factory)
    click = _interaction(_bot(db_session_factory), user_id=8, channel_id=123)
    click.message.content = content
    await view.children[1].callback(click)
    assert _said(click.response.send_message) == SETTLE_ELSEWHERE, "only the posted channel settles"
    ((_id, status, _hold, _post),) = await _rows(db_session_factory)
    assert status == "pending", "nothing was settled"


async def _own_channel(sessionmaker: async_sessionmaker[AsyncSession], channel_id: str) -> None:
    """The workspace picks its own request channel."""
    async with sessionmaker() as session, session.begin():
        await store.set_support_channel(
            session,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=_GUILD),
            platform="discord",
            channel_id=channel_id,
            set_by_account_id=None,
        )


def _also_at(bot: Any, channel_id: int) -> Any:
    """`bot` with its support channel mock reachable at `channel_id` instead of 999."""
    known = bot.get_channel.side_effect
    bot.get_channel = MagicMock(
        side_effect=lambda cid: bot.support if cid == channel_id else known(cid)
    )
    return bot


def _only_at(bot: Any, channel_id: int) -> Any:
    """`bot` with its support channel mock at `channel_id` and nothing at 999."""
    bot.get_channel = MagicMock(side_effect=lambda cid: bot.support if cid == channel_id else None)
    return bot


async def _stamps(sessionmaker: async_sessionmaker[AsyncSession]) -> list[tuple[Any, ...]]:
    async with sessionmaker() as session:
        result = await session.execute(
            text("SELECT support_channel_id, support_post_id FROM support_escalations")
        )
        return [tuple(r) for r in result.all()]


async def test_confirm_when_the_workspace_has_its_own_channel_then_posts_and_stamps_there(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A workspace's own channel takes its requests; the stamp names it."""
    await _seed(db_session_factory)
    await _own_channel(db_session_factory, "888")
    bot = _only_at(_bot(db_session_factory), 888)
    await _confirmed(bot, "review")
    assert bot.support.send.await_count == 1, "posted once"
    assert await _stamps(db_session_factory) == [("888", "555")], "the post's channel is stamped"


async def test_confirm_when_own_channels_are_off_then_posts_to_the_deployment_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With `support_own_channel` off a picked channel is ignored."""
    await _seed(db_session_factory)
    await _own_channel(db_session_factory, "888")
    bot = _bot(db_session_factory)
    bot.runtime.settings.features["support_own_channel"] = "off"
    await _confirmed(bot, "review")
    assert await _stamps(db_session_factory) == [("999", "555")], "the deployment's channel"


async def test_settle_when_the_channel_changed_then_the_open_request_settles_where_posted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An open request settles where it was posted, not in the newly picked channel."""
    view, content = await _pending(db_session_factory)
    await _own_channel(db_session_factory, "888")
    bot = _also_at(_bot(db_session_factory), 888)
    moved = _interaction(bot, user_id=8, channel_id=888)
    moved.message.content = content
    await view.children[1].callback(moved)
    assert _said(moved.response.send_message) == SETTLE_ELSEWHERE, "the new channel did not get it"
    click = _on_post(bot, content)
    await view.children[1].callback(click)
    said = _said(click.followup.send)
    assert said == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}", said


async def test_settle_when_the_request_predates_the_stamp_then_the_default_channel_settles(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A request from before the stamp settles in the deployment channel only."""
    await _seed(db_session_factory)
    await _asked_before(db_session_factory, "review")
    ((escalation_id, _status, _hold, _post),) = await _rows(db_session_factory)
    assert await _stamps(db_session_factory) == [(None, None)], "an old, unstamped request"
    button = SupportSettleButton(escalation_id=escalation_id, action="decline")
    elsewhere = _interaction(_bot(db_session_factory), user_id=8, channel_id=123)
    await button.callback(elsewhere)
    assert _said(elsewhere.response.send_message) == SETTLE_ELSEWHERE, "another channel refuses"
    click = _on_post(_bot(db_session_factory), "Human support requested")
    await button.callback(click)
    said = _said(click.followup.send)
    assert said == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}", said


async def test_button_when_an_uncached_admin_has_no_credits_then_the_form_opens(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An admin missing from the cache is fetched once and stays exempt."""
    await _seed(db_session_factory, menu=False, per_person=D("0.1"))
    bot = _cold_bot(db_session_factory)
    admin = MagicMock(spec=discord.Member)
    admin.guild_permissions = discord.Permissions(administrator=True)
    bot.get_guild.return_value.fetch_member = AsyncMock(return_value=admin)
    interaction = await _press(bot)
    assert isinstance(interaction.response.send_modal.await_args.args[0], SupportModal), (
        "an admin is exempt, so the form opens"
    )
    bot.get_guild.return_value.fetch_member.assert_awaited_once()


def test_new_templates_when_matched_then_overlap_no_other() -> None:
    """Menu, settle and old `sup:` ids each dispatch to exactly one item."""
    menu = _menu_select().item.custom_id
    settle = f"sps:{uuid.uuid4().hex}:set"
    button = f"sup:{_GUILD}:{_THREAD}:{_ANSWER}"
    templates = {
        "menu": SupportMenuSelect.__discord_ui_compiled_template__,
        "settle": SupportSettleButton.__discord_ui_compiled_template__,
        "button": SupportEscalateButton.__discord_ui_compiled_template__,
    }
    assert templates["button"].pattern == BUTTON_TEMPLATE, "old sup: ids keep their template"
    for name, custom_id in {"menu": menu, "settle": settle, "button": button}.items():
        matched = [key for key, template in templates.items() if template.fullmatch(custom_id)]
        assert matched == [name], f"{custom_id} must dispatch to exactly one item"
        assert len(custom_id) <= 100, "Discord caps a custom id at 100 characters"


async def test_reaction_when_limit_has_a_menu_then_dms_the_menu(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A thumbs-down under a limit with a menu DMs the menu."""
    await _seed(db_session_factory)
    bot = _bot(db_session_factory)
    user = MagicMock()
    user.send = AsyncMock()
    bot.get_user = MagicMock(return_value=user)
    payload = MagicMock(guild_id=int(_GUILD), channel_id=int(_THREAD), message_id=int(_ANSWER))
    payload.user_id = 7
    await FeedbackReactionCog(bot)._offer_support(payload)  # pyright: ignore[reportPrivateUsage]
    kwargs = user.send.await_args.kwargs
    assert isinstance(kwargs["view"].children[0], SupportMenuSelect), (
        "the menu, not the note button"
    )
    assert kwargs["embed"].footer.text == "2 of 2 credits left"


async def test_reaction_when_no_limit_then_dms_the_note_button_with_requests_left(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a limit the DM offers the note button and requests left."""
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=_GUILD)
    bot = _bot(db_session_factory)
    user = MagicMock()
    user.send = AsyncMock()
    bot.get_user = MagicMock(return_value=user)
    payload = MagicMock(guild_id=int(_GUILD), channel_id=int(_THREAD), message_id=int(_ANSWER))
    payload.user_id = 7
    await FeedbackReactionCog(bot)._offer_support(payload)  # pyright: ignore[reportPrivateUsage]
    kwargs = user.send.await_args.kwargs
    expected = feedback_reactions.support_prompt_embed(
        f"https://discord.com/channels/{_GUILD}/{_THREAD}/{_ANSWER}", remaining=20
    )
    assert kwargs["embed"].to_dict() == expected.to_dict(), "today's prompt, unchanged"
    assert isinstance(kwargs["view"].children[0], SupportEscalateButton)


async def test_reaction_when_nothing_is_affordable_then_dms_the_refusal_without_a_menu(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, per_person=D("0.5"))
    await _asked_before(db_session_factory, "quick")
    bot = _bot(db_session_factory)
    user = MagicMock()
    user.send = AsyncMock()
    bot.get_user = MagicMock(return_value=user)
    payload = MagicMock(guild_id=int(_GUILD), channel_id=int(_THREAD), message_id=int(_ANSWER))
    payload.user_id = 7
    await FeedbackReactionCog(bot)._offer_support(payload)  # pyright: ignore[reportPrivateUsage]
    kwargs = user.send.await_args.kwargs
    assert "view" not in kwargs, "no picker when nothing can be picked"
    assert kwargs["embed"].description == f"{ALREADY_REQUESTED}\n0 of 0.5 credits left."
