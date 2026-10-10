"""Vault metadata and write-only host-resolved MCP credentials."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.receipts import Operation
from mux.contracts.resources import CredentialBinding, CredentialInfo, Vault
from mux.drivers.openai._common import Context, objects, owned, page_of, query, revision, text
from mux.drivers.openai.secret_value import RedactedCredential
from mux.drivers.openai.transport import Object, object_json, segment
from mux.errors import ProviderError, ScopeViolation

SecretResolver = Callable[[Scope, str], Awaitable[str]]


def metadata_credential(raw: Object, vault_id: str) -> CredentialInfo:
    if raw.get("vault_id") != vault_id:
        raise ValueError("credential belongs to another vault")
    auth = object_json(raw["auth"])
    kind = text(auth["type"])
    # Build the revision exclusively from non-secret fields. Faulty upstream
    # extras, echoed tokens and refresh secrets must not enter a record/digest.
    public: Object = {
        "id": raw["id"],
        "name": raw["name"],
        "type": kind,
        "mcp_server_url": auth.get("mcp_server_url"),
        "created_at": raw["created_at"],
    }
    return CredentialInfo.model_validate(
        {
            "id": text(raw["id"]),
            "name": text(raw["name"]),
            "kind": "environment" if kind == "environment_variable" else kind,
            "revision": revision(public),
        }
    )


class OpenAIVaults:
    def __init__(self, context: Context, secrets: SecretResolver | None) -> None:
        self._c, self._secrets = context, secrets

    def _decode(self, scope: Scope, raw: Object) -> Vault:
        self._c.record(scope, raw)
        metadata = object_json(raw.get("metadata") or {})
        if (
            not scope.is_platform
            and metadata.get("mux_account", scope.account_id) != scope.account_id
        ):
            raise ScopeViolation(text(raw["id"]), "foreign account vault")
        public: Object = {"id": raw["id"], "name": raw["name"], "metadata": metadata}
        return Vault(
            ref=self._c.ref(scope, "vault", text(raw["id"])),
            name=text(raw["name"]),
            revision=revision(public),
        )

    async def _retrieve(self, scope: Scope, vault: ResourceRef) -> Vault:
        self._c.check(scope, vault, "vault")
        raw = await self._c.call("GET", "/vaults/" + segment(vault.id))
        if raw.get("id") != vault.id:
            raise ValueError("wrong vault identity")
        return self._decode(scope, raw)

    @owned
    async def list(self, scope: Scope, *, page: PageRequest) -> Page[Vault]:
        self._c.authorize(scope, "vault")
        raw = await self._c.call("GET", "/vaults", params=query(page))
        result = page_of(raw, lambda value: value)
        values: list[Vault] = []
        for value in result.data:
            metadata = object_json(value.get("metadata") or {})
            if not scope.is_platform and (
                metadata.get("mux_tenant") != scope.tenant_id
                or metadata.get("mux_account") != scope.account_id
            ):
                continue
            try:
                self._c.authorize(scope, "vault", text(value["id"]))
            except ScopeViolation:
                continue
            values.append(self._decode(scope, value))
        return Page(data=tuple(values), has_more=result.has_more, next_cursor=result.next_cursor)

    @owned
    async def ensure(self, scope: Scope, name: str, *, key: str) -> Vault:
        self._c.authorize(scope, "vault")
        name = name.strip()
        if not 1 <= len(name.encode()) <= 256:
            raise ValueError("invalid vault name")
        cursor: str | None = None
        seen: set[str] = set()
        found: Vault | None = None
        while True:
            page = await self.list(scope, page=PageRequest(cursor=cursor, limit=100, order="asc"))
            matches = [vault for vault in page.data if vault.name == name]
            if len(matches) > 1 or (matches and found is not None):
                raise ProviderError("conflict", retryable=False, native_code="ambiguous_vault_name")
            if matches:
                found = matches[0]
            if page.next_cursor is None:
                break
            if page.next_cursor in seen:
                raise ValueError("vault pagination loop")
            cursor = page.next_cursor
            seen.add(cursor)
        if found is not None:
            return found
        body: Object = {
            "name": name,
            "metadata": {"mux_tenant": scope.tenant_id, "mux_account": scope.account_id or ""},
        }
        return self._decode(scope, await self._c.call("POST", "/vaults", body=body, key=key))

    @owned
    async def credentials(self, scope: Scope, vault: ResourceRef) -> tuple[CredentialInfo, ...]:
        await self._retrieve(scope, vault)
        result: list[CredentialInfo] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            raw = await self._c.call(
                "GET",
                f"/vaults/{segment(vault.id)}/credentials",
                params={"limit": 100, **({"after": cursor} if cursor else {})},
            )
            values = objects(raw["data"])
            result.extend(metadata_credential(value, vault.id) for value in values)
            if raw["has_more"] is False:
                return tuple(result)
            if raw["has_more"] is not True:
                raise ValueError("invalid credential page")
            cursor = text(raw["last_id"])
            if cursor in seen:
                raise ValueError("credential pagination loop")
            seen.add(cursor)

    @owned
    async def put(
        self,
        scope: Scope,
        vault: ResourceRef,
        credential: CredentialBinding,
        *,
        expected: Revision | None,
        key: str,
    ) -> CredentialInfo:
        self._c.check(scope, vault, "vault")
        if expected is not None:
            raise self._c.unsupported("vault_credential_conditional_update")
        if credential.kind == "environment":
            raise self._c.unsupported("environment_credential_destination_policy")
        url = urlsplit(credential.mcp_server_url or "")
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            raise self._c.unsupported("credential_https_mcp_destination")
        name = credential.name.strip()
        if not 1 <= len(name.encode()) <= 256:
            raise ValueError("invalid credential name")
        if self._secrets is None:
            raise self._c.unsupported("host_secret_resolver")
        await self._retrieve(scope, vault)
        try:
            secret = await self._secrets(scope, credential.credential_ref)
        except Exception:
            raise ProviderError(
                "permission", retryable=False, native_code="secret_unavailable"
            ) from None
        if not secret:
            raise ProviderError("permission", retryable=False, native_code="secret_unavailable")
        auth: Object = {
            "type": credential.kind,
            "mcp_server_url": credential.mcp_server_url,
            "token" if credential.kind == "static_bearer" else "access_token": RedactedCredential(
                secret
            ),
        }
        raw = await self._c.call(
            "POST",
            f"/vaults/{segment(vault.id)}/credentials",
            body={"name": name, "auth": auth},
            key=key,
        )
        return metadata_credential(raw, vault.id)

    @owned
    async def remove(
        self, scope: Scope, vault: ResourceRef, credential_id: str, *, key: str
    ) -> Operation:
        await self._retrieve(scope, vault)
        path = f"/vaults/{segment(vault.id)}/credentials/{segment(credential_id)}"
        raw = await self._c.call("GET", path)
        if raw.get("id") != credential_id:
            raise ValueError("wrong credential identity")
        metadata_credential(raw, vault.id)
        await self._c.call("DELETE", path, key=key)
        now = datetime.now(UTC)
        digest = hashlib.sha256(
            json.dumps(
                {"vault": vault.id, "credential": credential_id, "action": "delete"}, sort_keys=True
            ).encode()
        ).hexdigest()
        return Operation(
            id=key,
            key=key,
            request_digest=digest,
            status="processed",
            resource=vault,
            created_at=now,
            updated_at=now,
        )

    @owned
    async def archive(self, scope: Scope, vault: ResourceRef, *, key: str) -> Operation:
        self._c.check(scope, vault, "vault")
        raise self._c.unsupported("vault_archive")
