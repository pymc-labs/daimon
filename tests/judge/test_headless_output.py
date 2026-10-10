"""Actual headless return capture; no native/render text promoted to delivery."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from daimon.core import headless_runner
from daimon.core.config import TurnSettings
from daimon.core.turn import driver
from daimon.core.turn.state import TurnState
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from pydantic import ValidationError

from .headless_output import HeadlessOutput, capture_headless_return


def clock(*ticks: float) -> Iterator[float]:
    return iter(ticks)


@pytest.mark.parametrize("answer", [None, "A" * 1000 + "TRUNCATED_SUFFIX"])
async def test_captures_actual_headless_host_return_not_pretool_text_or_truncated_suffix(
    monkeypatch: pytest.MonkeyPatch,
    answer: str | None,
) -> None:
    # Exercise the real host function and driver through SDK HTTP/SSE. This is
    # boundary validation on the legacy path, not a mux/default admission cert.
    monkeypatch.setattr(headless_runner, "load_turn_settings", lambda: TurnSettings(path="legacy"))
    monkeypatch.setattr(driver, "load_turn_settings", lambda: TurnSettings(path="legacy"))
    agent = ma_agent(id="agent_x", model="claude-haiku-5-5")
    environment = ma_environment(id="env_x")
    session = ma_session(
        id="session_x", agent_id=agent.id, environment_id=environment.id, model="claude-haiku-5-5"
    )
    events: list[dict[str, object]] = [
        {
            "id": "narration",
            "type": "agent.message",
            "processed_at": "2026-10-10T00:00:00Z",
            "content": [{"type": "text", "text": "HIDDEN_PRETOOL"}],
        },
        {
            "id": "tool",
            "type": "agent.tool_use",
            "processed_at": "2026-10-10T00:00:00Z",
            "name": "bash",
            "input": {"command": "true"},
        },
        {
            "id": "tool-result",
            "type": "agent.tool_result",
            "processed_at": "2026-10-10T00:00:00Z",
            "tool_use_id": "tool",
            "content": [{"type": "text", "text": "HIDDEN_TOOL_RESULT"}],
            "is_error": False,
        },
        {
            "id": "idle",
            "type": "session.status_idle",
            "processed_at": "2026-10-10T00:00:00Z",
            "stop_reason": {"type": "end_turn"},
        },
    ]
    if answer is not None:
        events.insert(
            -1,
            {
                "id": "answer",
                "type": "agent.message",
                "processed_at": "2026-10-10T00:00:00Z",
                "content": [{"type": "text", "text": answer}],
            },
        )
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET", "/v1/agents/agent_x", httpx.Response(200, json=agent.model_dump(mode="json"))
        ),
        ScriptedReply(
            "GET",
            "/v1/environments/env_x",
            httpx.Response(200, json=environment.model_dump(mode="json")),
        ),
        ScriptedReply(
            "POST", "/v1/sessions", httpx.Response(200, json=session.model_dump(mode="json"))
        ),
        ScriptedReply.stream("/v1/sessions/session_x/events/stream", events),
        ScriptedReply(
            "POST", "/v1/sessions/session_x/events", httpx.Response(200, json={"data": None})
        ),
    )
    states: list[TurnState] = []
    ticks = clock(1.0, 2.0)
    async with transport.client() as client:

        async def invoke() -> str:
            return await headless_runner.run_turn_impl(
                anthropic=client,
                agent_id=agent.id,
                environment_id=environment.id,
                trigger_message="Answer",
                on_state=states.append,
            )

        receipt = await asyncio.wait_for(
            capture_headless_return(invoke, evidence_id="return-1", clock=lambda: next(ticks)),
            timeout=10,
        )
    expected = answer[: headless_runner.LAST_RESULT_TAIL_MAX] if answer is not None else ""
    assert receipt.text == expected
    assert receipt.has_visible_text == bool(expected) and receipt.returned_s == 2.0
    assert states and any(block.kind == "tool_use" for block in states[0].content)
    assert not transport.violations and not transport.replies and len(transport.requests) == 5
    assert HeadlessOutput.model_validate_json(receipt.model_dump_json()) == receipt


@pytest.mark.parametrize("text", ["", " \n\t"])
async def test_empty_return_closes_only_the_headless_boundary_without_visible_message(
    text: str,
) -> None:
    async def invoke() -> str:
        return text

    ticks = clock(1.0, 1.0)
    receipt = await capture_headless_return(invoke, evidence_id="empty", clock=lambda: next(ticks))
    assert not receipt.has_visible_text and receipt.text == text
    assert receipt.boundary == "headless_return"


@pytest.mark.parametrize("failure", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_invocation_propagates_without_a_receipt(
    failure: BaseException,
) -> None:
    async def invoke() -> str:
        raise failure

    ticks = clock(1.0)
    with pytest.raises(type(failure)):
        await capture_headless_return(invoke, evidence_id="failed", clock=lambda: next(ticks))


@pytest.mark.parametrize("value", [None, 1, {}, TurnState()])
async def test_non_string_and_render_state_cannot_be_promoted_to_return(value: Any) -> None:
    async def invoke() -> str:
        return cast(str, value)

    ticks = clock(1.0, 2.0)
    with pytest.raises(ValidationError):
        await capture_headless_return(invoke, evidence_id="bad", clock=lambda: next(ticks))


@pytest.mark.parametrize("start", [-1.0, float("inf"), float("nan")])
async def test_invalid_opening_clock_refuses_before_dispatch(start: float) -> None:
    dispatched = False

    async def invoke() -> str:
        nonlocal dispatched
        dispatched = True
        return "value"

    with pytest.raises(ValidationError):
        await capture_headless_return(invoke, evidence_id="bad", clock=lambda: start)
    assert not dispatched


async def test_clock_reversal_refuses_return_receipt() -> None:
    async def invoke() -> str:
        return "value"

    ticks = clock(2.0, 1.0)
    with pytest.raises(ValueError, match="predates"):
        await capture_headless_return(invoke, evidence_id="bad", clock=lambda: next(ticks))


def test_return_validation_and_failure_propagation_survive_optimized_python() -> None:
    script = """
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from judge.headless_output import capture_headless_return
async def check():
    async def empty(): return ""
    receipt = await capture_headless_return(empty, evidence_id="empty", clock=lambda: 1.0)
    if receipt.has_visible_text: raise RuntimeError("empty return became visible")
    for value in (None, {}, 1):
        async def invalid(): return value
        try: await capture_headless_return(invalid, evidence_id="bad", clock=lambda: 1.0)
        except ValueError: pass
        else: raise RuntimeError("invalid return accepted")
    async def failed(): raise RuntimeError("host failure")
    try: await capture_headless_return(failed, evidence_id="fail", clock=lambda: 1.0)
    except RuntimeError as err:
        if str(err) != "host failure": raise
    else: raise RuntimeError("failed return produced evidence")
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(Path(__file__).parents[1])],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode())
