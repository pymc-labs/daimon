"""Eval pods on Kubernetes, with the worker's in-cluster service account (REST API via httpx).

One pod per item: an init container (`NET_ADMIN`) sets the packet rules, a gateway sidecar
runs as its own uid, and `main` has no network capabilities. Configs and keys travel in a
per-pod Secret mounted only where needed; workspace files arrive by exec (tar on stdin) and
`main` waits for the ready marker they end with. The pod gets no service account token,
runs under the runtime's default seccomp profile with resource limits and a deadline, and
owns its Secret, so deleting the pod (`stop`, `reap`) deletes the keys too.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlencode

import httpx
from daimon.core.evals.format import JsonObject
from daimon.core.evals.sandbox.runtime import (
    GATEWAY_UID,
    MAX_OUTPUT_BYTES,
    CommandResult,
    PodHandle,
    PodLimits,
    PodSpec,
    SandboxError,
    agent_command,
    environment_label,
    gateway_config,
    main_config,
    main_tar,
    pod_name,
    read_files,
    wait_ready,
)
from daimon.core.output_delivery import DeliverableFile
from pydantic import JsonValue
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Subprotocol

_ACCOUNT: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_PYTHON: Final = ["/opt/venv/bin/python", "-m", "daimon_eval_sandbox"]
_MAIN_CAPS: Final = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "SETPCAP", "KILL"]
_START_S: Final = 300
_STDOUT, _STDERR, _STATUS = 1, 2, 3
_ITEM_LABEL: Final = "daimon.eval/item"
_ENVIRONMENT_LABEL: Final = "daimon.eval/environment"
_SIDECAR: Final[JsonObject] = {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "1Gi"}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def secret_manifest(spec: PodSpec, name: str, pod_uid: str) -> JsonObject:
    owner: JsonObject = {"apiVersion": "v1", "kind": "Pod", "name": name, "uid": pod_uid}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": name,
            "labels": {_ITEM_LABEL: str(spec.item.id)},
            "ownerReferences": [owner],
        },
        "data": {
            "gateway-config.json": _b64(gateway_config(spec)),
            "gateway-ca.pem": _b64(spec.ca.gateway_pem),
            "main-pod.json": _b64(main_config(spec)),
            "main-ca.crt": _b64(spec.ca.cert_pem),
        },
    }


def _secret_volume(volume: str, name: str, items: dict[str, tuple[str, int]]) -> JsonObject:
    listed: list[JsonValue] = [{"key": k, "path": p, "mode": m} for k, (p, m) in items.items()]
    return {"name": volume, "secret": {"secretName": name, "items": listed}}


def pod_labels(spec: PodSpec) -> JsonObject:
    labels: JsonObject = {_ITEM_LABEL: str(spec.item.id)}
    if spec.shared_environment is not None:
        labels[_ENVIRONMENT_LABEL] = environment_label(spec.shared_environment)
    return labels


def pod_manifest(spec: PodSpec, name: str) -> JsonObject:
    no_caps: JsonObject = {"drop": ["ALL"]}
    gateway_mount: JsonObject = {
        "name": "gateway",
        "mountPath": "/run/eval/gateway",
        "readOnly": True,
    }
    limits = spec.limits
    main: JsonObject = {
        "cpu": f"{int(limits.cpus * 1000)}m",
        "memory": f"{limits.memory_mb}Mi",
        "ephemeral-storage": f"{limits.disk_mb}Mi",
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": pod_labels(spec)},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "activeDeadlineSeconds": limits.deadline_s,
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "initContainers": [
                {
                    "name": "net-init",
                    "image": spec.image,
                    "command": [*_PYTHON, "init"],
                    "securityContext": {
                        "runAsUser": 0,
                        "capabilities": {**no_caps, "add": ["NET_ADMIN", "NET_RAW"]},
                    },
                    "resources": {"requests": _SIDECAR, "limits": _SIDECAR},
                    "volumeMounts": [gateway_mount],
                }
            ],
            "containers": [
                {
                    "name": "gateway",
                    "image": spec.image,
                    "command": [*_PYTHON, "gateway"],
                    "securityContext": {
                        "runAsUser": GATEWAY_UID,
                        "runAsGroup": GATEWAY_UID,
                        "runAsNonRoot": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": no_caps,
                    },
                    "resources": {"requests": _SIDECAR, "limits": _SIDECAR},
                    "volumeMounts": [gateway_mount],
                },
                {
                    "name": "main",
                    "image": spec.image,
                    "command": [*_PYTHON, "main"],
                    "workingDir": "/workspace",
                    "securityContext": {
                        "runAsUser": 0,
                        "capabilities": {**no_caps, "add": [*_MAIN_CAPS]},
                    },
                    "resources": {"requests": main, "limits": main},
                    "volumeMounts": [
                        {"name": "main", "mountPath": "/run/eval/main", "readOnly": True},
                        {"name": "state", "mountPath": "/run/eval/state"},
                    ],
                },
            ],
            "volumes": [
                _secret_volume(
                    "gateway",
                    name,
                    {
                        "gateway-config.json": ("config.json", 0o444),
                        "gateway-ca.pem": ("ca.pem", 0o444),
                    },
                ),
                _secret_volume(
                    "main",
                    name,
                    {"main-pod.json": ("pod.json", 0o400), "main-ca.crt": ("ca.crt", 0o444)},
                ),
                {"name": "state", "emptyDir": {}},
            ],
        },
    }


def exit_code(status: JsonObject) -> int:
    """The exit code in a Kubernetes exec status message."""
    if status.get("status") == "Success":
        return 0
    details = cast(JsonObject, status.get("details") or {})
    for cause in cast(list[JsonObject], details.get("causes") or []):
        if cause.get("reason") == "ExitCode":
            return int(str(cause.get("message")))
    raise SandboxError(f"exec failed: {status.get('message')}")


def overdue_pods(pods: list[JsonObject], now: float) -> list[str]:
    """Names of pods that finished or outlived the deadline, Pending ones included.

    A pod that never schedules has no start time, so the cluster's own deadline never
    applies to it; its creation time does.
    """
    names: list[str] = []
    for pod in pods:
        meta = cast(JsonObject, pod["metadata"])
        created = datetime.fromisoformat(str(meta["creationTimestamp"])).timestamp()
        phase = cast(JsonObject, pod.get("status") or {}).get("phase")
        if phase in ("Failed", "Succeeded") or now - created > PodLimits().deadline_s:
            names.append(str(meta["name"]))
    return names


class KubernetesRuntime:
    def __init__(self, namespace: str, *, api: str | None = None, account: Path = _ACCOUNT) -> None:
        host, port = (
            os.environ.get("KUBERNETES_SERVICE_HOST"),
            os.environ.get("KUBERNETES_SERVICE_PORT"),
        )
        self.api = api or f"https://{host}:{port}"
        self.namespace = namespace
        self.account = account
        self.tls = ssl.create_default_context(cafile=str(account / "ca.crt"))
        self.client = httpx.AsyncClient(base_url=self.api, verify=self.tls, timeout=60)

    def _auth(self) -> dict[str, str]:
        # Re-read: projected tokens rotate.
        return {"authorization": f"Bearer {(self.account / 'token').read_text().strip()}"}

    def _path(self, kind: str, name: str = "") -> str:
        return f"/api/v1/namespaces/{self.namespace}/{kind}{f'/{name}' if name else ''}"

    async def _call(self, method: str, path: str, body: JsonObject | None = None) -> JsonObject:
        reply = await self.client.request(method, path, json=body, headers=self._auth())
        if reply.status_code >= 400:
            raise SandboxError(
                f"kubernetes {method} {path}: {reply.status_code} {reply.text[:300]}"
            )
        return cast(JsonObject, reply.json())

    async def _exec(
        self, name: str, argv: Sequence[str], stdin: bytes | None, timeout_s: float
    ) -> CommandResult:
        query = urlencode(
            [
                ("container", "main"),
                ("stdout", "true"),
                ("stderr", "true"),
                ("stdin", str(stdin is not None).lower()),
            ]
            + [("command", arg) for arg in argv]
        )
        url = f"{self.api.replace('https://', 'wss://')}{self._path('pods', name)}/exec?{query}"
        out, err, status = bytearray(), bytearray(), cast(JsonObject | None, None)
        async with (
            asyncio.timeout(timeout_s),
            connect(
                url,
                ssl=self.tls,
                additional_headers=self._auth(),
                max_size=None,
                subprotocols=[Subprotocol("v5.channel.k8s.io")],
            ) as socket,
        ):
            if stdin is not None:
                for at in range(0, len(stdin), 1 << 20):
                    await socket.send(b"\x00" + stdin[at : at + (1 << 20)])
                await socket.send(b"\xff\x00")  # close stdin (v5 protocol)
            try:
                async for message in socket:
                    data = message if isinstance(message, bytes) else message.encode()
                    channel, payload = data[0], data[1:]
                    if channel == _STDOUT:
                        out += payload
                    elif channel == _STDERR:
                        err += payload
                    elif channel == _STATUS:
                        status = cast(JsonObject, json.loads(payload))
                    if len(out) + len(err) > MAX_OUTPUT_BYTES:
                        raise SandboxError(f"command output is over {MAX_OUTPUT_BYTES} bytes")
            except ConnectionClosed:
                pass
        if status is None:
            raise SandboxError(f"exec in {name} ended without an exit status")
        return CommandResult(exit_code(status), bytes(out), bytes(err))

    async def _wait_running(self, name: str) -> JsonObject:
        deadline = time.monotonic() + _START_S
        while time.monotonic() < deadline:
            pod = await self._call("GET", self._path("pods", name))
            status = cast(JsonObject, pod.get("status") or {})
            if status.get("phase") in ("Failed", "Succeeded"):
                raise SandboxError(f"eval pod {name} stopped while starting")
            containers = cast(list[JsonObject], status.get("containerStatuses") or [])
            if status.get("phase") == "Running" and all(
                cast(JsonObject, c.get("state") or {}).get("running") for c in containers
            ):
                return status
            await asyncio.sleep(1)
        raise SandboxError(f"eval pod {name} did not start in {_START_S}s")

    async def start(self, spec: PodSpec) -> PodHandle:
        name = pod_name(spec)
        handle = PodHandle(name, "kubernetes")
        try:
            created = await self._call("POST", self._path("pods"), pod_manifest(spec, name))
            uid = str(cast(JsonObject, created["metadata"])["uid"])
            await self._call("POST", self._path("secrets"), secret_manifest(spec, name, uid))
            status = await self._wait_running(name)
            loaded = await self._exec(
                name,
                ["tar", "-x", "-p", "--same-owner", "-C", "/"],
                main_tar(spec, configs=False),
                300,
            )
            if loaded.exit_code != 0:
                raise SandboxError(f"pod files did not load: {loaded.stderr[:300]!r}")
            await wait_ready(self, handle)
        except (SandboxError, httpx.HTTPError, OSError, TimeoutError):
            await self.stop(handle)
            raise
        return PodHandle(name, "kubernetes", str(status.get("podIP") or "") or None)

    async def run(self, pod: PodHandle, command: str, *, timeout_s: float = 300) -> CommandResult:
        return await self._exec(pod.id, agent_command(command), None, timeout_s)

    async def files(self, pod: PodHandle, root: str) -> list[DeliverableFile]:
        return await read_files(self, pod, root)

    async def stop(self, pod: PodHandle) -> None:
        for kind in ("pods", "secrets"):
            reply = await self.client.delete(
                self._path(kind, pod.id), params={"gracePeriodSeconds": "0"}, headers=self._auth()
            )
            if reply.status_code not in (200, 202, 404):
                raise SandboxError(f"kubernetes delete {kind}/{pod.id}: {reply.status_code}")

    async def reap(self) -> list[str]:
        """Delete finished or overdue eval pods (a crashed worker's) and their Secrets."""
        query = urlencode({"labelSelector": _ITEM_LABEL})
        listed = await self._call("GET", f"{self._path('pods')}?{query}")
        names = overdue_pods(cast(list[JsonObject], listed.get("items") or []), time.time())
        for name in names:
            await self.stop(PodHandle(name, "kubernetes"))
        return names

    async def stop_environment(self, environment_id: str) -> list[str]:
        query = urlencode(
            {"labelSelector": f"{_ENVIRONMENT_LABEL}={environment_label(environment_id)}"}
        )
        listed = await self._call("GET", f"{self._path('pods')}?{query}")
        pods = cast(list[JsonObject], listed.get("items") or [])
        names = [str(cast(JsonObject, p["metadata"])["name"]) for p in pods]
        for name in names:
            await self.stop(PodHandle(name, "kubernetes"))
        return names
