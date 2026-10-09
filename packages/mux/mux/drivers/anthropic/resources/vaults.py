"""Vault metadata and reference-only credential writes for the native extension."""

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from anthropic.types.beta.vault_list_params import VaultListParams
from anthropic.types.beta.vaults.beta_managed_agents_credential import BetaManagedAgentsCredential
from anthropic.types.beta.vaults.credential_create_params import CredentialCreateParams
from anthropic.types.beta.vaults.credential_update_params import CredentialUpdateParams
from pydantic import BaseModel, JsonValue

from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.ports import Vaults as CoreVaults
from mux.contracts.receipts import Operation
from mux.contracts.resources import CredentialBinding, CredentialInfo, Vault
from mux.drivers.anthropic.credential_schemas import CredentialCreate, CredentialUpdate
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_record,
    check_ref,
    visible_grant,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.resources._secrets import (
    SecretResolver,
    credential_request,
    unavailable_secret,
)
from mux.errors import UnsupportedCapability


class VaultRecord(Vault):
    created_at: datetime
    native: JsonValue


class CredentialRecord(CredentialInfo):
    native: JsonValue


class Vaults(CoreVaults, Protocol):
    def walk(self, scope: Scope) -> AsyncIterator[VaultRecord]: ...
    async def create(self, scope: Scope, display_name: str, *, key: str) -> VaultRecord: ...
    def credential_walk(
        self, scope: Scope, vault: ResourceRef
    ) -> AsyncIterator[CredentialRecord]: ...
    async def create_credential(
        self, scope: Scope, vault: ResourceRef, config: CredentialCreate, *, key: str
    ) -> CredentialRecord: ...
    async def update_credential(
        self,
        scope: Scope,
        vault: ResourceRef,
        credential_id: str,
        config: CredentialUpdate,
        *,
        key: str,
    ) -> CredentialRecord: ...


def operation(ref: ResourceRef, key: str, action: str) -> Operation:
    now = datetime.now(UTC)
    digest = hashlib.sha256(
        json.dumps({"action": action, "ref": ref.model_dump(mode="json")}, sort_keys=True).encode()
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


class AnthropicVaults:
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

    def _vault(self, scope: Scope, item: BetaManagedAgentsVault) -> VaultRecord:
        check_record(scope, item.id, item.metadata)
        return VaultRecord(
            ref=ResourceRef(
                id=item.id,
                kind="vault",
                provider="anthropic",
                account_scope_id=self._account_scope_id,
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            ),
            name=item.display_name,
            revision=Revision(local=0),
            created_at=item.created_at,
            native=cast(
                JsonValue,
                item.model_dump(
                    mode="json", include=set(type(item).model_fields), exclude_unset=True
                ),
            ),
        )

    def _credential(self, item: BetaManagedAgentsCredential) -> CredentialRecord:
        # Response auth schemas contain metadata only; explicitly exclude any
        # unknown response extras, so a secret echoed by a faulty upstream
        # cannot become a stored record or repr.
        def snapshot(model: BaseModel) -> dict[str, JsonValue]:
            result = cast(
                dict[str, JsonValue],
                model.model_dump(
                    mode="json", include=set(type(model).model_fields), exclude_unset=True
                ),
            )
            for name in tuple(result):
                child: object = getattr(model, name)
                if isinstance(child, BaseModel):
                    result[name] = snapshot(child)
            return result

        native = snapshot(item)
        kind = "environment" if item.auth.type == "environment_variable" else item.auth.type
        return CredentialRecord(
            id=item.id,
            name=item.display_name or item.id,
            kind=kind,
            revision=Revision(local=0),
            native=cast(JsonValue, native),
        )

    async def walk(self, scope: Scope) -> AsyncIterator[VaultRecord]:
        authorize(self._authorization, scope, "vault")
        async for item in provider_iter(self._client.beta.vaults.list()):
            if visible_grant(self._authorization, scope, "vault", item.id):
                yield self._vault(scope, item)

    async def create(self, scope: Scope, display_name: str, *, key: str) -> VaultRecord:
        authorize(self._authorization, scope, "vault")
        return self._vault(
            scope, await provider_call(self._client.beta.vaults.create(display_name=display_name))
        )

    async def ensure(self, scope: Scope, name: str, *, key: str) -> Vault:
        found: list[VaultRecord] = []
        async for vault in self.walk(scope):
            if vault.name == name:
                found.append(vault)
        if found:
            # Host callers retain their own locking and canonical selection;
            # generic ensure chooses the oldest provider-created record.
            return min(found, key=lambda vault: vault.created_at)
        return await self.create(scope, name, key=key)

    async def list(self, scope: Scope, *, page: PageRequest) -> Page[Vault]:
        authorize(self._authorization, scope, "vault")
        kwargs: dict[str, object] = {}
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.order is not None:
            raise UnsupportedCapability(("vault_list_order",), "anthropic.managed_agents")
        result = await provider_call(self._client.beta.vaults.list(**cast(VaultListParams, kwargs)))
        return Page(
            data=tuple(
                self._vault(scope, item)
                for item in (result.data or ())
                if visible_grant(self._authorization, scope, "vault", item.id)
            ),
            next_cursor=result.next_page or None,
            has_more=bool(result.next_page),
        )

    async def credential_walk(
        self, scope: Scope, vault: ResourceRef
    ) -> AsyncIterator[CredentialRecord]:
        authorize(self._authorization, scope, "vault", vault.id)
        check_ref(scope, vault, self._account_scope_id, "vault")
        async for item in provider_iter(
            self._client.beta.vaults.credentials.list(vault_id=vault.id)
        ):
            yield self._credential(item)

    async def credentials(self, scope: Scope, vault: ResourceRef) -> tuple[CredentialInfo, ...]:
        return tuple([item async for item in self.credential_walk(scope, vault)])

    async def create_credential(
        self, scope: Scope, vault: ResourceRef, config: CredentialCreate, *, key: str
    ) -> CredentialRecord:
        authorize(self._authorization, scope, "vault", vault.id)
        check_ref(scope, vault, self._account_scope_id, "vault")
        item = await credential_request(
            scope,
            config,
            self._secrets,
            lambda kwargs: self._client.beta.vaults.credentials.create(
                vault_id=vault.id, **cast(CredentialCreateParams, kwargs)
            ),
        )
        return self._credential(item)

    async def update_credential(
        self,
        scope: Scope,
        vault: ResourceRef,
        credential_id: str,
        config: CredentialUpdate,
        *,
        key: str,
    ) -> CredentialRecord:
        authorize(self._authorization, scope, "vault", vault.id)
        check_ref(scope, vault, self._account_scope_id, "vault")
        item = await credential_request(
            scope,
            config,
            self._secrets,
            lambda kwargs: self._client.beta.vaults.credentials.update(
                credential_id, **cast(CredentialUpdateParams, {"vault_id": vault.id, **kwargs})
            ),
        )
        return self._credential(item)

    async def put(
        self,
        scope: Scope,
        vault: ResourceRef,
        credential: CredentialBinding,
        *,
        expected: Revision | None,
        key: str,
    ) -> CredentialInfo:
        authorize(self._authorization, scope, "vault", vault.id)
        check_ref(scope, vault, self._account_scope_id, "vault")
        if (
            expected is not None
            or credential.kind != "static_bearer"
            or credential.mcp_server_url is None
        ):
            raise UnsupportedCapability(
                ("vault_conditional_or_native_credential",), "anthropic.managed_agents"
            )
        config = CredentialCreate.model_validate(
            {
                "display_name": credential.name,
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": credential.mcp_server_url,
                    "token_ref": credential.credential_ref,
                },
            }
        )
        return await self.create_credential(scope, vault, config, key=key)

    async def remove(
        self, scope: Scope, vault: ResourceRef, credential_id: str, *, key: str
    ) -> Operation:
        authorize(self._authorization, scope, "vault", vault.id)
        check_ref(scope, vault, self._account_scope_id, "vault")
        await provider_call(
            self._client.beta.vaults.credentials.delete(credential_id, vault_id=vault.id)
        )
        return operation(vault, key, "remove_credential")

    async def archive(self, scope: Scope, vault: ResourceRef, *, key: str) -> Operation:
        authorize(self._authorization, scope, "vault", vault.id)
        check_ref(scope, vault, self._account_scope_id, "vault")
        await provider_call(self._client.beta.vaults.archive(vault.id))
        return operation(vault, key, "archive_vault")
