"""Explicit request-owned SDK transport; credentials are supplied by the host."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import IO, cast

from openai import AsyncOpenAI

from mux.drivers.openai.transport import Method, Object, SDKTransport, Transport


class _OwnedStream[T](AsyncIterator[T]):
    def __init__(self, source: AsyncIterator[T], client: AsyncOpenAI) -> None:
        self._source, self._client = source, client
        self._closed = False

    def __aiter__(self) -> _OwnedStream[T]:
        return self

    async def __anext__(self) -> T:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._source.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._source, "aclose", None)
            if callable(close):
                await cast(Callable[[], Awaitable[None]], close)()
        finally:
            await self._client.close()


class _ConfiguredTransport:
    def __init__(self, api_key: str, project: str) -> None:
        self._api_key, self._project = api_key, project

    def _client(self) -> AsyncOpenAI:
        # Explicit origin/project/key override SDK environment discovery. The
        # credential is not a client-global singleton shared between callers.
        return AsyncOpenAI(
            api_key=self._api_key,
            project=self._project,
            organization="",
            base_url="https://api.openai.com/v1",
            max_retries=0,
        )

    async def request(
        self,
        method: Method,
        path: str,
        *,
        body: Object | None = None,
        query: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).request(method, path, body=body, query=query, key=key)

    async def open_stream(self, path: str) -> AsyncIterator[Object]:
        client = self._client()
        try:
            source = await SDKTransport(client).open_stream(path)
        except BaseException:
            await client.close()
            raise
        return _OwnedStream(source, client)

    async def multipart(
        self,
        path: str,
        *,
        files: tuple[tuple[str, str, bytes | IO[bytes], str], ...],
        fields: Mapping[str, str],
        key: str,
    ) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).multipart(path, files=files, fields=fields, key=key)

    async def download(self, path: str) -> AsyncIterator[bytes]:
        client = self._client()
        try:
            source = await SDKTransport(client).download(path)
        except BaseException:
            await client.close()
            raise
        return _OwnedStream(source, client)


def configured_transport(*, api_key: str, project: str) -> Transport:
    """No I/O at construction; each request/stream owns its SDK client lifetime."""
    if not api_key.strip() or not project.strip():
        raise ValueError("explicit OpenAI credential and project are required")
    return _ConfiguredTransport(api_key, project)
