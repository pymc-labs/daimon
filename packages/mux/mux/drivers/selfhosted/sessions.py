"""Sessions are host-bound identities; the harness creates history on first send."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from mux.contracts.actions import UserMessage
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ChannelRef, Page, PageRequest, ResourceRef, Revision, Scope, ThreadRef
from mux.contracts.receipts import DeletionReceipt, Operation, RestoreReceipt, UpdateReceipt
from mux.contracts.resources import (
    Continuity,
    ExportRequirements,
    ProviderBinding,
    Session,
    SessionExport,
    SessionFilter,
    SessionSpec,
    UpdatePlan,
)
from mux.drivers.selfhosted.context import Context
from mux.drivers.selfhosted.transport import sandbox_id
from mux.errors import ContinuityLost, MigrationUnsupported, OperationConflict, ScopeViolation
from mux.state.operations import request_digest


class SelfHostedSessions:
    def __init__(self, context: Context) -> None:
        self._c = context
        self._creates: dict[tuple[str, str, str, str], tuple[str, Session]] = {}
        # Set by the driver after both ports have been constructed.
        from mux.contracts.ports import Events

        self.events: Events | None = None

    async def spec(self, scope: Scope, ref: ResourceRef) -> SessionSpec:
        binding = self._c.binding(scope, ref)
        if ref in self._c.specs:
            return self._c.specs[ref]
        refs = binding.native_refs
        if "agent" not in refs or "environment" not in refs:
            raise ContinuityLost(binding.id, ("self-hosted session has no agent/environment pins",))
        return SessionSpec(
            agent=self._c.ref(scope, "agent", refs["agent"]),
            agent_revision=self._c.revision(binding, "agent"),
            environment=self._c.ref(scope, "environment", refs["environment"]),
            config_revision=binding.config_revision,
        )

    async def create(
        self, scope: Scope, spec: SessionSpec, *, key: str, initial: UserMessage | None = None
    ) -> Session:
        if not self._c.authorization(scope, "session", None):
            raise ScopeViolation("session", "session creation is not authorized")
        self._c.check(scope, spec.agent, "agent")
        if spec.environment is None:
            raise self._c.unsupported("selfhosted_environment_required")
        self._c.check(scope, spec.environment, "environment")
        if (
            initial is not None
            or spec.resources
            or spec.extensions
            or spec.state_mode != "continue"
        ):
            raise self._c.unsupported("session_create_options")
        digest = request_digest(spec.model_dump(mode="json"))
        identity = (scope.tenant_id, scope.account_id, scope.principal_id, key)
        previous = self._creates.get(identity)
        if previous:
            if previous[0] != digest:
                raise OperationConflict(key)
            return previous[1]
        # The identity excludes both renewable grants and the mutable request
        # payload, so a reconstructed client can detect a changed intent.
        id_ = "sh_session_" + str(uuid5(NAMESPACE_URL, request_digest(list(identity))))
        ref = self._c.ref(scope, "session", id_)
        prior_binding = self._c.bindings.get(ref)
        if prior_binding is None and self._c.binding_lookup is not None:
            prior_binding = self._c.binding_lookup(scope, id_)
        if prior_binding is not None:
            if prior_binding.native_refs.get("spec_digest") != digest:
                raise OperationConflict(key)
            self._c.bindings[ref] = prior_binding
            prior_binding = self._c.binding(scope, ref)
            result = self._session(ref, prior_binding, "idle")
            self._creates[identity] = digest, result
            return result
        agent = await self._c.agents.retrieve(scope, spec.agent)
        environment = await self._c.environments.retrieve(scope, spec.environment)
        if agent.ref != spec.agent or environment.ref != spec.environment:
            raise ScopeViolation(spec.agent.id, "resource ports returned foreign identities")
        if agent.lifecycle != "active" or environment.lifecycle != "active":
            raise ContinuityLost(spec.agent.id, ("agent or environment is archived",))
        self._c.check_environment(environment)
        if (
            spec.agent_revision.local != 0 or spec.agent_revision.native is not None
        ) and agent.revision != spec.agent_revision:
            raise ContinuityLost(spec.agent.id, ("agent revision differs from session pin",))
        binding = ProviderBinding(
            id=id_,
            provider="selfhosted",
            profile="selfhosted.loop",
            thread=ThreadRef(
                channel=ChannelRef(
                    tenant_id=scope.tenant_id, platform="selfhosted", channel_id=id_
                ),
                thread_id=id_,
            ),
            generation=0,
            config_revision=spec.config_revision,
            legacy_account_id=scope.account_id,
            native_refs={
                "session": id_,
                "sandbox": sandbox_id(id_),
                "agent": spec.agent.id,
                "environment": spec.environment.id,
                "agent_revision": str(agent.revision.local),
                "environment_revision": str(environment.revision.local),
                "agent_revision_pin": agent.revision.model_dump_json(),
                "environment_revision_pin": environment.revision.model_dump_json(),
                "spec_digest": digest,
            },
        )
        self._c.bindings[ref], self._c.specs[ref] = (
            binding,
            spec.model_copy(update={"agent_revision": agent.revision}),
        )
        result = self._session(ref, binding, "idle")
        self._creates[identity] = digest, result
        return result

    @staticmethod
    def _session(ref: ResourceRef, binding: ProviderBinding, state: str) -> Session:
        return Session.model_validate(
            {
                "ref": ref,
                "binding": binding,
                "continuity": Continuity(
                    conversation="native_session", workspace="native_reuse", processes="unknown"
                ),
                "requested_revision": Revision(local=binding.config_revision),
                "effective_revision": Revision(local=binding.config_revision),
                "state": state,
            }
        )

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Session:
        binding = self._c.binding(scope, ref)
        if self.events is None:
            raise RuntimeError("events port is not connected")
        snapshot = await self.events.reconcile(scope, ref)
        return self._session(ref, binding, snapshot.state).model_copy(
            update={
                "active_root_turn": snapshot.active_root_turn,
                "required_actions": snapshot.required_actions,
            }
        )

    async def list(
        self, scope: Scope, *, filters: SessionFilter, page: PageRequest
    ) -> Page[Session]:
        raise self._c.unsupported("session_listing")

    async def plan_update(self, scope: Scope, ref: ResourceRef, desired: SessionSpec) -> UpdatePlan:
        self._c.binding(scope, ref)
        return UpdatePlan(
            session=ref,
            expected_revision=Revision(local=desired.config_revision),
            action="refuse",
            unmet=("session_update",),
        )

    async def apply_update(
        self, scope: Scope, plan: UpdatePlan, *, expected: Revision, key: str
    ) -> UpdateReceipt:
        self._c.binding(scope, plan.session)
        raise self._c.unsupported("session_update")

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._c.binding(scope, ref)
        raise self._c.unsupported("session_archive")

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._c.binding(scope, ref)
        raise self._c.unsupported("session_delete")

    async def export(
        self, scope: Scope, ref: ResourceRef, *, requested: ExportRequirements, key: str
    ) -> SessionExport:
        self._c.binding(scope, ref)
        raise self._c.unsupported("workspace_export_import")

    async def restore(
        self,
        scope: Scope,
        export: SessionExport,
        target: SessionSpec,
        *,
        accept_losses: frozenset[str],
        key: str,
    ) -> RestoreReceipt:
        self._c.check(scope, export.source, "session")
        raise self._c.unsupported("workspace_export_import")

    async def migrate(
        self, scope: Scope, ref: ResourceRef, target: ConfigRevision, *, expected: int, key: str
    ) -> Session:
        self._c.binding(scope, ref)
        raise MigrationUnsupported(ref.id)
