"""Blob recovery across complete sandbox loss preserves workspace and billing inventory."""

from __future__ import annotations

import asyncio
import io
import shutil
import tarfile
from contextlib import asynccontextmanager

import httpx
import pytest
from daimon_harness.checkpoint_archive import restore_work
from daimon_harness.checkpoints import Checkpoints
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
        if self.fail:
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
        workspace, _ = supervisor.journal.sandbox("session-1")
        (workspace / "work/result.txt").chmod(0o700)
        await supervisor.io(supervisor.checkpoint, "session-1")
        before = (await client.get("/v0/sessions/session-1/events")).json()
        history = supervisor.journal.history("session-1")
        seq = before["events"][-1]["seq"]
        assert blobs.uploads[-1] == f"sessions/{sandbox_id('session-1')}/{seq}.tar.zst"
    shutil.rmtree(home)
    model = FakeModelAdapter()
    async with harness(home, blobs, model) as (supervisor, client):
        # Polling is the first request to a completely new sandbox.
        restored = (await client.get("/v0/sessions/session-1/events")).json()
        assert restored["events"][:-1] == before["events"]
        assert restored["events"][-1]["type"] == "history_gap"
        workspace, _ = supervisor.journal.sandbox("session-1")
        assert (workspace / "work/result.txt").read_text() == "durable"
        assert (workspace / "work/result.txt").stat().st_mode & 0o777 == 0o700
        assert supervisor.journal.history("session-1") == history
        assert (await client.post("/v0/turns", json=turn("tool"))).status_code == 202
        assert model.calls == 0
        assert (
            await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        ).status_code == 202
        await supervisor.tasks["next"]
        after = (
            await client.get("/v0/sessions/session-1/events", params={"after_seq": seq})
        ).json()
        assert after["events"][0]["seq"] > seq
        assert model.histories[0] == history
        assert len(blobs.blobs) == 2
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
        assert (await client.post("/v0/turns/turn-1/cancel")).json() == {"cancelled": True}
        before = (await client.get("/v0/sessions/session-1/events")).json()
        assert before["events"][-1]["data"]["outcome"] == "interrupted"
    shutil.rmtree(home)
    async with harness(home, blobs) as (_, client):
        assert (await client.post("/v0/turns/turn-1/cancel")).json() == {"cancelled": False}
        restored = (await client.get("/v0/sessions/session-1/events")).json()
        assert restored["events"][:-1] == before["events"]
        assert restored["events"][-1]["type"] == "history_gap"


async def test_crash_checkpoint_records_unknown_usage_and_never_reexecutes(tmp_path):
    class CrashWindow(FakeModelAdapter):
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            await context.emit("model_request_started", {"request_id": "pending", "model": "gpt-5"})
            self.started.set()
            await asyncio.Event().wait()

    blobs = MemoryBlobs()
    model = CrashWindow()
    home = tmp_path / "old"
    async with harness(home, blobs, model) as (_, client):
        await client.post("/v0/turns", json=turn())
        await asyncio.wait_for(model.started.wait(), 5)
        # Clone what Blob held before the process could commit terminal events.
        crash_blobs = MemoryBlobs()
        crash_blobs.blobs = blobs.blobs.copy()
    fresh_model = FakeModelAdapter()
    async with harness(tmp_path / "new", crash_blobs, fresh_model) as (_, client):
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
        blobs.fail = True
        assert (await client.post("/v0/turns/turn-1/cancel")).status_code == 200
        response = await client.get("/v0/sessions/session-1/events")
        assert response.status_code == 200
        assert response.json()["complete"] is True
        assert "secret" not in response.text
        assert (
            sum(e["data"].get("kind") == "checkpoint_warning" for e in response.json()["events"])
            == 1
        )
        assert (
            await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        ).status_code == 202
        await supervisor.tasks["next"]
        assert supervisor.checkpoint_pending == "session-1"
        blobs.fail = False
        await supervisor.io(supervisor.checkpoint, "session-1")
        assert supervisor.checkpoint_pending is None


async def test_restore_corruption_refuses_new_work_without_losing_remote_blob(tmp_path):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
    latest = max(blobs.blobs, key=lambda name: int(name.rsplit("/", 1)[1].split(".")[0]))
    blobs.blobs[latest] = b"corrupt"
    model = FakeModelAdapter()
    async with harness(tmp_path / "new", blobs, model) as (_, client):
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
        await transport.request("POST", "/v0/turns/old/cancel", session_id="session-1")


async def test_authentication_and_session_binding_precede_any_restore(tmp_path):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "old", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn())
        await supervisor.tasks["turn-1"]
    async with harness(tmp_path / "new", blobs) as (supervisor, client):
        response = await client.post(
            "/v0/turns/turn-1/cancel", headers={"Authorization": "Bearer wrong"}
        )
        assert response.status_code == 401
        assert supervisor.restored is False
        assert not supervisor.journal.turns()
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
        expected = supervisor.journal.events("session-1")
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
        assert supervisor.journal.events("session-1")[:-1] == expected
        assert not (supervisor.journal.root / "restore.pending").exists()
        workspace, _ = supervisor.journal.sandbox("session-1")
        assert (workspace / "work/result.txt").read_text() == "durable"


async def test_workspace_link_cannot_export_service_files(tmp_path):
    blobs = MemoryBlobs()
    async with harness(tmp_path / "home", blobs) as (supervisor, client):
        await client.post("/v0/turns", json=turn("tool"))
        await supervisor.tasks["turn-1"]
        workspace, _ = supervisor.journal.sandbox("session-1")
        secret = tmp_path / "secret"
        secret.write_text("service credential")
        (workspace / "work/link").symlink_to(secret)
        before = blobs.blobs.copy()
        await supervisor.io(supervisor.checkpoint, "session-1")
        assert blobs.blobs != before
        events = supervisor.journal.events("session-1")
        assert events[-1].data["kind"] == "checkpoint_warning"
        assert all(b"service credential" not in data for data in blobs.blobs.values())


async def test_failed_upload_does_not_block_after_local_process_restart(tmp_path):
    blobs = MemoryBlobs()
    home = tmp_path / "home"
    model = FakeModelAdapter()
    async with harness(home, blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn("wait"))
        await asyncio.wait_for(model.started.wait(), 5)
        blobs.fail = True
        assert (await client.post("/v0/turns/turn-1/cancel")).status_code == 200
        assert (supervisor.journal.root / "checkpoint.pending").read_text() == "session-1"
    async with harness(home, blobs) as (supervisor, client):
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 200
        blobs.fail = False
        await supervisor.io(supervisor.checkpoint, "session-1")
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
            await context.emit("model_request_started", {"request_id": "pending", "model": "gpt-5"})
            self.started.set()
            await asyncio.Event().wait()

    blobs = MemoryBlobs()
    model = WaitingModel()
    async with harness(tmp_path / "old", blobs, model) as (_, client):
        await client.post("/v0/turns", json=turn())
        await asyncio.wait_for(model.started.wait(), 5)
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
    async with harness(home, crash_blobs) as (_, client):
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 503
    before = crash_blobs.blobs.copy()
    async with harness(home, crash_blobs) as (_, client):
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

        def log_message(self, *args):
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
            for index in range(7):
                await context.emit(
                    "agent_message_delta", {"message_id": "streamed", "text": str(index)}
                )
            self.started.set()
            await asyncio.Event().wait()

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
                source = await driver.events.open_stream(
                    SCOPE, session.ref, after="0", previews=True
                )
                try:
                    seen = [await anext(source) for _ in range(9)]
                finally:
                    await source.aclose()
                cursor = seen[-1].sequence
                assert cursor == 9
                crash_blobs = MemoryBlobs()
                crash_blobs.blobs = blobs.blobs.copy()
                assert (
                    max(int(name.rsplit("/", 1)[1].split(".")[0]) for name in crash_blobs.blobs)
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
    reasons = {item["reason"] for item in report["skipped"]}
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
            await context.emit("model_request_started", {"request_id": "pending", "model": "test"})
            self.started.set()
            await asyncio.Event().wait()
            return []

    blobs = MemoryBlobs()
    model = Pending()
    async with harness(tmp_path / "old", blobs, model) as (_, client):
        await client.post("/v0/turns", json=turn())
        await asyncio.wait_for(model.started.wait(), 5)
        crash_blobs = MemoryBlobs()
        crash_blobs.blobs = blobs.blobs.copy()
    async with harness(tmp_path / "new", crash_blobs) as (_, client):
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
        assert response["events"][0]["seq"] == 10
        assert response["events"][0]["type"] == "history_gap"
        assert response["events"][-1]["data"]["error"]["kind"] == "sandbox_lost"
        assert any(e["data"].get("kind") == "usage_unavailable" for e in response["events"])
