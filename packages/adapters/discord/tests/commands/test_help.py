"""Tests for HelpCog: flat /help slash with V2 LayoutView."""

from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from daimon.adapters.discord.commands.help import (
    HelpCog,
    build_help_view,
)


def _collect_text_display_content(view: discord.ui.LayoutView) -> list[str]:
    """Collect content from all TextDisplay components in the view tree."""
    return [
        child.content for child in view.walk_children() if isinstance(child, discord.ui.TextDisplay)
    ]


def _joined_content(view: discord.ui.LayoutView) -> str:
    """Join all TextDisplay content from the view into one string for searching."""
    return "\n".join(_collect_text_display_content(view))


class TestBuildHelpView:
    """V2 shape tests — pure, no interaction needed."""

    def test_build_help_view_returns_layout_view(self) -> None:
        view = build_help_view()
        assert isinstance(view, discord.ui.LayoutView), (
            "build_help_view() must return a discord.ui.LayoutView"
        )

    def test_header_text_display_starts_with_h2_commands(self) -> None:
        view = build_help_view()
        texts = _collect_text_display_content(view)
        assert any(t.startswith("## 📖 Commands") for t in texts), (
            "first TextDisplay must start with '## 📖 Commands'"
        )

    def test_header_contains_only_you_can_see_this_subtext(self) -> None:
        view = build_help_view()
        texts = _collect_text_display_content(view)
        assert any("-# only you can see this" in t for t in texts), (
            "header must include '-# only you can see this' subtext"
        )

    def test_joined_content_contains_billing_command(self) -> None:
        view = build_help_view()
        assert "-# /billing" in _joined_content(view), (
            "joined content must list /billing (was previously missing)"
        )

    def test_joined_content_contains_privacy_command(self) -> None:
        view = build_help_view()
        assert "-# /privacy" in _joined_content(view), (
            "joined content must list /privacy (was previously missing)"
        )

    def test_joined_content_contains_agent_setup_command(self) -> None:
        view = build_help_view()
        assert "-# /agent-setup" in _joined_content(view), "joined content must list /agent-setup"

    def test_joined_content_contains_routines_command(self) -> None:
        view = build_help_view()
        assert "-# /routines" in _joined_content(view), "joined content must list /routines"

    def test_joined_content_contains_help_command(self) -> None:
        view = build_help_view()
        assert "-# /help" in _joined_content(view), "joined content must list /help"

    def test_joined_content_contains_conversational_group(self) -> None:
        view = build_help_view()
        assert "💬 **Or just talk to your agent**" in _joined_content(view), (
            "view must include the conversational group '💬 **Or just talk to your agent**'"
        )

    def test_every_command_line_references_exactly_one_slash_command(self) -> None:
        """Each '-# /command' line must contain exactly one slash command token."""
        view = build_help_view()
        texts = _collect_text_display_content(view)
        for text in texts:
            for line in text.splitlines():
                if line.startswith("-# /"):
                    slash_commands = re.findall(r"/[a-z-]+", line)
                    assert len(slash_commands) == 1, (
                        f"command line must reference exactly one slash command, "
                        f"got {slash_commands!r} in line: {line!r}"
                    )

    def test_command_lines_match_dim_slash_dash_description_format(self) -> None:
        """Lines starting with '-# /' must match '-# /command — description' pattern."""
        view = build_help_view()
        texts = _collect_text_display_content(view)
        pattern = re.compile(r"^-# /[a-z-]+ — ")
        for text in texts:
            for line in text.splitlines():
                if line.startswith("-# /"):
                    assert pattern.match(line), (
                        f"command line must match '^-# /[a-z-]+ — ' pattern, got: {line!r}"
                    )

    def test_view_has_no_button_or_select_interactive_children(self) -> None:
        """No-controls card: no Button or Select components in the tree."""
        view = build_help_view()
        for child in view.walk_children():
            assert not isinstance(child, discord.ui.Button), (
                "help view must not contain any Button (no-controls card)"
            )
            assert not isinstance(child, discord.ui.Select), (  # type: ignore[misc]
                "help view must not contain any Select (no-controls card)"
            )

    def test_content_length_within_4000_char_budget(self) -> None:
        view = build_help_view()
        length = view.content_length()
        assert length <= 4000, (
            f"aggregate TextDisplay content must stay within 4000-char budget, got {length}"
        )


class TestHelpCog:
    def test_help_cog_is_not_a_group(self) -> None:
        """HelpCog is a flat Cog, not a GroupCog."""
        assert not issubclass(HelpCog, commands.GroupCog), (  # noqa: F821 — see import below
            "HelpCog must be a plain Cog (D-SHAPE-01); the one-subcommand group was deleted"
        )

    def test_help_cog_registers_flat_help_command(self) -> None:
        """The slash is named 'help', not 'help info'."""
        bot = MagicMock()
        cog = HelpCog(bot)
        command_names = [cmd.name for cmd in cog.walk_app_commands()]
        assert command_names == ["help"], (
            f"HelpCog must register exactly one flat /help slash; got {command_names!r}"
        )

    async def test_help_sends_ephemeral_layout_view(self) -> None:
        """The handler must send a single ephemeral V2 LayoutView via response.send_message."""
        interaction = MagicMock()
        interaction.guild_id = 123456
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.client.runtime = MagicMock()
        cog = HelpCog(MagicMock())

        with patch(
            "daimon.adapters.discord.checks.resolve_tenant_for_interaction",
            AsyncMock(return_value=uuid.uuid4()),
        ):
            await cog.help.callback(cog, interaction)  # type: ignore[arg-type]

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.call_args.kwargs
        assert kwargs.get("ephemeral") is True, "/help must be ephemeral"
        view = kwargs.get("view")
        assert isinstance(view, discord.ui.LayoutView), (
            "must send a LayoutView, not a classic embed"
        )
        assert "embed" not in kwargs or kwargs.get("embed") is None, (
            "must not send a classic embed alongside the LayoutView"
        )


# Late import to keep TestHelpCog.test_help_cog_is_not_a_group's subclass-check
# unambiguous — we need the discord.ext.commands module imported under the
# same name the production cog imports it as.
from discord.ext import commands  # noqa: E402
