"""Credential spies whitelist exactly the bool-only key-presence check."""

from __future__ import annotations

import os

import pytest
from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_present
from daimon.testing.key_presence import called_by_key_presence


def test_only_the_presence_check_is_whitelisted(monkeypatch: pytest.MonkeyPatch) -> None:
    env = DEFAULT_KEY_ENV["openai"]
    monkeypatch.setenv(env, "set")
    original = os.environ.get
    seen: list[bool] = []

    def spy(name: str, default: str | None = None) -> str | None:
        if name == env:
            seen.append(called_by_key_presence())
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", spy)
    assert provider_key_present("openai")
    os.environ.get(env)
    assert seen == [True, False], "a direct read is still caught"
