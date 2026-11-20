"""Eval pods on a Docker Engine, local socket or remote over TLS (Engine HTTP API via httpx).

A pod is a `net` container (packet rules, then the gateway as its own uid) and a `main`
container sharing its network namespace with no network capabilities. Files go in through
the archive endpoint before each container starts, so secrets never touch argv or env.
"""

from __future__ import annotations

import asyncio
import ssl
import struct
from typing import Final
from urllib.parse import urlsplit

import httpx
from daimon.core.config import EvalsSettings
from daimon.core.evals.sandbox.runtime import (
    CommandResult,
    PodHandle,
    PodSpec,
    SandboxError,
    agent_command,
    gateway_tar,
    main_tar,
    pod_name,
    untar_files,
    wait_ready,
)
from daimon.core.output_delivery import DeliverableFile

_PYTHON: Final = ["/opt/venv/bin/python", "-m", "daimon_eval_sandbox"]
_NET_CAPS: Final = ["NET_ADMIN", "NET_RAW", "SETUID", "SETGID", "SETPCAP"]
#: What the main entrypoint needs to set up and drop to the executor; no network caps.
_MAIN_CAPS: Final = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "SETPCAP", "KILL"]


def docker_client(settings: EvalsSettings) -> httpx.AsyncClient:
    url = urlsplit(settings.docker_host)
    if url.scheme == "unix":
        transport = httpx.AsyncHTTPTransport(uds=url.path)
        return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=120)
    files = (settings.docker_tls_ca, settings.docker_tls_cert, settings.docker_tls_key)
    if url.scheme != "tcp" or not all(files):
        raise SandboxError("a remote Docker host needs tcp:// and all three TLS files")
    ca, cert, key = files
    context = ssl.create_default_context(cafile=str(ca))
    context.load_cert_chain(str(cert), str(key))
    return httpx.AsyncClient(base_url=f"https://{url.netloc}", verify=context, timeout=120)


def demux(stream: bytes) -> tuple[bytes, bytes]:
    """Split Docker's multiplexed exec stream into stdout and stderr."""
    out, err, at = bytearray(), bytearray(), 0
    while at + 8 <= len(stream):
        kind, size = stream[at], struct.unpack(">I", stream[at + 4 : at + 8])[0]
        (err if kind == 2 else out).extend(stream[at + 8 : at + 8 + size])
        at += 8 + size
    return bytes(out), bytes(err)


class DockerRuntime:
    def __init__(self, client: httpx.AsyncClient, *, network: str | None = None) -> None:
        self.client = client
        self.network = network

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: object = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        reply = await self.client.request(
            method, path, params=params, json=json, content=content, headers=headers
        )
        if reply.status_code >= 400:
            raise SandboxError(f"docker {method} {path}: {reply.status_code} {reply.text[:300]}")
        return reply

    async def _create(self, name: str, body: dict[str, object], files: bytes) -> str:
        created = await self._call("POST", "/containers/create", params={"name": name}, json=body)
        container = str(created.json()["Id"])
        await self._call(
            "PUT",
            f"/containers/{container}/archive",
            params={"path": "/"},
            content=files,
            headers={"content-type": "application/x-tar"},
        )
        await self._call("POST", f"/containers/{container}/start")
        return container

    async def start(self, spec: PodSpec) -> PodHandle:
        name = pod_name(spec)
        labels = {"daimon.eval.item": str(spec.item.id)}
        try:
            net_host: dict[str, object] = {"CapDrop": ["ALL"], "CapAdd": _NET_CAPS}
            if self.network:
                net_host["NetworkMode"] = self.network
            net = await self._create(
                f"{name}-net",
                {
                    "Image": spec.image,
                    "Cmd": [*_PYTHON, "net"],
                    "Labels": labels,
                    "HostConfig": net_host,
                },
                gateway_tar(spec),
            )
            await self._create(
                f"{name}-main",
                {
                    "Image": spec.image,
                    "Labels": labels,
                    "WorkingDir": "/workspace",
                    "HostConfig": {
                        "NetworkMode": f"container:{net}",
                        "CapDrop": ["ALL"],
                        "CapAdd": _MAIN_CAPS,
                    },
                },
                main_tar(spec, configs=True),
            )
            inspected = (await self._call("GET", f"/containers/{net}/json")).json()
            networks = dict(inspected.get("NetworkSettings", {}).get("Networks") or {})
            address = next(
                (str(n["IPAddress"]) for n in networks.values() if n.get("IPAddress")), None
            )
            await wait_ready(self, PodHandle(name, "docker"))
        except (SandboxError, httpx.HTTPError) as exc:
            logs = await self._logs(name)
            await self.stop(PodHandle(name, "docker"))
            raise SandboxError(f"{exc}\n{logs}") from exc
        return PodHandle(name, "docker", address)

    async def run(self, pod: PodHandle, command: str, *, timeout_s: float = 300) -> CommandResult:
        body = {"Cmd": agent_command(command), "AttachStdout": True, "AttachStderr": True}
        made = await self._call("POST", f"/containers/{pod.id}-main/exec", json=body)
        exec_id = str(made.json()["Id"])
        async with asyncio.timeout(timeout_s):
            stream = await self._call(
                "POST", f"/exec/{exec_id}/start", json={"Detach": False, "Tty": False}
            )
        out, err = demux(stream.content)
        state = (await self._call("GET", f"/exec/{exec_id}/json")).json()
        return CommandResult(int(state.get("ExitCode") or 0), out, err)

    async def files(self, pod: PodHandle, root: str) -> list[DeliverableFile]:
        reply = await self.client.get(f"/containers/{pod.id}-main/archive", params={"path": root})
        if reply.status_code == 404:
            return []
        if reply.status_code >= 400:
            raise SandboxError(f"docker archive {root}: {reply.status_code}")
        return untar_files(reply.content, root)

    async def _logs(self, name: str) -> str:
        """The containers' log tails, for a pod that failed to start."""
        tails: list[str] = []
        for part in ("net", "main"):
            reply = await self.client.get(
                f"/containers/{name}-{part}/logs", params={"stdout": 1, "stderr": 1, "tail": 50}
            )
            out, err = demux(reply.content) if reply.status_code == 200 else (b"", b"")
            tails.append(f"[{part}]\n{(out + err).decode(errors='replace')}")
        return "\n".join(tails)

    async def stop(self, pod: PodHandle) -> None:
        for part in ("main", "net"):
            reply = await self.client.delete(
                f"/containers/{pod.id}-{part}", params={"force": "true"}
            )
            if reply.status_code not in (204, 404):
                raise SandboxError(f"docker remove {pod.id}-{part}: {reply.status_code}")
