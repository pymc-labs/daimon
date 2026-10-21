"""Blob recovery across complete sandbox loss preserves workspace and billing inventory."""

from __future__ import annotations

import asyncio
import io
import shutil
import tarfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import httpx
import pytest
from daimon_harness.checkpoint_archive import restore_work
from daimon_harness.checkpoints import Checkpoints
from daimon_harness.journal import BLOCK
from daimon_harness.runtime import TurnContext
from daimon_harness.service import create_app
from mux.drivers.selfhosted.transport import DirectTransport, sandbox_id
from pydantic import JsonValue

from .conftest import FakeModelAdapter, turn


class MemoryBlobs:
    def __init__(self):
        self.blobs = {}
        self.uploads = []
        self.fail = False

    def list(self, prefix):
        return [name for name in self.blobs if name.startswith(prefix)]

    def upload(self, name, source):
        if self.fail and name.endswith(".tar.zst"):
            raise RuntimeError("secret storage credential")
        self.blobs[name] = source.read()
        self.uploads.append(name)

    def download(self, name, target):
        target.write(self.blobs[name])

    def delete(self, name):
        del self.blobs[name]

    def close(self):
        pass


@asynccontextmanager
async def harness(home, blobs, model=None):
    app = create_app(
        home=home,
        token="test",
        adapter=model or FakeModelAdapter(),
        checkpoints=Checkpoints(blobs, keep=2),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            params={"session_id": "session-1"},
            headers={
                "Authorization": "Bearer test",
                "Accept": "application/json",
            },
        ) as client,
    ):
        yield app.state.supervisor, client


async def test_restore_after_sandbox_destruction_continues_seq_history_and_idempotency(tmp_path):
    blobs = MemoryBlobs()
    home = tmp_path / "home"
    async with harness(home, blobs) as (supervisor, client):
        assert (await client.post("/v0/turns", json=turn("tool"))).status_code == 202
        await supervisor.tasks["turn-1"]

        await supervisor.flush_checkpoints()
        workspace, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        (workspace / "work/result.txt").chmod(0o700)
        await supervisor.checkpoint("session-1")
        before = (await client.get("/v0/sessions/session-1/events")).json()
        history = await supervisor.io(supervisor.journal.history, "session-1")
        seq = before["events"][-1]["seq"]
        assert blobs.uploads[-1] == f"sessions/{sandbox_id('session-1')}/{seq}.tar.zst"
    shutil.rmtree(home)
    model = FakeModelAdapter()
    async with harness(home, blobs, model) as (supervisor, client):
        # Polling is the first request to a completely new sandbox.
        restored = (await client.get("/v0/sessions/session-1/events")).json()
        assert restored["events"][:-1] == before["events"]
        assert restored["complete"] == before["complete"]
        assert restored["events"][-1]["type"] == "history_gap"
        assert restored["events"][-1]["seq"] == BLOCK + 1
        workspace, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        assert (workspace / "work/result.txt").read_text() == "durable"
        assert (workspace / "work/result.txt").stat().st_mode & 0o777 == 0o700
        assert await supervisor.io(supervisor.journal.history, "session-1") == history
        assert (await client.post("/v0/turns", json=turn("tool"))).status_code == 202
        assert model.calls == 0
        assert (
            await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        ).status_code == 202
        await supervisor.tasks["next"]

        await supervisor.flush_checkpoints()
        after = (
            await client.get("/v0/sessions/session-1/events", params={"after_seq": seq})
        ).json()
        assert after["events"][0]["seq"] > seq
        assert model.histories[0] == history
        assert len([name for name in blobs.blobs if name.endswith(".tar.zst")]) == 2
        assert all(name.startswith(f"sessions/{sandbox_id('session-1')}/") for name in blobs.blobs)
        foreign = await client.get("/v0/sessions/foreign/events")
        assert foreign.status_code == 409


async def test_cancel_ack_waits_for_checkpoint_and_cold_cancel_restores(tmp_path):
    blobs = MemoryBlobs()
    home = tmp_path / "home"
    model = FakeModelAdapter()
    async with harness(home, blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn("wait"))
        await asyncio.wait_for(model.started.wait(), 5)
        await supervisor.flush_checkpoints()
        assert (await client.post("/v0/turns/turn-1/cancel")).json() == {"cancelled": True}
        before = (await client.get("/v0/sessions/session-1/events")).json()
        assert before["events"][-1]["data"]["outcome"] == "interrupted"
    shutil.rmtree(home)
    async with harness(home, blobs) as (supervisor, client):
        assert (await client.post("/v0/turns/turn-1/cancel")).json() == {"cancelled": False}
        restored = (await client.get("/v0/sessions/session-1/events")).json()
        assert restored["events"][:-1] == before["events"]
        assert restored["complete"] == before["complete"]
        assert restored["events"][-1]["type"] == "history_gap"
        assert restored["events"][-1]["seq"] == BLOCK + 1


async def test_crash_checkpoint_records_unknown_usage_and_never_reexecutes(tmp_path):
    class CrashWindow(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            await context.emit(
                "model_request_started", {"request_id": "pending", "model": "gpt-6-luna"}
            )
            self.started.set()
            await asyncio.Event().wait()
            return []

    blobs = MemoryBlobs()
    model = CrashWindow()
    home = tmp_path / "old"
    async with harness(home, blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await asyncio.wait_for(model.started.wait(), 5)
        await supervisor.flush_checkpoints()
        # Clone what Blob held before the process could commit terminal events.
        crash_blobs = MemoryBlobs()
        crash_blobs.blobs = blobs.blobs.copy()
    fresh_model = FakeModelAdapter()
    async with harness(tmp_path / "new", crash_blobs, fresh_model) as (supervisor, client):
        response = (await client.get("/v0/sessions/session-1/events")).json()
        unavailable = [
            event
            for event in response["events"]
            if event["type"] == "error" and event["data"]["kind"] == "usage_unavailable"
        ]
        assert [event["data"]["request_id"] for event in unavailable] == ["pending"]
        assert not [event for event in response["events"] if event["type"] == "usage"]
        assert response["complete"] is True
        assert response["events"][-1]["data"]["outcome"] == "interrupted"
        assert (await client.post("/v0/turns", json=turn())).status_code == 202
        assert fresh_model.calls == 0


async def test_failed_checkpoint_warns_without_blocking_polls_or_new_turns(tmp_path):
    blobs = MemoryBlobs()
    model = FakeModelAdapter()
    async with harness(tmp_path, blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn("wait"))
        await asyncio.wait_for(model.started.wait(), 5)
        await supervisor.flush_checkpoints()
        blobs.fail = True
        assert (await client.post("/v0/turns/turn-1/cancel")).status_code == 200
        await supervisor.flush_checkpoints()
        await supervisor.flush_checkpoints()
        response = await client.get("/v0/sessions/session-1/events")
        assert response.status_code == 200
        assert response.json()["complete"] is True
        assert "secret" not in response.text
        assert not any(e["type"] == "checkpoint_warning" for e in response.json()["events"])
        assert (
            await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        ).status_code == 202
        await supervisor.tasks["next"]
        assert (
            sum(
                e.type == "checkpoint_warning"
                for e in await supervisor.io(supervisor.journal.events, "session-1")
            )
            == 1
        )
        await supervisor.tasks["next"]

        await supervisor.flush_checkpoints()
        assert supervisor.checkpoint_pending == "session-1"
        blobs.fail = False
        await supervisor.checkpoint("session-1")
        assert supervisor.checkpoint_pending is None


async def test_restore_corruption_refuses_new_work_without_losing_remote_blob(tmp_path):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]

        await supervisor.flush_checkpoints()
    latest = max(
        (name for name in blobs.blobs if name.endswith(".tar.zst")),
        key=lambda name: int(name.rsplit("/", 1)[1].split(".")[0]),
    )
    blobs.blobs[latest] = b"corrupt"
    model = FakeModelAdapter()
    async with harness(tmp_path / "new", blobs, model) as (supervisor, client):
        assert (await client.post("/v0/turns", json=turn())).status_code == 503
        assert model.calls == 0
        assert blobs.blobs[latest] == b"corrupt"


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside", tarfile.REGTYPE),
        ("/outside", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hard", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
    ],
)
def test_restore_rejects_unsafe_workspace_members(tmp_path, name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/outside"
        tar.addfile(info)
    stream.seek(0)
    with pytest.raises(ValueError, match="unsafe"):
        restore_work(stream, tmp_path / "work")
    assert not (tmp_path / "outside").exists()


async def test_transport_supplies_session_context_for_cold_actions_and_cancel():
    def respond(request):
        assert request.url.params["session_id"] == "session-1"
        return httpx.Response(200, json={"cancelled": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = DirectTransport(client, base_url="https://harness.example", token="test")
        await transport.request("POST", "/v0/turns/old/cancel", after_seq=0, session_id="session-1")


async def test_authentication_and_session_binding_precede_any_restore(tmp_path):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]

        await supervisor.flush_checkpoints()
    async with harness(tmp_path / "new", blobs) as (supervisor, client):
        response = await client.post(
            "/v0/turns/turn-1/cancel", headers={"Authorization": "Bearer wrong"}
        )
        assert response.status_code == 401
        assert supervisor.restored is False
        assert not await supervisor.io(supervisor.journal.turns)
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 200
        response = await client.post("/v0/turns/turn-1/cancel", params={"session_id": "other"})
        assert response.status_code == 409
        response = await client.post("/v0/turns", json=turn(), params={"session_id": "other"})
        assert response.status_code == 409
        response = await client.post(
            "/v0/turns/turn-1/actions", json={"action_id": "old", "decision": "allow"}
        )
        assert response.status_code == 404


async def test_actions_and_cancel_cannot_target_a_foreign_sessions_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SESSION_MODE", "multi")
    model = FakeModelAdapter()
    app = create_app(home=tmp_path, token="test", adapter=model)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test", "Accept": "application/json"},
        ) as client,
    ):
        await client.post("/v0/turns", json=turn("wait"))
        await asyncio.wait_for(model.started.wait(), 5)
        await app.state.supervisor.flush_checkpoints()
        for operation, body in [
            ("cancel", {}),
            ("actions", {"action_id": "x", "decision": "allow"}),
        ]:
            response = await client.post(
                f"/v0/turns/turn-1/{operation}", json=body, params={"session_id": "other"}
            )
            assert response.status_code == 409
        assert not app.state.supervisor.tasks["turn-1"].done()
        assert (
            await client.post("/v0/turns/turn-1/cancel", params={"session_id": "session-1"})
        ).json() == {"cancelled": True}


async def test_interrupted_restore_retries_before_serving(tmp_path, monkeypatch):
    from pathlib import Path

    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn("tool"))
        await supervisor.tasks["turn-1"]

        await supervisor.flush_checkpoints()
        expected = await supervisor.io(supervisor.journal.events, "session-1")
    original = Path.replace
    failed = False

    def interrupt(path, destination):
        nonlocal failed
        if path.name == "journal.sqlite3" and not failed:
            failed = True
            raise OSError("publication interrupted")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", interrupt)
    async with harness(tmp_path / "new", blobs) as (supervisor, client):
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 503
        assert (supervisor.journal.root / "restore.pending").exists()
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 200
        actual = await supervisor.io(supervisor.journal.events, "session-1")
        assert actual[:-1] == expected
        assert actual[-1].type == "history_gap" and actual[-1].seq == BLOCK + 1
        assert not (supervisor.journal.root / "restore.pending").exists()
        workspace, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        assert (workspace / "work/result.txt").read_text() == "durable"


async def test_workspace_link_cannot_export_service_files(tmp_path):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "home", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn("tool"))
        await supervisor.tasks["turn-1"]

        await supervisor.flush_checkpoints()
        workspace, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        secret = tmp_path / "secret"
        secret.write_text("service credential")
        (workspace / "work/link").symlink_to(secret)
        before = blobs.blobs.copy()
        await supervisor.checkpoint("session-1")
        assert blobs.blobs != before
        assert (supervisor.journal.root / "checkpoint.notice").exists()
        await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        await supervisor.tasks["next"]
        events = await supervisor.io(supervisor.journal.events, "session-1")
        warning = next(event for event in events if event.type == "checkpoint_warning")
        assert warning.turn_id == "next"
        assert all(b"service credential" not in data for data in blobs.blobs.values())


async def test_failed_upload_does_not_block_after_local_process_restart(tmp_path):
    blobs = MemoryBlobs()
    home = tmp_path / "home"
    model = FakeModelAdapter()
    async with harness(home, blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn("wait"))
        await asyncio.wait_for(model.started.wait(), 5)
        await supervisor.flush_checkpoints()
        blobs.fail = True
        assert (await client.post("/v0/turns/turn-1/cancel")).status_code == 200
        await supervisor.flush_checkpoints()
        assert (supervisor.journal.root / "checkpoint.pending").read_text() == "session-1"
    async with harness(home, blobs) as (supervisor, client):
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 200
        blobs.fail = False
        await supervisor.checkpoint("session-1")
        assert (await client.get("/v0/sessions/session-1/events")).json()["complete"] is True
        assert not (supervisor.journal.root / "checkpoint.pending").exists()


@pytest.mark.parametrize(
    "account",
    [
        "http://storage.example",
        "https://storage.example?sig=secret",
        "https://user:secret@storage.example",
    ],
)
def test_checkpoint_account_rejects_insecure_or_secret_carriers(account):
    from daimon_harness.checkpoints import AzureCheckpointStore

    with pytest.raises(ValueError, match="HTTPS account origin"):
        AzureCheckpointStore(account, "checkpoints")


def test_checkpoint_identity_refuses_imds_fallback(monkeypatch):
    from daimon_harness.checkpoints import AzureCheckpointStore

    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    with pytest.raises(RuntimeError, match="header-protected"):
        AzureCheckpointStore("https://storage.example", "checkpoints")


async def test_process_restart_during_restore_does_not_checkpoint_partial_state(
    tmp_path, monkeypatch
):
    from pathlib import Path

    class WaitingModel(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            await context.emit(
                "model_request_started", {"request_id": "pending", "model": "gpt-6-luna"}
            )
            self.started.set()
            await asyncio.Event().wait()
            return []

    blobs = MemoryBlobs()
    model = WaitingModel()
    async with harness(tmp_path / "old", blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await asyncio.wait_for(model.started.wait(), 5)
        await supervisor.checkpoint("session-1")
        crash_blobs = MemoryBlobs()
        crash_blobs.blobs = blobs.blobs.copy()
    original = Path.replace
    interrupted = False

    def interrupt(path, destination):
        nonlocal interrupted
        if path.name == "journal.sqlite3" and not interrupted:
            interrupted = True
            raise OSError("process interrupted")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", interrupt)
    home = tmp_path / "new"
    async with harness(home, crash_blobs) as (supervisor, client):
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 503
    before = crash_blobs.blobs.copy()
    async with harness(home, crash_blobs) as (supervisor, client):
        # Startup has not published a checkpoint of partially restored state.
        assert crash_blobs.blobs == before
        events = (await client.get("/v0/sessions/session-1/events")).json()["events"]
        assert any(event["type"] == "model_request_started" for event in events)
        assert any(
            event["data"].get("request_id") == "pending"
            and event["data"].get("kind") == "usage_unavailable"
            for event in events
        )
        assert events[-1]["type"] == "turn_ended"


def test_real_managed_identity_retains_endpoint_and_header_after_scrub_and_refresh(monkeypatch):
    import json
    import socket
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit

    from daimon_harness.checkpoints import AzureCheckpointStore

    received = []
    connections = []
    connect = socket.socket.connect

    def intercepted(sock, address):
        connections.append(address)
        assert address[0] in ("127.0.0.1", "::1"), address
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", intercepted)
    now = time.time()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("X-IDENTITY-HEADER")))
            body = json.dumps(
                {
                    "access_token": f"mock-access-{len(received)}",
                    "expires_on": int(now + 3600),
                    "resource": "https://storage.azure.com",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("IDENTITY_ENDPOINT", f"http://127.0.0.1:{server.server_port}/identity")
    monkeypatch.setenv("IDENTITY_HEADER", "protected-identity-header")
    monkeypatch.setenv("AZURE_CLIENT_ID", "configured-identity")
    store = AzureCheckpointStore("https://storage.example", "checkpoints")
    try:
        for name in ("IDENTITY_ENDPOINT", "IDENTITY_HEADER", "AZURE_CLIENT_ID"):
            monkeypatch.delenv(name)
        assert (
            store.credential.get_token("https://storage.azure.com/.default").token
            == "mock-access-1"
        )
        with monkeypatch.context() as clock:
            clock.setattr(time, "time", lambda: now + 7200)
            assert (
                store.credential.get_token("https://storage.azure.com/.default").token
                == "mock-access-2"
            )
        assert connections
        assert all(address[0] != "169.254.169.254" for address in connections)
        assert len(received) == 2
        for path, header in received:
            assert urlsplit(path).path == "/identity"
            assert header == "protected-identity-header"
            assert parse_qs(urlsplit(path).query) == {
                "api-version": ["2019-08-01"],
                "resource": ["https://storage.azure.com"],
                "client_id": ["configured-identity"],
            }
    finally:
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_real_driver_resumes_beyond_published_cursor_after_sandbox_loss(tmp_path):
    from .interop_support import SCOPE, collect, driver_session, message

    class StreamingModel(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            await context.emit(
                "model_request_started", {"request_id": "pending", "model": context.request.model}
            )
            await active.state.supervisor.checkpoint(context.request.session_id)
            for index in range(7):
                await context.emit(
                    "agent_message_delta", {"message_id": "streamed", "text": str(index)}
                )
            self.started.set()
            await asyncio.Event().wait()
            return []

    blobs = MemoryBlobs()
    model = StreamingModel()
    active = create_app(
        home=tmp_path / "old", token="test-token", adapter=model, checkpoints=Checkpoints(blobs)
    )

    async def route(request):
        assert "after_seq" in request.url.params
        return await httpx.ASGITransport(app=active).handle_async_request(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(route), base_url="http://localhost"
    ) as client:
        driver, session = await driver_session(client)
        try:
            async with active.router.lifespan_context(active):
                await driver.events.send(SCOPE, session.ref, [message("hello")], key="send")
                await asyncio.wait_for(model.started.wait(), 5)
                await active.state.supervisor.flush_checkpoints()
                source = await driver.events.open_stream(
                    SCOPE, session.ref, after="0", previews=True
                )
                try:
                    seen = [await anext(source) for _ in range(9)]
                finally:
                    assert isinstance(source, AsyncGenerator)
                    await source.aclose()
                cursor = seen[-1].sequence
                assert cursor == 9
                crash_blobs = MemoryBlobs()
                crash_blobs.blobs = blobs.blobs.copy()
                assert (
                    max(
                        int(name.rsplit("/", 1)[1].split(".")[0])
                        for name in crash_blobs.blobs
                        if name.endswith(".tar.zst")
                    )
                    < cursor
                )
            active = create_app(
                home=tmp_path / "new",
                token="test-token",
                adapter=FakeModelAdapter(),
                checkpoints=Checkpoints(crash_blobs),
            )
            async with active.router.lifespan_context(active):
                remaining = await collect(driver, session, after=str(cursor))
                assert remaining[0].type == "session.history_gap"
                assert remaining[0].authority == "gap"
                assert remaining[0].payload["after"] == "2"
                assert remaining[-1].type == "session.turn_ended"
                assert remaining[-1].payload["outcome"] == "interrupted"
                assert all(event.sequence > cursor for event in remaining)
                assert not {event.id for event in seen} & {event.id for event in remaining}
                snapshot = await driver.events.reconcile(SCOPE, session.ref)
                assert snapshot.state == "idle"
                assert snapshot.gaps == (remaining[0].id,)
                usage = await driver.usage.reconcile(SCOPE, session.ref)
                assert len(usage) == 1
                assert usage[0].completeness == "unknown"
        finally:
            await driver.aclose()


def test_degraded_archive_skips_caps_special_and_unreadable_entries(tmp_path, monkeypatch):
    import os

    from daimon_harness import checkpoint_archive as archive

    work = tmp_path / "work"
    work.mkdir()
    (work / "executable").write_text("run")
    (work / "executable").chmod(0o7700)
    (work / "oversized").write_bytes(b"x" * 100)
    (work / "link").symlink_to("executable")
    os.mkfifo(work / "pipe")
    (work / "deep").mkdir()
    (work / "deep/nested").mkdir()
    (work / "deep/nested/file").write_text("too deep")
    monkeypatch.setattr(archive, "MAX_BYTES", 10)
    monkeypatch.setattr(archive, "MAX_DEPTH", 0)
    output = io.BytesIO()
    report = archive.export_work(work, output)
    assert report["degraded"]
    reasons = {item["reason"] for item in cast(list[dict[str, str]], report["skipped"])}
    assert {"size_cap", "depth_cap", "symlink", "unsupported_type"} <= reasons
    output.seek(0)
    archive.restore_work(output, tmp_path / "restored")
    assert (tmp_path / "restored/executable").stat().st_mode & 0o7777 == 0o700
    monkeypatch.setattr(archive, "MAX_ENTRIES", 1)
    assert archive.export_work(work, io.BytesIO())["degraded"]


@pytest.mark.parametrize("operation", ["turns", "actions", "cancel"])
async def test_cold_post_cursor_fences_recovery_before_acting(tmp_path, operation):
    class Pending(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            await context.emit(
                "model_request_started", {"request_id": "pending", "model": "gpt-6-luna"}
            )
            self.started.set()
            await asyncio.Event().wait()
            return []
            return []

    blobs = MemoryBlobs()
    model = Pending()
    async with harness(tmp_path / "old", blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await asyncio.wait_for(model.started.wait(), 5)
        await supervisor.flush_checkpoints()
        crash_blobs = MemoryBlobs()
        crash_blobs.blobs = blobs.blobs.copy()
    async with harness(tmp_path / "new", crash_blobs) as (supervisor, client):
        if operation == "turns":
            result = await client.post("/v0/turns", json=turn(), params={"after_seq": 9})
            assert result.status_code == 202
        else:
            result = await client.post(
                f"/v0/turns/turn-1/{operation}",
                json={"action_id": "lost", "decision": "allow"} if operation == "actions" else {},
                params={"after_seq": 9},
            )
            assert result.status_code == (404 if operation == "actions" else 200)
        response = (
            await client.get("/v0/sessions/session-1/events", params={"after_seq": 9})
        ).json()
        assert response["complete"]
        assert response["events"][0]["seq"] == BLOCK + 1
        assert response["events"][0]["type"] == "history_gap"
        assert response["events"][-1]["data"]["error"]["kind"] == "sandbox_lost"
        assert any(e["data"].get("kind") == "usage_unavailable" for e in response["events"])


async def test_fresh_driver_uses_persisted_projection_and_delivers_next_turn_usage(tmp_path):
    from mux.contracts.resources import ProjectionSnapshot

    from .interop_support import SCOPE, driver_session, message

    blobs = MemoryBlobs()
    active = create_app(
        home=tmp_path / "old",
        token="test-token",
        adapter=FakeModelAdapter(),
        checkpoints=Checkpoints(blobs),
    )
    requests = []

    async def route(request):
        requests.append(request)
        return await httpx.ASGITransport(app=active).handle_async_request(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(route), base_url="http://localhost"
    ) as client:
        driver, session = await driver_session(client)
        async with active.router.lifespan_context(active):
            await driver.events.send(SCOPE, session.ref, [message("first")], key="first")
            await asyncio.gather(*active.state.supervisor.tasks.values())
            await active.state.supervisor.flush_checkpoints()
            assert (
                max(
                    int(name.rsplit("/", 1)[1].split(".")[0])
                    for name in blobs.blobs
                    if name.endswith(".tar.zst")
                )
                == 6
            )
            blobs.fail = True
            await driver.events.send(SCOPE, session.ref, [message("second")], key="second")
            await asyncio.gather(*active.state.supervisor.tasks.values())
            await active.state.supervisor.flush_checkpoints()
            projection = await driver.events.reconcile(SCOPE, session.ref)
            assert projection.cursor == "12"
            persisted = tmp_path / "projection.json"
            persisted.write_text(projection.model_dump_json())
        await driver.aclose()
        blobs.fail = False

        async def high_water(ref):
            assert ref == session.ref
            return int(ProjectionSnapshot.model_validate_json(persisted.read_text()).cursor)

        fresh, resumed = await driver_session(client, high_water=high_water)
        active = create_app(
            home=tmp_path / "new",
            token="test-token",
            adapter=FakeModelAdapter(),
            checkpoints=Checkpoints(blobs),
        )
        requests.clear()
        async with active.router.lifespan_context(active):
            await fresh.events.replay(SCOPE, resumed.ref)
            assert requests[0].url.params["after_seq"] == "12"
            source = await fresh.events.open_stream(SCOPE, resumed.ref, after="12", previews=True)
            assert requests[0].url.params["after_seq"] == "12"
            await fresh.events.send(SCOPE, resumed.ref, [message("third")], key="third")
            async with asyncio.timeout(5):
                received = [event async for event in source]
            assert received[-1].type == "session.turn_ended"
            assert all(event.sequence > 12 for event in received)
            usage = await fresh.usage.reconcile(SCOPE, resumed.ref)
            assert {item.completeness for item in usage} == {"measured"}
            assert any(item.turn_id == received[-1].turn_id for item in usage)
        await fresh.aclose()


async def test_manifest_bytes_and_unsupported_names_remain_restorable(tmp_path):
    import json

    import zstandard
    from daimon_harness.checkpoints import MAX_MANIFEST_BYTES

    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        sandbox, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        nested = sandbox / "work"
        for _ in range(4):
            nested /= "界" * 70
            nested.mkdir()
        for index in range(100):
            (nested / str(index)).symlink_to("absent")
        (sandbox / "work" / "ordinary\\filename").write_text("unsupported")
        await supervisor.checkpoint("session-1")
        name = max(
            (name for name in blobs.blobs if name.endswith(".tar.zst")),
            key=lambda n: int(n.rsplit("/", 1)[1].split(".")[0]),
        )
        with (
            zstandard.ZstdDecompressor().stream_reader(io.BytesIO(blobs.blobs[name])) as source,
            tarfile.open(fileobj=source, mode="r|") as archive,
        ):
            for member in archive:
                if member.name == "manifest.json":
                    content = archive.extractfile(member)
                    assert content is not None
                    encoded = content.read()
                    assert len(encoded) <= MAX_MANIFEST_BYTES
                    manifest = json.loads(encoded)
                    assert manifest["workspace"]["skipped_count"] == 101
    async with harness(tmp_path / "new", blobs) as (_, client):
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 200


@pytest.mark.parametrize("restore_cursor", [0, 9])
async def test_restore_fence_survives_crash_before_supervisor_recovery(tmp_path, restore_cursor):
    from daimon_harness.journal import Journal

    class Waiting(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            await context.emit(
                "model_request_started", {"request_id": "lost", "model": "gpt-6-luna"}
            )
            self.started.set()
            await asyncio.Event().wait()
            return []

    blobs = MemoryBlobs()
    model = Waiting()
    async with harness(tmp_path / "old", blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await model.started.wait()
        await supervisor.flush_checkpoints()
        crash = MemoryBlobs()
        crash.blobs = blobs.blobs.copy()
    local = Checkpoints(crash).restore(
        Journal(tmp_path / "new"), "session-1", after_seq=restore_cursor
    )
    assert (local.root / "restore.cursor").exists()
    local.close()  # Process dies after publication, before prepare_session/recover.
    async with harness(tmp_path / "new", crash) as (supervisor, client):
        assert not await supervisor.io(supervisor.journal.ended, "turn-1")
        result = (await client.get("/v0/sessions/session-1/events", params={"after_seq": 9})).json()
        assert result["events"][0]["type"] == "history_gap"
        assert result["events"][0]["seq"] == BLOCK + 1
        assert result["events"][-1]["data"]["error"]["kind"] == "sandbox_lost"
        assert result["complete"]


async def test_held_checkpoint_upload_does_not_block_poll_cancel_or_next_turn(tmp_path):
    import threading

    class Held(MemoryBlobs):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def upload(self, name, source):
            if name.endswith(".tar.zst"):
                self.entered.set()
                assert self.release.wait(10)
            super().upload(name, source)

    blobs = Held()
    async with harness(tmp_path, blobs) as (supervisor, client):
        try:
            await client.post("/v0/turns", json=turn())
            assert await asyncio.to_thread(blobs.entered.wait, 5)
            async with asyncio.timeout(2):
                await supervisor.tasks["turn-1"]
                assert (await client.get("/v0/sessions/session-1/events")).json()["complete"]
                assert (
                    await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
                ).status_code == 202
                await supervisor.tasks["next"]
                assert (await client.post("/v0/turns/next/cancel")).status_code == 200
            assert not blobs.release.is_set()
        finally:
            blobs.release.set()
        await supervisor.flush_checkpoints()


async def test_combined_archive_budget_degrades_workspace_and_keeps_journals(tmp_path, monkeypatch):
    from daimon_harness import checkpoints as module

    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        await supervisor.flush_checkpoints()
        sandbox, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        (sandbox / "work/large").write_bytes(b"a" * (2 * 1024 * 1024))
        monkeypatch.setattr(module, "MAX_ARCHIVE_BYTES", 2 * 1024 * 1024)
        await supervisor.checkpoint("session-1")
        assert supervisor.checkpoint_pending is None
    async with harness(tmp_path / "new", blobs) as (supervisor, client):
        result = await client.get("/v0/sessions/session-1/events")
        assert result.status_code == 200 and result.json()["complete"]
        sandbox, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        assert not (sandbox / "work/large").exists()


@pytest.mark.parametrize("cursor", [str(2**53 + 1), "-1"])
async def test_cursor_outside_safe_integer_range_is_rejected(tmp_path, cursor):
    async with harness(tmp_path, MemoryBlobs()) as (_, client):
        assert (
            await client.get("/v0/sessions/session-1/events", params={"after_seq": cursor})
        ).status_code == 422


async def test_clean_restore_announces_abandoned_reservation_to_projection(tmp_path):
    from .interop_support import SCOPE, driver_session, message

    blobs = MemoryBlobs()
    active = create_app(
        home=tmp_path / "old",
        token="test-token",
        adapter=FakeModelAdapter(),
        checkpoints=Checkpoints(blobs),
    )

    async def route(request):
        return await httpx.ASGITransport(app=active).handle_async_request(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(route), base_url="http://localhost"
    ) as client:
        driver, session = await driver_session(client)
        async with active.router.lifespan_context(active):
            await driver.events.send(SCOPE, session.ref, [message("first")], key="first")
            await asyncio.gather(*active.state.supervisor.tasks.values())
            await active.state.supervisor.flush_checkpoints()
            before = await driver.events.reconcile(SCOPE, session.ref)
        await driver.aclose()
        active = create_app(
            home=tmp_path / "new",
            token="test-token",
            adapter=FakeModelAdapter(),
            checkpoints=Checkpoints(blobs),
        )
        fresh, resumed = await driver_session(client)
        async with active.router.lifespan_context(active):
            after = await fresh.events.reconcile(SCOPE, resumed.ref)
            assert before.gaps == () and len(after.gaps) == 1
            assert after.state == before.state == "idle"
            assert int(after.cursor) == BLOCK + 1 > int(before.cursor)
        await fresh.aclose()


async def test_snapshot_failure_does_not_fail_the_turn(tmp_path, monkeypatch):
    blobs = MemoryBlobs()
    async with harness(tmp_path, blobs) as (supervisor, client):

        def failed(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(supervisor.checkpoints, "capture", failed)
        assert (await client.post("/v0/turns", json=turn())).status_code == 202
        await supervisor.tasks["turn-1"]
        await supervisor.flush_checkpoints()
        events = (await client.get("/v0/sessions/session-1/events")).json()["events"]
        assert (
            next(event for event in events if event["type"] == "turn_ended")["data"]["outcome"]
            == "completed"
        )
        assert (supervisor.journal.root / "checkpoint.notice").exists()
        await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        await supervisor.tasks["next"]
        assert (
            sum(
                e.type == "checkpoint_warning"
                for e in await supervisor.io(supervisor.journal.events, "session-1")
            )
            == 1
        )


@pytest.mark.parametrize("foundry", [False, True])
async def test_old_baseline_with_newer_high_water_replays_without_false_gap(tmp_path, foundry):
    from mux.drivers.selfhosted.wire import TurnRequest

    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, _):
        request = TurnRequest.model_validate(turn())

        def populate():
            supervisor.journal.claim(request)
            supervisor.journal.append(request, "status_running", {})
            for index in range(1000):
                supervisor.journal.append(
                    request,
                    "agent_message_delta",
                    {
                        "message_id": "long-message",
                        "text": str(index),
                    },
                )
            supervisor.journal.append(request, "turn_ended", {"outcome": "completed"})

        await supervisor.io(populate)
        await supervisor.checkpoint("session-1")
    async with harness(tmp_path / "old", blobs) as (_, client):
        query = {"after_seq": 1002, "replay_after_seq": 990, "session_id": "session-1"}
        if foundry:
            response = await client.post(
                "/invocations",
                json={
                    "c3": {
                        "method": "GET",
                        "path": "/v0/sessions/session-1/events",
                        "query": query,
                    }
                },
            )
        else:
            response = await client.get("/v0/sessions/session-1/events", params=query)
        assert response.status_code == 200
        events = response.json()["events"]
        assert [event["seq"] for event in events if event["type"] != "history_gap"] == list(
            range(991, 1003)
        )
        gap = next(event for event in events if event["type"] == "history_gap")
        assert gap["data"] == {"from_seq": 1003, "to_seq": BLOCK, "reason": "sandbox_restored"}
        clamped = await client.get(
            "/v0/sessions/session-1/events",
            params={
                "after_seq": 1002,
                "replay_after_seq": 2000,
            },
        )
        assert clamped.json()["events"] == [gap]
        negative = await client.get(
            "/v0/sessions/session-1/events",
            params={
                "after_seq": 1002,
                "replay_after_seq": -1,
            },
        )
        assert negative.status_code == 422


@pytest.mark.parametrize("foundry", [False, True])
async def test_restore_replay_range_does_not_lower_the_high_water_fence(tmp_path, foundry):
    from daimon_harness.journal import Journal
    from mux.drivers.selfhosted.wire import TurnRequest

    blobs = MemoryBlobs()
    journal = Journal(tmp_path / "old")
    request = TurnRequest.model_validate(turn())
    journal.bind_session("session-1")
    journal.claim(request)
    journal.append(request, "status_running", {})
    journal.append(request, "model_request_started", {"request_id": "lost", "model": "gpt-6-luna"})
    Checkpoints(blobs).save(journal, "session-1")
    journal.close()
    async with harness(tmp_path / "new", blobs) as (_, client):
        query = {"after_seq": 9, "replay_after_seq": 1, "session_id": "session-1"}
        if foundry:
            response = await client.post(
                "/invocations",
                json={
                    "c3": {
                        "method": "GET",
                        "path": "/v0/sessions/session-1/events",
                        "query": query,
                    }
                },
            )
        else:
            response = await client.get("/v0/sessions/session-1/events", params=query)
        assert response.status_code == 200
        events = response.json()["events"]
        assert events[0]["seq"] == 2
        gap = next(event for event in events if event["type"] == "history_gap")
        assert gap["seq"] == 10 and gap["data"]["from_seq"] == 3 and gap["data"]["to_seq"] == 9
        assert all(event["seq"] > 9 for event in events[1:])
        assert events[-1]["data"]["error"]["kind"] == "sandbox_lost"


async def test_observed_session_without_any_checkpoint_cannot_reuse_sequences(tmp_path):
    async with harness(tmp_path, MemoryBlobs()) as (supervisor, client):
        response = await client.post("/v0/turns", json=turn(), params={"after_seq": 13})
        assert response.status_code == 202, response.text
        await supervisor.tasks["turn-1"]
        response = await client.get("/v0/sessions/session-1/events", params={"after_seq": 13})
        assert response.status_code == 200, response.text
        events = response.json()["events"]
        gap = next(event for event in events if event["type"] == "history_gap")
        assert gap["seq"] > 13 and gap["data"]["from_seq"] == 1 and gap["data"]["to_seq"] >= 13
        assert all(event["seq"] > 13 for event in events)
        assert events[-1]["type"] == "turn_ended"
        response = await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        assert response.status_code == 202, response.text
        await supervisor.tasks["next"]


async def test_remote_write_ahead_prevents_resend_of_a_turn_missing_its_checkpoint(tmp_path):
    blobs = MemoryBlobs()
    model = FakeModelAdapter()
    async with harness(tmp_path / "old", blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        await supervisor.flush_checkpoints()
        checkpoint_names = {name for name in blobs.blobs if name.endswith(".tar.zst")}
        assert checkpoint_names
        blobs.fail = True  # Full checkpoints fail; small write-ahead records still commit.
        body = turn("tool", turn_id="paid", key="paid")
        await client.post("/v0/turns", json=body)
        await supervisor.tasks["paid"]
        await supervisor.flush_checkpoints()
        assert model.calls == 2
        workspace, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        assert (workspace / "work/result.txt").read_text() == "durable"
        assert await supervisor.io(supervisor.journal.ended, "paid")
        assert {name for name in blobs.blobs if name.endswith(".tar.zst")} == checkpoint_names
        seq = await supervisor.io(supervisor.journal.latest_seq, "session-1")
    shutil.rmtree(tmp_path / "old")
    blobs.fail = False
    fresh = FakeModelAdapter()
    async with harness(tmp_path / "new", blobs, fresh) as (supervisor, client):
        assert (
            await client.post("/v0/turns", json=body, params={"after_seq": seq})
        ).status_code == 202
        assert fresh.calls == 0 and not supervisor.tasks
        events = await supervisor.io(supervisor.journal.events, "session-1")
        recovered = [
            event for event in events if event.turn_id == "paid" and event.type != "history_gap"
        ]
        assert recovered[-1].type == "turn_ended"
        assert recovered[-1].data["outcome"] == "completed"
        assert any(event.type == "usage" for event in recovered)
        assert not any(event.data.get("kind") == "usage_unavailable" for event in recovered)
        workspace, _ = await supervisor.io(supervisor.journal.sandbox, "session-1")
        assert not (workspace / "work/result.txt").exists()
        changed = body | {"instructions": "changed"}
        assert (await client.post("/v0/turns", json=changed)).status_code == 409
        assert fresh.calls == 0


async def test_failed_remote_write_ahead_refuses_paid_work(tmp_path):
    class FailedWal(MemoryBlobs):
        unavailable = True

        def upload(self, name, source):
            if self.unavailable and "/wal/" in name:
                raise OSError("unavailable")
            super().upload(name, source)

    model = FakeModelAdapter()
    blobs = FailedWal()
    async with harness(tmp_path, blobs, model) as (supervisor, client):
        from daimon_harness.errors import HarnessError

        await client.post("/v0/turns", json=turn("tool"))
        with pytest.raises(HarnessError, match="inventory"):
            await supervisor.tasks["turn-1"]
        assert model.calls == 0
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 503
        blobs.unavailable = False
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 200
        terminal = next(
            event
            for event in await supervisor.io(supervisor.journal.events, "session-1")
            if event.type == "turn_ended"
        )
        assert terminal.data["outcome"] == "errored"
        assert terminal.data["error"] == {
            "kind": "durability_unavailable",
            "message": "Remote write-ahead is unavailable",
            "retryable": True,
        }


async def test_cleanup_cancel_transport_carries_the_persisted_high_water(tmp_path):
    from daimon_harness.journal import Journal
    from mux.drivers.selfhosted.wire import EventsResponse, TurnRequest

    blobs = MemoryBlobs()
    journal = Journal(tmp_path / "old")
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    journal.append(request, "status_running", {})
    journal.append(request, "model_request_started", {"request_id": "lost", "model": "gpt-6-luna"})
    Checkpoints(blobs).save(journal, "session-1")
    assert journal.latest_seq("session-1") == 2
    journal.close()
    async with harness(tmp_path / "new", blobs) as (_, client):
        client.params = {}
        transport = DirectTransport(client, base_url="http://localhost", token="test")
        await transport.request(
            "POST", "/v0/turns/turn-1/cancel", session_id="session-1", after_seq=8
        )
        response = await transport.request(
            "GET", "/v0/sessions/session-1/events", session_id="session-1", after_seq=8
        )
        events = EventsResponse.model_validate(response).events
        assert events[0].seq == 9 and events[0].type == "history_gap"
        assert all(event.seq > 8 for event in events)
        assert any(event.data.get("kind") == "usage_unavailable" for event in events)
        assert events[-1].data["outcome"] == "interrupted"


async def test_write_ahead_failure_before_second_model_request_stops_that_request(tmp_path):
    class TwoRequests(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            for index in range(2):
                await context.emit(
                    "model_request_started", {"request_id": str(index), "model": "gpt-6-luna"}
                )
                self.calls += 1  # Paid I/O happens only after the write-ahead barrier.
                await context.emit(
                    "usage",
                    {
                        "request_id": str(index),
                        "model": "gpt-6-luna",
                        "input_tokens": 1,
                        "cached_input_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_tokens": 0,
                    },
                )
            return []

    class FailSecondRequest(MemoryBlobs):
        unavailable = True

        def upload(self, name, source):
            raw = source.read()
            if self.unavailable and "/wal/" in name and b'"request_id":"1"' in raw:
                raise OSError("unavailable")
            super().upload(name, io.BytesIO(raw))

    model = TwoRequests()
    store = FailSecondRequest()
    async with harness(tmp_path, store, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        from daimon_harness.errors import HarnessError

        with pytest.raises(HarnessError, match="inventory"):
            await supervisor.tasks["turn-1"]
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 503
        store.unavailable = False
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 200
        assert model.calls == 1
        events = await supervisor.io(supervisor.journal.events, "session-1")
        assert (
            sum(
                event.data.get("kind") == "usage_unavailable"
                and event.data.get("request_id") == "1"
                for event in events
            )
            == 1
        )
        terminal = next(event for event in events if event.type == "turn_ended")
        assert terminal.data["error"]["kind"] == "process_interrupted"


async def test_remote_inventory_recovery_retries_after_local_publication_crash(
    tmp_path, monkeypatch
):
    from daimon_harness.journal import Journal

    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        await supervisor.flush_checkpoints()
        blobs.fail = True
        await client.post("/v0/turns", json=turn("tool", turn_id="paid", key="paid"))
        await supervisor.tasks["paid"]
        await supervisor.flush_checkpoints()
    blobs.fail = False
    original = Journal.remember_recovery
    interrupted = False

    def crash_after_claim(journal, request, digest):
        nonlocal interrupted
        original(journal, request, digest)
        if not interrupted:
            interrupted = True
            raise OSError("crash before inventory publication")

    monkeypatch.setattr(Journal, "remember_recovery", crash_after_claim)
    home = tmp_path / "new"
    async with harness(home, blobs) as (_, client):
        assert (
            await client.get("/v0/sessions/session-1/events", params={"after_seq": 13})
        ).status_code == 503
    async with harness(home, blobs) as (supervisor, client):
        result = await client.get("/v0/sessions/session-1/events", params={"after_seq": 13})
        assert result.status_code == 200
        events = await supervisor.io(supervisor.journal.events, "session-1")
        assert any(event.turn_id == "paid" and event.type == "usage" for event in events)
        assert await supervisor.io(supervisor.journal.ended, "paid")


@pytest.mark.parametrize("checkpoint", ["none", "started", "measured"])
async def test_restore_preserves_remote_measured_usage_and_only_marks_unmatched_start_unknown(
    tmp_path, checkpoint
):
    from daimon_harness.journal import Journal
    from mux.drivers.selfhosted.wire import TurnRequest

    blobs = MemoryBlobs()
    journal = Journal(tmp_path / "old")
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    journal.append(request, "status_running", {})
    journal.append(request, "model_request_started", {"request_id": "A", "model": "gpt-6-luna"})
    if checkpoint == "started":
        Checkpoints(blobs).save(journal, "session-1")
    measured = dict(
        request_id="A",
        model="gpt-6-luna",
        input_tokens=1000,
        cached_input_tokens=10,
        output_tokens=100,
        reasoning_tokens=5,
    )
    journal.append(request, "usage", measured)
    if checkpoint == "measured":
        Checkpoints(blobs).save(journal, "session-1")
    journal.append(request, "model_request_started", {"request_id": "B", "model": "gpt-6-luna"})
    checkpoints = Checkpoints(blobs)
    checkpoints.upload_write_ahead(
        "session-1", request.turn_id, checkpoints.write_ahead(journal, request)
    )
    seq = journal.latest_seq("session-1")
    journal.close()
    shutil.rmtree(tmp_path / "old")
    model = FakeModelAdapter()
    async with harness(tmp_path / "new", blobs, model) as (supervisor, client):
        response = await client.get("/v0/sessions/session-1/events", params={"after_seq": seq})
        assert response.status_code == 200
        events = await supervisor.io(supervisor.journal.events, "session-1")
        assert [event.data for event in events if event.type == "usage"] == [measured]
        unknown = [
            event.data["request_id"]
            for event in events
            if event.type == "error" and event.data["kind"] == "usage_unavailable"
        ]
        assert unknown == ["B"]
        assert (
            await client.get("/v0/sessions/session-1/events", params={"after_seq": seq})
        ).json() == response.json()
        assert (await client.post("/v0/turns", json=turn())).status_code == 202
        assert model.calls == 0
        assert (
            len(
                [
                    event
                    for event in await supervisor.io(supervisor.journal.events, "session-1")
                    if event.type == "usage"
                ]
            )
            == 1
        )


async def test_delayed_checkpoint_failure_is_delivered_on_next_turn_after_reader_closed(tmp_path):
    import threading

    class DelayedFailure(MemoryBlobs):
        def __init__(self):
            super().__init__()
            self.upload_started = threading.Event()
            self.release_upload = threading.Event()

        def upload(self, name, source):
            if name.endswith(".tar.zst"):
                self.upload_started.set()
                assert self.release_upload.wait(10)
                raise OSError("upload unavailable")
            super().upload(name, source)

    blobs = DelayedFailure()
    home = tmp_path / "home"
    async with harness(home, blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
        assert await asyncio.to_thread(blobs.upload_started.wait, 5)
        # The original reader observes the terminal event and closes before failure.
        closed = (await client.get("/v0/sessions/session-1/events")).json()
        assert closed["complete"] and closed["events"][-1]["type"] == "turn_ended"
        floor = closed["events"][-1]["seq"]
        blobs.release_upload.set()
        await supervisor.flush_checkpoints()
        assert (supervisor.journal.root / "checkpoint.notice").exists()
    # A local process restart must preserve the queued notice too.
    async with harness(home, blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        await supervisor.tasks["next"]
        events = (
            await client.get("/v0/sessions/session-1/events", params={"after_seq": floor})
        ).json()["events"]
        warnings = [event for event in events if event["type"] == "checkpoint_warning"]
        assert len(warnings) == 1 and warnings[0]["turn_id"] == "next"
        await supervisor.flush_checkpoints()
        await client.post("/v0/turns", json=turn(turn_id="third", key="third"))
        await supervisor.tasks["third"]
        assert (
            sum(
                event.type == "checkpoint_warning"
                for event in await supervisor.io(supervisor.journal.events, "session-1")
            )
            == 1
        )


async def test_shutdown_returns_while_checkpoint_upload_remains_blocked(
    tmp_path, monkeypatch, caplog
):
    import threading

    from daimon_harness import service

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockedBlob(MemoryBlobs):
        def upload(self, name, source):
            if name.endswith(".tar.zst"):
                started.set()
                release.wait()
            try:
                super().upload(name, source)
                if name.endswith(".tar.zst"):
                    import logging

                    logging.getLogger("storage.late").warning("late token: %s", "test")
            finally:
                if name.endswith(".tar.zst"):
                    finished.set()

    monkeypatch.setattr(service, "CHECKPOINT_SHUTDOWN_TIMEOUT", 0.05)
    try:
        async with asyncio.timeout(5):
            async with harness(tmp_path, BlockedBlob()) as (supervisor, client):
                await client.post("/v0/turns", json=turn())
                await supervisor.tasks["turn-1"]
                assert await asyncio.to_thread(started.wait, 3)
            assert not release.is_set()
            assert supervisor.storage_worker.closed and supervisor.wal_worker.closed
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
    assert "late token: [redacted]" in caplog.text
    assert "late token: test" not in caplog.text


def test_blocked_storage_does_not_queue_more_work_or_prevent_process_exit():
    import subprocess
    import sys

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import threading
from daimon_harness.storage_worker import StorageWorker
worker = StorageWorker("blocked-storage")
started = threading.Event()
def blocked():
    started.set()
    threading.Event().wait()
worker.submit(blocked)
assert started.wait(1)
try:
    worker.submit(lambda: None)
except RuntimeError:
    pass
else:
    raise AssertionError("queued behind blocked storage")
worker.shutdown(wait=False, cancel_futures=True)
print("closed")
""",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    assert child.stdout.strip() == "closed"


async def test_shutdown_flushes_delayed_upload_before_closing_shared_captured_identity(
    tmp_path, monkeypatch
):
    import time

    from azure.core.credentials import AccessToken
    from daimon_harness.model_adapter import AzureModelAdapter
    from daimon_harness.service import Supervisor
    from mux.drivers.selfhosted.wire import TurnRequest

    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1:1/identity")
    monkeypatch.setenv("IDENTITY_HEADER", "synthetic-shared-identity-header")
    adapter = AzureModelAdapter()
    identity = adapter.identity
    assert identity is not None
    issued = []

    async def token(*scopes, **kwargs):
        issued.append(scopes)
        return AccessToken("synthetic-blob-token", int(time.time()) + 3600)

    monkeypatch.setattr(identity.credential, "get_token", token)

    class DelayedBlob(MemoryBlobs):
        def upload(self, name, source):
            time.sleep(0.05)
            assert not identity.closed
            identity.get_token("https://storage.azure.com/.default")
            super().upload(name, source)

        def close(self):
            identity.close()

    blobs = DelayedBlob()
    supervisor = Supervisor(tmp_path, adapter, checkpoints=Checkpoints(blobs))
    request = TurnRequest.model_validate(turn())
    await supervisor.io(supervisor.journal.claim, request)
    await supervisor.io(supervisor.journal.append, request, "turn_ended", {"outcome": "completed"})
    supervisor.schedule_checkpoint(request.session_id)
    await supervisor.close()
    assert issued == [("https://storage.azure.com/.default",)] * 3
    assert identity.closed
    assert len(blobs.uploads) == 3
    assert "/reservations/" in blobs.uploads[0]
    assert "/wal/" in blobs.uploads[1] and blobs.uploads[2].endswith(".tar.zst")
    assert not (tmp_path / ".daimon/checkpoint.pending").exists()


async def test_surviving_real_driver_recovers_wal_usage_once_after_sandbox_loss(tmp_path):
    from .interop_support import SCOPE, driver_session, message

    ready_a, allow_b, ready_b = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class TwoRequests:
        calls = 0

        async def run(self, context, history):
            self.calls += 1
            await context.emit("model_request_started", {"request_id": "A", "model": "gpt-6-luna"})
            await context.emit(
                "usage",
                dict(
                    request_id="A",
                    model="gpt-6-luna",
                    input_tokens=1000,
                    cached_input_tokens=0,
                    output_tokens=100,
                    reasoning_tokens=0,
                ),
            )
            ready_a.set()
            await allow_b.wait()
            await context.emit("model_request_started", {"request_id": "B", "model": "gpt-6-luna"})
            ready_b.set()
            await asyncio.Event().wait()

    blobs, model = MemoryBlobs(), TwoRequests()
    current = None

    async def forward(request):
        assert current is not None
        response = await current.request(
            request.method,
            request.url.path,
            params=dict(request.url.params),
            content=request.content,
            headers={**dict(request.headers), "authorization": "Bearer test"},
        )
        assert response.is_success, response.text
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(forward), base_url="https://test"
    ) as client:
        async with harness(tmp_path / "old", blobs, model) as (_, old_client):
            current = old_client
            driver, session = await driver_session(client)
            receipt = await driver.events.send(
                SCOPE, session.ref, [message("question")], key="paid"
            )
            await asyncio.wait_for(ready_a.wait(), 3)
            await driver.events.reconcile(SCOPE, session.ref)
            first_usage = await driver.usage.reconcile(SCOPE, session.ref)
            assert len(first_usage) == 1 and first_usage[0].input_tokens == 1000
            allow_b.set()
            await asyncio.wait_for(ready_b.wait(), 3)
            await driver.events.reconcile(SCOPE, session.ref)
            acknowledged = await driver.events.cursor(session.ref)
            # No terminal checkpoint survives destruction of the old sandbox.
            blobs.fail = True
        shutil.rmtree(tmp_path / "old")
        assert not any(name.endswith(".tar.zst") for name in blobs.blobs)
        blobs.fail = False
        new_model = FakeModelAdapter()
        async with harness(tmp_path / "new", blobs, new_model) as (supervisor, new_client):
            current = new_client
            assert (await driver.events.reconcile(SCOPE, session.ref)).state == "idle"
            observations = await driver.usage.reconcile(SCOPE, session.ref)
            measured = [value for value in observations if value.completeness == "measured"]
            unknown = [value for value in observations if value.completeness == "unknown"]
            assert measured == list(first_usage)
            assert len(unknown) == 1 and unknown[0].id.endswith(":request:B")
            assert await supervisor.io(supervisor.journal.ended, receipt.turn_id)
            recovered = await supervisor.io(supervisor.journal.events, session.ref.id, acknowledged)
            assert not any(
                event.type == "usage" and event.data["request_id"] == "A" for event in recovered
            )
            assert (
                next(e for e in reversed(recovered) if e.type != "history_gap").type == "turn_ended"
            )
            assert model.calls == 1 and new_model.calls == 0
            assert await driver.usage.reconcile(SCOPE, session.ref) == observations


@pytest.mark.parametrize(
    "incoming", ["start_only", "unknown_same_seq", "unknown_later_seq", "measured"]
)
async def test_write_ahead_upload_never_downgrades_remote_measured_usage(tmp_path, incoming):
    from daimon_harness.checkpoints import WriteAhead
    from daimon_harness.journal import Journal
    from mux.drivers.selfhosted.wire import Event, TurnRequest

    blobs, journal = MemoryBlobs(), Journal(tmp_path)
    checkpoints = Checkpoints(blobs)
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    journal.append(request, "model_request_started", {"request_id": "paid", "model": "gpt-6-luna"})
    journal.append(
        request,
        "usage",
        dict(
            request_id="paid",
            model="gpt-6-luna",
            input_tokens=123,
            cached_input_tokens=0,
            output_tokens=45,
            reasoning_tokens=0,
        ),
    )
    rich = WriteAhead.model_validate_json(checkpoints.write_ahead(journal, request))
    measured = rich.inventory[-1]
    try:
        checkpoints.upload_write_ahead(
            request.session_id, request.turn_id, rich.model_dump_json().encode()
        )
        local = rich.model_copy(deep=True)
        if incoming == "start_only":
            local.inventory = rich.inventory[:1]
            local.high_water = local.inventory[-1].seq
        elif incoming.startswith("unknown"):
            unknown = Event(
                seq=measured.seq + int(incoming == "unknown_later_seq"),
                session_id=request.session_id,
                turn_id=request.turn_id,
                at=measured.at,
                type="error",
                data={
                    "kind": "usage_unavailable",
                    "request_id": "paid",
                    "message": "stale local inventory",
                    "retryable": False,
                },
            )
            local.inventory[-1] = unknown
            local.high_water = unknown.seq
        checkpoints.upload_write_ahead(
            request.session_id, request.turn_id, local.model_dump_json().encode()
        )
        remote = WriteAhead.model_validate_json(next(iter(blobs.blobs.values())))
        assert remote.inventory == rich.inventory
        assert remote.inventory[-1] == measured
        assert remote.high_water == max(rich.high_water, local.high_water)
    finally:
        journal.close()


async def test_conflicting_measured_wal_update_preserves_original_blob(tmp_path):
    from daimon_harness.checkpoints import WriteAhead
    from daimon_harness.journal import Journal
    from mux.drivers.selfhosted.wire import TurnRequest

    blobs, journal = MemoryBlobs(), Journal(tmp_path)
    checkpoints = Checkpoints(blobs)
    request = TurnRequest.model_validate(turn())
    journal.claim(request)
    journal.append(
        request,
        "usage",
        dict(
            request_id="paid",
            model="gpt-6-luna",
            input_tokens=123,
            cached_input_tokens=0,
            output_tokens=45,
            reasoning_tokens=0,
        ),
    )
    raw = checkpoints.write_ahead(journal, request)
    try:
        checkpoints.upload_write_ahead(request.session_id, request.turn_id, raw)
        before = blobs.blobs.copy()
        conflict = WriteAhead.model_validate_json(raw)
        conflict.inventory[0].data["input_tokens"] = 999
        with pytest.raises(ValueError, match="immutable event"):
            checkpoints.upload_write_ahead(
                request.session_id, request.turn_id, conflict.model_dump_json().encode()
            )
        assert blobs.blobs == before
    finally:
        journal.close()


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_divergence_seals_appends_until_remote_merge_succeeds_in_same_process(
    tmp_path, lost_ack
):
    from daimon_harness.errors import HarnessError
    from mux.drivers.selfhosted.wire import TurnRequest

    from .test_sequence_ratchet import fail_local_commits, free_local_space

    class Storage(MemoryBlobs):
        offline = False
        lose_ack = lost_ack

        def list(self, prefix):
            if self.offline:
                raise OSError("storage offline during reconciliation")
            return super().list(prefix)

        def upload(self, name, source):
            raw = source.read()
            super().upload(name, io.BytesIO(raw))
            if self.lose_ack and "/wal/" in name and b'"type":"usage"' in raw:
                raise OSError("remote commit acknowledgement lost")

    class Model:
        async def run(self, context, history):
            await context.emit(
                "model_request_started", {"request_id": "paid", "model": "gpt-6-luna"}
            )
            await supervisor.io(fail_local_commits, supervisor.journal)
            await context.emit(
                "usage",
                dict(
                    request_id="paid",
                    model="gpt-6-luna",
                    input_tokens=123,
                    cached_input_tokens=0,
                    output_tokens=45,
                    reasoning_tokens=0,
                ),
            )
            return history

    blobs = Storage()
    async with harness(tmp_path, blobs, Model()) as (supervisor, client):
        request = TurnRequest.model_validate(turn())
        assert (await client.post("/v0/turns", json=turn())).status_code == 202
        await asyncio.gather(supervisor.tasks["turn-1"], return_exceptions=True)
        remote_before = blobs.blobs.copy()
        assert supervisor.journal.diverged
        with pytest.raises(HarnessError, match="diverged"):
            await supervisor.io(supervisor.journal.append, request, "status_running", {})
        with pytest.raises(HarnessError, match="diverged"):
            await supervisor.io(supervisor.recover)
        await supervisor.io(free_local_space, supervisor.journal)
        blobs.lose_ack, blobs.offline = False, True
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 503
        with pytest.raises(HarnessError, match="diverged"):
            await supervisor.io(supervisor.checkpoints.write_ahead, supervisor.journal, request)
        assert blobs.blobs == remote_before
        blobs.offline = False
        response = await client.get("/v0/sessions/session-1/events")
        assert response.status_code == 200 and response.json()["complete"]
        events = response.json()["events"]
        usage = [e for e in events if e["type"] == "usage"]
        assert len(usage) == 1
        assert usage[0]["seq"] == 3 and usage[0]["data"]["input_tokens"] == 123
        assert usage[0]["data"]["output_tokens"] == 45
        assert not any(e["data"].get("kind") == "usage_unavailable" for e in events)
        assert sum(e["type"] == "turn_ended" for e in events) == 1
        supervisor.adapter = FakeModelAdapter()
        assert (
            await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        ).status_code == 202
        await supervisor.tasks["next"]
        assert (await client.get("/v0/sessions/session-1/events")).json()["complete"]


async def cleanup_failure_case(supervisor, client, monkeypatch, turn_id, *, real_timeout=False):
    """Codex r10 repro: an abandoned tool ignores cancellation and writes late."""
    from contextlib import suppress

    from daimon_harness.errors import HarnessError

    release, started = asyncio.Event(), asyncio.Event()
    side_effects = []
    workers = []
    original_stop = TurnContext.stop_workers

    class StuckModel:
        async def run(self, context, history):
            await context.emit(
                "model_request_started", {"request_id": turn_id, "model": "gpt-6-luna"}
            )
            await context.emit(
                "usage",
                {
                    "request_id": turn_id,
                    "model": "gpt-6-luna",
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_tokens": 0,
                },
            )

            async def stuck():
                started.set()
                while not release.is_set():
                    with suppress(asyncio.CancelledError):
                        await release.wait()
                side_effects.append("late write")
                return "late write"

            workers.append(asyncio.create_task(context.tool("stuck", "write", {}, stuck)))
            await started.wait()
            return history

    async def failed_stop(context):
        context.stopping = True
        if any(not worker.done() for worker in context.workers):
            raise HarnessError("workers_active", "Tool cleanup incomplete; session sealed")
        await original_stop(context)

    previous_adapter = supervisor.adapter
    supervisor.adapter = StuckModel()
    with monkeypatch.context() as patch:
        if not real_timeout:
            patch.setattr(TurnContext, "stop_workers", failed_stop)
        try:
            response = await client.post("/v0/turns", json=turn(turn_id=turn_id, key=turn_id))
            assert response.status_code == 202, response.text
            result = await asyncio.gather(supervisor.tasks[turn_id], return_exceptions=True)
            assert isinstance(result[0], HarnessError) and result[0].kind == "workers_active"
            patch.setattr(TurnContext, "stop_workers", failed_stop)
            assert not workers[0].done()
            for _ in range(2):
                response = await client.get("/v0/sessions/session-1/events")
                assert response.status_code == 503, "I5: poll ended a turn with live workers"
                assert response.json()["detail"]["retryable"] is True
                response = await client.post(
                    "/v0/turns", json=turn(turn_id=turn_id + "-next", key=turn_id + "-next")
                )
                assert response.status_code == 503, "I5: admitted a new turn with live workers"
            assert not side_effects
        finally:
            release.set()
            await asyncio.gather(*workers, return_exceptions=True)
            supervisor.adapter = previous_adapter
        response = await client.get("/v0/sessions/session-1/events")
        assert response.status_code == 200, response.text
        events = response.json()["events"]
        terminal = [e for e in events if e["turn_id"] == turn_id and e["type"] == "turn_ended"]
        assert len(terminal) == 1 and side_effects == ["late write"]
        assert not any(e["turn_id"] == turn_id and e["seq"] > terminal[0]["seq"] for e in events)
        response = await client.post(
            "/v0/turns", json=turn(turn_id=turn_id + "-next", key=turn_id + "-next")
        )
        assert response.status_code == 202, response.text
        await supervisor.tasks[turn_id + "-next"]


async def test_cleanup_failure_keeps_session_sealed_until_workers_stop(tmp_path, monkeypatch):
    async with harness(tmp_path, MemoryBlobs()) as (supervisor, client):
        await cleanup_failure_case(supervisor, client, monkeypatch, "stuck", real_timeout=True)


@pytest.mark.parametrize("failure", ["reopen", "cursor"])
async def test_failed_restore_reopen_and_cursor_are_retryable(tmp_path, monkeypatch, failure):
    import sqlite3

    from daimon_harness import service
    from daimon_harness.journal import Journal

    async with harness(tmp_path, MemoryBlobs()) as (supervisor, client):
        restore = supervisor.checkpoints.restore
        record = Journal.record_cursor
        calls = 0

        def fail_restore(*args):
            raise OSError("storage unavailable")

        def full(*args, **kwargs):
            raise sqlite3.OperationalError("disk full")

        def fail_second_cursor(self, *args):
            nonlocal calls
            calls += 1
            if calls > 1:
                return full()
            return record(self, *args)

        with monkeypatch.context() as patch:
            patch.setattr(supervisor.checkpoints, "restore", fail_restore)
            if failure == "reopen":
                patch.setattr(service, "Journal", full)
            else:
                patch.setattr(Journal, "record_cursor", fail_second_cursor)
            for _ in range(2):
                response = await client.get("/v0/sessions/session-1/events")
                assert response.status_code == 503, response.text
                assert response.json()["detail"]["retryable"] is True
        supervisor.checkpoints.restore = restore
        response = await client.get("/v0/sessions/session-1/events")
        assert response.status_code == 200, response.text
        response = await client.post("/v0/turns", json=turn())
        assert response.status_code == 202
        await supervisor.tasks["turn-1"]


@pytest.mark.parametrize("failure", ["killed", "timeout"])
@pytest.mark.parametrize("persistent", [False, True])
async def test_interrupted_export_is_retried_without_publishing_partial_archive(
    tmp_path, monkeypatch, failure, persistent
):
    import subprocess

    from daimon_harness import checkpoints
    from daimon_harness.journal import Journal

    blobs = MemoryBlobs()
    original_run = checkpoints.subprocess.run
    attempts = 0

    def interrupted(command, **kwargs):
        nonlocal attempts
        if "checkpoint_archive.py" in str(command):
            attempts += 1
            if persistent or attempts == 1:
                kwargs["stdout"].write(b"partial workspace archive")
                assert not any(name.endswith(".tar.zst") for name in blobs.blobs)
                if failure == "killed":
                    raise subprocess.CalledProcessError(-9, command)
                raise subprocess.TimeoutExpired(command, 60)
        return original_run(command, **kwargs)

    async with harness(tmp_path / "home", blobs) as (supervisor, client):
        with monkeypatch.context() as patch:
            patch.setattr(checkpoints.subprocess, "run", interrupted)
            assert (await client.post("/v0/turns", json=turn("tool"))).status_code == 202
            await supervisor.tasks["turn-1"]
            await supervisor.flush_checkpoints()
            assert attempts == 2
            if persistent:
                assert supervisor.checkpoint_pending == "session-1"
                assert (supervisor.journal.root / "checkpoint.pending").exists()
                assert not any(name.endswith(".tar.zst") for name in blobs.blobs)
        if persistent:
            # No further turn or operator action is needed to retry the export.
            assert (await client.get("/v0/sessions/session-1/events")).status_code == 200
            await supervisor.flush_checkpoints()
        assert supervisor.checkpoint_pending is None
        assert not (supervisor.journal.root / "checkpoint.pending").exists()
        assert any(name.endswith(".tar.zst") for name in blobs.blobs)
        restored = supervisor.checkpoints.restore(Journal(tmp_path / "restored"), "session-1")
        try:
            sandbox, _ = restored.sandbox("session-1")
            assert (sandbox / "work/result.txt").read_text() == "durable"
        finally:
            restored.close()
