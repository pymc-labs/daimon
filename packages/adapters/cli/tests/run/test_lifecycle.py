"""NdjsonLifecycle — TurnLifecycle impl that writes NDJSON to stdout."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import TextIO, cast

import pytest
from daimon.adapters.cli.run.lifecycle import NdjsonLifecycle
from daimon.core.errors import TurnError
from daimon.core.turn.state import TurnState


class _FakeSseEvent:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self, mode: str) -> dict[str, object]:
        return self._payload


def _make(stdout: io.StringIO) -> NdjsonLifecycle:
    return NdjsonLifecycle(stdout=cast(TextIO, stdout), session_id="sess_1", turn_id="turn_1")


@pytest.mark.asyncio
async def test_on_sse_event_writes_sse_line() -> None:
    buf = io.StringIO()
    lc = _make(buf)
    await lc.on_sse_event(cast("object", _FakeSseEvent({"type": "message_start"})))  # type: ignore[arg-type]
    parsed = json.loads(buf.getvalue())
    assert parsed["kind"] == "sse"
    assert parsed["event"] == {"type": "message_start"}
    assert parsed["session_id"] == "sess_1"
    assert parsed["turn_id"] == "turn_1"


@pytest.mark.asyncio
async def test_on_render_is_noop() -> None:
    buf = io.StringIO()
    lc = _make(buf)
    await lc.on_render(TurnState())
    assert buf.getvalue() == ""


@pytest.mark.asyncio
async def test_on_reconnect_writes_reconnect_line() -> None:
    buf = io.StringIO()
    lc = _make(buf)
    await lc.on_reconnect("connection_dropped")
    parsed = json.loads(buf.getvalue())
    assert parsed["kind"] == "reconnect"
    assert parsed["reason"] == "connection_dropped"


@pytest.mark.asyncio
async def test_on_rate_limited_writes_line_with_iso_until() -> None:
    buf = io.StringIO()
    lc = _make(buf)
    until = datetime(2026, 4, 22, 10, 5, 0, tzinfo=UTC)
    await lc.on_rate_limited(until)
    parsed = json.loads(buf.getvalue())
    assert parsed["kind"] == "rate_limited"
    assert isinstance(parsed["until"], str)
    assert parsed["until"].startswith("2026-04-22T10:05:00")


@pytest.mark.asyncio
async def test_on_interrupt_sent_writes_line_with_source() -> None:
    buf = io.StringIO()
    lc = _make(buf)
    await lc.on_interrupt_sent("sigint")
    parsed = json.loads(buf.getvalue())
    assert parsed["kind"] == "interrupt_sent"
    assert parsed["source"] == "sigint"


@pytest.mark.asyncio
async def test_on_terminal_success_end_turn() -> None:
    buf = io.StringIO()
    lc = _make(buf)
    await lc.on_terminal_success(TurnState())
    parsed = json.loads(buf.getvalue())
    assert parsed["kind"] == "terminal"
    assert parsed["status"] in {"end_turn", "max_turns"}


@pytest.mark.asyncio
async def test_on_terminal_failure_emits_failed_terminal_with_error() -> None:
    buf = io.StringIO()
    lc = _make(buf)
    cause = RuntimeError("upstream boom")
    state = TurnState(error=TurnError(kind="upstream", message="upstream boom", cause=cause))
    await lc.on_terminal_failure(state, cause)
    parsed = json.loads(buf.getvalue())
    assert parsed["status"] == "failed"
    assert parsed["error"]["kind"] == "upstream"


class _CountingStringIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


@pytest.mark.asyncio
async def test_stdout_flushed_after_every_write() -> None:
    buf = _CountingStringIO()
    lc = _make(buf)
    await lc.on_reconnect("connection_dropped")
    assert buf.flush_count >= 1
