from collections.abc import AsyncIterator

import httpx
import pytest
from anthropic import APIConnectionError, APIStatusError, APITimeoutError
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.errors import ProviderError


def status_error(status: int) -> APIStatusError:
    return APIStatusError(
        "original provider message",
        response=httpx.Response(
            status,
            request=httpx.Request("GET", "https://example.test/v1/agents"),
            headers={"request-id": "req_test"},
        ),
        body={"error": {"type": "test_error", "message": "secret should stay out"}},
    )


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (400, "invalid_request", False),
        (401, "auth", False),
        (403, "permission", False),
        (404, "not_found", False),
        (409, "conflict", True),
        (422, "invalid_request", False),
        (429, "rate_limited", True),
        (500, "upstream", True),
        (529, "overloaded", True),
    ],
)
async def test_status_errors_keep_cause_and_request_id(status, category, retryable):
    original = status_error(status)

    async def fail():
        raise original

    with pytest.raises(ProviderError) as caught:
        await provider_call(fail())
    error = caught.value
    assert error.category == category
    assert error.retryable is retryable
    assert error.native_code == "test_error"
    assert error.operation_id == "req_test"
    assert error.__cause__ is original
    assert "secret" not in str(error)
    assert "original provider message" not in str(error)


@pytest.mark.parametrize("error_type", [APIConnectionError, APITimeoutError])
async def test_network_failure(error_type):
    original = error_type(request=httpx.Request("GET", "https://example.test"))

    async def fail():
        raise original

    with pytest.raises(ProviderError) as caught:
        await provider_call(fail())
    assert caught.value.category == "transient_network"
    assert caught.value.retryable
    assert caught.value.__cause__ is original


async def test_later_page_failure_is_normalized():
    original = status_error(429)

    async def pages() -> AsyncIterator[str]:
        yield "first"
        raise original

    items = provider_iter(pages())
    assert await anext(items) == "first"
    with pytest.raises(ProviderError) as caught:
        await anext(items)
    assert caught.value.__cause__ is original
    assert caught.value.category == "rate_limited"
