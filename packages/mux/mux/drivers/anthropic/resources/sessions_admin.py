"""Session mount administration and archive; no turns or event operations."""

from typing import Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import ResourceUpdateResponse
from anthropic.types.beta.sessions.beta_managed_agents_session_resource import (
    BetaManagedAgentsSessionResource,
)
from anthropic.types.beta.sessions.resource_add_params import ResourceAddParams
from pydantic import JsonValue

from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import SessionResources as CoreSessionResources
from mux.contracts.receipts import Operation, UpdateReceipt
from mux.contracts.resources import ResourceBinding
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_ref,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.resources._secrets import (
    SecretResolver,
    credential_request,
    unavailable_secret,
)
from mux.drivers.anthropic.resources.vaults import operation
from mux.drivers.anthropic.schemas import NativeConfig
from mux.errors import UnsupportedCapability


class RepoTokenUpdate(NativeConfig):
    authorization_token_ref: str


class SessionResources(CoreSessionResources, Protocol):
    async def archive(self, scope: Scope, session: ResourceRef, *, key: str) -> Operation: ...


class AnthropicSessionAdmin:
    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        secrets: SecretResolver | None = None,
        authorization: ResourceAuthorization | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._secrets = secrets or unavailable_secret
        self._authorization = authorization

    def _check(self, scope: Scope, session: ResourceRef) -> None:
        authorize(self._authorization, scope, "session", session.id)
        check_ref(scope, session, self._account_scope_id, "session")

    def _binding(self, scope: Scope, item: BetaManagedAgentsSessionResource) -> ResourceBinding:
        kind = (
            "artifact"
            if item.type == "file"
            else "repository"
            if item.type == "github_repository"
            else "native"
        )
        native_id = (
            item.file_id
            if item.type == "file"
            else item.memory_store_id
            if item.type == "memory_store"
            else item.id
        )
        return ResourceBinding(
            id=item.memory_store_id if item.type == "memory_store" else item.id,
            kind=kind,
            target_path=item.mount_path,
            resource=ResourceRef(
                id=native_id,
                kind="file" if item.type == "file" else item.type,
                provider="anthropic",
                account_scope_id=self._account_scope_id,
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            ),
            native=ExtensionConfig(
                namespace="anthropic.session_resource",
                version=1,
                value=cast(dict[str, JsonValue], item.model_dump(mode="json", exclude_unset=True)),
            ),
        )

    async def list(self, scope: Scope, session: ResourceRef) -> tuple[ResourceBinding, ...]:
        self._check(scope, session)
        return tuple(
            [
                self._binding(scope, item)
                async for item in provider_iter(
                    self._client.beta.sessions.resources.list(session.id)
                )
            ]
        )

    async def add(
        self, scope: Scope, session: ResourceRef, resource: ResourceBinding, *, key: str
    ) -> UpdateReceipt:
        self._check(scope, session)
        if resource.kind != "artifact" or resource.resource is None:
            raise UnsupportedCapability(
                ("session_add_non_file_resource",), "anthropic.managed_agents"
            )
        authorize(self._authorization, scope, "file", resource.resource.id)
        check_ref(scope, resource.resource, self._account_scope_id, "file")
        kwargs: ResourceAddParams = {"type": "file", "file_id": resource.resource.id}
        if "target_path" in resource.model_fields_set:
            kwargs["mount_path"] = resource.target_path
        await provider_call(self._client.beta.sessions.resources.add(session.id, **kwargs))
        return UpdateReceipt(operation_id=key, status="processed", applies="now", session=session)

    async def remove(
        self, scope: Scope, session: ResourceRef, resource_id: str, *, key: str
    ) -> UpdateReceipt:
        self._check(scope, session)
        await provider_call(
            self._client.beta.sessions.resources.delete(resource_id, session_id=session.id)
        )
        return UpdateReceipt(operation_id=key, status="processed", applies="now", session=session)

    async def rotate_repo_token(
        self, scope: Scope, session: ResourceRef, resource_id: str, credential_ref: str, *, key: str
    ) -> UpdateReceipt:
        self._check(scope, session)

        async def send(kwargs: dict[str, object]) -> ResourceUpdateResponse:
            return await self._client.beta.sessions.resources.update(
                resource_id,
                session_id=session.id,
                authorization_token=cast(str, kwargs["authorization_token"]),
            )

        await credential_request(
            scope, RepoTokenUpdate(authorization_token_ref=credential_ref), self._secrets, send
        )
        return UpdateReceipt(operation_id=key, status="processed", applies="now", session=session)

    async def archive(self, scope: Scope, session: ResourceRef, *, key: str) -> Operation:
        self._check(scope, session)
        await provider_call(self._client.beta.sessions.archive(session.id))
        return operation(session, key, "archive_session")
