"""Explicit offline F1 adapter; missing default capabilities never certify."""

from mux.conformance.default_capability import (
    DEFAULT_TOOLS,
    BuiltinCapability,
    DefaultCapabilityAdapter,
)
from mux.conformance.recording import Replay
from mux.conformance.runner import ConformanceFailure, PendingKind, PendingReason, require
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import Agent, AgentSpec, SkillUpload, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.state.memory import MemoryStateStore

PENDING = PendingReason(
    PendingKind.ADAPTER_DEPENDENCY,
    "Full default skill bundles need binary deployment beyond the text-only 2 MiB limit; "
    "authenticated daimon-mcp needs a credential resolver; inline sessions need a "
    "binding/thread scenario seam. Native remote MCP is supported, but these driver "
    "and scenario adapters are absent; see mux/drivers/gemini/DEFAULT-CAPABILITY.md.",
)


# source-G-runtime.txt:231,242-243: shell commands cover these file operations.
# Native filesystem tool identities are not modelled by this driver.
_CODE = ToolSpec(name="code_execution", kind="builtin")
BUILTIN_MAPPING: dict[BuiltinCapability, ToolSpec] = {name: _CODE for name in DEFAULT_TOOLS}


class PendingTransport(FakeTransport):
    """Empty upstream evidence; no fake deployment or script success for F1."""

    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]:
        return ()

    @property
    def deployed_agent(self) -> AgentSpec:
        raise ConformanceFailure("Gemini F1 has no upstream default deployment evidence")

    def agent_spec(self, agent: Agent) -> AgentSpec:
        return agent.spec

    def assert_consumed(self) -> None:
        require(
            not self.requests
            and not self.read_requests
            and not self.cancelled
            and not self.snapshot_reads
            and not self.responses
            and not self.reads
            and not self.saved
            and not self.streams
            and not self.snapshots,
            "pending Gemini F1 has provider I/O or unconsumed script evidence",
        )


def adapter(replay: Replay | None = None) -> DefaultCapabilityAdapter:
    """Fresh offline resources per call. Pending does not consume or pass a tape.

    The shared replay entry checks provider/model before dispatch. Until the
    full-default gaps close, it returns typed PENDING before provisioning and
    does not inject normalized events as fabricated native Gemini evidence.
    """
    transport = PendingTransport()
    driver = GeminiManagedAgents(
        transport,
        storage=MemoryStorage(),
        state_store=MemoryStateStore(),
        account_scope_id="f1-offline",
    )
    return DefaultCapabilityAdapter(
        driver=driver,
        scope=Scope(
            tenant_id="f1-tenant",
            account_id="f1-account",
            principal_id="f1-replay",
            authorization_id="f1-offline-only",
        ),
        model=ModelRef(provider="gemini", id="gemini-3.5-flash-lite"),
        environment=None,
        transport=transport,
        pending=PENDING,
        builtin_mapping=BUILTIN_MAPPING,
        atomic_revision_pin=False,
    )
