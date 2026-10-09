"""General input files and session-routed published artifacts."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile

from pydantic import JsonValue, TypeAdapter

from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import Artifact
from mux.drivers.openai._common import Context, owned, page_of, query, text, timestamp
from mux.drivers.openai.transport import Object, segment
from mux.errors import ProviderError


def file_identity(id_: str) -> str:
    if not id_.startswith("file:") or not id_[5:]:
        raise ValueError("reference is not an uploaded input file")
    return id_[5:]


def artifact_identity(session: str, artifact: str) -> str:
    wire = json.dumps([session, artifact], separators=(",", ":")).encode()
    return "session:" + base64.urlsafe_b64encode(wire).decode().rstrip("=")


def artifact_route(id_: str) -> tuple[str, str]:
    if not id_.startswith("session:"):
        raise ValueError("reference is not a session artifact")
    wire = id_[8:]
    value = TypeAdapter(tuple[str, str]).validate_json(
        base64.b64decode(wire + "=" * (-len(wire) % 4), altchars=b"-_", validate=True), strict=True
    )
    session, artifact = text(value[0]), text(value[1])
    if artifact_identity(session, artifact) != id_:
        raise ValueError("noncanonical artifact route")
    return session, artifact


def size(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid byte size")
    return value


class OpenAIArtifacts:
    def __init__(self, context: Context) -> None:
        self._c = context
        self._resource = context.transport

    def _file(self, scope: Scope, raw: Object) -> Artifact:
        filename = text(raw["filename"])
        return Artifact(
            ref=self._c.ref(scope, "artifact", "file:" + text(raw["id"])),
            filename=filename,
            media_type="application/octet-stream",  # Native metadata does not identify MIME.
            size_bytes=size(raw["bytes"]),
            created_at=timestamp(raw["created_at"]),
        )

    def _artifact(self, scope: Scope, session: ResourceRef, raw: Object) -> Artifact:
        if raw.get("session_id") != session.id:
            raise ValueError("artifact belongs to another session")
        filename = PurePosixPath(text(raw["path"])).name
        return Artifact(
            ref=self._c.ref(scope, "artifact", artifact_identity(session.id, text(raw["id"]))),
            filename=filename,
            media_type="application/octet-stream",  # Native metadata does not identify MIME.
            size_bytes=size(raw["size_bytes"]),
            session=session,
            turn_id=text(raw["turn_id"]),
            created_at=timestamp(raw["created_at"]),
        )

    def _route(self, scope: Scope, ref: ResourceRef) -> tuple[str, ResourceRef | None, str]:
        self._c.check(scope, ref, "artifact")
        if ref.id.startswith("file:"):
            native_id = file_identity(ref.id)
            return "/files/" + segment(native_id), None, native_id
        session_id, native_id = artifact_route(ref.id)
        session = self._c.ref(scope, "session", session_id)
        self._c.check(scope, session, "session")
        return (
            f"/agents/sessions/{segment(session_id)}/artifacts/{segment(native_id)}",
            session,
            native_id,
        )

    @owned
    async def upload(
        self, scope: Scope, body: AsyncIterator[bytes], *, filename: str, media_type: str, key: str
    ) -> Artifact:
        self._c.authorize(scope, "artifact")
        if (
            not filename
            or PurePosixPath(filename).name != filename
            or "\\" in filename
            or "\x00" in filename
        ):
            raise ValueError("invalid filename")
        size = 0
        with SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as file:
            async for chunk in body:
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise self._c.unsupported("provider_file_size_limit")
                file.write(chunk)
            file.seek(0)
            raw = await self._resource.multipart(
                "/files",
                files=(("file", filename, file, media_type),),
                fields={"purpose": "user_data"},
                key=key,
            )
        result = self._file(scope, raw)
        if result.size_bytes != size:
            raise ValueError("wrong uploaded size")
        return result.model_copy(update={"media_type": media_type})

    @owned
    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Artifact:
        path, session, native_id = self._route(scope, ref)
        if session is not None:
            current = await self._c.call("GET", "/agents/sessions/" + segment(session.id))
            if current.get("id") != session.id:
                raise ValueError("wrong parent session")
            self._c.record(scope, current)
        raw = await self._c.call("GET", path)
        if raw.get("id") != native_id:
            raise ValueError("wrong artifact identity")
        return self._file(scope, raw) if session is None else self._artifact(scope, session, raw)

    @owned
    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest, turn_id: str | None = None
    ) -> Page[Artifact]:
        self._c.check(scope, session, "session")
        current = await self._c.call("GET", "/agents/sessions/" + segment(session.id))
        if current.get("id") != session.id:
            raise ValueError("wrong parent session")
        self._c.record(scope, current)
        params = query(page)
        raw = await self._c.call(
            "GET", f"/agents/sessions/{segment(session.id)}/artifacts", params=params
        )
        result = page_of(raw, lambda value: self._artifact(scope, session, value))
        return (
            result
            if turn_id is None
            else result.model_copy(
                update={"data": tuple(value for value in result.data if value.turn_id == turn_id)}
            )
        )

    async def download(self, scope: Scope, ref: ResourceRef) -> AsyncIterator[bytes]:
        try:
            metadata = await self.retrieve(scope, ref)
            path, _, _ = self._route(scope, ref)
            source = await self._resource.download(path + "/content")
            received = 0
            try:
                async for chunk in source:
                    received += len(chunk)
                    if metadata.size_bytes is not None and received > metadata.size_bytes:
                        raise ProviderError(
                            "transient_network", retryable=True, native_code="oversized_download"
                        )
                    yield chunk
                if metadata.size_bytes is not None and received != metadata.size_bytes:
                    raise ProviderError(
                        "transient_network", retryable=True, native_code="incomplete_download"
                    )
            finally:
                closer = getattr(source, "aclose", None)
                if closer is not None:
                    await closer()
        except (ValueError, KeyError, TypeError):
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_artifact"
            ) from None

    @owned
    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        path, _, _ = self._route(scope, ref)
        # Read the scoped parent and native artifact identity before deleting.
        await self.retrieve(scope, ref)
        await self._c.call("DELETE", path, key=key)
        return DeletionReceipt(operation_id=key, deleted=(ref,))
