"""Documented Agents HTTP resources over the pinned SDK's public primitives."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from typing import IO, Literal, Protocol, cast
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
    async def multipart(
        self,
        path: str,
        *,
        files: tuple[tuple[str, str, bytes | IO[bytes], str], ...],
        fields: Mapping[str, str],
        key: str,
    ) -> Object: ...
    async def download(self, path: str) -> AsyncIterator[bytes]: ...


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


def _native_error_details(error: APIStatusError) -> dict[str, str | None]:
    """Bounded diagnostics; raw bodies, bearer values and URLs never become facts."""
    body = error.body
    if not isinstance(body, dict):
        return {}
    body = cast(dict[str, object], body)
    candidate = body.get("error", body)
    if not isinstance(candidate, dict):
        return {}
    raw = cast(dict[str, object], candidate)
    secrets: set[str] = set()
    try:
        request = error.response.request
        for name, value in request.headers.items():
            if name.lower() in ("authorization", "x-api-key", "cookie"):
                secrets.add(value)
                if value.lower().startswith("bearer "):
                    secrets.add(value[7:])

        def collect(value: object, private: bool = False) -> None:
            if isinstance(value, dict):
                for key, child in cast(dict[object, object], value).items():
                    collect(
                        child,
                        private
                        or any(
                            part in str(key).lower()
                            for part in (
                                "authorization",
                                "token",
                                "secret",
                                "password",
                                "api_key",
                                "headers",
                            )
                        ),
                    )
            elif isinstance(value, list):
                for child in cast(list[object], value):
                    collect(child, private)
            elif private and isinstance(value, str) and value:
                secrets.add(value)
                if value.lower().startswith("bearer "):
                    secrets.add(value[7:])

        if len(request.content) <= 1024 * 1024:
            collect(json.loads(request.content))
    except (RuntimeError, ValueError, httpx.RequestNotRead):
        pass

    def safe(value: object, *, identifier: bool = False) -> str | None:
        if not isinstance(value, str):
            return None
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        value = re.sub(
            r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]+|Bearer\s+[^\s\"']+",
            "[redacted]",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"https?://[^\s\"']+", "[redacted URL]", value)
        if identifier and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,128}", value) is None:
            return None
        return " ".join(value.split())[:512]

    return {
        "native_message": safe(raw.get("message")),
        "native_error_code": safe(raw.get("code"), identifier=True),
        "native_param": safe(raw.get("param"), identifier=True),
    }


@contextmanager
def suppress_mcp_error_details(enabled: bool = True) -> Iterator[None]:
    """MCP errors can echo credentials from an earlier vault request.

    No pattern can safely identify an opaque bearer or partial credential. Keep
    only the normalized category/status in this flow, including cleanup errors.
    """
    try:
        yield
    except ProviderError as error:
        if enabled:
            error.native_message = None
            error.native_error_code = None
            error.native_param = None
        raise


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
    # Keep only bounded/redacted diagnostic fields; never retain the raw body.
    return ProviderError(
        category,
        retryable=status in (408, 429) or status >= 500,
        native_code=str(status),
        **_native_error_details(error),
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

    async def multipart(
        self,
        path: str,
        *,
        files: tuple[tuple[str, str, bytes | IO[bytes], str], ...],
        fields: Mapping[str, str],
        key: str,
    ) -> Object:
        try:
            response = await self._client.post(
                path,
                cast_to=httpx.Response,
                body=dict(fields),
                files=[
                    (field, (name, data, media_type)) for field, name, data, media_type in files
                ],
                options={
                    "headers": {
                        "OpenAI-Beta": "agents=v1",
                        "Content-Type": "multipart/form-data",
                        "Idempotency-Key": key,
                    }
                },
            )
            return object_json(response.json())
        except (APIError, httpx.HTTPError) as error:
            raise provider_error(error) from None
        except (ValueError, TypeError):
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_response"
            ) from None

    async def download(self, path: str) -> AsyncIterator[bytes]:
        try:
            # cast_to=httpx.Response retains the raw streamed binary response;
            # the installed public SDK path is checked by offline HTTP tests.
            response: object = await self._client.get(
                path,
                cast_to=httpx.Response,
                stream=True,
                stream_cls=AsyncStream[bytes],
                options={"headers": {"OpenAI-Beta": "agents=v1"}},
            )
            if not isinstance(response, httpx.Response):
                raise ProviderError(
                    "upstream", retryable=False, native_code="invalid_binary_response"
                )
            return _Bytes(response)
        except (APIError, httpx.HTTPError) as error:
            raise provider_error(error) from None


class _Bytes(AsyncIterator[bytes]):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._iterator = response.aiter_bytes(chunk_size=65536)
        self._closed = False

    def __aiter__(self) -> _Bytes:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._iterator.__anext__()
        except httpx.HTTPError as error:
            await self.aclose()
            raise provider_error(error) from None
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._response.aclose()
