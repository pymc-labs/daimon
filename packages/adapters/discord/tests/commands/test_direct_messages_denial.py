"""/dm renders an admission refusal as copy, not as its reason code."""

from __future__ import annotations

from typing import get_args
from unittest.mock import MagicMock

import pytest
from daimon.adapters.discord.commands.direct_messages import (
    _error_text,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.turn.errors import AdmissionDenialReason, AdmissionDenied


def _denial_text(reason: AdmissionDenialReason) -> str:
    return _error_text(AdmissionDenied(reason=reason), "fallback", settings=MagicMock())


@pytest.mark.parametrize("reason", get_args(AdmissionDenialReason))
def test_every_admission_refusal_has_person_facing_copy(reason: AdmissionDenialReason) -> None:
    message = _denial_text(reason)
    assert reason not in message and message.endswith("."), message


def test_a_pinned_agent_says_why_it_cannot_move_to_a_dm() -> None:
    message = _denial_text("agent_pinned_elsewhere")
    assert "pinned" in message and "DM" in message, message


def test_an_isolated_channel_says_why_it_cannot_move_to_a_dm() -> None:
    message = _denial_text("channel_isolated")
    assert "isolated" in message and "DM" in message, message


def test_a_dm_refusal_uses_the_server_nouns() -> None:
    message = _denial_text("invoker_not_allowed")
    assert "this server's list" in message and "A server admin" in message, message
