"""Replay-first F1 adapter over the pinned Agents SDK, with native evidence.

Hosted Bash implements bash/read/grep/glob; distinct Bash invocations of the
hosted apply_patch command implement write/edit. All six map to native bash.
This is a declared capability mapping, not six invented provider tool names.
Agent revision fingerprints are descriptive: F1 explicitly provisions unpinned.
No credentials, live clients, recorder bypasses or verdicts are discovered here.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping
from email import message_from_bytes, policy
from types import TracebackType

import httpx
from openai import AsyncOpenAI

from mux.conformance.default_capability import (
    BuiltinCapability,
    DefaultCapabilityAdapter,
    DefaultCapabilityReplayEvents,
    DefaultManifest,
)
from mux.conformance.recording import Replay
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import Agent, AgentSpec, SkillUpload, SkillUploadFile, ToolSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import objects
from mux.drivers.openai.agents import agent_spec
from mux.drivers.openai.skill_bindings import decode
from mux.drivers.openai.transport import Object, SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions

BUILTIN_MAPPING: Mapping[BuiltinCapability, ToolSpec] = {
    name: ToolSpec(name="bash", kind="builtin")
    for name in ("bash", "read", "edit", "grep", "glob", "write")
}
MODEL = ModelRef(provider="openai", id="gpt-6-luna")
SCOPE = Scope(tenant_id="f1", account_id="offline", principal_id="probe", authorization_id="f1")


def decode_upload(request: httpx.Request) -> SkillUpload:
    """Decode the actual multipart ZIP independently of the desired manifest."""
    message = message_from_bytes(
        ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
        + request.content,
        policy=policy.default,
    )
    files = tuple(part for part in message.iter_parts() if part.get_filename())
    if len(files) != 1 or files[0].get_filename() != "bundle.zip":
        raise ValueError("unexpected skill multipart")
    payload = files[0].get_payload(decode=True)
    if not isinstance(payload, bytes):
        raise ValueError("missing skill ZIP bytes")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return SkillUpload(
            files=tuple(
                SkillUploadFile(path=name.removeprefix("bundle/"), content=archive.read(name))
                for name in archive.namelist()
            )
        )


class OpenAIDefaultCapabilityTransport:
    """Ordered native provisioning script; evidence is decoded upstream I/O."""

    def __init__(
        self, manifest: DefaultManifest, native_events: tuple[Object, ...], *, replay: bool
    ) -> None:
        self.native_events = native_events
        self.requests: list[httpx.Request] = []
        self._uploads: list[SkillUpload] = []
        # Use realistic long provider IDs, so all eleven pins require chunking.
        self.skill_ids = tuple("skill_" + "x" * 42 + f"{n:02d}" for n in range(11))
        self._names = tuple(skill.name for skill in manifest.skills)
        self._agent: Object | None = None
        self._session: Object | None = None
        self._steps: list[tuple[str, str]] = []
        for identity in self.skill_ids:
            self._steps.extend((("POST", "/skills"), ("GET", "/skills/" + identity)))
        versions = [("GET", f"/skills/{identity}/versions/1") for identity in self.skill_ids]
        self._steps.extend(versions)
        self._steps.extend(
            (("POST", "/agents"), ("GET", "/agents/agent"), ("GET", "/agents/agent"))
        )
        self._steps.extend(versions)
        self._steps.append(("POST", "/agents/sessions"))
        if not replay:
            self._steps.extend(
                (
                    ("GET", "/agents/sessions/session"),
                    ("POST", "/agents/sessions/session/events"),
                    ("GET", "/agents/sessions/session/events"),
                )
            )
        self._index = 0

    def _mapped(self, spec: AgentSpec) -> AgentSpec:
        # Bash is provided by the hosted harness, never agent.tools JSON. F1's
        # explicit capability map and final hosted-session check prove that base.
        tools = (
            (ToolSpec(name="bash", kind="builtin"),)
            + tuple(spec.tools or ())
            + tuple(
                ToolSpec(name=server.name, kind="mcp_toolset") for server in spec.mcp_servers or ()
            )
        )
        return spec.model_copy(update={"tools": tools})

    @property
    def deployed_agent(self) -> AgentSpec:
        if self._agent is None:
            raise ValueError("no native deployed agent")
        return self._mapped(agent_spec(self._agent))

    def agent_spec(self, agent: Agent) -> AgentSpec:
        return self._mapped(agent.spec)

    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]:
        return tuple(self._uploads)

    def assert_consumed(self) -> None:
        if self._index != len(self._steps) or self._session is None:
            raise ValueError("unconsumed native provisioning script")
        environment = object_json(self._session["environment"])
        if environment.get("type") != "openai_hosted" or environment.get("id") != "hosted":
            raise ValueError("native Bash requires the observed hosted environment")
        pins = decode(self._session["metadata"])
        if pins is None or tuple(pin.id for pin in pins) != self.skill_ids:
            raise ValueError("native skill installation changed")
        expected = [
            {"type": "skill_reference", "skill_id": identity, "version": "1"}
            for identity in self.skill_ids
        ]
        if objects(environment["skills"]) != tuple(expected):
            raise ValueError("native installed pins changed")

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        if self._index == len(self._steps) or (request.method, path) != self._steps[self._index]:
            raise ValueError("unseen or reordered native request")
        if request.headers.get("OpenAI-Beta") != "agents=v1":
            raise ValueError("missing native beta header")
        if request.method == "POST" and not request.headers.get("Idempotency-Key"):
            raise ValueError("missing native submission key")
        self._index += 1
        self.requests.append(request)
        if path == "/skills":
            number = len(self._uploads)
            self._uploads.append(decode_upload(request))
            return httpx.Response(200, json=self._skill(number))
        if path.startswith("/skills/"):
            number = self.skill_ids.index(path.split("/")[2])
            if path.endswith("/versions/1"):
                return httpx.Response(
                    200, json={"skill_id": self.skill_ids[number], "version": "1"}
                )
            return httpx.Response(200, json=self._skill(number))
        if path == "/agents":
            self._agent = {
                "id": "agent",
                "created_at": 0,
                **object_json(json.loads(request.content)),
            }
            return httpx.Response(200, json=self._agent)
        if path == "/agents/agent":
            if self._agent is None:
                raise ValueError("agent was not deployed")
            return httpx.Response(200, json=self._agent)
        if path == "/agents/sessions":
            body = object_json(json.loads(request.content))
            self._session = {
                "id": "session",
                "created_at": 0,
                "agent": {"id": "agent", "model": MODEL.id},
                "environment": {"id": "hosted", **object_json(body["environment"])},
                "metadata": body["metadata"],
                "status": "idle",
                "required_actions": [],
            }
            return httpx.Response(200, json=self._session)
        if path == "/agents/sessions/session":
            if self._session is None:
                raise ValueError("session was not created")
            return httpx.Response(200, json=self._session)
        if path.endswith("/events") and request.method == "POST":
            return httpx.Response(202)
        if path.endswith("/events"):
            if (
                request.url.params.get("stream") != "true"
                or request.headers.get("Accept") != "text/event-stream"
            ):
                raise ValueError("wrong native stream negotiation")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(
                    "data: " + json.dumps(event) + "\n\n" for event in self.native_events
                ),
            )
        raise ValueError("native path not scripted")

    def _skill(self, number: int) -> Object:
        return {
            "id": self.skill_ids[number],
            "name": self._names[number],
            "description": "offline scripted upload",
            "created_at": 0,
            "latest_version": "1",
            "default_version": "1",
        }


class OpenAIDefaultCapabilityFactory:
    """Fresh SDK/resources for each initial script and each normalized replay."""

    def __init__(self, manifest: DefaultManifest, native_events: tuple[Object, ...] = ()) -> None:
        self.manifest, self.native_events = manifest, native_events
        self.scripts: list[OpenAIDefaultCapabilityTransport] = []
        self._clients: list[AsyncOpenAI] = []

    def __call__(self, replay: Replay | None = None) -> DefaultCapabilityAdapter:
        script = OpenAIDefaultCapabilityTransport(
            self.manifest, self.native_events, replay=replay is not None
        )
        sdk = AsyncOpenAI(
            api_key="offline-fixture",
            base_url="https://openai.invalid/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(script.handle)),
        )
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="offline",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda scope, kind, identity: scope == SCOPE,
            events=DefaultCapabilityReplayEvents(replay) if replay is not None else None,
        )
        self.scripts.append(script)
        self._clients.append(sdk)
        return DefaultCapabilityAdapter(
            driver=driver,
            scope=SCOPE,
            model=MODEL,
            environment=None,
            transport=script,
            builtin_mapping=BUILTIN_MAPPING,
            atomic_revision_pin=False,
        )

    async def __aenter__(self) -> OpenAIDefaultCapabilityFactory:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for client in self._clients:
            await client.close()
        self._clients.clear()
