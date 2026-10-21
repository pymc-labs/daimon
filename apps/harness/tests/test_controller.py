from __future__ import annotations

import asyncio
import copy
import io

import httpx
import pytest
from daimon_harness.controller import SESSION, Controller, create_app
from daimon_harness.pvc_store import PVCStore


class Kube:
    def __init__(self):
        self.items = {}
        self.created = 0

    async def list(self, owner):
        return copy.deepcopy(list(self.items.values()))

    async def create(self, pod):
        name = pod["metadata"]["name"]
        if name in self.items:
            return
        self.created += 1
        pod["metadata"].update(uid=name, resourceVersion="1")
        pod["status"] = {"podIP": "127.0.0.1", "conditions": [{"type": "Ready", "status": "True"}]}
        self.items[name] = copy.deepcopy(pod)

    async def patch(self, pod, values):
        current = self.items[pod["metadata"]["name"]]
        assert current["metadata"]["resourceVersion"] == pod["metadata"]["resourceVersion"]
        for key, value in values.items():
            current["metadata"].setdefault(key, {}).update(value)
        current["metadata"]["resourceVersion"] = str(
            int(current["metadata"]["resourceVersion"]) + 1
        )

    async def delete(self, pod):
        del self.items[pod["metadata"]["name"]]


def controller(kube, **kw):
    return Controller(
        kube, {"spec": {"containers": [{"env": []}]}}, "test", maximum=kw.pop("maximum", 2), **kw
    )


async def test_single_flight_warm_rebuild_and_reap():
    kube = Kube()
    now = [1000]
    ctl = controller(kube, clock=lambda: now[0])
    await ctl.maintain()
    pods = await asyncio.gather(*(ctl.acquire("session-1") for _ in range(12)))
    assert len({p["metadata"]["uid"] for p in pods}) == 1
    assert kube.created == 1
    now[0] += 901
    await ctl.maintain()
    assert len(kube.items) == 2  # active pod retained plus replacement warm pod
    for pod in pods:
        await ctl.release(pod)
    rebuilt = controller(kube, warm=0, clock=lambda: now[0])
    same = await rebuilt.acquire("session-1")
    assert same["metadata"]["uid"] == pods[0]["metadata"]["uid"]
    await rebuilt.release(same)
    now[0] += 901
    await rebuilt.maintain()
    assert all(p["metadata"]["labels"][SESSION] == "warm" for p in kube.items.values())


async def test_routing_auth_scope_and_restart(tmp_path):
    kube = Kube()
    ctl = controller(kube)
    app = create_app(controller=ctl, root=tmp_path, token="caller")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/v0/turns", json={"session_id": "a"})).status_code == 401
        assert kube.created == 0
        headers = {"Authorization": "Bearer caller"}
        response = await client.post(
            "/v0/turns?session_id=b", json={"session_id": "a"}, headers=headers
        )
        assert response.status_code == 409
        assert (await client.post("/v0/turns/t/cancel", headers=headers)).status_code == 422
        pod = await ctl.acquire("a")
        cap = {"Authorization": "Bearer " + ctl.credential(pod, "checkpoint")}
        prefix = "sessions/" + pod["metadata"]["labels"][SESSION] + "/"
        assert (
            await client.put(
                "/internal/checkpoints/" + prefix + "wal/t.json", content=b"wal", headers=cap
            )
        ).status_code == 204
        assert (
            await client.get("/internal/checkpoints/" + prefix + "wal/t.json", headers=cap)
        ).content == b"wal"
        assert (
            await client.get("/internal/checkpoints/sessions/foreign/wal/t.json", headers=cap)
        ).status_code == 403
        assert (
            await client.get("/internal/checkpoints", params={"prefix": prefix}, headers=headers)
        ).status_code == 403
        assert (
            await client.get("/internal/checkpoints", params={"prefix": prefix}, headers=cap)
        ).json() == [prefix + "wal/t.json"]


def test_atomic_pvc_store(tmp_path):
    store = PVCStore(tmp_path)
    store.upload("sessions/a/wal/t.json", io.BytesIO(b"first"))

    class Broken(io.BytesIO):
        def read(self, size=-1):
            raise OSError("failed upload")

    with pytest.raises(OSError):
        store.upload("sessions/a/wal/t.json", Broken())
    output = io.BytesIO()
    store.download("sessions/a/wal/t.json", output)
    assert output.getvalue() == b"first"
    assert not list(tmp_path.rglob(".pending-*"))
    with pytest.raises(ValueError):
        store.upload("../escape", io.BytesIO())


async def test_proxy_preserves_c3_body_query_headers_and_sse(tmp_path):
    kube = Kube()
    ctl = controller(kube)
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(
            202,
            stream=httpx.ByteStream(b'{"accepted":true}'),
            headers={"content-type": "application/json"},
        )

    forward = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(controller=ctl, root=tmp_path, token="caller", http_client=forward)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v0/turns?session_id=a&after_seq=8123",
            json={"session_id": "a"},
            headers={
                "Authorization": "Bearer caller",
                "Accept": "application/json",
            },
        )
        assert response.status_code == 202
        assert response.json() == {"accepted": True}
        assert seen[0].url.params["after_seq"] == "8123"
        blocked = await client.post(
            "/v0/turns?session_id=a",
            json={"session_id": "a"},
            headers={
                "Authorization": "Bearer caller",
                "X-Daimon-MCP-Authorization": "Bearer turn-only",
            },
        )
        assert blocked.status_code == 202
        assert len(seen) == 2
        assert seen[1].url.scheme == "https"
        assert seen[1].headers["x-daimon-mcp-authorization"] == "Bearer turn-only"
        assert seen[1].headers["authorization"] != "Bearer caller"
        assert not any(ctl.active.values())
        response = await client.post(
            "/v0/turns/t/cancel?session_id=a&after_seq=9000",
            headers={"Authorization": "Bearer caller"},
        )
        assert response.status_code == 202
        assert kube.created == 1
    await forward.aclose()


async def test_status_version_conflict_retries_but_foreign_binding_refuses():
    from daimon_harness.controller import Kubernetes
    from fastapi import HTTPException

    original = {
        "metadata": {"name": "p", "uid": "u", "resourceVersion": "1", "labels": {SESSION: "warm"}}
    }
    calls = []
    foreign = False

    async def api(request):
        calls.append(request.method)
        if request.method == "GET":
            pod = copy.deepcopy(original)
            pod["metadata"]["resourceVersion"] = "2"
            if foreign:
                pod["metadata"]["labels"][SESSION] = "another"
            return httpx.Response(200, json=pod)
        if len(calls) == 1:
            return httpx.Response(409)
        return httpx.Response(200, json={})

    kube = object.__new__(Kubernetes)
    kube.client = httpx.AsyncClient(base_url="http://kube", transport=httpx.MockTransport(api))
    kube.path = "/pods"
    await kube.patch(original, {"labels": {SESSION: "ours"}})
    assert calls == ["PATCH", "GET", "PATCH"]
    calls.clear()
    foreign = True
    with pytest.raises(HTTPException) as exc:
        await kube.patch(original, {"labels": {SESSION: "ours"}})
    assert exc.value.status_code == 503
    await kube.client.aclose()


def test_controller_checkpoint_configuration_captures_and_scrubs(monkeypatch):
    from daimon_harness.checkpoints import configured_checkpoints

    monkeypatch.setenv("HARNESS_CHECKPOINT_CONTROLLER_URL", "https://controller:8089")
    monkeypatch.setenv("HARNESS_CHECKPOINT_TOKEN", "pod-capability")
    monkeypatch.setenv("HARNESS_CHECKPOINT_KEEP", "2")
    configured = configured_checkpoints()
    assert configured is not None and configured.keep == 2
    assert "HARNESS_CHECKPOINT_TOKEN" not in __import__("os").environ
    configured.close()
    monkeypatch.setenv("HARNESS_CHECKPOINT_TOKEN", "orphan")
    with pytest.raises(ValueError, match="requires a controller URL"):
        configured_checkpoints()


def test_http_controller_store_refuses_plaintext():
    from daimon_harness.pvc_store import ControllerStore

    with pytest.raises(ValueError, match="HTTPS"):
        ControllerStore("http://controller:8089", "secret")


async def test_capacity_stale_template_termination_and_host_bearer_isolation(tmp_path):
    from daimon_harness.controller import TEMPLATE
    from fastapi import HTTPException

    kube = Kube()
    ctl = controller(kube, maximum=1)
    await ctl.maintain()
    warm = next(iter(kube.items.values()))
    warm["metadata"]["labels"][TEMPLATE] = "old-template"
    await ctl.maintain()
    assert kube.created == 2
    pod = await ctl.acquire("a")
    with pytest.raises(HTTPException, match="503"):
        await ctl.acquire("b")
    assert len(kube.items) == 1
    assert "HARNESS_CHECKPOINT_TOKEN" not in str(pod["spec"])
    assert "HARNESS_TOKEN" not in str(pod["spec"])
    await ctl.release(pod)
    kube.items[pod["metadata"]["name"]]["metadata"]["deletionTimestamp"] = "now"
    with pytest.raises(HTTPException, match="terminating"):
        await ctl.acquire("a")


async def test_unauthenticated_checkpoint_flood_never_calls_kubernetes(tmp_path):
    kube = Kube()
    ctl = controller(kube)
    app = create_app(controller=ctl, root=tmp_path, token="host")

    async def forbidden(owner):
        raise AssertionError("unauthenticated request reached Kubernetes")

    kube.list = forbidden
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        for _ in range(50):
            response = await client.get(
                "/internal/checkpoints", headers={"Authorization": "Bearer forged"}
            )
            assert response.status_code == 403
        assert (
            await client.post("/v0/turns", headers={"Authorization": "Bearer forged"})
        ).status_code == 401


async def test_foreign_name_conflict_returns_retryable_busy():
    from fastapi import HTTPException

    kube = Kube()
    ctl = controller(kube)

    async def conflict(pod):
        return None  # API create 409; unrelated pod is absent from owner inventory.

    kube.create = conflict
    with pytest.raises(HTTPException) as exc:
        await ctl.acquire("a")
    assert exc.value.status_code == 503
    assert "conflict" in exc.value.detail


def test_pvc_atomic_commit_order_and_missing_delete(tmp_path, monkeypatch):
    import os

    import daimon_harness.pvc_store as module

    store = PVCStore(tmp_path)
    calls = []
    fsync, replace, directory_sync = os.fsync, os.replace, module.sync_directory

    def sync(fd):
        calls.append("fsync")
        return fsync(fd)

    def rename(source, target):
        calls.append("rename")
        return replace(source, target)

    def sync_dir(path):
        calls.append("directory")
        return directory_sync(path)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", rename)
    monkeypatch.setattr(module, "sync_directory", sync_dir)
    store.upload("object", io.BytesIO(b"durable"))
    assert calls == ["fsync", "rename", "directory", "fsync"]
    store.delete("missing/parent/object")


async def test_maintenance_exception_survives_and_full_disk_reports_admission(
    tmp_path, monkeypatch
):
    from collections import namedtuple

    import daimon_harness.controller as module

    ctl = controller(Kube())

    async def broken():
        raise RuntimeError("maintenance stopped")

    ctl.maintain = broken
    app = create_app(controller=ctl, root=tmp_path, token="host")
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as client:
            assert (await client.get("/readiness")).status_code == 200
    ctl = controller(Kube())
    app = create_app(controller=ctl, root=tmp_path / "full", token="host")
    usage = namedtuple("usage", ["total", "used", "free"])
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: usage(100, 100, 0))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (await client.get("/readiness")).json() == {"ok": True, "admission_available": False}
        r = await client.post(
            "/v0/turns", json={"session_id": "a"}, headers={"Authorization": "Bearer host"}
        )
        assert r.status_code == 503 and "disk" in r.text
        assert ctl.kube.created == 0


async def test_retention_collects_only_unbound_sessions(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("HARNESS_CHECKPOINT_RETENTION_SECONDS", "1")
    ctl = controller(Kube(), warm=0)
    pod = await ctl.acquire("retained")
    app = create_app(controller=ctl, root=tmp_path, token="host")
    store = PVCStore(tmp_path)
    bound = pod["metadata"]["labels"][SESSION]
    for key in ["expired", bound]:
        store.upload("sessions/" + key + "/wal/t", io.BytesIO(b"wal"))
        os.utime(store.path("sessions/" + key), (time.time() - 100, time.time() - 100))
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if not (tmp_path / "sessions/expired").exists():
                break
            await asyncio.sleep(0.01)
        assert not (tmp_path / "sessions/expired").exists()
        assert (tmp_path / "sessions" / bound).exists()


async def test_certificate_bootstrap_pins_first_key_and_never_queries_api(tmp_path, monkeypatch):
    import daimon_harness.pod_tls as tls

    kube = Kube()
    ctl = controller(kube, warm=0)
    pod = await ctl.acquire("a")
    app = create_app(controller=ctl, root=tmp_path, token="host")
    signed = []

    def issue(csr, ip, ca, key):
        signed.append((csr, ip))
        return b"certificate"

    async def forbidden(owner):
        raise AssertionError("certificate endpoint queried Kubernetes")

    monkeypatch.setenv("HARNESS_SANDBOX_CA", "/public-ca")
    monkeypatch.setenv("HARNESS_SANDBOX_CA_KEY", "/private-ca")
    monkeypatch.setattr(tls, "issue", issue)
    kube.list = forbidden
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test:8089"
    ) as client:
        path = "/internal/certificate"
        uid = pod["metadata"]["uid"]
        assert (await client.post(path, json={"uid": "foreign", "csr": "first"})).status_code == 403
        assert not signed
        assert (await client.post(path, json={"uid": uid, "csr": "first"})).status_code == 200
        assert (await client.post(path, json={"uid": uid, "csr": "replacement"})).status_code == 403
        assert len(signed) == 1
        assert (
            await client.get("/v0/healthz", headers={"Authorization": "Bearer host"})
        ).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test:8088"
    ) as client:
        assert (await client.post(path, json={"uid": uid, "csr": "first"})).status_code == 404


async def test_reaper_uses_sandbox_activity_after_controller_restart():
    kube = Kube()
    now = [1000]
    ctl = controller(kube, warm=0, clock=lambda: now[0])
    pod = await ctl.acquire("a")
    await ctl.release(pod)
    now[0] += 901
    rebuilt = controller(kube, warm=0, clock=lambda: now[0])
    active = True

    async def upstream(request):
        return httpx.Response(200, json={"active": active, "last_activity": 1000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        rebuilt.client = client
        await rebuilt.maintain()
        assert pod["metadata"]["name"] in kube.items
        active = False
        await rebuilt.maintain()
        assert not kube.items


async def test_sandbox_activity_retains_sealed_worker_context(tmp_path, monkeypatch):
    from daimon_harness.service import create_app as service

    from .conftest import FakeModelAdapter

    monkeypatch.setenv("HARNESS_ROLE", "sandbox")
    app = service(home=tmp_path, token="test", adapter=FakeModelAdapter())
    async with app.router.lifespan_context(app):
        app.state.supervisor.contexts["sealed"] = object()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://test"
            ) as client:
                response = await client.get(
                    "/internal/activity", headers={"Authorization": "Bearer test"}
                )
                assert response.json()["active"] is True
        finally:
            app.state.supervisor.contexts.clear()


async def test_retention_waits_for_acquire_lock_and_rechecks_binding(tmp_path, monkeypatch):
    import os
    import time

    from mux.drivers.selfhosted.transport import sandbox_id

    monkeypatch.setenv("HARNESS_CHECKPOINT_RETENTION_SECONDS", "1")
    ctl = controller(Kube(), warm=0)
    app = create_app(controller=ctl, root=tmp_path, token="host")
    key = sandbox_id("returning")
    wal = tmp_path / "sessions" / key / "wal" / "turn.json"
    PVCStore(tmp_path).upload(str(wal.relative_to(tmp_path)), io.BytesIO(b"paid inventory"))
    os.utime(wal.parent.parent, (time.time() - 100, time.time() - 100))
    # acquire is paused inside its session lock while GC takes its initial scan.
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = ctl.kube.list

    async def paused(owner):
        entered.set()
        await proceed.wait()
        return await original(owner)

    ctl.kube.list = paused
    acquire = asyncio.create_task(ctl.acquire("returning"))
    await entered.wait()
    gc = asyncio.create_task(app.state.collect())
    await asyncio.sleep(0)
    assert not gc.done()
    proceed.set()
    pod = await acquire
    await gc
    assert wal.read_bytes() == b"paid inventory"
    await ctl.release(pod)


@pytest.mark.parametrize("reply", ["missing", "non-json", "wrong-types", "never", "keyerror"])
async def test_hostile_activity_is_bounded_and_other_pods_still_reap(reply):
    kube = Kube()
    now = [1000]
    ctl = controller(kube, warm=0, clock=lambda: now[0], probe_timeout=0.02)
    first = await ctl.acquire("bad")
    second = await ctl.acquire("healthy")
    await ctl.release(first)
    await ctl.release(second)
    now[0] += 901

    async def upstream(request):
        if request.url.path.endswith("bootstrap"):
            return httpx.Response(200)
        if request.headers["authorization"].endswith(ctl.credential(first, "c3")):
            if reply == "never":
                await asyncio.Event().wait()
            if reply == "keyerror":
                raise KeyError("hostile transport")
            if reply == "non-json":
                return httpx.Response(200, text="not json")
            return httpx.Response(
                200,
                json={"active": False}
                if reply == "missing"
                else {"active": "false", "last_activity": "yesterday"},
            )
        return httpx.Response(200, json={"active": False, "last_activity": 1000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        ctl.client = client
        async with asyncio.timeout(1):
            await ctl.maintain()
            assert second["metadata"]["name"] not in kube.items
            assert first["metadata"]["name"] in kube.items
            await ctl.maintain()
            await ctl.maintain()
        assert not kube.items


async def test_bootstrap_timeout_is_bounded():
    ctl = controller(Kube(), warm=0, probe_timeout=0.02)
    pod = await ctl.acquire("a")

    async def never(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(never)) as client:
        ctl.client = client
        with pytest.raises(TimeoutError):
            await ctl.bootstrap(pod)


@pytest.mark.parametrize("stale", [False, True])
async def test_bound_age_or_stale_template_recycles_only_after_idle_checkpoint(stale):
    from daimon_harness.controller import TEMPLATE

    now = [1000]
    ctl = controller(Kube(), warm=0, clock=lambda: now[0])
    pod = await ctl.acquire("a")
    await ctl.release(pod)
    if stale:
        ctl.kube.items[pod["metadata"]["name"]]["metadata"]["labels"][TEMPLATE] = "old"
    else:
        now[0] += 86401
    active, durable = True, False

    async def activity(request):
        return httpx.Response(
            200, json={"active": active, "last_activity": now[0], "durable": durable}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(activity)) as client:
        ctl.client = client
        await ctl.maintain()
        assert ctl.kube.items
        active = False
        await ctl.maintain()
        assert ctl.kube.items  # checkpoint is not yet durable
        durable = True
        await ctl.maintain()
        assert not ctl.kube.items


async def test_low_disk_preserves_events_cancel_and_replay(tmp_path, monkeypatch):
    import shutil

    from daimon_harness.service import create_app as sandbox

    from .conftest import FakeModelAdapter

    model = FakeModelAdapter()
    monkeypatch.setenv("HARNESS_ROLE", "sandbox")
    monkeypatch.setenv("HARNESS_CHECKPOINT_CONTROLLER_URL", "https://unused")
    service = sandbox(home=tmp_path / "sandbox", token="unused", adapter=model)
    ctl = controller(Kube(), warm=0)
    pod = await ctl.acquire("a")
    await ctl.release(pod)
    # Use the real service and journal while injecting only the per-pod C3 credential.
    bootstrap = service.state.bootstrap_handler
    from starlette.requests import Request

    body = __import__("json").dumps({"c3": ctl.credential(pod, "c3"), "checkpoint": "cap"}).encode()

    async def receive():
        return {"type": "http.request", "body": body}

    await bootstrap(
        Request(
            {"type": "http", "method": "POST", "path": "/internal/bootstrap", "headers": []},
            receive,
        )
    )
    service.state.supervisor.checkpoints = None
    service.state.supervisor.journal.reserve = None
    service.state.supervisor.journal.commit_inventory = None
    forward = httpx.AsyncClient(transport=httpx.ASGITransport(app=service))
    app = create_app(
        controller=ctl, root=tmp_path / "controller", token="host", http_client=forward
    )
    turn = {
        "session_id": "a",
        "turn_id": "t",
        "idempotency_key": "k",
        "model": "gpt-6-luna",
        "instructions": "",
        "mcp_servers": [],
        "skills": [],
        "limits": {"max_tool_calls": 3, "deadline_s": 30},
        "input": [{"type": "text", "text": "wait"}],
    }
    headers = {"Authorization": "Bearer host", "Accept": "application/json"}
    async with (
        service.router.lifespan_context(service),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as client,
    ):
        assert (await client.post("/v0/turns", json=turn, headers=headers)).status_code == 202
        await model.started.wait()
        monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 100, 0))
        assert (await client.get("/readiness")).status_code == 200
        assert (await client.get("/v0/sessions/a/events", headers=headers)).status_code == 200
        assert (await client.post("/v0/turns", json=turn, headers=headers)).status_code == 202
        fresh = dict(turn, turn_id="new", idempotency_key="new")
        assert (await client.post("/v0/turns", json=fresh, headers=headers)).status_code == 503
        assert (
            await client.post("/v0/turns/t/cancel?session_id=a", headers=headers)
        ).status_code == 200
        assert model.calls == 1
    await forward.aclose()


async def test_gc_enoent_and_session_touch_on_nested_write(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("HARNESS_CHECKPOINT_RETENTION_SECONDS", "1")
    store = PVCStore(tmp_path)
    store.upload("sessions/old/wal/t", io.BytesIO(b"wal"))
    session = tmp_path / "sessions/old"
    os.utime(session, (time.time() - 100, time.time() - 100))
    store.upload("sessions/old/wal/t", io.BytesIO(b"new inventory"))
    ctl = controller(Kube(), warm=0)
    app = create_app(controller=ctl, root=tmp_path, token="host")
    await app.state.collect()
    assert session.exists()
    os.utime(session, (time.time() - 100, time.time() - 100))
    import daimon_harness.controller as module

    def disappeared(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(module.shutil, "rmtree", disappeared)
    await app.state.collect()  # Concurrent removal is harmless.


async def test_sandbox_bootstrap_first_idempotent_conflict_and_keep(tmp_path, monkeypatch):
    from daimon_harness.service import create_app as service

    from .conftest import FakeModelAdapter

    monkeypatch.setenv("HARNESS_ROLE", "sandbox")
    monkeypatch.setenv("HARNESS_CHECKPOINT_CONTROLLER_URL", "https://controller:8089")
    monkeypatch.setenv("HARNESS_CHECKPOINT_KEEP", "7")
    app = service(home=tmp_path, token="test", adapter=FakeModelAdapter())
    # Direct handler: production HTTP access to this endpoint additionally requires mTLS.
    from starlette.requests import Request

    async def call(c3):
        body = __import__("json").dumps({"c3": c3, "checkpoint": "checkpoint-cap"}).encode()

        async def receive():
            return {"type": "http.request", "body": body}

        return await app.state.bootstrap_handler(
            Request(
                {"type": "http", "method": "POST", "path": "/internal/bootstrap", "headers": []},
                receive,
            )
        )

    from fastapi import HTTPException

    async with app.router.lifespan_context(app):
        assert await call("first-cap") == {"ok": True}
        checkpoints = app.state.supervisor.checkpoints
        assert checkpoints.keep == 7
        assert await call("first-cap") == {"ok": True}
        assert app.state.supervisor.checkpoints is checkpoints
        with pytest.raises(HTTPException) as exc:
            await call("changed-cap")
        assert exc.value.status_code == 409


async def test_maintenance_heartbeat_stale_fails_liveness(tmp_path, monkeypatch):
    import daimon_harness.controller as module

    app = create_app(controller=controller(Kube()), root=tmp_path, token="host")
    now = module.time.monotonic()
    monkeypatch.setattr(module.time, "monotonic", lambda: now + 61)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (await client.get("/liveness")).status_code == 503
