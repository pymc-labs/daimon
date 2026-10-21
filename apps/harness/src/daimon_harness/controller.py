"""Namespaced pod controller and unchanged direct C3 transport gateway.

One controller holds a PVC flock. Pod labels are the routing source of truth;
resourceVersion preconditions arbitrate warm-pod claims. No tool runs here.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hmac
import json
import os
import secrets
import time
from collections import defaultdict
from collections.abc import Callable, Generator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from mux.drivers.selfhosted.transport import sandbox_id
from starlette.background import BackgroundTask

from .auth import Authenticator
from .checkpoints import MAX_ARCHIVE_BYTES
from .journal import identifier
from .pvc_store import PVCStore

OWNER = "daimon-controller"
SESSION = "daimon-session"
TOUCHED = "daimon-last-active"


class PodAPI(Protocol):
    async def list(self, owner: str) -> list[dict[str, Any]]: ...
    async def create(self, pod: dict[str, Any]) -> None: ...
    async def patch(self, pod: dict[str, Any], values: dict[str, Any]) -> None: ...
    async def delete(self, pod: dict[str, Any]) -> None: ...


class ServiceAccountAuth(httpx.Auth):
    def __init__(self, token: Path):
        self.token = token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        # Projected service-account tokens rotate; do not cache them at startup.
        request.headers["Authorization"] = "Bearer " + self.token.read_text().strip()
        yield request


class Kubernetes:
    def __init__(self, namespace: str):
        account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.client = httpx.AsyncClient(
            base_url="https://"
            + os.environ["KUBERNETES_SERVICE_HOST"]
            + ":"
            + os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443"),
            verify=str(account / "ca.crt"),
            auth=ServiceAccountAuth(account / "token"),
            timeout=30,
            trust_env=False,
        )
        self.path = f"/api/v1/namespaces/{namespace}/pods"

    async def list(self, owner: str) -> list[dict[str, Any]]:
        response = await self.client.get(self.path, params={"labelSelector": f"{OWNER}={owner}"})
        response.raise_for_status()
        return response.json()["items"]

    async def create(self, pod: dict[str, Any]) -> None:
        response = await self.client.post(self.path, json=pod)
        if response.status_code != 409:
            response.raise_for_status()

    async def patch(self, pod: dict[str, Any], values: dict[str, Any]) -> None:
        path = self.path + "/" + pod["metadata"]["name"]
        current = pod
        for _ in range(8):
            response = await self.client.patch(
                path,
                headers={"Content-Type": "application/merge-patch+json"},
                json={
                    "metadata": {
                        "resourceVersion": current["metadata"]["resourceVersion"],
                        **values,
                    }
                },
            )
            if response.status_code != 409:
                response.raise_for_status()
                return
            # Kubelet status updates also advance resourceVersion. Retry only
            # while the immutable binding and UID still match our observation.
            fresh = await self.client.get(path)
            fresh.raise_for_status()
            current = fresh.json()
            if (
                current["metadata"]["uid"] != pod["metadata"]["uid"]
                or current["metadata"]["labels"][SESSION] != pod["metadata"]["labels"][SESSION]
                or current["metadata"].get("deletionTimestamp")
            ):
                raise HTTPException(503, "sandbox binding changed; retry")
        raise HTTPException(503, "sandbox metadata busy; retry")

    async def delete(self, pod: dict[str, Any]) -> None:
        response = await self.client.request(
            "DELETE",
            self.path + "/" + pod["metadata"]["name"],
            json={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": pod["metadata"]["uid"]},
            },
        )
        if response.status_code != 404:
            response.raise_for_status()


def capability(pod: dict[str, Any]) -> str:
    return next(
        env["value"]
        for env in pod["spec"]["containers"][0]["env"]
        if env["name"] == "HARNESS_CHECKPOINT_TOKEN"
    )


def ready(pod: dict[str, Any]) -> bool:
    return bool(pod.get("status", {}).get("podIP")) and any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in pod.get("status", {}).get("conditions", [])
    )


def dead(pod: dict[str, Any]) -> bool:
    status = pod.get("status", {})
    return status.get("phase") in {"Failed", "Succeeded"} or any(
        "terminated" in item.get("state", {}) for item in status.get("containerStatuses", [])
    )


class Controller:
    def __init__(
        self,
        kube: PodAPI,
        template: dict[str, Any],
        owner: str,
        *,
        warm: int = 1,
        idle: float = 900,
        clock: Callable[[], float] = time.time,
    ):
        if not 0 <= warm <= 2:
            raise ValueError("warm pool must contain 0–2 pods")
        self.kube, self.template, self.owner = kube, template, owner
        self.warm, self.idle, self.clock = warm, idle, clock
        self.lock = asyncio.Lock()
        self.active: dict[str, int] = defaultdict(int)

    async def pods(self) -> list[dict[str, Any]]:
        return await self.kube.list(self.owner)

    async def make(self, name: str, session: str = "warm") -> None:
        pod = copy.deepcopy(self.template)
        pod.update(apiVersion="v1", kind="Pod")
        pod["metadata"] = {
            "name": name,
            "labels": {OWNER: self.owner, SESSION: session},
            "annotations": {TOUCHED: str(self.clock())},
        }
        pod["spec"]["containers"][0].setdefault("env", []).append(
            {"name": "HARNESS_CHECKPOINT_TOKEN", "value": secrets.token_urlsafe(32)}
        )
        await self.kube.create(pod)

    async def maintain(self) -> None:
        async with self.lock:
            pods = await self.pods()
            for pod in pods:
                name = pod["metadata"]["name"]
                touched = float(pod["metadata"].get("annotations", {}).get(TOUCHED, self.clock()))
                if dead(pod):
                    await self.kube.delete(pod)
                    continue
                if (
                    pod["metadata"]["labels"][SESSION] != "warm"
                    and not self.active[name]
                    and self.clock() - touched >= self.idle
                ):
                    await self.kube.delete(pod)
            pods = await self.pods()
            warm = [p for p in pods if p["metadata"]["labels"][SESSION] == "warm"]
            for _ in range(max(0, self.warm - len(warm))):
                await self.make(self.owner + "-warm-" + secrets.token_hex(5))

    async def acquire(self, session: str) -> dict[str, Any]:
        key = sandbox_id(session)
        async with self.lock:
            pods = await self.pods()
            found = next((p for p in pods if p["metadata"]["labels"][SESSION] == key), None)
            if found is None:
                found = next(
                    (
                        p
                        for p in pods
                        if p["metadata"]["labels"][SESSION] == "warm"
                        and ready(p)
                        and not p["metadata"].get("deletionTimestamp")
                    ),
                    None,
                )
                if found is not None:
                    await self.kube.patch(
                        found,
                        {"labels": {SESSION: key}, "annotations": {TOUCHED: str(self.clock())}},
                    )
                    found = next(
                        p
                        for p in await self.pods()
                        if p["metadata"]["name"] == found["metadata"]["name"]
                    )
                else:
                    await self.make(self.owner + "-" + key, key)
                    found = next(
                        p for p in await self.pods() if p["metadata"]["labels"][SESSION] == key
                    )
            if found["metadata"].get("deletionTimestamp"):
                raise HTTPException(503, "sandbox is terminating; retry")
            await self.kube.patch(found, {"annotations": {TOUCHED: str(self.clock())}})
            self.active[found["metadata"]["name"]] += 1
        try:
            async with asyncio.timeout(180):
                while True:
                    candidates = await self.pods()
                    current = next(
                        (p for p in candidates if p["metadata"]["uid"] == found["metadata"]["uid"]),
                        None,
                    )
                    if current is None or dead(current):
                        if current is not None:
                            await self.kube.delete(current)
                        raise HTTPException(503, "sandbox lost; retry to restore")
                    if ready(current):
                        return current
                    await asyncio.sleep(0.2)
        except BaseException as exc:
            await self.release(found)
            if isinstance(exc, TimeoutError):
                raise HTTPException(503, "sandbox startup timed out; retry") from None
            raise

    async def release(self, pod: dict[str, Any]) -> None:
        async with self.lock:
            name = pod["metadata"]["name"]
            self.active[name] = max(0, self.active[name] - 1)
            current = next(
                (p for p in await self.pods() if p["metadata"]["uid"] == pod["metadata"]["uid"]),
                None,
            )
            if current and not current["metadata"].get("deletionTimestamp"):
                await self.kube.patch(current, {"annotations": {TOUCHED: str(self.clock())}})


def create_app(
    *,
    controller: Controller | None = None,
    root: Path | None = None,
    token: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    auth = Authenticator(token)
    if controller is None:
        if os.environ.get("HARNESS_CONTROLLER_ALLOW_INSECURE_HTTP") != "1":
            raise ValueError(
                "controller HTTP sandbox hop requires explicit allow_insecure_http consent (F76)"
            )
        controller = Controller(
            Kubernetes(os.environ["HARNESS_NAMESPACE"]),
            json.loads(Path(os.environ["HARNESS_POD_TEMPLATE"]).read_text()),
            os.environ["HARNESS_CONTROLLER_NAME"],
            warm=int(os.environ.get("HARNESS_WARM_POOL", "1")),
        )
    ctl = controller
    store = PVCStore(root or Path("/checkpoints"))
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(180, read=None), trust_env=False
    )

    async def maintenance() -> None:
        while True:
            with suppress(httpx.HTTPError):
                await ctl.maintain()
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with (store.root / ".controller.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await ctl.pods()  # Rebuild from API before becoming ready.
            task = asyncio.create_task(maintenance())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await client.aclose()
                if isinstance(ctl.kube, Kubernetes):
                    await ctl.kube.client.aclose()

    app = FastAPI(lifespan=lifespan)

    async def upstream_unavailable(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": "sandbox or durability service unavailable; retry"}, status_code=503
        )

    app.add_exception_handler(httpx.HTTPError, upstream_unavailable)

    @app.get("/readiness")
    async def readiness():
        return {"ok": True}

    @app.api_route("/internal/checkpoints", methods=["GET"])
    @app.api_route("/internal/checkpoints/{name:path}", methods=["GET", "PUT", "DELETE"])
    async def objects(request: Request, name: str = ""):
        value = request.headers.get("authorization", "").removeprefix("Bearer ")
        pod = next((p for p in await ctl.pods() if hmac.compare_digest(capability(p), value)), None)
        if pod is None or pod["metadata"]["labels"][SESSION] == "warm":
            raise HTTPException(403)
        key = name or request.query_params.get("prefix", "")
        if not key.startswith("sessions/" + pod["metadata"]["labels"][SESSION] + "/"):
            raise HTTPException(403)
        if request.method == "GET":
            if not name:
                return await asyncio.to_thread(store.list, key)
            path = store.path(name)
            if not path.is_file():
                raise HTTPException(404)
            return FileResponse(path)
        if request.method == "PUT":
            # Spool before atomic replacement, bounded even with chunked uploads.
            import tempfile

            with tempfile.TemporaryFile(dir=store.root) as source:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise HTTPException(413)
                    await asyncio.to_thread(source.write, chunk)
                source.seek(0)
                await asyncio.to_thread(store.upload, name, source)
        else:
            await asyncio.to_thread(store.delete, name)
        return Response(status_code=204)

    @app.api_route("/v0/{path:path}", methods=["GET", "POST"])
    async def proxy(request: Request, path: str):
        await auth.verify(request.headers.get("authorization"))
        if path == "healthz":
            return {
                "ok": True,
                "protocol": "daimon-harness/0",
                "version": os.environ.get("HARNESS_VERSION", "controller"),
            }
        # C3.9a permits an explicit plaintext C3 bearer hop, but does not
        # grant a plaintext exception for the per-turn MCP credential carrier.
        if request.headers.get("x-daimon-mcp-authorization"):
            raise HTTPException(503, "per-turn MCP credentials require sandbox TLS (C3.9a/F76)")
        body = await request.body()
        session = request.query_params.get("session_id")
        if path == "turns" and request.method == "POST":
            try:
                body_session = json.loads(body)["session_id"]
            except (ValueError, KeyError, TypeError):
                raise HTTPException(422, "session_id required") from None
            if session and session != body_session:
                raise HTTPException(409, "session mismatch")
            session = body_session
        elif path.startswith("sessions/") and path.endswith("/events"):
            path_session = path.removeprefix("sessions/").removesuffix("/events")
            if session and session != path_session:
                raise HTTPException(409, "session mismatch")
            session = path_session
        elif not (path.startswith("turns/") and path.endswith(("/cancel", "/actions"))):
            raise HTTPException(404)
        if not isinstance(session, str) or not session or len(session) > 256:
            raise HTTPException(422, "session_id required")
        try:
            identifier(session)
        except ValueError:
            raise HTTPException(422, "invalid session_id") from None
        pod = await ctl.acquire(session)
        closed = False

        async def close(upstream: httpx.Response | None = None):
            nonlocal closed
            if not closed:
                closed = True
                if upstream is not None:
                    await upstream.aclose()
                await ctl.release(pod)

        try:
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower()
                in {"authorization", "accept", "content-type", "x-daimon-mcp-authorization"}
            }
            url = f"http://{pod['status']['podIP']}:8088/v0/{path}"
            outgoing = client.build_request(
                request.method,
                url,
                params=str(request.query_params),
                headers=headers,
                content=body,
            )
            upstream = await client.send(outgoing, stream=True)

            async def chunks():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await asyncio.shield(close(upstream))

            return StreamingResponse(
                chunks(),
                status_code=upstream.status_code,
                headers={
                    k: v
                    for k, v in upstream.headers.items()
                    if k.lower() in {"content-type", "cache-control"}
                },
                background=BackgroundTask(close, upstream),
            )
        except BaseException:
            await close()
            raise

    app.state.handlers = (readiness, objects, proxy)
    return app
