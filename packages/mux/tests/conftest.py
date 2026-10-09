"""One valid instance of every contract type, for the round-trip tests.

Test modules cannot import each other here (every package's tests directory
is called `tests`), so the samples reach tests through fixtures and the
`sample` parametrization below.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mux.contracts.actions import (
    NativeInput,
    RequiredAction,
    UserMessage,
    UserToolConfirmation,
    UserToolResult,
)
from mux.contracts.admission import Admission, FallbackApplied
from mux.contracts.config import (
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    ResolvedBackend,
)
from mux.contracts.events import (
    AgentMessageDeltaPayload,
    AgentMessagePayload,
    ArtifactPart,
    Event,
    HistoryGapPayload,
    ImagePart,
    NativePart,
    NativeProvenance,
    ReconciledPayload,
    RequiresActionPayload,
    SessionErrorPayload,
    StatusRunningPayload,
    StatusTerminatedPayload,
    TextPart,
    ToolResultPayload,
    ToolServerDegradedPayload,
    ToolUsePayload,
    TurnEndedPayload,
    UsageObservedPayload,
    UserMessagePayload,
)
from mux.contracts.extensions import ExtensionConfig, ExtensionRef
from mux.contracts.ids import (
    ChannelRef,
    ModelRef,
    Page,
    PageRequest,
    ResourceRef,
    Revision,
    Scope,
    SkillRef,
    ThreadRef,
)
from mux.contracts.receipts import (
    CancelReceipt,
    DeletionReceipt,
    Operation,
    RestoreReceipt,
    SendReceipt,
    StopObservation,
    UpdateReceipt,
)
from mux.contracts.resources import (
    Agent,
    AgentFilter,
    AgentPatch,
    AgentSpec,
    AgentThread,
    Artifact,
    Continuity,
    CredentialBinding,
    CredentialInfo,
    Environment,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
    ExportRequirements,
    MCPConnection,
    Memory,
    ModelAdmission,
    ModelInfo,
    NetworkPolicy,
    ProjectionSnapshot,
    ProviderBinding,
    ResourceBinding,
    Session,
    SessionExport,
    SessionFilter,
    SessionSpec,
    SkillBundle,
    SkillFile,
    ToolSpec,
    UpdateOperation,
    UpdatePlan,
    Vault,
    WorkspaceSource,
)
from mux.contracts.usage import UsageObservation
from mux.profiles import MANAGED_AGENTS
from pydantic import BaseModel

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
REF = ResourceRef(id="sesn_1", kind="session", provider="anthropic", binding_id="ws_1")
ART = ResourceRef(id="file_1", kind="file", provider="anthropic", binding_id="ws_1")
REV = Revision(local=3, native="7")
MODEL = ModelRef(provider="anthropic", id="claude-opus-5-5", options={"effort": "high"})
CHANNEL = ChannelRef(tenant_id="t1", platform="discord", channel_id="c1")
THREAD = ThreadRef(channel=CHANNEL, thread_id="th1")
TEXT = TextPart(text="hi")
EXT = ExtensionConfig(namespace="anthropic.multiagent", version=1, value={"roster": ["a"]})
ACTION = RequiredAction(id="a1", kind="tool_confirmation", call_id="call_1", native_type="x")
PROV = NativeProvenance(provider="anthropic", api_revision="2026-07", event_id="evt_1")
RESOLVED = ResolvedBackend(
    backend="openai",
    profile="openai.persistent_workspace",
    model="gpt-6",
    requires={"steer": CapabilityRequirement(level="optional", fallback="queue_next_turn")},
    thread_mode="shared",
)
BINDING = ProviderBinding(
    thread=THREAD,
    provider="anthropic",
    profile="anthropic.managed_agents",
    native_refs={"session": "sesn_1", "environment": "env_1"},
    generation=2,
    config_revision=1,
    legacy_account_id="acct_1",
)
CONTINUITY = Continuity(
    conversation="native_session",
    workspace="native_reuse",
    processes="unknown",
    history_expires_at=NOW,
    retention_known=True,
)
AGENT_SPEC = AgentSpec(
    name="daimon",
    model=MODEL,
    system="be useful",
    tools=(ToolSpec(name="bash", kind="builtin", permission="ask"),),
    mcp_servers=(MCPConnection(name="daimon", url="https://x.test/mcp", credential_ref="cred:1"),),
    skills=(SkillRef(id="skill_1", digest="sha256:aa"),),
    extensions=(EXT,),
    metadata={"channel": "c1"},
)
ENV_SPEC = EnvironmentSpec(
    name="env",
    sources=(WorkspaceSource(kind="repository", target_path="/repo", repository_url="https://g"),),
    network=NetworkPolicy(mode="limited", allowed_hosts=("pypi.org",)),
    packages={"pip": ("numpy",)},
)
SESSION_SPEC = SessionSpec(
    agent=REF,
    agent_revision=REV,
    config_revision=1,
    resources=(ResourceBinding(id="r1", kind="artifact", target_path="/a", resource=ART),),
)


def _event(type_: str, payload: BaseModel, authority: str = "record") -> Event:
    return Event.model_validate(
        {
            "id": f"j_{type_}",
            "session_id": "sesn_1",
            "sequence": 4,
            "type": type_,
            "turn_id": "turn_1",
            "caused_by": ("j_0",),
            "observed_at": NOW,
            "occurred_at": NOW,
            "authority": authority,
            "payload": payload.model_dump(mode="json"),
            "native": PROV,
        }
    )


SAMPLES: tuple[BaseModel, ...] = (
    Scope(tenant_id="t1", account_id="a1", principal_id="p1", authorization_id="z1"),
    CHANNEL,
    THREAD,
    REF,
    REV,
    PageRequest(cursor="c", limit=5, order="desc"),
    Page[SkillRef](data=(SkillRef(id="s", digest="d"),), next_cursor="n"),
    MODEL,
    SkillRef(id="skill_1", digest="sha256:aa"),
    ExtensionRef(namespace="openai.steer", version=1),
    EXT,
    TEXT,
    ImagePart(media_type="image/png", data_base64="aGk="),
    ArtifactPart(artifact=ART, filename="a.csv", media_type="text/csv"),
    NativePart(namespace="anthropic.multiagent", version=1, payload={"k": 1}),
    PROV,
    UserMessagePayload(input_id="in_1", content=(TEXT,), mode="steer"),
    AgentMessagePayload(item_id="i1", content=(TEXT,), revision=2, complete=False, phase="x"),
    AgentMessageDeltaPayload(item_id="i1", content_index=0, text="h", preview_sequence=1),
    ToolUsePayload(call_id="c", tool_name="bash", input={"cmd": "ls"}, executor="agent"),
    ToolResultPayload(call_id="c", content=(TEXT,), is_error=True),
    StatusRunningPayload(root_turn_id="turn_1"),
    TurnEndedPayload(root_turn_id="turn_1", outcome="interrupted", cancel_receipt="op_1"),
    SessionErrorPayload(category="rate_limited", retry_status="retrying", native_code="429"),
    StatusTerminatedPayload(reason="deleted"),
    ToolServerDegradedPayload(server="daimon", error_type="timeout", retry_status="exhausted"),
    UsageObservedPayload(observation_id="evt_9", revision="1"),
    ReconciledPayload(snapshot_ref="snap_1", coverage="full", gaps=("x",)),
    HistoryGapPayload(domain="events", after="evt_1", recoverable=False),
    RequiresActionPayload(action_ids=("a1",)),
    _event("agent.message", AgentMessagePayload(item_id="i1", content=(TEXT,))),
    _event("native.anthropic.span", StatusRunningPayload(root_turn_id="x")),
    UserMessage(content=(TEXT,)),
    UserToolConfirmation(action_id="a1", decision="deny", deny_message="no"),
    UserToolResult(action_id="a1", content=(TEXT,)),
    NativeInput(extension=EXT),
    ACTION,
    Operation(
        id="op_1",
        key="k",
        request_digest="d",
        status="outcome_unknown",
        resource=REF,
        created_at=NOW,
        updated_at=NOW,
    ),
    SendReceipt(operation_id="op_1", status="queued", input_ids=("in_1",), turn_id="turn_1"),
    CancelReceipt(
        operation_id="op_2", session=REF, turn_id="turn_1", status="requested", requested_at=NOW
    ),
    StopObservation(
        receipt_operation_id="op_2", stopped=True, outcome="interrupted", observed_at=NOW
    ),
    UpdateReceipt(operation_id="op_3", status="processed", applies="next_turn", session=REF),
    DeletionReceipt(operation_id="op_4", deleted=(REF,), retained=(ART,)),
    RestoreReceipt(operation_id="op_5", session=REF, accepted_losses=("processes",)),
    UsageObservation(
        id="evt_9",
        revision="1",
        session=REF,
        turn_id="turn_1",
        model=MODEL,
        grain="model_request",
        basis="increment",
        input_tokens=10,
        output_tokens=None,
        native_meter={"cache_creation": {"ephemeral_5m_input_tokens": 3}},
        completeness="measured",
        observed_at=NOW,
    ),
    ToolSpec(name="t", kind="custom", input_schema={"type": "object"}),
    MCPConnection(name="m", url="https://m.test", transport="sse"),
    AGENT_SPEC,
    AgentPatch(system=None, tools=()),
    Agent(ref=REF, revision=REV, spec=AGENT_SPEC, lifecycle="archived"),
    AgentFilter(name="daimon", created_after=NOW),
    WorkspaceSource(kind="file", target_path="/f", artifact=ART),
    NetworkPolicy(),
    ENV_SPEC,
    EnvironmentPatch(network=NetworkPolicy(mode="none")),
    Environment(ref=REF, revision=REV, spec=ENV_SPEC),
    EnvironmentFilter(include_archived=True),
    ResourceBinding(id="r2", kind="native", native=EXT),
    SESSION_SPEC,
    CONTINUITY,
    BINDING,
    Session(
        ref=REF,
        binding=BINDING,
        continuity=CONTINUITY,
        requested_revision=REV,
        effective_revision=Revision(local=2),
        state="requires_action",
        active_root_turn="turn_1",
        required_actions=(ACTION,),
    ),
    SessionFilter(agent=REF),
    UpdateOperation(kind="tools", detail={"add": ["bash"]}),
    UpdatePlan(session=REF, expected_revision=REV, action="replace", losses=("processes",)),
    ExportRequirements(),
    SessionExport(
        source=REF,
        config_revision=1,
        journal_cursor="j_9",
        artifacts=(ART,),
        manifest_digest="sha256:bb",
        included=frozenset({"workspace"}),
        consistency="best_effort",
    ),
    ProjectionSnapshot(session=REF, cursor="j_9", state="idle", taken_at=NOW),
    Artifact(ref=ART, filename="a.csv", media_type="text/csv", size_bytes=3, created_at=NOW),
    SkillFile(path="SKILL.md", digest="sha256:cc", size_bytes=12),
    SkillBundle(name="s", files=(SkillFile(path="SKILL.md", digest="d", size_bytes=1),)),
    ModelInfo(model=MODEL, context_window=200_000),
    ModelAdmission(model=MODEL, admitted=False, reason="not offered"),
    Vault(ref=REF, name="v", revision=REV),
    CredentialBinding(name="gh", kind="static_bearer", credential_ref="cred:2"),
    CredentialInfo(id="cr_1", name="gh", kind="static_bearer", revision=REV),
    Memory(id="m1", path="/notes.md", content="x", revision=REV),
    AgentThread(id="th_1", parent_thread_id="th_0", status="running"),
    MANAGED_AGENTS,
    CapabilityRequirement(level="required"),
    BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3"),
    RESOLVED,
    ConfigRevision.create(CHANNEL, 4, RESOLVED),
    FallbackApplied(capability="steer", support="unknown", fallback="queue_next_turn"),
    Admission(
        provider="anthropic",
        profile_id="anthropic.managed_agents",
        model=None,
        thread_mode="per_caller",
        config_local=0,
        config_digest="d",
        satisfied=("cancel",),
        fallbacks=(FallbackApplied(capability="steer", support="unknown", fallback="f"),),
    ),
)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "sample" in metafunc.fixturenames:
        metafunc.parametrize("sample", SAMPLES, ids=[type(s).__name__ for s in SAMPLES])


@pytest.fixture
def samples() -> tuple[BaseModel, ...]:
    return SAMPLES


@pytest.fixture
def channel() -> ChannelRef:
    return CHANNEL


@pytest.fixture
def resolved() -> ResolvedBackend:
    return RESOLVED
