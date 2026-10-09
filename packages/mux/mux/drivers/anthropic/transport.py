"""Private legacy turn transport during the dual-path rollout.

This is deliberately outside the neutral ports: its SDK values and exceptions
belong only to the temporary host compatibility edge. It preserves the old
request arguments, timeout policy and stream lifetime exactly.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
from anthropic import AsyncAnthropic, AsyncStream
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsStreamSessionEvents,
)


class LegacyTurnTransport:
    def __init__(self, client: AsyncAnthropic, session_id: str) -> None:
        self._client = client
        self._session_id = session_id

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        await self._client.beta.sessions.events.send(self._session_id, events=events)

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
        await self._client.beta.sessions.archive(self._session_id)
