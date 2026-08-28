"""`daimon run` Typer command."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.run import command as cmd_mod
from daimon.adapters.cli.run.command import run_conversation
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.errors import TurnError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.ceiling import CEILING_MESSAGE
from daimon.core.turn.state import TurnState
from daimon.testing.ma import MARouter, sse_response
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
    class _FakeReq:
        method = "POST"
        url = "https://x"

    async def raising(**_kwargs: Any) -> TurnState:
        raise anthropic.APIConnectionError(request=cast(Any, _FakeReq()))

    monkeypatch.setattr(cmd_mod, "run_turn", raising)

    rt = _make_rt()
    exit_code = await run_conversation(rt=rt, session_id="s1", user_message="hello")
    assert exit_code == 1


@pytest.mark.asyncio
async def test_run_conversation_past_deadline_exits_nonzero_with_a_ceiling_terminal_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercises the REAL driver (not a monkeypatched run_turn) so this test
    actually stands behind the claim that `daimon run` is ceiling-covered --
    a test built out of `_install_run_turn` would assert on a fake return
    value and pass identically even if the `deadline=` plumbing were deleted.
    """
    router = MARouter()
    idle_event = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_idle_1",
        type="session.status_idle",
        processed_at=datetime.now(UTC),
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events/stream",
        lambda request, match: sse_response([idle_event.model_dump(mode="json")]),
    )
    router.add(
        "POST",
        r"/v1/sessions/[^/]+/events",
        lambda request, match: httpx.Response(200, json={"data": None}),
    )

    # The sleep is what makes the breach deterministic: `remaining_s` clamps
    # an already-past deadline to 0.001s, and an unslowed in-process fake
    # could finish inside that window (observed for headless_runner's own
    # assembly leg -- see 19-12-SUMMARY.md).
    async def _slow_stream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/events/stream"):
            await asyncio.sleep(2.0)
        return router.dispatch(request)

    transport = httpx.MockTransport(_slow_stream)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = anthropic.AsyncAnthropic(api_key="test", http_client=http_client)

    rt = CliRuntime(
        settings=cast(Settings, object()),
        anthropic=client,
        sessionmaker=cast(async_sessionmaker[AsyncSession], object()),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    exit_code = await run_conversation(
        rt=rt,
        session_id="sess_ceiling",
        user_message="hello",
        deadline=datetime.now(UTC) - timedelta(seconds=5),
    )

    assert exit_code == 1, "a ceiling breach must exit nonzero"

    # structlog's own log lines share stdout with the NDJSON protocol lines;
    # only lines that parse as JSON objects belong to the protocol.
    lines: list[dict[str, Any]] = []
    for raw_line in capsys.readouterr().out.splitlines():
        if not raw_line.strip():
            continue
        try:
            lines.append(json.loads(raw_line))
        except json.JSONDecodeError:
            continue
    failed_terminals = [
        line for line in lines if line.get("kind") == "terminal" and line.get("status") == "failed"
    ]
    assert len(failed_terminals) == 1, (
        "exactly one failed terminal NDJSON line must be emitted on a ceiling breach"
    )
    assert failed_terminals[0]["error"]["kind"] == "ceiling", (
        "the terminal failure's error kind must be 'ceiling'"
    )
    assert failed_terminals[0]["error"]["message"] == CEILING_MESSAGE, (
        "the terminal failure's error message must be the shared ceiling message"
    )
    assert not any(line.get("status") == "end_turn" for line in lines), (
        "a breach must never also emit a success terminal"
    )
