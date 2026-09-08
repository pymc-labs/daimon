"""Byte-identical-when-unset tests for ``DiscordSettings.bot_display_name`` (SPEC req 9).

Every render function covered here must be BYTE-IDENTICAL to today's output when
``bot_display_name`` is left unset (default "daimon"), and must change to reflect
the configured name when it is set (e.g. "daimon-staging"). This is the safety
net for the in-scope render-site sweep — anything not covered here is presumed
out of scope per the plan's site list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.discord.bot import (
    _build_welcome_embed,  # pyright: ignore[reportPrivateUsage]
    _credit_depleted_message,  # pyright: ignore[reportPrivateUsage]
    _setting_up_message,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.discord.commands.help import build_help_view
from daimon.adapters.discord.commands.privacy import PrivacyCog
from daimon.adapters.discord.context import build_context_xml
from daimon.adapters.discord.privacy_panel.embeds import build_post_delete_container
from daimon.adapters.discord.privacy_panel.panel import (
    _export_placeholder_message,  # pyright: ignore[reportPrivateUsage]
    build_privacy_main_container,
)
from daimon.adapters.discord.privacy_panel.state import PurgePreview, PurgePreviewRow
from daimon.core.config import DiscordSettings
from daimon.core.purge import AccountPurgeResult, PurgeReport
from pydantic import SecretStr


class _AsyncIter:
    """Async iterator adapter for a mocked ``thread.history()``."""

    def __init__(self, items: list[discord.Message]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> discord.Message:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


def _make_message(*, msg_id: int, content: str) -> discord.Message:
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.content = content
    msg.author = MagicMock()
    msg.author.display_name = "Alice"
    msg.author.id = 100
    msg.author.bot = False
    msg.created_at = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    msg.attachments = []
    return msg


def _make_thread(messages: list[discord.Message]) -> discord.Thread:
    thread = MagicMock(spec=discord.Thread)
    thread.history = MagicMock(return_value=_AsyncIter(messages))
    thread.id = 900
    thread.parent_id = 800
    thread.starter_message = None
    thread.parent = None
    return thread


def _make_preview() -> PurgePreview:
    zero = PurgePreviewRow(count=0, example=None)
    return PurgePreview(
        linked_principals=PurgePreviewRow(count=1, example="Discord:1234567890"),
        principal_links=zero,
        routines=zero,
        user_configs=zero,
        account=PurgePreviewRow(count=1, example=None),
        user_skills=zero,
        github_credentials=zero,
        github_oauth_states=zero,
        mcp_tokens=zero,
        agent_github_binding=zero,
        slack_user_tokens=zero,
        slack_turn_contexts=zero,
        credential_requests=zero,
        wizard_sessions=zero,
        message_feedback=zero,
        support_escalations=zero,
    )


def _collect_text(
    view_or_container: discord.ui.LayoutView | discord.ui.Container[discord.ui.LayoutView],
) -> str:
    return "\n".join(
        child.content
        for child in view_or_container.walk_children()
        if isinstance(child, discord.ui.TextDisplay)
    )


class TestDefaultBotDisplayName:
    def test_default_is_daimon(self) -> None:
        settings = DiscordSettings(bot_token=SecretStr("test-bot-token"))
        assert settings.bot_display_name == "daimon", "default bot_display_name must be 'daimon'"


class TestMentionReplacement:
    @pytest.mark.asyncio
    async def test_unset_renders_at_daimon(self) -> None:
        trigger = _make_message(msg_id=10, content="<@999> help me")
        thread = _make_thread([trigger])
        result, _ = await build_context_xml(thread, trigger, bot_user_id=999)
        assert "@daimon" in result, (
            "unset bot_display_name must render '@daimon' (today's behavior)"
        )

    @pytest.mark.asyncio
    async def test_set_name_changes_mention(self) -> None:
        trigger = _make_message(msg_id=10, content="<@999> help me")
        thread = _make_thread([trigger])
        result, _ = await build_context_xml(
            thread, trigger, bot_user_id=999, bot_display_name="daimon-staging"
        )
        assert "<@999>" not in result, "raw bot mention should be replaced"
        assert "@daimon-staging" in result, "set bot_display_name must change the @mention"


class TestSettingUpMessage:
    def test_unset_matches_todays_text(self) -> None:
        assert _setting_up_message("daimon") == (
            "Daimon is setting up this server — try again in a moment."
        ), "unset bot_display_name must render byte-identical setting-up text"

    def test_set_name_changes_text(self) -> None:
        assert _setting_up_message("daimon-staging") == (
            "Daimon-staging is setting up this server — try again in a moment."
        )


class TestWelcomeEmbedOnceReadyField:
    def test_unset_matches_todays_text(self) -> None:
        embed = _build_welcome_embed("daimon")
        field = embed.fields[0]
        assert field.value == (
            "Mention `@daimon` anywhere to chat, or run `/agent-setup` to manage your agents."
        ), "unset bot_display_name must render byte-identical welcome copy"

    def test_set_name_changes_text(self) -> None:
        embed = _build_welcome_embed("daimon-staging")
        field = embed.fields[0]
        assert field.value == (
            "Mention `@daimon-staging` anywhere to chat, or run `/agent-setup` to manage your agents."
        )


class TestCreditDepletedMessage:
    def test_unset_matches_todays_text(self) -> None:
        assert _credit_depleted_message("daimon") == (
            "This server's daimon credit is depleted. An admin can top up with `/billing`."
        )

    def test_set_name_changes_text(self) -> None:
        assert _credit_depleted_message("daimon-staging") == (
            "This server's daimon-staging credit is depleted. An admin can top up with `/billing`."
        )


class TestHelpViewConversationalExamples:
    def test_unset_matches_todays_text(self) -> None:
        texts = _collect_text(build_help_view("daimon"))
        assert "See, export, or delete what daimon stores about you" in texts
        assert "@daimon help me set up" in texts
        assert "@daimon make a routine that runs daily" in texts

    def test_set_name_changes_text(self) -> None:
        texts = _collect_text(build_help_view("daimon-staging"))
        assert "See, export, or delete what daimon-staging stores about you" in texts
        assert "@daimon-staging help me set up" in texts
        assert "@daimon-staging make a routine that runs daily" in texts


class TestPrivacyCommandDescription:
    def test_unset_matches_todays_text(self) -> None:
        bot = MagicMock()
        bot.runtime.settings.discord = DiscordSettings(bot_token=SecretStr("test-bot-token"))
        cog = PrivacyCog(bot)
        assert cog.privacy.description == "See, export, or delete what daimon stores about you"

    def test_set_name_changes_text(self) -> None:
        bot = MagicMock()
        bot.runtime.settings.discord = DiscordSettings(
            bot_token=SecretStr("test-bot-token"), bot_display_name="daimon-staging"
        )
        cog = PrivacyCog(bot)
        assert (
            cog.privacy.description == "See, export, or delete what daimon-staging stores about you"
        )


class TestPrivacyMainContainer:
    def test_unset_matches_todays_text(self) -> None:
        texts = _collect_text(build_privacy_main_container(_make_preview(), user_name="Alice"))
        assert "daimon holds" in texts

    def test_set_name_changes_text(self) -> None:
        texts = _collect_text(
            build_privacy_main_container(
                _make_preview(), user_name="Alice", bot_display_name="daimon-staging"
            )
        )
        assert "daimon-staging holds" in texts


class TestExportPlaceholderMessage:
    def test_unset_matches_todays_text(self) -> None:
        assert _export_placeholder_message("daimon") == (
            "📤 **Export** is not yet implemented.\n\n"
            "When ready, this will produce a JSON dump of every daimon-side row "
            "tied to your identity and either attach it here or DM you a 7-day "
            "signed URL."
        )

    def test_set_name_changes_text(self) -> None:
        result = _export_placeholder_message("daimon-staging")
        assert "every daimon-staging-side row" in result


class TestPostDeleteContainer:
    def test_unset_matches_todays_text(self) -> None:
        result = AccountPurgeResult(db=PurgeReport(accounts=1))
        texts = _collect_text(build_post_delete_container(result))
        assert "Your daimon data has been deleted." in texts

    def test_set_name_changes_text(self) -> None:
        result = AccountPurgeResult(db=PurgeReport(accounts=1))
        texts = _collect_text(
            build_post_delete_container(result, bot_display_name="daimon-staging")
        )
        assert "Your daimon-staging data has been deleted." in texts
