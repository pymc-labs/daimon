from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from daimon_harness.runtime import TurnContext
from daimon_harness.service import create_app
from pydantic import JsonValue


class FakeModelAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.histories: list[list[JsonValue]] = []
        self.started = asyncio.Event()

    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
        self.calls += 1
        self.histories.append(history)
        text = context.request.input[0]
        assert text.type == "text"
        mode = text.text
        await context.emit(
            "model_request_started",
            {"request_id": f"req-{self.calls}", "model": context.request.model},
        )
        if mode != "unmetered":
            await context.emit(
                "usage",
                {
                    "request_id": f"req-{self.calls}",
                    "model": context.request.model,
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "output_tokens": 4,
                    "reasoning_tokens": 1,
                },
            )
        self.started.set()
        if mode == "wait":
            await asyncio.Event().wait()
        if mode in {"tool", "approval"}:

            async def execute() -> JsonValue:
                return context.write("result.txt", "durable")

            await context.tool(
                "call-1", "write", {"path": "result.txt", "content": "durable"}, execute
            )
        if mode == "error":
            raise RuntimeError("secret provider message must never be exposed")
        await context.emit(
            "agent_message_delta", {"message_id": "msg-" + context.request.turn_id, "text": "done"}
        )
        await context.emit(
            "agent_message",
            {
                "message_id": "msg-" + context.request.turn_id,
                "parts": [{"type": "text", "text": "done"}],
            },
        )
        return [
            *history,
            {"role": "user", "content": mode},
            {"role": "assistant", "content": "done"},
        ]


@pytest.fixture
async def harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeModelAdapter, Path]]:
    monkeypatch.setenv("HARNESS_CONFIRM_TOOLS", "")
    model = FakeModelAdapter()
    app = create_app(home=tmp_path, token="test-token", adapter=model)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer test-token"},
            timeout=10,
        ) as client:
            yield client, model, tmp_path
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        listener.close()


def turn(mode: str = "hello", *, turn_id: str = "turn-1", key: str = "key-1") -> dict[str, object]:
    return {
        "session_id": "session-1",
        "turn_id": turn_id,
        "idempotency_key": key,
        "model": "gpt-5",
        "instructions": "Help",
        "input": [{"type": "text", "text": mode}],
        "mcp_servers": [],
        "skills": [],
        "limits": {"max_tool_calls": 3, "deadline_s": 30},
    }


@pytest.fixture(autouse=True)
def fake_harness_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TEST_MODE", "1")
