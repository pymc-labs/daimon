"""Tests for SlackRuntime construction and build_runtime bootstrap."""

from __future__ import annotations

import dataclasses
import os
from unittest.mock import MagicMock

import pytest
from daimon.adapters.slack.runtime import SlackRuntime, build_runtime
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
            "http_client",
            "deployment_default",
        }, (
            "expected exactly settings/anthropic/sessionmaker/http_client/"
            f"deployment_default fields, got {fields}"
        )

    def test_frozen(self) -> None:
        """SlackRuntime should be immutable (frozen=True)."""
        rt = SlackRuntime(
            settings=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub for structural test
            anthropic=MagicMock(),  # pyright: ignore[reportArgumentType]
            sessionmaker=MagicMock(),  # pyright: ignore[reportArgumentType]
            http_client=MagicMock(),  # pyright: ignore[reportArgumentType]
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
