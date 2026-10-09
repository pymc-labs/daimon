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
    "C02": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Snapshot bytes implemented; clock-driven expiry and unexpected-loss proof absent.",
    ),
    "C04": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Ambiguous accepted POST reconciliation is absent; provider offers no idempotency lookup.",
    ),
    "C05": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "No durable saved-item/SSE gap bridge; preview and EOF tests do not prove this fixture.",
    ),
    "C08": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Driver next-turn tool/mount update port is not implemented.",
    ),
    "C09": PendingReason(
        PendingKind.CAPABILITY_UNAVAILABLE,
        "Snapshot downloads are implemented; no provider vault API.",
    ),
    "C11": PendingReason(
        PendingKind.ADAPTER_DEPENDENCY,
        "Conformance bridge for interaction-time inline skill deployment is absent.",
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
        self.scope = Scope(
            tenant_id="tenant", account_id="account", principal_id="human", authorization_id="auth"
        )
        self.ma = GeminiManagedAgents(
            self, storage=self.storage, state_store=self.store, account_scope_id="project"
        )

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
            self.responses.append(native("acknowledged"))
        elif fixture_id in ("C06", "C07"):
            self.responses.append(native("root", status="in_progress", output=None))
            await self.ma.events.send(
                self.scope,
                session.ref,
                (UserMessage(content=(TextPart(text="seed"),)),),
                key="seed",
            )
            if fixture_id == "C07":
                initial = datetime.now(UTC)
                for index, count in enumerate((100, 120, 110), start=1):
                    response = native("root", status="in_progress", output=count)
                    response["updated"] = (initial + timedelta(seconds=index)).isoformat()
                    self.saved["root"] = response
                    await self.ma.usage.reconcile(self.scope, session.ref)
            session = await self.ma.sessions.retrieve(self.scope, session.ref)
        elif fixture_id not in ("C10", "C13", "C15", "C16"):
            raise ValueError("unsupported executable fixture")
        return Scenario(
            self.scope, self.scope.model_copy(update={"tenant_id": "foreign"}), session, spec
        )

    def fault(self, name: str) -> None:
        if name == "timeout_after_accept":
            self.responses.append(native("unobserved-acceptance"))
            self.timeout_after_accept = True
        elif name == "observed_stop":
            self.saved["root"] = native("root", status="cancelled", output=None)
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
