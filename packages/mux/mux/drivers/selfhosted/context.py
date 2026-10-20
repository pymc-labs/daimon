"""Explicit account authorization and durable host binding injection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import Environment, ProviderBinding, SessionSpec
from mux.drivers.selfhosted.transport import Transport
from mux.errors import ScopeViolation, UnsupportedCapability

Authorization = Callable[[Scope, str, str | None], bool]
BindingLookup = Callable[[Scope, str], ProviderBinding | None]


@dataclass
class Context:
    transport: Transport
    account_scope_id: str
    agents: Agents
    environments: Environments
    authorization: Authorization
    binding_lookup: BindingLookup | None = None
    bindings: dict[ResourceRef, ProviderBinding] = field(
        default_factory=dict[ResourceRef, ProviderBinding]
    )
    specs: dict[ResourceRef, SessionSpec] = field(default_factory=dict[ResourceRef, SessionSpec])

    def check(self, scope: Scope, ref: ResourceRef, kind: str) -> None:
        if (
            ref.provider != "selfhosted"
            or ref.kind != kind
            or ref.account_scope_id != self.account_scope_id
            or ref.tenant_id != scope.tenant_id
            or (
                ref.account_id != scope.account_id
                and not (kind in ("agent", "environment") and ref.account_id is None)
            )
            or not self.authorization(scope, kind, ref.id)
        ):
            raise ScopeViolation(ref.id, "self-hosted resource is outside authorized scope")

    def ref(self, scope: Scope, kind: str, id_: str) -> ResourceRef:
        return ResourceRef(
            id=id_,
            kind=kind,
            provider="selfhosted",
            account_scope_id=self.account_scope_id,
            tenant_id=scope.tenant_id,
            account_id=scope.account_id if kind == "session" else None,
        )

    def binding(self, scope: Scope, ref: ResourceRef) -> ProviderBinding:
        self.check(scope, ref, "session")
        binding = self.bindings.get(ref)
        if binding is None and self.binding_lookup is not None:
            binding = self.binding_lookup(scope, ref.id)
        if binding is None or (
            binding.provider != "selfhosted"
            or binding.profile != "selfhosted.loop"
            or binding.native_refs.get("session") != ref.id
            or binding.thread.channel.tenant_id != scope.tenant_id
            or binding.legacy_account_id != scope.account_id
        ):
            raise ScopeViolation(ref.id, "self-hosted session requires its authorized host binding")
        return binding

    def check_environment(self, environment: Environment) -> None:
        spec = environment.spec
        if (
            spec.execution != "self_hosted"
            or spec.sources
            or spec.packages
            or (
                spec.network is not None
                and (spec.network.mode != "unrestricted" or spec.network.allowed_hosts)
            )
        ):
            raise self.unsupported("environment_execution_policy")

    def unsupported(self, name: str) -> UnsupportedCapability:
        return UnsupportedCapability((name,), "selfhosted.loop")
