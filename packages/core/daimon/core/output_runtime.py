"""Where one session's output files live, per provider.

A runtime lists, reads and deletes the files an agent left for the user, on
whatever provider ran the session. The Anthropic runtime keeps the Files API
(the listing doubles as the delivery ledger); the others sit on the neutral
`Artifacts` port, whose Gemini driver keeps the ledger itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.output_ports_compat import (
    delete_output_record,
    list_output_records,
    read_output_record,
)
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.ports import Artifacts
from mux.errors import ProviderError, UnsupportedCapability

_log = structlog.get_logger(__name__)

_PAGE_LIMIT = 200
# A listing that never ends is a driver fault; stop rather than spin.
_MAX_PAGES = 50


@dataclass(frozen=True)
class SessionOutput:
    """One downloadable file in a session's outputs."""

    id: str
    filename: str
    mime_type: str
    size_bytes: int


class OutputRuntime(Protocol):
    """One session's outputs, bound to its provider and the turn's scope."""

    @property
    def session_id(self) -> str: ...

    @property
    def settles(self) -> bool:
        """Whether the provider's listing lags a write, so one poll is not final."""
        ...

    async def list_outputs(self) -> tuple[SessionOutput, ...]: ...

    async def read_output(self, output_id: str) -> bytes: ...

    async def delete_output(self, output_id: str) -> None:
        """Remove a delivered entry; a missing one is fine, a failure is logged."""
        ...


class AnthropicOutputRuntime:
    settles = True

    def __init__(self, client: AsyncAnthropic, session_id: str, *, scope: Scope) -> None:
        self._client = client
        self._scope = scope
        self.session_id = session_id

    async def list_outputs(self) -> tuple[SessionOutput, ...]:
        records = await list_output_records(
            self._client, self.session_id, all_pages=True, scope=self._scope
        )
        return tuple(
            SessionOutput(meta.id, meta.filename, meta.mime_type, meta.size_bytes)
            for meta in records
            if meta.downloadable is True
        )

    async def read_output(self, output_id: str) -> bytes:
        return await read_output_record(self._client, output_id, scope=self._scope)

    async def delete_output(self, output_id: str) -> None:
        try:
            await delete_output_record(self._client, output_id, scope=self._scope)
        except anthropic.NotFoundError:
            # An entry a concurrent sweep already removed is not an error.
            pass
        except anthropic.APIError as exc:
            _log.warning(
                "output_delivery.delete_failed",
                session_id=self.session_id,
                file_id=output_id,
                error=str(exc)[:300],
            )


class ArtifactOutputRuntime:
    """Any provider's session artifacts through the neutral port."""

    settles = False

    def __init__(self, artifacts: Artifacts, session: ResourceRef, *, scope: Scope) -> None:
        self._artifacts = artifacts
        self._session = session
        self._scope = scope
        self._refs: dict[str, ResourceRef] = {}
        self.session_id = session.id

    async def list_outputs(self) -> tuple[SessionOutput, ...]:
        found: list[SessionOutput] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = await self._artifacts.list(
                self._scope, self._session, page=PageRequest(cursor=cursor, limit=_PAGE_LIMIT)
            )
            for artifact in page.data:
                if artifact.size_bytes is None:
                    # Without a size the cap cannot apply and an empty file cannot be told apart.
                    _log.info("output_delivery.skipped_unsized", session_id=self.session_id)
                    continue
                self._refs[artifact.ref.id] = artifact.ref
                found.append(
                    SessionOutput(
                        artifact.ref.id, artifact.filename, artifact.media_type, artifact.size_bytes
                    )
                )
            if not page.has_more or page.next_cursor is None:
                break
            cursor = page.next_cursor
        return tuple(found)

    async def read_output(self, output_id: str) -> bytes:
        chunks = [
            chunk async for chunk in self._artifacts.download(self._scope, self._ref(output_id))
        ]
        return b"".join(chunks)

    async def delete_output(self, output_id: str) -> None:
        from uuid import uuid4

        try:
            await self._artifacts.delete(self._scope, self._ref(output_id), key=uuid4().hex)
        except ProviderError as exc:
            if exc.category == "not_found":
                return
            self._delete_failed(output_id, exc)
        except UnsupportedCapability as exc:
            self._delete_failed(output_id, exc)

    def _ref(self, output_id: str) -> ResourceRef:
        return self._refs[output_id]

    def _delete_failed(self, output_id: str, exc: Exception) -> None:
        _log.warning(
            "output_delivery.delete_failed",
            session_id=self.session_id,
            file_id=output_id,
            error=str(exc)[:300],
        )
