"""Private SDK boundary. Construction and importing this module perform no I/O."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol, cast

import httpx
from google import genai
from google.genai._interactions import APIConnectionError, APIError, APIStatusError
from pydantic import JsonValue

from mux.contracts.errors import ProviderErrorCategory
from mux.errors import ProviderError

API_REVISION = "2026-05-20"
type Object = dict[str, JsonValue]


class Transport(Protocol):
    async def create(self, request: Mapping[str, JsonValue]) -> Object: ...
    async def get(self, interaction_id: str) -> Object: ...
    async def cancel(self, interaction_id: str) -> Object: ...
    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]: ...


def object_value(value: JsonValue) -> Object:
    if not isinstance(value, dict):
        raise ValueError("expected a provider object")
    return value


def string(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected a provider identity")
    return value


def normalize_error(error: Exception) -> ProviderError:
    status = error.status_code if isinstance(error, APIStatusError) else None
    categories: dict[int, ProviderErrorCategory] = {
        400: "invalid_request",
        401: "auth",
        403: "permission",
        404: "not_found",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
        503: "overloaded",
    }
    if isinstance(error, APIConnectionError | httpx.HTTPError):
        return ProviderError("transient_network", retryable=True)
    category = categories.get(status or 0, "upstream")
    return ProviderError(
        category, retryable=status == 429 or (status is not None and status >= 500)
    )


async def provider_call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except (APIError, httpx.HTTPError) as error:
        raise normalize_error(error) from None


class OwnedStream[T](AsyncIterator[T]):
    """Close the opened connection even if iteration never begins."""

    def __init__(self, source: AsyncIterator[T], close: Callable[[], Awaitable[None]]) -> None:
        self._source, self._close, self._closed = source, close, False

    def __aiter__(self) -> OwnedStream[T]:
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
        if not self._closed:
            self._closed = True
            close = getattr(self._source, "aclose", None)
            if close is not None:
                await close()
            await self._close()


async def close_iterator(source: AsyncIterator[object]) -> None:
    close = getattr(source, "aclose", None)
    if close is not None:
        await close()


class SDKTransport:
    """Caller supplies a no-retry SDK client; no keys or raw SDK values leave this boundary.

    The pinned SDK supports `extra_body` for newer Antigravity configuration.
    Interactions retries are disabled here; the host also configures the
    supplied base client's HttpRetryOptions(attempts=1).
    """

    def __init__(self, client: genai.Client) -> None:
        # google-genai 2.7 forwards attempts as Stainless max_retries, so
        # attempts=1 still retries once. Set the Interactions edge to zero.
        nextgen = client.aio._nextgen_client  # pyright: ignore[reportPrivateUsage]
        self._interactions = nextgen.with_options(max_retries=0).interactions

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        response = await provider_call(
            self._interactions.create(
                agent=string(request["agent"]),
                input=[],
                background=True,
                store=True,
                extra_body=dict(request),
                extra_headers={"Api-Revision": API_REVISION},
            )
        )
        return object_value(cast(JsonValue, response.model_dump(mode="json", exclude_none=True)))

    async def get(self, interaction_id: str) -> Object:
        response = await provider_call(
            self._interactions.get(
                id=interaction_id,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )
        return object_value(cast(JsonValue, response.model_dump(mode="json", exclude_none=True)))

    async def cancel(self, interaction_id: str) -> Object:
        response = await provider_call(
            self._interactions.cancel(
                id=interaction_id,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )
        return object_value(cast(JsonValue, response.model_dump(mode="json", exclude_none=True)))

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        from google.genai._interactions import omit

        source = await provider_call(
            self._interactions.get(
                id=interaction_id,
                stream=True,
                last_event_id=after if after is not None else omit,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )

        async def records() -> AsyncIterator[Object]:
            try:
                async for event in source:
                    yield object_value(
                        cast(JsonValue, event.model_dump(mode="json", exclude_none=True))
                    )
            except (APIError, httpx.HTTPError) as error:
                raise normalize_error(error) from None
            finally:
                await source.close()

        return OwnedStream(records(), source.close)
