"""Every provider's outputs are delivered once through one sweep, then leave the listing.

Anthropic runs over the transport-level fake client, Gemini over its real driver
with a fake transport (a workspace file cannot be removed, so a ledger hides it),
self-hosted over its real driver with a fake harness, and a third port removes
deleted artifacts outright.
"""

from __future__ import annotations

import ast
import base64
import io
import re
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
from anthropic.types.beta import FileMetadata
from daimon.core.output_delivery import DeliverableFile, sweep_outputs
from daimon.core.output_runtime import AnthropicOutputRuntime, ArtifactOutputRuntime, OutputRuntime
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.ports import Agents, Artifacts, Environments
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    Artifact,
    Environment,
    EnvironmentSpec,
    SessionSpec,
)
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.selfhosted import SelfHostedDriver, wire
from mux.drivers.selfhosted.transport import DirectTransport
from mux.errors import ProviderError
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
STAMP = NOW.isoformat()
FILES = {"report.csv": b"a,b\n1,2\n", "notes.txt": b"hello"}


async def _no_sleep(_: float) -> None:
    return None


async def _deliver(runtime: OutputRuntime) -> dict[str, bytes]:
    got: dict[str, bytes] = {}

    async def post(file: DeliverableFile) -> None:
        got[file.filename] = file.content

    count = await sweep_outputs(runtime, post=post, sleep=_no_sleep)
    assert count == len(got), "the count is the number of files posted"
    return got


def _anthropic() -> tuple[OutputRuntime, list[str]]:
    live = {f"file_{i}": (name, body) for i, (name, body) in enumerate(FILES.items())}
    deleted: list[str] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                FileMetadata(
                    id=id_,
                    created_at=NOW,
                    filename=name,
                    mime_type="text/plain",
                    size_bytes=len(body),
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
                for id_, (name, body) in live.items()
            ]
        )

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deleted.append(match.group(1))
        live.pop(match.group(1), None)
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=live[match.group(1)][1]),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    client = build_fake_anthropic(router.dispatch)
    return AnthropicOutputRuntime(client, "sesn_1", scope=SCOPE), deleted


class _RemovingArtifacts:
    """A port whose delete removes the artifact, as OpenAI's does."""

    def __init__(self) -> None:
        self.live = dict(FILES)
        self.deleted: list[str] = []

    def _ref(self, name: str) -> ResourceRef:
        return ResourceRef(provider="openai", kind="artifact", id=name, account_scope_id="a")

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest | None = None
    ) -> Page[Artifact]:
        data = tuple(
            Artifact(
                ref=self._ref(name),
                filename=name,
                media_type="text/plain",
                size_bytes=len(body),
                created_at=NOW,
            )
            for name, body in self.live.items()
        )
        return Page(data=data, has_more=False)

    async def download(self, scope: Scope, ref: ResourceRef) -> AsyncIterator[bytes]:
        body = self.live[ref.id]
        yield body[:2]
        yield body[2:]

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        if ref.id not in self.live:
            raise ProviderError("not_found", retryable=False)
        del self.live[ref.id]
        self.deleted.append(ref.id)
        return DeletionReceipt(operation_id=key, deleted=(ref,))


def _snapshot(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


async def _gemini() -> tuple[OutputRuntime, FakeTransport]:
    transport = FakeTransport()
    ma = GeminiManagedAgents(
        transport, storage=MemoryStorage(), state_store=MemoryStateStore(), account_scope_id="p"
    )
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    env = await ma.environments.create(SCOPE, EnvironmentSpec(name="e"), key="env")
    session = await ma.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=env.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={
                        "thread": {
                            "channel": {"tenant_id": "t", "platform": "discord", "channel_id": "c"},
                            "thread_id": "th",
                        },
                        "binding_id": "binding",
                    },
                )
            },
        ),
        key="session",
    )
    stamp = datetime.now(UTC).isoformat()
    transport.responses.append(
        {
            "id": "i1",
            "status": "completed",
            "created": stamp,
            "updated": stamp,
            "environment_id": "e1",
            "steps": [],
            "usage": None,
        }
    )
    await ma.events.send(
        SCOPE, session.ref, (UserMessage(content=(TextPart(text="hi"),)),), key="s"
    )
    transport.snapshots["e1"] = _snapshot(FILES)
    return ArtifactOutputRuntime(ma.artifacts, session.ref, scope=SCOPE), transport


class _Record:
    """An agent or environment port that only answers `retrieve`."""

    def __init__(self, value: Agent | Environment) -> None:
        self.value = value

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent | Environment:
        return self.value


class _SelfHostedHarness:
    """The harness output routes over files the agent saved in its workspace."""

    def __init__(self, *, legacy: bool = False) -> None:
        self.saved = dict(FILES)
        self.legacy = legacy

    def _ids(self) -> dict[str, str]:
        return {wire.output_id(path, f"1-{len(body)}-1"): path for path, body in self.saved.items()}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.legacy:
            return httpx.Response(404, json={"detail": "Not Found"})
        tail = request.url.path.split("/outputs", 1)[1].strip("/").split("/")
        ids = self._ids()
        if tail == [""]:
            listed = [
                {"id": key, "path": path, "size_bytes": len(self.saved[path]), "modified_at": STAMP}
                for key, path in ids.items()
            ]
            return httpx.Response(200, json={"outputs": listed})
        path = ids[tail[0]]
        if tail[1:] == ["delete"]:
            del self.saved[path]
            return httpx.Response(200, json={"deleted": True, "id": tail[0]})
        body = base64.b64encode(self.saved[path]).decode()
        return httpx.Response(
            200, json={"id": tail[0], "size_bytes": len(self.saved[path]), "content_b64": body}
        )


async def _selfhosted(client: httpx.AsyncClient) -> OutputRuntime:
    def ref(kind: str) -> ResourceRef:
        return ResourceRef(
            provider="selfhosted", kind=kind, id=kind, account_scope_id="t", tenant_id="t"
        )

    spec = AgentSpec(name="a", model=ModelRef(provider="selfhosted", id="model"))
    agent = Agent(ref=ref("agent"), revision=Revision(local=1), spec=spec, created_at=NOW)
    env = Environment(
        ref=ref("environment"),
        revision=Revision(local=1),
        spec=EnvironmentSpec(name="e", execution="self_hosted"),
        created_at=NOW,
    )
    driver = SelfHostedDriver(
        DirectTransport(client, base_url="https://harness", token="token"),
        account_scope_id="t",
        agents=cast(Agents, _Record(agent)),
        environments=cast(Environments, _Record(env)),
        authorization=lambda scope, kind, id_: scope == SCOPE,
        deployment="model",
    )
    session = await driver.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=env.ref,
            config_revision=1,
        ),
        key="session",
    )
    return ArtifactOutputRuntime(driver.artifacts, session.ref, scope=SCOPE)


async def test_anthropic_outputs_are_delivered_once_then_deleted() -> None:
    runtime, deleted = _anthropic()
    assert await _deliver(runtime) == FILES, "every file is posted with its bytes"
    assert sorted(deleted) == ["file_0", "file_1"], "each posted file is deleted"
    assert await _deliver(runtime) == {}, "nothing is delivered twice"


async def test_removing_port_outputs_are_delivered_once_then_deleted() -> None:
    artifacts = _RemovingArtifacts()
    session = ResourceRef(provider="openai", kind="session", id="s", account_scope_id="a")
    runtime = ArtifactOutputRuntime(cast(Artifacts, artifacts), session, scope=SCOPE)
    assert await _deliver(runtime) == FILES, "every file is posted with its bytes"
    assert sorted(artifacts.deleted) == sorted(FILES), "each posted file is deleted"
    assert await _deliver(runtime) == {}, "nothing is delivered twice"


async def test_selfhosted_outputs_are_delivered_once_then_deleted() -> None:
    """Files a self-hosted agent saves reach the reply, then leave its workspace."""
    harness = _SelfHostedHarness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        runtime = await _selfhosted(client)
        assert await _deliver(runtime) == FILES, "every saved file is posted with its bytes"
        assert harness.saved == {}, "each posted file is deleted from the workspace"
        assert await _deliver(runtime) == {}, "nothing is delivered twice"


async def test_selfhosted_outputs_when_harness_predates_files_then_reply_unaffected() -> None:
    """A harness without the output routes delivers no files and raises nothing."""
    harness = _SelfHostedHarness(legacy=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        runtime = await _selfhosted(client)
        assert await _deliver(runtime) == {}, "an old harness delivers nothing"
    assert harness.saved == FILES, "nothing is deleted on an old harness"


async def test_gemini_outputs_are_delivered_once_then_ledgered() -> None:
    runtime, transport = await _gemini()
    assert await _deliver(runtime) == FILES, "every file is posted with its bytes"
    assert await _deliver(runtime) == {}, "the ledger hides delivered workspace files"
    transport.snapshots["e1"] = _snapshot({**FILES, "notes.txt": b"edited"})
    assert await _deliver(runtime) == {"notes.txt": b"edited"}, (
        "a rewritten file is delivered again"
    )


_ALLOWED = {"output_delivery.py", "output_ports_compat.py", "output_runtime.py"}


def test_no_module_sweeps_or_lists_files_outside_the_runtime_layer() -> None:
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in root.glob("*/**/daimon/**/*.py"):
        if path.name in _ALLOWED or "tests" in path.parts or ".venv" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in {"sweep_session_outputs"}:
                offenders.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id == "sweep_session_outputs":
                offenders.append(f"{path}:{node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "list"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "files"
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"output files go through the output runtime: {offenders}"
