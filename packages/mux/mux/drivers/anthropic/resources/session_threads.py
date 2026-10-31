"""Native session threads: the child threads a coordinator session's helpers run in.

`anthropic.session_threads@1`. A multiagent session's primary event list carries
only the lead's own model calls; each helper's calls are listed under its thread
(`/sessions/{id}/threads/{thread}/events`). The host reads them there to bill
them, and archives a finished thread to free its place under the provider's
25-thread cap. Snapshots stay native: the host decodes them with the SDK types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from anthropic import AsyncAnthropic

from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_ref,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.resources._native import NativeSnapshot, native_snapshot


class SessionThreads(Protocol):
    def threads(self, scope: Scope, session: ResourceRef) -> AsyncIterator[NativeSnapshot]: ...
    def thread_events(
        self, scope: Scope, session: ResourceRef, thread_id: str
    ) -> AsyncIterator[NativeSnapshot]: ...
    async def archive_thread(self, scope: Scope, session: ResourceRef, thread_id: str) -> None: ...


class AnthropicSessionThreads:
    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization

    def _check(self, scope: Scope, ref: ResourceRef) -> None:
        authorize(self._authorization, scope, "session", ref.id)
        check_ref(scope, ref, self._account_scope_id, "session")

    async def threads(self, scope: Scope, session: ResourceRef) -> AsyncIterator[NativeSnapshot]:
        """Every thread of the session, the primary included, archived ones too."""
        self._check(scope, session)
        async for item in provider_iter(self._client.beta.sessions.threads.list(session.id)):
            yield native_snapshot(item)

    async def thread_events(
        self, scope: Scope, session: ResourceRef, thread_id: str
    ) -> AsyncIterator[NativeSnapshot]:
        """One thread's events, oldest first (the endpoint takes no type filter)."""
        self._check(scope, session)
        async for item in provider_iter(
            self._client.beta.sessions.threads.events.list(thread_id, session_id=session.id)
        ):
            yield native_snapshot(item)

    async def archive_thread(self, scope: Scope, session: ResourceRef, thread_id: str) -> None:
        self._check(scope, session)
        await provider_call(
            self._client.beta.sessions.threads.archive(thread_id, session_id=session.id)
        )


__all__ = ["AnthropicSessionThreads", "SessionThreads"]
