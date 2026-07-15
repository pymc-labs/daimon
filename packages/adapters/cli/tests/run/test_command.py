"""`daimon run` Typer command."""

from __future__ import annotations

from typing import Any, cast

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.run import command as cmd_mod
from daimon.adapters.cli.run.command import run_conversation
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.errors import TurnError
from daimon.core.turn.state import TurnState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner


def _make_rt() -> CliRuntime:
    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", cast(Settings, object()))
    object.__setattr__(rt, "anthropic", cast(AsyncAnthropic, object()))
    object.__setattr__(rt, "sessionmaker", cast(async_sessionmaker[AsyncSession], object()))
    return cast(CliRuntime, rt)


def _install_run_turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: TurnState,
    captured: dict[str, Any] | None = None,
) -> None:
    async def fake_run_turn(**kwargs: Any) -> TurnState:
        if captured is not None:
            captured.update(kwargs)
        return state

    monkeypatch.setattr(cmd_mod, "run_turn", fake_run_turn)


def _invoke(args: list[str], **kwargs: Any) -> Any:
    runner = CliRunner()
    return runner.invoke(main_mod.app, ["run", *args], **kwargs)


def test_run_missing_session_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd_mod, "load_settings", lambda: cast(Settings, object()))
    result = _invoke(["hello"])
    assert result.exit_code == 1
    assert "--session" in result.stderr


def test_run_positional_and_message_flag_conflict_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cmd_mod, "load_settings", lambda: cast(Settings, object()))
    result = _invoke(["hi", "--session", "s1", "--message", "there"])
    assert result.exit_code == 1
    assert "positional OR --message" in result.stderr


def test_run_new_turn_missing_message_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cmd_mod, "load_settings", lambda: cast(Settings, object()))
    result = _invoke(["--session", "s1"])
    assert result.exit_code == 1
    assert "user message" in result.stderr


@pytest.mark.asyncio
async def test_run_exits_0_on_end_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run_turn(monkeypatch, state=TurnState())
    rt = _make_rt()
    exit_code = await run_conversation(rt=rt, session_id="s1", user_message="hello")
    assert exit_code == 0


@pytest.mark.asyncio
async def test_run_exits_1_on_turn_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run_turn(
        monkeypatch,
        state=TurnState(error=TurnError(kind="upstream", message="boom")),
    )
    rt = _make_rt()
    exit_code = await run_conversation(rt=rt, session_id="s1", user_message="hello")
    assert exit_code == 1


@pytest.mark.asyncio
async def test_run_reads_message_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _install_run_turn(monkeypatch, state=TurnState(), captured=captured)
    rt = _make_rt()
    exit_code = await run_conversation(rt=rt, session_id="s1", user_message="hello from stdin")
    assert exit_code == 0
    assert captured["user_message"] == "hello from stdin"


@pytest.mark.asyncio
async def test_run_api_error_emits_failed_terminal_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic

    class _FakeReq:
        method = "POST"
        url = "https://x"

    async def raising(**_kwargs: Any) -> TurnState:
        raise anthropic.APIConnectionError(request=cast(Any, _FakeReq()))

    monkeypatch.setattr(cmd_mod, "run_turn", raising)

    rt = _make_rt()
    exit_code = await run_conversation(rt=rt, session_id="s1", user_message="hello")
    assert exit_code == 1
