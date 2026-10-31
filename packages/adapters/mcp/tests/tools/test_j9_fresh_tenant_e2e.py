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
    "DAIMON_TURN__PATH",
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
    assert settings.turn.path == "mux"
    assert settings.turn.channel_backends and settings.turn.agent_backends
    assert backend_selection_enabled(settings.turn), "switch tools are offered by default"


@pytest.mark.parametrize(
    ("flag", "stopped"),
    [
        ("path", {"turn": TurnSettings(path="legacy")}),
        ("channel_backends", {"turn": TurnSettings(channel_backends=False)}),
    ],
)
def test_j9_a_turn_flag_only_stops_its_own_feature(no_flags, flag, stopped):
    """Turning one switch off stops backend selection."""
    settings = _settings(**stopped)
    assert not backend_selection_enabled(settings.turn)


def test_j9_a_new_tenant_has_every_keyed_provider_and_its_code_default_rows(monkeypatch):
    """No rows: Anthropic plus each keyed provider, each with its code-default rows."""
    from daimon.core.turn.runtime_defaults import default_rows

    tenant = str(uuid.uuid4())
    for provider in ("openai", "gemini"):
        monkeypatch.delenv(DEFAULT_KEY_ENV[provider], raising=False)
    assert default_enabled_backends() == ("anthropic",)
    assert default_rows(tenant) == []
    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")
    assert set(default_enabled_backends()) == {"anthropic", "openai", "gemini"}
    rows = default_rows(tenant)
    assert {row["profile"] for row in rows} == {"gemini.inline_reuse"}
    assert all(row["tenant_id"] == tenant for row in rows)


@pytest.mark.parametrize("backend", ["openai", "gemini"])
async def test_j9_a_never_seen_tenant_previews_a_switch_with_zero_setup(fresh_chat, backend):
    """J1/J2's first step on a tenant created just now: the copy's switch previews."""
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
    preview = await _request_agent_backend_switch_impl(
        fresh_chat.runtime,
        fresh_chat.auth,
        agent_name=copy.name,
        target_backend=backend,
        origin_context_id=str(fresh_chat.origin.id),
    )
    assert preview.status == "preview" and preview.preview_digest
    assert preview.target_backend == backend


@pytest.mark.xfail(
    strict=True,
    reason="a channel pinned to OpenAI needs a native agent of its own, so an operator row"
    " (runtime_defaults.py module docstring) - nc-defaults",
)
def test_j9_a_channel_pinned_to_openai_runs_from_the_code_defaults(monkeypatch):
    from daimon.core.turn.runtime_defaults import default_rows

    monkeypatch.setenv(DEFAULT_KEY_ENV["openai"], "offline")
    rows = default_rows(str(uuid.uuid4()))
    assert any(
        row["profile"] == "openai.persistent_workspace"
        and not row.get("root_materialization")
        and not row.get("worker_materialization")
        for row in rows
    )
