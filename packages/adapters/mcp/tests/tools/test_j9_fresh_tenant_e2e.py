"""Ordinary agent operation and configuration paths, using offline providers."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from daimon.adapters.mcp.tools.agents import (
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,
)
from daimon.adapters.mcp.tools.task_continuity import _hand_off_task_impl
from daimon.core.channel_backend import backend_selection_enabled
from daimon.core.config import Settings, TurnSettings
from daimon.core.provider_keys import DEFAULT_KEY_ENV, default_enabled_backends

from .test_j2_gemini_root_e2e import fresh_chat as fresh_chat

#: Every flag an operator may set to stop a feature (the stop leg sets the turn ones).
FLAGS = (
    "DAIMON_TURN__CHANNEL_BACKENDS",
    "DAIMON_TURN__AGENT_BACKENDS",
)


@pytest.fixture
def no_flags(monkeypatch):
    """The code defaults: no flag set at all, as on a fresh install."""
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)


def _settings(**changes: object) -> Settings:
    return Settings(
        database={"url": "postgresql+asyncpg://fixture:fixture@localhost/fixture"},
        anthropic={"api_key": "test-fixture-key"},
        **cast("dict[str, Any]", changes),
    )


def test_j9_every_feature_default_is_on(no_flags):
    settings = _settings()
    assert settings.turn.channel_backends and settings.turn.agent_backends
    assert backend_selection_enabled(settings.turn), "switch tools are offered by default"


def test_j9_the_channel_backends_switch_stops_backend_selection(no_flags):
    """Turning channel backends off stops backend selection."""
    settings = _settings(turn=TurnSettings(channel_backends=False))
    assert not backend_selection_enabled(settings.turn)


def test_j9_a_new_tenant_is_on_anthropic_and_keyed_providers_have_code_default_rows(monkeypatch):
    """No rows: Anthropic only until an admin opts in; each keyed provider has code-default rows."""
    from daimon.core.turn.runtime_defaults import default_rows

    tenant = str(uuid.uuid4())
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "offline")
    for provider in ("openai", "gemini"):
        monkeypatch.delenv(DEFAULT_KEY_ENV[provider], raising=False)
    assert default_enabled_backends() == ("anthropic",)
    assert default_rows(tenant) == []
    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")
    assert default_enabled_backends() == ("anthropic",), "keys never enable a provider"
    rows = default_rows(tenant)
    assert {row["profile"] for row in rows} == {"gemini.inline_reuse"}
    assert all(row["tenant_id"] == tenant for row in rows)


@pytest.mark.parametrize("backend", ["openai", "gemini"])
async def test_j9_a_never_seen_tenant_switches_once_it_opted_in(fresh_chat, backend):
    """J1/J2's first step on a tenant created just now and opted in: the copy's switch applies."""
    copy = await _fork_agent_impl(
        fresh_chat.runtime,
        fresh_chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
    )
    await _hand_off_task_impl(
        fresh_chat.runtime,
        fresh_chat.auth,
        origin_context_id=str(fresh_chat.origin.id),
        agent_id=copy.id,
    )
    switched = await _request_agent_backend_switch_impl(
        fresh_chat.runtime,
        fresh_chat.auth,
        agent_name=copy.name,
        target_backend=backend,
        origin_context_id=str(fresh_chat.origin.id),
        expected_revision=0,
    )
    assert switched.status == "changed", "applied once the call carries the previewed revision"
    assert switched.target_backend == backend, switched
