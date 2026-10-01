"""The chrome the three setup screens share: gating, swapping, Done, paging."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.agent_setup.navigation import (
    INVOKER_ONLY_MESSAGE,
    PanelViewBase,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.core.roster import paginate


class _Screen(PanelViewBase):
    """A minimal concrete screen — the base class is what is under test."""


def _state() -> PanelState:
    return PanelState(roster=[], selected=None, account_id=uuid.uuid4())


def _clicker(*, user_id: int = 42, responded: bool = False) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=responded)
    interaction.edit_original_response = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    return interaction


async def test_interaction_check_refuses_anyone_but_the_invoker() -> None:
    view = _Screen(_state(), runtime=MagicMock(), allowed_user_id=42)
    intruder = _clicker(user_id=7)

    assert await view.interaction_check(intruder) is False, "only the invoker may click"
    assert intruder.response.send_message.call_args.args[0] == INVOKER_ONLY_MESSAGE, (
        "the refusal names the rule"
    )


async def test_interaction_check_admits_the_invoker() -> None:
    view = _Screen(_state(), runtime=MagicMock(), allowed_user_id=42)

    assert await view.interaction_check(_clicker()) is True, "the invoker's clicks go through"


async def test_swap_to_edits_the_message_when_the_click_is_still_unacknowledged() -> None:
    state = _state()
    view = _Screen(state, runtime=MagicMock(), allowed_user_id=42)
    following = _Screen(state, runtime=MagicMock(), allowed_user_id=42)
    interaction = _clicker()

    await view.swap_to(interaction, following)

    interaction.response.edit_message.assert_awaited_once()
    interaction.edit_original_response.assert_not_awaited()
    kwargs = interaction.response.edit_message.call_args.kwargs
    assert kwargs["view"] is following, "the panel's one message now shows the new screen"
    assert kwargs["allowed_mentions"].users is False, (
        "attribution lines render mentions; the edit must not ping anybody"
    )


async def test_swap_to_edits_the_original_response_after_the_callback_deferred() -> None:
    state = _state()
    view = _Screen(state, runtime=MagicMock(), allowed_user_id=42)
    following = _Screen(state, runtime=MagicMock(), allowed_user_id=42)
    interaction = _clicker(responded=True)

    await view.swap_to(interaction, following)

    interaction.edit_original_response.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()


async def test_swap_to_makes_the_new_screen_the_one_that_owns_the_message() -> None:
    state = _state()
    view = _Screen(state, runtime=MagicMock(), allowed_user_id=42)
    view.bind_render_interaction(_clicker(), panel=state)
    following = _Screen(state, runtime=MagicMock(), allowed_user_id=42)
    interaction = _clicker()

    await view.swap_to(interaction, following)
    await view.on_timeout()

    interaction.edit_original_response.assert_not_awaited()
    assert state.render_seq == 2, (
        "the swap bumps the render generation so the replaced screen stops expiring the message"
    )


async def test_done_removes_the_panel_and_stops_the_view() -> None:
    view = _Screen(_state(), runtime=MagicMock(), allowed_user_id=42)
    button = view.done_button()
    interaction = _clicker()

    await button.callback(interaction)

    interaction.delete_original_response.assert_awaited_once()
    assert view.is_finished(), "Done stops the view instead of leaving a timer running"


def test_page_row_is_omitted_when_everything_fits_on_one_page() -> None:
    view = _Screen(_state(), runtime=MagicMock(), allowed_user_id=42)

    row = view.page_row(
        paginate(["only"], page=0, page_size=8), on_previous=AsyncMock(), on_next=AsyncMock()
    )

    assert row is None, "one page needs no pager at all"


def test_page_row_disables_the_direction_that_leads_nowhere() -> None:
    view = _Screen(_state(), runtime=MagicMock(), allowed_user_id=42)
    items = [str(index) for index in range(5)]

    first = view.page_row(
        paginate(items, page=0, page_size=2), on_previous=AsyncMock(), on_next=AsyncMock()
    )
    last = view.page_row(
        paginate(items, page=99, page_size=2), on_previous=AsyncMock(), on_next=AsyncMock()
    )

    assert first is not None and last is not None, "three pages need a pager"
    assert [
        button.disabled for button in first.children if isinstance(button, discord.ui.Button)
    ] == [
        True,
        False,
    ], "on the first page only Previous is dead"
    assert [
        button.disabled for button in last.children if isinstance(button, discord.ui.Button)
    ] == [
        False,
        True,
    ], "a stale page number clamps to the last page, where only Next is dead"
