"""Ask a human on Discord: support requests and sealed-channel warnings."""

from __future__ import annotations

import uuid
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
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.admin_actions import DelegatedAction
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
    SupportSettings,
)
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores import accounts
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = "111"
_CHANNEL = "222"
_THREAD = "333"
_ESCALATION = "999"


async def _seed(
    session: AsyncSession, *, sealed: bool = False, agent: str | None = None
) -> uuid.UUID:
    """A guild, an admin and, with `agent`, a live session of that agent in `_THREAD`."""
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
    if agent is not None:
        await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=_THREAD,
            account_id=account.id,
            ma_session_id="sess_1",
            ma_agent_id=agent,
        )
    await session.commit()
    return tenant.id


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


def _settings(escalation_channel: str) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "support": SupportSettings(escalation_channel_id=escalation_channel),
        }
    )


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
    runtime.settings = _settings(escalation_channel)
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


async def _switch_off(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, agent: str
) -> None:
    settings = _settings(_ESCALATION)

    async def admin_for(_: DelegatedAction | None) -> bool:
        return True

    await change_feature(
        sessionmaker,
        settings,
        FeatureActor(tenant_id, None, "web", None, admin_for),
        key="support_escalation",
        tier="agent",
        value="off",
        place=FeaturePlace.of(tenant_id, "discord", None, agent_id=agent),
    )


async def test_the_form_is_refused_for_an_agent_set_to_off(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        tenant_id = await _seed(session, agent="agent_off")
    await _switch_off(db_session_factory, tenant_id, "agent_off")
    _bot, escalation, dm = await _submit(db_session_factory)
    escalation.send.assert_not_awaited()
    dm.send.assert_not_awaited()
    assert await _delivered(db_session_factory) == []


async def test_the_form_still_works_for_another_agent_in_the_workspace(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        tenant_id = await _seed(session, agent="agent_client")
    await _switch_off(db_session_factory, tenant_id, "agent_off")
    _bot, escalation, _dm = await _submit(db_session_factory)
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
