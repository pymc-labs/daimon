"""`/dm` and later DM turns answer to the source channel's budget.

Admission and the DM store are patched on the command module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import direct_messages as dm_module
from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_queue import TurnQueue
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = 700000101
_PARENT = 2
_THREAD = 3
_OVER_BUDGET = "This channel has used its spending budget. A server admin can raise or clear it."


async def _no_history(*, limit: int) -> AsyncIterator[discord.Message]:
    for message in ():
        yield message


@pytest.mark.parametrize("over_budget", [True, False])
async def test_dm_from_a_thread_admits_and_records_its_parent_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    over_budget: bool,
) -> None:
    await provision_tenant(db_session_factory, platform="discord", workspace_id=str(_GUILD))
    admitted: list[dict[str, Any]] = []

    async def admit(deps: object, **kwargs: Any) -> MagicMock:
        admitted.append(kwargs)
        if over_budget:
            raise AdmissionDenied(reason="channel_budget_exceeded")
        return MagicMock(source_sealed=False)

    monkeypatch.setattr(dm_module, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(dm_module, "admit", admit)
    start_dm = AsyncMock()
    monkeypatch.setattr(dm_module, "start_dm", start_dm)

    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    dm_channel = MagicMock()
    dm_channel.id = 5
    dm_channel.send = AsyncMock()
    member = MagicMock()
    member.id = 999
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    member.create_dm = AsyncMock(return_value=dm_channel)
    guild = MagicMock()
    guild.id = _GUILD
    guild.owner_id = 1
    guild.fetch_member = AsyncMock(return_value=member)
    thread = MagicMock(spec=discord.Thread)
    thread.id = _THREAD
    thread.parent_id = _PARENT
    thread.history = _no_history
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.guild_id = _GUILD
    interaction.guild = guild
    interaction.channel = thread
    interaction.user.id = member.id
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    cog = DirectMessageCog(MagicMock(runtime=runtime))

    await cog.dm.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    (call,) = admitted
    assert (call["is_dm"], call["dm_source_channel_id"]) == (True, str(_PARENT)), (
        "admitted against the thread's parent channel"
    )
    (reply,), _ = interaction.followup.send.await_args
    if over_budget:
        assert reply == _OVER_BUDGET, "the member is told why"
        start_dm.assert_not_awaited()
        member.create_dm.assert_not_awaited()
    else:
        assert reply == "Ready in your DMs.", "a channel within its budget opens the DM"
        assert start_dm.await_args.kwargs["source_channel_id"] == str(_PARENT), (
            "the DM records its source channel"
        )


async def test_a_later_dm_turn_over_its_source_budget_tells_the_member(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key="5",
            external_user_id="999",
            workspace_id=str(_GUILD),
            source_channel_id=str(_PARENT),
        )
    monkeypatch.setattr(
        dm_module,
        "reply_to_dm",
        AsyncMock(side_effect=AdmissionDenied(reason="channel_budget_exceeded")),
    )
    member = MagicMock()
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    guild = MagicMock()
    guild.owner_id = 1
    guild.fetch_member = AsyncMock(return_value=member)
    bot = MagicMock()
    bot.draining = False
    bot.runtime.sessionmaker = db_session_factory
    bot.get_guild.return_value = guild
    bot.turn_queue = TurnQueue(
        platform="discord", global_cap=1, max_queued_per_tenant=1, max_queued=1, max_wait_s=60
    )
    message = MagicMock()
    message.author.bot = False
    message.author.id = 999
    message.channel = MagicMock(spec=discord.DMChannel)
    message.channel.id = 5
    message.channel.send = AsyncMock()
    message.content = "hello"
    cog = DirectMessageCog(bot)

    await cog.on_message(message)

    (reply,), _ = message.channel.send.await_args
    assert reply == _OVER_BUDGET, "the member is told why the DM stopped"
    assert bot.turn_queue.in_flight() == 0, "the slot is returned"
