"""Files the agent saved under the harness workspace `outputs` directory."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import PurePosixPath

from pydantic import JsonValue, TypeAdapter, ValidationError

from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import Artifact
from mux.drivers.selfhosted import wire
from mux.drivers.selfhosted.context import Context
from mux.drivers.selfhosted.events import SelfHostedEvents, segment
from mux.errors import ProviderError, ScopeViolation

log = logging.getLogger(__name__)
# The 404 details of a harness without the output routes (direct, then invocations).
ROUTE_MISSING = frozenset({"Not Found", "Unknown C3 route"})
REPROBE_SECONDS = 600.0


def artifact_identity(session: str, output: str) -> str:
    raw = json.dumps([session, output], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def artifact_route(id_: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(id_ + "=" * (-len(id_) % 4))
        session, output = TypeAdapter(tuple[str, str]).validate_json(raw, strict=True)
    except (ValueError, binascii.Error):
        raise ValueError("reference is not a self-hosted output") from None
    if artifact_identity(session, output) != id_:
        raise ValueError("noncanonical output reference")
    return session, output


def malformed() -> ProviderError:
    return ProviderError("upstream", retryable=False, native_code="malformed_output")


class SelfHostedArtifacts:
    """Output files only: Daimon never uploads input files to the harness."""

    def __init__(self, context: Context, events: SelfHostedEvents) -> None:
        self._c, self._events = context, events
        self._unavailable_until = 0.0

    def _base(self, session: ResourceRef) -> str:
        return f"/v0/sessions/{segment(session.id)}/outputs"

    def _ref(self, session: ResourceRef, output: str) -> ResourceRef:
        return session.model_copy(
            update={"id": artifact_identity(session.id, output), "kind": "artifact"}
        )

    def _route(self, scope: Scope, ref: ResourceRef) -> tuple[ResourceRef, str]:
        """Authorize through the owning session; the ref must be exactly the one listed."""
        try:
            session_id, output = artifact_route(ref.id)
        except ValueError:
            raise ScopeViolation(ref.id, "not a self-hosted output reference") from None
        session = self._c.ref(scope, "session", session_id)
        self._c.check(scope, session, "session")
        if ref != self._ref(session, output):
            raise ScopeViolation(ref.id, "self-hosted output is outside authorized scope")
        return session, output

    async def _call(
        self,
        session: ResourceRef,
        method: str,
        path: str,
        body: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        return await self._c.transport.request(
            method,
            path,
            body=body,
            session_id=session.id,
            after_seq=await self._events.cursor(session),
        )

    def _artifact(self, session: ResourceRef, raw: wire.OutputFile) -> Artifact:
        filename = PurePosixPath(raw.path).name
        return Artifact(
            ref=self._ref(session, raw.id),
            filename=filename,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            size_bytes=raw.size_bytes,
            session=session,
            created_at=raw.modified_at,
            max_download_bytes=wire.OUTPUT_FILE_MAX_BYTES,
            native={"path": raw.path},
        )

    async def upload(
        self, scope: Scope, body: AsyncIterator[bytes], *, filename: str, media_type: str, key: str
    ) -> Artifact:
        raise self._c.unsupported("artifact_input")

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest, turn_id: str | None = None
    ) -> Page[Artifact]:
        self._c.check(scope, session, "session")
        if turn_id is not None:
            raise self._c.unsupported("file_turn_or_order_filter")
        if time.monotonic() < self._unavailable_until:
            return Page[Artifact](data=())
        try:
            raw = await self._call(session, "GET", self._base(session))
        except ProviderError as exc:
            if exc.category != "not_found":
                raise
            if exc.native_error_code in ROUTE_MISSING:
                # A harness older than 0.2.0 has no output routes: deliver nothing, and
                # ask again later in case it was upgraded in place.
                self._unavailable_until = time.monotonic() + REPROBE_SECONDS
                log.warning("selfhosted.outputs_unavailable", extra={"session": session.id})
            return Page[Artifact](data=())
        try:
            listing = wire.OutputsResponse.model_validate(raw)
        except ValidationError:
            raise malformed() from None
        return Page[Artifact](data=tuple(self._artifact(session, f) for f in listing.outputs))

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Artifact:
        session, _ = self._route(scope, ref)
        for artifact in (await self.list(scope, session, page=PageRequest())).data:
            if artifact.ref == ref:
                return artifact
        raise ProviderError("not_found", retryable=False, native_code="output_not_found")

    async def download(self, scope: Scope, ref: ResourceRef) -> AsyncIterator[bytes]:
        session, output = self._route(scope, ref)
        raw = await self._call(session, "GET", f"{self._base(session)}/{segment(output)}")
        try:
            content = wire.OutputContent.model_validate(raw)
            if content.id != output or content.size_bytes > wire.OUTPUT_FILE_MAX_BYTES:
                raise ValueError("wrong output")
            data = base64.b64decode(content.content_b64, validate=True)
        except (ValidationError, ValueError, binascii.Error):
            raise malformed() from None
        if len(data) != content.size_bytes:
            raise ProviderError(
                "transient_network", retryable=True, native_code="incomplete_output"
            )
        yield data

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        session, output = self._route(scope, ref)
        raw = await self._call(
            session, "POST", f"{self._base(session)}/{segment(output)}/delete", body={}
        )
        try:
            wire.OutputDeleted.model_validate(raw)
        except ValidationError:
            raise malformed() from None
        return DeletionReceipt(operation_id=key, deleted=(ref,))
