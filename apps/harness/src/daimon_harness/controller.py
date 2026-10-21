"""Namespaced pod controller and unchanged direct C3 transport gateway.

One controller holds a PVC flock. Pod labels are the routing source of truth;
resourceVersion preconditions arbitrate warm-pod claims. No tool runs here.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import hmac
import io
import json
import logging
import math
import os
import secrets
import shutil
import ssl
import time
from collections import defaultdict
from collections.abc import Callable, Generator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from mux.drivers.selfhosted.transport import sandbox_id
from starlette.background import BackgroundTask
from starlette.middleware.base import RequestResponseEndpoint

from .auth import Authenticator
from .checkpoints import MAX_ARCHIVE_BYTES
from .journal import identifier
from .pvc_store import PVCStore

OWNER = "daimon-controller"
SESSION = "daimon-session"
TOUCHED = "daimon-last-active"
TEMPLATE = "daimon-template"
CREATED = "daimon-created"
CHECKPOINT_PENDING = "daimon-checkpoint-pending"
logger = logging.getLogger(__name__)


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
        maximum: int = 4,
        clock: Callable[[], float] = time.time,
        max_age: float = 86400,
        hard_age: float = 2 * 86400,
        unknown_limit: int = 3,
        probe_timeout: float = 4,
    ):
        if not 0 <= warm <= 2 or maximum < 1 or warm > maximum:
            raise ValueError("invalid sandbox capacity/warm pool")
        if not 0 < max_age < hard_age < 7 * 86400 or unknown_limit < 1:
            raise ValueError("sandbox lifetime must remain below certificate validity")
        self.max_age, self.hard_age = max_age, hard_age
        self.unknown_limit, self.probe_timeout = unknown_limit, probe_timeout
        self.unknown: dict[str, int] = defaultdict(int)
        self.checkpoint_blocked: set[str] = set()
        self.observed: set[str] = set()
        self.kube, self.template, self.owner = kube, template, owner
        self.warm, self.idle, self.maximum, self.clock = warm, idle, maximum, clock
        self.version = hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()[
            :32
        ]
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.allocating = False
        self.active: dict[str, int] = defaultdict(int)
        self.inventory: list[dict[str, Any]] = []
        self.client: httpx.AsyncClient | None = None
        self.seed = secrets.token_bytes(32)
        self.capabilities: dict[str, dict[str, Any]] = {}
        self.bootstrapped: set[str] = set()

    def credential(self, pod: dict[str, Any], purpose: str) -> str:
        return hmac.new(
            self.seed, (purpose + pod["metadata"]["uid"]).encode(), "sha256"
        ).hexdigest()

    async def pods(self) -> list[dict[str, Any]]:
        self.inventory = await self.kube.list(self.owner)
        bound = {
            p["metadata"]["uid"]: p
            for p in self.inventory
            if p["metadata"]["labels"][SESSION] != "warm"
            and not p["metadata"].get("deletionTimestamp")
        }
        self.checkpoint_blocked.intersection_update(bound)
        # Restore before maintenance: a new controller must obtain a fresh
        # durable=true report even if the previous controller observed success.
        self.checkpoint_blocked.update(bound.keys() - self.observed)
        self.checkpoint_blocked.update(
            uid
            for uid, p in bound.items()
            if p["metadata"].get("annotations", {}).get(CHECKPOINT_PENDING) == "true"
        )
        self.observed = set(bound)
        self.capabilities = {
            self.credential(p, "checkpoint"): p
            for p in self.inventory
            if p["metadata"]["labels"][SESSION] != "warm"
            and not p["metadata"].get("deletionTimestamp")
        }
        return self.inventory

    async def make(self, name: str, session: str = "warm") -> None:
        pod = copy.deepcopy(self.template)
        pod.update(apiVersion="v1", kind="Pod")
        pod["metadata"] = {
            "name": name,
            "labels": {OWNER: self.owner, SESSION: session, TEMPLATE: self.version},
            "annotations": {TOUCHED: str(self.clock()), CREATED: str(self.clock())},
        }
        await self.kube.create(pod)

    async def bootstrap(self, pod: dict[str, Any]) -> None:
        uid = pod["metadata"]["uid"]
        if uid in self.bootstrapped or self.client is None:
            return
        async with asyncio.timeout(self.probe_timeout):
            response = await self.client.post(
                self.url(pod) + "/internal/bootstrap",
                json={
                    "c3": self.credential(pod, "c3"),
                    "checkpoint": self.credential(pod, "checkpoint"),
                },
            )
            response.raise_for_status()
        self.bootstrapped.add(uid)

    @staticmethod
    def url(pod: dict[str, Any]) -> str:
        return f"https://{pod['status']['podIP']}:8088"

    async def maintain(self) -> None:
        pods = await self.pods()
        for pod in pods:
            try:
                await self.maintain_pod(pod)
            except Exception as exc:
                # Never log untrusted response bodies or credentials.
                logger.warning("sandbox_maintenance_failed reason=%s", type(exc).__name__)
        if self.allocating:
            return
        self.allocating = True
        try:
            pods = await self.pods()
            warm = sum(p["metadata"]["labels"][SESSION] == "warm" for p in pods)
            for _ in range(min(max(0, self.warm - warm), self.maximum - len(pods))):
                await self.make(self.owner + "-warm-" + secrets.token_hex(5))
        finally:
            self.allocating = False

    async def maintain_pod(self, pod: dict[str, Any]) -> None:
        meta = pod["metadata"]
        if meta.get("deletionTimestamp"):
            return
        warm = meta["labels"][SESSION] == "warm"
        stale = meta["labels"].get(TEMPLATE) != self.version
        annotations = meta.get("annotations", {})
        # Kubernetes timestamp survives controller restarts; annotation covers fake API.
        from datetime import datetime

        born = float(annotations.get(CREATED, self.clock()))
        if meta.get("creationTimestamp"):
            born = datetime.fromisoformat(
                meta["creationTimestamp"].replace("Z", "+00:00")
            ).timestamp()
        age = self.clock() - born
        if dead(pod) or (warm and (stale or age >= self.max_age)):
            await self.kube.delete(pod)
            return
        if warm:
            return
        async with self.locks[meta["labels"][SESSION]]:
            if self.active[meta["name"]]:
                return
            touched = float(annotations.get(TOUCHED, self.clock()))
            if self.clock() - touched < self.idle and age < self.max_age and not stale:
                return
            if self.client is not None:
                try:
                    async with asyncio.timeout(self.probe_timeout):
                        await self.bootstrap(pod)
                        response = await self.client.get(
                            self.url(pod) + "/internal/activity",
                            headers={"Authorization": "Bearer " + self.credential(pod, "c3")},
                        )
                        response.raise_for_status()
                        raw_activity = response.json()
                        if not isinstance(raw_activity, dict):
                            raise ValueError("invalid activity")
                        activity = cast(dict[str, Any], raw_activity)
                        active, last = activity.get("active"), activity.get("last_activity")
                        durable = activity.get("durable", False)
                        if (
                            type(active) is not bool
                            or type(last) not in (int, float)
                            or not math.isfinite(cast(float, last))
                            or type(durable) is not bool
                        ):
                            raise ValueError("invalid activity")
                    self.unknown.pop(meta["uid"], None)
                    if not durable:
                        self.checkpoint_blocked.add(meta["uid"])
                        if annotations.get(CHECKPOINT_PENDING) != "true":
                            await self.kube.patch(
                                pod, {"annotations": {CHECKPOINT_PENDING: "true"}}
                            )
                        logger.warning("sandbox_eviction_blocked reason=checkpoint_pending")
                        return
                    if annotations.get(CHECKPOINT_PENDING) == "true":
                        await self.kube.patch(pod, {"annotations": {CHECKPOINT_PENDING: "false"}})
                    self.checkpoint_blocked.discard(meta["uid"])
                    if age < self.hard_age and (
                        active
                        or (
                            age < self.max_age
                            and not stale
                            and self.clock() - cast(float, last) < self.idle
                        )
                    ):
                        return
                except Exception as exc:
                    self.unknown[meta["uid"]] += 1
                    logger.warning("sandbox_activity_unknown reason=%s", type(exc).__name__)
                    if meta["uid"] in self.checkpoint_blocked or (
                        self.unknown[meta["uid"]] < self.unknown_limit and age < self.hard_age
                    ):
                        return
            # Lost/wedged workers reconcile from the write-ahead inventory on restore.
            await self.kube.delete(pod)
            self.unknown.pop(meta["uid"], None)

    async def acquire(self, session: str) -> dict[str, Any]:
        key = sandbox_id(session)
        async with self.locks[key]:
            pods = await self.pods()
            found = next((p for p in pods if p["metadata"]["labels"][SESSION] == key), None)
            if found is None:
                # Reserve allocation synchronously; unrelated routes never await a global lock.
                if self.allocating:
                    raise HTTPException(
                        503, "sandbox allocation busy; retry", headers={"Retry-After": "2"}
                    )
                self.allocating = True
                try:
                    pods = await self.pods()
                    found = next(
                        (
                            p
                            for p in pods
                            if p["metadata"]["labels"][SESSION] == "warm"
                            and p["metadata"]["labels"].get(TEMPLATE) == self.version
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
                    else:
                        if len(pods) >= self.maximum:
                            raise HTTPException(
                                503,
                                "Busy, try again shortly",
                                headers={"Retry-After": "2", "X-Daimon-Error": "sandbox_busy"},
                            )
                        await self.make(self.owner + "-" + key, key)
                    found = next(
                        (p for p in await self.pods() if p["metadata"]["labels"][SESSION] == key),
                        None,
                    )
                    if found is None:
                        raise HTTPException(503, "sandbox name conflict; retry")
                finally:
                    self.allocating = False
            if found["metadata"].get("deletionTimestamp"):
                raise HTTPException(503, "sandbox is terminating; retry")
            self.active[found["metadata"]["name"]] += 1
            try:
                async with asyncio.timeout(180):
                    while True:
                        current = next(
                            (
                                p
                                for p in await self.pods()
                                if p["metadata"]["uid"] == found["metadata"]["uid"]
                            ),
                            None,
                        )
                        if current is None or dead(current):
                            if current is not None:
                                await self.kube.delete(current)
                            raise HTTPException(503, "sandbox lost; retry to restore")
                        if ready(current):
                            await self.bootstrap(current)
                            return current
                        await asyncio.sleep(0.2)
            except BaseException as exc:
                self.active[found["metadata"]["name"]] -= 1
                if isinstance(exc, TimeoutError):
                    raise HTTPException(503, "sandbox startup timed out; retry") from None
                raise

    async def release(self, pod: dict[str, Any]) -> None:
        self.active[pod["metadata"]["name"]] = max(0, self.active[pod["metadata"]["name"]] - 1)


def create_app(
    *,
    controller: Controller | None = None,
    root: Path | None = None,
    token: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    auth = Authenticator(token)
    production = controller is None
    if controller is None:
        controller = Controller(
            Kubernetes(os.environ["HARNESS_NAMESPACE"]),
            json.loads(Path(os.environ["HARNESS_POD_TEMPLATE"]).read_text()),
            os.environ["HARNESS_CONTROLLER_NAME"],
            warm=int(os.environ.get("HARNESS_WARM_POOL", "1")),
            maximum=int(os.environ.get("HARNESS_MAX_SANDBOXES", "4")),
            idle=float(os.environ.get("HARNESS_IDLE_SECONDS", "300")),
        )
    ctl = controller
    store = PVCStore(root or Path("/checkpoints"))
    context = ssl.create_default_context(cafile=os.environ.get("HARNESS_SANDBOX_CA"))
    if os.environ.get("HARNESS_CONTROLLER_CLIENT_CERT"):
        context.load_cert_chain(
            os.environ["HARNESS_CONTROLLER_CLIENT_CERT"],
            os.environ["HARNESS_CONTROLLER_CLIENT_KEY"],
        )
    client = http_client or httpx.AsyncClient(
        verify=context, timeout=httpx.Timeout(180, read=None), trust_env=False
    )
    control_client = httpx.AsyncClient(verify=context, timeout=4, trust_env=False)
    ctl.client = control_client if production else None
    maintenance_task: asyncio.Task[None] | None = None
    retention = float(os.environ.get("HARNESS_CHECKPOINT_RETENTION_SECONDS", str(30 * 86400)))
    buckets: dict[str, tuple[float, int]] = {}

    def space_ok() -> bool:
        disk = shutil.disk_usage(store.root)
        return disk.free >= max(
            int(os.environ.get("HARNESS_MIN_FREE_BYTES", str(1024**3))),
            disk.total * float(os.environ.get("HARNESS_MIN_FREE_FRACTION", "0.1")),
        )

    async def collect() -> None:
        sessions = store.root / "sessions"
        with suppress(FileNotFoundError):
            for directory in sessions.iterdir():
                async with ctl.locks[directory.name]:
                    # Recheck the live binding after taking the SAME lock as acquire/restore.
                    bound = {p["metadata"]["labels"][SESSION] for p in await ctl.pods()}
                    if directory.name in bound:
                        continue
                    with suppress(FileNotFoundError):
                        if (
                            directory.is_dir()
                            and time.time() - directory.stat().st_mtime > retention
                        ):
                            await asyncio.to_thread(shutil.rmtree, directory)
                            from .checkpoints import sync_directory

                            await asyncio.to_thread(sync_directory, sessions)

    heartbeat = time.monotonic()

    async def maintenance() -> None:
        nonlocal heartbeat
        while True:
            try:
                await ctl.maintain()
                await collect()
            except Exception as exc:
                logger.warning("controller_maintenance_failed reason=%s", type(exc).__name__)
            heartbeat = time.monotonic()
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal maintenance_task
        with (store.root / ".controller.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seed = store.root / ".capability-key"
            if not seed.exists():
                store.upload(".capability-key", io.BytesIO(secrets.token_bytes(32)))
            ctl.seed = seed.read_bytes()
            if len(ctl.seed) != 32:
                raise RuntimeError("invalid persistent capability key")
            await ctl.pods()  # Rebuild from API before becoming ready.
            task = asyncio.create_task(maintenance())
            maintenance_task = task
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await client.aclose()
                await control_client.aclose()
                if isinstance(ctl.kube, Kubernetes):
                    await ctl.kube.client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def upstream_unavailable(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": "sandbox or durability service unavailable; retry"}, status_code=503
        )

    app.add_exception_handler(httpx.HTTPError, upstream_unavailable)
    app.add_exception_handler(OSError, upstream_unavailable)

    @app.get("/liveness")
    async def liveness():
        if (
            maintenance_task is not None and maintenance_task.done()
        ) or time.monotonic() - heartbeat > 60:
            raise HTTPException(503, "maintenance heartbeat stale")
        return {"ok": True}

    @app.get("/readiness")
    async def readiness():
        await liveness()
        return {
            "ok": True,
            "admission_available": space_ok(),
            "checkpoint_blocked_sandboxes": len(ctl.checkpoint_blocked),
        }

    @app.middleware("http")
    async def separate_ports(request: Request, call_next: RequestResponseEndpoint) -> Response:
        port = request.scope.get("server", (None, 0))[1]
        internal = request.url.path.startswith("/internal/")
        if (port == 8089 and not internal) or (port == 8088 and internal):
            return Response(status_code=404)
        return await call_next(request)

    @app.post("/internal/certificate")
    async def certificate(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                raise HTTPException(413)
        try:
            raw_body = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "invalid certificate request") from None
        if not isinstance(raw_body, dict):
            raise HTTPException(422, "invalid certificate request")
        body = cast(dict[str, Any], raw_body)
        if not isinstance(body.get("csr"), str) or not isinstance(body.get("uid"), str):
            raise HTTPException(422, "invalid certificate request")
        uid = body.get("uid", "")
        pod = next(
            (
                p
                for p in ctl.inventory
                if p["metadata"]["uid"] == uid
                and p.get("status", {}).get("podIP")
                == (request.client.host if request.client else "")
                and not p["metadata"].get("deletionTimestamp")
            ),
            None,
        )
        if pod is None:
            raise HTTPException(403)
        now = time.monotonic()
        bucket = "certificate-" + uid
        stamp, count = buckets.get(bucket, (now, 0))
        if now - stamp >= 60:
            stamp, count = now, 0
        if count >= 10:
            raise HTTPException(429, "certificate rate limit", headers={"Retry-After": "60"})
        buckets[bucket] = (stamp, count + 1)
        from .pod_tls import issue

        async with ctl.locks["certificate-" + uid]:
            csr = body["csr"].encode()
            pin = store.root / ".certificates" / uid
            if pin.exists() and pin.read_bytes() != csr:
                raise HTTPException(403, "pod public key already pinned")
            try:
                cert = issue(
                    csr,
                    pod["status"]["podIP"],
                    Path(os.environ["HARNESS_SANDBOX_CA"]),
                    Path(os.environ["HARNESS_SANDBOX_CA_KEY"]),
                )
            except ValueError:
                raise HTTPException(422, "invalid certificate request") from None
            if not pin.exists():
                store.upload(".certificates/" + uid, io.BytesIO(csr))
            return Response(cert, media_type="application/x-pem-file")

    @app.api_route("/internal/checkpoints", methods=["GET"])
    @app.api_route("/internal/checkpoints/{name:path}", methods=["GET", "PUT", "DELETE"])
    async def objects(request: Request, name: str = ""):
        value = request.headers.get("authorization", "").removeprefix("Bearer ")
        pod = ctl.capabilities.get(value)
        if pod is None:
            raise HTTPException(403)
        now = time.monotonic()
        stamp, count = buckets.get(value, (now, 0))
        if now - stamp >= 1:
            stamp, count = now, 0
        if count >= 100:
            raise HTTPException(429, "checkpoint rate limit", headers={"Retry-After": "1"})
        buckets[value] = (stamp, count + 1)
        key = name or request.query_params.get("prefix", "")
        if not key.startswith("sessions/" + pod["metadata"]["labels"][SESSION] + "/"):
            raise HTTPException(403)

        async def validate_binding() -> None:
            # Deletion and commits share this lock. Rebuild capabilities from the
            # API so an upload spooled before deletion cannot resurrect its data.
            await ctl.pods()
            current = ctl.capabilities.get(value)
            if current is None or (
                current["metadata"]["uid"] != pod["metadata"]["uid"]
                or current["metadata"]["labels"][SESSION] != pod["metadata"]["labels"][SESSION]
            ):
                raise HTTPException(410, "sandbox binding retired")

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
                async with ctl.locks[pod["metadata"]["labels"][SESSION]]:
                    await validate_binding()
                    await asyncio.to_thread(store.upload, name, source)
        else:
            async with ctl.locks[pod["metadata"]["labels"][SESSION]]:
                await validate_binding()
                await asyncio.to_thread(store.delete, name)
        return Response(status_code=204)

    @app.delete("/v0/sessions/{session}")
    async def delete_session(session: str, request: Request):
        await auth.verify(request.headers.get("authorization"))
        try:
            identifier(session)
        except ValueError:
            raise HTTPException(422, "invalid session_id") from None
        key = sandbox_id(session)
        async with ctl.locks[key]:
            pods = [p for p in await ctl.pods() if p["metadata"]["labels"][SESSION] == key]
            for pod in pods:
                await ctl.kube.delete(pod)
            if pods:
                raise HTTPException(503, "session deleting; retry after sandbox terminates")
            directory = store.root / "sessions" / key
            if directory.exists():
                await asyncio.to_thread(shutil.rmtree, directory)
                from .checkpoints import sync_directory

                await asyncio.to_thread(sync_directory, directory.parent)
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
        low_disk = not space_ok()
        if (
            low_disk
            and not any(
                p["metadata"]["labels"][SESSION] == sandbox_id(session) for p in await ctl.pods()
            )
            and not (store.root / "sessions" / sandbox_id(session)).exists()
        ):
            raise HTTPException(
                503, "checkpoint disk near full; retry", headers={"Retry-After": "30"}
            )
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
            headers["authorization"] = "Bearer " + ctl.credential(pod, "c3")
            if low_disk:
                headers["x-daimon-admission-blocked"] = "true"
            url = ctl.url(pod) + f"/v0/{path}"
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

    app.state.collect = collect
    app.state.handlers = (readiness, objects, proxy, separate_ports, certificate, delete_session)
    return app
