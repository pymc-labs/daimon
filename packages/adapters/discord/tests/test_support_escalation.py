"""Ask a human on Discord: support requests and sealed-channel warnings."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.support_escalation import (
    SupportEscalateButton,
    SupportModal,
    discord_channel,
    post_to_support_channel,
)
from daimon.adapters.discord.support_post import (
    CANNOT_SEE,
    NEEDS_PERMISSIONS,
    NOT_TEXT,
    post_blocker,
)
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import SupportSettings
from daimon.core.stores import accounts
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.settings_mock import mock_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = "111"
_CHANNEL = "222"
_THREAD = "333"
_ESCALATION = "999"


async def _seed(session: AsyncSession, *, sealed: bool = False) -> None:
    tenant = await make_tenant(session, platform="discord", workspace_id=_GUILD)
    if sealed:
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(channel_rules={_CHANNEL: ChannelRule(readers="inside")}),
        )
    account = await make_account(session, tenant=tenant)
    await make_platform_principal(
        session, platform="discord", external_id="50", tenant=tenant, account=account
    )
    await accounts.set_role(session, account.id, Role.ADMIN)
    await session.commit()


async def test_long_support_note_reaches_channel_in_order() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    sent: list[str] = []

    async def send(body: str, **_kwargs: Any) -> None:
        if len(body) > 2000:
            raise discord.HTTPException(
                SimpleNamespace(status=400, reason="Bad Request"), "too long"
            )
        sent.append(body)

    channel.send = AsyncMock(side_effect=send)
    bot = MagicMock()
    bot.get_channel.return_value = channel
    note = "x" * 4000
    body = "**Human support requested** by <@7>\nhttps://discord.com/channels/1/2/3\n\n" + note

    assert await post_to_support_channel(bot, channel_id=_ESCALATION, body=body)
    assert all(len(part) <= 2000 for part in sent)
    assert sent[0].startswith("**Human support requested** by <@7>\nhttps://")
    assert "".join(sent).endswith(note)


async def test_partial_support_channel_post_is_undelivered() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(
        side_effect=[
            None,
            discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "lost access"),
        ]
    )
    bot = MagicMock()
    bot.get_channel.return_value = channel

    assert not await post_to_support_channel(bot, channel_id=_ESCALATION, body="x" * 4000)
    assert channel.send.await_count == 2


async def _submit(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    open_dm: Any = None,
    escalation_channel: str = _ESCALATION,
    note: str = "help please",
) -> tuple[Any, Any, AsyncMock]:
    """Send a note on an answer in a thread of `_CHANNEL`; the bot, the escalation channel, a DM."""
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = int(_CHANNEL)
    escalation = MagicMock(spec=discord.TextChannel)
    escalation.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel = MagicMock(side_effect=lambda cid: {333: thread, 999: escalation}.get(cid))
    dm = AsyncMock()
    bot.open_member_dm = AsyncMock(return_value=dm, side_effect=open_dm)
    interaction = MagicMock()
    interaction.client = bot
    interaction.user.id = 7
    interaction.user.mention = "<@7>"
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    runtime = MagicMock()
    runtime.sessionmaker = sessionmaker
    runtime.settings = mock_settings()
    runtime.settings.support = SupportSettings(escalation_channel_id=escalation_channel)
    runtime.settings.direct_message_policies = {}
    modal = SupportModal(runtime=runtime, guild_id=_GUILD, channel_id=_THREAD, message_id="444")
    modal.note_input = SimpleNamespace(value=note)  # type: ignore[assignment]
    await modal.on_submit(interaction)
    return bot, escalation, dm


async def _delivered(sessionmaker: async_sessionmaker[AsyncSession]) -> list[bool]:
    async with sessionmaker() as session:
        rows = await session.execute(text("SELECT delivered_at FROM support_escalations"))
        return [row is not None for (row,) in rows]


async def test_support_note_uses_the_escalation_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session)
    bot, escalation, _dm = await _submit(db_session_factory)
    bot.open_member_dm.assert_not_awaited()
    escalation.send.assert_awaited_once()
    assert await _delivered(db_session_factory) == [True]


async def test_a_teams_escalation_channel_spends_nothing_and_posts_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session)
    _bot, escalation, dm = await _submit(
        db_session_factory, escalation_channel="19:support@thread.tacv2"
    )
    escalation.send.assert_not_awaited()
    dm.send.assert_not_awaited()
    assert await _delivered(db_session_factory) == []


async def test_a_sealed_channel_marks_the_post_and_an_open_one_does_not(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session, sealed=True)
    _bot, escalation, _dm = await _submit(db_session_factory)
    head, link, marker, note = escalation.send.await_args.args[0].split("\n\n")
    assert head.startswith("**Human support requested** by ") and link.endswith("/444")
    assert marker == "*Reply in the original channel.*"
    assert note == "help please"


async def test_the_form_warns_only_for_a_sealed_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session, sealed=True)
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = int(_CHANNEL)
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    runtime.settings = mock_settings()
    runtime.settings.support = SupportSettings(escalation_channel_id=_ESCALATION)
    bot = MagicMock()
    bot.runtime = runtime
    bot.get_channel = MagicMock(side_effect=lambda cid: {333: thread}.get(cid))
    interaction = MagicMock()
    interaction.client = bot
    interaction.response.send_modal = AsyncMock()
    await SupportEscalateButton(guild_id=_GUILD, channel_id=_THREAD, message_id="444").callback(
        interaction
    )
    modal = interaction.response.send_modal.await_args.args[0]
    hints = [item.content for item in modal.children if isinstance(item, discord.ui.TextDisplay)]
    assert hints == [
        "-# Support gets your note and a link, not the conversation.\n\n"
        "-# Leave out anything that must stay in this channel."
    ]
    open_form = SupportModal(
        runtime=cast(Any, None), guild_id=_GUILD, channel_id=_THREAD, message_id="444"
    )
    assert not any(isinstance(item, discord.ui.TextDisplay) for item in open_form.children)


def test_the_dm_button_and_form_say_ask_the_team_like_slack() -> None:
    button = SupportEscalateButton(guild_id=_GUILD, channel_id=_THREAD, message_id="444").item
    assert (button.label, str(button.emoji)) == ("Ask a person", "🙋")
    modal = SupportModal(
        runtime=cast(Any, None), guild_id=_GUILD, channel_id=_THREAD, message_id="444"
    )
    assert modal.title == "Ask a person"
    assert modal.note_input.label == "What do you need help with?"
    assert modal.note_input.placeholder == "Write a few words."
    assert len(modal.note_input.label) <= 45, "Discord rejects a longer text-input label"


def test_a_teams_escalation_channel_is_not_a_discord_one() -> None:
    """A Teams id left from when Teams shared the setting is not Discord's."""
    teams = SupportSettings(escalation_channel_id="19:support@thread.tacv2")
    assert discord_channel(teams) is None, "the Discord bot cannot post in a Teams channel"
    assert discord_channel(SupportSettings(escalation_channel_id=_ESCALATION)) == _ESCALATION, (
        "a Discord channel id stays the destination"
    )


def _channel_bot(channel: Any) -> Any:
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    return bot


def _text_channel(**allowed: bool) -> Any:
    channel = MagicMock(spec=discord.TextChannel)
    channel.permissions_for = MagicMock(return_value=discord.Permissions(**allowed))
    return channel


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        ({"view_channel": True, "send_messages": True, "embed_links": True}, None),
        ({"view_channel": True, "send_messages": True}, NEEDS_PERMISSIONS),
        ({"view_channel": True, "embed_links": True}, NEEDS_PERMISSIONS),
    ],
)
async def test_post_blocker_when_permissions_vary_then_names_what_is_missing(
    allowed: dict[str, bool], expected: str | None
) -> None:
    """View Channel, Send Messages and Embed Links are each required."""
    got = await post_blocker(_channel_bot(_text_channel(**allowed)), "1")
    assert got == expected, f"{allowed} reads as {expected}"


async def test_post_blocker_when_not_a_text_channel_or_unreachable_then_says_so() -> None:
    """A voice channel is not a text channel; a fetch Discord refuses is out of reach."""
    voice = MagicMock(spec=discord.VoiceChannel)
    assert await post_blocker(_channel_bot(voice), "1") == NOT_TEXT, "only text channels"
    bot = _channel_bot(None)
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
    bot.fetch_channel = AsyncMock(side_effect=forbidden)
    assert await post_blocker(bot, "1") == CANNOT_SEE, "a refused fetch"
    assert await post_blocker(bot, "not-a-number") == CANNOT_SEE, "a malformed id"
