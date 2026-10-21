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
    return Controller(kube, {"spec": {"containers": [{"env": []}]}}, "test", **kw)


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
        from daimon_harness.controller import capability

        cap = {"Authorization": "Bearer " + capability(pod)}
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
        assert blocked.status_code == 503
        assert len(seen) == 1
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

    monkeypatch.setenv("HARNESS_CHECKPOINT_CONTROLLER_URL", "http://controller:8088")
    monkeypatch.setenv("HARNESS_CHECKPOINT_TOKEN", "pod-capability")
    monkeypatch.setenv("HARNESS_CHECKPOINT_KEEP", "2")
    configured = configured_checkpoints()
    assert configured is not None and configured.keep == 2
    assert "HARNESS_CHECKPOINT_TOKEN" not in __import__("os").environ
    configured.close()
    monkeypatch.setenv("HARNESS_CHECKPOINT_TOKEN", "orphan")
    with pytest.raises(ValueError, match="requires a controller URL"):
        configured_checkpoints()


def test_http_controller_requires_explicit_consent(monkeypatch):
    monkeypatch.delenv("HARNESS_CONTROLLER_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(ValueError, match="allow_insecure_http"):
        create_app(token="caller")
