"""`/dm-me` and later DM turns answer to the source channel's budget.

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
from daimon.adapters.discord.theme import COLOR_RED
from daimon.core.authz import AgentRef
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.dm_access import DmAgent
from daimon.core.stores import web_audit
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.stores.direct_messages import dm_enabled, get_dm_agent_name
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_queue import TurnQueue
from daimon.testing.builders import provision_workspace
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = 700000101
_PARENT = 2
_THREAD = 3
_OVER_BUDGET = "This channel's budget is used up.\n\nAn admin can raise it."


@pytest.mark.parametrize(
    ("admin", "action", "expected"),
    [
        (False, "enable", "Only a platform admin can turn Daimon DMs on or off here."),
        (True, "enable", "Daimon DMs are on for this server."),
        (True, "disable", "Daimon DMs are off for this server."),
    ],
)
async def test_dm_policy_replies_use_server_words(
    monkeypatch: pytest.MonkeyPatch,
    admin: bool,
    action: str,
    expected: str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await provision_tenant(db_session_factory, platform="discord", workspace_id=str(_GUILD))
    save = AsyncMock()
    monkeypatch.setattr(dm_module, "set_dms", save)
    monkeypatch.setattr(dm_module, "is_member_daimon_admin", AsyncMock(return_value=True))
    member = MagicMock()
    member.guild_permissions.administrator = admin
    member.guild_permissions.manage_guild = admin
    guild = MagicMock()
    guild.id = _GUILD
    guild.owner_id = 1
    guild.fetch_member = AsyncMock(return_value=member)
    member.guild = guild
    member.id = 999
    member.roles = []
    interaction = MagicMock()
    interaction.guild = guild
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.user.id = 999
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    runtime = MagicMock(sessionmaker=db_session_factory)
    cog = DirectMessageCog(MagicMock(runtime=runtime))
    await cog.dm.callback.__wrapped__(cog, interaction, action)  # pyright: ignore[reportFunctionMemberAccess]
    sent = interaction.followup.send.await_args
    if admin:
        assert sent.kwargs["embed"].description == expected
    else:
        assert sent.args == () and sent.kwargs["embed"].description == expected
        assert sent.kwargs["embed"].color.value == COLOR_RED
    assert sent.kwargs["ephemeral"] is True
    assert save.await_count == (1 if admin else 0)


def _admin_interaction(runtime: MagicMock) -> MagicMock:
    member = MagicMock()
    member.id = 999
    member.roles = []
    guild = MagicMock()
    guild.id = _GUILD
    guild.owner_id = 999
    guild.fetch_member = AsyncMock(return_value=member)
    interaction = MagicMock()
    interaction.guild = guild
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.user.id = 999
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.parametrize("action", ["agent", "clear-agent"])
async def test_dm_me_agent_goes_through_the_default_agent_writer_and_is_audited(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    action: str,
) -> None:
    """`/dm-me agent` sets the default `daimon dm default` and the web app set, with an audit row
    naming the person who ran it, and refuses an agent the server does not have."""
    result = await provision_workspace(db_session_factory, str(_GUILD))
    monkeypatch.setattr(dm_module, "is_member_daimon_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(
        dm_module,
        "list_dm_agents",
        AsyncMock(return_value=[DmAgent("private", "ag_private", AgentRef.of("private"))]),
    )
    runtime = MagicMock(sessionmaker=db_session_factory)
    cog = DirectMessageCog(MagicMock(runtime=runtime))
    call = cog.dm.callback.__wrapped__  # pyright: ignore[reportFunctionMemberAccess]

    interaction = _admin_interaction(runtime)
    await call(cog, interaction, "agent", "nobody")
    refusal = interaction.followup.send.await_args.kwargs["embed"].description
    assert "nobody" in refusal, f"an unknown agent is refused, as elsewhere: {refusal}"
    async with db_session_factory() as session:
        assert await get_dm_agent_name(session, tenant_id=result.tenant_id) is None

    if action == "clear-agent":
        interaction = _admin_interaction(runtime)
        await call(cog, interaction, "agent", "private")
    interaction = _admin_interaction(runtime)
    await call(cog, interaction, action, "private" if action == "agent" else None)
    async with db_session_factory() as session:
        stored = await get_dm_agent_name(session, tenant_id=result.tenant_id)
        rows = await web_audit.list_recent(
            session, tenant_id=result.tenant_id, action="Changed DM default agent"
        )
    expected = "private" if action == "agent" else None
    assert stored == expected, "the default agent is the one value cold DMs and dm default read"
    assert rows[0].actor_account_id is not None, "the audit row names who ran the command"
    assert rows[0].detail == (expected or "None")


async def test_dm_me_enable_records_who_turned_dms_on(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    result = await provision_workspace(db_session_factory, str(_GUILD))
    monkeypatch.setattr(dm_module, "is_member_daimon_admin", AsyncMock(return_value=True))
    runtime = MagicMock(sessionmaker=db_session_factory)
    cog = DirectMessageCog(MagicMock(runtime=runtime))
    await cog.dm.callback.__wrapped__(  # pyright: ignore[reportFunctionMemberAccess]
        cog, _admin_interaction(runtime), "enable"
    )
    async with db_session_factory() as session:
        assert await dm_enabled(session, tenant_id=result.tenant_id), "DMs are on"
        changes = await list_changes(session, tenant_id=result.tenant_id, channel_id=WORKSPACE_ID)
    assert [(c.setting, c.surface) for c in changes] == [("direct_messages", "chat")], changes
    assert changes[0].actor_account_id is not None, "the change names who ran the command"


async def test_dm_from_somewhere_without_history_is_a_red_card() -> None:
    interaction = MagicMock()
    interaction.guild = None
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    cog = DirectMessageCog(MagicMock())
    await cog.dm.callback.__wrapped__(cog, interaction, "move")  # pyright: ignore[reportFunctionMemberAccess]
    sent = interaction.followup.send.await_args
    assert sent.kwargs["embed"].color.value == COLOR_RED
    assert sent.kwargs["embed"].description.startswith("You can't start a private conversation")


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
        return MagicMock(source_sealed=False, source_shared=False)

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
    if over_budget:
        reply = interaction.followup.send.await_args.kwargs["embed"].description
        assert reply == _OVER_BUDGET, "the member is told why"
        start_dm.assert_not_awaited()
        member.create_dm.assert_not_awaited()
    else:
        assert (
            interaction.followup.send.await_args.kwargs["embed"].description == "Ready in your DMs."
        ), "a channel within its budget opens the DM"
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
        "reply_to_dm_result",
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
    bot.runtime.turn_deps.sessionmaker = db_session_factory
    from daimon.core.membership import CachedMembershipResolver, Membership

    bot.runtime.turn_deps.membership_resolver = CachedMembershipResolver(
        AsyncMock(return_value=Membership(is_member=True))
    )
    bot.runtime.settings.direct_message_policies = {}
    bot.get_guild.return_value = guild
    bot.turn_queue = TurnQueue(
        platform="discord", global_cap=1, max_queued_per_tenant=1, max_queued=1, max_wait_s=60
    )
    message = MagicMock()
    message.add_reaction = message.remove_reaction = AsyncMock()
    message.author.bot = False
    message.author.id = 999
    message.channel = MagicMock(spec=discord.DMChannel)
    message.channel.id = 5
    message.channel.send = AsyncMock()
    message.content = "hello"
    cog = DirectMessageCog(bot)

    await cog.on_message(message)

    reply = message.channel.send.await_args.kwargs["embed"].description
    assert reply == _OVER_BUDGET, "the member is told why the DM stopped"
    assert bot.turn_queue.in_flight() == 0, "the slot is returned"


def test_command_is_registered_as_dm_me() -> None:
    assert DirectMessageCog.dm.name == "dm-me"


async def test_role_loss_while_dm_runs_prevents_private_result_delivery(
    db_session_factory, monkeypatch
):  # type: ignore[no-untyped-def]
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.membership import CachedMembershipResolver, Membership
    from daimon.core.stores.access_policy import set_access_policy

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id=str(_GUILD))
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
        await set_access_policy(
            session, tenant_id=tenant.id, policy=TenantAccessPolicy(invoker_role_ids=("123",))
        )
    from .test_cold_direct_messages import private_reply

    async def answer(deps, **kwargs):
        from daimon.core.stores.direct_messages import get_conversation

        async with db_session_factory() as session:
            row = await get_conversation(
                session, platform="discord", route_key="5", external_user_id="999"
            )
        return private_reply(row, "private secret")

    monkeypatch.setattr(dm_module, "reply_to_dm_result", AsyncMock(side_effect=answer))
    member = MagicMock()
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = False
    guild = MagicMock()
    guild.owner_id = 1
    guild.fetch_member = AsyncMock(return_value=member)
    bot = MagicMock()
    bot.draining = False
    bot.runtime.sessionmaker = db_session_factory
    bot.runtime.turn_deps.sessionmaker = db_session_factory
    bot.runtime.turn_deps.membership_resolver = CachedMembershipResolver(
        AsyncMock(
            side_effect=[Membership(is_member=True, role_ids=("123",)), Membership(is_member=True)]
        )
    )
    bot.runtime.settings.direct_message_policies = {}
    bot.get_guild.return_value = guild
    bot.turn_queue = TurnQueue(
        platform="discord", global_cap=1, max_queued_per_tenant=1, max_queued=1, max_wait_s=60
    )
    message = MagicMock()
    message.add_reaction = message.remove_reaction = AsyncMock()
    message.author.bot = False
    message.author.id = 999
    message.channel = MagicMock(spec=discord.DMChannel)
    message.channel.id = 5
    message.channel.send = AsyncMock()
    message.content = "hello"
    await DirectMessageCog(bot).on_message(message)
    sent = [call.args[0] for call in message.channel.send.await_args_list]
    assert not sent, "revoked access suppresses the private result without replying"
    dm_module.reply_to_dm_result.assert_awaited_once()
