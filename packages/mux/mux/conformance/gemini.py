"""Explicit offline Gemini adapter; deferred scenarios never certify."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from mux.conformance.runner import (
    Adapter,
    PendingKind,
    PendingReason,
    Registry,
    Scenario,
    SendEvidence,
)
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec, SkillUpload
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.drivers.gemini.transport import Object
from mux.errors import ProviderError
from mux.state.memory import CrashPoint, MemoryStateStore
from mux.state.store import StateStore

PENDING_REASONS = {
    "C01": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Multi-human admission/batching host scenario and conformance hook are absent.",
    ),
    "C02": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Snapshot bytes implemented; clock-driven expiry and unexpected-loss proof absent.",
    ),
    "C04": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Ambiguous accepted POST reconciliation is absent; provider offers no idempotency lookup.",
    ),
    "C08": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Driver next-turn tool/mount update port is not implemented.",
    ),
    "C09": PendingReason(
        PendingKind.CAPABILITY_UNAVAILABLE,
        "Binary snapshots are proved; provider vault API and session-delete receipt "
        "are unavailable.",
    ),
    "C11": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Conformance bridge for interaction-time inline skill deployment is absent.",
    ),
    "C12": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Gemini host ledger/outbox recovery scenario has not been connected "
        "to the accounting hook.",
    ),
    "C14": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Existing/new-thread backend selection host scenario and conformance hook are absent.",
    ),
    "C17": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Host wake generation/lease fencing scenario and conformance hook are absent.",
    ),
    "C18": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Gemini host termination outcome persistence scenario and conformance hook are absent.",
    ),
}


def native(id_: str, *, status: str = "completed", output: int | None = 0) -> Object:
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": id_,
        "status": status,
        "environment_id": "workspace",
        "created": stamp,
        "updated": stamp,
        "steps": [],
        "usage": {"total_output_tokens": output, "total_thought_tokens": 0},
    }


class GeminiScript(FakeTransport):
    """Seeds native responses through real driver calls, never edits projections."""

    def __init__(self) -> None:
        super().__init__()
        self.storage = MemoryStorage()
        self.store = MemoryStateStore()
        self.timeout_after_accept = False
        self.stamp = datetime.now(UTC)
        self.scope = Scope(
            tenant_id="tenant", account_id="account", principal_id="human", authorization_id="auth"
        )
        self.ma = GeminiManagedAgents(
            self, storage=self.storage, state_store=self.store, account_scope_id="project"
        )

    def native(self, id_: str, *, status: str = "completed", output: int | None = 0) -> Object:
        # Provider revisions advance deterministically, independently of the
        # speed of repeated reads/cancellation on the shared test host.
        self.stamp += timedelta(seconds=1)
        response = native(id_, status=status, output=output)
        response["created"] = response["updated"] = self.stamp.isoformat()
        return response

    async def arrange(self, fixture_id: str) -> Scenario:
        if fixture_id in PENDING_REASONS:
            raise ValueError("declared deferred fixture must be dispatched through run_fixture")
        agent = await self.ma.agents.create(
            self.scope,
            AgentSpec(name="fixture", model=ModelRef(provider="gemini", id="fixture")),
            key="fixture-agent",
        )
        env = await self.ma.environments.create(
            self.scope, EnvironmentSpec(name="fixture"), key="fixture-environment"
        )
        spec = SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=env.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={
                        "binding_id": "fixture-binding",
                        "thread": ThreadRef(
                            channel=ChannelRef(
                                tenant_id="tenant", platform="test", channel_id="channel"
                            ),
                            thread_id="thread",
                        ).model_dump(mode="json"),
                    },
                )
            },
        )
        session = await self.ma.sessions.create(self.scope, spec, key="fixture-session")
        if fixture_id in ("C03", "C07"):
            await self.store.put_binding(session.binding, expected_generation=0)
        if fixture_id == "C03":
            self.responses.append(self.native("acknowledged"))
        elif fixture_id in ("C05", "C06", "C07"):
            running = self.native("root", status="in_progress", output=None)
            self.responses.append(running)
            await self.ma.events.send(
                self.scope,
                session.ref,
                (UserMessage(content=(TextPart(text="seed"),)),),
                key="seed",
            )
            if fixture_id == "C07":
                initial = self.stamp
                for index, count in enumerate((100, 120, 110), start=1):
                    response = self.native("root", status="in_progress", output=count)
                    response["updated"] = (initial + timedelta(seconds=index)).isoformat()
                    self.saved["root"] = response
                    await self.ma.usage.reconcile(self.scope, session.ref)
            session = await self.ma.sessions.retrieve(self.scope, session.ref)
            if fixture_id == "C05":
                completed = self.native("root")
                completed["steps"] = [
                    {"type": "model_output", "content": [{"type": "text", "text": "done"}]},
                    {"type": "function_result", "call_id": "call", "result": "result"},
                ]
                # A child-completion signal and EOF cannot terminate the root.
                # Both the stream's canonical read and post-EOF retrieval see
                # it running; only the final saved GET observes completion.
                self.reads["root"] = [running, running, completed]
                gap: Object = {
                    "event_type": "error",
                    "event_id": "lost-domain",
                    "error": {"code": "stream_interrupted", "message": "scripted loss"},
                }
                self.streams["root"] = [
                    {
                        "event_type": "step.delta",
                        "event_id": "preview",
                        "index": 0,
                        "delta": {"type": "text", "text": "do"},
                    },
                    {
                        "event_type": "interaction.completed",
                        "event_id": "child-end",
                        "interaction": {"id": "child", "status": "completed"},
                    },
                    gap,
                    gap,
                ]
        elif fixture_id not in ("C10", "C13", "C15", "C16"):
            raise ValueError("unsupported executable fixture")
        return Scenario(
            self.scope,
            self.scope.model_copy(update={"tenant_id": "foreign"}),
            session,
            spec,
            saved_message_item_id="step:0",
        )

    def fault(self, name: str) -> None:
        if name == "timeout_after_accept":
            self.responses.append(self.native("unobserved-acceptance"))
            self.timeout_after_accept = True
        elif name == "observed_stop":
            self.saved["root"] = self.native("root", status="cancelled", output=None)
        elif name in ("admission_unsupported", "admission_unknown"):
            # memory_stores is statically unsupported in the real profile;
            # either missing-support probe must be refused without native I/O.
            pass
        else:
            raise ValueError("unsupported scripted fault")

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        # Native acceptance survives independently of the lost delivery response.
        response = await super().create(request)
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise ProviderError("transient_network", retryable=True)
        return response

    @property
    def mutation_count(self) -> int:
        return len(self.requests) + len(self.cancelled)

    @property
    def deleted_resources(self) -> tuple[ResourceRef, ...]:
        return ()

    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]:
        return ()

    @property
    def upstream_sends(self) -> tuple[SendEvidence, ...]:
        return ()

    @property
    def reconciled_sends(self) -> tuple[SendEvidence, ...]:
        return ()

    def restart_store(
        self, store: StateStore, *, crash: Mapping[str, CrashPoint] | None = None
    ) -> StateStore:
        if not isinstance(store, MemoryStateStore):
            raise TypeError("offline Gemini adapter requires MemoryStateStore")
        self.store = store.restart(crash=crash)
        self.ma.__init__(
            self, storage=self.storage, state_store=self.store, account_scope_id="project"
        )
        return self.store


def adapter() -> Adapter:
    script = GeminiScript()
    return Adapter(script.ma, script.store, script, pending=PENDING_REASONS)


def register(registry: Registry) -> None:
    registry.register("gemini.inline_reuse.offline", adapter)
