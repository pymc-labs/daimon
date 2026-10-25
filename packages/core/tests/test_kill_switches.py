"""The turn flags are on by default and stay usable as emergency stops."""

from __future__ import annotations

import pytest
from daimon.core.config import TurnSettings, load_turn_settings

_FLAGS = (
    "DAIMON_TURN__PATH",
    "DAIMON_TURN__CHANNEL_BACKENDS",
    "DAIMON_TURN__AGENT_BACKENDS",
    "DAIMON_TURN__PROVIDER_RUNTIME_FILE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in _FLAGS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    ("name", "value", "field", "stopped"),
    [
        ("DAIMON_TURN__PATH", "legacy", "path", "legacy"),
        ("DAIMON_TURN__CHANNEL_BACKENDS", "false", "channel_backends", False),
        ("DAIMON_TURN__AGENT_BACKENDS", "false", "agent_backends", False),
    ],
)
def test_each_turn_flag_still_stops(
    clean_env: pytest.MonkeyPatch, name: str, value: str, field: str, stopped: object
) -> None:
    clean_env.setenv(name, value)
    assert getattr(load_turn_settings(_env_file=None), field) == stopped


def test_the_default_load_needs_no_runtime_manifest(clean_env: pytest.MonkeyPatch) -> None:
    """Defaults on validate a manifest only when one is configured; none is required."""
    assert load_turn_settings(_env_file=None) == TurnSettings()
