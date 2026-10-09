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
