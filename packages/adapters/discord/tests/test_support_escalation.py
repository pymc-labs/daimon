"""Ask a human on Discord: where a request from a channel with its own admins goes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.support_escalation import (
    SupportEscalateButton,
    SupportModal,
    discord_channel,
    post_to_support_channel,
)
from daimon.core.config import SupportSettings
from daimon.core.stores import accounts
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = "111"
_CHANNEL = "222"
_THREAD = "333"
_ESCALATION = "999"


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


async def _seed(session: AsyncSession, *, grant: bool) -> None:
    tenant = await make_tenant(session, platform="discord", workspace_id=_GUILD)
    account = await make_account(session, tenant=tenant)
    await make_platform_principal(
        session, platform="discord", external_id="50", tenant=tenant, account=account
    )
    await accounts.set_role(session, account.id, Role.ADMIN)
    if grant:
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=_CHANNEL,
            role_ids=(),
            user_ids=("40", "7"),
            actor_account_id=None,
        )
    await session.commit()


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


async def test_a_thread_of_a_channel_with_admins_dms_them_not_the_escalation_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session, grant=True)

    bot, escalation, dm = await _submit(db_session_factory)

    bot.open_member_dm.assert_awaited_once_with(int(_GUILD), 40)
    body = dm.send.await_args.args[0]
    assert "help please" in body and f"/{_THREAD}/444" in body
    head, link, note = body.split("\n\n")
    assert head.startswith("**Human support requested** by "), "who asked, then a blank line"
    assert link.endswith(f"/{_THREAD}/444") and note == "help please", "link, then the note"
    escalation.send.assert_not_awaited()
    assert await _delivered(db_session_factory) == [True]


async def test_partial_admin_dm_falls_back_to_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session, grant=True)

    dm = AsyncMock()
    dm.send.side_effect = [
        None,
        discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "closed"),
    ]

    async def open_dm(_guild_id: int, _user_id: int) -> Any:
        if _user_id != 40:
            raise LookupError("server admin unavailable")
        return dm

    _bot, channel, _dm = await _submit(db_session_factory, open_dm=open_dm, note="x" * 4000)

    assert dm.send.await_count == 2
    assert channel.send.await_count > 1
    assert await _delivered(db_session_factory) == [True]


async def test_a_channel_without_admins_keeps_the_escalation_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session, grant=False)

    bot, escalation, _dm = await _submit(db_session_factory)

    bot.open_member_dm.assert_not_awaited()
    escalation.send.assert_awaited_once()
    assert await _delivered(db_session_factory) == [True]


async def test_unreachable_channel_admins_fall_back_to_the_server_admins(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session, grant=True)
    opened: list[int] = []

    async def open_dm(guild_id: int, user_id: int) -> Any:
        opened.append(user_id)
        if user_id == 40:
            raise LookupError("left the server")
        return AsyncMock()

    _bot, escalation, _dm = await _submit(db_session_factory, open_dm=open_dm)

    assert opened == [40, 50], "the channel's admin first, then the server admin"
    escalation.send.assert_not_awaited()


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


async def test_a_teams_escalation_channel_spends_nothing_and_posts_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        await _seed(session, grant=False)

    bot, escalation, dm = await _submit(
        db_session_factory, escalation_channel="19:support@thread.tacv2"
    )

    escalation.send.assert_not_awaited()
    dm.send.assert_not_awaited()
    assert await _delivered(db_session_factory) == [], "no credit is spent on an undeliverable ask"
