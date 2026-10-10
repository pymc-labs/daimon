"""Explicit request-owned Gemini SDK transport, with no ambient routing."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import cast

import httpx
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from pydantic import JsonValue

from mux.drivers.gemini.transport import Object, SDKTransport, Transport


class _OwnedStream(AsyncIterator[Object]):
    def __init__(self, source: AsyncIterator[Object], close: Callable[[], Awaitable[None]]) -> None:
        self._source, self._close, self._closed = source, close, False

    def __aiter__(self) -> _OwnedStream:
        return self

    async def __anext__(self) -> Object:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await anext(self._source)
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
            await self._close()


class _ConfiguredTransport:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[genai.Client]:
        async with httpx.AsyncClient(trust_env=False, timeout=60) as http:
            client = genai.Client(
                api_key=self._api_key,
                vertexai=False,
                http_options=HttpOptions(
                    base_url="https://generativelanguage.googleapis.com",
                    httpx_async_client=http,
                    headers={"x-goog-api-key": self._api_key},
                    retry_options=HttpRetryOptions(attempts=1),
                ),
            )
            try:
                yield client
            finally:
                try:
                    await client.aio.aclose()
                finally:
                    client.close()

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).create(request)

    async def get(self, interaction_id: str) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).get(interaction_id)

    async def cancel(self, interaction_id: str) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        async with self._client() as client:
            return await SDKTransport(client).download_snapshot(environment_id)

    async def open_stream(
        self,
        interaction_id: str,
        *,
        after: str | None = None,
    ) -> AsyncIterator[Object]:
        lifetime = self._client()
        client = await lifetime.__aenter__()
        try:
            source = await SDKTransport(client).open_stream(interaction_id, after=after)
        except BaseException:
            await lifetime.__aexit__(None, None, None)
            raise

        async def close() -> None:
            await lifetime.__aexit__(None, None, None)

        return _OwnedStream(source, close)


def configured_transport(*, api_key: str) -> Transport:
    if not api_key.strip():
        raise ValueError("explicit Gemini credential is required")
    return _ConfiguredTransport(api_key)
