"""Environment port maps reusable hosted templates, never self-hosted compute."""

from __future__ import annotations

from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.receipts import DeletionReceipt, Operation
from mux.contracts.resources import (
    Environment,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
)
from mux.drivers.openai._common import Context, owned, page_of, query, revision, text, timestamp
from mux.drivers.openai.transport import Object, object_json, segment


class OpenAIEnvironments:
    def __init__(self, context: Context) -> None:
        self._c = context

    def _decode(self, scope: Scope, raw: Object) -> Environment:
        network = object_json(raw.get("network") or {})
        native_mode = network.get("access")
        mode = {"enabled": "unrestricted", "disabled": "none", "restricted": "limited"}.get(
            text(native_mode) if native_mode is not None else ""
        )
        spec = EnvironmentSpec.model_validate(
            {
                "name": raw.get("name") or "",
                "network": {"mode": mode, "allowed_hosts": network.get("allowed_domains", [])}
                if mode is not None
                else None,
                "packages": raw.get("packages"),
            }
        )
        return Environment(
            ref=self._c.ref(scope, "environment", text(raw["id"])),
            revision=revision(raw),
            spec=spec,
            created_at=timestamp(raw["created_at"]),
        )

    @owned
    async def create(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment:
        self._c.authorize(scope, "environment")
        if (
            spec.execution != "hosted"
            or spec.description is not None
            or spec.scope is not None
            or spec.sources
            or spec.metadata
            or spec.native_config is not None
        ):
            raise self._c.unsupported("environment_configuration")
        body: Object = {"name": spec.name}
        if spec.sources is not None:
            body["files"] = []
        if spec.packages is not None:
            if set(spec.packages) - {"python", "npm", "system"}:
                raise self._c.unsupported("environment_packages")
            body["packages"] = {k: list(v) for k, v in spec.packages.items()}
        if spec.network is not None:
            body["network"] = {
                "access": {"none": "disabled", "limited": "restricted", "unrestricted": "enabled"}[
                    spec.network.mode
                ],
                "allowed_domains": list(spec.network.allowed_hosts),
            }
        return self._decode(
            scope, await self._c.call("POST", "/agents/environments/templates", body=body)
        )

    @owned
    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Environment:
        self._c.check(scope, ref, "environment")
        raw = await self._c.call("GET", "/agents/environments/templates/" + segment(ref.id))
        if raw.get("id") != ref.id:
            raise ValueError("wrong environment identity")
        return self._decode(scope, raw)

    @owned
    async def list(
        self, scope: Scope, *, filters: EnvironmentFilter, page: PageRequest
    ) -> Page[Environment]:
        self._c.authorize(scope, "environment")
        if not scope.is_platform:
            raise self._c.unsupported("tenant_environment_listing")
        if filters.name is not None or filters.include_archived:
            raise self._c.unsupported("environment_filters")
        return page_of(
            await self._c.call("GET", "/agents/environments/templates", params=query(page)),
            lambda item: self._decode(scope, item),
        )

    @owned
    async def update(
        self,
        scope: Scope,
        ref: ResourceRef,
        patch: EnvironmentPatch,
        *,
        expected: Revision,
        key: str,
    ) -> Environment:
        self._c.check(scope, ref, "environment")
        raise self._c.unsupported("environment_conditional_update")

    @owned
    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._c.check(scope, ref, "environment")
        raise self._c.unsupported("environment_archive")

    @owned
    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._c.check(scope, ref, "environment")
        await self._c.call("DELETE", "/agents/environments/templates/" + segment(ref.id))
        return DeletionReceipt(operation_id=key, deleted=(ref,))
