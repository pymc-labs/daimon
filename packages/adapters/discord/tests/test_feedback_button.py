"""Tests for FeedbackButton -- from_custom_id / callback / template disjointness."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.credential_button import CredentialRequestButton
from daimon.adapters.discord.feedback_button import FeedbackButton
from daimon.adapters.discord.feedback_modal import FeedbackModal
from daimon.core.credential_requests import build_custom_id as build_credential_custom_id
from daimon.core.credential_requests import mint_request_token
from daimon.core.message_feedback import build_custom_id


def _raising_sessionmaker() -> Any:
    def _raise(*_args: Any, **_kwargs: Any) -> None:
        msg = "from_custom_id must not touch the database"
        raise AssertionError(msg)

    return _raise


def _fake_bot(sessionmaker: Any) -> Any:
    """A minimal stand-in for DaimonBot -- only `.runtime.sessionmaker` is read."""
    return SimpleNamespace(runtime=SimpleNamespace(sessionmaker=sessionmaker))


def _interaction(*, client: Any) -> MagicMock:
    interaction = MagicMock()
    interaction.client = client
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup.send = AsyncMock()
    return interaction


def _match(feedback_id: str) -> Any:
    custom_id = build_custom_id(feedback_id)
    matched = FeedbackButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert matched is not None, "test feedback_id must satisfy the button's own custom_id template"
    return matched


# --- from_custom_id -----------------------------------------------------------


async def test_from_custom_id_returns_item_with_parsed_feedback_id() -> None:
    feedback_id = str(uuid.uuid4())

    item = await FeedbackButton.from_custom_id(
        _interaction(client=None), MagicMock(), _match(feedback_id)
    )

    assert item.feedback_id == feedback_id, (
        "the reconstructed item must carry the exact feedback_id encoded in the custom_id"
    )


async def test_from_custom_id_never_touches_the_database() -> None:
    """Pins the property that makes the button restart-safe with no lookup."""
    feedback_id = str(uuid.uuid4())
    bot = _fake_bot(_raising_sessionmaker())
    interaction = _interaction(client=bot)

    item = await FeedbackButton.from_custom_id(interaction, MagicMock(), _match(feedback_id))

    assert item.feedback_id == feedback_id, "reconstruction must succeed with no DB access"


# --- callback ------------------------------------------------------------------


async def test_callback_sends_modal_with_matching_feedback_id() -> None:
    feedback_id = str(uuid.uuid4())
    item = FeedbackButton(feedback_id=feedback_id)
    bot = _fake_bot(MagicMock())
    interaction = _interaction(client=bot)

    await item.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, FeedbackModal), "callback must open FeedbackModal"
    assert sent_modal._feedback_id == uuid.UUID(feedback_id), (  # pyright: ignore[reportPrivateUsage]
        "the modal must be constructed with the same feedback_id the button carries"
    )


async def test_callback_when_send_modal_raises_replies_once_and_swallows_the_error() -> None:
    item = FeedbackButton(feedback_id=str(uuid.uuid4()))
    bot = _fake_bot(MagicMock())
    interaction = _interaction(client=bot)
    interaction.response.send_modal = AsyncMock(side_effect=RuntimeError("boom"))

    await item.callback(interaction)  # must not raise -- dispatcher-swallowing boundary

    interaction.response.send_message.assert_awaited_once()
    message = interaction.response.send_message.call_args.args[0]
    assert "didn't work" in message.lower(), (
        "failure reply must tell the user opening the form failed"
    )
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True, "the failure reply must be ephemeral"


async def test_callback_when_the_interaction_was_already_responded_replies_via_followup() -> None:
    """The apology must not itself raise InteractionResponded inside the catch.

    An unconditional `response.send_message` here raises a second time and
    vanishes into discord.py's dispatcher, so the user gets neither the modal
    nor the apology -- exactly what the catch exists to prevent.
    """
    item = FeedbackButton(feedback_id=str(uuid.uuid4()))
    bot = _fake_bot(MagicMock())
    interaction = _interaction(client=bot)
    interaction.response.send_modal = AsyncMock(
        side_effect=discord.InteractionResponded(MagicMock())
    )
    interaction.response.is_done = MagicMock(return_value=True)

    await item.callback(interaction)  # must not raise

    interaction.response.send_message.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs.get("ephemeral") is True, "the failure reply must be ephemeral"


# --- template disjointness ------------------------------------------------------


def test_feedback_template_does_not_match_a_credential_custom_id() -> None:
    credential_custom_id = build_credential_custom_id(mint_request_token())
    assert (
        FeedbackButton.__discord_ui_compiled_template__.fullmatch(credential_custom_id) is None
    ), "overlapping templates double-dispatch a single click because discord.py fires every match"


def test_credential_template_does_not_match_a_feedback_custom_id() -> None:
    feedback_custom_id = build_custom_id(str(uuid.uuid4()))
    assert (
        CredentialRequestButton.__discord_ui_compiled_template__.fullmatch(feedback_custom_id)
        is None
    ), "overlapping templates double-dispatch a single click because discord.py fires every match"
