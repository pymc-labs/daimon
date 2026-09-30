"""`/dm` refuses to open a DM from a channel whose spending budget is used up.

Admission and the DM store are patched on the command module; the budget
check reads a real `channel_budgets` row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.bot import CHANNEL_BUDGET_NOTICE
from daimon.adapters.discord.commands import direct_messages as dm_module
from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.channel_budgets import set_channel_budget
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = 700000101
_CHANNEL = 2


async def _no_history(*, limit: int) -> AsyncIterator[discord.Message]:
    for message in ():
        yield message


@pytest.mark.parametrize(("limit_usd", "refused"), [(Decimal("0"), True), (Decimal("5"), False)])
async def test_dm_from_a_channel_over_its_budget_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    limit_usd: Decimal,
    refused: bool,
) -> None:
    tenant = await provision_tenant(
        db_session_factory, platform="discord", workspace_id=str(_GUILD)
    )
    async with db_session_factory.begin() as session:
        await set_channel_budget(
            session,
            tenant_id=tenant.tenant_id,
            platform="discord",
            channel_id=str(_CHANNEL),
            limit_usd=limit_usd,
            window="monthly",
            starts_at=None,
            ends_at=None,
            set_by_account_id=None,
        )
    monkeypatch.setattr(dm_module, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(dm_module, "admit", AsyncMock(return_value=MagicMock()))
    start_dm = AsyncMock()
    monkeypatch.setattr(dm_module, "start_dm", start_dm)

    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    member = MagicMock()
    member.id = 999
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    member.create_dm = AsyncMock(return_value=MagicMock(id=5, send=AsyncMock()))
    guild = MagicMock(id=_GUILD, owner_id=1)
    guild.fetch_member = AsyncMock(return_value=member)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = _CHANNEL
    channel.history = _no_history
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.guild_id = _GUILD
    interaction.guild = guild
    interaction.channel = channel
    interaction.user.id = member.id
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    cog = DirectMessageCog(MagicMock(runtime=runtime))

    await cog.dm.callback(cog, interaction)  # pyright: ignore[reportArgumentType]

    (reply,), _ = interaction.followup.send.await_args
    if refused:
        assert reply == "Sorry, " + CHANNEL_BUDGET_NOTICE, "the member is told why"
        start_dm.assert_not_awaited()
        member.create_dm.assert_not_awaited()
    else:
        assert reply == "Ready in your DMs.", "a channel within its budget opens the DM"
        start_dm.assert_awaited_once()
        assert start_dm.await_args.kwargs["source_channel_id"] == str(_CHANNEL), (
            "the DM records the channel it was started from"
        )
