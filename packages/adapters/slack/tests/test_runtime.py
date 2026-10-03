"""Tests for SlackRuntime construction and build_runtime bootstrap."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from daimon.adapters.slack.runtime import SlackRuntime, build_runtime
from daimon.core.channel_budget_notice import BudgetNotice, spawn_budget_notice
from daimon.core.config import Settings


def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip DAIMON_* env vars + repo .env so tests see exactly what they construct."""
    for name in list(os.environ):
        if name.startswith("DAIMON_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")


class TestSlackRuntime:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(SlackRuntime), "SlackRuntime should be a dataclass"
        fields = {f.name for f in dataclasses.fields(SlackRuntime)}
        assert fields == {
            "settings",
            "anthropic",
            "sessionmaker",
            "billing_config",
            "http_client",
            "resolver_cache",
            "turn_deps",
            "deployment_default",
            "mcp_token_probe",
            "group_members",
        }, (
            "expected exactly settings/anthropic/sessionmaker/billing_config/http_client/"
            f"resolver_cache/turn_deps/deployment_default/mcp_token_probe/group_members fields, "
            f"got {fields}"
        )

    def test_frozen(self) -> None:
        """SlackRuntime should be immutable (frozen=True)."""
        rt = SlackRuntime(
            settings=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub for structural test
            anthropic=MagicMock(),  # pyright: ignore[reportArgumentType]
            sessionmaker=MagicMock(),  # pyright: ignore[reportArgumentType]
            billing_config=None,
            http_client=MagicMock(),  # pyright: ignore[reportArgumentType]
            resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]
            turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rt.settings = MagicMock()  # type: ignore[misc]  # intentionally testing frozen


async def test_build_runtime_yields_wired_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_runtime yields a SlackRuntime with non-None sessionmaker and anthropic."""
    _isolate_settings_env(monkeypatch)
    monkeypatch.setenv("DAIMON_SLACK__SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("DAIMON_SLACK__APP_TOKEN", "xapp-test-token")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    async with build_runtime(settings) as runtime:
        assert runtime.sessionmaker is not None, "build_runtime must wire a non-None sessionmaker"
        assert runtime.anthropic is not None, "build_runtime must wire a non-None anthropic client"
        assert runtime.http_client is not None, "build_runtime wires an http_client"


async def test_main_guard_exits_cleanly_when_slack_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() calls sys.exit(0) when no DAIMON_SLACK__* env vars are set."""
    _isolate_settings_env(monkeypatch)
    # No DAIMON_SLACK__* vars set, so settings.slack is None

    from daimon.adapters.slack.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        await main()

    assert exc_info.value.code == 0, (
        "guard must exit cleanly with code 0 when Slack is unconfigured"
    )


async def test_shutdown_waits_for_a_budget_notice_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deploy during a refusal must not strand its notice once the engine is gone."""
    _isolate_settings_env(monkeypatch)
    monkeypatch.setenv("DAIMON_SLACK__SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("DAIMON_SLACK__APP_TOKEN", "xapp-test-token")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    @asynccontextmanager
    async def slow_session() -> AsyncIterator[None]:
        await asyncio.sleep(0.2)
        raise RuntimeError("database gone")
        yield

    async def notifier(notice: BudgetNotice) -> int:
        return 0

    async with build_runtime(settings):
        spawn_budget_notice(
            sessionmaker=cast(Any, slow_session),
            notifier=notifier,
            tenant_id=uuid.uuid4(),
            platform="slack",
            channel_id="C1",
            now=datetime.now(UTC),
        )
        notices = [t for t in asyncio.all_tasks() if t.get_name() == "channel_budget.notice"]

    assert notices, "the refusal spawned a notice"
    assert all(t.done() for t in notices), "shutdown drained it before disposing the engine"
