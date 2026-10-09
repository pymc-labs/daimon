"""Normalize SDK failures, including errors raised while consuming pages."""

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Mapping
from typing import cast

from anthropic import AnthropicError, APIConnectionError, APIStatusError

from mux.errors import ProviderError, ProviderErrorCategory

_STATUS_CATEGORIES: dict[int, ProviderErrorCategory] = {
    400: "invalid_request",
    401: "auth",
    403: "permission",
    404: "not_found",
    409: "conflict",
    422: "invalid_request",
    429: "rate_limited",
    529: "overloaded",
}


def normalize_error(error: AnthropicError) -> ProviderError:
    """Keep the SDK exception as the cause for the legacy host error edge.

    The normalized error contains neither credential material nor arbitrary
    provider messages. A status error's request id identifies the failed call.
    """
    if isinstance(error, APIStatusError):
        code: str | None = None
        error_body: object = error.body
        if isinstance(error_body, Mapping):
            body = cast(Mapping[str, object], error_body)
            detail = body.get("error")
            detail_body = (
                cast(Mapping[str, object], detail) if isinstance(detail, Mapping) else body
            )
            native_type = detail_body.get("type")
            if isinstance(native_type, str):
                code = native_type
        return ProviderError(
            _STATUS_CATEGORIES.get(error.status_code, "upstream"),
            retryable=error.status_code in {408, 409, 429} or error.status_code >= 500,
            native_code=code,
            operation_id=error.request_id,
        )
    return ProviderError(
        "transient_network" if isinstance(error, APIConnectionError) else "upstream",
        retryable=isinstance(error, APIConnectionError),
    )


async def provider_call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except AnthropicError as error:
        raise normalize_error(error) from error


async def provider_iter[T](items: AsyncIterable[T]) -> AsyncIterator[T]:
    try:
        async for item in items:
            yield item
    except AnthropicError as error:
        raise normalize_error(error) from error
