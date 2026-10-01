"""Tests for WizardNavButton / WizardSelect / WizardCustomTextModal --
authorization, expiry, defer-then-edit ordering, and state persistence.

Driven directly against real seeded `wizard_session` rows (`make_wizard_session`),
following `test_credential_button.py`'s interaction stand-in shape. Only the
interaction is a stand-in -- the store, the pure transition, and the renderer
all run for real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.wizard import (
    WizardCustomTextModal,
    WizardNavButton,
    WizardSelect,
)
from daimon.core.stores.domain import WizardSessionRow
from daimon.core.stores.wizard_session import (
    delete_wizard_sessions_for_platform_user,
    get_wizard_session,
    update_wizard_state,
)
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.core.wizard.state import build_custom_id
from daimon.testing.factories import make_wizard_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_REQUESTER_ID = "100000000000000001"
_OTHER_USER_ID = "200000000000000002"
_MESSAGE_ID = "900000000000000001"


def _two_step_spec() -> dict[str, Any]:
    """A choice step (allow_custom) followed by a multi step -- enough steps
    to exercise Next/Back/option/select/opener transitions distinctly."""
    return WizardSpec(
        prompt="Plan the picnic",
        steps=[
            Step(
                key="colour",
                question="Favourite colour?",
                kind=StepKind.CHOICE,
                options=[Option(label="Red", value="red"), Option(label="Blue", value="blue")],
            ),
            Step(
                key="toppings",
                question="Which toppings?",
                kind=StepKind.MULTI,
                options=[
                    Option(label="Cheese", value="cheese"),
                    Option(label="Olives", value="olives"),
                ],
            ),
        ],
    ).model_dump(mode="json")


def _fake_bot(sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    """A minimal stand-in for DaimonBot -- only `.runtime.sessionmaker` is read."""
    return SimpleNamespace(runtime=SimpleNamespace(sessionmaker=sessionmaker))


def _interaction(*, user_id: str, client: Any, message_id: str = _MESSAGE_ID) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = int(user_id)
    interaction.client = client
    interaction.message.id = int(message_id)
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _nav_match(short_id: str, action: str) -> Any:
    custom_id = build_custom_id(short_id, action)
    matched = WizardNavButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert matched is not None, "test action must satisfy WizardNavButton's own template"
    return matched


def _select_match(short_id: str, action: str) -> Any:
    custom_id = build_custom_id(short_id, action)
    matched = WizardSelect.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert matched is not None, "test action must satisfy WizardSelect's own template"
    return matched


async def _seed(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    current_step: int = 0,
    answers: dict[str, list[str]] | None = None,
    status: str = "open",
    requester_platform_user_id: str = _REQUESTER_ID,
    expires_at: datetime | None = None,
) -> WizardSessionRow:
    async with db_session_factory() as session, session.begin():
        return await make_wizard_session(
            session,
            requester_platform_user_id=requester_platform_user_id,
            message_id=_MESSAGE_ID,
            spec=_two_step_spec(),
            answers=answers,
            current_step=current_step,
            status=status,
            expires_at=expires_at,
        )


# --- interaction_check: authorization and lifecycle -------------------------


async def test_interaction_check_allows_the_requester(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "next")
    )
    allowed = await item.interaction_check(interaction)

    assert allowed is True, "the requester tapping an open, unexpired form must be allowed"
    interaction.response.send_message.assert_not_awaited()


async def test_interaction_check_rejects_a_different_user_with_no_state_change(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_OTHER_USER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "next")
    )
    allowed = await item.interaction_check(interaction)

    assert allowed is False, "a non-requester tap must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert "someone else" in message, "rejection must say the form was for someone else"

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after == row, "a rejected tap must leave the row byte-identical -- no state change"


async def test_interaction_check_rejects_a_tap_from_a_different_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A custom_id carries no state, so the requester replaying their own
    form's id against a component on another message -- in another channel or
    guild -- would otherwise bind and bill the turn wherever they replayed
    it."""
    row = await _seed(db_session_factory)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, message_id="800000000000000009")

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "next")
    )
    allowed = await item.interaction_check(interaction)

    assert allowed is False, "a tap that did not come from the form's own message must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert "no longer available" in message

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after == row, "a rejected tap must leave the row byte-identical -- no state change"


async def test_interaction_check_rejects_unknown_short_id(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    unknown_short_id = "zzzzzzz9"

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(unknown_short_id, "next")
    )
    allowed = await item.interaction_check(interaction)

    assert allowed is False, "an unknown short id must be rejected"
    message = interaction.response.send_message.call_args.args[0]
    assert "no longer available" in message


async def test_interaction_check_rejects_an_already_submitted_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, status="submitted", current_step=2)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "back")
    )
    allowed = await item.interaction_check(interaction)

    assert allowed is False, "a submitted row must reject further taps"
    message = interaction.response.send_message.call_args.args[0]
    assert "already submitted" in message


async def test_interaction_check_rejects_an_expired_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "next")
    )
    allowed = await item.interaction_check(interaction)

    assert allowed is False, (
        "an expired form must say so -- this replaces what would otherwise be "
        "a silent no-op on a stale form"
    )
    message = interaction.response.send_message.call_args.args[0]
    assert "expired" in message


# --- callback: transitions ----------------------------------------------------


async def test_next_tap_defers_before_any_write_or_render(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    call_order: list[str] = []
    interaction.response.defer = AsyncMock(side_effect=lambda *a, **k: call_order.append("defer"))
    interaction.edit_original_response = AsyncMock(
        side_effect=lambda *a, **k: call_order.append("edit")
    )

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "next")
    )
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    assert call_order == ["defer", "edit"], (
        "the deferral must land before the database write and the re-render"
    )


async def test_next_tap_advances_current_step_and_edits_with_a_new_view(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "next")
    )
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    interaction.edit_original_response.assert_awaited_once()
    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.current_step == 1, "Next must advance current_step in the database"


async def test_option_tap_on_choice_step_records_the_value_and_advances(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "s0_c0")
    )
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.answers["colour"] == ["red"], "the tapped option's value must be recorded"
    assert after.current_step == 1, "a choice tap must advance to the next step"


async def test_select_change_records_every_chosen_value_and_does_not_advance(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=1)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardSelect.from_custom_id(
        interaction, MagicMock(), _select_match(row.id, "s1_sel")
    )
    item.item._values = ["cheese", "olives"]  # pyright: ignore[reportPrivateUsage]  # simulates a real dispatch's _refresh_state without running the full ViewStore machinery
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.answers["toppings"] == ["cheese", "olives"], "every chosen value must be recorded"
    assert after.current_step == 1, "a multi-select change must not advance the step"


async def test_select_change_carrying_an_undeclared_value_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A modified client can put anything in a select's values -- the tap must
    be refused rather than recorded under the step's key."""
    row = await _seed(db_session_factory, current_step=1)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardSelect.from_custom_id(
        interaction, MagicMock(), _select_match(row.id, "s1_sel")
    )
    item.item._values = ["cheese", "anchovies"]  # pyright: ignore[reportPrivateUsage]  # simulates a real dispatch's _refresh_state without running the full ViewStore machinery
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after == row, "a forged select value must leave the row byte-identical"
    interaction.edit_original_response.assert_not_awaited()


async def test_back_tap_from_step_one_returns_to_step_zero(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=1)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "back")
    )
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.current_step == 0, "Back from step 1 must return to step 0"


async def test_tap_on_a_row_deleted_between_read_and_write_edits_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "next")
    )
    assert await item.interaction_check(interaction) is True

    async with db_session_factory() as session, session.begin():
        deleted = await delete_wizard_sessions_for_platform_user(
            session, platform_user_id=_REQUESTER_ID, tenant_id=row.tenant_id
        )
    assert deleted == 1, "the row must actually be gone before the write races it"

    await item.callback(interaction)

    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()


async def test_re_render_suppresses_every_mention(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Head text and the review summary interpolate agent-authored prompt and
    question text verbatim, so every re-render has to suppress mentions."""
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "next")
    )
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    allowed = interaction.edit_original_response.call_args.kwargs["allowed_mentions"]
    assert allowed.to_dict() == discord.AllowedMentions.none().to_dict(), (
        "a wizard re-render must ping nobody"
    )


async def test_second_tap_computed_from_the_same_base_row_is_refused_as_stale(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A double-click: both taps read the row before either writes. The
    second one's answers/step predate the first's write, so it must be
    refused rather than silently erasing what the first recorded."""
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    interaction_a = _interaction(user_id=_REQUESTER_ID, client=bot)
    interaction_b = _interaction(user_id=_REQUESTER_ID, client=bot)

    option_tap = await WizardNavButton.from_custom_id(
        interaction_a, MagicMock(), _nav_match(row.id, "s0_c0")
    )
    next_tap = await WizardNavButton.from_custom_id(
        interaction_b, MagicMock(), _nav_match(row.id, "next")
    )
    assert await option_tap.interaction_check(interaction_a) is True
    assert await next_tap.interaction_check(interaction_b) is True

    await option_tap.callback(interaction_a)
    await next_tap.callback(interaction_b)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.answers["colour"] == ["red"], (
        "the second tap must not erase the answer the first tap recorded"
    )
    interaction_b.edit_original_response.assert_not_awaited()
    stale_message = interaction_b.followup.send.call_args.args[0]
    assert "changed" in stale_message, "the losing tap must say the form changed under it"


async def test_opener_action_responds_with_a_modal_and_performs_no_write(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "s0_custom")
    )
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, WizardCustomTextModal), "an opener must open the text modal"
    interaction.response.defer.assert_not_awaited()

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after == row, "opening the modal must not write anything"


async def test_modal_submit_records_typed_text_and_re_renders(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    modal = WizardCustomTextModal(bot=bot, row=row, step_index=0, question="Favourite colour?")
    modal.answer_input._value = "chartreuse"  # pyright: ignore[reportPrivateUsage]  # simulates the user's typed text
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    await modal.on_submit(interaction)

    interaction.edit_original_response.assert_awaited_once()
    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.answers["colour"] == ["chartreuse"], "the typed text must be recorded"
    assert after.current_step == 1, "a choice step's custom text must advance to the next step"


async def test_whitespace_only_modal_text_writes_nothing_and_replies(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    modal = WizardCustomTextModal(bot=bot, row=row, step_index=0, question="Favourite colour?")
    modal.answer_input._value = "   "  # pyright: ignore[reportPrivateUsage]  # whitespace-only
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)

    await modal.on_submit(interaction)

    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    message = interaction.followup.send.call_args.args[0]
    assert "empty" in message.lower()

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after == row, "whitespace-only text must not change the row"


async def test_modal_submit_writes_from_a_re_read_row_not_its_open_time_snapshot(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The modal-open-to-submit window is user-paced and unbounded, so every
    tap that lands in it must survive the eventual typed answer."""
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)
    modal = WizardCustomTextModal(bot=bot, row=row, step_index=1, question="Which toppings?")
    modal.answer_input._value = "pineapple"  # pyright: ignore[reportPrivateUsage]  # simulates the user's typed text

    # While the modal sits open, an ordinary tap records the choice step's
    # answer and advances -- the snapshot the modal holds is now stale.
    tap_interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    tap = await WizardNavButton.from_custom_id(
        tap_interaction, MagicMock(), _nav_match(row.id, "s0_c0")
    )
    assert await tap.interaction_check(tap_interaction) is True
    await tap.callback(tap_interaction)

    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.answers["colour"] == ["red"], (
        "the modal's write must not erase the answer recorded while it was open"
    )
    assert after.answers["toppings"] == ["pineapple"], "the typed text must still be recorded"
    interaction.edit_original_response.assert_awaited_once()


# --- state durability ---------------------------------------------------------


async def test_reconstructing_from_custom_id_reflects_a_write_by_another_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """This is the restart-survival property: a fresh `from_custom_id` call
    carries no in-memory view, so it must read whatever the LAST session
    wrote -- exactly what lets Back/Next survive a bot restart."""
    row = await _seed(db_session_factory, current_step=0)
    bot = _fake_bot(db_session_factory)

    # Simulate a different process's earlier tap: advance the row directly
    # through the store, with no WizardNavButton instance involved.
    async with db_session_factory() as session, session.begin():
        rowcount = await update_wizard_state(
            session,
            short_id=row.id,
            answers=row.answers,
            current_step=1,
            expected_updated_at=row.updated_at,
            now=datetime.now(UTC),
        )
    assert rowcount == 1

    interaction = _interaction(user_id=_REQUESTER_ID, client=bot)
    item = await WizardNavButton.from_custom_id(
        interaction, MagicMock(), _nav_match(row.id, "back")
    )

    assert item.wizard_row is not None
    assert item.wizard_row.current_step == 1, (
        "a fresh from_custom_id call must read the current_step the OTHER "
        "session wrote, not any value cached from when this row was first seeded "
        "-- this is what lets Back/Next survive a bot restart (D-12)"
    )

    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.current_step == 0, "Back from the externally-written step 1 must reach step 0"
