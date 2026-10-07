"""Ask a human on Discord: where a request from a channel with its own admins goes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.support_escalation import SupportEscalateButton, SupportModal
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
    sessionmaker: async_sessionmaker[AsyncSession], *, open_dm: Any = None
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
    runtime.settings.support = SupportSettings(escalation_channel_id=_ESCALATION)
    runtime.settings.direct_message_policies = {}
    modal = SupportModal(runtime=runtime, guild_id=_GUILD, channel_id=_THREAD, message_id="444")
    modal.note_input = SimpleNamespace(value="help please")  # type: ignore[assignment]
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
    escalation.send.assert_not_awaited()
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
    assert (button.label, str(button.emoji)) == ("Ask the team", "🙋")
    modal = SupportModal(
        runtime=cast(Any, None), guild_id=_GUILD, channel_id=_THREAD, message_id="444"
    )
    assert modal.title == "Ask the team"
    assert modal.note_input.label == "What do you need help with?"
    assert modal.note_input.placeholder == "Someone from the team will reply."
    assert len(modal.note_input.label) <= 45, "Discord rejects a longer text-input label"
