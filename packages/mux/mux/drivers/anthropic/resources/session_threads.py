"""Native session threads and budget: what a coordinator session's helpers need.

`anthropic.session_threads@1`. A multiagent session's primary event list carries
only the lead's own model calls; each helper's calls are listed under its thread
(`/sessions/{id}/threads/{thread}/events`). The host reads them there to bill
them, and archives a finished thread to free its place under the provider's
25-thread cap. Snapshots stay native: the host decodes them with the SDK types.

The same port reads and moves the session's native spending limit (`budget`):
Managed Agents attaches one only at creation and lets an existing one be raised.
The pinned SDK has no typed budget field, so it travels in the request body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, cast

from anthropic import AsyncAnthropic

from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_record,
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
    async def spent_cents(self, scope: Scope, session: ResourceRef) -> int | None: ...
    async def set_budget(self, scope: Scope, session: ResourceRef, *, max_cents: int) -> None: ...


if TYPE_CHECKING:
    from mux.drivers.anthropic import AnthropicManagedAgents


def session_threads(backend: AnthropicManagedAgents) -> SessionThreads:
    """The backend's `anthropic.session_threads@1` port."""
    return backend.extension(SessionThreads, namespace="anthropic.session_threads", version=1)


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

    async def spent_cents(self, scope: Scope, session: ResourceRef) -> int | None:
        """The session's reported list cost in whole cents; None when it has no budget."""
        self._check(scope, session)
        item = await provider_call(self._client.beta.sessions.retrieve(session.id))
        check_record(scope, session.id, item.metadata)
        native = native_snapshot(item).native
        if not isinstance(native.get("budget"), dict):
            return None
        usage = native.get("usage")
        cost = usage.get("list_cost") if isinstance(usage, dict) else None
        amount = cost.get("amount") if isinstance(cost, dict) else None
        if not isinstance(amount, str) or not amount.isdigit():
            return 0
        return int(amount)

    async def set_budget(self, scope: Scope, session: ResourceRef, *, max_cents: int) -> None:
        """Raise or lower an existing limit to `max_cents` (whole cents, at least 1)."""
        self._check(scope, session)
        budget = {
            "type": "limit",
            "max_list_cost": {"amount": str(max(1, max_cents)), "currency": "USD"},
        }
        await provider_call(
            self._client.beta.sessions.update(
                session.id, extra_body=cast(dict[str, object], {"budget": budget})
            )
        )


__all__ = ["AnthropicSessionThreads", "SessionThreads", "session_threads"]
