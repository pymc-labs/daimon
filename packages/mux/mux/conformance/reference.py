"""Small in-memory harness oracle. Not a provider, registry default or runtime driver.

Only probed operations are implemented; all other port access fails explicitly.
No reference result may be used as a provider conformance certificate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import cast

from mux.conformance.runner import Adapter, Scenario
from mux.contracts.actions import InputEvent, NativeInput, UserMessage
from mux.contracts.admission import Admission, admit
from mux.contracts.config import ConfigRevision
from mux.contracts.errors import (
    ContinuityLost,
    MigrationUnsupported,
    ProviderError,
    ScopeViolation,
    UnsupportedCapability,
)
from mux.contracts.events import Event, NativeProvenance, RequiredAction
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
from mux.contracts.ports import ManagedAgents
from mux.contracts.profile import Profile
from mux.contracts.receipts import (
    CancelReceipt,
    DeletionReceipt,
    SendReceipt,
    StopObservation,
    UpdateReceipt,
)
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    Artifact,
    Continuity,
    Environment,
    EnvironmentSpec,
    MCPConnection,
    ProjectionSnapshot,
    ProviderBinding,
    Session,
    SessionSpec,
    SkillBundle,
    SkillFile,
    UpdateOperation,
    UpdatePlan,
    WorkspaceSource,
)
from mux.profiles import MANAGED_AGENTS

IS_TEST_ORACLE = True

NOW = datetime(2026, 10, 9, tzinfo=UTC)


class UnsupportedPort:
    def __getattr__(self, name: str) -> object:
        raise UnsupportedCapability((name,), "test.reference")


class Transport:
    def __init__(self) -> None:
        self.fixture = ""
        self.faults: set[str] = set()
        self.mutations = 0
        self.reconciled = False
        self.history: list[Event] = []
        self.deletions: list[ResourceRef] = []
        self.scope = Scope(
            tenant_id="tenant", account_id="account", principal_id="human", authorization_id="auth"
        )
        self.ref = ResourceRef(
            id="session", kind="session", provider="anthropic", account_scope_id="workspace"
        )
        self.session = Session(
            ref=self.ref,
            binding=ProviderBinding(
                id="binding",
                thread=ThreadRef(
                    channel=ChannelRef(tenant_id="tenant", platform="test", channel_id="channel"),
                    thread_id="thread",
                ),
                provider="anthropic",
                profile=MANAGED_AGENTS.profile_id,
                native_refs={"session": "session"},
                generation=1,
                config_revision=1,
            ),
            continuity=Continuity(
                conversation="native_session", workspace="native_reuse", processes="unknown"
            ),
            requested_revision=Revision(local=1),
            effective_revision=Revision(local=1),
            state="idle",
        )

    async def arrange(self, fixture_id: str) -> Scenario:
        self.fixture = fixture_id
        if fixture_id in ("C05", "C06"):
            self.session = self.session.model_copy(
                update={"state": "running", "active_root_turn": "root"}
            )
        if fixture_id == "C11":
            self.session = self.session.model_copy(
                update={
                    "state": "requires_action",
                    "active_root_turn": "root",
                    "required_actions": (
                        RequiredAction(id="missing-tool", kind="function_result", call_id="call"),
                    ),
                }
            )
        desired = SessionSpec(
            agent=self.ref.model_copy(update={"kind": "agent"}),
            agent_revision=Revision(local=2),
            config_revision=2,
            environment=self.ref.model_copy(update={"id": "environment", "kind": "environment"}),
        )
        return Scenario(
            self.scope,
            self.scope.model_copy(update={"tenant_id": "foreign"}),
            self.session,
            desired,
            shared_resources=(self.ref.model_copy(update={"id": "shared-vault", "kind": "vault"}),),
        )

    def fault(self, name: str) -> None:
        self.faults.add(name)

    @property
    def mutation_count(self) -> int:
        return self.mutations

    @property
    def deleted_resources(self) -> tuple[ResourceRef, ...]:
        return tuple(self.deletions)

    def check(self, scope: Scope, ref: ResourceRef) -> None:
        if (
            scope.tenant_id != self.scope.tenant_id
            or ref.account_scope_id != self.ref.account_scope_id
        ):
            raise ScopeViolation(ref.id, "foreign resource")

    def event(
        self, index: int, type_: str, payload: dict[str, object], *, thread: str | None = None
    ) -> Event:
        return Event.model_validate(
            dict(
                id=f"journal-{index}",
                session_id=self.ref.id,
                sequence=index,
                type=type_,
                turn_id="root",
                thread_id=thread,
                observed_at=NOW,
                authority="record",
                payload=payload,
                native=NativeProvenance(provider="anthropic", api_revision="test"),
            )
        )


class ReferenceSessions(UnsupportedPort):
    def __init__(self, t: Transport) -> None:
        self.t = t

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Session:
        self.t.check(scope, ref)
        if self.t.faults & {"expiry", "unexpected_loss"}:
            reason = (
                "unexpected workspace loss"
                if "unexpected_loss" in self.t.faults
                else "workspace expiry"
            )
            raise ContinuityLost(self.t.session.binding.id, (reason,))
        return self.t.session

    async def plan_update(self, scope: Scope, ref: ResourceRef, desired: SessionSpec) -> UpdatePlan:
        self.t.check(scope, ref)
        return UpdatePlan(
            session=ref,
            expected_revision=self.t.session.effective_revision,
            action="next_turn",
            operations=(UpdateOperation(kind="resources"),),
        )

    async def apply_update(
        self, scope: Scope, plan: UpdatePlan, *, expected: Revision, key: str
    ) -> UpdateReceipt:
        self.t.check(scope, plan.session)
        self.t.mutations += 1
        if "mount_add_failed_after_delete" in self.t.faults:
            self.t.session = self.t.session.model_copy(
                update={"state": "provisioning", "requested_revision": Revision(local=2)}
            )
            raise ProviderError("upstream", retryable=False)
        return UpdateReceipt(
            operation_id=key, status="processed", applies="next_turn", session=plan.session
        )

    async def migrate(
        self, scope: Scope, ref: ResourceRef, target: ConfigRevision, *, expected: int, key: str
    ) -> Session:
        self.t.check(scope, ref)
        raise MigrationUnsupported(self.t.session.binding.thread.thread_id)

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self.t.check(scope, ref)
        self.t.mutations += 1
        self.t.deletions.append(ref)
        return DeletionReceipt(
            operation_id=key,
            deleted=(ref,),
            retained=(ref.model_copy(update={"id": "shared-vault", "kind": "vault"}),),
        )


class ReferenceEvents(UnsupportedPort):
    def __init__(self, t: Transport) -> None:
        self.t = t

    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        self.t.check(scope, session)
        for event in events:
            if isinstance(event, NativeInput):
                MANAGED_AGENTS.offered_extension(event.extension.namespace, event.extension.version)
                raise UnsupportedCapability((event.extension.namespace,), "test.reference")
        self.t.mutations += 1
        for event in events:
            if isinstance(event, UserMessage):
                self.t.history.append(
                    self.t.event(
                        len(self.t.history),
                        "user.message",
                        {
                            "input_id": key,
                            "content": [part.model_dump(mode="json") for part in event.content],
                        },
                    )
                )
        return SendReceipt(operation_id=key, status="processed", input_ids=(key,), turn_id=key)

    async def stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        self.t.check(scope, session)
        if self.t.fixture == "C02":
            yield self.t.event(
                20, "session.turn_ended", {"root_turn_id": "root", "outcome": "completed"}
            )
        elif self.t.fixture == "C11":
            yield self.t.event(
                20,
                "session.requires_action",
                {
                    "actions": [
                        action.model_dump(mode="json") for action in self.t.session.required_actions
                    ]
                },
            )
        else:
            yield self.t.event(0, "agent.thread.ended", {"outcome": "completed"}, thread="child")
            if self.t.fixture == "C05":
                yield self.t.event(
                    1,
                    "agent.message",
                    {"item_id": "item", "content": [{"type": "text", "text": "done"}]},
                )
                yield self.t.event(
                    2,
                    "agent.tool_result",
                    {"call_id": "call", "content": [{"type": "text", "text": "result"}]},
                )
        # Disconnect is EOF; completion must come from reconciliation, never EOF.

    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        self.t.check(scope, session)
        self.t.reconciled = True
        if self.t.fixture == "C05":
            self.t.session = self.t.session.model_copy(
                update={"state": "idle", "active_root_turn": None}
            )
        if "mount_reconciled" in self.t.faults:
            self.t.session = self.t.session.model_copy(
                update={"state": "idle", "effective_revision": Revision(local=2)}
            )
        return ProjectionSnapshot(
            session=session,
            cursor="journal-4",
            state=self.t.session.state if self.t.session.state != "provisioning" else "running",
            active_root_turn=self.t.session.active_root_turn,
            gaps=("missing-preview-domain",) if self.t.fixture == "C05" else (),
            taken_at=NOW,
        )

    async def list(self, scope: Scope, session: ResourceRef, *, page: PageRequest) -> Page[Event]:
        self.t.check(scope, session)
        if self.t.fixture == "C02":
            return Page[Event](data=tuple(self.t.history))
        if not self.t.reconciled:
            return Page[Event](data=())
        saved = (
            self.t.event(0, "agent.thread.ended", {"outcome": "completed"}, thread="child"),
            self.t.event(
                1,
                "agent.message",
                {"item_id": "item", "content": [{"type": "text", "text": "done"}]},
            ),
            self.t.event(
                2,
                "agent.tool_result",
                {"call_id": "call", "content": [{"type": "text", "text": "result"}]},
            ),
            self.t.event(3, "session.history_gap", {"domain": "previews", "recoverable": False}),
            self.t.event(4, "session.turn_ended", {"root_turn_id": "root", "outcome": "completed"}),
        )
        start = int(page.cursor or 0)
        end = start + page.limit
        return Page[Event](
            data=saved[start:end], next_cursor=str(end) if end < len(saved) else None
        )

    async def cancel(
        self, scope: Scope, session: ResourceRef, *, turn_id: str, key: str
    ) -> CancelReceipt:
        self.t.check(scope, session)
        self.t.mutations += 1
        return CancelReceipt(
            operation_id=key, session=session, turn_id=turn_id, status="requested", requested_at=NOW
        )

    async def wait_stopped(
        self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        self.t.check(scope, receipt.session)
        stopped = "observed_stop" in self.t.faults and datetime.now(UTC) < deadline
        if stopped:
            self.t.session = self.t.session.model_copy(
                update={"state": "idle", "active_root_turn": None}
            )
        return StopObservation(
            receipt_operation_id=receipt.operation_id,
            stopped=stopped,
            outcome="interrupted" if stopped else None,
            observed_at=NOW,
        )


class ReferenceArtifacts(UnsupportedPort):
    def __init__(self, t: Transport) -> None:
        self.t = t

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest, turn_id: str | None = None
    ) -> Page[Artifact]:
        self.t.check(scope, session)
        artifacts = tuple(
            Artifact(
                ref=session.model_copy(update={"id": f"artifact-{i}", "kind": "file"}),
                filename=f"binary-{i}",
                media_type="application/octet-stream",
                session=session,
                size_bytes=256,
                created_at=NOW,
            )
            for i in range(2)
        )
        start = int(page.cursor or 0)
        end = start + page.limit
        return Page[Artifact](data=artifacts[start:end], next_cursor=str(end) if end < 2 else None)

    async def download(self, scope: Scope, ref: ResourceRef) -> AsyncIterator[bytes]:
        self.t.check(scope, ref)
        yield bytes(range(128))
        if "download_interrupted" in self.t.faults:
            raise ProviderError("transient_network", retryable=True)
        yield bytes(range(128, 256))


class ReferenceAgents(UnsupportedPort):
    def __init__(self, t: Transport) -> None:
        self.t = t

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
        self.t.check(scope, ref)
        return Agent(
            ref=ref,
            revision=Revision(local=1),
            spec=AgentSpec(
                name="fixture",
                model=ModelRef(provider="anthropic", id="fixture"),
                skills=(SkillRef(id="skill", digest="sha256:fixture-bundle"),),
                mcp_servers=(MCPConnection(name="fixture", url="https://fixture.invalid/mcp"),),
            ),
        )


class ReferenceEnvironments(UnsupportedPort):
    def __init__(self, t: Transport) -> None:
        self.t = t

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Environment:
        self.t.check(scope, ref)
        return Environment(
            ref=ref,
            revision=Revision(local=1),
            spec=EnvironmentSpec(
                name="fixture",
                sources=(
                    WorkspaceSource(
                        kind="repository",
                        target_path="/repo",
                        repository_url="https://fixture.invalid/repo",
                    ),
                ),
            ),
        )


class ReferenceSkills(UnsupportedPort):
    def __init__(self, t: Transport) -> None:
        self.t = t

    async def retrieve(self, scope: Scope, ref: SkillRef) -> SkillBundle:
        return SkillBundle(
            name="fixture",
            files=(SkillFile(path="SKILL.md", digest="sha256:file", size_bytes=7),),
            artifact=self.t.ref.model_copy(update={"kind": "file", "id": "skill-archive"}),
        )


class ReferenceDriver:
    def __init__(self, t: Transport) -> None:
        self._transport = t
        self.sessions = ReferenceSessions(t)
        self.events = ReferenceEvents(t)
        self.artifacts = ReferenceArtifacts(t)
        self.agents = ReferenceAgents(t)
        self.environments = ReferenceEnvironments(t)
        self.skills = ReferenceSkills(t)
        self.models = UnsupportedPort()
        self.usage = UnsupportedPort()

    def capabilities(self) -> Profile:
        if "admission_unknown" in self._transport.faults:
            support = "unknown"
        elif "admission_unsupported" in self._transport.faults:
            support = "unsupported"
        else:
            return MANAGED_AGENTS
        return MANAGED_AGENTS.model_copy(
            update={
                "core": False,
                "support": {"usage_observations": "native", "memory_stores": support},
            }
        )

    def admit(self, config: ConfigRevision) -> Admission:
        return admit(config, self.capabilities())

    def extension[T](self, port: type[T], *, namespace: str, version: int) -> T:
        self.capabilities().offered_extension(namespace, version)
        raise UnsupportedCapability((namespace,), "test.reference")


def create() -> Adapter:
    t = Transport()
    # The oracle implements only exercised methods; all remaining access fails closed.
    return Adapter(cast(ManagedAgents, ReferenceDriver(t)), None, t)
