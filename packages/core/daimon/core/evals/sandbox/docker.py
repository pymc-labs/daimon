"""Eval pods on a Docker Engine, local socket or remote over TLS (Engine HTTP API via httpx).

A pod is a `net` container (packet rules, then the gateway as its own uid) and a `main`
container sharing its network namespace with no network capabilities. Files go in through
the archive endpoint before each container starts, so secrets never touch argv or env.
Each pod has cpu, memory and pids limits and an expiry label `reap` acts on; Docker has no
per-container disk cap.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import struct
import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

import httpx
from daimon.core.config import EvalsSettings
from daimon.core.evals.sandbox.runtime import (
    MAX_OUTPUT_BYTES,
    CommandResult,
    PodHandle,
    PodSpec,
    SandboxError,
    agent_command,
    environment_label,
    gateway_tar,
    main_tar,
    pod_name,
    read_files,
    wait_ready,
)
from daimon.core.output_delivery import DeliverableFile

_PYTHON: Final = ["/opt/venv/bin/python", "-m", "daimon_eval_sandbox"]
#: DAC_READ_SEARCH lets the root packet-rule step read the gateway's own config.
_NET_CAPS: Final = ["NET_ADMIN", "NET_RAW", "DAC_READ_SEARCH", "SETUID", "SETGID", "SETPCAP"]
#: What the main entrypoint needs to set up and drop to the executor; no network caps.
_MAIN_CAPS: Final = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "SETPCAP", "KILL"]
_ITEM_LABEL: Final = "daimon.eval.item"
_EXPIRES_LABEL: Final = "daimon.eval.expires"
_ENVIRONMENT_LABEL: Final = "daimon.eval.environment"
_NET_LIMITS: Final = {"Memory": 512 << 20, "NanoCpus": 500_000_000, "PidsLimit": 256}


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
        if network in ("host", "none") or (network or "").startswith("container:"):
            raise SandboxError(f"eval pods need a network of their own, not {network!r}")
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
        expires = int(time.time()) + spec.limits.deadline_s
        labels = {_ITEM_LABEL: str(spec.item.id), _EXPIRES_LABEL: str(expires)}
        if spec.shared_environment is not None:
            labels[_ENVIRONMENT_LABEL] = environment_label(spec.shared_environment)
        try:
            net_host: dict[str, object] = {"CapDrop": ["ALL"], "CapAdd": _NET_CAPS, **_NET_LIMITS}
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
                        "Memory": spec.limits.memory_mb << 20,
                        "NanoCpus": int(spec.limits.cpus * 1e9),
                        "PidsLimit": spec.limits.pids,
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
            logs = await self.logs(name)
            await self.stop(PodHandle(name, "docker"))
            raise SandboxError(f"{exc}\n{logs}") from exc
        return PodHandle(name, "docker", address)

    async def run(self, pod: PodHandle, command: str, *, timeout_s: float = 300) -> CommandResult:
        body = {"Cmd": agent_command(command), "AttachStdout": True, "AttachStderr": True}
        made = await self._call("POST", f"/containers/{pod.id}-main/exec", json=body)
        exec_id = str(made.json()["Id"])
        stream = bytearray()
        async with (
            asyncio.timeout(timeout_s),
            self.client.stream(
                "POST", f"/exec/{exec_id}/start", json={"Detach": False, "Tty": False}
            ) as reply,
        ):
            if reply.status_code >= 400:
                raise SandboxError(f"docker exec {pod.id}: {reply.status_code}")
            async for chunk in reply.aiter_bytes():
                stream += chunk
                if len(stream) > MAX_OUTPUT_BYTES:
                    raise SandboxError(f"command output is over {MAX_OUTPUT_BYTES} bytes")
        out, err = demux(bytes(stream))
        state = (await self._call("GET", f"/exec/{exec_id}/json")).json()
        return CommandResult(int(state.get("ExitCode") or 0), out, err)

    async def files(self, pod: PodHandle, root: str) -> list[DeliverableFile]:
        return await read_files(self, pod, root)

    async def logs(self, name: str) -> str:
        """The pod's container log tails, for diagnosing a failed pod."""
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

    async def _remove(self, label: str, keep: Callable[[dict[str, str]], bool]) -> list[str]:
        """Remove the pods whose containers carry `label` and whose labels `keep` picks."""
        filters = json.dumps({"label": [label]})
        listed = await self._call(
            "GET", "/containers/json", params={"all": "1", "filters": filters}
        )
        names = sorted(
            {
                str(c["Names"][0]).lstrip("/").rsplit("-", 1)[0]
                for c in listed.json()
                if keep(c["Labels"])
            }
        )
        for name in names:
            await self.stop(PodHandle(name, "docker"))
        return names

    async def reap(self) -> list[str]:
        """Remove eval pods past their expiry, e.g. a crashed worker's."""
        now = time.time()
        return await self._remove(_EXPIRES_LABEL, lambda labels: int(labels[_EXPIRES_LABEL]) < now)

    async def stop_environment(self, environment_id: str) -> list[str]:
        label = f"{_ENVIRONMENT_LABEL}={environment_label(environment_id)}"
        return await self._remove(label, lambda _labels: True)
