"""Private legacy turn transport during the dual-path rollout.

This is deliberately outside the neutral ports: its SDK values and exceptions
belong only to the temporary host compatibility edge. It preserves the old
request arguments, timeout policy and stream lifetime exactly.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

import httpx
from anthropic import AsyncAnthropic, AsyncStream
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsStreamSessionEvents,
)

from mux.contracts.ids import Scope
from mux.errors import ScopeViolation

_CLAIMED_MUTATION: ContextVar[bool] = ContextVar("anthropic_claimed_mutation", default=False)


@contextmanager
def single_attempt_mutation() -> Iterator[None]:
    """A durable claim permits one native attempt, independently of read retries."""
    token = _CLAIMED_MUTATION.set(True)
    try:
        yield
    finally:
        _CLAIMED_MUTATION.reset(token)


def mutation_client(client: AsyncAnthropic) -> AsyncAnthropic:
    """Do not mutate the shared client or weaken unrelated tasks/read requests."""
    return client.with_options(max_retries=0) if _CLAIMED_MUTATION.get() else client


class LegacyTurnTransport:
    def __init__(
        self, client: AsyncAnthropic, session_id: str, *, scope: Scope | None = None
    ) -> None:
        if scope is not None and (
            scope.is_platform
            or scope.is_legacy_host_authorized
            or not scope.tenant_id.strip()
            or not scope.account_id.strip()
        ):
            raise ScopeViolation(session_id, "turn transport requires a real tenant/account scope")
        self._client = client
        self._session_id = session_id
        # The host binds this identity from its existing admitted session.
        # Legacy raw/operator entry points still omit it during the rollout;
        # no lookup or wire argument is added to obtain an identity.
        self.scope = scope

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        await mutation_client(self._client).beta.sessions.events.send(
            self._session_id, events=events
        )

    async def status(self) -> str:
        return (await self._client.beta.sessions.retrieve(self._session_id)).status

    async def open_stream(
        self, *, read_timeout_s: float
    ) -> AsyncStream[BetaManagedAgentsStreamSessionEvents]:
        return await self._client.beta.sessions.events.stream(
            session_id=self._session_id,
            timeout=httpx.Timeout(read_timeout_s, connect=5.0),
        )

    async def latest_idle_is_settled(self) -> bool:
        """Main's exact filtered history proof for confirmation recovery."""
        async for event in self._client.beta.sessions.events.list(
            session_id=self._session_id,
            types=["session.status_idle"],
            order="desc",
            limit=1,
        ):
            stop_reason = getattr(event, "stop_reason", None)
            return getattr(stop_reason, "type", None) != "requires_action"
        return False

    async def replay(self) -> list[BetaManagedAgentsSessionEvent]:
        events: list[BetaManagedAgentsSessionEvent] = []
        async for event in self._client.beta.sessions.events.list(session_id=self._session_id):
            events.append(event)
        return events

    async def open_interrupt_stream(self) -> AsyncStream[BetaManagedAgentsStreamSessionEvents]:
        # Interrupt waiting historically uses the client's default timeout,
        # unlike the live turn stream's explicit read timeout.
        return await self._client.beta.sessions.events.stream(session_id=self._session_id)

    async def archive(self) -> None:
        await mutation_client(self._client).beta.sessions.archive(self._session_id)
