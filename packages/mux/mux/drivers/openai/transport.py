"""Documented Agents HTTP resources over the pinned SDK's public primitives."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Literal, Protocol
from urllib.parse import quote

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AsyncOpenAI,
    AsyncStream,
    RequestOptions,
)
from pydantic import JsonValue, TypeAdapter

from mux.contracts.errors import ProviderErrorCategory
from mux.errors import ProviderError

Object = dict[str, JsonValue]
_OBJECT = TypeAdapter(Object)
Method = Literal["GET", "POST", "DELETE"]


def object_json(value: object) -> Object:
    return _OBJECT.validate_python(value, strict=True)


def segment(value: str) -> str:
    if not value:
        raise ValueError("empty resource identity")
    return quote(value, safe="")


class Transport(Protocol):
    async def request(
        self,
        method: Method,
        path: str,
        *,
        body: Object | None = None,
        query: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> Object: ...
    async def open_stream(self, path: str) -> AsyncIterator[Object]: ...


NATIVE_ERROR_CATEGORIES: dict[str, ProviderErrorCategory] = {
    "authentication_error": "auth",
    "permission_denied": "permission",
    "resource_not_found": "not_found",
    "invalid_request": "invalid_request",
    "rate_limit_exceeded": "rate_limited",
    "server_overloaded": "overloaded",
    "flex_unavailable": "overloaded",
    "connection_failed": "transient_network",
    "request_timeout": "transient_network",
    "server_error": "upstream",
    "internal_error": "upstream",
}


def error_category(code: object) -> ProviderErrorCategory:
    return NATIVE_ERROR_CATEGORIES.get(code, "upstream") if isinstance(code, str) else "upstream"


def provider_error(error: APIError | httpx.HTTPError) -> ProviderError:
    if isinstance(error, APIConnectionError | httpx.HTTPError):
        return ProviderError("transient_network", retryable=True)
    if not isinstance(error, APIStatusError):
        category = error_category(error.code or error.type)
        return ProviderError(
            category, retryable=category in ("rate_limited", "overloaded", "transient_network")
        )
    status = error.status_code
    categories: dict[int, ProviderErrorCategory] = {
        400: "invalid_request",
        401: "auth",
        403: "permission",
        404: "not_found",
        408: "transient_network",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
        503: "overloaded",
    }
    category = categories.get(status, "upstream")
    # Never retain exception bodies or provider messages: they can contain credentials.
    return ProviderError(
        category, retryable=status in (408, 429) or status >= 500, native_code=str(status)
    )


class _Stream(AsyncIterator[Object]):
    def __init__(self, source: AsyncStream[Object]) -> None:
        self._source = source
        self._iterator = source.__aiter__()
        self._closed = False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Object:
        if self._closed:
            raise StopAsyncIteration
        try:
            return object_json(await self._iterator.__anext__())
        except (APIConnectionError, APIError, APIStatusError, httpx.HTTPError) as error:
            await self.aclose()
            raise provider_error(error) from None
        except (ValueError, TypeError):
            await self.aclose()
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_event"
            ) from None
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._source.close()


class SDKTransport:
    """Private SDK ownership; no automatic retries of uncertain mutations.

    SDK 2.54 does not include generated beta.agents bindings. These public
    HTTP methods invoke the documented Agents API, never Responses or Agents SDK.
    The injected client remains owned by its creator; streams close independently.
    """

    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client.with_options(max_retries=0)

    async def request(
        self,
        method: Method,
        path: str,
        *,
        body: Object | None = None,
        query: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> Object:
        headers = {"OpenAI-Beta": "agents=v1"}
        if key is not None:
            headers["Idempotency-Key"] = key
        options: RequestOptions = {
            "headers": headers,
            "params": dict(query or {}),
        }
        try:
            if method == "POST":
                response = await self._client.post(
                    path, cast_to=httpx.Response, body=body, options=options
                )
            elif method == "DELETE":
                response = await self._client.delete(path, cast_to=httpx.Response, options=options)
            else:
                response = await self._client.get(path, cast_to=httpx.Response, options=options)
            if not response.content:
                return {}
            return object_json(response.json())
        except (APIConnectionError, APIError, APIStatusError, httpx.HTTPError) as error:
            raise provider_error(error) from None
        except (ValueError, TypeError):
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_response"
            ) from None

    async def open_stream(self, path: str) -> AsyncIterator[Object]:
        try:
            source = await self._client.get(
                path,
                cast_to=Object,
                stream=True,
                stream_cls=AsyncStream[Object],
                options={
                    "headers": {"OpenAI-Beta": "agents=v1", "Accept": "text/event-stream"},
                    "params": {"stream": "true"},
                },
            )
            return _Stream(source)
        except (APIConnectionError, APIError, APIStatusError, httpx.HTTPError) as error:
            raise provider_error(error) from None
