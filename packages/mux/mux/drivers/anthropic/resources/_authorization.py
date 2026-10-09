"""Host-authorized resource access, checked without provider lookups."""

from collections.abc import Mapping
from dataclasses import dataclass

from mux.contracts.ids import ResourceRef, Scope
from mux.errors import ScopeViolation


@dataclass(frozen=True)
class ResourceAuthorization:
    """One host authorization decision and its already-authorized native IDs.

    The host supplies these from tenant-scoped state or its existing ownership
    checks. This immutable decision is neither a resource cache nor a journal.
    """

    scope: Scope
    resources: frozenset[tuple[str, str]] = frozenset()

    def check(self, scope: Scope, kind: str, resource_id: str | None = None) -> None:
        if scope.is_platform or scope.is_legacy_host_authorized:
            return
        if scope != self.scope:
            raise ScopeViolation(resource_id or kind, "scope differs from host authorization")
        if resource_id is not None and (kind, resource_id) not in self.resources:
            raise ScopeViolation(resource_id, "resource was not authorized by the host")


def authorize(
    authorization: ResourceAuthorization | None,
    scope: Scope,
    kind: str,
    resource_id: str | None = None,
) -> None:
    if scope.is_platform or scope.is_legacy_host_authorized:
        return
    if authorization is None:
        raise ScopeViolation(resource_id or kind, "resource access requires host authorization")
    authorization.check(scope, kind, resource_id)


def check_ref(scope: Scope, ref: ResourceRef, account_scope_id: str, kind: str) -> None:
    if ref.provider != "anthropic" or ref.account_scope_id != account_scope_id or ref.kind != kind:
        raise ScopeViolation(
            ref.id, "reference does not belong to this provider workspace and kind"
        )
    if not (scope.is_platform or scope.is_legacy_host_authorized) and (
        ref.tenant_id != scope.tenant_id or ref.account_id != scope.account_id
    ):
        raise ScopeViolation(ref.id, "reference does not belong to this tenant and account")


def visible(scope: Scope, metadata: Mapping[str, object] | None) -> bool:
    if scope.is_platform or scope.is_legacy_host_authorized or metadata is None:
        return True
    tenant = metadata.get("daimon_tenant")
    return tenant is None or tenant == scope.tenant_id


def check_record(scope: Scope, resource_id: str, metadata: Mapping[str, object] | None) -> None:
    if not visible(scope, metadata):
        raise ScopeViolation(resource_id, "provider record belongs to another tenant")


def visible_skill(
    authorization: ResourceAuthorization | None, scope: Scope, skill_id: str, source: str | None
) -> bool:
    """Catalog skills are shared; custom skill visibility follows the host grant."""
    if scope.is_platform or scope.is_legacy_host_authorized:
        return True
    return source == "anthropic" or (
        source == "custom"
        and authorization is not None
        and ("skill", skill_id) in authorization.resources
    )


def visible_grant(
    authorization: ResourceAuthorization | None, scope: Scope, kind: str, resource_id: str
) -> bool:
    """Untagged organization resources need an explicit host ID grant."""
    return (
        scope.is_platform
        or scope.is_legacy_host_authorized
        or (authorization is not None and (kind, resource_id) in authorization.resources)
    )
