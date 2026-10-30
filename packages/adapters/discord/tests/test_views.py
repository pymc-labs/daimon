"""Tests for GuardedView and CancelView."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import discord
import pytest
import structlog
from daimon.adapters.discord import views
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.views import (
    CancelView,
    GuardedView,
)


def test_module_docstring_names_both_persistent_button_exceptions() -> None:
    docstring = views.__doc__ or ""
    assert "CredentialRequestButton" in docstring, (
        "this docstring is the only place the view-persistence rule is written down, "
        "so a persistent component that does not appear here is an undocumented exception"
    )
    assert "FeedbackButton" in docstring, (
        "this docstring is the only place the view-persistence rule is written down, "
        "so a new persistent component that does not appear here is an undocumented exception"
    )


def _mock_interaction(*, user_id: int) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    return interaction


def _member_interaction(
    *,
    user_id: int,
    owner: bool = False,
    administrator: bool = False,
    manage_guild: bool = False,
) -> MagicMock:
    """A guild click by a member whose live permissions are as given."""
    interaction = _mock_interaction(user_id=user_id)
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild_permissions = discord.Permissions(
        administrator=administrator, manage_guild=manage_guild
    )
    member.guild = MagicMock(spec=discord.Guild)
    member.guild.owner_id = user_id if owner else 999
    interaction.user = member
    return interaction


def _find_button(view: discord.ui.View, label: str) -> discord.ui.Button[GuardedView]:
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child  # type: ignore[return-value]  # generic Button
    msg = f"No button with label {label!r}"
    raise AssertionError(msg)


class TestGuardedView:
    @pytest.mark.asyncio
    async def test_allows_correct_user(self) -> None:
        view = GuardedView(allowed_user_id=123, timeout=60.0)
        interaction = _mock_interaction(user_id=123)
        result = await view.interaction_check(interaction)
        assert result is True, "should allow authorized user"

    @pytest.mark.asyncio
    async def test_rejects_wrong_user(self) -> None:
        view = GuardedView(allowed_user_id=123, timeout=60.0)
        interaction = _mock_interaction(user_id=456)
        result = await view.interaction_check(interaction)
        assert result is False, "should reject non-authorized user"
        interaction.response.send_message.assert_called_once_with(
            embed=build_notice_embed("Only the command invoker can use these buttons."),
            ephemeral=True,
        )

    @pytest.mark.asyncio
    async def test_rejects_double_click(self) -> None:
        view = GuardedView(allowed_user_id=123, timeout=60.0)
        view._handled = True
        interaction = _mock_interaction(user_id=123)
        result = await view.interaction_check(interaction)
        assert result is False, "should reject already-handled interaction"
        interaction.response.send_message.assert_called_once_with(
            embed=build_notice_embed("Already handled."),
            ephemeral=True,
        )

    def test_finalize_disables_children_and_sets_handled(self) -> None:
        view = GuardedView(allowed_user_id=123, timeout=60.0)
        view.add_item(discord.ui.Button(label="X"))
        view._finalize()
        assert view._handled is True, "_handled should be set"
        for child in view.children:
            assert getattr(child, "disabled", False) is True, "button should be disabled"

    @pytest.mark.asyncio
    async def test_on_timeout_disables_children_and_edits(self) -> None:
        view = GuardedView(allowed_user_id=123, timeout=60.0)
        view.add_item(discord.ui.Button(label="X"))
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        await view.on_timeout()
        for child in view.children:
            assert getattr(child, "disabled", False) is True, "button should be disabled"
        view.message.edit.assert_called_once_with(view=view)

    @pytest.mark.asyncio
    async def test_on_timeout_no_message_does_not_raise(self) -> None:
        view = GuardedView(allowed_user_id=123, timeout=60.0)
        view.add_item(discord.ui.Button(label="X"))
        await view.on_timeout()  # should not raise
        for child in view.children:
            assert getattr(child, "disabled", False) is True, "button should be disabled"


class TestCancelView:
    @pytest.mark.asyncio
    async def test_stale_stop_only_cancels_its_original_turn(self) -> None:
        """A dead turn's leftover card must not stop the next turn in its thread."""
        old_cancel = asyncio.Event()
        new_cancel = asyncio.Event()
        old_turn_id = UUID("12345678-1234-5678-1234-567812345678")
        new_turn_id = UUID("87654321-4321-8765-4321-876543218765")
        old_view = CancelView(allowed_user_id=123, cancel=old_cancel, turn_id=old_turn_id)
        new_view = CancelView(allowed_user_id=123, cancel=new_cancel, turn_id=new_turn_id)

        assert _find_button(old_view, "Stop").custom_id != _find_button(new_view, "Stop").custom_id
        await _find_button(old_view, "Stop").callback(_mock_interaction(user_id=123))

        assert old_cancel.is_set()
        assert not new_cancel.is_set(), "a stale Stop must not affect the newer turn"

    @pytest.mark.asyncio
    async def test_cancel_sets_event_and_finalizes(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        interaction = _mock_interaction(user_id=123)
        btn = _find_button(view, "Stop")
        await btn.callback(interaction)
        assert cancel.is_set(), "cancel event should be set"
        assert view._handled is True, "view should be finalized"
        interaction.response.edit_message.assert_called_once_with(view=view)

    @pytest.mark.asyncio
    async def test_double_cancel_is_harmless(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        # First press
        interaction1 = _mock_interaction(user_id=123)
        btn = _find_button(view, "Stop")
        await btn.callback(interaction1)
        # Second press -- interaction_check rejects
        interaction2 = _mock_interaction(user_id=123)
        result = await view.interaction_check(interaction2)
        assert result is False, "second press should be rejected"
        interaction2.response.send_message.assert_called_once_with(
            embed=build_notice_embed("Already handled."),
            ephemeral=True,
        )

    @pytest.mark.asyncio
    async def test_wrong_user_rejected(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        interaction = _mock_interaction(user_id=456)
        result = await view.interaction_check(interaction)
        assert result is False, "wrong user should be rejected"
        assert not cancel.is_set(), "cancel event should NOT be set"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("owner", "administrator", "manage_guild"),
        [(True, False, False), (False, True, False), (False, False, True)],
    )
    async def test_a_current_server_admin_can_stop_anyones_reply(
        self, owner: bool, administrator: bool, manage_guild: bool
    ) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        interaction = _member_interaction(
            user_id=456, owner=owner, administrator=administrator, manage_guild=manage_guild
        )
        assert await view.interaction_check(interaction) is True
        with structlog.testing.capture_logs() as logs:
            await _find_button(view, "Stop").callback(interaction)
        assert cancel.is_set(), "an owner, Administrator or Manage Server member stops it"
        assert [entry for entry in logs if entry["event"] == "turn.stop_pressed"] == [
            {
                "event": "turn.stop_pressed",
                "log_level": "info",
                "platform": "discord",
                "turn_id": None,
                "requester_id": "123",
                "pressed_by": "456",
                "by_admin": True,
            }
        ], "the stop is recorded with who pressed it"
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_another_member_is_told_who_can_stop_it(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        interaction = _member_interaction(user_id=456)
        assert await view.interaction_check(interaction) is False
        interaction.response.send_message.assert_called_once_with(
            embed=build_notice_embed(
                "Only the person who asked, or someone with Manage Server, can stop this reply."
            ),
            ephemeral=True,
        )
        assert not cancel.is_set()

    @pytest.mark.asyncio
    async def test_an_admin_click_after_the_stop_is_already_handled(self) -> None:
        view = CancelView(allowed_user_id=123, cancel=asyncio.Event())
        await _find_button(view, "Stop").callback(_mock_interaction(user_id=123))
        interaction = _member_interaction(user_id=456, administrator=True)
        assert await view.interaction_check(interaction) is False
        interaction.response.send_message.assert_called_once_with(
            embed=build_notice_embed("Already handled."), ephemeral=True
        )

    def test_timeout_is_none(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        assert view.timeout is None, "CancelView timeout should be None (lifecycle manages removal)"

    def test_button_style_is_grey(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        btn = _find_button(view, "Stop")
        assert btn.style == discord.ButtonStyle.grey, "cancel button should be grey"

    @pytest.mark.asyncio
    async def test_turn_id_custom_id_keeps_the_existing_cancel_handler(self) -> None:
        turn_id = UUID("12345678-1234-5678-1234-567812345678")
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel, turn_id=turn_id)
        button = _find_button(view, "Stop")
        interaction = _mock_interaction(user_id=123)

        assert button.custom_id == f"daimon:cancel:{turn_id}", (
            "the durable turn ID should be recoverable from the posted component"
        )
        await button.callback(interaction)

        assert cancel.is_set(), "adding a custom ID must preserve cancellation behavior"
        assert view._handled is True, "the existing handler should still finalize the view"
        interaction.response.edit_message.assert_called_once_with(view=view)


@pytest.mark.parametrize("code", [10008, 10015])
async def test_cancel_on_deleted_card_only_ignores_unknown_message(code):
    cancel = asyncio.Event()
    view = CancelView(allowed_user_id=123, cancel=cancel)
    interaction = _mock_interaction(user_id=123)
    interaction.response.edit_message.side_effect = discord.NotFound(
        MagicMock(status=404, reason="Not Found"), {"code": code, "message": "Gone"}
    )
    button = _find_button(view, "Stop")
    if code == 10008:
        await button.callback(interaction)
    else:
        with pytest.raises(discord.NotFound):
            await button.callback(interaction)
    assert cancel.is_set()
    assert view._handled
    interaction.response.send_message.assert_not_awaited()
