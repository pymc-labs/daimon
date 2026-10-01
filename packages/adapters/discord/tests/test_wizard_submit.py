"""Tests for `WizardSubmitButton` -- the single-claim gate, the
acknowledge-then-collapse-then-spawn ordering, and reuse of `wizard.py`'s
shared requester-only authorization.

Driven directly against real seeded `wizard_session` rows (`make_wizard_session`),
following `test_wizard.py`'s interaction stand-in shape. The bot stand-in's
`_spawn` records the coroutine and closes it without running it -- what
`run_wizard_submit_turn` actually does is covered end-to-end by
`tests/integration/test_wizard_submit_turn.py`; this file only proves the
button dispatches (or doesn't) correctly.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.wizard_submit import WizardSubmitButton
from daimon.core.stores.domain import WizardSessionRow
from daimon.core.stores.wizard_session import get_wizard_session, update_wizard_state
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.core.wizard.state import build_custom_id
from daimon.testing.factories import make_wizard_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_REQUESTER_ID = "300000000000000001"
_OTHER_USER_ID = "400000000000000002"
_MESSAGE_ID = "900000000000000002"


def _one_step_spec() -> dict[str, Any]:
    """A single choice step -- current_step=1 is one past the last step
    index, i.e. the review screen (`daimon.core.wizard.render.to_screen`)."""
    return WizardSpec(
        prompt="Plan the picnic",
        steps=[
            Step(
                key="colour",
                question="Favourite colour?",
                kind=StepKind.CHOICE,
                options=[Option(label="Red", value="red"), Option(label="Blue", value="blue")],
            ),
        ],
    ).model_dump(mode="json")


def _fake_bot(
    sessionmaker: async_sessionmaker[AsyncSession],
    call_order: list[str],
    *,
    draining: bool = False,
) -> Any:
    """A minimal stand-in for DaimonBot -- `.runtime.sessionmaker` is real,
    `._spawn` records the coroutine (appending to `call_order`) and closes it
    without running it, so `run_wizard_submit_turn`'s own behaviour is never
    exercised here (see `tests/integration/test_wizard_submit_turn.py`)."""
    spawned: list[Any] = []

    def _spawn(coro: Any) -> None:
        call_order.append("spawn")
        spawned.append(coro)
        coro.close()

    bot = SimpleNamespace(
        runtime=SimpleNamespace(sessionmaker=sessionmaker),
        _spawn=_spawn,
        spawned=spawned,
        draining=draining,
    )
    return bot


def _interaction(
    *,
    user_id: str,
    client: Any,
    call_order: list[str] | None = None,
    message_id: str = _MESSAGE_ID,
) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = int(user_id)
    interaction.client = client
    interaction.message.id = int(message_id)
    interaction.response.send_message = AsyncMock()

    # is_done() tracks the deferral the way a real InteractionResponse does,
    # so `_reply_or_followup` picks the followup once the callback has
    # acknowledged -- which is the only route a post-defer reply can take.
    deferred: list[bool] = []

    def _defer(*_args: Any, **_kwargs: Any) -> None:
        deferred.append(True)
        if call_order is not None:
            call_order.append("defer")

    interaction.response.defer = AsyncMock(side_effect=_defer)
    interaction.response.is_done = MagicMock(side_effect=lambda: bool(deferred))
    interaction.edit_original_response = AsyncMock(
        side_effect=(lambda *a, **k: call_order.append("edit")) if call_order is not None else None
    )
    interaction.followup.send = AsyncMock()
    return interaction


def _submit_match(short_id: str) -> Any:
    custom_id = build_custom_id(short_id, "submit")
    matched = WizardSubmitButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert matched is not None, "test action must satisfy WizardSubmitButton's own template"
    return matched


async def _seed(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    current_step: int,
    status: str = "open",
    answers: dict[str, list[str]] | None = None,
    requester_platform_user_id: str = _REQUESTER_ID,
) -> WizardSessionRow:
    async with db_session_factory() as session, session.begin():
        return await make_wizard_session(
            session,
            requester_platform_user_id=requester_platform_user_id,
            message_id=_MESSAGE_ID,
            spec=_one_step_spec(),
            answers=answers if answers is not None else {"colour": ["red"]},
            current_step=current_step,
            status=status,
        )


def _container(view: discord.ui.LayoutView) -> Any:
    children = view.children
    assert len(children) == 1, "a wizard view must hold exactly one top-level item"
    return children[0]


# --- claim, collapse, spawn --------------------------------------------------


async def test_submit_at_review_claims_the_row_and_spawns_exactly_one_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, call_order=call_order)

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.status == "submitted", "a review-screen submit must claim the row"

    assert len(bot.spawned) == 1, "a winning claim must spawn exactly one background task"
    assert bot.spawned[0].__qualname__ == "run_wizard_submit_turn", (
        "the spawned coroutine must be run_wizard_submit_turn, never awaited inline"
    )
    interaction.edit_original_response.assert_awaited_once()


async def test_collapsed_view_has_no_interactive_components(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, call_order=call_order)

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    view = interaction.edit_original_response.call_args.kwargs["view"]
    container = _container(view)
    action_rows = [child for child in container.children if isinstance(child, discord.ui.ActionRow)]
    assert action_rows == [], (
        "a submitted form's collapsed screen must carry no interactive components -- "
        "it must never be re-drivable"
    )
    text_children = [
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    ]
    assert any("Submitted" in text for text in text_children), (
        "the collapsed screen must visibly say the form was submitted"
    )


async def test_a_tap_not_at_the_review_screen_re_renders_and_neither_claims_nor_spawns(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # current_step=0 is the first (and only) real step -- not the review
    # screen -- so apply(SUBMIT) refuses it (returns state unchanged).
    row = await _seed(db_session_factory, current_step=0)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, call_order=call_order)

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.status == "open", "a stale tap not at the review screen must not claim the row"
    assert after.current_step == 0

    assert bot.spawned == [], "a stale tap must never spawn a turn"
    interaction.edit_original_response.assert_awaited_once()


async def test_two_sequential_submits_the_second_claims_nothing_and_spawns_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Simulates a double-tap: both taps reconstruct their `WizardSubmitButton`
    from the row while it is still open (both `from_custom_id` calls land
    before either write), then are dispatched one after the other. The first
    callback wins the claim; the second's `try_claim_submit` call finds the
    row no longer `open` and returns None -- proving the claim gate itself
    (not just `interaction_check`) is what makes a double submit safe."""
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction_a = _interaction(user_id=_REQUESTER_ID, client=bot)
    interaction_b = _interaction(user_id=_REQUESTER_ID, client=bot)

    item_a = await WizardSubmitButton.from_custom_id(
        interaction_a, MagicMock(), _submit_match(row.id)
    )
    item_b = await WizardSubmitButton.from_custom_id(
        interaction_b, MagicMock(), _submit_match(row.id)
    )
    assert await item_a.interaction_check(interaction_a) is True
    assert await item_b.interaction_check(interaction_b) is True

    await item_a.callback(interaction_a)
    await item_b.callback(interaction_b)

    assert len(bot.spawned) == 1, "only the winning tap may spawn a background turn"
    interaction_a.edit_original_response.assert_awaited_once()
    interaction_b.edit_original_response.assert_awaited_once()

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.status == "submitted"


async def test_stale_submit_rerenders_a_concurrent_edit_without_spawning(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    async with db_session_factory() as session, session.begin():
        updated = await update_wizard_state(
            session,
            short_id=row.id,
            answers={"colour": ["blue"]},
            current_step=1,
            expected_updated_at=row.updated_at,
            now=row.updated_at + timedelta(seconds=1),
        )
    assert updated == 1, "the concurrent answer edit must commit before stale submit dispatch"

    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.status == "open", "the stale submit must not claim the edited row"
    assert after.answers == {"colour": ["blue"]}, "the stale submit must preserve current answers"
    assert bot.spawned == [], "a stale submit must not spawn a turn"
    view = interaction.edit_original_response.call_args.kwargs["view"]
    container = _container(view)
    action_rows = [child for child in container.children if isinstance(child, discord.ui.ActionRow)]
    assert action_rows, "the current open form must remain interactive after a stale submit"


async def test_a_submit_during_drain_claims_nothing_and_leaves_the_form_tappable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The claim is one-shot, so refusing before it (unlike the mention path,
    which has nothing to preserve) leaves a form the user can submit again
    once a fresh process picks it up."""
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order, draining=True)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, call_order=call_order)

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.status == "open", "a submit refused during drain must not consume the claim"

    assert bot.spawned == [], "no turn may start while the process is draining"
    interaction.edit_original_response.assert_not_awaited()
    message = interaction.followup.send.call_args.args[0]
    assert "restarting" in message, "the refusal must say the bot is restarting"


# --- authorization -------------------------------------------------------------


async def test_submit_by_a_non_requester_is_rejected_writes_nothing_spawns_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction = _interaction(user_id=_OTHER_USER_ID, client=bot, call_order=call_order)

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    allowed = await item.interaction_check(interaction)

    assert allowed is False, "a non-requester submit tap must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert "someone else" in message

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after == row, "a rejected tap must leave the row byte-identical -- no state change"
    assert bot.spawned == [], "a rejected tap must never spawn a turn"


async def test_submit_from_a_different_message_is_rejected_and_claims_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replaying the form's custom_id against a component on another message
    would bind and bill the turn in that message's channel while charging the
    tenant the form belongs to."""
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction = _interaction(
        user_id=_REQUESTER_ID, client=bot, call_order=call_order, message_id="800000000000000009"
    )

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    allowed = await item.interaction_check(interaction)

    assert allowed is False, "a submit that did not come from the form's own message is rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert "no longer available" in message

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after == row, "a rejected submit must leave the row byte-identical"
    assert bot.spawned == [], "a rejected submit must never spawn a turn"


# --- ordering --------------------------------------------------------------------


async def test_defer_precedes_the_spawn_which_precedes_the_collapse_edit(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, call_order=call_order)

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    assert call_order == ["defer", "spawn", "edit"], (
        "the acknowledgement must land first, and the claimed turn must be handed to the "
        "tracked background task before the cosmetic collapsed re-render -- nothing that "
        "can fail may sit between the irreversible claim and the hand-off"
    )


async def test_a_failing_collapse_edit_still_spawns_the_claimed_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The claim is irreversible: once it commits, the user can never
    resubmit, so a transient Discord failure on the purely cosmetic collapse
    re-render must not cost them the turn they already paid for."""
    row = await _seed(db_session_factory, current_step=1)
    call_order: list[str] = []
    bot = _fake_bot(db_session_factory, call_order)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, call_order=call_order)
    interaction.edit_original_response = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500, reason="Server Error"), "boom")
    )

    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.status == "submitted", "the claim still commits when the re-render fails"

    assert len(bot.spawned) == 1, (
        "a failed collapse edit must not prevent the claimed turn from being spawned"
    )
    interaction.followup.send.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()
