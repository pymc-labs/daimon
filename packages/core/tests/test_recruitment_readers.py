"""Enumerated non-recruitment readers: protected metadata stops content collection.

All variants run against the same restart-bound settings predicate. The empty-R
case tests the privacy decision alone: ordinary readers still perform their own
normal work, but privacy requires no SQL, HTTP, title or enrollment resolution.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import pytest
from daimon.core.config import RecruitmentSettings

READERS = (
    "gateway",
    "startup_replay",
    "turn_card_recovery",
    "mention_queue",
    "participation_admission",
    "participation_classifier",
    "classifier_direct",
    "continuation",
    "dm_me",
    "dm_transfer",
    "raw_thread_edit",
    "raw_thread_delete",
    "continuation_dispatch",
    "continuation_direct",
    "feedback",
    "mcp_search",
    "mcp_send_attachment",
    "mcp_create_thread",
    "mcp_rename_thread",
    "mcp_read_channel",
    "mcp_get_message",
    "mcp_snapshot_during_fetch",
    "mcp_permission_error_names",
    "mcp_read_thread",
    "mcp_list_threads",
    "mcp_list_channels",
)


def policy():
    return RecruitmentSettings(tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}})


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("in_thread", [False, True, "unknown_parent"])
async def test_enumerated_readers_refuse_parent_and_thread_before_content(
    reader, in_thread, monkeypatch
):
    from daimon.adapters.discord.bot import DaimonBot
    from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
    from daimon.adapters.discord.thread_participation import ThreadParticipant
    from daimon.adapters.mcp.tools.discord import _read
    from daimon.core.direct_messages import start_dm
    from fastmcp.exceptions import ToolError

    settings = policy()
    channel = MagicMock(spec=discord.Thread if in_thread else discord.TextChannel)
    channel.id, channel.name = (20 if in_thread else 2), "!labs-staffing: private-name"
    channel.parent_id = None if in_thread == "unknown_parent" else 2
    channel.guild.id = 101
    channel.history = MagicMock(side_effect=AssertionError("protected history"))
    channel.fetch_message = AsyncMock(side_effect=AssertionError("protected attachment/message"))
    message = MagicMock(spec=discord.Message)
    message.guild, message.channel = channel.guild, channel
    message.content = "private-candidate@example.test https://cdn.discord.com/private-cv.pdf"
    runtime = SimpleNamespace(
        settings=SimpleNamespace(recruitment=settings),
        sessionmaker=MagicMock(side_effect=AssertionError("privacy SQL")),
    )
    bot = SimpleNamespace(runtime=runtime, _allows_guild=lambda _: True)
    tenant = uuid4()
    if reader == "gateway":
        await DaimonBot.on_message(bot, message)
    elif reader == "startup_replay":
        from daimon.adapters.discord import bot as bot_module

        runtime.settings.discord = SimpleNamespace(
            startup_replay_minutes=1, startup_replay_channels_per_guild=1
        )
        runtime.sessionmaker = MagicMock()
        monkeypatch.setattr(
            bot_module, "replay_channels", AsyncMock(return_value=[(str(channel.id), "0")])
        )
        bot.guilds = [channel.guild]
        bot.get_channel = lambda _: channel
        await DaimonBot._replay_missed_messages(bot)
    elif reader == "turn_card_recovery":
        bot.wait_until_ready = AsyncMock()
        bot.get_channel = lambda _: channel
        bot._record_failed_card_recovery = AsyncMock()
        await DaimonBot._reconcile_turn_card_intent(
            bot, SimpleNamespace(thread_id=str(channel.id), id=uuid4())
        )
    elif reader == "mention_queue":
        await DaimonBot._handle_mention(bot, message, "101", tenant)
    elif reader == "participation_admission":
        await DaimonBot._maybe_participate(bot, message)
    elif reader == "participation_classifier":
        responder = SimpleNamespace(resolve=AsyncMock(), should_respond=AsyncMock())
        await DaimonBot._participation_fire(
            bot, channel, guild_id="101", tenant_id=tenant, responder=responder
        )
        responder.should_respond.assert_not_awaited()
        responder.resolve.assert_not_awaited()
    elif reader == "classifier_direct":
        gates = SimpleNamespace(should_respond=AsyncMock())
        participant = SimpleNamespace(_recruitment=settings, _gates=gates)
        assert not await ThreadParticipant.should_respond(
            participant, channel, [message], trigger=message, tenant_id=tenant, resolved=None
        )
        gates.should_respond.assert_not_awaited()
    elif reader == "continuation":
        with pytest.raises(PermissionError):
            await DaimonBot._run_continuation_turn_observed(
                bot,
                None,
                None,
                thread=channel,
                tenant_id=tenant,
                guild_id="101",
                surface=MagicMock(),
                origins=MagicMock(),
            )
    elif reader in {"continuation_dispatch", "continuation_direct"}:
        fn = (
            DaimonBot.dispatch_continuations_in_thread
            if reader == "continuation_dispatch"
            else DaimonBot._dispatch_continuations
        )
        await fn(bot, thread=channel, tenant_id=tenant, guild_id="101")
    elif reader == "feedback":
        from daimon.adapters.discord.feedback_reactions import FeedbackReactionCog

        bot.get_channel = lambda _: channel
        cog = FeedbackReactionCog(bot, recruitment=settings)
        await cog._record_vote_from_reaction(SimpleNamespace(guild_id=101, channel_id=channel.id))
    elif reader == "dm_me":
        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild, interaction.channel = channel.guild, channel
        interaction.guild_id = 101
        monkeypatch.setattr(
            "daimon.adapters.discord.checks.resolve_tenant_for_interaction",
            AsyncMock(return_value=tenant),
        )
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        cog = SimpleNamespace(bot=bot)
        await DirectMessageCog.dm.callback(cog, interaction, "move")
        text = interaction.followup.send.await_args.args[0]
        assert "private-name" not in text and "private-candidate" not in text
        channel.guild.fetch_member.assert_not_called()
    elif reader == "dm_transfer":
        with pytest.raises(ValueError, match="cannot be moved"):
            await start_dm(
                SimpleNamespace(recruitment=settings),
                None,
                tenant_id=tenant,
                platform="discord",
                workspace_id="101",
                route_key="dm",
                channel_id="dm",
                external_user_id="7",
                source_url="url",
                source_channel_id="2",
                source_thread_id="20" if in_thread else None,
                context=[],
            )
    elif reader.startswith("raw_thread"):
        bot._allows_guild = lambda _: True
        payload = SimpleNamespace(guild_id=101, thread_id=20, parent_id=2)
        fn = (
            DaimonBot.on_raw_thread_update
            if reader == "raw_thread_edit"
            else DaimonBot.on_raw_thread_delete
        )
        await fn(bot, payload)
    else:

        @asynccontextmanager
        async def client(token):
            yield SimpleNamespace()

        guild = SimpleNamespace(fetch_channels=AsyncMock(return_value=[channel]))
        monkeypatch.setattr(_read, "rest_client", client)
        monkeypatch.setattr(_read, "_resolve_member", AsyncMock(return_value=(guild, MagicMock())))
        monkeypatch.setattr(_read, "_resolve_channel", AsyncMock(return_value=channel))
        runtime.settings.discord = SimpleNamespace(
            bot_token=SimpleNamespace(get_secret_value=lambda: "fake")
        )
        auth = SimpleNamespace(platform_user_id="7", external_id="101")
        if reader in {"mcp_send_attachment", "mcp_create_thread", "mcp_rename_thread"}:
            from daimon.adapters.mcp.tools.discord import _send, _threads

            mod = _send if reader == "mcp_send_attachment" else _threads
            monkeypatch.setattr(mod, "rest_client", client)
            monkeypatch.setattr(
                mod, "_resolve_member", AsyncMock(return_value=(guild, MagicMock()))
            )
            monkeypatch.setattr(mod, "_resolve_channel", AsyncMock(return_value=channel))
            download = AsyncMock(side_effect=AssertionError("protected CV must not be downloaded"))
            monkeypatch.setattr(_send, "_build_files", download)
            with pytest.raises(ToolError, match="Source unavailable"):
                if reader == "mcp_send_attachment":
                    await _send._send_message_impl(
                        runtime,
                        auth,
                        channel_id=str(channel.id),
                        content="ordinary",
                        attachments=[
                            {"url": "https://cdn.discord.com/private-cv.pdf", "filename": "cv.pdf"}
                        ],
                    )
                elif reader == "mcp_create_thread":
                    await _threads._create_thread_impl(
                        runtime,
                        auth,
                        channel_id=str(channel.id),
                        name="ordinary",
                        content="ordinary",
                    )
                else:
                    await _threads._rename_thread_impl(
                        runtime, auth, thread_id=str(channel.id), name="ordinary"
                    )
            download.assert_not_awaited()
        elif reader == "mcp_search":
            from daimon.adapters.mcp.tools.discord import _search

            c = SimpleNamespace(
                http=SimpleNamespace(
                    request=AsyncMock(
                        return_value={
                            "total_results": 1,
                            "messages": [
                                [
                                    {
                                        "id": "40",
                                        "channel_id": str(channel.id),
                                        "author": {"id": "7", "username": "private-name"},
                                        "content": message.content,
                                        "timestamp": "2026-10-22T00:00:00+00:00",
                                        "type": 0,
                                    }
                                ]
                            ],
                            "threads": [
                                {
                                    "id": "20",
                                    "parent_id": "2",
                                    "type": 11,
                                    "name": "old indexed name",
                                }
                            ]
                            if in_thread
                            else [],
                        }
                    )
                )
            )

            @asynccontextmanager
            async def search_client(token):
                yield c

            parent = MagicMock(spec=discord.TextChannel)
            parent.id, parent.guild = 2, channel.guild
            guild.fetch_channels.return_value = [parent]
            monkeypatch.setattr(_search, "rest_client", search_client)
            monkeypatch.setattr(
                _search, "_resolve_member", AsyncMock(return_value=(guild, MagicMock()))
            )
            monkeypatch.setattr(_search, "_resolve_channel", AsyncMock(return_value=channel))
            result = await _search._search_messages_impl(runtime, auth, content="candidate")
            assert result.rows == []
            assert "private-name" not in result.model_dump_json()
            assert "private-candidate" not in result.model_dump_json()
        elif reader == "mcp_list_channels":
            assert await _read._list_channels_impl(runtime, auth) == []
        else:
            name = (
                "mcp_get_message"
                if reader in {"mcp_snapshot_during_fetch", "mcp_permission_error_names"}
                else reader
            )
            fn = getattr(_read, "_" + name.removeprefix("mcp_") + "_impl")
            kwargs = (
                {"thread_id": str(channel.id)}
                if reader == "mcp_read_thread"
                else {"channel_id": str(channel.id)}
            )
            if reader in {
                "mcp_get_message",
                "mcp_snapshot_during_fetch",
                "mcp_permission_error_names",
            }:
                kwargs["message_id"] = "40"
            with pytest.raises(ToolError, match="Source unavailable") as error:
                await fn(runtime, auth, **kwargs)
            assert "private-name" not in str(error.value)
    channel.history.assert_not_called()
    channel.fetch_message.assert_not_awaited()
    if reader != "startup_replay":
        runtime.sessionmaker.assert_not_called()


@pytest.mark.parametrize("reader", READERS)
def test_empty_R_privacy_is_pure_and_has_no_dependencies(reader):
    settings = RecruitmentSettings()
    assert not settings.is_recruitment_channel("101", "2", "")
    assert not settings.is_recruitment_channel("101", "20", "2")
    assert not settings.is_recruitment_channel("101", "20", None)


async def test_thread_listing_filters_actual_returned_parent_before_names(monkeypatch):
    from daimon.adapters.mcp.tools.discord import _read

    settings = policy()
    parent = MagicMock(spec=discord.TextChannel)
    parent.id, parent.guild.id = 3, 101
    private = MagicMock(spec=discord.Thread)
    private.id, private.parent_id, private.guild.id, private.name = 20, 2, 101, "private-name"
    guild = SimpleNamespace(active_threads=AsyncMock(return_value=[private]))

    async def archived(**kwargs):
        yield private

    parent.archived_threads = archived
    member = MagicMock()

    @asynccontextmanager
    async def client(token):
        yield SimpleNamespace()

    monkeypatch.setattr(_read, "rest_client", client)
    monkeypatch.setattr(_read, "_resolve_member", AsyncMock(return_value=(guild, member)))
    monkeypatch.setattr(_read, "_resolve_channel", AsyncMock(return_value=parent))
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            recruitment=settings,
            discord=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "fake")),
        )
    )
    auth = SimpleNamespace(platform_user_id="7", external_id="101")
    assert await _read._list_threads_impl(runtime, auth, channel_id="3") == []


async def test_actual_partial_messageable_refuses_before_gateway_or_naming():
    from daimon.adapters.discord.bot import DaimonBot
    from daimon.adapters.discord.recruitment_guard import protected

    channel = discord.PartialMessageable(state=MagicMock(), id=20, guild_id=101)
    settings = policy()
    assert protected(settings, channel, "101")
    assert not protected(RecruitmentSettings(), channel, "101")
    message = MagicMock(spec=discord.Message)
    message.channel, message.guild = channel, SimpleNamespace(id=101)
    bot = SimpleNamespace(
        runtime=SimpleNamespace(settings=SimpleNamespace(recruitment=settings)),
        _orchestrate=AsyncMock(side_effect=AssertionError("provider/naming")),
    )
    await DaimonBot._handle_mention(bot, message, "101", uuid4())
    bot._orchestrate.assert_not_awaited()


def test_empty_settings_never_inspect_unresolved_metadata():
    from daimon.adapters.discord.recruitment_guard import protected
    from daimon.adapters.mcp.tools.discord._recruitment import protected as mcp_protected

    class Unavailable:
        def __getattr__(self, name):
            raise AssertionError("disabled boundary inspected metadata")

    assert not protected(RecruitmentSettings(), Unavailable(), "101")
    runtime = SimpleNamespace(settings=SimpleNamespace(recruitment=RecruitmentSettings()))
    assert not mcp_protected(runtime, "101", Unavailable())
