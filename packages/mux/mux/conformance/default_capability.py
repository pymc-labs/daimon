"""F1: provision the authored default capabilities and observe one root turn.

No SDK or host imports. Adapters expose upstream facts, never a verdict. Loading
a pinned skill's SKILL.md is the skill invocation; scripted output is not a
claim about live model quality or about arguments omitted from a replay tape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol

from mux.conformance.recording import Recorder, RecordingError, Replay, RequestMetadata
from mux.conformance.runner import ConformanceFailure, PendingKind, PendingReason, Result, require
from mux.contracts.actions import InputEvent, UserMessage
from mux.contracts.events import (
    AgentMessagePayload,
    Event,
    StatusRunningPayload,
    TextPart,
    ToolResultPayload,
    ToolUsePayload,
    TurnEndedPayload,
    UserMessagePayload,
)
from mux.contracts.ids import ModelRef, Page, PageRequest, ResourceRef, Revision, Scope, SkillRef
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    MCPConnection,
    ProjectionSnapshot,
    SessionSpec,
    SkillUpload,
    ToolSpec,
)

DEFAULT_SKILLS = (
    "cli-auth",
    "file-handling",
    "channel-tidy",
    "marimo_notebooks",
    "workspace-setup",
    "pymc-artifact-style",
    "data-ingestion",
    "data-validation",
    "data-cleaning",
    "exploratory-data-analysis",
    "eda-storytelling",
)
BuiltinCapability = Literal["bash", "read", "edit", "grep", "glob", "write"]
DEFAULT_TOOLS: tuple[BuiltinCapability, ...] = ("bash", "read", "edit", "grep", "glob", "write")
MCP_TOOLS = frozenset({"client_context", "list_events"})
BASH_RESULT = "f1-bash-ok"
FINAL_MESSAGE = "f1-default-capability-ok"
PROMPT = (
    "Load the attached file-handling skill by reading its entire SKILL.md. "
    "Call daimon-mcp client_context and list_events (read-only). "
    "In this fresh disposable workspace, write f1.txt containing f1-initial, "
    "edit it to f1-edited, read it back (f1-edited), grep with line numbers "
    "(1:f1-edited), and glob its basename (f1.txt). Use the mapped tool for "
    "each capability; return each operation's successful tool result. "
    "Run bash printf f1-bash-ok. Finish with f1-default-capability-ok. "
    "Modify only f1.txt; do not modify services or other workspace files."
)


@dataclass(frozen=True)
class NamedSkill:
    name: str
    upload: SkillUpload


@dataclass(frozen=True)
class DefaultManifest:
    name: str
    system: str
    skills: tuple[NamedSkill, ...]
    builtin_tools: tuple[str, ...]
    mcp: MCPConnection
    skill_to_invoke: str = "file-handling"


class DefaultCapabilityTransport(Protocol):
    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]: ...
    @property
    def deployed_agent(self) -> AgentSpec: ...
    def agent_spec(self, agent: Agent) -> AgentSpec: ...
    def assert_consumed(self) -> None: ...


@dataclass(frozen=True)
class DefaultCapabilityAdapter:
    driver: ManagedAgents
    scope: Scope
    model: ModelRef
    environment: ResourceRef | None
    transport: DefaultCapabilityTransport
    pending: PendingReason | None = None
    builtin_mapping: Mapping[BuiltinCapability, ToolSpec] = field(
        default_factory=lambda: {
            name: ToolSpec(name=name, kind="builtin") for name in DEFAULT_TOOLS
        }
    )
    atomic_revision_pin: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "builtin_mapping", MappingProxyType(dict(self.builtin_mapping)))


def request_metadata(session: ResourceRef, *, stream: bool) -> RequestMetadata:
    """Logical neutral port operations, not provider-native HTTP certification."""
    return RequestMetadata(
        method="GET" if stream else "POST",
        path=f"/sessions/{session.id}/events" + ("/stream" if stream else ""),
        body_fields=() if stream else ("events",),
    )


def skill_text(manifest: DefaultManifest) -> str:
    require(manifest.name == "daimon", "F1: expected authored default agent")
    require(
        tuple(s.name for s in manifest.skills) == DEFAULT_SKILLS,
        "F1: default must bind all eleven authored skills",
    )
    require(manifest.builtin_tools == DEFAULT_TOOLS, "F1: default builtin toolset changed")
    require(manifest.mcp.name == "daimon-mcp", "F1: reserved MCP server missing")
    require(bool(manifest.system.strip()), "F1: default system instructions missing")
    require(manifest.skill_to_invoke == "file-handling", "F1: unexpected skill invocation")
    for skill in manifest.skills:
        paths = tuple(f.path for f in skill.upload.files)
        require(
            bool(paths) and len(set(paths)) == len(paths), "F1: incomplete or duplicate skill files"
        )
    files = next(s.upload.files for s in manifest.skills if s.name == manifest.skill_to_invoke)
    instructions = tuple(f for f in files if f.path == "SKILL.md" or f.path.endswith("/SKILL.md"))
    require(len(instructions) == 1, "F1: invoked skill needs one SKILL.md")
    text = instructions[0].content.decode("utf-8")
    require(bool(text.strip()), "F1: invoked skill instructions empty")
    return text


def check_agent(actual: AgentSpec, desired: AgentSpec) -> None:
    require(
        actual.name == desired.name and actual.system == desired.system,
        "F1: default agent identity/instructions changed",
    )
    require(
        actual.model.provider == desired.model.provider and actual.model.id == desired.model.id,
        "F1: probe model changed",
    )
    require(actual.tools == desired.tools, "F1: deployed toolset mapping changed")
    require(actual.mcp_servers == desired.mcp_servers, "F1: deployed MCP attachment changed")
    require(
        actual.skills is not None and desired.skills is not None, "F1: skill attachment missing"
    )
    if actual.skills is None or desired.skills is None:
        raise ConformanceFailure("F1: skill attachment missing")
    require(
        tuple((s.id, s.version) for s in actual.skills)
        == tuple((s.id, s.version) for s in desired.skills),
        "F1: default skill pins changed",
    )
    for observed, expected in zip(actual.skills, desired.skills, strict=True):
        require(
            observed.source is None
            or expected.source is None
            or observed.source == expected.source,
            "F1: default skill source changed",
        )
        require(
            observed.digest is None
            or expected.digest is None
            or observed.digest == expected.digest,
            "F1: default skill digest changed",
        )


def check_turn(
    events: tuple[Event, ...],
    session: ResourceRef,
    instructions: str,
    builtin_mapping: Mapping[BuiltinCapability, ToolSpec] | None = None,
) -> None:
    """Evidence comes from authoritative paired records, never text claims."""
    require(bool(events), "F1: turn produced no events")
    mapping = (
        builtin_mapping
        if builtin_mapping is not None
        else {name: ToolSpec(name=name, kind="builtin") for name in DEFAULT_TOOLS}
    )
    calls: dict[str, ToolUsePayload] = {}
    results: dict[str, ToolResultPayload] = {}
    running: list[StatusRunningPayload] = []
    ended: list[TurnEndedPayload] = []
    messages: list[AgentMessagePayload] = []
    ids: set[str] = set()
    previous_sequence = -1
    terminal = False
    inputs = 0
    for event in events:
        require(
            event.session_id == session.id
            and event.native.provider == session.provider
            and event.thread_id is None,
            "F1: event belongs to another session/thread",
        )
        require(
            event.id not in ids and event.sequence > previous_sequence,
            "F1: duplicate or reordered event",
        )
        ids.add(event.id)
        previous_sequence = event.sequence
        require(
            event.authority in ("record", "reconciled"), "F1: preview is not authoritative evidence"
        )
        require(not terminal, "F1: records after root completion")
        if running:
            require(event.turn_id == running[0].root_turn_id, "F1: event belongs to another root")
        payload = event.typed_payload()
        if event.type == "user.message":
            inputs += 1
            require(
                inputs == 1
                and not running
                and isinstance(payload, UserMessagePayload)
                and payload.content == (TextPart(text=PROMPT),),
                "F1: another or changed user turn",
            )
        elif event.type == "usage.observed":
            continue
        elif event.type == "session.status_running":
            require(isinstance(payload, StatusRunningPayload), "F1: untyped running record")
            if isinstance(payload, StatusRunningPayload):
                running.append(payload)
        elif event.type == "agent.tool_use":
            require(len(running) == 1, "F1: tool invocation outside one root turn")
            require(isinstance(payload, ToolUsePayload), "F1: untyped tool invocation")
            if isinstance(payload, ToolUsePayload):
                require(payload.call_id not in calls, "F1: duplicate tool invocation")
                require(payload.permission == "auto", "F1: unconfirmed tool invocation")
                allowed = (
                    payload.executor == "mcp"
                    and payload.mcp_server == "daimon-mcp"
                    and payload.tool_name in MCP_TOOLS
                ) or (
                    payload.mcp_server is None
                    and any(
                        payload.tool_name == tool.name
                        and payload.executor == ("host" if tool.kind == "custom" else "agent")
                        for tool in mapping.values()
                    )
                )
                require(allowed, "F1: unexpected/mutating tool or wrong executor/server")
                calls[payload.call_id] = payload
        elif event.type == "agent.tool_result":
            require(isinstance(payload, ToolResultPayload), "F1: untyped tool result")
            if isinstance(payload, ToolResultPayload):
                require(
                    payload.call_id in calls and payload.call_id not in results,
                    "F1: unpaired or duplicate tool result",
                )
                require(
                    not payload.is_error and bool(payload.content),
                    "F1: failed or empty tool result",
                )
                results[payload.call_id] = payload
        elif event.type == "agent.message":
            require(len(running) == 1, "F1: message outside root turn")
            require(
                len(calls) >= 9 and set(calls) == set(results),
                "F1: final message precedes capabilities",
            )
            require(isinstance(payload, AgentMessagePayload), "F1: untyped final message")
            if isinstance(payload, AgentMessagePayload):
                messages.append(payload)
        elif event.type == "session.turn_ended":
            require(isinstance(payload, TurnEndedPayload), "F1: untyped root completion")
            if isinstance(payload, TurnEndedPayload):
                ended.append(payload)
            terminal = True
        else:
            raise ConformanceFailure("F1: unexpected event/gap/degradation")
    require(
        len(running) == len(ended) == 1
        and ended[0].root_turn_id == running[0].root_turn_id
        and ended[0].outcome == "completed",
        "F1: one completed authoritative root required",
    )
    require(set(calls) == set(results), "F1: tool invocation lacks a result")
    used_mcp = {c.tool_name for c in calls.values() if c.executor == "mcp"}
    require(used_mcp == set(MCP_TOOLS), "F1: two distinct read-only MCP tools required")
    claimed: set[str] = set()
    read_call: str | None = None
    probes: tuple[tuple[BuiltinCapability, str], ...] = (
        ("read", instructions),
        ("bash", BASH_RESULT),
        ("read", "f1-edited"),
        ("grep", "1:f1-edited"),
        ("glob", "f1.txt"),
    )
    for capability, text in probes:
        tool = mapping[capability]
        matches = tuple(
            id_
            for id_, call in calls.items()
            if id_ not in claimed
            and call.tool_name == tool.name
            and call.executor == ("host" if tool.kind == "custom" else "agent")
            and results[id_].content == (TextPart(text=text),)
        )
        require(bool(matches), "F1: mapped skill/read/bash/grep/glob capability not exercised")
        claimed.add(matches[0])
        if text == "f1-edited":
            read_call = matches[0]
    ordered = tuple(calls)
    previous_write = -1
    for capability in ("write", "edit"):
        tool = mapping[capability]
        matches = tuple(
            id_
            for id_, call in calls.items()
            if id_ not in claimed
            and call.tool_name == tool.name
            and call.executor == ("host" if tool.kind == "custom" else "agent")
            and previous_write < ordered.index(id_) < ordered.index(read_call or "")
        )
        require(
            bool(matches),
            "F1: distinct successful mapped write/edit calls before readback required",
        )
        claimed.add(matches[0])
        previous_write = ordered.index(matches[0])
    require(
        len(messages) == 1
        and messages[0].complete
        and messages[0].content == (TextPart(text=FINAL_MESSAGE),),
        "F1: complete final capability message required",
    )


async def scenario(
    manifest: DefaultManifest, adapter: DefaultCapabilityAdapter, recorder: Recorder | None = None
) -> Result:
    if adapter.pending is not None:
        return Result("F1", "pending", (adapter.pending.detail,), adapter.pending)
    instructions = skill_text(manifest)
    missing = tuple(name for name in DEFAULT_TOOLS if name not in adapter.builtin_mapping)
    if missing:
        reason = PendingReason(
            PendingKind.CAPABILITY_UNAVAILABLE, "default builtin capability mapping is incomplete"
        )
        return Result("F1", "pending", (reason.detail,), reason)
    require(
        set(adapter.builtin_mapping) == set(DEFAULT_TOOLS),
        "F1: unexpected builtin capability mapping",
    )
    tools: list[ToolSpec] = []
    for name in DEFAULT_TOOLS:
        tool = adapter.builtin_mapping[name]
        require(
            tool.kind in ("builtin", "custom") and bool(tool.name),
            "F1: invalid builtin capability route",
        )
        same_name = tuple(existing for existing in tools if existing.name == tool.name)
        require(not same_name or same_name == (tool,), "F1: conflicting mapped tool definitions")
        if not same_name:
            tools.append(tool)
    ma, scope, transport = adapter.driver, adapter.scope, adapter.transport
    pins: list[SkillRef] = []
    for named in manifest.skills:
        skill = await ma.skills.create(scope, named.upload, key=f"f1-skill-{named.name}")
        pin = skill.latest_version
        require(
            pin is not None
            and pin.id == skill.id
            and bool(pin.version)
            and pin.version != "latest",
            "F1: uploaded skill needs an immutable pin",
        )
        if pin is None:
            raise ConformanceFailure("F1: uploaded skill needs an immutable pin")
        retrieved = await ma.skills.retrieve(scope, skill.id)
        require(
            retrieved.id == skill.id
            and retrieved.latest_version is not None
            and (retrieved.latest_version.id, retrieved.latest_version.version)
            == (pin.id, pin.version),
            "F1: retrieved skill pin changed",
        )
        pins.append(pin)
    require(len({pin.id for pin in pins}) == 11, "F1: eleven distinct uploaded skills required")
    require(
        transport.skill_uploads == tuple(s.upload for s in manifest.skills),
        "F1: upstream skill bytes changed",
    )
    desired = AgentSpec(
        name=manifest.name,
        system=manifest.system,
        model=adapter.model,
        tools=tuple(tools) + (ToolSpec(name="daimon-mcp", kind="mcp_toolset"),),
        mcp_servers=(manifest.mcp,),
        skills=tuple(pins),
    )
    agent = await ma.agents.create(scope, desired, key="f1-default-agent")
    check_agent(transport.deployed_agent, desired)
    check_agent(transport.agent_spec(agent), desired)
    saved = await ma.agents.retrieve(scope, agent.ref)
    require(
        saved.ref == agent.ref and saved.lifecycle == "active",
        "F1: saved agent identity/lifecycle changed",
    )
    check_agent(transport.agent_spec(saved), desired)
    session = await ma.sessions.create(
        scope,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision if adapter.atomic_revision_pin else Revision(local=0),
            environment=adapter.environment,
            config_revision=0,
        ),
        key="f1-session",
    )
    require(
        session.ref.provider == adapter.model.provider
        and session.ref.tenant_id == scope.tenant_id
        and session.ref.account_id == scope.account_id,
        "F1: unowned/foreign session",
    )
    receipt = await ma.events.send(
        scope, session.ref, (UserMessage(content=(TextPart(text=PROMPT),)),), key="f1-turn"
    )
    require(receipt.status in ("processed", "queued"), "F1: ambiguous/rejected turn admission")
    if recorder is not None:
        recorder.record(request_metadata(session.ref, stream=False), ())
    events = tuple([event async for event in ma.events.stream(scope, session.ref)])
    if recorder is not None:
        recorder.record(request_metadata(session.ref, stream=True), events)
    check_turn(events, session.ref, instructions, adapter.builtin_mapping)
    transport.assert_consumed()
    gaps = (
        ()
        if adapter.atomic_revision_pin
        else (
            "capability gap: atomic native agent revision pin unavailable; "
            "session provisioned unpinned; no CAS claim",
        )
    )
    return Result(
        "F1",
        "pass",
        (
            "eleven exact skill uploads and immutable pins",
            "default toolset and daimon-mcp deployment/readback",
            "one root: skill read, two read-only MCP tools, "
            "six mapped builtin capabilities and completion",
        )
        + gaps,
    )


class DefaultCapabilityReplayEvents:
    """Explicit F1-only event fake; unused operations refuse without fallback."""

    def __init__(self, replay: Replay) -> None:
        self.replay = replay

    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        if expected_turn is not None or tuple(events) != (
            UserMessage(content=(TextPart(text=PROMPT),)),
        ):
            raise RecordingError("F1 replay input changed")
        batch = await self.replay.events(request_metadata(session, stream=False))
        if batch:
            raise RecordingError("F1 send metadata batch must be empty")
        return SendReceipt(operation_id=key, status="queued", input_ids=())

    async def open_stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        if after is not None or previews:
            raise RecordingError("F1 replay stream options changed")
        events = await self.replay.events(request_metadata(session, stream=True))

        async def iterate() -> AsyncIterator[Event]:
            for event in events:
                yield event

        return iterate()

    async def stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        async for event in await self.open_stream(scope, session, after=after, previews=previews):
            yield event

    async def list(self, scope: Scope, session: ResourceRef, *, page: PageRequest) -> Page[Event]:
        raise RecordingError("F1 replay operation not recorded")

    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        raise RecordingError("F1 replay operation not recorded")

    async def cancel(
        self, scope: Scope, session: ResourceRef, *, turn_id: str, key: str
    ) -> CancelReceipt:
        raise RecordingError("F1 replay operation not recorded")

    async def wait_stopped(
        self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        raise RecordingError("F1 replay operation not recorded")
