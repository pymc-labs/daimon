"""Tests for GuardedView, ConfirmationView, and CancelView."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.views import (
    CancelView,
    ConfirmationView,
    GuardedView,
)


def _mock_interaction(*, user_id: int) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
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
            "Only the command invoker can use these buttons.",
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
            "Already handled.",
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


class TestConfirmationView:
    @pytest.mark.asyncio
    async def test_confirm_sets_state(self) -> None:
        view = ConfirmationView(allowed_user_id=123)
        interaction = _mock_interaction(user_id=123)
        btn = _find_button(view, "Confirm")
        await btn.callback(interaction)
        assert view.confirmed is True, "confirmed should be True"
        assert view._handled is True, "view should be finalized"
        interaction.response.edit_message.assert_called_once_with(view=view)

    @pytest.mark.asyncio
    async def test_cancel_sets_state(self) -> None:
        view = ConfirmationView(allowed_user_id=123)
        interaction = _mock_interaction(user_id=123)
        btn = _find_button(view, "Cancel")
        await btn.callback(interaction)
        assert view.confirmed is False, "confirmed should be False"
        assert view._handled is True, "view should be finalized"
        interaction.response.edit_message.assert_called_once_with(view=view)

    def test_timeout_is_60(self) -> None:
        view = ConfirmationView(allowed_user_id=123)
        assert view.timeout == 60.0, "confirmation timeout should be 60s"

    @pytest.mark.asyncio
    async def test_on_timeout_disables_buttons(self) -> None:
        view = ConfirmationView(allowed_user_id=123)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        await view.on_timeout()
        for child in view.children:
            assert getattr(child, "disabled", False) is True, "button should be disabled"
        view.message.edit.assert_called_once_with(view=view)


class TestCancelView:
    @pytest.mark.asyncio
    async def test_cancel_sets_event_and_finalizes(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        interaction = _mock_interaction(user_id=123)
        btn = _find_button(view, "Cancel")
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
        btn = _find_button(view, "Cancel")
        await btn.callback(interaction1)
        # Second press -- interaction_check rejects
        interaction2 = _mock_interaction(user_id=123)
        result = await view.interaction_check(interaction2)
        assert result is False, "second press should be rejected"
        interaction2.response.send_message.assert_called_once_with(
            "Already handled.",
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

    def test_timeout_is_none(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        assert view.timeout is None, "CancelView timeout should be None (lifecycle manages removal)"

    def test_button_style_is_grey(self) -> None:
        cancel = asyncio.Event()
        view = CancelView(allowed_user_id=123, cancel=cancel)
        btn = _find_button(view, "Cancel")
        assert btn.style == discord.ButtonStyle.grey, "cancel button should be grey"
