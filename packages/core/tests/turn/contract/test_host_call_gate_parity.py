"""Every mux profile has exactly one way to confirm a gated Daimon call."""

import pytest
from daimon.core.host_calls import gate_path
from mux.profiles import PROFILES


@pytest.mark.parametrize("profile_id", sorted(PROFILES))
def test_profile_without_native_confirmation_uses_press_applies(profile_id: str) -> None:
    """A provider that can't show its own card falls back to Daimon's card, never to none."""
    profile = PROFILES[profile_id]
    native = profile.support.get("tool_confirmation") == "native"
    path = gate_path(profile.provider)
    if native:
        assert path in ("native", "in_process"), f"{profile_id} confirms natively but got {path}"
    else:
        assert path == "press_applies", f"{profile_id} has no card of its own but got {path}"


def test_own_gates_are_the_managed_agents_and_selfhosted_loops() -> None:
    """MA keeps its native card; the selfhosted loop asks in process."""
    assert gate_path("anthropic") == "native", "managed agents confirm on their own card"
    assert gate_path("selfhosted") == "in_process", "the selfhosted loop asks in process"
    assert gate_path("unknown-provider") == "press_applies", "unknown providers fail to the card"
