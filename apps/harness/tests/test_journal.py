"""Durability, indexed polling and interrupted side-effect recovery."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
from daimon_harness.journal import Journal
from daimon_harness.runtime import TurnContext
from daimon_harness.service import create_app
from mux.drivers.selfhosted.wire import TurnRequest
from pydantic import JsonValue

from .conftest import FakeModelAdapter, turn


def test_polling_indexes_and_wal_durability(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    for index in range(100):
        request = TurnRequest.model_validate(turn(turn_id=f"turn-{index}", key=f"key-{index}"))
        journal.claim(request)
        for _ in range(39):
            journal.append(request, "status_running", {})
        journal.append(request, "turn_ended", {"outcome": "completed"})
    assert journal.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert journal.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    connection = journal.connection("session-1")
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    queries: list[str] = []
    journal.db.set_trace_callback(queries.append)
    started = time.perf_counter()
    assert journal.complete("session-1")
    assert journal.ended("turn-99")
    assert journal.events("session-1", 3998)[-1].seq == 4000
    assert time.perf_counter() - started < 0.1
    assert len(queries) == 2
    assert all("request" not in query for query in queries)
    assert journal.events("unknown-session") == []
    assert not (journal.root / "sessions/unknown-session").exists()
    journal.close()


async def test_slow_sqlite_does_not_block_event_loop(tmp_path: Path) -> None:
    app = create_app(home=tmp_path, token="test", adapter=FakeModelAdapter())
    supervisor = app.state.supervisor
    waiting = asyncio.create_task(supervisor.io(time.sleep, 0.15))
    started = time.perf_counter()
    await asyncio.sleep(0.01)
    assert time.perf_counter() - started < 0.1
    await waiting
    await supervisor.close()


async def test_cancelled_tool_side_effects_reach_next_turn_history(tmp_path: Path) -> None:
    class InterruptedModel(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            if self.calls:
                return await super().run(context, history)
            await super().run(context, history)

            async def execute() -> JsonValue:
                context.write("effect.txt", "changed")
                side_effect.set()
                await asyncio.Event().wait()
                return None

            self.started.clear()
            await context.tool("effect", "write", {"path": "effect.txt"}, execute)
            return []

    model = InterruptedModel()
    side_effect = asyncio.Event()
    app = create_app(home=tmp_path, token="test", adapter=model)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test", "Accept": "application/json"},
        ) as client,
    ):
        assert (await client.post("/v0/turns", json=turn())).status_code == 202
        await asyncio.wait_for(side_effect.wait(), 5)
        await client.post("/v0/turns/turn-1/cancel")
        journal = Journal(tmp_path)
        history = journal.history("session-1")
        assert "effect.txt" in str(history)
        assert "side effects may have occurred" in str(history)
        journal.close()
        await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        await app.state.supervisor.tasks["next"]
        assert model.histories[-1] == history
