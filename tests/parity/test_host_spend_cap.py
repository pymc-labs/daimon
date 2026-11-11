"""Executable record: a profile with no native session spend control gets the host cap.

A row's `spend_limit_usd_cents` is a per-turn limit. Anthropic enforces it natively;
OpenAI answers `spend_control` with HTTP 400, so the host stops the turn
(`daimon.core.turn.host_spend_cap`). A profile that does neither must be listed
here with the control that bounds it instead, so a new profile cannot ship
without an answer.
"""

from __future__ import annotations

import inspect

import daimon.core.turn.openai_deployment as openai_deployment
import pytest
from mux.profiles import PROFILES

_HOST_CAPPED = {"openai": openai_deployment}
# Without a per-turn cap: the daily model cap (checked as each turn starts) still holds.
_DAILY_CAP_ONLY = {"gemini", "selfhosted"}


@pytest.mark.parametrize("profile_id", sorted(PROFILES))
def test_every_profile_has_a_per_turn_cap_or_a_named_reason(profile_id: str) -> None:
    profile = PROFILES[profile_id]
    if profile.support_for("session_spend_control") == "native":
        return
    if profile.provider in _HOST_CAPPED:
        source = inspect.getsource(_HOST_CAPPED[profile.provider])
        assert "SpendGuard(" in source
        return
    assert profile.provider in _DAILY_CAP_ONLY
