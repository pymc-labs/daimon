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
from daimon_harness.service import create_app
from fastapi import HTTPException
from mux.drivers.selfhosted.transport import DirectTransport, sandbox_id

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
        before = (await client.get("/v0/sessions/session-1/events")).json()
        history = supervisor.journal.history("session-1")
        seq = before["events"][-1]["seq"]
        assert blobs.uploads[-1] == f"sessions/{sandbox_id('session-1')}/{seq}.tar.zst"
    shutil.rmtree(home)
    model = FakeModelAdapter()
    async with harness(home, blobs, model) as (supervisor, client):
        # Polling is the first request to a completely new sandbox.
        assert (await client.get("/v0/sessions/session-1/events")).json() == before
        workspace, _ = supervisor.journal.sandbox("session-1")
        assert (workspace / "work/result.txt").read_text() == "durable"
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
        assert after["events"][0]["seq"] == seq + 1
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
        assert (await client.get("/v0/sessions/session-1/events")).json() == before


async def test_crash_checkpoint_records_unknown_usage_and_never_reexecutes(tmp_path):
    class CrashWindow(FakeModelAdapter):
        async def run(self, context, history):
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


async def test_failed_checkpoint_seals_terminal_state_until_retry(tmp_path):
    blobs = MemoryBlobs()
    model = FakeModelAdapter()
    async with harness(tmp_path, blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn("wait"))
        await asyncio.wait_for(model.started.wait(), 5)
        blobs.fail = True
        cancelled = await client.post("/v0/turns/turn-1/cancel")
        assert cancelled.status_code == 503
        response = await client.get("/v0/sessions/session-1/events")
        assert response.status_code == 503
        assert "secret" not in response.text
        blobs.fail = False
        recovered = (await client.get("/v0/sessions/session-1/events")).json()
        assert recovered["events"][-1]["type"] == "turn_ended"
        assert recovered["complete"] is True
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
        assert supervisor.journal.events("session-1") == expected
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
        with pytest.raises(HTTPException, match="Session checkpoint unavailable"):
            await supervisor.io(supervisor.checkpoint, "session-1")
        assert blobs.blobs == before
        assert all(b"service credential" not in data for data in blobs.blobs.values())


async def test_failed_upload_remains_sealed_after_local_process_restart(tmp_path):
    blobs = MemoryBlobs()
    home = tmp_path / "home"
    model = FakeModelAdapter()
    async with harness(home, blobs, model) as (supervisor, client):
        await client.post("/v0/turns", json=turn("wait"))
        await asyncio.wait_for(model.started.wait(), 5)
        blobs.fail = True
        assert (await client.post("/v0/turns/turn-1/cancel")).status_code == 503
        assert (supervisor.journal.root / "checkpoint.pending").read_text() == "session-1"
    async with harness(home, blobs) as (supervisor, client):
        assert (await client.get("/v0/sessions/session-1/events")).status_code == 503
        blobs.fail = False
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
        async def run(self, context, history):
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
