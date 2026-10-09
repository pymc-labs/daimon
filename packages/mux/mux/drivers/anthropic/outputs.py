"""Session outputs use MA headers; standalone uploads keep the Files API default."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import JsonValue

from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import DeletionReceipt
from mux.drivers.anthropic.resources._authorization import authorize, check_ref
from mux.drivers.anthropic.resources._errors import provider_call
from mux.drivers.anthropic.resources.artifacts import AnthropicArtifacts

_MA_BETA = "managed-agents-2026-04-01"


@dataclass(frozen=True)
class OutputEntry:
    """Opaque listing entry; the existing host decides which fields it needs.

    Non-downloadable entries can omit all delivery metadata, including their
    identity. A downloadable entry need not include an unused creation time.
    """

    native: JsonValue


class Outputs(Protocol):
    async def upload_bundle(
        self, scope: Scope, body: AsyncIterator[bytes], *, filename: str, media_type: str, key: str
    ) -> str: ...

    async def list_outputs(self, scope: Scope, session: ResourceRef) -> tuple[OutputEntry, ...]: ...

    async def read_output(self, scope: Scope, ref: ResourceRef) -> bytes: ...

    async def delete_output(
        self, scope: Scope, ref: ResourceRef, *, key: str
    ) -> DeletionReceipt: ...


class AnthropicOutputs(AnthropicArtifacts):
    """One listing page and buffered reads, matching the existing host sweeps.

    Inherit N6's reference construction and shared upload request. Bundle
    rehosting consumes only its returned ID. The three session-output requests
    differ from the standalone Files API port; they explicitly opt into the MA beta.
    """

    async def upload_bundle(
        self, scope: Scope, body: AsyncIterator[bytes], *, filename: str, media_type: str, key: str
    ) -> str:
        """Return only the upload identity the existing rehost consumer uses."""
        item = await self._upload_file(scope, body, filename=filename, media_type=media_type)
        return item.id

    async def list_outputs(self, scope: Scope, session: ResourceRef) -> tuple[OutputEntry, ...]:
        authorize(self._authorization, scope, "session", session.id)
        check_ref(scope, session, self._account_scope_id, "session")
        page = await provider_call(
            self._client.beta.files.list(scope_id=session.id, betas=[_MA_BETA], limit=1000)
        )
        return tuple(
            OutputEntry(native=cast(JsonValue, item.model_dump(mode="json", exclude_unset=True)))
            for item in page.data
        )

    async def read_output(self, scope: Scope, ref: ResourceRef) -> bytes:
        authorize(self._authorization, scope, "file", ref.id)
        check_ref(scope, ref, self._account_scope_id, "file")
        response = await provider_call(self._client.beta.files.download(ref.id, betas=[_MA_BETA]))
        return await provider_call(response.read())

    async def delete_output(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        authorize(self._authorization, scope, "file", ref.id)
        check_ref(scope, ref, self._account_scope_id, "file")
        await provider_call(self._client.beta.files.delete(ref.id, betas=[_MA_BETA]))
        return DeletionReceipt(operation_id=key, deleted=(ref,))
