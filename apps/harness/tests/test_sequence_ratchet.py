"""Sequence ratchet exercised through C3, real SQLite, checkpoint archives and WAL."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from daimon_harness.journal import BLOCK, MAX_CURSOR, Journal
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from mux.drivers.selfhosted.wire import Event, TurnRequest

from .conftest import turn
from .test_checkpoints import MemoryBlobs, cleanup_failure_case, harness

SESSION = "session-1"
EVENTS = "/v0/sessions/session-1/events"


@pytest.mark.parametrize("route", ["poll", "turn", "action", "cancel", "invocations"])
async def test_stale_poll_restore_then_newer_cursor_never_reuses_13_or_14(tmp_path, route):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        await supervisor.flush_checkpoints()
        assert await supervisor.io(supervisor.journal.latest_seq, SESSION) == 6
        blobs.fail = True
        await client.post("/v0/turns", json=turn("tool", turn_id="second", key="second"))
        await supervisor.tasks["second"]
        await supervisor.flush_checkpoints()
        assert await supervisor.io(supervisor.journal.latest_seq, SESSION) == 14
        seen = {
            raw["seq"]: Event.model_validate(raw)
            for raw in (await client.get(EVENTS, params={"after_seq": 0})).json()["events"]
        }
    shutil.rmtree(tmp_path / "old")
    blobs.fail = False
    async with harness(tmp_path / "new", blobs) as (supervisor, client):
        stale = await client.get(EVENTS, params={"after_seq": 6})
        assert stale.status_code == 200
        for event in await supervisor.io(supervisor.journal.events, SESSION):
            if event.seq <= max(seen):
                assert seen[event.seq] == event, "host-seen sequence reused by stale-first restore"
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
        assert events[0]["seq"] == BLOCK + 1
        assert events[0]["data"]["from_seq"] <= 13
        assert events[0]["data"]["to_seq"] == BLOCK
        assert events[0]["data"]["reason"] == "sandbox_restored"
        if route != "turn":
            await client.post("/v0/turns", json=turn(turn_id="third", key="third"))
            await supervisor.tasks["third"]
        third = [
            e
            for e in await supervisor.io(supervisor.journal.events, SESSION)
            if e.turn_id == "third"
        ]
        assert third and min(e.seq for e in third) > BLOCK
        assert third[-1].type == "turn_ended"
        all_events = await supervisor.io(supervisor.journal.events, SESSION)
        by_seq = {event.seq: event for event in all_events}
        gaps = [event.data for event in all_events if event.type == "history_gap"]
        for seq, original in seen.items():
            if seq in by_seq:
                assert by_seq[seq] == original, "host-seen sequence reused"
            else:
                assert any(g["from_seq"] <= seq <= g["to_seq"] for g in gaps)
        assert await supervisor.io(supervisor.journal.client_cursor, SESSION) == 14


async def test_cursor_ratchet_is_durable_before_append_and_accepts_large_fences(tmp_path):
    journal = Journal(tmp_path)
    cursor = MAX_CURSOR - 2 * BLOCK
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
        await context.emit(
            "model_request_started", {"request_id": request_id, "model": "gpt-6-luna"}
        )
        if context.request.input[0].text == "hold":

            async def execute():
                return "approved"

            await context.tool("call", "write", {"path": "result"}, execute)
        await context.emit(
            "usage",
            dict(
                request_id=request_id,
                model="gpt-6-luna",
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


class CommitFailure:
    """SQLite proxy: fail only writes at COMMIT, after remote inventory upload."""

    def __init__(self, db):
        self.db, self.full = db, True

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __enter__(self):
        self.mark = self.db.total_changes
        return self.db.__enter__()

    def __exit__(self, kind, value, traceback):
        if kind is None and self.full and self.db.total_changes != self.mark:
            self.db.rollback()
            raise sqlite3.OperationalError("database or disk is full")
        return self.db.__exit__(kind, value, traceback)

    def commit(self):
        if self.full and self.db.in_transaction:
            self.db.rollback()
            raise sqlite3.OperationalError("database or disk is full")
        return self.db.commit()


def fail_local_commits(journal):
    db = journal.connection(SESSION)
    while isinstance(db, CommitFailure):
        db = db.db
    journal.connections[SESSION] = CommitFailure(db)


def free_local_space(journal):
    journal.connection(SESSION).full = False


class ReservationBlobs(MemoryBlobs):
    def __init__(self):
        super().__init__()
        self.fail_reservations = False
        self.fail_recovery = False
        self.fail_usage_ack = False
        self.oracle = None

    def upload(self, name, source):
        raw = source.read()
        if self.fail_recovery and b'"kind":"usage_unavailable"' in raw:
            self.fail_recovery = False
            raise OSError("partial recovery upload failed")
        if self.fail_reservations and "/reservations/" in name:
            raise OSError("reservation unavailable")
        result = super().upload(name, io.BytesIO(raw))
        if self.oracle is not None and "/wal/" in name:
            for event in json.loads(raw)["inventory"]:
                if event["type"] == "usage":
                    self.oracle.durable(event["turn_id"], event["data"])
        if self.fail_usage_ack and "/wal/" in name and b'"type":"usage"' in raw:
            raise OSError("usage committed but acknowledgement lost")
        return result


class HostSeen:
    """Only wire responses populate the oracle; request cursors are independent."""

    def __init__(self):
        self.events, self.outcomes, self.started, self.turns = {}, {}, set(), {}
        self.measured, self.terminals = {}, {}

    def durable(self, turn_id, data):
        identity = (turn_id, data["request_id"])
        assert self.measured.setdefault(identity, data) == data

    def observe(self, events):
        for event in events:
            seq = event["seq"]
            assert self.events.setdefault(seq, event) == event, "host-seen sequence reused"
            kind, data = event["type"], event["data"]
            turn_id = event["turn_id"]
            if kind == "turn_ended":
                assert self.terminals.setdefault(turn_id, event) == event, "two terminals"
            if kind != "history_gap":
                terminal = self.terminals.get(turn_id)
                assert terminal is None or seq <= terminal["seq"], "event after terminal"
                assert not any(
                    old["turn_id"] == turn_id
                    and old["type"] != "history_gap"
                    and old["seq"] > self.terminals.get(turn_id, {"seq": 2**63})["seq"]
                    for old in self.events.values()
                ), "event after terminal"
            if kind != "history_gap":
                self.turns[event["turn_id"]] = max(seq, self.turns.get(event["turn_id"], 0))
            identity = (event["turn_id"], data.get("request_id"))
            if kind == "model_request_started":
                self.started.add(identity)
            if kind == "usage" or (kind == "error" and data.get("kind") == "usage_unavailable"):
                assert self.outcomes.setdefault(identity, event) == event, "two usage outcomes"

    def complete(self, events):
        self.observe(events)
        terminal = {e["turn_id"] for e in events if e["type"] == "turn_ended"}
        gaps = [e["data"] for e in events if e["type"] == "history_gap"]
        for turn_id, last in self.turns.items():
            assert turn_id in terminal or any(g["from_seq"] <= last <= g["to_seq"] for g in gaps)
        assert self.started <= self.outcomes.keys(), "model request has no usage outcome"
        for identity, data in self.measured.items():
            assert identity in self.outcomes, "durable measured usage disappeared"
            outcome = self.outcomes[identity]
            assert outcome["type"] == "usage" and outcome["data"] == data, "measured became unknown"


OPERATIONS = st.lists(
    st.tuples(
        st.sampled_from(
            [
                "poll",
                "turn",
                "action",
                "cancel",
                "checkpoint",
                "restore",
                "crash",
                "concurrent",
                "reservation_failure",
                "cold_cancel",
                "remote_usage_crash",
                "paid_reservation_failure",
                "warning_reservation_failure",
                "recover_retry",
                "local_commit_failure",
                "cleanup_failure",
                "empty_remote_cursor",
            ]
        ),
        st.integers(min_value=0, max_value=3000),
        st.integers(min_value=0, max_value=2),
    ),
    min_size=8,
    max_size=24,
)


@settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(trace=OPERATIONS)
@example(
    trace=[
        ("turn", 0, 0),
        ("poll", 0, 0),
        ("restore", 0, 0),
        ("cold_cancel", 0, 0),
        ("reservation_failure", 0, 0),
        ("turn", 3000, 1),
        ("crash", 0, 1),
        ("poll", 0, 2),
    ]
)
@example(trace=[("remote_usage_crash", 0, 0)] * 8)
@example(trace=[("paid_reservation_failure", 0, 0)] * 8)
@example(trace=[("warning_reservation_failure", 0, 0)] * 8)
@example(trace=[("recover_retry", 0, 0)] * 8)
@example(trace=[("local_commit_failure", 0, 0)] * 8)
@example(trace=[("local_commit_failure", 0, 1)] * 8)
@example(trace=[("cleanup_failure", 0, 0)] * 8)
@example(trace=[("empty_remote_cursor", 3000, 0)] * 8)
async def test_sequence_ratchet_random_interleavings(tmp_path, monkeypatch, trace):
    # The ordinary CI property also exercises the production filesystem store.
    monkeypatch.setenv("HARNESS_RECOVERY_STORE", "pvc")
    with tempfile.TemporaryDirectory(prefix="sequence-reservation-", dir=tmp_path) as scratch:
        root, blobs, oracle = Path(scratch), ReservationBlobs(), HostSeen()
        blobs.oracle = oracle
        generation = serial = 0
        clients = [0, 0, 0]
        manager = harness(root / "0", blobs, ControlledModel())
        supervisor, client = await manager.__aenter__()

        async def settle():
            for task in list(supervisor.tasks.values()):
                async with asyncio.timeout(5):
                    while not task.done() and not any(
                        c.pending for c in supervisor.contexts.values()
                    ):
                        await asyncio.sleep(0.001)
            # Polling is the only observation channel. Never inspect the journal
            # to learn an event or advance a restoring client's cursor.

        async def poll(cursor=0, *, expect_complete=False):
            response = await client.get(EVENTS, params={"after_seq": cursor, "replay_after_seq": 0})
            assert response.status_code == 200, response.text
            if expect_complete:
                assert response.json()["complete"], "warning turn remained incomplete"
            events = response.json()["events"]
            oracle.observe(events)
            return events

        async def restart(
            cursor=0,
            *,
            local=False,
            cancel_first=False,
            local_home=None,
            remote_blobs=None,
            recover_retry=False,
        ):
            nonlocal generation, manager, supervisor, client, blobs
            generation += 1
            home = root / str(generation)
            if local_home is not None:
                shutil.copytree(local_home, home)
            elif local:
                await supervisor.io(clone_local, supervisor.journal, home)
            remote = ReservationBlobs()
            remote.blobs = (blobs.blobs if remote_blobs is None else remote_blobs).copy()
            remote.oracle = oracle
            remote.fail_recovery = recover_retry
            await manager.__aexit__(None, None, None)
            blobs = remote
            manager = harness(home, blobs, ControlledModel())
            supervisor, client = await manager.__aenter__()
            if cancel_first:
                active = next(reversed(oracle.turns), "first")
                result = await client.post(
                    f"/v0/turns/{active}/cancel", params={"after_seq": cursor}
                )
                assert result.status_code == 200, result.text
            if recover_retry:
                result = await client.get(EVENTS)
                assert result.status_code == 503, result.text
                # Diagnostic local reads never populate the host event oracle.
                partial = await supervisor.io(supervisor.journal.events, SESSION)
                last_gap = max(e.seq for e in partial if e.type == "history_gap")
            # Critically, this never raises a stale cursor to the host high-water.
            events = await poll(cursor)
            if recover_retry:
                assert sum(e["type"] == "history_gap" and e["seq"] >= last_gap for e in events) == 1
                repairs = [
                    e
                    for e in events
                    if e["type"] == "error"
                    and e["data"].get("kind") in {"sandbox_lost", "process_interrupted"}
                ]
                assert len({e["turn_id"] for e in repairs}) == len(repairs), (
                    "duplicate recovery error"
                )

        try:
            # Every example begins with a FIRST-TURN crash, NO checkpoint,
            # and a cold cleanup cancel carrying a stale cursor. Both in-flight
            # and measured first-turn model requests are exercised.
            blobs.fail = True  # No first-turn workspace checkpoint can survive.
            first = turn(
                "hold" if trace and trace[0][2] % 2 else "complete", turn_id="first", key="first"
            )
            first["tool_policy"] = {"write": "ask"}
            await client.post("/v0/turns", json=first)
            await settle()
            await supervisor.flush_checkpoints()
            await poll()
            blobs.blobs = {
                name: value for name, value in blobs.blobs.items() if not name.endswith(".tar.zst")
            }
            await restart(0, cancel_first=True)
            for operation, cursor, which in trace:
                if operation == "empty_remote_cursor":
                    # A separate genuinely cold session has no checkpoint, WAL,
                    # or reservation. An ahead cursor is still a valid fence.
                    async with harness(root / f"empty-{serial}", ReservationBlobs()) as (
                        cold,
                        cold_client,
                    ):
                        serial += 1
                        fence = max(1, cursor)
                        response = await cold_client.post(
                            "/v0/turns", json=turn(), params={"after_seq": fence}
                        )
                        assert response.status_code == 202, "empty remote cursor fence rejected"
                        await cold.tasks["turn-1"]
                        response = await cold_client.get(EVENTS, params={"after_seq": fence})
                        assert response.status_code == 200, response.text
                        events = response.json()["events"]
                        empty_oracle = HostSeen()
                        empty_oracle.complete(events)
                        assert all(e["seq"] > fence for e in events)
                        assert any(
                            e["type"] == "history_gap"
                            and e["data"]["from_seq"] == 1
                            and e["data"]["to_seq"] >= fence
                            for e in events
                        )
                        assert events[-1]["type"] == "turn_ended"
                        response = await cold_client.post(
                            "/v0/turns", json=turn(turn_id="next", key="next")
                        )
                        assert response.status_code == 202, (
                            "empty remote cursor wedged later submit"
                        )
                        await cold.tasks["next"]
                        response = await cold_client.get(EVENTS)
                        assert response.status_code == 200 and response.json()["complete"]
                        empty_oracle.complete(response.json()["events"])
                        assert any(
                            e["type"] == "turn_ended" and e["turn_id"] == "next"
                            for e in response.json()["events"]
                        )
                elif operation in {"restore", "crash", "cold_cancel"}:
                    await restart(
                        min(cursor, clients[which]),
                        local=operation == "crash",
                        cancel_first=operation == "cold_cancel",
                    )
                elif operation in {
                    "remote_usage_crash",
                    "paid_reservation_failure",
                    "warning_reservation_failure",
                    "recover_retry",
                    "local_commit_failure",
                }:
                    for turn_id, task in list(supervisor.tasks.items()):
                        if not task.done():
                            await client.post(f"/v0/turns/{turn_id}/cancel")
                    await settle()
                    serial += 1
                    special = f"fault-{serial}"
                    frozen_home = root / f"frozen-{serial}"
                    frozen_remote = {}
                    ready, release = asyncio.Event(), asyncio.Event()

                    class FaultModel:
                        async def run(
                            self,
                            context,
                            history,
                            operation=operation,
                            special=special,
                            ready=ready,
                            release=release,
                            which=which,
                        ):
                            request_id = special + ":request"
                            await context.emit(
                                "model_request_started",
                                {
                                    "request_id": request_id,
                                    "model": "gpt-6-luna",
                                },
                            )
                            if operation in {"remote_usage_crash", "recover_retry"}:
                                ready.set()
                                await release.wait()
                            data = dict(
                                request_id=request_id,
                                model="gpt-6-luna",
                                input_tokens=123,
                                cached_input_tokens=0,
                                output_tokens=45,
                                reasoning_tokens=0,
                            )
                            if operation == "local_commit_failure":
                                await supervisor.io(fail_local_commits, supervisor.journal)
                                blobs.fail_usage_ack = bool(which % 2)
                            if operation == "paid_reservation_failure":
                                fence = await supervisor.io(
                                    supervisor.journal.reserved_high_water, SESSION
                                )
                                await supervisor.io(
                                    supervisor.journal.record_cursor, SESSION, fence
                                )
                                blobs.fail_reservations = True
                            try:
                                await context.emit("usage", data)
                            finally:
                                if operation == "paid_reservation_failure":
                                    oracle.durable(special, data)
                                    blobs.fail_reservations = False
                            if operation == "warning_reservation_failure":
                                fence = await supervisor.io(
                                    supervisor.journal.reserved_high_water, SESSION
                                )
                                await supervisor.io(
                                    supervisor.journal.record_cursor, SESSION, fence - 2
                                )
                            return history

                    supervisor.adapter = FaultModel()
                    blobs.fail = True  # Preserve a WAL-only turn beyond the checkpoint.
                    result = await client.post("/v0/turns", json=turn(turn_id=special, key=special))
                    assert result.status_code == 202, result.text
                    if operation in {"remote_usage_crash", "recover_retry"}:
                        await asyncio.wait_for(ready.wait(), 5)
                        await poll()
                        await supervisor.io(clone_local, supervisor.journal, frozen_home)
                        if operation == "remote_usage_crash":
                            original = supervisor.journal.commit_inventory

                            def capture(
                                request,
                                original=original,
                                frozen_remote=frozen_remote,
                                special=special,
                            ):
                                original(request)
                                events = supervisor.journal.events(SESSION, include_pending=True)
                                if not frozen_remote and any(
                                    e.type == "usage" and e.turn_id == special for e in events
                                ):
                                    frozen_remote.update(blobs.blobs)

                            supervisor.journal.commit_inventory = capture
                            release.set()
                            await settle()
                            supervisor.journal.commit_inventory = original
                            assert frozen_remote
                            await restart(local_home=frozen_home, remote_blobs=frozen_remote)
                        else:
                            # Crash before the model responds, then fail after the
                            # first recovery error but before its usage outcome commits.
                            frozen_remote.update(blobs.blobs)
                            release.set()
                            await restart(remote_blobs=frozen_remote, recover_retry=True)
                    else:
                        await settle()
                        if operation == "local_commit_failure":
                            assert (await client.get(EVENTS)).status_code == 503
                            await supervisor.io(free_local_space, supervisor.journal)
                            blobs.fail_usage_ack = False
                        events = await poll()
                        if operation == "local_commit_failure":
                            # Same process/supervisor; never hide the failure by
                            # restarting before checking the host's usage outcome.
                            oracle.complete(events)
                        if operation == "warning_reservation_failure":
                            assert await supervisor.io(
                                supervisor.journal.latest_seq, SESSION
                            ) == await supervisor.io(
                                supervisor.journal.reserved_high_water, SESSION
                            )
                            await supervisor.io(supervisor.checkpoint_notice, SESSION)
                            blobs.fail_reservations = True
                            warning_turn = special + "-warning"
                            result = await client.post(
                                "/v0/turns", json=turn(turn_id=warning_turn, key=warning_turn)
                            )
                            assert result.status_code == 202, "warning submit was not accepted"
                            await asyncio.gather(*supervisor.tasks.values(), return_exceptions=True)
                            blobs.fail_reservations = False
                            events = await poll()
                            assert (
                                sum(
                                    e["turn_id"] == warning_turn and e["type"] == "turn_ended"
                                    for e in events
                                )
                                == 1
                            ), "warning turn has no terminal"
                            supervisor.adapter = ControlledModel()
                            following = warning_turn + "-next"
                            response = await client.post(
                                "/v0/turns", json=turn(turn_id=following, key=following)
                            )
                            assert response.status_code == 202, "warning blocked the next turn"
                            await settle()
                            events = await poll()
                            assert (
                                sum(
                                    e["turn_id"] == following and e["type"] == "turn_ended"
                                    for e in events
                                )
                                == 1
                            )
                    supervisor.adapter = ControlledModel()
                    blobs.fail = False
                elif operation == "cleanup_failure":
                    for turn_id, task in list(supervisor.tasks.items()):
                        if not task.done():
                            await client.post(f"/v0/turns/{turn_id}/cancel")
                    await settle()
                    serial += 1
                    await cleanup_failure_case(supervisor, client, monkeypatch, f"cleanup-{serial}")
                elif operation == "reservation_failure":
                    blobs.fail_reservations = True
                    # A warm cursor ahead of the block forces a reservation.
                    fence = await supervisor.io(supervisor.journal.reserved_high_water, SESSION)
                    response = await client.get(EVENTS, params={"after_seq": fence + 1})
                    assert response.status_code == 503
                    assert "durability_unavailable" in response.text
                    blobs.fail_reservations = False
                    await poll(0)
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
                    assert result.status_code in {202, 409}, result.text
                    await settle()
                elif operation == "poll":
                    events = await poll(cursor)
                    clients[which] = max([clients[which], *(e["seq"] for e in events)])
                elif operation == "checkpoint":
                    await supervisor.checkpoint(SESSION)
                else:
                    active = next(
                        (name for name, task in supervisor.tasks.items() if not task.done()),
                        "first",
                    )
                    if operation == "concurrent":
                        results = await asyncio.gather(
                            client.get(EVENTS, params={"after_seq": cursor, "replay_after_seq": 0}),
                            client.get(
                                EVENTS, params={"after_seq": clients[which], "replay_after_seq": 0}
                            ),
                            client.post(
                                f"/v0/turns/{active}/cancel", params={"after_seq": cursor // 2}
                            ),
                        )
                        for response in results:
                            assert response.status_code == 200, response.text
                            if "events" in response.json():
                                oracle.observe(response.json()["events"])
                    elif operation == "cancel":
                        assert (
                            await client.post(
                                f"/v0/turns/{active}/cancel", params={"after_seq": cursor}
                            )
                        ).status_code == 200
                    else:
                        context = supervisor.contexts.get(active)
                        action_id = next(iter(context.pending), "missing") if context else "missing"
                        result = await client.post(
                            f"/v0/turns/{active}/actions",
                            params={"after_seq": cursor},
                            json={"action_id": action_id, "decision": "allow"},
                        )
                        assert result.status_code in {200, 404}
                    await settle()
                await poll(0)
            for turn_id, task in list(supervisor.tasks.items()):
                if not task.done():
                    await client.post(f"/v0/turns/{turn_id}/cancel")
            # Stale-first recovery is deliberately repeated at final reconcile.
            await restart(0)
            for cursor in clients:
                oracle.complete(await poll(cursor))
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
        assert await supervisor.io(supervisor.journal.client_cursor, SESSION) == 6


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
        assert await supervisor.io(supervisor.journal.client_cursor, SESSION) == cursor + 5
        events = [
            event
            for event in await supervisor.io(supervisor.journal.events, SESSION)
            if event.turn_id == "next"
        ]
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


def test_sequence_block_write_precedes_allocation_and_failure_burns_no_events(tmp_path):
    from daimon_harness.checkpoints import Checkpoints
    from daimon_harness.errors import HarnessError

    blobs, journal = ReservationBlobs(), Journal(tmp_path)
    checkpoints = Checkpoints(blobs)
    journal.reserve = checkpoints.reserve_sequences
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    try:
        for seq in range(1, BLOCK + 1):
            event = journal.append(request, "status_running", {})
            assert checkpoints.reservation_high_water(SESSION) >= event.seq == seq
        blobs.fail_reservations = True
        with pytest.raises(HarnessError, match="reservation"):
            journal.append(request, "status_running", {})
        assert journal.latest_seq(SESSION) == BLOCK
        assert journal.reserved_high_water(SESSION) == BLOCK
        blobs.fail_reservations = False
        assert journal.append(request, "status_running", {}).seq == BLOCK + 1
        assert checkpoints.reservation_high_water(SESSION) == 2 * BLOCK
    finally:
        journal.close()


async def test_failed_restore_preserves_cursor_and_repairs_missing_singleton(tmp_path):
    class Unavailable(ReservationBlobs):
        def list(self, prefix):
            raise OSError("restore unavailable")

    async with harness(tmp_path, Unavailable()) as (supervisor, client):
        result = await client.get(EVENTS, params={"after_seq": 17})
        assert result.status_code == 503
        assert await supervisor.io(supervisor.journal.client_cursor, SESSION) == 17

        def remove_singleton():
            with supervisor.journal.connection(SESSION) as db:
                db.execute("DELETE FROM event_sequence")

        await supervisor.io(remove_singleton)
        assert await supervisor.io(supervisor.journal.client_cursor, SESSION) == 0
        assert await supervisor.io(supervisor.journal.record_cursor, SESSION, 17) == 17


async def test_ambiguous_inventory_write_preserves_the_pending_event_identity(tmp_path):
    class Ambiguous(ReservationBlobs):
        once = True

        def upload(self, name, source):
            raw = source.read()
            super().upload(name, io.BytesIO(raw))
            if self.once and "/wal/" in name and b"model_request_started" in raw:
                self.once = False
                raise OSError("commit succeeded but acknowledgement was lost")

    blobs, oracle = Ambiguous(), HostSeen()
    blobs.fail = True
    async with harness(tmp_path / "old", blobs, ControlledModel()) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        response = await client.get(EVENTS, params={"after_seq": 0})
        oracle.observe(response.json()["events"])
        assert any(event["type"] == "model_request_started" for event in response.json()["events"])
        assert (
            sum(
                event["data"].get("kind") == "usage_unavailable"
                for event in response.json()["events"]
            )
            == 1
        )
    blobs.fail = False
    async with harness(tmp_path / "new", blobs) as (_, client):
        response = await client.get(EVENTS, params={"after_seq": 0})
        oracle.complete(response.json()["events"])


async def test_measured_usage_with_lost_ack_is_withheld_then_recovered_without_unknown(tmp_path):
    ready, release = asyncio.Event(), asyncio.Event()

    class Model:
        async def run(self, context, history):
            await context.emit(
                "model_request_started", {"request_id": "measured", "model": "gpt-6-luna"}
            )
            ready.set()
            await release.wait()
            await context.emit(
                "usage",
                dict(
                    request_id="measured",
                    model="gpt-6-luna",
                    input_tokens=1000,
                    cached_input_tokens=0,
                    output_tokens=100,
                    reasoning_tokens=0,
                ),
            )
            return history

    class LostAck(ReservationBlobs):
        unavailable = True

        def upload(self, name, source):
            raw = source.read()
            super().upload(name, io.BytesIO(raw))
            if self.unavailable and "/wal/" in name and b'"type":"usage"' in raw:
                raise OSError("measured usage committed, acknowledgement lost")

    from daimon_harness.errors import HarnessError

    blobs, oracle = LostAck(), HostSeen()
    blobs.fail = True
    async with harness(tmp_path / "old", blobs, Model()) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await asyncio.wait_for(ready.wait(), 5)
        oracle.observe((await client.get(EVENTS)).json()["events"])
        release.set()
        with pytest.raises(HarnessError, match="inventory"):
            await supervisor.tasks["turn-1"]
        assert not supervisor.contexts
        assert (await client.get(EVENTS)).status_code == 503
        assert not any(
            event.type == "usage"
            for event in await supervisor.io(supervisor.journal.events, SESSION)
        )
        frozen = ReservationBlobs()
        frozen.blobs = blobs.blobs.copy()
        # The frozen store represents a crash here, before the old process's
        # later recovery terminal. Keep these two possible futures separate.
        frozen_oracle = HostSeen()
        frozen_oracle.observe(list(oracle.events.values()))
        blobs.unavailable = False
        response = await client.get(EVENTS)
        oracle.complete(response.json()["events"])
    async with harness(tmp_path / "new", frozen) as (_, client):
        response = await client.get(EVENTS, params={"after_seq": 0})
        frozen_oracle.complete(response.json()["events"])
        assert not any(
            e["data"].get("kind") == "usage_unavailable" for e in response.json()["events"]
        )
        usage = [e for e in response.json()["events"] if e["type"] == "usage"]
        assert len(usage) == 1 and usage[0]["data"]["input_tokens"] == 1000


async def test_warning_reservation_failure_at_seq_1024_keeps_claim_owned(tmp_path):
    class FillBlock(ControlledModel):
        async def run(self, context, history):
            history = await super().run(context, history)
            await supervisor.io(supervisor.journal.record_cursor, SESSION, BLOCK - 2)
            return history

    blobs = ReservationBlobs()
    async with harness(tmp_path, blobs, FillBlock()) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        assert await supervisor.io(supervisor.journal.latest_seq, SESSION) == BLOCK
        await supervisor.flush_checkpoints()
        await supervisor.io(supervisor.checkpoint_notice, SESSION)
        supervisor.adapter = ControlledModel()
        blobs.fail_reservations = True
        result = await client.post("/v0/turns", json=turn(turn_id="warning", key="warning"))
        assert result.status_code == 202
        await asyncio.gather(supervisor.tasks["warning"], return_exceptions=True)
        blobs.fail_reservations = False
        assert (await client.get(EVENTS)).status_code == 200
        assert await supervisor.io(supervisor.journal.ended, "warning")
        assert (
            await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        ).status_code == 202
        await supervisor.tasks["next"]
        assert await supervisor.io(supervisor.journal.ended, "next")


@pytest.mark.parametrize("operation", ["reserved", "next", "gap"])
def test_all_sequence_readers_repair_missing_singleton(tmp_path, operation):
    journal = Journal(tmp_path)
    try:
        db = journal.connection(SESSION)
        with db:
            db.execute("DELETE FROM event_sequence")
        if operation == "reserved":
            assert journal.reserved_high_water(SESSION) == 0
        elif operation == "next":
            assert journal._next_seq(db) == 1
        else:
            journal._announce_gap(db, SESSION, "session")
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM event_sequence").fetchone()[0] == 1
    finally:
        journal.close()


def test_gap_reserves_even_when_imported_inventory_reaches_floor(tmp_path):
    from daimon_harness.checkpoints import Checkpoints
    from daimon_harness.errors import HarnessError

    journal = Journal(tmp_path)
    blobs = ReservationBlobs()
    try:
        journal.reserve = Checkpoints(blobs).reserve_sequences
        request = TurnRequest.model_validate(turn())
        journal.claim(request)
        journal.append(request, "status_running", {})
        db = journal.connection(SESSION)
        with db:
            db.execute("UPDATE event_sequence SET floor=2, gap_from=1, reserved_high_water=1")
        blobs.fail_reservations = True
        with pytest.raises(HarnessError, match="reservation"):
            journal.announce_gap(SESSION)
        assert journal.latest_seq(SESSION) == 1
        blobs.fail_reservations = False
        journal.announce_gap(SESSION)
        assert journal.events(SESSION)[-1].type == "history_gap"
        assert journal.reserved_high_water(SESSION) >= 2
    finally:
        journal.close()


@pytest.mark.parametrize("route", ["poll", "turn", "action", "cancel", "invocations"])
@pytest.mark.parametrize("cursor", [MAX_CURSOR, MAX_CURSOR - 1])
async def test_cursor_without_wire_sequence_headroom_is_rejected_before_ratchet(
    tmp_path, route, cursor
):
    async with harness(tmp_path, MemoryBlobs()) as (supervisor, client):
        if route == "poll":
            response = await client.get(EVENTS, params={"after_seq": cursor})
        elif route == "turn":
            response = await client.post("/v0/turns", json=turn(), params={"after_seq": cursor})
        elif route == "invocations":
            response = await client.post(
                "/invocations",
                json={
                    "c3": {
                        "method": "GET",
                        "path": EVENTS,
                        "query": {"after_seq": str(cursor)},
                    }
                },
            )
        else:
            response = await client.post(
                f"/v0/turns/turn-1/{'actions' if route == 'action' else 'cancel'}",
                params={"after_seq": cursor},
                json={"action_id": "missing", "decision": "allow"} if route == "action" else None,
            )
        assert response.status_code == 422, response.text
        assert await supervisor.io(supervisor.journal.client_cursor, SESSION) == 0
        assert (await client.post("/v0/turns", json=turn())).status_code == 202
        await supervisor.tasks["turn-1"]
        replay = await client.get(EVENTS)
        assert replay.status_code == 200 and replay.json()["complete"]


async def test_near_limit_emitted_cursor_round_trips_without_being_rejected_as_a_new_fence(
    tmp_path,
):
    async with harness(tmp_path, MemoryBlobs()) as (supervisor, client):
        assert (await client.post("/v0/turns", json=turn())).status_code == 202
        await supervisor.tasks["turn-1"]
        cursor = MAX_CURSOR - BLOCK - 1
        assert (
            await client.post(
                "/v0/turns", json=turn(turn_id="near", key="near"), params={"after_seq": cursor}
            )
        ).status_code == 202
        for _ in range(100):
            response = await client.get(EVENTS, params={"after_seq": cursor, "replay_after_seq": 0})
            assert response.status_code == 200, response.text
            events = response.json()["events"]
            assert all(event["seq"] <= MAX_CURSOR for event in events)
            cursor = max(cursor, *(event["seq"] for event in events))
            if response.json()["complete"]:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("near-limit turn did not complete")
        assert (await client.get(EVENTS, params={"after_seq": cursor})).status_code == 200
