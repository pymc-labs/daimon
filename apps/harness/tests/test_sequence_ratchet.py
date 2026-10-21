"""Sequence ratchet exercised through C3, real SQLite, checkpoint archives and WAL."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from daimon_harness.journal import Journal
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mux.drivers.selfhosted.wire import TurnRequest

from .conftest import turn
from .test_checkpoints import MemoryBlobs, harness

SESSION = "session-1"
EVENTS = "/v0/sessions/session-1/events"


@pytest.mark.parametrize("route", ["poll", "turn", "action", "cancel", "invocations"])
async def test_stale_poll_restore_then_newer_cursor_never_reuses_13_or_14(tmp_path, route):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        await supervisor.flush_checkpoints()
        assert supervisor.journal.latest_seq(SESSION) == 6
        blobs.fail = True
        await client.post("/v0/turns", json=turn("tool", turn_id="second", key="second"))
        await supervisor.tasks["second"]
        await supervisor.flush_checkpoints()
        assert supervisor.journal.latest_seq(SESSION) == 14
    shutil.rmtree(tmp_path / "old")
    blobs.fail = False
    async with harness(tmp_path / "new", blobs) as (supervisor, client):
        stale = await client.get(EVENTS, params={"after_seq": 6})
        assert stale.status_code == 200
        if route == "poll":
            result = await client.get(EVENTS, params={"after_seq": 14})
        elif route == "turn":
            result = await client.post(
                "/v0/turns", json=turn(turn_id="third", key="third"), params={"after_seq": 14}
            )
            await supervisor.tasks["third"]
        elif route == "invocations":
            result = await client.post(
                "/invocations",
                json={"c3": {"method": "GET", "path": EVENTS, "query": {"after_seq": 14}}},
            )
        else:
            result = await client.post(
                f"/v0/turns/second/{'actions' if route == 'action' else 'cancel'}",
                params={"after_seq": 14},
                json={"action_id": "gone", "decision": "allow"} if route == "action" else None,
            )
        assert result.status_code == (404 if route == "action" else 202 if route == "turn" else 200)
        response = await client.get(EVENTS, params={"after_seq": 14})
        events = response.json()["events"]
        assert events[0]["type"] == "history_gap"
        assert events[0]["seq"] == 15
        assert events[0]["data"]["from_seq"] <= 13
        assert events[0]["data"]["to_seq"] == 14
        assert events[0]["data"]["reason"] == "sandbox_restored"
        if route != "turn":
            await client.post("/v0/turns", json=turn(turn_id="third", key="third"))
            await supervisor.tasks["third"]
        third = [e for e in supervisor.journal.events(SESSION) if e.turn_id == "third"]
        assert third and min(e.seq for e in third) > 14
        assert third[-1].type == "turn_ended"
        assert supervisor.journal.client_cursor(SESSION) == 14


async def test_cursor_ratchet_is_durable_before_append_and_accepts_large_fences(tmp_path):
    journal = Journal(tmp_path)
    cursor = 2**53
    assert journal.record_cursor(SESSION, cursor) == cursor
    assert not journal.events(SESSION)
    journal.close()  # Crash after accepting the cursor, before any event append.
    journal = Journal(tmp_path)
    assert journal.record_cursor(SESSION, 6) == cursor
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    event = journal.append(request, "status_running", {})
    gap = journal.events(SESSION)[0]
    assert gap.type == "history_gap" and gap.seq == cursor + 1
    assert gap.data["from_seq"] == 1 and gap.data["to_seq"] == cursor
    assert event.seq == cursor + 2
    journal.close()


class ControlledModel:
    """Keep a model request open across actions, cancellation and crashes."""

    async def run(self, context, history):
        request_id = context.request.turn_id + ":request"
        await context.emit("model_request_started", {"request_id": request_id, "model": "test"})
        if context.request.input[0].text == "hold":

            async def execute():
                return "approved"

            await context.tool("call", "write", {"path": "result"}, execute)
        await context.emit(
            "usage",
            dict(
                request_id=request_id,
                model="test",
                input_tokens=10,
                cached_input_tokens=0,
                output_tokens=2,
                reasoning_tokens=0,
            ),
        )
        return history


def clone_local(journal, destination):
    """A local process crash retains committed SQLite, not task finalizers."""
    for source in (journal.root / "turns.sqlite3", journal.session(SESSION) / "journal.sqlite3"):
        target = destination / source.relative_to(journal.root.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(target)) as db:
            (journal.db if source.name == "turns.sqlite3" else journal.connection(SESSION)).backup(
                db
            )


OPERATIONS = st.lists(
    st.tuples(
        st.sampled_from(
            ["poll", "turn", "action", "cancel", "checkpoint", "restore", "crash", "concurrent"]
        ),
        st.integers(min_value=0, max_value=80),
        st.integers(min_value=0, max_value=2),
    ),
    min_size=12,
    max_size=40,
)


@settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(trace=OPERATIONS)
async def test_sequence_ratchet_random_interleavings(tmp_path, monkeypatch, trace):
    # Clients retain their observed high-water across sandbox destruction. Cursors
    # on ordinary requests may be stale, or arbitrarily ahead of the server.
    with (
        tempfile.TemporaryDirectory(prefix="sequence-ratchet-", dir=tmp_path) as scratch,
        monkeypatch.context() as patch,
    ):
        original_append = Journal.append

        def audited_append(journal, request, kind, data):
            accepted = journal.client_cursor(request.session_id)
            event = original_append(journal, request, kind, data)
            assert event.seq > accepted, "append ignored a durably accepted client fence"
            return event

        patch.setattr(Journal, "append", audited_append)
        root = Path(scratch)
        blobs = MemoryBlobs()
        generation, serial = 0, 0
        clients = [0, 0, 0]
        assigned, usage, turns = {}, {}, {}
        manager = harness(root / str(generation), blobs, ControlledModel())
        supervisor, client = await manager.__aenter__()

        def remember():
            for event in supervisor.journal.events(SESSION):
                assert assigned.setdefault(event.seq, event) == event, "sequence reused"
                if event.type != "history_gap":
                    turns[event.turn_id] = max(turns.get(event.turn_id, 0), event.seq)
                if event.type == "usage":
                    identity = (event.turn_id, event.data["request_id"])
                    assert usage.setdefault(identity, event) == event, "usage identity duplicated"

        async def settled():
            # Let short fake model work reach approval or completion, without
            # assuming how many awaits the production journal/WAL path takes.
            for task in list(supervisor.tasks.values()):
                async with asyncio.timeout(3):
                    while not task.done() and not any(
                        c.pending for c in supervisor.contexts.values()
                    ):
                        await asyncio.sleep(0.001)
            remember()

        async def poll(cursor, replay=0):
            response = await client.get(
                EVENTS, params={"after_seq": cursor, "replay_after_seq": replay}
            )
            assert response.status_code == 200, response.text
            assert supervisor.journal.client_cursor(SESSION) >= cursor
            remember()
            return response.json()["events"]

        try:
            # A real terminal checkpoint guarantees a restore source, while
            # later random checkpoints can also capture an active request.
            await client.post("/v0/turns", json=turn(turn_id="seed", key="seed"))
            await supervisor.tasks["seed"]
            await supervisor.flush_checkpoints()
            remember()
            for operation, cursor, which in trace:
                if operation in {"restore", "crash"}:
                    remember()
                    generation += 1
                    home = root / str(generation)
                    if operation == "crash":
                        clone_local(supervisor.journal, home)
                    # Freeze remote state at the crash, before task finalizers
                    # and shutdown uploads can change the old process's store.
                    remote = MemoryBlobs()
                    remote.blobs = blobs.blobs.copy()
                    await manager.__aexit__(None, None, None)
                    blobs = remote
                    manager = harness(home, blobs, ControlledModel())
                    supervisor, client = await manager.__aenter__()
                    # The restoring client supplies all previously observed
                    # sequences; subsequent requests freely interleave cursors.
                    await poll(max(cursor, max(assigned, default=0)))
                elif operation == "turn":
                    serial += 1
                    request = turn(
                        "hold" if cursor % 2 else "complete",
                        turn_id=f"turn-{serial}",
                        key=f"turn-{serial}",
                    )
                    request["tool_policy"] = {"write": "ask"}
                    result = await client.post(
                        "/v0/turns", params={"after_seq": cursor}, json=request
                    )
                    assert result.status_code in {202, 409}
                    assert supervisor.journal.client_cursor(SESSION) >= cursor
                    await settled()
                elif operation == "poll":
                    response = await poll(cursor, min(cursor, clients[which]))
                    clients[which] = max([clients[which], cursor, *(e["seq"] for e in response)])
                elif operation == "checkpoint":
                    await supervisor.checkpoint(SESSION)
                elif operation == "concurrent":
                    active = next(
                        (name for name, task in supervisor.tasks.items() if not task.done()), "seed"
                    )
                    results = await asyncio.gather(
                        client.get(EVENTS, params={"after_seq": cursor}),
                        client.get(EVENTS, params={"after_seq": clients[which]}),
                        client.post(
                            f"/v0/turns/{active}/cancel", params={"after_seq": cursor // 2}
                        ),
                    )
                    assert all(result.status_code == 200 for result in results)
                    assert supervisor.journal.client_cursor(SESSION) >= max(cursor, clients[which])
                    await settled()
                else:
                    active = next(
                        (name for name, task in supervisor.tasks.items() if not task.done()), "seed"
                    )
                    if operation == "cancel":
                        result = await client.post(
                            f"/v0/turns/{active}/cancel", params={"after_seq": cursor}
                        )
                        assert result.status_code == 200
                    else:
                        context = supervisor.contexts.get(active)
                        action_id = next(iter(context.pending), "missing") if context else "missing"
                        result = await client.post(
                            f"/v0/turns/{active}/actions",
                            params={"after_seq": cursor},
                            json={"action_id": action_id, "decision": "allow"},
                        )
                        assert result.status_code in {200, 404}
                    assert supervisor.journal.client_cursor(SESSION) >= cursor
                    await settled()
                remember()
            # Each client's complete replay must contain the turn's terminal,
            # or an authoritative gap covering the last record lost for it.
            for turn_id, task in list(supervisor.tasks.items()):
                if not task.done():
                    await client.post(f"/v0/turns/{turn_id}/cancel")
            for cursor in clients:
                events = await poll(max(cursor, max(assigned, default=0)))
                terminal = {e["turn_id"] for e in events if e["type"] == "turn_ended"}
                gaps = [e["data"] for e in events if e["type"] == "history_gap"]
                for turn_id, last in turns.items():
                    assert turn_id in terminal or any(
                        g["from_seq"] <= last <= g["to_seq"] for g in gaps
                    )
        finally:
            await manager.__aexit__(None, None, None)


async def test_unauthenticated_request_cannot_raise_the_ratchet(tmp_path):
    async with harness(tmp_path, MemoryBlobs()) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        await client.get(EVENTS, params={"after_seq": 6})
        result = await client.get(
            EVENTS, params={"after_seq": 10**9 + 7}, headers={"Authorization": "Bearer wrong"}
        )
        assert result.status_code == 401
        assert supervisor.journal.client_cursor(SESSION) == 6


async def test_large_cursor_on_warm_and_restored_session_survives_restart(tmp_path):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        await supervisor.flush_checkpoints()
    cursor = 10**9 + 7
    home = tmp_path / "new"
    async with harness(home, blobs) as (supervisor, client):
        result = await client.get(EVENTS, params={"after_seq": cursor})
        assert result.status_code == 200
        gap = result.json()["events"][0]
        assert gap["type"] == "history_gap" and gap["seq"] == cursor + 1
        # This second higher cursor is applied without restoring again.
        assert (await client.get(EVENTS, params={"after_seq": cursor + 5})).status_code == 200
    async with harness(home, blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        await supervisor.tasks["next"]
        assert supervisor.journal.client_cursor(SESSION) == cursor + 5
        events = [event for event in supervisor.journal.events(SESSION) if event.turn_id == "next"]
        assert min(event.seq for event in events) > cursor + 5


def test_gap_and_event_append_commit_together(tmp_path):
    from pydantic import ValidationError

    journal = Journal(tmp_path)
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    journal.record_cursor(SESSION, 14)
    with pytest.raises(ValidationError):
        journal.append(request, "usage", {})
    assert journal.events(SESSION) == []
    assert journal.client_cursor(SESSION) == 14
    event = journal.append(request, "status_running", {})
    assert [(e.seq, e.type) for e in journal.events(SESSION)] == [
        (15, "history_gap"),
        (16, "status_running"),
    ]
    assert event.seq == 16
    journal.close()


def test_ratchet_migrates_an_old_reserved_sequence_floor(tmp_path):
    path = tmp_path / ".daimon/sessions/session-1/journal.sqlite3"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE event_sequence(id INTEGER PRIMARY KEY, floor INTEGER)")
        db.execute("INSERT INTO event_sequence VALUES (1, 11)")
        db.commit()
    journal = Journal(tmp_path)
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    journal.record_cursor(SESSION, 6)
    journal.append(request, "status_running", {})
    assert [(e.seq, e.type) for e in journal.events(SESSION)] == [
        (11, "history_gap"),
        (12, "status_running"),
    ]
    journal.close()
