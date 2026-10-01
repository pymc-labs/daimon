"""/dm renders an admission refusal as copy, not as its reason code."""

from __future__ import annotations

from typing import get_args

import pytest
from daimon.adapters.discord.commands.direct_messages import (
    _dm_denial_message,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.turn.errors import AdmissionDenialReason, AdmissionDenied


@pytest.mark.parametrize("reason", get_args(AdmissionDenialReason))
def test_every_admission_refusal_has_person_facing_copy(reason: AdmissionDenialReason) -> None:
    message = _dm_denial_message(AdmissionDenied(reason=reason))
    assert reason not in message and message.endswith("."), message


def test_a_pinned_agent_says_why_it_cannot_move_to_a_dm() -> None:
    assert "pinned" in _dm_denial_message(AdmissionDenied(reason="agent_pinned_elsewhere"))
