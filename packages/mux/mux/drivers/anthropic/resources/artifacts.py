"""Native files, using the host's existing multipart upload and byte stream."""

import io
from collections.abc import AsyncIterator
from typing import Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from anthropic.types.beta.file_list_params import FileListParams
from pydantic import JsonValue

from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.ports import Artifacts as CoreArtifacts
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import Artifact
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_ref,
)
from mux.drivers.anthropic.resources._errors import provider_call
from mux.drivers.anthropic.resources._secrets import (
    CredentialFileUpload,
    SecretResolver,
    credential_request,
    unavailable_secret,
)
from mux.errors import UnsupportedCapability


class Artifacts(CoreArtifacts, Protocol):
    async def upload_credential_file(
        self, scope: Scope, config: CredentialFileUpload, *, key: str
    ) -> Artifact: ...


class AnthropicArtifacts:
    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
        secrets: SecretResolver | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization
        self._secrets = secrets or unavailable_secret

    def _ref(self, scope: Scope, kind: str, native_id: str) -> ResourceRef:
        return ResourceRef(
            id=native_id,
            kind=kind,
            provider="anthropic",
            account_scope_id=self._account_scope_id,
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
        )

    def _artifact(self, scope: Scope, item: FileMetadata) -> Artifact:
        session = (
            self._ref(scope, "session", item.scope.id)
            if item.scope is not None and item.scope.type == "session"
            else None
        )
        return Artifact(
            ref=self._ref(scope, "file", item.id),
            filename=item.filename,
            media_type=item.mime_type,
            size_bytes=item.size_bytes,
            created_at=item.created_at,
            session=session,
            native=cast(JsonValue, item.model_dump(mode="json", exclude_unset=True)),
        )

    async def upload(
        self, scope: Scope, body: AsyncIterator[bytes], *, filename: str, media_type: str, key: str
    ) -> Artifact:
        authorize(self._authorization, scope, "file")
        content = b"".join([chunk async for chunk in body])
        item = await provider_call(
            self._client.beta.files.upload(file=(filename, content, media_type))
        )
        return self._artifact(scope, item)

    async def upload_credential_file(
        self, scope: Scope, config: CredentialFileUpload, *, key: str
    ) -> Artifact:
        authorize(self._authorization, scope, "file")

        async def send(kwargs: dict[str, object]) -> FileMetadata:
            return await self._client.beta.files.upload(
                file=(
                    cast(str, kwargs["filename"]),
                    io.BytesIO(cast(bytes, kwargs["content"])),
                    cast(str, kwargs["media_type"]),
                )
            )

        return self._artifact(scope, await credential_request(scope, config, self._secrets, send))

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Artifact:
        authorize(self._authorization, scope, "file", ref.id)
        check_ref(scope, ref, self._account_scope_id, "file")
        return self._artifact(
            scope, await provider_call(self._client.beta.files.retrieve_metadata(ref.id))
        )

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest, turn_id: str | None = None
    ) -> Page[Artifact]:
        authorize(self._authorization, scope, "session", session.id)
        check_ref(scope, session, self._account_scope_id, "session")
        if turn_id is not None or page.order is not None:
            raise UnsupportedCapability(("file_turn_or_order_filter",), "anthropic.managed_agents")
        kwargs: FileListParams = {"scope_id": session.id}
        if page.cursor is not None:
            kwargs["after_id"] = page.cursor
        if page.limit is not None:
            kwargs["limit"] = page.limit
        result = await provider_call(self._client.beta.files.list(**kwargs))
        return Page(
            data=tuple(self._artifact(scope, item) for item in (result.data or ())),
            has_more=bool(result.has_more),
            next_cursor=result.last_id if result.has_more else None,
        )

    async def download(self, scope: Scope, ref: ResourceRef) -> AsyncIterator[bytes]:
        authorize(self._authorization, scope, "file", ref.id)
        check_ref(scope, ref, self._account_scope_id, "file")
        response = await provider_call(self._client.beta.files.download(ref.id))
        try:
            iterator = response.iter_bytes()
            while True:
                try:
                    yield await provider_call(anext(iterator))
                except StopAsyncIteration:
                    break
        finally:
            await provider_call(response.close())

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        authorize(self._authorization, scope, "file", ref.id)
        check_ref(scope, ref, self._account_scope_id, "file")
        await provider_call(self._client.beta.files.delete(ref.id))
        return DeletionReceipt(operation_id=key, deleted=(ref,))
