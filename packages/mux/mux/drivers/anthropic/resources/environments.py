"""Anthropic environment port; config omission and metadata patches survive."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment
from anthropic.types.beta.environment_create_params import EnvironmentCreateParams
from anthropic.types.beta.environment_list_params import EnvironmentListParams
from anthropic.types.beta.environment_update_params import EnvironmentUpdateParams

from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.receipts import DeletionReceipt, Operation
from mux.contracts.resources import (
    Environment,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
)
from mux.drivers.anthropic.resources._errors import provider_call
from mux.drivers.anthropic.resources.agents import check_ref
from mux.drivers.anthropic.schemas import EnvironmentConfig
from mux.errors import UnsupportedCapability


def environment_payload(spec: EnvironmentSpec | EnvironmentPatch) -> dict[str, object]:
    fields = spec.model_fields_set
    result: dict[str, object] = {}
    for name in ("name", "description", "scope", "metadata"):
        value = getattr(spec, name)
        if name in fields and (value is not None or isinstance(spec, EnvironmentPatch)):
            result[name] = dict(value) if name == "metadata" and value is not None else value
    if spec.sources:
        raise UnsupportedCapability(("environment_sources",), "anthropic.managed_agents")
    if "native_config" in fields:
        native = spec.native_config
        if native is None:
            result["config"] = None
        else:
            if native.namespace != "anthropic.environment_config" or native.version != 1:
                raise ValueError("expected anthropic.environment_config@1")
            result.update(
                EnvironmentConfig.model_validate(dict(native.value)).model_dump(
                    mode="json", exclude_unset=True
                )
            )
    if "network" in fields and spec.network is not None:
        network: dict[str, object] = {"type": spec.network.mode}
        if spec.network.mode == "limited":
            network["allowed_hosts"] = list(spec.network.allowed_hosts)
        result["config"] = {"type": "cloud", "networking": network}
    if "packages" in fields and spec.packages is not None:
        config = cast(dict[str, object], result.setdefault("config", {"type": "cloud"}))
        config["packages"] = {name: list(packages) for name, packages in spec.packages.items()}
    return result


def environment_record(item: BetaEnvironment, account_scope_id: str) -> Environment:
    return Environment.model_validate(
        {
            "ref": {
                "id": item.id,
                "kind": "environment",
                "provider": "anthropic",
                "account_scope_id": account_scope_id,
            },
            "revision": {"local": 0},
            "native": item.model_dump(mode="json", exclude_unset=True),
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "archived_at": item.archived_at,
            "lifecycle": "archived" if item.archived_at is not None else "active",
            "spec": {
                "name": item.name,
                "description": item.description,
                "metadata": item.metadata,
                "scope": item.scope,
                "execution": "self_hosted" if item.config.type == "self_hosted" else "hosted",
                "native_config": {
                    "namespace": "anthropic.environment_config",
                    "version": 1,
                    "value": {"config": item.config.model_dump(mode="json", exclude_unset=True)},
                },
            },
        }
    )


class AnthropicEnvironments:
    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id

    async def create(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment:
        item = await provider_call(
            self._client.beta.environments.create(
                **cast(EnvironmentCreateParams, environment_payload(spec))
            )
        )
        return environment_record(item, self._account_scope_id)

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Environment:
        check_ref(ref, self._account_scope_id, "environment")
        return environment_record(
            await provider_call(self._client.beta.environments.retrieve(ref.id)),
            self._account_scope_id,
        )

    async def list(
        self, scope: Scope, *, filters: EnvironmentFilter, page: PageRequest
    ) -> Page[Environment]:
        kwargs: dict[str, object] = {"include_archived": filters.include_archived}
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.order is not None:
            raise UnsupportedCapability(("environment_list_order",), "anthropic.managed_agents")
        result = await provider_call(
            self._client.beta.environments.list(**cast(EnvironmentListParams, kwargs))
        )
        return Page(
            data=tuple(
                environment_record(item, self._account_scope_id)
                for item in result.data
                if filters.name is None or item.name == filters.name
            ),
            next_cursor=result.next_page,
            has_more=bool(result.next_page),
        )

    async def update(
        self,
        scope: Scope,
        ref: ResourceRef,
        patch: EnvironmentPatch,
        *,
        expected: Revision,
        key: str,
    ) -> Environment:
        check_ref(ref, self._account_scope_id, "environment")
        item = await provider_call(
            self._client.beta.environments.update(
                ref.id, **cast(EnvironmentUpdateParams, environment_payload(patch))
            )
        )
        return environment_record(item, self._account_scope_id)

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        check_ref(ref, self._account_scope_id, "environment")
        await provider_call(self._client.beta.environments.archive(ref.id))
        now = datetime.now(UTC)
        digest = hashlib.sha256(
            json.dumps({"archive_environment": ref.id}, sort_keys=True).encode()
        ).hexdigest()
        return Operation(
            id=key,
            key=key,
            request_digest=digest,
            status="processed",
            resource=ref,
            created_at=now,
            updated_at=now,
        )

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        check_ref(ref, self._account_scope_id, "environment")
        await provider_call(self._client.beta.environments.delete(ref.id))
        return DeletionReceipt(operation_id=key, deleted=(ref,))
