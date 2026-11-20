"""The sandbox runtime port: start an enclosed pod for one eval item, run in it, read it, stop it.

A pod is two containers on one network namespace (`apps/eval-sandbox`): the gateway, which
alone reaches the network, and `main`, which runs the executor and the agent's commands.
Both runtimes deliver the same files, built here.
"""

from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import posixpath
import re
import shlex
import tarfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

from daimon.core.evals.sandbox.ca import EvalCA
from daimon.core.evals.world.model import ItemRef
from daimon.core.features.snapshot import FeatureSnapshot
from daimon.core.output_delivery import DeliverableFile
from mux.contracts.ids import Provider
from mux.profiles import CONTROL_PLANE_HOSTS

#: The worker the pod runs: a provider's environment worker, or "probe" (none).
Executor = Provider | Literal["probe"]
#: The image's uids (`apps/eval-sandbox/image`).
AGENT_UID: Final = 10001
GATEWAY_UID: Final = 61000
AGENT_SHELL: Final = "/opt/eval/agent-bin/bash"
#: The most a command's output or a files archive may hold; reading stops past it.
MAX_OUTPUT_BYTES: Final = 256 * 1024 * 1024
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Names that would change how the agent's programs load or what clock they see.
_RESERVED_ENV: Final = re.compile(r"LD_.*|FAKETIME.*|BASH_ENV|ENV|SHELLOPTS|BASHOPTS|PS4")


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class PodFile:
    """A file the agent finds in its workspace (absolute path in the main container)."""

    path: str
    content: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class PodLimits:
    """What one pod may use. Kubernetes caps disk too; pids there follow the node's limit."""

    cpus: float = 2.0
    memory_mb: int = 4096
    disk_mb: int = 20480
    pids: int = 4096
    #: After this the pod is stopped where the runtime can, and `reap` removes it.
    deadline_s: int = 4 * 3600


@dataclass(frozen=True)
class PodSpec:
    item: ItemRef
    image: str
    executor: Executor
    world_url: str
    world_token: str
    ca: EvalCA
    #: The executor's credentials and settings: its environment only, never the agent's.
    executor_config: Mapping[str, str] = field(default_factory=dict[str, str])
    files: Sequence[PodFile] = ()
    #: The agent's environment (the case's `world.env.vars`).
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    control_plane: frozenset[str] = frozenset()
    #: Hosts an admin allows the executor besides its control plane.
    passthrough: frozenset[str] = frozenset()
    registries: Literal["passthrough", "deny"] = "deny"
    clock: datetime | None = None
    limits: PodLimits = PodLimits()


@dataclass(frozen=True)
class PodHandle:
    id: str
    runtime: Literal["docker", "kubernetes"]
    #: Where the pod accepts inbound connections (the harness port), if reachable.
    address: str | None = None


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


class SandboxRuntime(Protocol):
    async def start(self, spec: PodSpec) -> PodHandle: ...
    async def run(
        self, pod: PodHandle, command: str, *, timeout_s: float = 300
    ) -> CommandResult: ...
    async def files(self, pod: PodHandle, root: str) -> list[DeliverableFile]: ...
    async def stop(self, pod: PodHandle) -> None: ...
    async def reap(self) -> list[str]:
        """Remove eval pods past their deadline or finished, e.g. a crashed worker's."""
        ...


async def wait_ready(runtime: SandboxRuntime, pod: PodHandle, timeout_s: float = 300) -> None:
    """Until the agent shell works: files in, gateway up, eval CA trusted."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            result = await runtime.run(pod, "true", timeout_s=30)
            if result.exit_code == 0:
                return
            last = f"exit {result.exit_code}: {result.stderr[-500:].decode(errors='replace')}"
        except SandboxError as exc:
            last = str(exc)
        await asyncio.sleep(1)
    raise SandboxError(f"eval pod {pod.id} was not ready in {timeout_s:.0f}s ({last})")


def registries(features: FeatureSnapshot) -> Literal["passthrough", "deny"]:
    """Whether the gateway lets package downloads reach the real registries."""
    return "passthrough" if features.value("eval_package_registries") == "passthrough" else "deny"


def pod_name(spec: PodSpec) -> str:
    return f"eval-{spec.item.id.hex[:20]}"


def agent_command(command: str) -> list[str]:
    """argv that runs a shell command in `main` as the agent, with the agent environment."""
    return [AGENT_SHELL, "-c", command]


async def read_files(runtime: SandboxRuntime, pod: PodHandle, root: str) -> list[DeliverableFile]:
    """Files under `root`, archived by the agent so its links never reach past its own reach."""
    command = f"cd -- {shlex.quote(root)} 2>/dev/null && exec tar -c . || true"
    listed = await runtime.run(pod, command, timeout_s=300)
    return untar_files(listed.stdout, root) if listed.stdout else []


def gateway_config(spec: PodSpec) -> bytes:
    hosts = sorted(
        spec.control_plane
        | (frozenset[str]() if spec.executor == "probe" else CONTROL_PLANE_HOSTS[spec.executor])
        | spec.passthrough
    )
    return json.dumps(
        {
            "world_url": spec.world_url,
            "world_token": spec.world_token,
            "item": str(spec.item.id),
            "control_plane": hosts,
            "registries": spec.registries,
        }
    ).encode()


def main_config(spec: PodSpec) -> bytes:
    for name in spec.env:
        if not _ENV_NAME.fullmatch(name) or _RESERVED_ENV.fullmatch(name):
            raise SandboxError(f"the agent environment cannot set {name!r}")
    clock = spec.clock.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") if spec.clock else None
    return json.dumps(
        {
            "executor": spec.executor,
            "exec_env": dict(spec.executor_config),
            "agent_env": dict(spec.env),
            "clock": clock,
        }
    ).encode()


@dataclass(frozen=True)
class _Entry:
    path: str
    content: bytes | None  # None: a directory
    mode: int
    uid: int = 0


def _tar(entries: Sequence[_Entry]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for entry in entries:
            info = tarfile.TarInfo(entry.path.lstrip("/"))
            info.mode, info.uid, info.gid = entry.mode, entry.uid, entry.uid
            if entry.content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(entry.content)
                tar.addfile(info, io.BytesIO(entry.content))
    return buffer.getvalue()


def gateway_tar(spec: PodSpec) -> bytes:
    gid = GATEWAY_UID
    return _tar(
        [
            _Entry("/run/eval/gateway", None, 0o500, gid),
            _Entry("/run/eval/gateway/config.json", gateway_config(spec), 0o400, gid),
            _Entry("/run/eval/gateway/ca.pem", spec.ca.gateway_pem, 0o400, gid),
        ]
    )


def workspace_entries(spec: PodSpec) -> list[_Entry]:
    entries: list[_Entry] = []
    for file in spec.files:
        path = posixpath.normpath(file.path)
        if not path.startswith("/workspace/"):
            raise SandboxError(f"pod files go under /workspace: {file.path}")
        parent = posixpath.dirname(path)
        while parent != "/workspace" and all(e.path != parent for e in entries):
            entries.append(_Entry(parent, None, 0o2775, AGENT_UID))
            parent = posixpath.dirname(parent)
        entries.append(_Entry(path, file.content, file.mode & 0o777, AGENT_UID))
    return sorted(entries, key=lambda e: (e.path.count("/"), e.path))


def main_tar(spec: PodSpec, *, configs: bool) -> bytes:
    """Workspace files, then the ready marker; with `configs`, the main container's secrets."""
    head = (
        [
            _Entry("/run/eval/main", None, 0o700),
            _Entry("/run/eval/main/pod.json", main_config(spec), 0o600),
            _Entry("/run/eval/main/ca.crt", spec.ca.cert_pem, 0o644),
        ]
        if configs
        else []
    )
    ready = [_Entry("/run/eval/state", None, 0o755), _Entry("/run/eval/state/ready", b"", 0o644)]
    return _tar([*head, *workspace_entries(spec), *ready])


def untar_files(data: bytes, root: str) -> list[DeliverableFile]:
    """Regular files of a tar stream, named relative to `root`."""
    files: list[DeliverableFile] = []
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            total += member.size
            if total > MAX_OUTPUT_BYTES:
                raise SandboxError(f"files under {root} are over {MAX_OUTPUT_BYTES} bytes")
            reader = tar.extractfile(member)
            content = reader.read() if reader else b""
            name = posixpath.normpath(member.name).removeprefix("./")
            base = posixpath.basename(root.rstrip("/"))
            relative = posixpath.relpath(name, base) if name.startswith(f"{base}/") else name
            mime = mimetypes.guess_type(relative)[0] or "application/octet-stream"
            files.append(DeliverableFile(relative, relative, mime, len(content), content))
    return files
