"""Offline scripted transport and storage, explicitly test-only; never SDK discovery."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy

from pydantic import JsonValue

from mux.drivers.gemini.storage import Records
from mux.drivers.gemini.transport import Object, OwnedStream
from mux.errors import ProviderError


class MemoryStorage:
    """Test-only transactional storage; retained data survives a driver restart."""

    def __init__(self) -> None:
        self._records = Records()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Records]:
        async with self._lock:
            snapshot = deepcopy(self._records)
            yield snapshot
            self._records = snapshot


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[Object] = []
        self.responses: list[Object | ProviderError] = []
        self.saved: dict[str, Object] = {}
        self.streams: dict[str, list[Object]] = {}
        self.cancelled: list[str] = []
        self.stream_closed = False

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        self.requests.append(deepcopy(dict(request)))
        if not self.responses:
            raise AssertionError("unscripted provider mutation")
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        self.saved[str(response["id"])] = deepcopy(response)
        return deepcopy(response)

    async def get(self, interaction_id: str) -> Object:
        if interaction_id not in self.saved:
            raise ProviderError("not_found", retryable=False)
        return deepcopy(self.saved[interaction_id])

    async def cancel(self, interaction_id: str) -> Object:
        self.cancelled.append(interaction_id)
        return await self.get(interaction_id)

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        async def events() -> AsyncIterator[Object]:
            try:
                skipping = after is not None
                for raw in self.streams.get(interaction_id, []):
                    if skipping:
                        if raw.get("event_id") == after:
                            skipping = False
                        continue
                    yield deepcopy(raw)
            finally:
                self.stream_closed = True

        async def close() -> None:
            self.stream_closed = True

        return OwnedStream(events(), close)
